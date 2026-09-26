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
INCREMENTAL = ("Saving the redaction as an incremental update, which appends the change "
               "and keeps the original.")
RECOVER_REVISION = "Cut the file at the earlier revision's %%EOF and open that revision."


def leak(id: str, cells, story: str, expected=SUPERSEDED, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story, **kw)


@leak("revision.redacted-incremental", "orphaned.plain",
      "The SSN was redacted and saved incrementally: the original content stream is "
      "still in the file, no longer referenced by the new revision.",
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      features="superseded.plain", mistake=INCREMENTAL, recovery=RECOVER_REVISION)
def redacted_incremental(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@leak("revision.redacted-incremental-embedded-font", "orphaned.font",
      "Redacted and saved incrementally, page in an embedded font: the original "
      "stream's glyph codes stay in the file.",
      expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),)),
      mistake=INCREMENTAL)
def redacted_incremental_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@leak("revision.redacted-incremental-cjk", "orphaned.font",
      "The same, with the page in an embedded CJK font.",
      expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),)))
def redacted_incremental_cjk(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, cjk_font(page), lines=[*FILLER, f"SSN {SSN}"]); save(doc, path)
    update(path, lambda d: redact(d, SSN))


@leak("revision.redacted-incremental-compact", "orphaned.font.compact-syntax",
      "A compactly written page in an embedded font, garbage-collected, then redacted "
      "in an incremental save: the original stream, no longer referenced, still holds "
      "the SSN's glyph codes.",
      expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),)),
      mistake=INCREMENTAL, recovery=RECOVER_REVISION)
def redacted_incremental_compact(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"])
    page.clean_contents(); save(doc, path, garbage=4)
    update(path, lambda d: redact(d, SSN))


@leak("revision.redacted-after-object-streams", "orphaned.plain",
      "Redacted incrementally after an object-stream save.",
      expected=expect(1, findings=(("SSN", "orphaned"),)), mistake=INCREMENTAL)
def redacted_after_objstm(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"])
    save(doc, path, garbage=4, deflate=True, use_objstms=1)
    update(path, lambda d: redact(d, SSN))


@leak("revision.deleted-annotation", "orphaned.plain",
      "A sticky note with the SSN was deleted in an incremental save.",
      expected=expect(1, findings=(("SSN", "orphaned"),)), mistake=INCREMENTAL)
def deleted_annotation(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.add_text_annot((300, 300), f"Customer SSN {SSN}"); save(doc, path)

    def delete(d: fitz.Document) -> None:
        page = d[0]; page.delete_annot(next(page.annots()))
    update(path, delete)


@leak("revision.overwritten-title", "superseded.plain",
      "The title held the SSN; an incremental save changed it.",
      features="metadata.plain", mistake=INCREMENTAL, recovery=RECOVER_REVISION)
def overwritten_title(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_metadata({"title": f"Case {SSN}", "creationDate": "", "modDate": ""})
    save(doc, path)
    update(path, lambda d: d.set_metadata({**d.metadata, "title": "Case file"}))


@leak("revision.deleted-page-embedded-font", "orphaned.font",
      "A page in an embedded font deleted in an incremental save.",
      expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),)))
def deleted_page_embedded(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); body(page, embedded_font(page), lines=[f"SSN {SSN}"])
    save(doc, path)
    update(path, lambda d: d.delete_page(1))


@leak("revision.replaced-image", "superseded.pixels",
      "A scan showing the SSN was replaced by a 'REDACTED' image incrementally.",
      expected=expect(2, warnings=(("LEFTOVER_IMAGE", "superseded"),)),
      mistake=INCREMENTAL, recovery="Extract the images from the earlier revision.")
def replaced_image(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_image(fitz.Rect(72, 200, 372, 260), stream=png_of(f"SSN {SSN}"))
    save(doc, path)

    def replace(d: fitz.Document) -> None:
        page = d[0]; page.replace_image(page.get_images()[0][0], stream=png_of("REDACTED"))
    update(path, replace)


@leak("revision.rewritten-stream-mixed", "superseded.font",
      "A content stream rewritten in place by an incremental save; the old version "
      "held the SSN as glyph codes among plain lines.",
      expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "superseded"),)))
def rewritten_mixed(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); embedded_font(page); body(page)
    xref = page.get_contents()[0]
    doc.update_stream(xref, mixed_stream(embedded_glyphs(f"SSN {SSN}"))); save(doc, path)
    update(path, lambda d: d.update_stream(xref, mixed_stream(None)))


@leak("revision.truncated-stream-embedded-font", "superseded.font.compact-syntax",
      "A page in an embedded font, compacted, then its content stream cut short in an "
      "incremental save to drop the SSN line: only the earlier version holds it.",
      expected=expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "superseded"),)),
      mistake=INCREMENTAL)
def truncated_stream(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, embedded_font(page), lines=[*FILLER, f"SSN {SSN}"])
    page.clean_contents(); save(doc, path, garbage=4)

    def cut(d: fitz.Document) -> None:
        xref = d[0].get_contents()[0]
        data = d.xref_stream(xref); d.update_stream(xref, data[:data.rfind(b"BT")])
    update(path, cut)


@leak("revision.rewritten-stream", "superseded.plain",
      "The SSN overwritten with X's in the same content stream, saved incrementally. "
      "Tidying the page first left its original stream unreferenced, so the SSN is in "
      "an orphan as well as in the earlier revision.",
      expected=expect(1, findings=(("SSN", "superseded"), ("SSN", "orphaned"))),
      mistake=INCREMENTAL, recovery=RECOVER_REVISION)
def rewritten_stream(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, lines=[*FILLER, f"SSN {SSN}"])
    page.clean_contents(); xref = page.get_contents()[0]; save(doc, path)
    update(path, lambda d: d.update_stream(
        xref, d.xref_stream(xref).replace(SSN.encode(), b"XXX-XX-XXXX")))


@leak("revision.first-of-fifty-six", "superseded.plain",
      "The SSN only in the first revision, followed by 55 incremental saves: the "
      "original is still scanned even past the revision cap.",
      expected=expect(1, findings=(("SSN", "superseded"),), warnings=(("REVISION_CAP", "superseded"),)),
      features="superseded.plain.revision-cap")
def first_of_many(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.set_metadata({"title": f"Case {SSN}", "creationDate": "", "modDate": ""})
    save(doc, path)
    update(path, lambda d: d.set_metadata({**d.metadata, "title": "Case file"}))
    for i in range(55):
        update(path, lambda d, i=i: d[0].add_text_annot((60 + 8 * (i % 60), 700), f"n{i}"))  # type: ignore[misc]


@leak("revision.secret-past-the-cap", "superseded.plain.revision-cap",
      "The SSN only in the second of 58 revisions — one of those the cap leaves "
      "unscanned: the tool cannot see it, and says so rather than passing the file.",
      expected=expect(2, warnings=(("REVISION_CAP", "superseded"),)))
def past_the_cap(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    update(path, lambda d: d.set_metadata({**d.metadata, "title": f"Case {SSN}",
                                           "creationDate": "", "modDate": ""}))
    update(path, lambda d: d.set_metadata({**d.metadata, "title": "Case file"}))
    for i in range(55):
        update(path, lambda d, i=i: d[0].add_text_annot((60 + 8 * (i % 60), 700), f"n{i}"))  # type: ignore[misc]
