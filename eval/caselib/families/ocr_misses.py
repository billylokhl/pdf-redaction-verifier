"""Scanned pages that OCR reads without error but wrongly
(docs/REDESIGN.md §8, K42 and K43).

Each page is an image only, as a scanner writes it: the text is drawn on
a scratch page, rendered at 300 dpi and inserted as the new page's only
content, so no layer but OCR can see it. Vision reports success, but
what it returns leaves the secret out, and nothing checks a reading
against the page's ink (DESIGN.md's known limitations: a change that
makes Vision read less does not fail closed). Each `known_gap` pins what
macOS 27.0.1's Vision gives; CI's macOS runners are older and run these
cases too (`full-macos`), so they say whether that version reads the
same way."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pymupdf as fitz

from ..model import SSN, KnownGap, case, expect
from ..pdfkit import save


def _scan_of(draw: Callable[[fitz.Page], None], path: Path) -> None:
    """Draw on a scratch page, render it at 300 dpi and write a one-page
    PDF whose only content is that render, compressed by MuPDF on save
    (``deflate``, as the carriers and clean cases are), so the bytes are
    the same on every machine with the same PyMuPDF (built twice: one
    hash, cases.lock.json)."""
    scratch = fitz.open()
    draw(scratch.new_page())
    pix = scratch[0].get_pixmap(dpi=300)
    scratch.close()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    save(doc, path, deflate=True)


# ── live.pixels.char-stack (K42, #61) ────────────────────────────────────

def _digit_stack(page: fitz.Page) -> None:
    # The page-text grid's vertical-stack layout (families/page_text.py):
    # Helvetica 11 pt, 14 pt apart, from (100, 100).
    for i, digit in enumerate("123456789"):
        page.insert_text((100, 100 + 14 * i), digit, fontsize=11)


@case(
    "layout.scanned-vertical-stack", truth="leak", cells="live.pixels.char-stack",
    story="K42. The SSN's digits one per line, as in the layout.vertical-stack cases, but in a "
          "scanned page: an image only, so the Text layer, which reads those cases' stack as "
          "manual review, has nothing to read. Vision on macOS 27 reads the stack as one line "
          "turned sideways and misreads it (199140190 with language correction, 1994090 "
          "without); no upright re-read confirms that line, so the genuine readings hold it "
          "one character per line, but as Vision's misread digits, and neither those nor the "
          "raw line contain 123456789. With the value rule nothing matches (measured: exit 0, "
          "no warnings). The target is the Text layer's answer for a stack: manual review.",
    expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
    known_gap=KnownGap("live.pixels.char-stack", expect(0)),
    rules=({"name": "SSN", "value": SSN},),
    requires=("ocr",),
    mistake="Nobody redacted the value; it is written one character per line in a scanned "
            "page, such as a narrow column or a form's vertical label.",
    recovery="Look at the page and read each line's single character from top to bottom.",
)
def scanned_vertical_stack(path: Path) -> None:
    _scan_of(_digit_stack, path)


# ── live.pixels.ocr-dropped-line (K43, #67) ──────────────────────────────

_SIDEWAYS_LINES = 40
_SSN_LINE = 20


def _dense_sideways_page(page: fitz.Page) -> None:
    for i in range(_SIDEWAYS_LINES):
        text = (f"Employee record SSN {SSN} on file" if i == _SSN_LINE
                else f"Line {i} of the sideways scanned report body text")
        page.insert_text((30 + i * 8 * 1.3, 770), text, fontsize=8, rotate=90)


@case(
    "page.dense-sideways-scan", truth="leak", cells="live.pixels.ocr-dropped-line",
    story="K43. A scanned page of 40 lines of 8 pt text turned 90 degrees, 10.4 pt apart, the "
          "middle one holding the SSN whole, searched with the SSN pattern rule. Vision on "
          "macOS 27 reports success but returns 36 lines and leaves out lines 14 to 21, the "
          "SSN's among them; nothing compares what Vision read with the ink on the page, so "
          "the run certifies the page clean (measured: exit 0, no warnings). The same page "
          "with 20 or 30 lines is read whole and exits 1, as rotated text does.",
    expected=expect(1, findings=(("Any SSN", "live"),), layers=(("Any SSN", "OCR"),)),
    known_gap=KnownGap("live.pixels.ocr-dropped-line", expect(0)),
    rules=({"name": "Any SSN", "class": "ssn"},),
    requires=("ocr",),
    mistake="Scanning a page of a report sideways and sharing the image without a text "
            "layer, trusting the scan to be read like any other page.",
    recovery="Rotate the page view and read it; every line is plainly visible.",
)
def dense_sideways_scan(path: Path) -> None:
    _scan_of(_dense_sideways_page, path)
