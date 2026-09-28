"""redaction_verifier.views.ocr — rendered-page OCR (Apple Vision).

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §6 ("Views are the existing page readings and OCR, moved
[not wrapped] in Phase 2"). Behaviour is byte-identical to the code this
replaced; verify.py re-exports every name here so existing imports,
`verify.X` references and the CLI keep working unchanged.

The Vision/Foundation import below keeps its original degrade semantics:
an ORDINARY failure (missing package, corrupt pyobjc install) sets
_OCR_IMPORTS_OK = False so OCR degrades to an OCR_UNAVAILABLE warning
later (fail-closed: exit 2, never a silent clean verdict) instead of
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

from typing import Any, Callable

import pymupdf as fitz

OCR_DPI: int = 300

try:
    import Vision
    from Foundation import NSData

    _OCR_IMPORTS_OK = True
except Exception:  # pragma: no cover
    # Broad on purpose, but the design here is to degrade, not exit: any
    # ORDINARY failure importing the OCR bridge (missing package, or a
    # corrupt pyobjc install) means OCR is simply unavailable on this
    # machine. That already surfaces later as an OCR_UNAVAILABLE warning
    # (fail-closed: exit 2, never a silent clean verdict) rather than a
    # crash, so it is not itself an operational failure worth a stderr
    # message here. A SystemExit/KeyboardInterrupt/other BaseException is
    # NOT caught by this clause (Exception, not BaseException) and
    # propagates instead — see the module docstring.
    _OCR_IMPORTS_OK = False


def _vision_recognize_batch(png_bytes: bytes) -> tuple[str, str]:
    """OCR a PNG with Apple Vision in one pass: correction on AND off.

    Both requests share a single VNImageRequestHandler so the image is
    decoded once instead of twice.  Raises on any Vision-level failure.
    """
    ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    results: dict[bool, list[str]] = {True: [], False: []}
    errors: dict[bool, list[str]] = {True: [], False: []}

    def _make_handler(correction: bool) -> Callable[[Any, Any], None]:
        def handler(request: Any, error: Any) -> None:
            if error:
                errors[correction].append(str(error))
                return
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates:
                    results[correction].append(candidates[0].string())
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

    image_handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        ns_data, {}
    )
    success, perform_error = image_handler.performRequests_error_(
        [request_on, request_off], None
    )
    if not success:
        raise RuntimeError(f"Apple Vision request failed: {perform_error}")
    for correction in (True, False):
        if errors[correction]:
            raise RuntimeError(f"Apple Vision error: {'; '.join(errors[correction])}")

    return ("\n".join(results[True]), "\n".join(results[False]))


def extract_ocr_text(page: fitz.Page) -> list[str]:
    """Render a page and OCR it twice: language correction on AND off.

    Correction-on reads prose reliably; correction-off preserves literal
    code/serial-number character sequences that the language model might
    otherwise "correct". Searching the union maximizes recall. Grayscale
    rendering: Vision does not need color and the pixmap is 3x smaller.

    Both recognition passes share one image handler so the PNG is decoded
    once (see _vision_recognize_batch).
    """
    pix: fitz.Pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY)
    png_bytes: bytes = pix.tobytes("png")
    del pix  # release the raster before Vision runs
    text_on, text_off = _vision_recognize_batch(png_bytes)
    return [text_on, text_off]
