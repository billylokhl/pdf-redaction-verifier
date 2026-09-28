"""Cases hand-assembled byte by byte (the non-fitz writer): places the
tool is known to miss (K1–K7 in docs/REDESIGN.md §8), hidden-but-live
content that needs exact structure, and a known false alarm."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..model import SSN, KnownGap, case, expect
from ..rawpdf import build, flate, one_page, page, stream, text

SECRET = f"SSN {SSN}"
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
ORPHAN_SSN = expect(1, findings=(("SSN", "orphaned"),))
PAGE = stream(b"", text("Quarterly report"))
HIDDEN_LAYER = (b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [7 0 R] "
                b"/D << /OFF [7 0 R] /Order [7 0 R] >> >> >>")
NO_GC = "Saving without garbage collection: the original stream is left, unreferenced."
DECOMPRESS = "Decompress the file (mutool clean -d) and read the leftover stream by hand."


def leak(id: str, cells, story: str, expected=LIVE_SSN, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story,
                writer="raw", **kw)


def _write(path: Path, objects: dict[int, bytes], **kw) -> None:
    path.write_bytes(build(objects, **kw))


def outline_stream(line: str) -> bytes:
    """A content stream drawing *line* as filled outlines — no text
    operators at all, as 'convert text to outlines' produces."""
    doc = fitz.open(); src = doc.new_page(width=612, height=792)
    src.insert_text((72, 200), line, fontsize=28)
    svg = src.get_svg_image(text_as_path=True)
    vector = fitz.open("pdf", fitz.open("svg", svg.encode()).convert_to_pdf())
    return b"".join(vector.xref_stream(x) for x in vector[0].get_contents())


# ── Known gaps: exit 0 today with the secret present ───────────────────

@leak("page.data-after-stream-end", "live.plain.after-stream-end",
      "K1: text operators after the zlib end marker inside a live page's content "
      "stream. Both parsers stop at the marker; qpdf reports no error.",
      known_gap=KnownGap("live.plain.after-stream-end", expect(0)),
      mistake="A tool wrote extra text after the compressed stream's end marker.",
      recovery="Decompress the file and read past the zlib end marker.")
def k1(path: Path) -> None:
    _write(path, one_page(flate(b"", text("Quarterly report"),
                                trailing=b"\n" + text(SECRET, 600) + b"\n")))


@leak("leftover.data-after-stream-end", "orphaned.plain.after-stream-end",
      "K2: the same trailing data in an orphaned stream.", expected=ORPHAN_SSN,
      known_gap=KnownGap("orphaned.plain.after-stream-end", expect(0)),
      mistake=NO_GC, recovery=DECOMPRESS)
def k2(path: Path) -> None:
    trailing = b"\n(" + SECRET.encode() + b") Tj\n"
    _write(path, one_page(PAGE, o5=flate(b"", text("nothing here", 600), trailing=trailing)))


@leak("leftover.text-labelled-font", "orphaned.plain.mislabelled",
      "K3: an orphaned plain-text stream carrying /Length1, so it is taken for a font "
      "program and skipped.", expected=ORPHAN_SSN,
      known_gap=KnownGap("orphaned.plain.mislabelled", expect(0)),
      mistake=NO_GC, recovery=DECOMPRESS)
def k3(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Length1 20", b"Notes: " + SECRET.encode())))


@leak("leftover.text-labelled-image", "orphaned.plain.mislabelled",
      "K4: orphaned plain text labelled as a 1×1 image.", expected=ORPHAN_SSN,
      known_gap=KnownGap("orphaned.plain.mislabelled", expect(0)),
      mistake=NO_GC, recovery=DECOMPRESS)
def k4(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(
        b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8 "
        b"/ColorSpace /DeviceGray", b"Notes: " + SECRET.encode())))


@leak("leftover.outlines", "orphaned.pixels.outlines",
      "K5: the SSN converted to outlines (filled paths), in an orphaned stream. "
      "Nothing reads it and nothing flags it.", expected=ORPHAN_SSN,
      known_gap=KnownGap("orphaned.pixels.outlines", expect(0)),
      mistake="Converting text to outlines instead of removing it, then saving without "
              "garbage collection.",
      recovery="Decompress the file and render the leftover stream's filled paths.")
def k5(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"", outline_stream(SECRET))))


@leak("page.hidden-layer-outlines", "oc-off.pixels",
      "K6: the SSN as outlines in a switched-off optional-content layer on the page.",
      known_gap=KnownGap("oc-off.pixels", expect(0)),
      mistake="Hiding a layer with outline text instead of deleting its content.",
      recovery="Turn the layer on in the viewer's layers panel.")
def k6(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = HIDDEN_LAYER
    objects[3] = page(b"[4 0 R 5 0 R]",
                      b"<< /Font << /F1 6 0 R >> /Properties << /oc1 7 0 R >> >>")
    objects[5] = stream(b"", b"/OC /oc1 BDC q " + outline_stream(SECRET) + b" Q EMC")
    objects[7] = b"<< /Type /OCG /Name (Layer 1) >>"
    _write(path, objects)


@leak("file.after-final-eof", "unindexed.plain",
      "K7: plain text appended after the file's final %%EOF.",
      expected=expect(1), known_gap=KnownGap("unindexed.plain", expect(0)),
      mistake="A tool appended data after the file's final %%EOF.",
      recovery="Open the file in a hex editor and read past the last %%EOF.")
def k7(path: Path) -> None:
    _write(path, one_page(PAGE), after_eof=b"\n" + SECRET.encode() + b"\n")


# ── Hidden but live: plain strings the Objects layer reads ─────────────

@leak("page.hidden-layer-text", "oc-off.plain",
      "The SSN as text in a switched-off optional-content layer: no viewer shows it.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Objects"),)),
      mistake="Hiding a layer instead of deleting its content.",
      recovery="Turn the layer on in the viewer's layers panel.")
def hidden_layer(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = HIDDEN_LAYER
    objects[3] = page(b"[4 0 R 5 0 R]",
                      b"<< /Font << /F1 6 0 R >> /Properties << /oc1 7 0 R >> >>")
    objects[5] = stream(b"", b"/OC /oc1 BDC " + text(SECRET, 600) + b" EMC")
    objects[7] = b"<< /Type /OCG /Name (Layer 1) >>"
    _write(path, objects)


@leak("page.hidden-annotation", "annot-appearance.plain",
      "A hidden annotation whose appearance draws the SSN.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Objects"),)),
      mistake="Hiding an annotation instead of deleting it.",
      recovery="Turn on hidden annotations in the viewer, or read the /AP appearance stream.")
def hidden_annotation(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Square /Rect [72 500 300 530] /F 2 "
                  b"/AP << /N 8 0 R >> >>")
    objects[8] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 228 30] "
                        b"/Resources << /Font << /F1 6 0 R >> >>", text(SECRET, 10))
    _write(path, objects)


@leak("page.unused-form-resource", "unused-resource.plain",
      "A form XObject in the page's resources that the page never draws.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Objects"),)),
      mistake="Removing the drawing operator but leaving the resource in place.",
      recovery="List the page's resources and inspect each XObject.")
def unused_resource(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R >> /XObject << /Fm1 7 0 R >> >>")
    objects[7] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                        b"/Resources << /Font << /F1 6 0 R >> >>", text(SECRET, 500))
    _write(path, objects)


@leak("document.private-data", "private-data.plain",
      "An editor's private data (/PieceInfo) on the page keeps the SSN as raw text; "
      "only the Binary layer's known-value sweep sees it (manual review).",
      expected=expect(2, warnings=(("REVIEW_BINARY", "live"),)), requires=("qpdf",),
      mistake="Editing tools keep undo data or originals in their private dictionaries.",
      recovery="Open the page's /PieceInfo dictionary and read the private stream.")
def piece_info(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/PieceInfo << /Editor << /Private 7 0 R >> >>")
    objects[7] = stream(b"", b"original line: " + SECRET.encode())
    _write(path, objects)


@leak("document.associated-file", "embedded-other.plain",
      "A PDF 2.0 associated file (/AF) on the page — not in the attachments list — "
      "holding the SSN; only the Binary layer sees it (manual review).",
      expected=expect(2, warnings=(("REVIEW_BINARY", "live"),)), requires=("qpdf",),
      mistake="Attaching a source file through PDF 2.0's /AF instead of the "
              "conventional attachments list.",
      recovery="List the page's /AF entries and read the associated file's stream.")
def associated_file(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/AF [7 0 R]")
    objects[7] = b"<< /Type /Filespec /F (source.txt) /EF << /F 8 0 R >> /AFRelationship /Source >>"
    objects[8] = stream(b"/Type /EmbeddedFile", b"Source notes: " + SECRET.encode())
    _write(path, objects)


# ── Clean ───────────────────────────────────────────────────────────────

@case("page.raw-minimal", truth="clean", features="live.plain", writer="raw",
      expected=expect(0),
      story="A minimal hand-assembled one-page document (control for the raw writer).")
def clean_raw(path: Path) -> None:
    _write(path, one_page(PAGE))


# ── Leftover content in the token forms real producers write ───────────

GLYPHS = b"<0034003400310003001400150016000E0017001800190019001A001B001C>"   # "SSN 123-45-6789" as glyph ids
UNDECODABLE = expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),))


@leak("leftover.font-set-before-text-object", "orphaned.font",
      "A leftover stream that sets its font before BT (text state persists across "
      "text objects), then shows the SSN as glyph codes.", expected=UNDECODABLE,
      mistake=NO_GC, recovery="Decompress the file and map the glyph codes through the "
                              "font's Unicode table.")
def font_before_bt(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"", b"q /EM 11 Tf BT 72 770 Td " + GLYPHS + b"Tj ET Q")))


@leak("leftover.kerned-glyph-codes", "orphaned.font",
      "A leftover stream showing the SSN's glyph codes one per string, kerned in a TJ "
      "array — no single string is long enough to judge.", expected=UNDECODABLE,
      mistake=NO_GC, recovery="Decompress the file, join the TJ array's strings, and map "
                              "the glyph codes through the font's Unicode table.")
def kerned(path: Path) -> None:
    codes = GLYPHS[1:-1]
    pieces = b"".join(b"<" + codes[i:i + 4] + b">-15" for i in range(0, len(codes), 4))
    _write(path, one_page(PAGE, o5=stream(b"", b"BT/EM 11 Tf 72 700 Td[" + pieces + b"]TJ ET")))


@leak("leftover.single-glyph-show-runs", "orphaned.font.single-glyph-strings",
      "A leftover stream in a custom-encoded font (one-byte codes 0x01–0x1E) showing "
      "the SSN one code per Tj operator: each string is one character, so none is "
      "judged, and the codes pass as readable.",
      expected=UNDECODABLE, known_gap=KnownGap("orphaned.font.single-glyph-strings", expect(0)),
      mistake=NO_GC, recovery="Decompress the file, join the one-character Tj runs in "
                              "order, and map the codes through the font's Unicode table.")
def single_glyph_runs(path: Path) -> None:
    codes = [0x05, 0x05, 0x0E, 0x01, 0x12, 0x13, 0x14, 0x02, 0x15, 0x16, 0x02, 0x17, 0x18, 0x19, 0x1A]
    shows = b" ".join(b"<%02X>Tj 6 0 Td" % c for c in codes)
    _write(path, one_page(PAGE, o5=stream(b"", b"BT /EM 11 Tf 72 700 Td " + shows + b" ET")))


@leak("leftover.inline-image-compact", "orphaned.pixels",
      "A leftover content stream drawing the SSN as an inline image, written the way "
      "MuPDF's clean_contents writes it (\"/D[0 1]ID\", no spaces).",
      expected=expect(2, warnings=(("LEFTOVER_IMAGE", "orphaned"),)),
      mistake=NO_GC, recovery="Decompress the file and render the leftover inline image.")
def inline_image_compact(path: Path) -> None:
    from ..pdfkit import png_of
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(SECRET)))
    data = (b"q %d 0 0 %d 72 400 cm BI /W %d/H %d/BPC 8/CS/G/D[0 1]ID "
            % (gray.width // 2, gray.height // 2, gray.width, gray.height)
            + gray.samples + b"\nEI Q")
    _write(path, one_page(PAGE, o5=stream(b"", data)))


@leak("leftover.untyped-text-operator-words", "orphaned-attachment.plain.operator-words",
      "A removed attachment without a /Type whose text has three stand-alone words that "
      "are also content operators (n, m, q): it is taken for page content and never "
      "searched as text.",
      expected=expect(2, warnings=(("REVIEW_OBJECT_TEXT", "orphaned"),)),
      known_gap=KnownGap("orphaned-attachment.plain.operator-words", expect(0)),
      mistake="Removing an attachment's listing, not its stream.",
      recovery="Decompress the file and read the leftover stream's plain text.")
def operator_words(path: Path) -> None:
    text = b"for n in rows:\n    m = n\n    q = m\n# claimant SSN 123-45-6789\n"
    _write(path, one_page(PAGE, o5=stream(b"", text)))
