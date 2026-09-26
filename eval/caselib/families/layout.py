"""Values split by layout: across a page break, across lines, under
boxes, among tight lines. What matters is how the page text is joined."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import SSN, case, expect
from ..pdfkit import body, embedded_font, save

SPLIT_CODE = expect(1, findings=(("Code", "live"),))
WRAPPED = expect(2, warnings=("REVIEW_CROSS_LINE",))


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


@case("leak.split.page-break", cells="match.page-break", expected=SPLIT_CODE,
      story="A code name split across a page break: BLUE at the bottom, HERON at the top.")
def page_break(path: Path) -> None:
    _split_pages(path, lambda p: p.insert_text((72, 760), "Project code BLUE"),
                 lambda p: p.insert_text((72, 60), "HERON continues here"))


@case("leak.split.page-break-slug-outside-crop", cells="match.page-break", expected=SPLIT_CODE,
      story="The same split, with a printer's slug outside the crop box between the halves.")
def page_break_slug(path: Path) -> None:
    _split_pages(path, lambda p: p.insert_text((72, 690), "Project code BLUE"),
                 lambda p: p.insert_text((72, 60), "HERON continues here"), crop=True,
                 extra=lambda p: p.insert_text((72, 780), "SLUG job 4471 proof 2"))


@case("leak.split.page-break-offpage-bates", cells="match.page-break-furniture",
      expected=expect(1, findings=(("Code", "live"),)), known_gap="match.page-break-furniture",
      story="The split, with a Bates number below the page between the halves: the seam "
            "reading puts the Bates line between BLUE and HERON.")
def page_break_bates(path: Path) -> None:
    def extra(p: fitz.Page) -> None:
        p.insert_text((72, 830), "bates 000123"); p.insert_text((72, 860), "x")
    _split_pages(path, lambda p: p.insert_text((72, 760), "Project code BLUE"),
                 lambda p: p.insert_text((72, 60), "HERON continues here"), extra=extra)


@case("leak.split.ssn-page-break", cells="match.page-break",
      expected=expect(1, findings=(("SSN", "live"),)),
      story="An SSN split across a page break after its second dash.")
def ssn_page_break(path: Path) -> None:
    _split_pages(path, lambda p: p.insert_text((72, 760), "SSN 123-45-"),
                 lambda p: p.insert_text((72, 60), "6789 end"))


@case("leak.split.boxed-page-break-slug", cells="match.page-break", expected=SPLIT_CODE,
      story="Boxed halves of a split code name, a slug outside the crop box between them.",
      mistake="Boxes drawn over text on both pages.")
def boxed_slug(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 690), "Project code BLUE"),
                 lambda p: _boxed(p, (72, 60), "HERON continues here"), crop=True,
                 extra=lambda p: p.insert_text((72, 780), "SLUG job 4471 proof 2"))


@case("leak.split.boxed-page-break", cells="match.page-break", expected=SPLIT_CODE,
      story="Boxed halves of a split code name, cropped pages, nothing between them.")
def boxed_split(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 690), "Project code BLUE"),
                 lambda p: _boxed(p, (72, 60), "HERON continues here"), crop=True)


@case("leak.split.boxed-page-break-offpage-bates", cells="match.page-break",
      expected=SPLIT_CODE,
      story="Boxed halves, first half near the bottom, a Bates number below the page.")
def boxed_bates(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 800), "Project code BLUE"),
                 lambda p: _boxed(p, (72, 60), "HERON continues here"),
                 extra=lambda p: p.insert_text((72, 870), "bates 000123"))


@case("leak.split.boxed-ssn-page-break-slug", cells="match.page-break",
      expected=expect(1, findings=(("SSN", "live"),)),
      story="A boxed SSN split across a page break, a slug outside the crop box.")
def boxed_ssn(path: Path) -> None:
    _split_pages(path, lambda p: _boxed(p, (72, 690), "SSN 123-45-"),
                 lambda p: _boxed(p, (72, 60), "6789 end"), crop=True,
                 extra=lambda p: p.insert_text((72, 780), "SLUG job 4471 proof 2"))


@case("leak.wrap.boxed-lines", cells="match.line-wrap", expected=WRAPPED,
      story="A code name wrapped across two boxed lines (manual review: a joined reading).")
def boxed_wrap(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    _boxed(page, (72, 300), "Project code BLUE"); _boxed(page, (72, 316), "HERON continues")
    save(doc, path)


@case("leak.wrap.boxed-lines-offpage-label", cells="match.line-wrap", expected=WRAPPED,
      story="The same wrap, with an off-page label on the first line's baseline.")
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


@case("leak.tight-lines-boxed", cells="live.font", expected=expect(1, findings=(("SSN", "live"),)),
      story="A boxed SSN among tightly spaced ledger lines.")
def tight(path: Path) -> None:
    _tight(path, False)


@case("leak.tight-lines-boxed-offpage-label", cells=("live.font", "off-page.font"),
      expected=expect(1, findings=(("SSN", "live"),)),
      story="The same, with a colour-bar label off the page between two lines.")
def tight_label(path: Path) -> None:
    _tight(path, True)
