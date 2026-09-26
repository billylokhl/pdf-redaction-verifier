"""Cases pinning the remaining ``UNDOCUMENTED_GAPS`` (docs/REDESIGN.md
Phase 0b-2): every ✗ cell that had no case yet gets one here, built with
the raw writer where the structure matters and fitz where the story is
purely visual (a box drawn over an image, a two-column layout).

Each case's ``known_gap`` pins today's tool actually returning exit 0
with the secret still in the file — the same "silent miss" contract as
``families/raw.py``'s K1-K11."""

from __future__ import annotations

import base64
from pathlib import Path

import fitz

from ..model import SSN, Expect, KnownGap, case, expect
from ..pdfkit import png_of, compressed
from ..rawpdf import build, one_page, page, stream, text
from .raw_cells import attachment_objects, coded, differences_font

SECRET = f"SSN {SSN}"
PAGE = stream(b"", text("Quarterly report"))
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
ORPHAN_SSN = expect(1, findings=(("SSN", "orphaned"),))
REVIEW_BINARY_LIVE = expect(2, warnings=(("REVIEW_BINARY", "live"),))
PATTERN_RULE = ({"name": "Any SSN", "class": "ssn"},)
HIDDEN_LAYER_CATALOG = (
    b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [7 0 R] "
    b"/D << /OFF [7 0 R] /Order [7 0 R] >> >> >>"
)


def _write(path: Path, objects: dict[int, bytes], **kw) -> None:
    path.write_bytes(build(objects, **kw))


def leak_raw(id: str, cells: str, story: str, *, expected: Expect, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story,
                writer="raw", **kw)


def leak_fitz(id: str, cells: str, story: str, *, expected: Expect, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story,
                writer="fitz", **kw)


def _gray_image_dict(width: int, height: int) -> bytes:
    return (b"/Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/BitsPerComponent 8 /ColorSpace /DeviceGray" % (width, height))


def _inline_gray(width: int, height: int, samples: bytes, x: int = 0, y: int = 0) -> bytes:
    return (b"q %d 0 0 %d %d %d cm BI /W %d/H %d/BPC 8/CS/G ID " % (width, height, x, y, width, height)
            + samples + b"\nEI Q")


# ── live.pixels.under-box ───────────────────────────────────────────────

@leak_fitz(
    "page.pixels-under-box", "live.pixels.under-box",
    "A scanned image of the SSN with a black box drawn over it on the same page: "
    "the pixels are still in the file, but OCR only ever sees the rendered, "
    "composited page — the box, not what is under it.",
    expected=LIVE_SSN,
    known_gap=KnownGap("live.pixels.under-box", expect(0)),
    mistake="Drawing a box over a scanned image instead of removing or replacing it.",
    recovery="Extract the image object directly and OCR it, ignoring what is drawn over it.",
)
def pixels_under_box(path: Path) -> None:
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "Quarterly report")
    rect = fitz.Rect(72, 200, 372, 260)
    pg.insert_image(rect, stream=png_of(SECRET))
    pg.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(str(path), no_new_id=True)
    doc.close()


# ── off-page.font.no-unicode ────────────────────────────────────────────

@leak_raw(
    "page.off-page-no-unicode-font", "off-page.font.no-unicode",
    "The SSN off the right edge of the page, in an embedded CID font with no "
    "/ToUnicode map at all: even a reader that looked past the page edge could "
    "not turn the glyph codes back into characters.",
    expected=LIVE_SSN,
    known_gap=KnownGap("off-page.font.no-unicode", expect(0)),
)
def off_page_no_unicode(path: Path) -> None:
    descendant = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Fake /CIDSystemInfo "
                  b"<< /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                  b"/FontDescriptor 9 0 R /CIDToGIDMap /Identity /DW 600 >>")
    descriptor = (b"<< /Type /FontDescriptor /FontName /Fake /Flags 4 "
                  b"/FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800 /Descent -200 "
                  b"/CapHeight 700 /StemV 80 >>")
    type0 = (b"<< /Type /Font /Subtype /Type0 /BaseFont /Fake /Encoding /Identity-H "
             b"/DescendantFonts [7 0 R] >>")
    codes = b"".join(bytes([0, i]) for i in range(1, len(SECRET) + 1))
    content = stream(b"", b"BT /F1 12 Tf 700 100 Td <" + codes.hex().encode() + b"> Tj ET")
    objects = one_page(content, o6=type0)
    objects[7] = descendant
    objects[9] = descriptor
    _write(path, objects)


# ── off-page.pixels ─────────────────────────────────────────────────────

@leak_fitz(
    "page.pixels-off-page", "off-page.pixels",
    "A scanned image of the SSN placed entirely outside the page's media box.",
    expected=LIVE_SSN,
    known_gap=KnownGap("off-page.pixels", expect(0)),
)
def pixels_off_page(path: Path) -> None:
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "Quarterly report")
    pg.insert_image(fitz.Rect(700, 700, 1000, 760), stream=png_of(SECRET))
    doc.save(str(path), no_new_id=True)
    doc.close()


# ── oc-off.font ──────────────────────────────────────────────────────────

@leak_raw(
    "page.hidden-layer-font-coded", "oc-off.font",
    "The SSN as font-coded text (a custom /Differences encoding) in a "
    "switched-off optional-content layer: nothing renders the layer, and the "
    "Objects layer's literal-string scan never applies a font's map.",
    expected=LIVE_SSN,
    known_gap=KnownGap("oc-off.font", expect(0)),
)
def hidden_layer_font(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = HIDDEN_LAYER_CATALOG
    objects[3] = page(b"[4 0 R 5 0 R]",
                      b"<< /Font << /F1 6 0 R >> /Properties << /oc1 7 0 R >> >>")
    objects[5] = stream(b"", b"/OC /oc1 BDC BT /F1 12 Tf 72 700 Td "
                        + coded(SECRET) + b" Tj ET EMC")
    objects[6] = differences_font(SECRET)
    objects[7] = b"<< /Type /OCG /Name (Layer 1) >>"
    _write(path, objects)


# ── annot-appearance.font / .pixels ─────────────────────────────────────

@leak_raw(
    "page.hidden-annotation-font-coded", "annot-appearance.font",
    "A hidden annotation whose appearance draws the SSN in a font with a "
    "custom /Differences encoding.",
    expected=LIVE_SSN,
    known_gap=KnownGap("annot-appearance.font", expect(0)),
)
def hidden_annotation_font(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Square /Rect [72 500 300 530] /F 2 "
                  b"/AP << /N 8 0 R >> >>")
    objects[8] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 228 30] "
                        b"/Resources << /Font << /F1 9 0 R >> >>",
                        b"BT /F1 12 Tf 0 10 Td " + coded(SECRET) + b" Tj ET")
    objects[9] = differences_font(SECRET)
    _write(path, objects)


@leak_raw(
    "page.hidden-annotation-pixels", "annot-appearance.pixels",
    "A hidden annotation whose appearance draws a scanned image of the SSN.",
    expected=LIVE_SSN,
    known_gap=KnownGap("annot-appearance.pixels", expect(0)),
)
def hidden_annotation_pixels(path: Path) -> None:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(SECRET)))
    w, h = gray.width, gray.height
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Square /Rect [72 500 %d %d] /F 2 "
                  b"/AP << /N 8 0 R >> >>" % (72 + w, 500 + h))
    objects[8] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 %d %d]" % (w, h),
                        _inline_gray(w, h, gray.samples))
    _write(path, objects)


# ── unused-resource.font / .pixels ──────────────────────────────────────

@leak_raw(
    "page.unused-form-resource-font-coded", "unused-resource.font",
    "A form XObject in the page's resources, never drawn, showing the SSN in a "
    "font with a custom /Differences encoding.",
    expected=LIVE_SSN,
    known_gap=KnownGap("unused-resource.font", expect(0)),
)
def unused_resource_font(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R >> /XObject << /Fm1 7 0 R >> >>")
    objects[7] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                        b"/Resources << /Font << /F1 9 0 R >> >>",
                        b"BT /F1 12 Tf 72 500 Td " + coded(SECRET) + b" Tj ET")
    objects[9] = differences_font(SECRET)
    _write(path, objects)


@leak_raw(
    "page.unused-form-resource-pixels", "unused-resource.pixels",
    "A form XObject in the page's resources, never drawn, showing a scanned "
    "image of the SSN.",
    expected=LIVE_SSN,
    known_gap=KnownGap("unused-resource.pixels", expect(0)),
)
def unused_resource_pixels(path: Path) -> None:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(SECRET)))
    w, h = gray.width, gray.height
    objects = one_page(PAGE)
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R >> /XObject << /Fm1 7 0 R >> >>")
    objects[7] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 %d %d]" % (w, h),
                        _inline_gray(w, h, gray.samples))
    _write(path, objects)


# ── orphaned.font.ordinary-codes ────────────────────────────────────────

_DIGIT_NAMES = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
                "6": "six", "7": "seven", "8": "eight", "9": "nine", "-": "hyphen"}


@leak_raw(
    "leftover.ordinary-looking-codes", "orphaned.font.ordinary-codes",
    "An orphaned stream shows the plain-looking string 'ABCDEFGHIJK', which reads "
    "as ordinary text; a font mapping those exact codes to other glyphs — "
    "reattached to a page — would render it as the SSN. Because the raw codes "
    "look like normal text, nothing flags the stream as undecodable, and the "
    "literal (wrong) characters simply do not match.",
    expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),)),
    known_gap=KnownGap("orphaned.font.ordinary-codes", expect(0)),
)
def ordinary_looking_codes(path: Path) -> None:
    raw_chars = "ABCDEFGHIJK"
    names = b" ".join(b"/" + _DIGIT_NAMES[c].encode() for c in SSN)
    font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding "
            b"<< /Type /Encoding /Differences [65 " + names + b"] >> >>")
    content = stream(b"", b"BT /F9 11 Tf 72 700 Td (" + raw_chars.encode() + b") Tj ET")
    _write(path, one_page(PAGE, o5=content, o9=font))


# ── orphaned.pixels.small ────────────────────────────────────────────────

@leak_raw(
    "leftover.small-image", "orphaned.pixels.small",
    "An orphaned image of the SSN just under the size gate (7 px tall): still a "
    "readable line of text, but the cutoff that filters out icons and masks "
    "skips it too.",
    expected=ORPHAN_SSN,
    known_gap=KnownGap("orphaned.pixels.small", expect(0)),
)
def small_orphaned_image(path: Path) -> None:
    tmp = fitz.open()
    tiny = tmp.new_page(width=31, height=7)
    tiny.insert_text((0, 5), SECRET, fontsize=5)
    png = tiny.get_pixmap(dpi=72).tobytes("png")
    tmp.close()
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png))
    w, h = gray.width, gray.height
    img = stream(_gray_image_dict(w, h), gray.samples)
    _write(path, one_page(PAGE, o5=img))


# ── metadata.pixels / leftover-xmp.pixels (XMP thumbnails) ─────────────

def _xmp_with_thumbnail(b64: str) -> str:
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        'xmlns:xmpGImg="http://ns.adobe.com/xap/1.0/g/img/">'
        '<xmp:Thumbnails><rdf:Alt><rdf:li rdf:parseType="Resource">'
        '<xmpGImg:format>PNG</xmpGImg:format>'
        f'<xmpGImg:image>{b64}</xmpGImg:image>'
        '</rdf:li></rdf:Alt></xmp:Thumbnails>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )


@leak_fitz(
    "document.xmp-thumbnail", "metadata.pixels",
    "The SSN as a page-thumbnail image inside the document's own XMP metadata "
    "packet: the Metadata layer searches the XMP as text, but a base64 "
    "thumbnail image is pixels, not text.",
    expected=LIVE_SSN,
    known_gap=KnownGap("metadata.pixels", expect(0)),
)
def xmp_thumbnail(path: Path) -> None:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Quarterly report")
    b64 = base64.b64encode(png_of(SECRET)).decode("ascii")
    doc.set_xml_metadata(_xmp_with_thumbnail(b64))
    doc.save(str(path), no_new_id=True)
    doc.close()


@leak_raw(
    "leftover.xmp-thumbnail", "leftover-xmp.pixels",
    "An old XMP metadata packet, no longer referenced, holding the SSN only as "
    "a base64 thumbnail image.",
    expected=ORPHAN_SSN,
    known_gap=KnownGap("leftover-xmp.pixels", expect(0)),
)
def orphan_xmp_thumbnail(path: Path) -> None:
    b64 = base64.b64encode(png_of(SECRET)).decode("ascii")
    xmp = _xmp_with_thumbnail(b64)
    objects = one_page(PAGE, o5=stream(b"/Type /Metadata /Subtype /XML", xmp.encode()))
    _write(path, objects)


# ── thumbnail.pixels (/Thumb) ────────────────────────────────────────────

@leak_raw(
    "page.thumb-image", "thumbnail.pixels",
    "The SSN as the page's own /Thumb preview image: referenced by the page, so "
    "it is live, but nothing renders or OCRs a page thumbnail.",
    expected=LIVE_SSN,
    known_gap=KnownGap("thumbnail.pixels", expect(0)),
)
def page_thumb(path: Path) -> None:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(SECRET)))
    w, h = gray.width, gray.height
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/Thumb 7 0 R")
    objects[7] = stream(_gray_image_dict(w, h), gray.samples)
    _write(path, objects)


# ── attachment.container.text-encoded ───────────────────────────────────

@leak_raw(
    "attachment.base64-zip", "attachment.container.text-encoded",
    "A zip of records attached as base64 text inside an .eml file: it has no "
    "zip signature at the start, so the tool reads it as plain readable text "
    "and finds nothing there — base64 hides the digits it searches for.",
    expected=expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),)),
    known_gap=KnownGap("attachment.container.text-encoded", expect(0)),
)
def base64_attachment(path: Path) -> None:
    zip_bytes = compressed("zip", SECRET)
    eml = (b"From: sender@example.com\r\nTo: recipient@example.com\r\n"
           b"Subject: Records\r\nContent-Type: application/zip\r\n"
           b"Content-Transfer-Encoding: base64\r\n\r\n"
           + base64.b64encode(zip_bytes) + b"\r\n")
    objects = one_page(PAGE)
    objects.update(attachment_objects("records.eml", eml))
    _write(path, objects)


# ── embedded-other.plain.pattern-rules / .pixels / .container ──────────
# A PDF 2.0 /AF file not on the attachments list: only the Binary (qpdf)
# layer's raw byte sweep reads it at all, and that sweep is value secrets
# only (never pattern rules), on undecoded bytes (never containers or
# images unpacked).

def _af_objects(filename: bytes, data: bytes) -> dict[int, bytes]:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/AF [7 0 R]")
    objects[7] = (b"<< /Type /Filespec /F (" + filename + b") /EF << /F 8 0 R >> "
                  b"/AFRelationship /Source >>")
    objects[8] = stream(b"/Type /EmbeddedFile", data)
    return objects


@leak_raw(
    "document.af-pattern-rule", "embedded-other.plain.pattern-rules",
    "A PDF 2.0 associated file holding the SSN, found only by a pattern rule: "
    "the Binary layer's raw-stream sweep — the only thing that reads an /AF "
    "file's content today — matches known values only, never pattern rules.",
    expected=REVIEW_BINARY_LIVE,
    known_gap=KnownGap("embedded-other.plain.pattern-rules", expect(0)),
    rules=PATTERN_RULE, requires=("qpdf",),
)
def af_pattern_rule(path: Path) -> None:
    _write(path, _af_objects(b"source.txt", b"Source notes: " + SECRET.encode()))


@leak_raw(
    "document.af-image", "embedded-other.pixels",
    "A PDF 2.0 associated file: a scan of the SSN. The Binary layer's raw sweep "
    "matches text bytes; compressed image data does not contain the value's "
    "literal digits.",
    expected=REVIEW_BINARY_LIVE,
    known_gap=KnownGap("embedded-other.pixels", expect(0)),
    requires=("qpdf",),
)
def af_image(path: Path) -> None:
    _write(path, _af_objects(b"scan.png", png_of(SECRET)))


@leak_raw(
    "document.af-container", "embedded-other.container",
    "A PDF 2.0 associated file: a zip of records holding the SSN. Compressed "
    "container bytes do not contain the value's literal digits either.",
    expected=REVIEW_BINARY_LIVE,
    known_gap=KnownGap("embedded-other.container", expect(0)),
    requires=("qpdf",),
)
def af_container(path: Path) -> None:
    _write(path, _af_objects(b"records.zip", compressed("zip", SECRET)))


# ── javascript.plain.pattern-rules-in-streams ───────────────────────────

@leak_raw(
    "document.js-stream-pattern-rule", "javascript.plain.pattern-rules-in-streams",
    "A link's JavaScript action stores its code in a stream rather than an "
    "inline string; found only by a pattern rule, which the tool never applies "
    "to raw stream bytes — only to PDF string-literal syntax.",
    expected=REVIEW_BINARY_LIVE,
    known_gap=KnownGap("javascript.plain.pattern-rules-in-streams", expect(0)),
    rules=PATTERN_RULE,
)
def js_stream_pattern(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Link /Rect [72 500 300 530] "
                  b"/A << /S /JavaScript /JS 8 0 R >> >>")
    objects[8] = stream(b"", b"var ssn = '" + SSN.encode() + b"';")
    _write(path, objects)


# ── private-data.plain.pattern-rules ────────────────────────────────────

@leak_raw(
    "document.piece-info-pattern-rule", "private-data.plain.pattern-rules",
    "An editor's private data (/PieceInfo) holds the SSN, found only by a "
    "pattern rule: the Binary layer's raw sweep — the only thing that reads "
    "live /PieceInfo streams — matches known values only.",
    expected=REVIEW_BINARY_LIVE,
    known_gap=KnownGap("private-data.plain.pattern-rules", expect(0)),
    rules=PATTERN_RULE, requires=("qpdf",),
)
def piece_info_pattern(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/PieceInfo << /Editor << /Private 7 0 R >> >>")
    objects[7] = stream(b"", b"original line: " + SSN.encode())
    _write(path, objects)


# ── unindexed.font / .pixels / .container ───────────────────────────────
# Bytes after the file's final %%EOF are outside any indexed object, the
# same structural blind spot as K7 (unindexed.plain in families/raw.py),
# whatever they encode.

@leak_raw(
    "file.after-final-eof-font-coded", "unindexed.font",
    "Font-coded glyph codes for the SSN, appended after the file's final "
    "%%EOF: entirely outside any indexed object, so nothing reads it at all.",
    expected=expect(1),
    known_gap=KnownGap("unindexed.font", expect(0)),
)
def unindexed_font(path: Path) -> None:
    _write(path, one_page(PAGE), after_eof=b"\n" + coded(SECRET) + b"\n")


@leak_raw(
    "file.after-final-eof-pixels", "unindexed.pixels",
    "Raw pixel bytes for a scan of the SSN, appended after the file's final "
    "%%EOF.",
    expected=expect(1),
    known_gap=KnownGap("unindexed.pixels", expect(0)),
)
def unindexed_pixels(path: Path) -> None:
    # A small strip (well under qpdf's 1024-byte end-of-file lookback), so
    # the trailing bytes demonstrate the unindexed blind spot without also
    # pushing the real startxref out of qpdf's damaged-file recovery window.
    tmp = fitz.open()
    tiny = tmp.new_page(width=31, height=7)
    tiny.insert_text((0, 5), SECRET, fontsize=5)
    png = tiny.get_pixmap(dpi=72).tobytes("png")
    tmp.close()
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png))
    _write(path, one_page(PAGE), after_eof=b"\n" + gray.samples + b"\n")


@leak_raw(
    "file.after-final-eof-container", "unindexed.container",
    "A zip of records, appended after the file's final %%EOF.",
    expected=expect(1),
    known_gap=KnownGap("unindexed.container", expect(0)),
)
def unindexed_container(path: Path) -> None:
    _write(path, one_page(PAGE), after_eof=b"\n" + compressed("zip", SECRET) + b"\n")


# ── match.columns ────────────────────────────────────────────────────────

@leak_fitz(
    "layout.two-column-wrap", "match.columns",
    "The SSN wrapped across two lines inside the left column of a two-column "
    "page, with an unrelated line of the right column sitting between them at "
    "the same height: joining the page's lines top-to-bottom (not column by "
    "column) puts that unrelated text between the two halves. OCR happens to "
    "read this particular page column by column and would find it anyway, so "
    "the gap is judged only where OCR is absent.",
    expected=expect(1, findings=(("SSN", "live"),)),
    known_gap=KnownGap("match.columns", expect(0)),
    requires=("no-ocr",),
)
def two_column_wrap(path: Path) -> None:
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 100), "Left column heading")
    pg.insert_text((72, 130), "SSN 123-45-")
    pg.insert_text((320, 148), "Right column: unrelated filler text here.")
    pg.insert_text((72, 166), "6789 is the reference number.")
    doc.save(str(path), no_new_id=True)
    doc.close()


# ── match.extreme-coordinates ────────────────────────────────────────────

@leak_raw(
    "page.extreme-coordinates", "match.extreme-coordinates",
    "The SSN split across two content-stream objects on one page, both drawn at "
    "coordinates around 10^9 points: PyMuPDF's text extraction does not return "
    "glyphs placed that far out, so neither half is read at all, and the "
    "Objects layer's per-object literal scan never sees either half whole.",
    expected=LIVE_SSN,
    known_gap=KnownGap("match.extreme-coordinates", expect(0)),
)
def extreme_coordinates(path: Path) -> None:
    first, second = SECRET[:8], SECRET[8:]
    content_a = stream(b"", b"BT /F1 12 Tf 1000000000 1000000000 Td ("
                       + first.encode() + b") Tj ET")
    content_b = stream(b"", b"BT /F1 12 Tf 1000000000 999999950 Td ("
                       + second.encode() + b") Tj ET")
    objects = one_page(content_a)
    objects[3] = page(contents=b"[4 0 R 5 0 R]")
    objects[5] = content_b
    _write(path, objects)


# ── false-alarm.binary-value-collision ──────────────────────────────────

@case(
    "page.binary-digit-collision", truth="clean", features="live.plain", writer="raw",
    expected=expect(0),
    story="An opaque image payload happens to contain the byte sequence for "
          "\"123456789\" (the SSN's digits, no separators): the Binary layer's "
          "raw byte sweep cannot tell a coincidental run inside binary data from "
          "a real leak, so a clean file gets a manual-review warning anyway.",
    known_gap=KnownGap("false-alarm.binary-value-collision",
                        expect(2, warnings=(("REVIEW_BINARY", "live"),))),
    requires=("qpdf",),
)
def binary_digit_collision(path: Path) -> None:
    # qpdf's QDF rewrite (like the reachability walk the Objects layer
    # trusts) drops objects nothing references — an orphaned image would
    # never reach the Binary layer's sweep at all. Referencing it from the
    # page's resources (undrawn, like unused-resource.*) keeps it live and
    # opaque (an /Image XObject: never parsed as text by the Objects layer)
    # so only the Binary layer's raw byte sweep ever sees these bytes.
    noise = bytes(range(256)) * 4
    payload = noise + b"123456789" + noise
    objects = one_page(PAGE)
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R >> /XObject << /Im1 7 0 R >> >>")
    objects[7] = stream(
        b"/Type /XObject /Subtype /Image /Width 4 /Height 4 /BitsPerComponent 8 "
        b"/ColorSpace /DeviceGray", payload)
    _write(path, objects)
