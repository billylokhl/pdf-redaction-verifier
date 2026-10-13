"""redaction_verifier.views.ocr — rendered-page OCR (Apple Vision).

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §6 ("Views are the existing page readings and OCR, moved
[not wrapped] in Phase 2"). Behaviour is byte-identical to the code this
replaced; verify.py re-exports every name here so existing imports,
`verify.X` references and the CLI keep working unchanged. Three changes
since the move. In _vision_recognize_batch: the image handler's options
are a native NSDictionary, not a Python {} (macOS 27 compatibility), and
a reading counts only once its completion handler ran to the end, with
nothing allowed to escape a handler. And a line Vision reads vertically
is hard-finding eligible only when an upright re-read confirms it, since
Vision on macOS 27 returns a stack of upright characters as one line
read sideways (see _confirm_vertical).

The Vision/Foundation import below keeps its original degrade semantics:
an ORDINARY failure (missing package, corrupt pyobjc install) sets
_OCR_IMPORTS_OK = False so OCR degrades to an OCR_UNAVAILABLE warning
later (fail-closed: never exit 0, a silent clean verdict) instead of
crashing. `except Exception` is deliberately narrower than BaseException
here: SystemExit, KeyboardInterrupt and other BaseExceptions are NOT
caught, so they propagate up through `redaction_verifier.views` and
`redaction_verifier`'s own import — this package must never import
verify (one-way dependency), so it cannot call verify.py's own
`_fatal_import` itself. verify.py's single guarded re-export block
(`except BaseException as exc: _fatal_import("redaction_verifier", exc)`)
is what turns that into the same "print an [ERROR] line, exit 2" result
a dedicated guard here used to produce directly.

_OCR_IMPORTS_OK is the one source of truth for whether the OCR bridge is
available: verify.py re-exports it (`from redaction_verifier.views import
_OCR_IMPORTS_OK as _OCR_IMPORTS_OK`) rather than recomputing it, and
nothing reassigns that re-exported name afterward, so `verify.py`'s copy
and this module's own value never drift apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import pymupdf as fitz

from redaction_verifier.matching import normalize_string

OCR_DPI: int = 300

# How many of extract_ocr_text's readings are genuine reading orders
# (hard-finding eligible): Vision's reading with language correction on
# and off. The rest are reconstructions (manual review).
OCR_GENUINE_READINGS = 2

# Upright re-reads of vertical lines (_confirm_vertical), at most this
# many per page, retries included; a vertical line past the cap stays
# unconfirmed (manual review), so a page of sideways text cannot make
# the run unbounded.
# Each re-read is two Vision calls, both passes each, on a crop around
# the line: 70-150 ms for a vertical line, more for a slanted one, whose
# crop is a square around the whole line. Measured per page: sideways
# text about 8-11 s, text slanted near 45 degrees about a minute, against
# under half a second.
MAX_UPRIGHT_REREADS = 64
# The margin around a re-read box, in multiples of the line's thickness:
# a second, wider crop for a line the first did not confirm (Vision
# sometimes misreads a crop's last glyph, and not at every margin).
REREAD_PADS = (1.0, 3.0)
# Unconfirmed vertical lines unstacked and read as one line
# (_extra_readings), at most this many per page; past that, a line is
# still in the raw readings.
MAX_UNSTACKS = 16
# An unstacked strip larger than this many pixels is not read: a column
# of glyphs is small (nine 11 pt digits at 300 dpi: under 0.05 Mpx), and
# MuPDF refuses an overly large image (a box holding a photo and many
# thin rules, say).
MAX_UNSTACK_PIXELS = 4_000_000

try:
    import Vision
    from Foundation import NSData, NSDictionary

    _OCR_IMPORTS_OK = True
except Exception:  # pragma: no cover
    # Broad on purpose, but the design here is to degrade, not exit: any
    # ORDINARY failure importing the OCR bridge (missing package, or a
    # corrupt pyobjc install) means OCR is simply unavailable on this
    # machine. That already surfaces later as an OCR_UNAVAILABLE warning
    # (fail-closed: never exit 0, a silent clean verdict) rather than a
    # crash, so it is not itself an operational failure worth a stderr
    # message here. A SystemExit/KeyboardInterrupt/other BaseException is
    # NOT caught by this clause (Exception, not BaseException) and
    # propagates instead — see the module docstring.
    _OCR_IMPORTS_OK = False


@dataclass(frozen=True)
class Observation:
    """One line Vision recognized: its top candidate's string and three
    corners of its quadrilateral, as (x, y) in Vision's normalized image
    coordinates (0 to 1, origin bottom-left). top_left -> top_right runs
    along the reading direction, bottom_left -> top_left across the line."""

    text: str
    top_left: tuple[float, float]
    top_right: tuple[float, float]
    bottom_left: tuple[float, float]


# What a re-read, a turned reading or an unstacked line may raise without
# failing the page: Vision's own failure (_vision_recognize_batch raises
# RuntimeError) and MuPDF's (its errors subclass FzErrorBase, not
# RuntimeError). Anything else is a bug and fails the page (PAGE_FAILED).
_READ_FAILURES: tuple[type[BaseException], ...] = (RuntimeError, fitz.mupdf.FzErrorBase)


def _vision_recognize_batch(
    png_bytes: bytes,
) -> tuple[list[Observation], list[Observation]]:
    """OCR a PNG with Apple Vision in one pass: correction on AND off.

    Both requests share a single VNImageRequestHandler so the image is
    decoded once instead of twice.  Raises on any Vision-level failure,
    and unless both completion handlers ran to the end: a reading is
    only returned once Vision has demonstrably delivered it. Returns each
    pass's observations in Vision's order.
    """
    ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    results: dict[bool, list[Observation]] = {True: [], False: []}
    errors: dict[bool, list[str]] = {True: [], False: []}
    # Set only as a handler's last step: if performRequests ever reported
    # success without having run a request's handler to the end, that
    # reading would be empty and the page would pass as read with nothing
    # on it.
    completed: dict[bool, bool] = {True: False, False: False}

    def _make_handler(correction: bool) -> Callable[[Any, Any], None]:
        def handler(request: Any, error: Any) -> None:
            # With more than one request, Vision runs these handlers on its
            # own worker threads (seen on macOS 27; they finish before
            # performRequests returns). An exception escaping one is
            # uncaught on that thread and aborts the whole process, so
            # nothing may leave this function: any failure is recorded and
            # raised by the caller instead.
            try:
                if error:
                    errors[correction].append(str(error))
                    return
                observations = request.results()
                if observations is None:
                    # A page with no text gives an empty list; None means
                    # the request produced no results at all.
                    errors[correction].append("no results")
                    return
                for observation in observations:
                    candidates = observation.topCandidates_(1)
                    if candidates:
                        tl, tr, bl = (observation.topLeft(), observation.topRight(),
                                      observation.bottomLeft())
                        results[correction].append(Observation(
                            candidates[0].string(),
                            (tl.x, tl.y), (tr.x, tr.y), (bl.x, bl.y),
                        ))
                completed[correction] = True
            except BaseException as exc:  # even SystemExit: it would abort
                try:
                    errors[correction].append(f"completion handler raised {exc!r}")
                except BaseException:
                    pass  # completed stays False: "never completed"
        return handler

    request_on = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
        _make_handler(True)
    )
    request_on.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request_on.setUsesLanguageCorrection_(True)

    request_off = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
        _make_handler(False)
    )
    request_off.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request_off.setUsesLanguageCorrection_(False)

    # A native, empty NSDictionary — never a Python {} literal. PyObjC
    # hands a Python dict to Objective-C as an OC_PythonDictionary proxy,
    # whose removeObjectForKey: raises NSInvalidArgumentException ("key
    # does not exist") for an absent key, where NSMutableDictionary's is a
    # no-op. From macOS 27, Vision mutable-copies the handler's options and
    # removes the keys it consumes (VNImageOptionProperties, then
    # VNImageOptionCameraIntrinsics), so a Python {} made every page fail
    # there (PAGE_FAILED: the run never exits 0).
    image_handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        ns_data, NSDictionary.dictionary()
    )
    success, perform_error = image_handler.performRequests_error_(
        [request_on, request_off], None
    )
    if not success:
        raise RuntimeError(f"Apple Vision request failed: {perform_error}")
    for correction in (True, False):
        if errors[correction]:
            raise RuntimeError(f"Apple Vision error: {'; '.join(errors[correction])}")
        if not completed[correction]:
            raise RuntimeError(
                "Apple Vision: the request with language correction "
                f"{'on' if correction else 'off'} never completed"
            )

    return (results[True], results[False])


def _is_vertical(observation: Observation, width: int, height: int,
                 steeper_than: float = 45.0) -> bool:
    """Whether Vision read the observation more than *steeper_than*
    degrees from the image's horizontal axis, measured in pixels of the
    *width* x *height* image so its aspect ratio cannot skew it."""
    (tlx, tly), (trx, tr_y) = observation.top_left, observation.top_right
    rise, run = abs((tr_y - tly) * height), abs((trx - tlx) * width)
    return rise > run * math.tan(math.radians(steeper_than))


def _box(observation: Observation, width: int, height: int, pad: float) -> fitz.IRect:
    """The observation's quadrilateral in image pixels (origin top-left),
    padded by *pad* times its thickness plus a margin, clipped to the image."""
    corners = [(x * width, (1 - y) * height) for x, y in (
        observation.top_left, observation.top_right, observation.bottom_left,
        (observation.top_right[0] + observation.bottom_left[0] - observation.top_left[0],
         observation.top_right[1] + observation.bottom_left[1] - observation.top_left[1]))]
    rect = fitz.Rect(min(x for x, _ in corners), min(y for _, y in corners),
                     max(x for x, _ in corners), max(y for _, y in corners))
    # The line's own thickness, not the box's short side: for a slanted
    # line the two differ by up to the line's length.
    (tlx, tly), (blx, bly) = corners[0], corners[2]
    margin = pad * math.hypot(tlx - blx, tly - bly) + 2 * OCR_DPI / 72
    rect = fitz.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin)
    return (rect & fitz.Rect(0, 0, width, height)).round()


def _upright_reads(image: fitz.Page, box: fitz.IRect) -> tuple[list[str], list[str]]:
    """Vision's horizontal lines in *box* of the rendered page *image*
    turned 90 and 270 degrees, per pass (correction on, off)."""
    reads: tuple[list[str], list[str]] = ([], [])
    for degrees in (90, 270):
        pix = image.get_pixmap(matrix=fitz.Matrix(1, 1).prerotate(degrees), clip=box,
                               colorspace=fitz.csGRAY)
        width, height = pix.width, pix.height
        passes = _vision_recognize_batch(pix.tobytes("png"))
        for read, observations in zip(reads, passes):
            # A stack turned upright may be read sideways again (near
            # vertical), which confirms nothing. A line near 45 degrees
            # stays near 45 turned, so only steep lines are left out: with
            # a 45-degree cut, a 10 pt line at 135 degrees near the page's
            # right edge was never confirmed.
            read.extend(o.text for o in observations
                        if not _is_vertical(o, width, height, steeper_than=60.0))
    return reads


def _confirm_vertical(
    png_bytes: bytes, width: int, height: int,
    passes: tuple[list[Observation], list[Observation]],
) -> list[list[bool]]:
    """Per pass, per observation: whether it is a line of the page. A
    line read horizontally is. A vertical one only if an upright re-read
    of its box, in the same pass, contains its text.

    On macOS 27 (CI's macOS 26 never made a hard finding of it), Vision
    returns a one-character-per-line stack as one vertical observation,
    the orientation it gives rotated text, and reads the upright glyphs
    sideways, often wrongly ('199140190' for the digits 1 to 9). Turned
    upright, rotated text is an ordinary line and reads back the same; a
    stack becomes a row of sideways glyphs and usually does not. Glyphs
    that look alike turned sideways (0, 1, 8, O, I, H) can read back the
    same misread, which then stays hard-eligible (#65). No geometric cut
    separates the two: letter-spaced rotated text is as sparse as a
    stack. Unconfirmed is never clean: the caller keeps such a line only
    as a reconstruction (manual review).
    """
    confirmed = [[not _is_vertical(o, width, height) for o in obs] for obs in passes]
    budget = MAX_UPRIGHT_REREADS
    doc = fitz.open()
    try:
        image: fitz.Page | None = None
        for pad in REREAD_PADS:
            boxes: dict[tuple[int, int, int, int], list[tuple[int, int]]] = {}
            for p, observations in enumerate(passes):
                for i, observation in enumerate(observations):
                    if not confirmed[p][i] and normalize_string(observation.text):
                        box = _box(observation, width, height, pad)
                        boxes.setdefault((box.x0, box.y0, box.x1, box.y1), []).append((p, i))
            for corners, members in list(boxes.items())[:budget]:
                if image is None:
                    image = doc.new_page(width=width, height=height)   # one point per pixel
                    image.insert_image(image.rect, stream=png_bytes)
                try:
                    reads = _upright_reads(image, fitz.IRect(*corners))
                except _READ_FAILURES:
                    # A re-read that fails confirms nothing: its lines stay
                    # manual review, and the page's other lines still count.
                    continue
                for p, i in members:
                    text = normalize_string(passes[p][i].text)
                    confirmed[p][i] = any(text in normalize_string(r) for r in reads[p])
            budget -= min(budget, len(boxes))
    finally:
        doc.close()
    return confirmed


def _unstack(image: fitz.Page, box: fitz.IRect) -> fitz.Pixmap | None:
    """The glyphs of a one-character-per-line column in *box* of the
    rendered page *image*, one per run of inked pixel rows, laid out left
    to right as one line; None when *box* holds fewer than three runs."""
    pix = image.get_pixmap(clip=box, colorspace=fitz.csGRAY)
    width, height, samples = pix.width, pix.height, pix.samples
    if width == 0 or height == 0:
        return None
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for y in range(height + 1):
        inked = y < height and min(samples[y * width:(y + 1) * width]) < 128
        if inked and start is None:
            start = y
        elif not inked and start is not None:
            runs.append((start, y))
            start = None
    if len(runs) < 3:
        return None
    heights = sorted(bottom - top for top, bottom in runs)
    tallest = heights[-1]
    if tallest > 3 * heights[len(heights) // 2]:
        return None     # one run far taller than the rest: not a column of glyphs
    columns = [x for x in range(width) if min(samples[x::width]) < 128]
    left, right = columns[0], columns[-1] + 1
    gap, margin = max(2, tallest // 6), tallest // 2
    if ((right - left + gap) * len(runs) + 2 * margin) * (tallest + 2 * margin) \
            > MAX_UNSTACK_PIXELS:
        return None
    strip = fitz.Pixmap(fitz.csGRAY, fitz.IRect(
        0, 0, (right - left) * len(runs) + gap * (len(runs) - 1) + 2 * margin,
        tallest + 2 * margin), False)
    strip.clear_with(255)
    x = margin
    for top, bottom in runs:
        glyph = fitz.Pixmap(fitz.csGRAY, right - left, bottom - top, b"".join(
            samples[y * width + left:y * width + right] for y in range(top, bottom)), False)
        glyph.set_origin(x, margin + (tallest - (bottom - top)) // 2)
        strip.copy(glyph, glyph.irect)
        x += right - left + gap
    return strip


def _extra_readings(
    png_bytes: bytes, width: int, height: int,
    passes: tuple[list[Observation], list[Observation]],
    confirmed: list[list[bool]],
) -> list[str]:
    """Two more reconstructed readings, one per pass (correction on, off),
    for a page where Vision read any line vertically; empty otherwise.

    - The page turned 90 and 270 degrees, its lines read horizontally
      there. On macOS 27 Vision reads sideways text in a page image only
      in part (a dense page of 40 sideways lines lost 14-21; at 200 or
      400 dpi it read none) and turned upright reads it whole (#67). Turning also makes a
      one-character-per-line stack a row of sideways glyphs, which
      Vision misreads as a number (13 of 48 stack pages matched the SSN
      pattern), so this reading is never hard.
    - Each vertical line no upright re-read confirmed, unstacked: its
      glyphs laid out as one line (_unstack) and read. A stack's misread
      ('199140190') then reads as written (#61).
    """
    if not any(_is_vertical(o, width, height) for obs in passes for o in obs):
        return ["", ""]
    lines: tuple[list[str], list[str]] = ([], [])
    doc = fitz.open()
    try:
        try:
            image = doc.new_page(width=width, height=height)   # one point per pixel
            image.insert_image(image.rect, stream=png_bytes)
        except _READ_FAILURES:   # no page image: no extra readings, as before them
            return ["", ""]
        for degrees in (90, 270):
            try:
                pix = image.get_pixmap(matrix=fitz.Matrix(1, 1).prerotate(degrees),
                                       colorspace=fitz.csGRAY)
                turned = _vision_recognize_batch(pix.tobytes("png"))
            except _READ_FAILURES:   # as for a re-read: nothing gained, nothing lost
                continue
            for read, observations in zip(lines, turned):
                read.extend(o.text for o in observations
                            if not _is_vertical(o, pix.width, pix.height))
        boxes: dict[tuple[int, int, int, int], set[int]] = {}
        for p, observations in enumerate(passes):
            for i, observation in enumerate(observations):
                if not confirmed[p][i]:
                    box = _box(observation, width, height, REREAD_PADS[0])
                    boxes.setdefault((box.x0, box.y0, box.x1, box.y1), set()).add(p)
        for corners, members in list(boxes.items())[:MAX_UNSTACKS]:
            try:
                strip = _unstack(image, fitz.IRect(*corners))
                if strip is None:
                    continue
                reads = _vision_recognize_batch(strip.tobytes("png"))
            except _READ_FAILURES:
                continue
            for p in members:
                lines[p].extend(o.text for o in reads[p])
    finally:
        doc.close()
    return ["\n".join(read) for read in lines]


def _reading(observations: list[Observation], lines_of_page: list[bool] | None) -> str:
    """One reading, one observation per line. With *lines_of_page*, an
    observation that is not a line of the page contributes one character
    per line instead, as the Text layer reads a stack."""
    lines: list[str] = []
    for i, observation in enumerate(observations):
        if lines_of_page is None or lines_of_page[i]:
            lines.append(observation.text)
        else:
            lines.extend(observation.text)
    return "\n".join(lines)


def extract_ocr_text(page: fitz.Page) -> list[str]:
    """Render a page and OCR it twice: language correction on AND off.

    Correction-on reads prose reliably; correction-off preserves literal
    code/serial-number character sequences that the language model might
    otherwise "correct". Searching the union maximizes recall. Grayscale
    rendering: Vision does not need color and the pixmap is 3x smaller.

    Both recognition passes share one image handler so the PNG is decoded
    once (see _vision_recognize_batch).

    Six readings, the first OCR_GENUINE_READINGS genuine: each pass with
    a vertical line no upright re-read confirms (_confirm_vertical) split
    one character per line, as the Text layer reads a stack, so nothing
    in it is a hard finding; then reconstructions, which only give manual
    review: each pass exactly as Vision returned it, and each pass's
    turned and unstacked reads (_extra_readings).
    """
    pix: fitz.Pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY)
    width, height = pix.width, pix.height
    png_bytes: bytes = pix.tobytes("png")
    del pix  # release the raster before Vision runs
    passes = _vision_recognize_batch(png_bytes)
    confirmed = _confirm_vertical(png_bytes, width, height, passes)
    return [_reading(obs, ok) for obs, ok in zip(passes, confirmed)] + [
        _reading(obs, None) for obs in passes] + _extra_readings(
        png_bytes, width, height, passes, confirmed)
