"""Secrets still in the live document: drawn on a page, off it, under a
box, in a field, in the document's own properties."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..model import CODE, SSN, KnownGap, case, expect
from ..pdfkit import (FILLER, body, cjk_font, embedded_font, lookalike_font,
                      png_of, redact, save, update)

LIVE_SSN = expect(1, findings=(("SSN", "live"),))


def leak(id: str, cells, story: str, expected=LIVE_SSN, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story, **kw)


@leak("page.visible", "live.plain", "The SSN is simply on the page.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Nobody redacted it.", recovery="Read the page.")
def visible(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)


@leak("page.visible-embedded-font", "live.font",
      "The SSN on the page in an embedded font (glyph codes).",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Nobody redacted it.", recovery="Read the page.")
def visible_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)


@leak("page.visible-cjk", "live.font",
      "A code name on a page set in an embedded CJK font.",
      expected=expect(1, findings=(("Code", "live"),), layers=(("Code", "Text"),)),
      mistake="Nobody redacted it.", recovery="Read the page.")
def visible_cjk(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, cjk_font(page), lines=[*FILLER, f"代号 {CODE}"]); save(doc, path)


@leak("page.visible-pattern-rule", "live.plain",
      "An SSN on the page, searched for by the built-in SSN pattern instead of its value.",
      rules=({"name": "Any SSN", "class": "ssn"},),
      expected=expect(1, findings=(("Any SSN", "live"),), layers=(("Any SSN", "Text"),)),
      mistake="Nobody redacted it.", recovery="Read the page.")
def visible_pattern(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)


@leak("page.scan-image", "live.pixels", "A scanned image showing the SSN, drawn on the page.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "OCR"),)),
      requires=("ocr",), mistake="Redacting the text layer and forgetting the scan.",
      recovery="Look at the page.")
def image_on_page(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_image(fitz.Rect(72, 200, 372, 260), stream=png_of(f"SSN {SSN}"))
    save(doc, path)


@leak("page.box-over-text", "live.plain",
      "A black rectangle drawn over the SSN; the text is still underneath.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Drawing a box (or highlighting in black) instead of redacting.",
      recovery="Select the text under the box and copy it.")
def boxed(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, lines=[*FILLER, f"SSN {SSN}"])
    rect = page.search_for(SSN)[0]
    page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0)); save(doc, path)


@leak("page.redaction-search-missed", "live.font",
      "A redaction tool searched for the SSN and found nothing: the font's Unicode map "
      "turns '-' into a soft hyphen and ' ' into a no-break space, so the text never "
      "matched. The SSN stays on the page.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Trusting a redactor's text search; checking the output by eye.",
      recovery="Read the page.")
def redaction_missed(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"])
    lookalike_font(doc); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@leak("page.above-page", "off-page.plain",
      "The SSN drawn above the top edge of the page, where no viewer shows it.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Nobody redacted it; the value simply sits off the visible page.",
      recovery="Copy all text, or enlarge the page box.")
def above_page(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((72, -40), f"SSN {SSN}"); save(doc, path)


@leak("page.right-of-page-embedded-font", "off-page.font",
      "The SSN right of the page edge, in an embedded font.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Nobody redacted it; the value simply sits off the visible page.",
      recovery="Enlarge the page box and map the glyph codes through the font's "
               "Unicode table.")
def right_of_page(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); font = embedded_font(page); body(page, font)
    page.insert_text((700, 100), f"SSN {SSN}", fontname=font); save(doc, path)


@leak("page.outside-crop-box", "off-page.font", "The SSN in the area a crop box hides.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
      mistake="Cropping the page to hide a line.", recovery="Remove the crop box.")
def outside_crop(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); font = embedded_font(page); body(page, font)
    page.insert_text((72, 780), f"SSN {SSN}", fontname=font)
    page.set_cropbox(fitz.Rect(0, 0, 595, 700)); save(doc, path)


@leak("page.runs-off-right-edge", "live.plain",
      "An SSN that starts on the page and runs past its right edge. Found in the "
      "stored string; the page reading splits it at the edge.",
      mistake="Nobody redacted it; the value simply runs past the visible margin.",
      recovery="Widen the page or copy the underlying string.")
def off_right_edge(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((540, 300), f"SSN {SSN}"); save(doc, path)


@leak("page.runs-off-right-edge-embedded-font", "off-page.font.straddling",
      "The same SSN running past the right edge, in an embedded font: the glyph codes "
      "are not readable as a string, and the page reading splits the value at the edge.",
      known_gap=KnownGap("off-page.font.straddling", expect(0)),
      mistake="Nobody redacted it; the value simply runs past the visible margin.",
      recovery="Widen the page and map the glyph codes through the font's Unicode table.")
def off_right_edge_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); font = embedded_font(page); body(page, font)
    page.insert_text((540, 300), f"SSN {SSN}", fontname=font); save(doc, path)


@leak("page.rotated-ssn", "live.plain", "The SSN in a rotated margin note.",
      mistake="Nobody redacted it.", recovery="Rotate the page view and read the note.")
def rotated(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((500, 600), f"SSN {SSN}", rotate=90); save(doc, path)


@leak("document.form-ssn", "annot-fields.plain", "The SSN as a form field's value.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Hidden"),)),
      mistake="Redacting the page but not the form field's stored value.",
      recovery="Open the form in any PDF editor and inspect the field's value.")
def form_field(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    widget = fitz.Widget(); widget.field_name = "ssn"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = fitz.Rect(72, 300, 300, 320); widget.field_value = SSN
    page.add_widget(widget); save(doc, path)


@leak("document.title", "metadata.plain", "The SSN in the document title.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Metadata"),)),
      requires=("exiftool",),
      mistake="Redacting the page but not the document properties.",
      recovery="File > Properties in any viewer.")
def metadata_title(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_metadata({"title": f"Case {SSN}", "creationDate": "", "modDate": ""})
    save(doc, path)


@leak("document.xmp-title", "metadata.plain", "The SSN in the XMP metadata packet's title.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Metadata"),)),
      mistake="Redacting the page but not the document's XMP metadata.",
      recovery="File > Properties, or read the /Metadata stream's XML directly.")
def xmp_title(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_xml_metadata(
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>Case {SSN}</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>'
        '<?xpacket end="w"?>')
    save(doc, path)


@leak("document.javascript", "javascript.plain",
      "The SSN inside the document's open action JavaScript.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Hidden"),)),
      mistake="Leaving a debug or prefill script with the value hard-coded.",
      recovery="Open the catalog's /OpenAction and read the /JS string.")
def javascript(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /S /JavaScript /JS (var ssn = '{SSN}';) >>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{xref} 0 R")
    save(doc, path)
