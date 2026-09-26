"""Secrets in files attached to the PDF — listed attachments and files
attached to annotations. The page looks clean; the attachment is not."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import fitz

from ..model import CODE, SSN, case, expect
from ..pdfkit import body, deflate, gzipbytes, png_of, save, zipbytes

NOT_TEXT = expect(2, warnings=("ATTACHMENT_NOT_TEXT",))
FORGOT = "Redacting the pages and forgetting the attachments."


def _attach(path: Path, name: str, data: bytes) -> None:
    doc = fitz.open(); body(doc.new_page()); doc.embfile_add(name, data); save(doc, path)


@case("leak.attachment.zip", cells="attachment.container", expected=NOT_TEXT,
      story="A zip of records attached to a clean-looking memo.", mistake=FORGOT,
      recovery="Open the attachments panel and save the zip.")
def zip_attachment(path: Path) -> None:
    _attach(path, "records.zip", zipbytes(f"SSN {SSN}"))


@case("leak.attachment.png", cells="attachment.pixels", expected=NOT_TEXT,
      story="A scan of the SSN attached as a PNG.", mistake=FORGOT)
def png_attachment(path: Path) -> None:
    _attach(path, "scan.png", png_of(f"SSN {SSN}"))


@case("leak.attachment.utf8-text", cells="attachment.plain",
      expected=expect(2, warnings=("REVIEW_HIDDEN_TEXT",)),
      story="Notes with the SSN attached as UTF-8 text (a run of arbitrary text: manual review).")
def utf8_text(path: Path) -> None:
    _attach(path, "notes.txt", f"メモ SSN {SSN}\n".encode())


@case("leak.attachment.utf16le-no-bom", cells="attachment.plain",
      expected=expect(2, warnings=("REVIEW_HIDDEN_TEXT",)),
      story="Notes attached as UTF-16LE without a byte-order mark.")
def utf16(path: Path) -> None:
    _attach(path, "notes.txt", f"SSN {SSN}\r\n".encode("utf-16-le"))


@case("leak.attachment.nested-pdf", cells="attachment.container", expected=NOT_TEXT,
      story="The unredacted original PDF attached to the redacted one.",
      mistake="Attaching the source document for reference.")
def nested_pdf(path: Path) -> None:
    inner = fitz.open(); body(inner.new_page(), lines=[f"SSN {SSN}"])
    data = inner.tobytes(garbage=4, deflate=True, no_new_id=True); inner.close()
    _attach(path, "inner.pdf", data)


@case("leak.attachment.nested-pdf-small", cells="attachment.container", expected=NOT_TEXT,
      story="A small receipt PDF with the SSN attached.")
def nested_pdf_small(path: Path) -> None:
    inner = fitz.open(); inner.new_page().insert_text((72, 72), f"Code {CODE} SSN {SSN}")
    data = inner.tobytes(deflate=True, no_new_id=True); inner.close()
    _attach(path, "receipt.pdf", data)


@case("leak.attachment.stored-zip-deflated-member", cells="attachment.container",
      expected=NOT_TEXT,
      story="A stored (uncompressed) zip whose second member is itself zlib-compressed.")
def stored_zip(path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("a.txt", (2000, 1, 1, 0, 0, 0)), "hello " * 50)
        zf.writestr(zipfile.ZipInfo("b.bin", (2000, 1, 1, 0, 0, 0)),
                    deflate(f"SSN {SSN}".encode()))
    _attach(path, "mix.zip", buf.getvalue())


@case("leak.attachment.gzip", cells="attachment.container", expected=NOT_TEXT,
      story="Notes attached gzip-compressed.")
def gzip_attachment(path: Path) -> None:
    _attach(path, "notes.txt.gz", gzipbytes(f"SSN {SSN}\n".encode()))


@case("leak.attachment.annotation-zip", cells="attachment.container", expected=NOT_TEXT,
      story="A zip attached to a file annotation (paperclip icon) on the page.")
def annotation_zip(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.add_file_annot((300, 300), zipbytes(f"SSN {SSN}"), "rec.zip"); save(doc, path)


@case("leak.revision.replaced-attachment", cells="superseded.container",
      expected=expect(2, warnings=("LEFTOVER_CONTAINER",)),
      story="A zip attachment holding the SSN replaced by a clean one in an incremental save.",
      mistake="Replacing an attachment incrementally keeps the original.")
def replaced_attachment(path: Path) -> None:
    from ..pdfkit import update
    _attach(path, "records.zip", zipbytes(f"SSN {SSN}"))
    def replace(d: fitz.Document) -> None:
        for xref in range(1, d.xref_length()):
            if d.xref_get_key(xref, "Type")[1] == "/EmbeddedFile":
                d.update_stream(xref, zipbytes("nothing here"))
    update(path, replace)
