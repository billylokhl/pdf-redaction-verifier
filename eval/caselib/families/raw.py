"""Cases hand-assembled byte by byte (the non-fitz writer): places the
tool is known to miss (K1–K7 in docs/REDESIGN.md §8), and hidden-but-live
content that needs exact structure."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import SSN, case, expect
from ..rawpdf import build, flate, one_page, page, stream, text

SECRET = f"SSN {SSN}"
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
ORPHAN_SSN = expect(1, findings=(("SSN", "orphaned"),))
PAGE = stream(b"", text("Quarterly report"))


def _write(path: Path, objects: dict[int, bytes], **kw) -> None:
    path.write_bytes(build(objects, **kw))


def outline_stream(line: str) -> bytes:
    """A content stream drawing *line* as filled outlines — no text
    operators at all, as 'convert text to outlines' produces."""
    doc = fitz.open(); src = doc.new_page(width=612, height=792)
    src.insert_text((72, 200), line, fontsize=28)
    svg = src.get_svg_image(text_as_path=True)
    vector = fitz.open("pdf", fitz.open("svg", svg.encode()).convert_to_pdf())
    data = b"".join(vector.xref_stream(x) for x in vector[0].get_contents())
    return data


# ── Known gaps: exit 0 today with the secret present ───────────────────

@case("gap.live-data-after-stream-end", cells="live.after-stream-end", writer="raw",
      expected=LIVE_SSN, known_gap="live.after-stream-end",
      story="K1: text operators after the zlib end marker inside a live page's content "
            "stream. Both parsers stop at the marker; qpdf reports no error.")
def k1(path: Path) -> None:
    _write(path, one_page(flate(b"", text("Quarterly report"),
                                trailing=b"\n" + text(SECRET, 600) + b"\n")))


@case("gap.orphan-data-after-stream-end", cells="orphaned.after-stream-end", writer="raw",
      expected=ORPHAN_SSN, known_gap="orphaned.after-stream-end",
      story="K2: the same trailing data in an orphaned stream.")
def k2(path: Path) -> None:
    trailing = b"\n(" + SECRET.encode() + b") Tj\n"
    _write(path, one_page(PAGE, o5=flate(b"", text("nothing here", 600), trailing=trailing)))


@case("gap.orphan-text-labelled-font", cells="orphaned.mislabelled", writer="raw",
      expected=ORPHAN_SSN, known_gap="orphaned.mislabelled",
      story="K3: an orphaned plain-text stream carrying /Length1, so it is taken for a font "
            "program and skipped.")
def k3(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Length1 20", b"Notes: " + SECRET.encode())))


@case("gap.orphan-text-labelled-image", cells="orphaned.mislabelled", writer="raw",
      expected=ORPHAN_SSN, known_gap="orphaned.mislabelled",
      story="K4: orphaned plain text labelled as a 1×1 image.")
def k4(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(
        b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8 "
        b"/ColorSpace /DeviceGray", b"Notes: " + SECRET.encode())))


@case("gap.orphan-outlines", cells="orphaned.outlines", writer="raw",
      expected=expect(1), known_gap="orphaned.outlines",
      story="K5: the SSN converted to outlines (filled paths), in an orphaned stream. "
            "Nothing reads it and nothing flags it.")
def k5(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"", outline_stream(SECRET))))


@case("gap.hidden-layer-outlines", cells="oc-off.pixels", writer="raw",
      expected=expect(1), known_gap="oc-off.pixels",
      story="K6: the SSN as outlines in a switched-off optional-content layer on the page.")
def k6(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = (b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [7 0 R] "
                  b"/D << /OFF [7 0 R] /Order [7 0 R] >> >> >>")
    objects[3] = page(b"[4 0 R 5 0 R]",
                      b"<< /Font << /F1 6 0 R >> /Properties << /oc1 7 0 R >> >>")
    objects[5] = stream(b"", b"/OC /oc1 BDC q " + outline_stream(SECRET) + b" Q EMC")
    objects[7] = b"<< /Type /OCG /Name (Layer 1) >>"
    _write(path, objects)


@case("gap.after-final-eof", cells="unindexed.plain", writer="raw",
      expected=expect(1), known_gap="unindexed.plain",
      story="K7: plain text appended after the file's final %%EOF.")
def k7(path: Path) -> None:
    _write(path, one_page(PAGE), after_eof=b"\n" + SECRET.encode() + b"\n")


# ── Hidden but live: plain strings the Objects layer reads ─────────────

@case("leak.hidden-layer-text", cells="oc-off.plain", writer="raw", expected=LIVE_SSN,
      story="The SSN as text in a switched-off optional-content layer: no viewer shows it.",
      mistake="Hiding a layer instead of deleting its content.",
      recovery="Turn the layer on in the viewer's layers panel.")
def hidden_layer(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = (b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [7 0 R] "
                  b"/D << /OFF [7 0 R] /Order [7 0 R] >> >> >>")
    objects[3] = page(b"[4 0 R 5 0 R]",
                      b"<< /Font << /F1 6 0 R >> /Properties << /oc1 7 0 R >> >>")
    objects[5] = stream(b"", b"/OC /oc1 BDC " + text(SECRET, 600) + b" EMC")
    objects[7] = b"<< /Type /OCG /Name (Layer 1) >>"
    _write(path, objects)


@case("leak.hidden-annotation-appearance", cells="annot-appearance.plain", writer="raw",
      expected=LIVE_SSN,
      story="A hidden annotation whose appearance draws the SSN.",
      mistake="Hiding an annotation instead of deleting it.")
def hidden_annotation(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Square /Rect [72 500 300 530] /F 2 "
                  b"/AP << /N 8 0 R >> >>")
    objects[8] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 228 30] "
                        b"/Resources << /Font << /F1 6 0 R >> >>", text(SECRET, 10))
    _write(path, objects)


@case("leak.unused-form-resource", cells="unused-resource.plain", writer="raw",
      expected=LIVE_SSN,
      story="A form XObject in the page's resources that the page never draws.")
def unused_resource(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R >> /XObject << /Fm1 7 0 R >> >>")
    objects[7] = stream(b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                        b"/Resources << /Font << /F1 6 0 R >> >>", text(SECRET, 500))
    _write(path, objects)


@case("clean.raw", cells="live.plain", writer="raw", expected=expect(0),
      story="A minimal hand-assembled one-page document (control for the raw writer).")
def clean_raw(path: Path) -> None:
    _write(path, one_page(PAGE))



@case("leak.private-application-data", cells="private-data.plain", writer="raw",
      expected=expect(2, warnings=("REVIEW_BINARY",)), requires=("qpdf",),
      story="An editor's private data (/PieceInfo) on the page keeps the SSN as raw text; "
            "only the Binary layer's known-value sweep sees it (manual review).",
      mistake="Editing tools keep undo data or originals in their private dictionaries.")
def piece_info(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/PieceInfo << /Editor << /Private 7 0 R >> >>")
    objects[7] = stream(b"", b"original line: " + SECRET.encode())
    _write(path, objects)


@case("leak.associated-file", cells="embedded-other.plain", writer="raw",
      expected=expect(2, warnings=("REVIEW_BINARY",)), requires=("qpdf",),
      story="A PDF 2.0 associated file (/AF) on the page — not in the attachments list — "
            "holding the SSN; only the Binary layer sees it (manual review).")
def associated_file(path: Path) -> None:
    objects = one_page(PAGE)
    objects[3] = page(extra=b"/AF [7 0 R]")
    objects[7] = b"<< /Type /Filespec /F (source.txt) /EF << /F 8 0 R >> /AFRelationship /Source >>"
    objects[8] = stream(b"/Type /EmbeddedFile", b"Source notes: " + SECRET.encode())
    _write(path, objects)
