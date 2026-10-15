"""Scanned pages that OCR reads without error but wrongly
(docs/REDESIGN.md §8, K42 and K43).

Each page is an image only, as a scanner writes it: the text is drawn on
a scratch page, rendered at 300 dpi and drawn as the page's only
content, so no layer but OCR can see it. Vision reports success, but
what it returns leaves the secret out. Both exited 0 until OCR read the
pages again: unstacked (K42, fixed) and turned (K43, now manual review,
short of its target). Labels are what macOS 27.0.1's Vision gives, the
owner's macOS. CI's ``full-macos`` runner (macOS 26) runs these cases too
and its Vision reads both pages differently, so each also carries what
macOS 26 gives (``older_macos``): there K42 still exits 0, and K43's
SSN line is read whole (exit 1, its target)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pymupdf as fitz

from ..model import SSN, KnownGap, OlderMacOS, case, expect
from ..pdfkit import save
from ..rawpdf import build, flate, one_page, page


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


def _raw_scan_of(draw: Callable[[fitz.Page], None], clip: fitz.Rect, path: Path) -> None:
    """The non-fitz writer's scan: *clip* of a scratch page rendered at
    300 dpi, thresholded to one bit per pixel and drawn at its own place
    and size on a letter page, the page's only content. Stored (level-0)
    Flate keeps the bytes the same under every zlib; one bit per pixel
    keeps the file small."""
    scratch = fitz.open()
    draw(scratch.new_page(width=612, height=792))
    pix = scratch[0].get_pixmap(dpi=300, clip=clip, colorspace=fitz.csGRAY)
    scratch.close()
    width, height, samples = pix.width, pix.height, pix.samples
    rows = bytearray()
    for y in range(height):
        row = samples[y * width:(y + 1) * width]
        for x in range(0, width, 8):
            byte = 0
            for bit, value in enumerate(row[x:x + 8]):
                byte |= (value >= 128) << (7 - bit)        # 1 = white
            rows.append(byte if x + 8 <= width else byte | (0xFF >> (width - x)))
    image = flate(b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                  b"/BitsPerComponent 1 /ColorSpace /DeviceGray" % (width, height), bytes(rows))
    objects = one_page(flate(b"", b"q %.2f 0 0 %.2f %.2f %.2f cm /Im1 Do Q" % (
        clip.width, clip.height, clip.x0, 792 - clip.y1)), o7=image)
    objects[3] = page(resources=b"<< /XObject << /Im1 7 0 R >> >>")
    path.write_bytes(build(objects))


@case(
    "layout.scanned-vertical-stack", truth="leak", cells="live.pixels.char-stack",
    writer="raw",
    story="K42. The SSN's digits one per line, as in the layout.vertical-stack cases, but in a "
          "scanned page: an image only, so the Text layer, which reads those cases' stack as "
          "manual review, has nothing to read. Vision on macOS 27 reads the stack as one line "
          "turned sideways and misreads it (199140190 with language correction, 1994090 "
          "without); no upright re-read confirms that line, so the genuine readings hold it "
          "one character per line, but as Vision's misread digits, and neither those nor the "
          "raw line contain 123456789. With the value rule nothing matched (exit 0, no "
          "warnings) until OCR unstacked such a line: its glyphs laid out as one line and read "
          "again, a reconstruction, where the value matches (manual review, the Text layer's "
          "answer for a stack). On macOS 26 (CI's runner) Vision reads this page "
          "differently, and it still exits 0 there.",
    expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
    # macOS 26 (CI): still exit 0, no warnings; why was not measured there.
    older_macos=OlderMacOS(27, expect(0)),
    rules=({"name": "SSN", "value": SSN},),
    requires=("ocr",),
    mistake="Nobody redacted the value; it is written one character per line in a scanned "
            "page, such as a narrow column or a form's vertical label.",
    recovery="Look at the page and read each line's single character from top to bottom.",
)
def scanned_vertical_stack(path: Path) -> None:
    _raw_scan_of(_digit_stack, fitz.Rect(80, 70, 140, 240), path)


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
          "macOS 27 reports success but returns 36 lines, leaves out 14 to 21 and reads 11 "
          "and 13 only in part, the "
          "SSN's among them; nothing compares what Vision read with the ink on the page, so "
          "the run certified the page clean (exit 0, no warnings). OCR now also reads a page "
          "with any vertical line turned 90 and 270 degrees, which finds the SSN's line; a "
          "turned reading also turns a one-character-per-line stack into a row Vision "
          "misreads, so it is only manual review (today: exit 2). The same page with 20 or 30 "
          "lines is read whole and exits 1, as rotated text does. On macOS 26 (CI's runner) "
          "Vision reads the SSN's line, and this page exits 1.",
    expected=expect(1, findings=(("Any SSN", "live"),), layers=(("Any SSN", "OCR"),)),
    known_gap=KnownGap("live.pixels.ocr-dropped-line",
                       expect(2, warnings=(("REVIEW_FUSED_PATTERN", "live"),))),
    # macOS 26 (CI): Vision reads the SSN's line, a hard finding (exit 1).
    older_macos=OlderMacOS(27, None),
    rules=({"name": "Any SSN", "class": "ssn"},),
    requires=("ocr",),
    mistake="Scanning a page of a report sideways and sharing the image without a text "
            "layer, trusting the scan to be read like any other page.",
    recovery="Rotate the page view and read it; every line is plainly visible.",
)
def dense_sideways_scan(path: Path) -> None:
    _scan_of(_dense_sideways_page, path)
