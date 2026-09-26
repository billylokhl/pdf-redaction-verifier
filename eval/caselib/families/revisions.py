"""Secrets kept by incremental saves: the update appends new versions of
objects, and every earlier version stays in the file."""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import SSN, case, expect
from ..pdfkit import (FILLER, body, cjk_font, embedded_font, png_of, redact,
                      save, update)
from .leftover import embedded_glyphs, mixed_stream

SUPERSEDED = expect(1, findings=(("SSN", "superseded"),))
INCREMENTAL = "Saving the redaction as an incremental update, which appends and keeps the original."
RECOVER_REVISION = "Cut the file at the earlier %%EOF and open that revision."


@case("leak.revision.redacted-incremental", cells=("superseded.plain", "orphaned.plain"),
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      story="The SSN was redacted and saved incrementally: the original content stream is "
            "still in the file, unreferenced by the new revision.",
      mistake=INCREMENTAL, recovery=RECOVER_REVISION)
def redacted_incremental(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@case("leak.revision.redacted-incremental-cjk", cells="superseded.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="The same, with the page in an embedded CJK font.")
def redacted_incremental_cjk(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, cjk_font(page), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@case("leak.revision.redacted-incremental-embedded-font", cells="orphaned.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="Redacted and saved incrementally, page in an embedded font: the original "
            "stream's glyph codes stay in the file.", mistake=INCREMENTAL)
def redacted_incremental_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@case("leak.revision.redacted-after-object-streams", cells="orphaned.plain",
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      story="Redacted incrementally after an object-stream save.", mistake=INCREMENTAL)
def redacted_after_objstm(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"])
    save(doc, path, garbage=4, deflate=True, use_objstms=1)
    update(path, lambda d: redact(d, SSN))


@case("leak.revision.deleted-annotation", cells="orphaned.plain",
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      story="A sticky note with the SSN was deleted in an incremental save.",
      mistake=INCREMENTAL)
def deleted_annotation(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.add_text_annot((300, 300), f"Customer SSN {SSN}"); save(doc, path)

    def delete(d: fitz.Document) -> None:
        page = d[0]; page.delete_annot(next(page.annots()))
    update(path, delete)


@case("leak.revision.overwritten-title", cells=("superseded.plain", "metadata.plain"),
      expected=SUPERSEDED,
      story="The title held the SSN; an incremental save changed it.", mistake=INCREMENTAL)
def overwritten_title(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_metadata({"title": f"Case {SSN}", "creationDate": "", "modDate": ""})
    save(doc, path)
    update(path, lambda d: d.set_metadata({**d.metadata, "title": "Case file"}))


@case("leak.revision.deleted-page-embedded-font", cells="superseded.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="A page in an embedded font deleted in an incremental save.")
def deleted_page_embedded(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); body(page, embedded_font(page), lines=[f"SSN {SSN}"])
    save(doc, path)
    update(path, lambda d: d.delete_page(1))


@case("leak.revision.replaced-image", cells="superseded.pixels",
      expected=expect(2, warnings=("LEFTOVER_IMAGE",)),
      story="A scan showing the SSN was replaced by a 'REDACTED' image incrementally.",
      mistake=INCREMENTAL, recovery="Extract the images from the earlier revision.")
def replaced_image(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_image(fitz.Rect(72, 200, 372, 260), stream=png_of(f"SSN {SSN}"))
    save(doc, path)

    def replace(d: fitz.Document) -> None:
        page = d[0]; page.replace_image(page.get_images()[0][0], stream=png_of("REDACTED"))
    update(path, replace)


@case("leak.revision.rewritten-stream-mixed", cells="superseded.font",
      expected=expect(2, warnings=("LEFTOVER_UNDECODABLE_TEXT",)),
      story="A content stream rewritten in place by an incremental save; the old version "
            "held the SSN as glyph codes among plain lines.")
def rewritten_mixed(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); embedded_font(page); body(page)
    xref = page.get_contents()[0]
    doc.update_stream(xref, mixed_stream(embedded_glyphs(f"SSN {SSN}"))); save(doc, path)
    update(path, lambda d: d.update_stream(xref, mixed_stream(None)))


@case("leak.revision.rewritten-stream", cells="superseded.plain", expected=SUPERSEDED,
      story="The SSN overwritten with X's in the same content stream, saved incrementally.",
      mistake=INCREMENTAL, recovery=RECOVER_REVISION)
def rewritten_stream(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, lines=[*FILLER, f"SSN {SSN}"])
    page.clean_contents(); xref = page.get_contents()[0]; save(doc, path)
    update(path, lambda d: d.update_stream(
        xref, d.xref_stream(xref).replace(SSN.encode(), b"XXX-XX-XXXX")))


@case("leak.revision.first-of-fifty-six", cells=("superseded.plain", "superseded.revision-cap"),
      expected=expect(1, findings=(("SSN", "superseded"),), warnings=("REVISION_CAP",)),
      story="The SSN only in the first revision, followed by 55 incremental saves: the "
            "original is still scanned even past the revision cap.")
def first_of_many(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_metadata({"title": f"Case {SSN}", "creationDate": "", "modDate": ""})
    save(doc, path)
    update(path, lambda d: d.set_metadata({**d.metadata, "title": "Case file"}))
    for i in range(55):
        update(path, lambda d, i=i: d[0].add_text_annot((60 + 8 * (i % 60), 700), f"n{i}"))
