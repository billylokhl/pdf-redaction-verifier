"""redaction_verifier.views.ocr — rendered-page OCR (Apple Vision).

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §6 ("Views are the existing page readings and OCR, moved
[not wrapped] in Phase 2"). Behaviour is byte-identical to the code this
replaced; verify.py re-exports every name here so existing imports,
`verify.X` references and the CLI keep working unchanged. Two changes
since the move, both for macOS 27: the image handler's options are a
native NSDictionary, not a Python {} (see _vision_recognize_batch); and
a vertical stack of upright characters, which Vision there returns as
one line read sideways, is only manual review (see _is_stacked).

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

OCR_DPI: int = 300

# How many of extract_ocr_text's readings are genuine reading orders
# (hard-finding eligible): Vision's reading with language correction on
# and off. The rest are reconstructions (manual review).
OCR_GENUINE_READINGS = 2

# A vertically read observation whose characters lie further apart along
# the reading direction than the line is thick is a stack of upright
# characters, not rotated text (see _is_stacked). Measured on the case
# library under macOS 27: rotated text and every other OCR hard finding
# at most 0.69, a one-digit-per-line stack 1.84.
STACK_PITCH_RATIO = 1.0

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


def _is_stacked(observation: Observation, width: int, height: int) -> bool:
    """Whether an observation is upright characters stacked one per line,
    read by Vision as a single line rotated 90 or 270 degrees.

    On macOS 27, Vision returns a one-character-per-line stack as one
    vertical observation (CI's macOS 26 never made a hard finding of it), the same orientation it gives rotated text. Its
    string is then a reading of upright glyphs turned sideways
    ('199140190' for 1 to 9, one digit per line): not a line of the page.
    The geometry tells the two apart. Rotated text is thick across the
    line (a glyph's height) with characters close together along it (a
    glyph's width); a stack is thin across (one upright glyph's width)
    with characters a line-spacing apart along it. Measured in pixels of
    the *width* x *height* image, so the page's aspect ratio cannot skew
    it. Observations read horizontally (within 45 degrees) are never
    stacks: a row of form boxes is one line of the page.
    """
    if not observation.text:
        return False
    def vector(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
        return (end[0] - start[0]) * width, (end[1] - start[1]) * height

    along_x, along_y = vector(observation.top_left, observation.top_right)
    if abs(along_y) <= abs(along_x):
        return False
    pitch = math.hypot(along_x, along_y) / len(observation.text)
    thickness = math.hypot(*vector(observation.bottom_left, observation.top_left))
    return pitch > STACK_PITCH_RATIO * thickness


def _reading(observations: list[Observation], width: int, height: int,
             *, split_stacks: bool) -> str:
    """One reading, one observation per line; with *split_stacks*, a
    stacked observation contributes one character per line instead."""
    lines: list[str] = []
    for observation in observations:
        if split_stacks and _is_stacked(observation, width, height):
            lines.extend(observation.text)
        else:
            lines.append(observation.text)
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
    a stacked observation (_is_stacked) split one character per line, as
    the Text layer reads such a stack, so nothing in it is a hard finding;
    then each pass exactly as Vision returned it, a reconstruction, so a
    value or pattern in a stack's fused string is still manual review.
    """
    pix: fitz.Pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY)
    width, height = pix.width, pix.height
    png_bytes: bytes = pix.tobytes("png")
    del pix  # release the raster before Vision runs
    passes = _vision_recognize_batch(png_bytes)
    return [_reading(obs, width, height, split_stacks=True) for obs in passes] + [
        _reading(obs, width, height, split_stacks=False) for obs in passes]
