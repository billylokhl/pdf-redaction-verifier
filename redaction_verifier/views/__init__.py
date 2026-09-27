"""redaction_verifier.views — page readings and rendered-page OCR.

Split into modules per docs/REDESIGN.md §4 ("views/ page readings,
rendered-page OCR"): text.py (the layout-aware visual text extractor:
_reconstruct, _join_cluster, extract_visual_text and the line-clustering
constants), ocr.py (the Apple Vision OCR bridge: _vision_recognize_batch,
extract_ocr_text, OCR_DPI, and the _OCR_IMPORTS_OK availability flag).
This module re-exports everything so callers can do `from
redaction_verifier.views import X` without knowing which submodule
defines it.

Importing this package can fail for two different reasons, by design
(docs/REDESIGN.md §6, "Move, don't wrap"):

- text.py imports `fitz` plainly. If PyMuPDF is missing or broken, that
  ImportError/BaseException propagates straight out of this package —
  verify.py's single guarded re-export block is what turns it into an
  "[ERROR] cannot import redaction_verifier" message and exit 2.
- ocr.py's Vision/Foundation import degrades instead: an ordinary
  failure there sets `_OCR_IMPORTS_OK = False` (OCR becomes unavailable,
  not fatal) without raising, while a BaseException (SystemExit,
  KeyboardInterrupt, ...) still propagates the same way fitz's would.
"""

from __future__ import annotations

from redaction_verifier.views.ocr import (
    OCR_DPI,
    _OCR_IMPORTS_OK,
    _vision_recognize_batch,
    extract_ocr_text,
)
from redaction_verifier.views.text import (
    MAX_LINE_TOLERANCE_PT,
    MIN_LINE_TOLERANCE_PT,
    TEXT_GENUINE_READINGS,
    _join_cluster,
    _reconstruct,
    extract_visual_text,
)

__all__ = [
    "MAX_LINE_TOLERANCE_PT",
    "MIN_LINE_TOLERANCE_PT",
    "OCR_DPI",
    "TEXT_GENUINE_READINGS",
    "_OCR_IMPORTS_OK",
    "_join_cluster",
    "_reconstruct",
    "_vision_recognize_batch",
    "extract_ocr_text",
    "extract_visual_text",
]
