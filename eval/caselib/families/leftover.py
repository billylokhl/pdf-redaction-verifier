"""Secrets left in objects the document no longer uses: orphaned by a
deletion or a redaction saved without garbage collection."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import CODE, SSN, case, expect
from ..pdfkit import (FILLER, body, cjk_font, embedded_font, orphan, png_of,
                      redact, save, zipbytes)

ORPHANED = expect(1, findings=(("SSN", "orphaned"),))
NO_GC = "Saving without garbage collection (PyMuPDF's default garbage=0)."
RECOVER_ORPHAN = "Decompress the file (mutool clean -d) and search the raw bytes."


def _deleted_page(path: Path, font=None) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page()
    body(page, font(page) if font else "helv", lines=[f"SSN {SSN}", f"Code {CODE}"])
    doc.delete_page(1); save(doc, path)


@case("leak.orphan.deleted-page", cells="orphaned.plain", expected=ORPHANED,
      story="A page with the SSN was deleted; its content stream stayed in the file.",
      mistake=NO_GC, recovery=RECOVER_ORPHAN)
def deleted_page(path: Path) -> None:
    _deleted_page(path)


@case("leak.orphan.deleted-page-embedded-font", cells="orphaned.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="A deleted page set in an embedded font: its glyph codes are left behind.",
      mistake=NO_GC, recovery="Decode the leftover stream with the font's ToUnicode map.")
def deleted_page_embedded(path: Path) -> None:
    _deleted_page(path, embedded_font)


@case("leak.orphan.deleted-page-cjk", cells="orphaned.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="A deleted page set in an embedded CJK font.")
def deleted_page_cjk(path: Path) -> None:
    _deleted_page(path, cjk_font)


@case("leak.orphan.redacted-no-gc", cells="orphaned.plain", expected=ORPHANED,
      story="The SSN was redacted properly on the page, but the file was saved without "
            "garbage collection, so the original content stream is still inside.",
      mistake=NO_GC, recovery=RECOVER_ORPHAN)
def redacted_no_gc(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"])
    redact(doc, SSN); save(doc, path)


@case("leak.orphan.mixed-fonts", cells="orphaned.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="A deleted page mostly in a standard font, with the secret in an embedded font.")
def mixed_fonts(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); font = embedded_font(page)
    body(page, lines=list(FILLER) * 6)
    page.insert_text((72, 600), f"SSN {SSN}", fontname=font)
    page.insert_text((72, 620), f"Code {CODE}", fontname=font)
    doc.delete_page(1); save(doc, path)


@case("leak.orphan.xmp", cells="leftover-xmp.plain", expected=ORPHANED,
      story="An old XMP metadata packet with the SSN in its title, no longer referenced.")
def orphan_xmp(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    xmp = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
           '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>Case {SSN}</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>'
           '<?xpacket end="w"?>')
    orphan(doc, "<< /Type /Metadata /Subtype /XML >>", xmp.encode()); save(doc, path)


@case("leak.orphan.attachment-text", cells="orphaned-attachment.plain",
      expected=expect(2, warnings=("REVIEW_OBJECT_TEXT",)),
      story="A removed attachment's text is still stored, unreferenced.")
def orphan_attachment_text(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Type /EmbeddedFile >>", f"Employee SSN {SSN}\n".encode()); save(doc, path)


@case("leak.orphan.attachment-untyped", cells="orphaned-attachment.plain",
      expected=expect(2, warnings=("REVIEW_OBJECT_TEXT",)),
      story="A removed attachment stored without a /Type.")
def orphan_attachment_untyped(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Params << /Size 22 >> >>", f"Employee SSN {SSN}\n".encode())
    save(doc, path)


@case("leak.orphan.attachment-zip", cells=("orphaned.container", "orphaned-attachment.container"),
      expected=expect(2, warnings=("LEFTOVER_CONTAINER",)),
      story="A removed zip attachment holding the SSN, still stored.")
def orphan_attachment_zip(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Type /EmbeddedFile >>", zipbytes(f"SSN {SSN}")); save(doc, path)


@case("leak.orphan.inline-image", cells="orphaned.pixels",
      expected=expect(2, warnings=("LEFTOVER_IMAGE",)),
      story="A leftover content stream drawing the SSN as an inline image.")
def orphan_inline_image(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(f"SSN {SSN}")))
    w, h = gray.width, gray.height
    data = (f"q {w / 2} 0 0 {h / 2} 72 400 cm BI /W {w} /H {h} /BPC 8 /CS /G ID ".encode()
            + gray.samples + b"\nEI Q")
    orphan(doc, "<< >>", data); save(doc, path)


@case("leak.orphan.single-stream-mixed", cells="orphaned.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="One leftover stream: many lines of plain text, and the SSN as glyph codes.")
def single_stream_mixed(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< >>", mixed_stream(embedded_glyphs(f"SSN {SSN}"))); save(doc, path)


def embedded_glyphs(text: str) -> str:
    """Hex glyph codes for *text* in the embedded font, as a PDF string."""
    font = fitz.Font("helv")
    return "<" + "".join(f"{font.has_glyph(ord(c)):04X}" for c in text) + ">"


def mixed_stream(secret_hex: str | None) -> bytes:
    lines, y = [], 780
    for text in list(FILLER) * 8:
        lines.append(f"BT /helv 11 Tf 72 {y} Td ({text}) Tj ET"); y -= 14
    if secret_hex:
        lines.append(f"BT /EM 11 Tf 72 {y} Td {secret_hex} Tj ET")
    return "\n".join(lines).encode()


@case("leak.orphan.attachment-png", cells="orphaned-attachment.pixels",
      expected=expect(2, warnings=("LEFTOVER_CONTAINER",)),
      story="A removed PNG attachment showing the SSN, still stored.")
def orphan_attachment_png(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Type /EmbeddedFile >>", png_of(f"SSN {SSN}")); save(doc, path)
