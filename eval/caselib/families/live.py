"""Secrets still in the live document: drawn on a page, off it, under a
box, or in a field."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import CODE, SSN, case, expect
from ..pdfkit import FILLER, body, cjk_font, embedded_font, save

LIVE_SSN = expect(1, findings=(("SSN", "live"),))


@case("leak.visible", cells="live.plain", expected=LIVE_SSN,
      story="The SSN is simply on the page.",
      mistake="Nobody redacted it.", recovery="Read the page.")
def visible(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)


@case("leak.visible-embedded-font", cells="live.font", expected=LIVE_SSN,
      story="The SSN on the page in an embedded font (glyph codes).")
def visible_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)


@case("leak.visible-cjk", cells="live.font", expected=expect(1, findings=(("Code", "live"),)),
      story="A code name on a page set in an embedded CJK font.")
def visible_cjk(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, cjk_font(page), lines=[*FILLER, f"代号 {CODE}"]); save(doc, path)


@case("leak.box-drawn-over-text", cells="live.plain", expected=LIVE_SSN,
      story="A black rectangle drawn over the SSN; the text is still underneath.",
      mistake="Drawing a box (or highlighting in black) instead of redacting.",
      recovery="Select the text under the box and copy it.")
def boxed(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, lines=[*FILLER, f"SSN {SSN}"])
    rect = page.search_for(SSN)[0]
    page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0)); save(doc, path)


@case("leak.above-page", cells="off-page.plain", expected=LIVE_SSN,
      story="The SSN drawn above the top edge of the page, where no viewer shows it.",
      recovery="Copy all text, or enlarge the page box.")
def above_page(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((72, -40), f"SSN {SSN}"); save(doc, path)


@case("leak.right-of-page-embedded-font", cells="off-page.font", expected=LIVE_SSN,
      story="The SSN right of the page edge, in an embedded font.")
def right_of_page(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); font = embedded_font(page); body(page, font)
    page.insert_text((700, 100), f"SSN {SSN}", fontname=font); save(doc, path)


@case("leak.outside-crop-box", cells="off-page.font", expected=LIVE_SSN,
      story="The SSN in the area a crop box hides.",
      mistake="Cropping the page to hide a line.", recovery="Remove the crop box.")
def outside_crop(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); font = embedded_font(page); body(page, font)
    page.insert_text((72, 780), f"SSN {SSN}", fontname=font)
    page.set_cropbox(fitz.Rect(0, 0, 595, 700)); save(doc, path)


@case("leak.runs-off-right-edge", cells=("live.plain", "off-page.plain"), expected=LIVE_SSN,
      story="An SSN that starts on the page and runs past its right edge.")
def off_right_edge(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((540, 300), f"SSN {SSN}"); save(doc, path)


@case("leak.form-field", cells="annot-fields.plain", expected=LIVE_SSN,
      story="The SSN as a form field's value.")
def form_field(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    widget = fitz.Widget(); widget.field_name = "ssn"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = fitz.Rect(72, 300, 300, 320); widget.field_value = SSN
    page.add_widget(widget); save(doc, path)


@case("leak.rotated", cells="live.plain", expected=LIVE_SSN,
      story="The SSN in a rotated margin note.")
def rotated(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((500, 600), f"SSN {SSN}", rotate=90); save(doc, path)


@case("leak.metadata-title", cells="metadata.plain", expected=LIVE_SSN,
      story="The SSN in the document title.",
      mistake="Redacting the page but not the document properties.",
      recovery="File > Properties in any viewer.")
def metadata_title(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_metadata({"title": f"Case {SSN}", "creationDate": "", "modDate": ""})
    save(doc, path)


@case("leak.javascript", cells="javascript.plain", expected=LIVE_SSN,
      story="The SSN inside a document-level JavaScript.")
def javascript(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /S /JavaScript /JS (var ssn = '{SSN}';) >>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{xref} 0 R")
    save(doc, path)


@case("leak.image-on-page", cells="live.pixels", expected=LIVE_SSN, requires=("ocr",),
      story="A scanned image showing the SSN, drawn on the page.",
      mistake="Redacting the text layer and forgetting the scan.",
      recovery="Look at the page.")
def image_on_page(path: Path) -> None:
    from ..pdfkit import png_of
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_image(fitz.Rect(72, 200, 372, 260), stream=png_of(f"SSN {SSN}"))
    save(doc, path)
