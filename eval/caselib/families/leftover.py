"""Secrets left in objects the document no longer uses: orphaned by a
deletion, or by a redaction saved without garbage collection."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import CODE, SSN, case, expect
from ..pdfkit import (FILLER, body, cjk_font, compressed, embedded_font, orphan,
                      png_of, redact, save)

ORPHANED = expect(1, findings=(("SSN", "orphaned"),))
UNDECODABLE = expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),))
NO_GC = "Saving without garbage collection (PyMuPDF's default garbage=0)."
RECOVER_HEX = ("Decompress the file (mutool clean -d), find the leftover content stream, and "
               "decode its hex strings — each pair of hex digits is one character.")
RECOVER_GLYPHS = ("Decompress the file, then map the leftover stream's glyph codes to characters "
                  "through the font's ToUnicode table.")


def leak(id: str, cells, story: str, expected=ORPHANED, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story, **kw)


def _deleted_page(path: Path, font=None, compact: bool = False) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page()
    body(page, font(page) if font else "helv", lines=[f"SSN {SSN}", f"Code {CODE}"])
    if compact:
        # Rewrite the page as a compact content stream (as clean_contents,
        # Acrobat and many producers write it) and drop the verbose copy.
        page.clean_contents()
        doc = fitz.open("pdf", doc.tobytes(garbage=4, no_new_id=True))
    doc.delete_page(1); save(doc, path)


@leak("leftover.deleted-page", "orphaned.plain",
      "A page with the SSN was deleted; its content stream stayed in the file.",
      mistake=NO_GC, recovery=RECOVER_HEX)
def deleted_page(path: Path) -> None:
    _deleted_page(path)


@leak("leftover.deleted-page-embedded-font", "orphaned.font",
      "A deleted page set in an embedded font: its glyph codes are left behind.",
      expected=UNDECODABLE, mistake=NO_GC, recovery=RECOVER_GLYPHS)
def deleted_page_embedded(path: Path) -> None:
    _deleted_page(path, embedded_font)


@leak("leftover.deleted-page-embedded-font-compact", "orphaned.font.compact-syntax",
      "The same deleted page, its content written compactly (\"BT/EM 11 Tf\", no space "
      "after BT), as clean_contents and many producers write it.",
      expected=UNDECODABLE,
      mistake=NO_GC, recovery=RECOVER_GLYPHS)
def deleted_page_embedded_compact(path: Path) -> None:
    _deleted_page(path, embedded_font, compact=True)


@leak("leftover.deleted-page-cjk", "orphaned.font",
      "A deleted page set in an embedded CJK font.", expected=UNDECODABLE)
def deleted_page_cjk(path: Path) -> None:
    _deleted_page(path, cjk_font)


@leak("leftover.redacted-no-gc", "orphaned.plain",
      "The SSN was redacted properly on the page, but the file was saved without "
      "garbage collection, so the original content stream is still inside.",
      mistake=NO_GC, recovery=RECOVER_HEX)
def redacted_no_gc(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"])
    redact(doc, SSN); save(doc, path)


@leak("leftover.redacted-no-gc-embedded-font-compact", "orphaned.font.compact-syntax",
      "The same, with the page in an embedded font written compactly: the original "
      "stream's glyph codes stay in the file.",
      expected=UNDECODABLE,
      mistake=NO_GC, recovery=RECOVER_GLYPHS)
def redacted_no_gc_compact(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"])
    page.clean_contents()
    doc = fitz.open("pdf", doc.tobytes(garbage=4, no_new_id=True))
    redact(doc, SSN); save(doc, path)


@leak("leftover.mixed-fonts", "orphaned.font",
      "A deleted page mostly in a standard font, with the secret in an embedded font.",
      expected=UNDECODABLE)
def mixed_fonts(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); font = embedded_font(page)
    body(page, lines=list(FILLER) * 6)
    page.insert_text((72, 600), f"SSN {SSN}", fontname=font)
    page.insert_text((72, 620), f"Code {CODE}", fontname=font)
    doc.delete_page(1); save(doc, path)


@leak("leftover.single-stream-mixed", "orphaned.font",
      "One leftover stream: many lines of plain text, and the SSN as glyph codes.",
      expected=UNDECODABLE)
def single_stream_mixed(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< >>", mixed_stream(embedded_glyphs(f"SSN {SSN}"))); save(doc, path)


@leak("leftover.xmp", "leftover-xmp.plain",
      "An old XMP metadata packet with the SSN in its title, no longer referenced.")
def orphan_xmp(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    xmp = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
           '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>Case {SSN}</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>'
           '<?xpacket end="w"?>')
    orphan(doc, "<< /Type /Metadata /Subtype /XML >>", xmp.encode()); save(doc, path)


@leak("leftover.attachment-text", "orphaned-attachment.plain",
      "A removed attachment's text is still stored, unreferenced (manual review: a run "
      "of arbitrary text).",
      expected=expect(2, warnings=(("REVIEW_OBJECT_TEXT", "orphaned"),)))
def orphan_attachment_text(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Type /EmbeddedFile >>", f"Employee SSN {SSN}\n".encode()); save(doc, path)


@leak("leftover.attachment-untyped", "orphaned-attachment.plain",
      "A removed attachment stored without a /Type.",
      expected=expect(2, warnings=(("REVIEW_OBJECT_TEXT", "orphaned"),)))
def orphan_attachment_untyped(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Params << /Size 22 >> >>", f"Employee SSN {SSN}\n".encode())
    save(doc, path)


@leak("leftover.attachment-zip", ("orphaned-attachment.container", "orphaned.container"),
      "A removed zip attachment holding the SSN, still stored.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)))
def orphan_attachment_zip(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Type /EmbeddedFile >>", compressed("zip", f"SSN {SSN}")); save(doc, path)


@leak("leftover.attachment-png", "orphaned-attachment.pixels",
      "A removed PNG attachment showing the SSN, still stored.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)))
def orphan_attachment_png(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    orphan(doc, "<< /Type /EmbeddedFile >>", png_of(f"SSN {SSN}")); save(doc, path)


@leak("leftover.inline-image", "orphaned.pixels",
      "A leftover content stream drawing the SSN as an inline image.",
      expected=expect(2, warnings=(("LEFTOVER_IMAGE", "orphaned"),)))
def orphan_inline_image(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(f"SSN {SSN}")))
    w, h = gray.width, gray.height
    data = (f"q {w / 2} 0 0 {h / 2} 72 400 cm BI /W {w} /H {h} /BPC 8 /CS /G ID ".encode()
            + gray.samples + b"\nEI Q")
    orphan(doc, "<< >>", data); save(doc, path)


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


@leak("leftover.deleted-page-long-text-object", "orphaned.font",
      "A deleted page whose text was inserted in one call, so all 60 lines — the SSN "
      "last — are one text object over 10 KB long.", expected=UNDECODABLE,
      mistake=NO_GC, recovery=RECOVER_GLYPHS)
def deleted_page_long_text(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); font = embedded_font(page)
    lines = [f"Ledger row {i} reconciled against the monthly statement" for i in range(60)]
    page.insert_text((36, 40), "\n".join([*lines, f"SSN {SSN}"]), fontname=font, fontsize=6)
    doc = fitz.open("pdf", doc.tobytes(garbage=4, no_new_id=True))
    doc.delete_page(1); save(doc, path)
