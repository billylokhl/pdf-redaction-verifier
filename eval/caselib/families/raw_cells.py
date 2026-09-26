"""Non-fitz evidence: every claimed cell (docs/REDESIGN.md §5) needs at
least one leak case whose bytes were never serialised by PyMuPDF — the
library the tool itself reads with, so a fitz-only corpus would hide any
place the two disagree. These cases rebuild, by hand, the structures a
few families elsewhere only ever produce through ``fitz.Document.save``:
a font-coded live page (a custom ``/Differences`` encoding needs no
embedded program), listed attachments (a real ``/EmbeddedFiles`` name
tree), and superseded content (``rawpdf.incremental_update``, this
review's second hand-assembled writer capability, alongside object
streams and cross-reference streams for the couple of cases that use
them)."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import SSN, case, expect
from ..pdfkit import compressed, png_of, zipbytes
from ..rawpdf import build, build_objstm, incremental_update, one_page, page, stream, text

SECRET = f"SSN {SSN}"
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
ORPHAN_SSN = expect(1, findings=(("SSN", "orphaned"),))
SUPERSEDED_SSN = expect(1, findings=(("SSN", "superseded"),))
UNDECODABLE_ORPHAN = expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),))
UNDECODABLE_SUPERSEDED = expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "superseded"),))
NOT_TEXT = expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),))
PAGE = stream(b"", text("Quarterly report"))
# "SSN 123-45-6789" as glyph ids (NUL-interleaved: unreadable as latin-1
# text), the same fixture families/raw.py's leftover cases use — the
# Objects layer judges leftover streams by their raw codes, not through a
# font, so no real font object is needed for it to call these undecodable.
GLYPHS = b"<0034003400310003001400150016000E0017001800190019001A001B001C>"


def leak(id: str, cells, story: str, expected=LIVE_SSN, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story,
                writer="raw", **kw)


def _write(path: Path, objects: dict[int, bytes], **kw) -> None:
    path.write_bytes(build(objects, **kw))


# ── Font-coded text with no embedded program ────────────────────────────
# A live page is read through the font's Unicode map (docs/COVERAGE.md
# footnote 1 names a custom /Differences encoding as one kind, alongside
# Identity-H). A standard font needs no embedded program for that map:
# /Differences alone, resolved through the Adobe Glyph List, is enough —
# no fitz.Font buffer required, so the whole file is hand-assembled.
_GLYPH_NAMES = {" ": "space", "-": "hyphen", "0": "zero", "1": "one", "2": "two",
                "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
                "8": "eight", "9": "nine"}


def _glyph_name(c: str) -> str:
    if c in _GLYPH_NAMES:
        return _GLYPH_NAMES[c]
    if c.isalpha():
        return c                # the AGL names 'A'-'Z' and 'a'-'z' after themselves
    raise ValueError(f"no standard glyph name for {c!r}")


def differences_font(chars: str) -> bytes:
    """A non-embedded Helvetica whose codes 1..N are remapped, in order, to
    *chars* via /Differences — font-coded text with no font program."""
    names = b" ".join(b"/" + _glyph_name(c).encode() for c in chars)
    return (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding "
            b"<< /Type /Encoding /Differences [1 " + names + b"] >> >>")


def coded(chars: str) -> bytes:
    """*chars* shown as the sequential codes differences_font(chars) maps."""
    return b"<" + "".join("%02X" % (i + 1) for i in range(len(chars))).encode() + b">"


@leak("page.raw-visible", "live.plain", "The SSN drawn directly on the page.")
def visible(path: Path) -> None:
    _write(path, one_page(stream(b"", text(SECRET))))


@leak("page.raw-font-coded", "live.font",
      "The SSN on the page in a font with a custom /Differences encoding: the codes "
      "are not the characters shown, but the font's own map recovers them.")
def font_coded(path: Path) -> None:
    content = stream(b"", b"BT /F1 12 Tf 72 700 Td " + coded(SECRET) + b" Tj ET")
    _write(path, one_page(content, o6=differences_font(SECRET)))


@leak("page.raw-pixels", "live.pixels",
      "A scanned-looking strip of pixels showing the SSN, drawn directly on the page "
      "as an inline image.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "OCR"),)),
      requires=("ocr",), mistake="Redacting the text layer and forgetting the scan.")
def pixels(path: Path) -> None:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(SECRET)))
    w, h = gray.width, gray.height
    data = (b"q %d 0 0 %d 72 400 cm BI /W %d/H %d/BPC 8/CS/G ID " % (w, h, w, h)
            + gray.samples + b"\nEI Q")
    _write(path, one_page(stream(b"", data)))


@leak("page.raw-above-page", "off-page.plain",
      "The SSN drawn above the top edge of the page, where no viewer shows it.",
      recovery="Copy all text, or enlarge the page box.")
def above_page(path: Path) -> None:
    _write(path, one_page(stream(b"", text(SECRET, -40))))


@leak("page.raw-off-page-font-coded", "off-page.font",
      "The SSN off the right edge of the page, in a font with a custom /Differences "
      "encoding — the map still resolves it, wherever it is drawn.")
def off_page_font_coded(path: Path) -> None:
    content = stream(b"", b"BT /F1 12 Tf 700 100 Td " + coded(SECRET) + b" Tj ET")
    _write(path, one_page(content, o6=differences_font(SECRET)))


# ── Document-level live content ─────────────────────────────────────────

@leak("document.raw-title", "metadata.plain",
      "The SSN in the Info dictionary's title.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Metadata"),)),
      requires=("exiftool",),
      mistake="Redacting the page but not the document properties.")
def info_title(path: Path) -> None:
    objects = one_page(PAGE, o7=b"<< /Title (Case " + SSN.encode() + b") >>")
    _write(path, objects, info=7)


@leak("leftover.raw-xmp", "leftover-xmp.plain",
      "An old XMP metadata packet with the SSN in its title, no longer referenced.",
      expected=ORPHAN_SSN)
def orphan_xmp(path: Path) -> None:
    xmp = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
           '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>Case {SSN}</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>'
           '<?xpacket end="w"?>')
    _write(path, one_page(PAGE, o5=stream(b"/Type /Metadata /Subtype /XML", xmp.encode())))


@leak("document.raw-javascript", "javascript.plain",
      "The SSN inside the document's open-action JavaScript.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Hidden"),)))
def javascript(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R /OpenAction 7 0 R >>"
    objects[7] = b"<< /S /JavaScript /JS (var ssn = '" + SSN.encode() + b"';) >>"
    _write(path, objects)


@leak("document.raw-form-field", "annot-fields.plain", "The SSN as a form field's value.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Hidden"),)))
def form_field(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [7 0 R] >> >>"
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (ssn) "
                  b"/Rect [72 300 300 320] /V (" + SSN.encode() + b") >>")
    _write(path, objects)


# ── Attachments: a real /EmbeddedFiles name tree ────────────────────────

def attachment_objects(name: str, data: bytes, filespec: int = 7, efile: int = 8) -> dict[int, bytes]:
    """A document-level attachment: catalog /Names /EmbeddedFiles name tree,
    file specification, embedded-file stream — the structure PyMuPDF's own
    embfile_* calls read, hand-assembled instead of written through them."""
    catalog = (b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names "
               b"[(%s) %d 0 R] >> >> >>" % (name.encode(), filespec))
    spec = b"<< /Type /Filespec /F (%s) /EF << /F %d 0 R >> >>" % (name.encode(), efile)
    ef = stream(b"/Type /EmbeddedFile /Params << /Size %d >>" % len(data), data)
    return {1: catalog, filespec: spec, efile: ef}


FORGOT = "Redacting the pages and forgetting the attachments."


@leak("attachment.raw-zip", "attachment.container",
      "A zip of records attached to a clean-looking memo.", expected=NOT_TEXT,
      mistake=FORGOT, recovery="Open the attachments panel and save the zip.")
def zip_attachment(path: Path) -> None:
    objects = one_page(PAGE)
    objects.update(attachment_objects("records.zip", compressed("zip", SECRET)))
    _write(path, objects)


@leak("attachment.raw-scan-png", "attachment.pixels",
      "A scan of the SSN attached as a PNG.", expected=NOT_TEXT, mistake=FORGOT)
def png_attachment(path: Path) -> None:
    objects = one_page(PAGE)
    objects.update(attachment_objects("scan.png", png_of(SECRET)))
    _write(path, objects)


@leak("attachment.raw-notes", "attachment.plain",
      "Notes with the SSN attached as plain text (a run of arbitrary text: manual "
      "review).",
      expected=expect(2, warnings=(("REVIEW_HIDDEN_TEXT", "live"),)), mistake=FORGOT)
def notes_attachment(path: Path) -> None:
    objects = one_page(PAGE)
    objects.update(attachment_objects("notes.txt", f"memo {SECRET}\n".encode()))
    _write(path, objects)


# ── Orphaned content ─────────────────────────────────────────────────────

@leak("leftover.raw-orphaned-container", "orphaned.container",
      "An orphaned zip payload, stored but referenced by nothing.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)))
def orphaned_container(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"", compressed("zip", SECRET))))


@leak("leftover.raw-attachment-zip", "orphaned-attachment.container",
      "A removed zip attachment holding the SSN, still stored.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)))
def orphan_attachment_zip(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Type /EmbeddedFile", compressed("zip", SECRET))))


@leak("leftover.raw-attachment-text", "orphaned-attachment.plain",
      "A removed attachment's text is still stored, unreferenced (manual review: a run "
      "of arbitrary text).",
      expected=expect(2, warnings=(("REVIEW_OBJECT_TEXT", "orphaned"),)))
def orphan_attachment_text(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Type /EmbeddedFile", f"Employee {SECRET}\n".encode())))


@leak("leftover.raw-attachment-png", "orphaned-attachment.pixels",
      "A removed PNG attachment showing the SSN, still stored.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)))
def orphan_attachment_png(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Type /EmbeddedFile", png_of(SECRET))))


@leak("leftover.raw-compact-glyphs", "orphaned.font.compact-syntax",
      "An orphaned stream sets its font and shows the SSN's glyph codes right after "
      "BT with no separating space (\"BT/EM 11 Tf\"), as a minimizer or redactor "
      "writes it.", expected=UNDECODABLE_ORPHAN)
def compact_glyphs(path: Path) -> None:
    content = b"BT/EM 11 Tf 72 700 Td" + GLYPHS + b"Tj ET"
    _write(path, one_page(PAGE, o5=stream(b"", content)))


# ── Superseded by an incremental update ─────────────────────────────────
# rawpdf.incremental_update appends the new objects and cross-reference
# section itself; the object each replaces stays at its old offset, which
# is exactly what lets the verifier's earlier-revision walk find it again.

INCREMENTAL = "Saving the redaction as an incremental update, which appends the change and keeps the original."
RECOVER_REVISION = "Cut the file at the earlier revision's %%EOF and open that revision."


def _incremental(base: bytes, new: dict[int, bytes | None]):
    """A case builder: *base* with *new* appended as one incremental update."""
    def build_case(path: Path) -> None:
        path.write_bytes(incremental_update(base, new))
    return build_case


case(
    "revision.raw-rewritten-stream", truth="leak", cells="superseded.plain",
    expected=SUPERSEDED_SSN, writer="raw",
    story="A content stream holding the SSN, overwritten by an incremental update with "
          "clean text — the original stays at its old offset in the file.",
    mistake=INCREMENTAL, recovery=RECOVER_REVISION,
)(_incremental(build(one_page(stream(b"", text(SECRET)))),
               {4: stream(b"", text("REDACTED"))}))

case(
    "revision.raw-rewritten-font", truth="leak", cells="superseded.font",
    expected=UNDECODABLE_SUPERSEDED, writer="raw",
    story="A content stream showing the SSN's glyph codes, rewritten by an incremental "
          "update to plain filler text: the earlier version is undecodable, but still there.",
    mistake=INCREMENTAL,
)(_incremental(build(one_page(stream(b"", b"BT /EM 11 Tf 72 700 Td " + GLYPHS + b" Tj ET"))),
               {4: stream(b"", text("clean now"))}))

case(
    "revision.raw-rewritten-font-compact", truth="leak", cells="superseded.font.compact-syntax",
    expected=UNDECODABLE_SUPERSEDED, writer="raw",
    story="The same, written compactly (\"BT/EM 11 Tf\", no space after BT) before the "
          "incremental update replaces it.",
    mistake=INCREMENTAL,
)(_incremental(build(one_page(stream(b"", b"BT/EM 11 Tf 72 700 Td" + GLYPHS + b"Tj ET"))),
               {4: stream(b"", text("clean now"))}))


def _pixel_strip(word: str) -> tuple[bytes, int, int]:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(word)))
    return gray.samples, gray.width, gray.height


def _inline_image(word: str) -> bytes:
    samples, w, h = _pixel_strip(word)
    return (b"q %d 0 0 %d 72 400 cm BI /W %d/H %d/BPC 8/CS/G ID " % (w, h, w, h)
            + samples + b"\nEI Q")


case(
    "revision.raw-replaced-image", truth="leak", cells="superseded.pixels",
    expected=expect(2, warnings=(("LEFTOVER_IMAGE", "superseded"),)), writer="raw",
    story="A scan showing the SSN, replaced by a clean image in an incremental update.",
    mistake=INCREMENTAL, recovery="Extract the images from the earlier revision.",
)(_incremental(build(one_page(stream(b"", _inline_image(SECRET)))),
               {4: stream(b"", _inline_image("REDACTED"))}))

_ATTACHMENT_BASE = one_page(stream(b"", text("memo")))
_ATTACHMENT_BASE.update(attachment_objects("records.zip", compressed("zip", SECRET)))
_CLEAN_ZIP = zipbytes("nothing here")

case(
    "revision.raw-replaced-attachment", truth="leak", cells="superseded.container",
    expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "superseded"),
                                 ("ATTACHMENT_NOT_TEXT", "live"))),
    writer="raw",
    story="A zip attachment holding the SSN, replaced by a clean one in an incremental "
          "update.",
    mistake="Replacing an attachment incrementally keeps the original.",
)(_incremental(build(_ATTACHMENT_BASE),
               {8: stream(b"/Type /EmbeddedFile /Params << /Size %d >>" % len(_CLEAN_ZIP),
                          _CLEAN_ZIP)}))


def _many_revisions(path: Path) -> None:
    """The SSN only in the second of 57 earlier revisions, the rest no-ops:
    past the revision cap, so the tool says it cannot certify rather than
    passing the file as clean."""
    pdf = build(one_page(stream(b"", text("no secret here"))))
    pdf = incremental_update(pdf, {7: b"<< /Marker (" + SSN.encode() + b") >>"})
    pdf = incremental_update(pdf, {7: b"<< /Marker (gone) >>"})
    for i in range(55):
        pdf = incremental_update(pdf, {8: b"<< /N %d >>" % i})
    path.write_bytes(pdf)


case(
    "revision.raw-many-revisions", truth="leak", cells="superseded.plain.revision-cap",
    expected=expect(2, warnings=(("REVISION_CAP", "superseded"),)), writer="raw",
    story="The SSN sits only in the second of 57 earlier revisions; once more than 50 "
          "pile up, that one falls outside what gets scanned, and the tool says so "
          "rather than pass the file.",
)(_many_revisions)


# ── Object streams and cross-reference streams (PDF 1.5) ────────────────
# The classic writer above never exercises this format; a couple of cases
# rebuilt on it check that the verifier (which reads through PyMuPDF)
# agrees with a hand-assembled ObjStm/XRef stream file too.

@case("page.raw-minimal-objstm", truth="clean", features="live.plain", writer="raw",
      expected=expect(0),
      story="A minimal one-page document written as an object stream with a "
            "cross-reference stream (control for the objstm/xref-stream writer).")
def clean_objstm(path: Path) -> None:
    path.write_bytes(build_objstm(one_page(stream(b"", text("Quarterly report")))))


@leak("leftover.raw-orphaned-objstm", "orphaned.plain",
      "An orphaned stream holding the SSN, in a file whose objects are packed into an "
      "object stream and whose cross-reference is itself a stream.",
      expected=ORPHAN_SSN)
def orphaned_objstm(path: Path) -> None:
    objects = one_page(PAGE, o5=stream(b"", text(SECRET)))
    path.write_bytes(build_objstm(objects))


def _superseded_objstm(path: Path) -> None:
    base = build_objstm(one_page(stream(b"", text(SECRET))))
    path.write_bytes(incremental_update(base, {4: stream(b"", text("REDACTED"))}))


case(
    "revision.raw-rewritten-stream-objstm", truth="leak", cells="superseded.plain",
    expected=SUPERSEDED_SSN, writer="raw",
    story="The same overwritten content stream, on a base file written as an object "
          "stream with a cross-reference stream, then updated with a classic "
          "incremental section — the two formats chain together correctly.",
)(_superseded_objstm)
