"""Secrets in files attached to the PDF — listed attachments and files
attached to annotations. The page looks clean; the attachment is not."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import fitz

from ..model import CODE, SSN, case, expect
from ..pdfkit import body, compressed, png_of, save, update, zipbytes

NOT_TEXT = expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),))
FORGOT = "Redacting the pages and forgetting the attachments."


def leak(id: str, cells, story: str, expected=NOT_TEXT, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story, **kw)


def _attach(path: Path, name: str, data: bytes) -> None:
    doc = fitz.open(); body(doc.new_page()); doc.embfile_add(name, data); save(doc, path)


@leak("attachment.records-zip", "attachment.container",
      "A zip of records attached to a clean-looking memo.", mistake=FORGOT,
      recovery="Open the attachments panel and save the zip.")
def zip_attachment(path: Path) -> None:
    _attach(path, "records.zip", compressed("zip", f"SSN {SSN}"))


@leak("attachment.scan-png", "attachment.pixels",
      "A scan of the SSN attached as a PNG.", mistake=FORGOT,
      recovery="Open the attachments panel and view the image.")
def png_attachment(path: Path) -> None:
    _attach(path, "scan.png", png_of(f"SSN {SSN}"))


@leak("attachment.ssn-notes-utf8", "attachment.plain",
      "Notes with the SSN attached as UTF-8 text (a run of arbitrary text: manual review).",
      expected=expect(2, warnings=(("REVIEW_HIDDEN_TEXT", "live"),)), mistake=FORGOT,
      recovery="Open the attachments panel and read the text file.")
def utf8_text(path: Path) -> None:
    _attach(path, "notes.txt", f"メモ SSN {SSN}\n".encode())


@leak("attachment.ssn-notes-utf16le", "attachment.plain",
      "Notes attached as UTF-16LE without a byte-order mark.",
      expected=expect(2, warnings=(("REVIEW_HIDDEN_TEXT", "live"),)), mistake=FORGOT,
      recovery="Open the attachment and decode it as UTF-16LE.")
def utf16(path: Path) -> None:
    _attach(path, "notes.txt", f"SSN {SSN}\r\n".encode("utf-16-le"))


@leak("attachment.original-pdf", "attachment.container",
      "The unredacted original PDF attached to the redacted one.",
      mistake="Attaching the source document for reference.",
      recovery="Open the attachments panel and read the inner PDF directly.")
def nested_pdf(path: Path) -> None:
    inner = fitz.open(); body(inner.new_page(), lines=[f"SSN {SSN}"])
    data = inner.tobytes(garbage=4, deflate=True, no_new_id=True); inner.close()
    _attach(path, "inner.pdf", data)


@leak("attachment.receipt-pdf", "attachment.container",
      "A small receipt PDF with the SSN attached.", mistake=FORGOT,
      recovery="Open the attachments panel and read the inner PDF directly.")
def nested_pdf_small(path: Path) -> None:
    inner = fitz.open(); inner.new_page().insert_text((72, 72), f"Code {CODE} SSN {SSN}")
    data = inner.tobytes(deflate=True, no_new_id=True); inner.close()
    _attach(path, "receipt.pdf", data)


@leak("attachment.stored-zip-deflated-member", "attachment.container",
      "A stored (uncompressed) zip whose second member is itself zlib-compressed.",
      mistake=FORGOT, recovery="Open the attached zip and extract the second member.")
def stored_zip(path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("a.txt", (2000, 1, 1, 0, 0, 0)), "hello " * 50)
        zf.writestr(zipfile.ZipInfo("b.bin", (2000, 1, 1, 0, 0, 0)),
                    compressed("zlib", f"SSN {SSN}"))
    _attach(path, "mix.zip", buf.getvalue())


@leak("attachment.notes-gzip", "attachment.container", "Notes attached gzip-compressed.",
      mistake=FORGOT, recovery="Open the attachments panel and decompress the .gz file.")
def gzip_attachment(path: Path) -> None:
    _attach(path, "notes.txt.gz", compressed("gzip", f"SSN {SSN}\n"))


@leak("attachment.annotation-zip", "attachment.container",
      "A zip attached to a file annotation (paperclip icon) on the page.",
      mistake=FORGOT, recovery="Double-click the paperclip icon and extract the zip.")
def annotation_zip(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.add_file_annot((300, 300), compressed("zip", f"SSN {SSN}"), "rec.zip"); save(doc, path)


@leak("revision.replaced-attachment", "superseded.container",
      "A zip attachment holding the SSN replaced by a clean one in an incremental save.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "superseded"),
                                   ("ATTACHMENT_NOT_TEXT", "live"))),
      mistake="Replacing an attachment incrementally keeps the original.",
      recovery="Decompress the file and read the earlier revision's embedded-file stream.")
def replaced_attachment(path: Path) -> None:
    _attach(path, "records.zip", compressed("zip", f"SSN {SSN}"))

    def replace(d: fitz.Document) -> None:
        for xref in range(1, d.xref_length()):
            if d.xref_get_key(xref, "Type")[1] == "/EmbeddedFile":
                d.update_stream(xref, zipbytes("nothing here"))
    update(path, replace)
