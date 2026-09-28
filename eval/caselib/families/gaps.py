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
from ..pdfkit import compressed, embedded_font, png_of
from ..rawpdf import build, one_page, page, stream, text
from .raw_cells import attachment_objects, coded, differences_font

SECRET = f"SSN {SSN}"
PAGE = stream(b"", text("Quarterly report"))
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
ORPHAN_SSN = expect(1, findings=(("SSN", "orphaned"),))
REVIEW_BINARY_LIVE = expect(2, warnings=(("REVIEW_BINARY", "live"),))
LEFTOVER_IMAGE_LIVE = expect(2, warnings=(("LEFTOVER_IMAGE", "live"),))
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


# ── live.font.overprinted ───────────────────────────────────────────────

@leak_fitz(
    "page.overprinted-embedded-font", "live.font.overprinted",
    "The SSN in an embedded (Identity-H) font, drawn starting at the exact same "
    "point as other page text: the two runs' glyphs share one baseline and "
    "interleave by x-position in both the Text layer's own reading and OCR's "
    "rendered pixels, so neither line comes out intact (verified: a plain, "
    "non-embedded Helvetica at the same point is still caught by the Objects "
    "layer's literal-string scan regardless of position, and a 20pt vertical "
    "offset — no more overlap — is read normally by both Text and OCR).",
    expected=LIVE_SSN,
    known_gap=KnownGap("live.font.overprinted", expect(0)),
    mistake="A redaction overlay or annotation reusing the exact insertion point of the text it covers.",
    recovery="Extract the page's raw glyph list with positions and separate the two overlapping runs by font/encoding.",
)
def overprinted_embedded_font(path: Path) -> None:
    doc = fitz.open()
    pg = doc.new_page()
    font = embedded_font(pg)
    pg.insert_text((72, 300), "Quarterly report", fontname=font)
    pg.insert_text((72, 300), SECRET, fontname=font)
    doc.save(str(path), no_new_id=True)
    doc.close()


# ── off-page.font.no-unicode ────────────────────────────────────────────

@leak_fitz(
    "page.off-page-no-unicode-font", "off-page.font.no-unicode",
    "The SSN off the right edge of the page, in a real embedded (Identity-H) "
    "font whose /ToUnicode map has been stripped: the glyphs are genuine — "
    "rendering the page (widening the media box) and OCRing it reads the SSN "
    "plainly — but with no /ToUnicode, no character-based reading (on or off "
    "the page) can turn the codes back into text; with the map kept, the same "
    "file is read (exit 1) by the Text layer today.",
    expected=LIVE_SSN,
    known_gap=KnownGap("off-page.font.no-unicode", expect(0)),
    mistake="Stripping ToUnicode from an embedded font (or using one that never had it) "
            "and moving the text off the visible page.",
    recovery="Widen the media box and render the page, then OCR it — the glyphs are real.",
)
def off_page_no_unicode(path: Path) -> None:
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "Quarterly report")
    font = embedded_font(pg)
    pg.insert_text((700, 100), SECRET, fontname=font)
    for xref in range(1, doc.xref_length()):
        kind, subtype = doc.xref_get_key(xref, "Subtype")
        if kind == "name" and subtype == "/Type0":
            doc.xref_set_key(xref, "ToUnicode", "null")
    doc.save(str(path), no_new_id=True)
    doc.close()


# ── off-page.pixels ─────────────────────────────────────────────────────

@leak_fitz(
    "page.pixels-off-page", "off-page.pixels",
    "A scanned image of the SSN placed entirely outside the page's media box.",
    expected=LIVE_SSN,
    known_gap=KnownGap("off-page.pixels", expect(0)),
    mistake="Placing a scanned image beyond the page's own edges instead of removing it.",
    recovery="Widen the media box and render the page, or extract the image object directly.",
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
    mistake="Switching off an optional-content layer instead of deleting its content.",
    recovery="Turn the layer on in the viewer's layers panel, then read or OCR it.",
)
def hidden_layer_font(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = HIDDEN_LAYER_CATALOG
    objects[3] = page(b"[4 0 R 5 0 R]",
                      b"<< /Font << /F1 6 0 R >> /Properties << /oc1 7 0 R >> >>")
    # y=500, not 700: PAGE already draws "Quarterly report" at 72 700 — drawn
    # at the same point, the SSN's glyphs would overlap it and (verified)
    # even a human turning the layer on could not read either line.
    objects[5] = stream(b"", b"/OC /oc1 BDC BT /F1 12 Tf 72 500 Td "
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
    mistake="Hiding an annotation instead of deleting it.",
    recovery="Show hidden annotations in the viewer, or render the appearance stream directly.",
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
    mistake="Hiding an annotation instead of deleting it.",
    recovery="Show hidden annotations in the viewer, or extract and OCR the appearance image.",
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
    mistake="Leaving an unused resource in the page dictionary instead of removing it.",
    recovery="List the page's resources and render each one, even those never drawn.",
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
    "image of the SSN. Unused under ADR 0004 owner decision D (drawn only by a form "
    "nothing draws), so the image is always FLAGGED — not a finding, since nothing "
    "reassembles or reads an unused image.",
    expected=LEFTOVER_IMAGE_LIVE,
    known_gap=KnownGap("unused-resource.pixels", expect(0)),
    mistake="Leaving an unused resource in the page dictionary instead of removing it.",
    recovery="List the page's resources and OCR each image, even those never drawn.",
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
    mistake="Leaving the original font-coded stream in the file instead of deleting it, "
            "relying on a custom glyph mapping to keep it unreadable.",
    recovery="Reattach the orphaned stream to a page using its own font and render it.",
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
    "An orphaned image of the SSN just under the size gate (7 px tall, still "
    "fully readable — upscaled 10x, OCR reads it back exactly): the cutoff "
    "meant for icons and masks skips it too. Its sibling above the gate is "
    "correctly flagged (LEFTOVER_IMAGE) rather than decoded, so that is the "
    "honest tier a lowered gate should give this one too.",
    expected=expect(2, warnings=(("LEFTOVER_IMAGE", "orphaned"),)),
    known_gap=KnownGap("orphaned.pixels.small", expect(0)),
    mistake="Leaving a small leftover scan in the file, assuming its size would keep it "
            "unnoticed.",
    recovery="Extract every orphaned image regardless of size and OCR it, upscaling first.",
)
def small_orphaned_image(path: Path) -> None:
    tmp = fitz.open()
    tiny = tmp.new_page(width=60, height=7)
    tiny.insert_text((0, 6), SECRET, fontsize=7)
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
    mistake="Leaving a page thumbnail embedded in the XMP metadata packet instead of "
            "stripping it.",
    recovery="Decode the XMP packet's base64 thumbnail image and OCR it.",
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
    "a base64 thumbnail image — the same honest flagged tier as an orphaned "
    "pixel image, once a lowered gate or a decoder reads it as pixels rather "
    "than only as XMP text.",
    expected=expect(2, warnings=(("LEFTOVER_IMAGE", "orphaned"),)),
    known_gap=KnownGap("leftover-xmp.pixels", expect(0)),
    mistake="Leaving an old XMP metadata packet in the file instead of deleting it.",
    recovery="Extract the orphaned XMP packet, decode its base64 thumbnail, and OCR it.",
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
    mistake="Leaving a page's /Thumb preview image in place instead of regenerating or "
            "removing it.",
    recovery="Extract the page's /Thumb image directly and OCR it.",
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
    mistake="Attaching a container as base64-encoded text (an .eml, an HTML file with a "
            "data: URI) instead of removing it.",
    recovery="Decode the attachment's base64 body and unpack the container it holds.",
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
    mistake="Leaving a PDF 2.0 associated file in place, off the attachments list, "
            "instead of deleting it.",
    recovery="Walk every /AF entry and read its content directly.",
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
    mistake="Leaving a PDF 2.0 associated file in place, off the attachments list, "
            "holding a scanned image.",
    recovery="Walk every /AF entry, extract its content, and OCR any image.",
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
    mistake="Leaving a PDF 2.0 associated file in place, off the attachments list, "
            "holding a container.",
    recovery="Walk every /AF entry, extract its content, and unpack any container.",
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
    rules=PATTERN_RULE, requires=("qpdf",),
    mistake="Leaving a link's JavaScript action in the file, stored as a stream, instead "
            "of removing it.",
    recovery="Walk every action's /JS entry and read it as raw text, string or stream alike.",
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
    mistake="Leaving an editor's private data (/PieceInfo) in the file instead of "
            "stripping it before sharing.",
    recovery="Read every live /PieceInfo stream as raw text directly.",
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
    "A /Differences-encoded font and the SSN's glyph codes, both appended after "
    "the file's final %%EOF: entirely outside any indexed object, so nothing "
    "reads either of them — even though, taken together, they are exactly the "
    "font-coded text a redactor's forensic reviewer could decode by hand.",
    expected=expect(1),
    known_gap=KnownGap("unindexed.font", expect(0)),
    mistake="Appending leftover font-coded bytes after the file's final %%EOF instead of "
            "truncating the file there.",
    recovery="Read past the final %%EOF and decode the trailing bytes by hand.",
)
def unindexed_font(path: Path) -> None:
    trailing = b"\n" + differences_font(SECRET) + b"\n" + coded(SECRET) + b"\n"
    _write(path, one_page(PAGE), after_eof=trailing)


@leak_raw(
    "file.after-final-eof-pixels", "unindexed.pixels",
    "Raw pixel bytes for a scan of the SSN, appended after the file's final "
    "%%EOF.",
    expected=expect(1),
    known_gap=KnownGap("unindexed.pixels", expect(0)),
    mistake="Appending leftover pixel bytes after the file's final %%EOF instead of "
            "truncating the file there.",
    recovery="Read past the final %%EOF and reconstruct the trailing image by hand.",
)
def unindexed_pixels(path: Path) -> None:
    # A small strip (well under qpdf's 1024-byte end-of-file lookback), so
    # the trailing bytes demonstrate the unindexed blind spot without also
    # pushing the real startxref out of qpdf's damaged-file recovery window.
    # Sized to actually fit "SSN 123-45-6789" (fontsize=7 needs ~60pt of
    # width; a narrower page truncated it to "SSN 123-45-6").
    tmp = fitz.open()
    tiny = tmp.new_page(width=60, height=7)
    tiny.insert_text((0, 6), SECRET, fontsize=7)
    png = tiny.get_pixmap(dpi=72).tobytes("png")
    tmp.close()
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png))
    _write(path, one_page(PAGE), after_eof=b"\n" + gray.samples + b"\n")


@leak_raw(
    "file.after-final-eof-container", "unindexed.container",
    "A zip of records, appended after the file's final %%EOF.",
    expected=expect(1),
    known_gap=KnownGap("unindexed.container", expect(0)),
    mistake="Appending a leftover container after the file's final %%EOF instead of "
            "truncating the file there.",
    recovery="Read past the final %%EOF and unpack the trailing container by hand.",
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
    "read this particular page column by column and gives the honest "
    "line-wrap review warning anyway (REVIEW_CROSS_LINE, as in "
    "layout.py's match.line-wrap), so the silent-miss gap is judged only "
    "where OCR is absent.",
    expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
    known_gap=KnownGap("match.columns", expect(0)),
    requires=("no-ocr",),
    mistake="Nobody redacted the value; it wraps across two lines inside one column of "
            "a two-column layout.",
    recovery="Read the page column by column, not strictly top-to-bottom.",
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
    "The SSN split across two content-stream objects on one page, both on the "
    "same baseline (the second starting 56pt right of the first, right where it "
    "ends) at coordinates around 10^9 points: PyMuPDF's text extraction returns "
    "no glyphs placed that far out, so neither half is read at all, and the "
    "Objects layer's per-object literal scan never sees either half whole. The "
    "same split at ordinary coordinates (x=700) is read normally (Text) — the "
    "coordinates, not the split, are what defeats it here.",
    expected=LIVE_SSN,
    known_gap=KnownGap("match.extreme-coordinates", expect(0)),
    mistake="Moving text to extreme coordinates instead of deleting it, assuming no "
            "reader would look there.",
    recovery="Search the raw content stream's literal strings directly, not just the "
             "rendered or extracted text.",
)
def extreme_coordinates(path: Path) -> None:
    x = 1_000_000_000
    first, second = SECRET[:8], SECRET[8:]
    content_a = stream(b"", b"BT /F1 12 Tf %d %d Td (" % (x, x)
                       + first.encode() + b") Tj ET")
    content_b = stream(b"", b"BT /F1 12 Tf %d %d Td (" % (x + 56, x)
                       + second.encode() + b") Tj ET")
    objects = one_page(content_a)
    objects[3] = page(contents=b"[4 0 R 5 0 R]")
    objects[5] = content_b
    _write(path, objects)


# ── false-alarm.binary-value-collision ──────────────────────────────────

@case(
    "page.binary-digit-collision", truth="clean",
    features=("live.plain", "unused-resource.pixels"), writer="raw",
    expected=expect(0),
    story="A correctly formed, undrawn image's raw byte ramp (0..255, repeated) "
          "happens to contain the byte sequence for \"0123456789\" (the SSN's "
          "digits, no separators) purely because consecutive byte values include "
          "the ASCII codes for '0'-'9' in order: the Binary layer's raw byte "
          "sweep cannot tell that coincidence inside binary data from a real "
          "leak, so a clean file gets a manual-review warning anyway.",
    known_gap=KnownGap("false-alarm.binary-value-collision",
                        expect(2, warnings=(("REVIEW_BINARY", "live"),))),
    requires=("qpdf",),
)
def binary_digit_collision(path: Path) -> None:
    # qpdf's QDF rewrite only emits objects reachable from the trailer — an
    # orphaned image would never reach the Binary layer's sweep at all
    # (verify.py's own module docstring: "Orphaned objects ... are not in
    # its output"). This is already recorded elsewhere in the suite, not new
    # here: every orphaned.* known-gap case (e.g. K3/K4, orphaned.plain.
    # mislabelled, in families/raw.py) is judged without requires=("qpdf",)
    # or any Binary-layer expectation, and PyMuPDF's own garbage collection
    # on save shows the same reachability-only rule (families/carriers.py:
    # "the rewrite drops the unreferenced object, so nothing is left").
    # Referencing this image from the page's resources (undrawn, like
    # unused-resource.*) keeps it live and opaque (an /Image XObject: never
    # parsed as text by the Objects layer) so only the Binary layer's raw
    # byte sweep ever sees these bytes. A correctly sized image (no bytes
    # beyond its declared Width*Height) keeps the file honestly clean: the
    # ramp itself, not anything spliced into padding, is what collides.
    samples = bytes(range(256)) * 4          # 256 x 4, 1 byte/pixel: exactly sized
    objects = one_page(PAGE)
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R >> /XObject << /Im1 7 0 R >> >>")
    objects[7] = stream(
        b"/Type /XObject /Subtype /Image /Width 256 /Height 4 /BitsPerComponent 8 "
        b"/ColorSpace /DeviceGray", samples)
    _write(path, objects)
