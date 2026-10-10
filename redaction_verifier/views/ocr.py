"""redaction_verifier.views.ocr — rendered-page OCR (Apple Vision).

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §6 ("Views are the existing page readings and OCR, moved
[not wrapped] in Phase 2"). Behaviour is byte-identical to the code this
replaced; verify.py re-exports every name here so existing imports,
`verify.X` references and the CLI keep working unchanged. Two changes
since the move, both for macOS 27: the image handler's options are a
native NSDictionary, not a Python {} (see _vision_recognize_batch); and
a line Vision reads vertically is hard-finding eligible only when an
upright re-read confirms it, since Vision there returns a stack of
upright characters as one line read sideways (see _confirm_vertical).

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
# many per page; a vertical line past the cap stays unconfirmed (manual
# review), so a page of sideways text cannot make the run unbounded.
# Each re-read costs two small Vision calls (~70 ms).
MAX_UPRIGHT_REREADS = 64
# The margin around a re-read box, in multiples of the line's thickness:
# a second, wider crop for a line the first did not confirm (Vision
# sometimes misreads a crop's last glyph, and not at every margin).
REREAD_PADS = (1.0, 3.0)

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


def _vision_recognize_batch(
    png_bytes: bytes,
) -> tuple[list[Observation], list[Observation]]:
    """OCR a PNG with Apple Vision in one pass: correction on AND off.

    Both requests share a single VNImageRequestHandler so the image is
    decoded once instead of twice.  Raises on any Vision-level failure.
    Returns each pass's observations in Vision's order.
    """
    ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    results: dict[bool, list[Observation]] = {True: [], False: []}
    errors: dict[bool, list[str]] = {True: [], False: []}

    def _make_handler(correction: bool) -> Callable[[Any, Any], None]:
        def handler(request: Any, error: Any) -> None:
            if error:
                errors[correction].append(str(error))
                return
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates:
                    tl, tr, bl = (observation.topLeft(), observation.topRight(),
                                  observation.bottomLeft())
                    results[correction].append(Observation(
                        candidates[0].string(),
                        (tl.x, tl.y), (tr.x, tr.y), (bl.x, bl.y),
                    ))
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

    return (results[True], results[False])


def _is_vertical(observation: Observation, width: int, height: int) -> bool:
    """Whether Vision read the observation along the image's vertical
    axis (within 45 degrees), measured in pixels of the *width* x
    *height* image so its aspect ratio cannot skew it."""
    (tlx, tly), (trx, tr_y) = observation.top_left, observation.top_right
    return abs((tr_y - tly) * height) > abs((trx - tlx) * width)


def _box(observation: Observation, width: int, height: int, pad: float) -> fitz.IRect:
    """The observation's quadrilateral in image pixels (origin top-left),
    padded by *pad* times its thickness plus a margin, clipped to the image."""
    corners = [(x * width, (1 - y) * height) for x, y in (
        observation.top_left, observation.top_right, observation.bottom_left,
        (observation.top_right[0] + observation.bottom_left[0] - observation.top_left[0],
         observation.top_right[1] + observation.bottom_left[1] - observation.top_left[1]))]
    rect = fitz.Rect(min(x for x, _ in corners), min(y for _, y in corners),
                     max(x for x, _ in corners), max(y for _, y in corners))
    margin = pad * min(rect.width, rect.height) + 2 * OCR_DPI / 72
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
            read.extend(o.text for o in observations if not _is_vertical(o, width, height))
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
    stack becomes a row of sideways glyphs and does not. No geometric
    cut separates them: letter-spaced rotated text is as sparse as a
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
                reads = _upright_reads(image, fitz.IRect(*corners))
                for p, i in members:
                    text = normalize_string(passes[p][i].text)
                    confirmed[p][i] = any(text in normalize_string(r) for r in reads[p])
            budget -= min(budget, len(boxes))
    finally:
        doc.close()
    return confirmed


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

    Four readings, the first OCR_GENUINE_READINGS genuine: each pass with
    a vertical line no upright re-read confirms (_confirm_vertical) split
    one character per line, as the Text layer reads a stack, so nothing
    in it is a hard finding; then each pass exactly as Vision returned
    it, a reconstruction, so a value or pattern in such a line is still
    manual review.
    """
    pix: fitz.Pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY)
    width, height = pix.width, pix.height
    png_bytes: bytes = pix.tobytes("png")
    del pix  # release the raster before Vision runs
    passes = _vision_recognize_batch(png_bytes)
    confirmed = _confirm_vertical(png_bytes, width, height, passes)
    return [_reading(obs, ok) for obs, ok in zip(passes, confirmed)] + [
        _reading(obs, None) for obs in passes]
