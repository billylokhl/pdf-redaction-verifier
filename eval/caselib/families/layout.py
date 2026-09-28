"""Values split by layout: across a page break, across lines, under
boxes, among tight lines. What matters is how the page text is joined."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..model import SSN, KnownGap, case, expect
from ..pdfkit import body, embedded_font, save

SPLIT_CODE = expect(1, findings=(("Code", "live"),))
WRAPPED = expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),))
NOBODY = "Nobody redacted it; the value simply spans the layout described."
READ_BREAK = "Read the last line of one page and the first line of the next."
READ_WRAP = "Read the two consecutive lines together."


def leak(id: str, cells, story: str, expected=SPLIT_CODE, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story, **kw)


def _boxed(page: fitz.Page, pos: tuple[float, float], text: str) -> None:
    page.insert_text(pos, text)
    rect = fitz.Rect(pos[0] - 2, pos[1] - 12, pos[0] + 8 * len(text), pos[1] + 4)
    page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0))


def _split_pages(path: Path, first, second, crop: bool = False, extra=None) -> None:
    doc = fitz.open()
    page = doc.new_page(); body(page); first(page)
    if extra:
        extra(page)
    if crop:
        page.set_cropbox(fitz.Rect(0, 0, 595, 700))
    page = doc.new_page(); second(page); body(page, y0=90)
    if crop:
        page.set_cropbox(fitz.Rect(0, 0, 595, 700))
    save(doc, path)


@leak("layout.page-break", "match.page-break",
      "A code name split across a page break: BLUE at the bottom, HERON at the top.",
      mistake=NOBODY, recovery=READ_BREAK)
def page_break(path: Path) -> None:
    _split_pages(path, lambda p: p.insert_text((72, 760), "Project code BLUE"),
                 lambda p: p.insert_text((72, 60), "HERON continues here"))


@leak("layout.page-break-slug-outside-crop", "match.page-break",
      "The same split, with a printer's slug outside the crop box between the halves.",
      mistake=NOBODY, recovery=READ_BREAK)
def page_break_slug(path: Path) -> None:
    _split_pages(path, lambda p: p.insert_text((72, 690), "Project code BLUE"),
                 lambda p: p.insert_text((72, 60), "HERON continues here"), crop=True,
                 extra=lambda p: p.insert_text((72, 780), "SLUG job 4471 proof 2"))


@leak("layout.page-break-bates-footer", "match.page-break-furniture",
      "A code name split across a page break, with a Bates number printed as a footer "
      "between the halves: the joined reading puts the footer between BLUE and HERON.",
      known_gap=KnownGap("match.page-break-furniture", expect(0)),
      mistake=NOBODY, recovery=READ_BREAK)
def page_break_bates(path: Path) -> None:
    def extra(p: fitz.Page) -> None:
        p.insert_text((72, 830), "bates 000123"); p.insert_text((72, 860), "x")
    _split_pages(path, lambda p: p.insert_text((72, 760), "Project code BLUE"),
                 lambda p: p.insert_text((72, 60), "HERON continues here"), extra=extra)


@leak("layout.ssn-page-break", "match.page-break",
      expected=expect(1, findings=(("SSN", "live"),)),
      story="An SSN split across a page break after its second dash.",
      mistake=NOBODY, recovery=READ_BREAK)
def ssn_page_break(path: Path) -> None:
    _split_pages(path, lambda p: p.insert_text((72, 760), "SSN 123-45-"),
                 lambda p: p.insert_text((72, 60), "6789 end"))


@leak("layout.boxed-page-break-slug", "match.page-break",
      "Boxed halves of a split code name, a slug outside the crop box between them.",
      mistake="Boxes drawn over text on both pages.", recovery=READ_BREAK)
def boxed_slug(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 690), "Project code BLUE"),
                 lambda p: _boxed(p, (72, 60), "HERON continues here"), crop=True,
                 extra=lambda p: p.insert_text((72, 780), "SLUG job 4471 proof 2"))


@leak("layout.boxed-page-break", "match.page-break",
      "Boxed halves of a split code name, cropped pages, nothing between them.",
      mistake="Boxes drawn over text on both pages.", recovery=READ_BREAK)
def boxed_split(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 690), "Project code BLUE"),
                 lambda p: _boxed(p, (72, 60), "HERON continues here"), crop=True)


@leak("layout.boxed-page-break-offpage-bates", "match.page-break",
      "Boxed halves, first half near the bottom, a Bates number below the page.",
      mistake="Boxes drawn over text on both pages.", recovery=READ_BREAK)
def boxed_bates(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 800), "Project code BLUE"),
                 lambda p: _boxed(p, (72, 60), "HERON continues here"),
                 extra=lambda p: p.insert_text((72, 870), "bates 000123"))


@leak("layout.boxed-ssn-page-break-slug", "match.page-break",
      expected=expect(1, findings=(("SSN", "live"),)),
      story="A boxed SSN split across a page break, a slug outside the crop box.",
      mistake="Boxes drawn over text on both pages.", recovery=READ_BREAK)
def boxed_ssn(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 690), "SSN 123-45-"),
                 lambda p: _boxed(p, (72, 60), "6789 end"), crop=True,
                 extra=lambda p: p.insert_text((72, 780), "SLUG job 4471 proof 2"))


@leak("layout.wrap-boxed-lines", "match.line-wrap", expected=WRAPPED,
      story="A code name wrapped across two boxed lines (manual review: a joined reading).",
      mistake="Boxes drawn over text on both lines.", recovery=READ_WRAP)
def boxed_wrap(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    _boxed(page, (72, 300), "Project code BLUE"); _boxed(page, (72, 316), "HERON continues")
    save(doc, path)


@leak("layout.wrap-boxed-lines-offpage-label", "match.line-wrap", expected=WRAPPED,
      features="off-page.plain",
      story="The same wrap, with an off-page label on the first line's baseline.",
      mistake="Boxes drawn over text on both lines.", recovery=READ_WRAP)
def boxed_wrap_label(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    _boxed(page, (72, 300), "Project code BLUE"); _boxed(page, (72, 316), "HERON continues")
    page.insert_text((620, 300), "REG"); save(doc, path)


def _tight(path: Path, offpage: bool) -> None:
    doc = fitz.open(); page = doc.new_page(); font = embedded_font(page)
    y = 100
    for i in range(30):
        text = f"SSN {SSN}" if i == 15 else f"Ledger row {i} reconciled against the monthly statement"
        page.insert_text((72, y), text, fontname=font, fontsize=9)
        if i == 15:
            page.draw_rect(fitz.Rect(70, y - 8, 160, y + 2), color=(0, 0, 0), fill=(0, 0, 0))
        y += 10
    if offpage:
        page.insert_text((610, 100 + 15 * 10 + 5), "CYAN 50% MAGENTA 50% YELLOW 50%",
                         fontname=font, fontsize=9)
    save(doc, path)


@leak("layout.tight-lines-boxed", "live.font", expected=expect(1, findings=(("SSN", "live"),)),
      story="A boxed SSN among tightly spaced ledger lines.",
      mistake="A box drawn over one line, not a real redaction.",
      recovery="Select the text under the box and copy it.")
def tight(path: Path) -> None:
    _tight(path, False)


@leak("layout.tight-lines-boxed-offpage-label", "live.font", features="off-page.font",
      expected=expect(1, findings=(("SSN", "live"),)),
      story="The same, with a colour-bar label off the page between two lines.",
      mistake="A box drawn over one line, not a real redaction.",
      recovery="Select the text under the box and copy it.")
def tight_label(path: Path) -> None:
    _tight(path, True)


@leak("layout.undashed-ssn-wrapped", "match.undashed-wrap",
      "An SSN written with spaces instead of dashes, wrapped after its second group; "
      "searched for by the built-in SSN pattern. The joined reading should raise it for "
      "review, but the pattern only accepts the dashed form across a line break.",
      rules=({"name": "Any SSN", "class": "ssn"},),
      expected=expect(2, warnings=(("REVIEW_FUSED_PATTERN", "live"),)),
      known_gap=KnownGap("match.undashed-wrap", expect(0)),
      mistake=NOBODY, recovery=READ_WRAP)
def undashed_wrap(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((72, 300), "Applicant number 123 45")
    page.insert_text((72, 316), "6789 on file"); save(doc, path)
