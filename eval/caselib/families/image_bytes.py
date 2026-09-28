"""Phase 1 decisions scheduled for Phase 0b/3a (docs/REDESIGN.md §7, ADRs
0003 and 0004): a value hidden in an image's raw sample bytes rather than
rendered as glyphs, a JPEG comment segment, a JPEG EXIF thumbnail, an
image that decodes to more rows than it declares, and one render cut
into strips the page never shows joined.

ADR 0003's governing rule is that the raw matcher must run over every
filter-chain stage of an image's bytes — never only the decoded samples
— and that a pattern class over those same bytes is a real, separate gap
(the Binary/qpdf layer's sweep is value secrets only). ADR 0004's
"decodes to more than declared" (owner decision B) and "unused images are
always FLAGGED" (owner decision D) are pinned here as today's silent
misses; each `known_gap` records the exact exit this file's build
measured against today's `verify.py`, not an assumption from the ADRs'
own prose."""

from __future__ import annotations

import struct
from pathlib import Path

import fitz

from ..model import SSN, KnownGap, case, expect
from ..rawpdf import build, one_page, page, stream, text

PAGE = stream(b"", text("Quarterly report"))
SECRET = f"Employee SSN {SSN}"
REVIEW_BINARY_LIVE = expect(2, warnings=(("REVIEW_BINARY", "live"),))
LEFTOVER_IMAGE_ORPHANED = expect(2, warnings=(("LEFTOVER_IMAGE", "orphaned"),))
LEFTOVER_IMAGE_LIVE = expect(2, warnings=(("LEFTOVER_IMAGE", "live"),))
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
LIVE_SSN_OCR = expect(1, findings=(("SSN", "live"),), layers=(("SSN", "OCR"),))
PATTERN_RULE = ({"name": "Any SSN", "class": "ssn"},)


def _write(path: Path, objects: dict[int, bytes], **kw) -> None:
    path.write_bytes(build(objects, **kw))


def leak_raw(id: str, cells: str, story: str, *, expected, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story,
                writer="raw", **kw)


def _gray_image_dict(width: int, height: int, length: int) -> bytes:
    return (b"/Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/BitsPerComponent 8 /ColorSpace /DeviceGray /Length %d" % (width, height, length))


def _drawn_image_page(width: int, height: int, image_obj: bytes) -> dict[int, bytes]:
    """A one-page document whose only image XObject (object 7) is drawn on
    the page at its own pixel size, alongside ordinary visible text."""
    objects = one_page(PAGE)
    objects[3] = page(contents=b"4 0 R",
                      resources=b"<< /Font << /F1 6 0 R >> /XObject << /Im1 7 0 R >> >>")
    objects[4] = stream(b"", b"BT /F1 12 Tf 72 700 Td (Quarterly report) Tj ET "
                        b"q %d 0 0 %d 72 400 cm /Im1 Do Q" % (width, height))
    objects[7] = image_obj
    return objects


# ── live.pixels.hidden-samples / .pattern-rules ──────────────────────────
# ADR 0003: a 10x10 DeviceGray image whose 100 raw sample bytes spell the
# SSN, never rendered as glyphs — steganographic to a human viewer, but
# plain to a byte scan. Measured directly against verify.py (not assumed
# from the ADR's own table): drawn + value = REVIEW_BINARY (exit 2);
# drawn + pattern rule, orphaned + value, orphaned + pattern rule = exit 0.

_HIDDEN_W, _HIDDEN_H = 10, 10
_HIDDEN_SAMPLES = (SECRET + " on file for the quarterly compliance review").encode("ascii")
_HIDDEN_SAMPLES = _HIDDEN_SAMPLES[:100].ljust(100, b".")
assert len(_HIDDEN_SAMPLES) == _HIDDEN_W * _HIDDEN_H

_HIDDEN_IMAGE_OBJ = (b"<< " + _gray_image_dict(_HIDDEN_W, _HIDDEN_H, len(_HIDDEN_SAMPLES))
                     + b" >>\nstream\n" + _HIDDEN_SAMPLES + b"\nendstream")


@leak_raw(
    "leftover.hidden-sample-bytes", "orphaned.pixels.small",
    "The SSN is the literal ASCII content of a 10x10 DeviceGray image's 100 raw sample "
    "bytes — not rendered as glyphs, so nothing about it is visually readable — and the "
    "image is referenced by nothing. At 10x10 it is also under today's 8x32 leftover-image "
    "size gate, so the orphaned-image check that would otherwise flag it never fires "
    "(measured: exit 0, no warnings).",
    expected=LEFTOVER_IMAGE_ORPHANED,
    known_gap=KnownGap("orphaned.pixels.small", expect(0)),
    mistake="Leaving an orphaned image behind whose raw pixel bytes happen to spell the "
            "secret, on the assumption that pixel content can't leak plain text.",
    recovery="Extract every orphaned image's raw sample bytes directly, whatever its "
             "declared size, and search them as text.",
)
def hidden_sample_bytes_orphaned(path: Path) -> None:
    objects = one_page(PAGE)
    objects[7] = _HIDDEN_IMAGE_OBJ           # referenced by nothing
    _write(path, objects)


@leak_raw(
    "leftover.hidden-sample-bytes-pattern-rule", "orphaned.pixels.small",
    "The same orphaned, under-the-gate 10x10 image, searched only by the built-in SSN "
    "pattern rule instead of the literal value: the same size-gate gap applies regardless "
    "of rule kind (measured: exit 0, no warnings).",
    expected=LEFTOVER_IMAGE_ORPHANED,
    known_gap=KnownGap("orphaned.pixels.small", expect(0)),
    rules=PATTERN_RULE,
    mistake="Leaving an orphaned image behind whose raw pixel bytes happen to spell the "
            "secret, on the assumption that pixel content can't leak plain text.",
    recovery="Extract every orphaned image's raw sample bytes directly, whatever its "
             "declared size, and search them as text.",
)
def hidden_sample_bytes_orphaned_pattern_rule(path: Path) -> None:
    objects = one_page(PAGE)
    objects[7] = _HIDDEN_IMAGE_OBJ
    _write(path, objects)


@leak_raw(
    "page.hidden-sample-bytes", "live.pixels.hidden-samples",
    "The same 10x10 image, this time drawn on the page: the composited render is visually "
    "blank noise, so OCR finds nothing, but qpdf's Binary sweep reads the object's raw "
    "stream bytes directly — including an image's, which are never routed through the "
    "image decoder — and matches the literal value there (measured: exit 2, REVIEW_BINARY). "
    "A control: today's tool already gets this one right, by a layer that has nothing to "
    "do with images as such.",
    expected=REVIEW_BINARY_LIVE,
    requires=("qpdf",),
    mistake="Leaving a live image whose raw sample bytes spell the secret, drawn on the "
            "page, on the assumption that pixel content is safe from a text search.",
    recovery="Run a raw byte search over qpdf's decompressed object stream (or the file's "
             "own bytes), not only OCR of the rendered page.",
)
def hidden_sample_bytes_drawn(path: Path) -> None:
    _write(path, _drawn_image_page(_HIDDEN_W, _HIDDEN_H, _HIDDEN_IMAGE_OBJ))


@leak_raw(
    "page.hidden-sample-bytes-pattern-rule", "live.pixels.hidden-samples.pattern-rules",
    "The same drawn image, searched only by the built-in SSN pattern rule: qpdf's Binary "
    "sweep matches known values in raw image bytes, never pattern classes (ADR 0003), so "
    "this one finds nothing (measured: exit 0, no warnings). Once pattern classes run over "
    "raw image bytes at the manual-review tier (ADR 0003's owner decision), this becomes "
    "the same REVIEW_BINARY catch as the value-rule sibling.",
    expected=REVIEW_BINARY_LIVE,
    known_gap=KnownGap("live.pixels.hidden-samples.pattern-rules", expect(0)),
    rules=PATTERN_RULE, requires=("qpdf",),
    mistake="Leaving a live image whose raw sample bytes spell the secret, drawn on the "
            "page, on the assumption that pixel content is safe from a text search.",
    recovery="Run a raw byte search over qpdf's decompressed object stream (or the file's "
             "own bytes), not only OCR of the rendered page.",
)
def hidden_sample_bytes_drawn_pattern_rule(path: Path) -> None:
    _write(path, _drawn_image_page(_HIDDEN_W, _HIDDEN_H, _HIDDEN_IMAGE_OBJ))


# ── JPEG COM segment (drawn) ──────────────────────────────────────────────
# ADR 0003: the value lives in the JPEG's comment (COM) segment — inside
# the encoded DCTDecode bytes the codec consumes, never in the decoded
# pixels. Scheduled next to the hidden-image case.

def _cover_jpeg() -> tuple[bytes, int, int, int]:
    """A small, innocuous rendered JPEG (no relation to the secret)."""
    tmp = fitz.open()
    pg = tmp.new_page(width=120, height=40)
    pg.insert_text((5, 25), "logo", fontsize=14)
    pix = pg.get_pixmap(dpi=72)
    jpeg = pix.tobytes("jpg")
    w, h, n = pix.width, pix.height, pix.n
    tmp.close()
    return jpeg, w, h, n


def _with_com(jpeg: bytes, comment: bytes) -> bytes:
    """*jpeg* with a COM (comment) marker segment holding *comment*,
    inserted right after the SOI marker."""
    payload = comment + b"\x00"
    segment = b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload
    return jpeg[:2] + segment + jpeg[2:]


def _jpeg_colorspace(components: int) -> bytes:
    return {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}[components]


def _jpeg_image_obj(jpeg: bytes, width: int, height: int, components: int) -> bytes:
    dictionary = (b"/Type /XObject /Subtype /Image /Width %d /Height %d /BitsPerComponent 8 "
                  b"/ColorSpace %s /Filter /DCTDecode /Length %d"
                  % (width, height, _jpeg_colorspace(components), len(jpeg)))
    return b"<< " + dictionary + b" >>\nstream\n" + jpeg + b"\nendstream"


@leak_raw(
    "page.jpeg-comment-ssn", "live.pixels.hidden-samples",
    "A JPEG's COM (comment) segment holds the SSN — present in the encoded DCTDecode bytes "
    "qpdf's raw sweep reads directly, but never reaching the decoded pixels OCR sees, since "
    "a JPEG comment segment is metadata a decoder skips over, not image data. A control: "
    "today's tool already catches this, the same way as the raw-sample-bytes case (measured: "
    "exit 2, REVIEW_BINARY).",
    expected=REVIEW_BINARY_LIVE,
    requires=("qpdf",),
    mistake="Leaving an editor- or scanner-added JPEG comment segment holding the secret, on "
            "the assumption that only the decoded picture matters.",
    recovery="Read the JPEG's raw bytes directly (or list its markers with a JPEG "
             "inspection tool) rather than relying on OCR of the decoded picture.",
)
def jpeg_comment_ssn(path: Path) -> None:
    jpeg, w, h, n = _cover_jpeg()
    jpeg_com = _with_com(jpeg, SECRET.encode("ascii"))
    _write(path, _drawn_image_page(w, h, _jpeg_image_obj(jpeg_com, w, h, n)))


# ── live.pixels.exif-thumbnail ────────────────────────────────────────────
# ADR 0004 owner decision B: any image data beyond the main decoded
# picture — an EXIF/APP1 thumbnail among the named examples — means the
# image is FLAGGED, whatever it shows. Built as a real, standards-shaped
# EXIF APP1 segment (TIFF header, IFD0, an IFD1 with Compression=6 and
# JPEGInterchangeFormat/-Length pointing at an embedded thumbnail JPEG) —
# verified readable by `exiftool -b -ThumbnailImage` against this exact
# generator's output before being pinned here — not a stand-in for one.

def _exif_app1_with_thumbnail(thumbnail_jpeg: bytes) -> bytes:
    """A minimal but structurally real EXIF APP1 segment: TIFF header,
    an empty IFD0, and an IFD1 (the "thumbnail IFD") whose Compression,
    JPEGInterchangeFormat and JPEGInterchangeFormatLength tags point at
    *thumbnail_jpeg*, appended right after IFD1."""
    tiff_header = b"II" + struct.pack("<H", 0x002A) + struct.pack("<I", 8)
    ifd0_offset = 8
    ifd1_offset = ifd0_offset + 2 + 0 * 12 + 4       # 0 entries + next-IFD pointer
    ifd0 = struct.pack("<H", 0) + struct.pack("<I", ifd1_offset)
    thumb_offset = ifd1_offset + 2 + 3 * 12 + 4      # 3 entries + next-IFD pointer (0)

    def entry(tag: int, kind: int, count: int, value: int) -> bytes:
        return struct.pack("<HHI", tag, kind, count) + struct.pack("<I", value)

    ifd1 = struct.pack("<H", 3)
    ifd1 += entry(0x0103, 3, 1, 6)                   # Compression = 6 (JPEG, old-style)
    ifd1 += entry(0x0201, 4, 1, thumb_offset)        # JPEGInterchangeFormat
    ifd1 += entry(0x0202, 4, 1, len(thumbnail_jpeg))  # JPEGInterchangeFormatLength
    ifd1 += struct.pack("<I", 0)                     # no further IFD

    tiff_data = tiff_header + ifd0 + ifd1 + thumbnail_jpeg
    payload = b"Exif\x00\x00" + tiff_data
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


@leak_raw(
    "page.jpeg-exif-thumbnail", "live.pixels.exif-thumbnail",
    "A small JPEG's own EXIF (APP1) thumbnail is a second, fully decodable picture of the "
    "SSN that the main image's declared frame never mentions: only the main picture is ever "
    "drawn, so OCR never sees the thumbnail, and unlike a raw sample byte or a JPEG comment, "
    "the thumbnail's SSN render is itself DCT/Huffman-compressed pixel data, not literal "
    "text bytes, so no raw byte sweep finds it either (measured: exit 0, no warnings at all "
    "— MuPDF gives no signal that a thumbnail is even present). ADR 0004 owner decision B: "
    "any image data beyond the main decoded picture means the image must be FLAGGED.",
    expected=LEFTOVER_IMAGE_LIVE,
    known_gap=KnownGap("live.pixels.exif-thumbnail", expect(0)),
    mistake="Leaving a JPEG whose camera- or editor-generated EXIF thumbnail still shows "
            "the secret, having redacted only the main picture.",
    recovery="Extract the image's EXIF thumbnail directly (e.g. `exiftool -b "
             "-ThumbnailImage`) and look at it.",
)
def jpeg_exif_thumbnail(path: Path) -> None:
    tmp = fitz.open()
    pg = tmp.new_page(width=120, height=40)
    pg.insert_text((5, 25), "logo", fontsize=14)
    pix = tmp[0].get_pixmap(dpi=72)
    main_jpeg = pix.tobytes("jpg")
    w, h, n = pix.width, pix.height, pix.n
    tmp.close()

    thumb_doc = fitz.open()
    thumb_pg = thumb_doc.new_page(width=90, height=20)
    thumb_pg.insert_text((2, 15), SSN, fontsize=11)
    thumb_jpeg = thumb_doc[0].get_pixmap(dpi=72).tobytes("jpg")
    thumb_doc.close()

    app1 = _exif_app1_with_thumbnail(thumb_jpeg)
    jpeg_with_exif = main_jpeg[:2] + app1 + main_jpeg[2:]
    _write(path, _drawn_image_page(w, h, _jpeg_image_obj(jpeg_with_exif, w, h, n)))


# ── live.pixels.extra-rows (K37) ──────────────────────────────────────────
# REDESIGN §8's note below the K-table / ADR 0004 owner decision B: a
# referenced, drawn 200x20 DeviceGray image whose declared frame renders
# blank, but whose stream holds 20 more undeclared rows with an SSN
# render. MuPDF reads only the declared Width*Height*components*bpc
# bytes, so the extra rows are never drawn (no OCR) and never warned
# about (measured: exit 0, no warnings).

_ROWS_W, _ROWS_H = 200, 20


@leak_raw(
    "page.image-extra-rows", "live.pixels.extra-rows",
    "K37. A referenced, drawn 200x20 DeviceGray image whose declared frame renders blank, "
    "but whose stream holds 20 more undeclared rows with an SSN render: MuPDF decodes only "
    "the declared Width*Height*components*bits-per-component byte range, so the extra rows "
    "are never drawn, never rendered, and never OCR'd — and MuPDF raises no warning that "
    "more data followed (measured: exit 0, no warnings at all). ADR 0004 owner decision B: "
    "any image data beyond the main decoded picture means the image must be FLAGGED.",
    expected=LEFTOVER_IMAGE_LIVE,
    known_gap=KnownGap("live.pixels.extra-rows", expect(0)),
    mistake="Leaving extra, undeclared pixel rows appended after an image's declared frame, "
            "on the assumption that only the declared frame is ever read.",
    recovery="Compare the stream's actual decompressed length against "
             "Width*Height*components*bits-per-component, and inspect any extra bytes as "
             "pixels of the same width.",
)
def image_extra_rows(path: Path) -> None:
    tmp = fitz.open()
    pg = tmp.new_page(width=_ROWS_W, height=_ROWS_H * 2)   # top half blank, bottom half secret
    pg.draw_rect(fitz.Rect(0, 0, _ROWS_W, _ROWS_H * 2), color=None, fill=(1, 1, 1))
    pg.insert_text((4, _ROWS_H + 14), SECRET.replace("Employee ", ""), fontsize=10)
    pix = tmp[0].get_pixmap(dpi=72, colorspace=fitz.csGRAY)
    tmp.close()
    samples = pix.samples
    assert len(samples) == _ROWS_W * _ROWS_H * 2         # declared frame + the same again
    img_obj = (b"<< " + _gray_image_dict(_ROWS_W, _ROWS_H, len(samples))
              + b" >>\nstream\n" + samples + b"\nendstream")
    _write(path, _drawn_image_page(_ROWS_W, _ROWS_H, img_obj))


# ── Strips: one render the page never shows joined ───────────────────────
# REDESIGN §8, K21's note and the "no K-number yet" section: a 90x14 SSN
# render split into two 90x7 images. (a) orphaned: below today's 8x32
# leftover-image gate, same as the small-image case (K21) itself. (b)
# drawn stacked and uncovered: a control — the composited page
# reassembles the render and OCR reads it whole. (c) drawn stacked under
# a black box (K21's row calls this "K12's strips variant"; K38 here,
# since K12 is already claimed by page.pixels-under-box). (d) listed in
# /Resources but never drawn.

_STRIP_W, _STRIP_H = 90, 14


def _ssn_strip_samples() -> tuple[bytes, bytes]:
    """The SSN rendered as one 90x14 DeviceGray bitmap, split into a top
    and a bottom 90x7 half."""
    tmp = fitz.open()
    pg = tmp.new_page(width=_STRIP_W, height=_STRIP_H)
    pg.draw_rect(fitz.Rect(0, 0, _STRIP_W, _STRIP_H), color=None, fill=(1, 1, 1))
    pg.insert_text((1, _STRIP_H - 3), SSN, fontsize=9)
    pix = tmp[0].get_pixmap(dpi=72, colorspace=fitz.csGRAY)
    tmp.close()
    whole = pix.samples
    assert len(whole) == _STRIP_W * _STRIP_H
    half = _STRIP_W * (_STRIP_H // 2)
    return whole[:half], whole[half:]


_STRIP_TOP, _STRIP_BOTTOM = _ssn_strip_samples()
_STRIP_TOP_OBJ = (b"<< " + _gray_image_dict(_STRIP_W, 7, len(_STRIP_TOP))
                  + b" >>\nstream\n" + _STRIP_TOP + b"\nendstream")
_STRIP_BOTTOM_OBJ = (b"<< " + _gray_image_dict(_STRIP_W, 7, len(_STRIP_BOTTOM))
                     + b" >>\nstream\n" + _STRIP_BOTTOM + b"\nendstream")
_STRIP_MISTAKE = ("A scan or redaction tool banding one render into separate image objects "
                  "(e.g. a page split into strips for compression or printing).")


@leak_raw(
    "leftover.ssn-strips", "orphaned.pixels.small",
    "K21's strips variant. One 90x14 render of the SSN, split into two orphaned 90x7 "
    "images: each is under today's 8x32 leftover-image size gate, the same gap as the "
    "single small image K21 already pins (measured: exit 0, no warnings).",
    expected=LEFTOVER_IMAGE_ORPHANED,
    known_gap=KnownGap("orphaned.pixels.small", expect(0)),
    mistake=_STRIP_MISTAKE,
    recovery="Extract every orphaned image's raw sample bytes directly, whatever its "
             "declared size, and reassemble same-width strips before OCRing them.",
)
def ssn_strips_orphaned(path: Path) -> None:
    objects = one_page(PAGE)
    objects[7] = _STRIP_TOP_OBJ
    objects[8] = _STRIP_BOTTOM_OBJ
    _write(path, objects)


def _strips_page_objects(extra_content: bytes = b"", draw: bool = True) -> dict[int, bytes]:
    objects = one_page(PAGE)
    objects[3] = page(
        contents=b"4 0 R",
        resources=b"<< /Font << /F1 6 0 R >> /XObject << /ImTop 7 0 R /ImBot 8 0 R >> >>")
    body = b"BT /F1 12 Tf 72 700 Td (Quarterly report) Tj ET"
    if draw:
        body += (b" q %d 0 0 7 72 407 cm /ImTop Do Q q %d 0 0 7 72 400 cm /ImBot Do Q"
                 % (_STRIP_W, _STRIP_W))
    objects[4] = stream(b"", body + extra_content)
    objects[7] = _STRIP_TOP_OBJ
    objects[8] = _STRIP_BOTTOM_OBJ
    return objects


@leak_raw(
    "page.ssn-strips-joined", "live.pixels",
    "The same 90x14 render, split into two 90x7 images and drawn stacked on the page with "
    "nothing between or over them: the composited render reassembles the whole picture, "
    "and OCR reads it whole. A control, not a known gap: the tool gets this right today "
    "(measured: exit 1, OCR finding).",
    expected=LIVE_SSN_OCR,
    requires=("ocr",),
    mistake=_STRIP_MISTAKE,
    recovery="Look at the page — OCR reads the two strips as the single image they "
             "reassemble into.",
)
def ssn_strips_joined(path: Path) -> None:
    _write(path, _strips_page_objects())


@leak_raw(
    "page.ssn-strips-under-box", "live.pixels.under-box",
    "K38. The same two strips, drawn stacked, but with a black box painted over both "
    "afterwards: OCR only ever sees the rendered, composited page — the box, not what is "
    "under it — the strips version of K12's known gap (K21's own row calls this "
    "\"K12's strips variant\"; measured: exit 0, no warnings).",
    expected=LIVE_SSN,
    known_gap=KnownGap("live.pixels.under-box", expect(0)),
    mistake="Drawing a box over the reassembled strips instead of removing or replacing "
            "the underlying images.",
    recovery="Extract each image object directly (ignoring what is drawn over it), "
             "reassemble same-width strips, and OCR the result.",
)
def ssn_strips_under_box(path: Path) -> None:
    box = b" 0 0 0 rg 72 400 %d 14 re f" % _STRIP_W
    _write(path, _strips_page_objects(extra_content=box))


@leak_raw(
    "page.ssn-strips-unused-resource", "unused-resource.pixels",
    "The same two strips, listed in the page's /Resources /XObject dictionary but never "
    "drawn by any content stream: nothing composites them, so the page's OCR never sees "
    "them (measured: exit 0, no warnings).",
    expected=LIVE_SSN,
    known_gap=KnownGap("unused-resource.pixels", expect(0)),
    mistake="Leaving unused strip images listed in the page's resources instead of "
            "removing them.",
    recovery="List the page's resources and OCR each image, even those never drawn, "
             "reassembling same-width strips first.",
)
def ssn_strips_unused_resource(path: Path) -> None:
    _write(path, _strips_page_objects(draw=False))
