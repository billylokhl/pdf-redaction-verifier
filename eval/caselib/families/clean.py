"""Clean documents: nothing sensitive anywhere. They measure false
alarms — a clean document must exit 0 unless it holds something the tool
honestly cannot read (then 2, never 1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import fitz

from ..model import SSN, case, expect
from ..pdfkit import (FILLER, body, cjk_font, embedded_font, png_of, redact,
                      save, update, zipbytes)

CLEAN = expect(0)


def clean(id: str, features, story: str, expected=CLEAN, **kw):
    return case(id, truth="clean", features=features, expected=expected, story=story, **kw)


@clean("page.memo", "live.plain", "A one-page memo in a standard font.")
def plain(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)


@clean("page.memo-three-pages-compacted", "live.plain",
       "Three pages, saved compacted and compressed.")
def multipage(path: Path) -> None:
    doc = fitz.open()
    for i in range(3):
        body(doc.new_page(), lines=[*FILLER, f"Page {i + 1} of 3"])
    save(doc, path, garbage=4, deflate=True)


@clean("page.memo-embedded-font", "live.font",
       "A memo in an embedded font, text stored as glyph codes.")
def embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, embedded_font(page)); save(doc, path)


@clean("page.memo-cjk", "live.font", "A memo with a line in Chinese, embedded CJK font.")
def cjk(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, cjk_font(page), lines=[*FILLER, "季度报告"]); save(doc, path)


@clean("page.memo-with-picture", "live.pixels",
       "A memo with a picture containing harmless text.", requires=("ocr",))
def live_image(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_image(fitz.Rect(72, 200, 372, 260), stream=png_of("Harmless picture text"))
    save(doc, path)


@clean("page.memo-linearized", "live.font",
       "A two-page memo linearized for fast web view.", writer="qpdf", requires=("qpdf",))
def linearized(path: Path) -> None:
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(); body(page, embedded_font(page))
    tmp = path.with_suffix(".tmp.pdf"); save(doc, tmp)
    subprocess.run(["qpdf", "--deterministic-id", "--linearize", str(tmp), str(path)],
                   check=True)
    tmp.unlink()


@clean("page.memo-object-streams", ("live.font", "metadata.plain"),
       "A memo saved with object streams, as Acrobat and Chrome do.")
def object_streams(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, embedded_font(page))
    doc.set_metadata({"title": "Ops summary", "author": "Facilities"})
    save(doc, path, garbage=4, deflate=True, use_objstms=1)


@clean("page.slug-outside-crop", "off-page.plain", "A printer's slug line outside the crop box.")
def slug(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(width=612, height=792); body(page)
    page.insert_text((72, 780), "SLUG: job 4471 proof 2 printed on press 3")
    page.set_cropbox(fitz.Rect(0, 0, 612, 700)); save(doc, path)


@clean("page.rotated-margin-note", "live.plain", "A memo with a rotated margin note.")
def rotated(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((500, 600), "Internal distribution only", rotate=90); save(doc, path)


@clean("attachment.notes-text", "attachment.plain", "A memo with plain-text notes attached.")
def text_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("notes.txt", b"Plain notes, nothing sensitive\n"); save(doc, path)


@clean("attachment.bundle-zip", "attachment.container",
       "A memo with a harmless zip attached — flagged, since zips are not unpacked.",
       expected=expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),)))
def zip_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("bundle.zip", zipbytes("nothing sensitive here")); save(doc, path)


@clean("attachment.logo-png", "attachment.pixels",
       "A memo with a logo attached — flagged, since attached images are not OCR'd.",
       expected=expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),)))
def png_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("logo.png", png_of("ACME")); save(doc, path)


@clean("attachment.notes-utf8", "attachment.plain",
       "Notes in Japanese and accented Latin, attached as UTF-8.")
def utf8_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("notes_ja.txt", "会議のメモ。機密情報なし。Café résumé\n".encode())
    save(doc, path)


@clean("attachment.clean-pdf-uncompressed", "attachment.container",
       "A clean PDF attached, stored uncompressed — flagged, since attached PDFs are not opened.",
       expected=expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),)))
def attached_pdf(path: Path) -> None:
    inner = fitz.open(); page = inner.new_page(); body(page, embedded_font(page))
    data = inner.tobytes(no_new_id=True); inner.close()
    doc = fitz.open(); body(doc.new_page()); doc.embfile_add("inner.pdf", data)
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Type")[1] == "/EmbeddedFile":
            doc.update_stream(xref, data, compress=False)
    save(doc, path)


@clean("document.form-name", "annot-fields.plain", "A form with a harmless name filled in.")
def form(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    widget = fitz.Widget(); widget.field_name = "name"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = fitz.Rect(72, 300, 300, 320); widget.field_value = "Jane Public"
    page.add_widget(widget); save(doc, path)


@clean("leftover.small-icon", "orphaned.pixels.small",
       "A deleted page left a 12-pixel icon behind — too small to hold text.")
def small_icon(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); body(page)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 12, 12), False)
    pix.set_rect(pix.irect, (0, 0, 0))
    page.insert_image(fitz.Rect(72, 300, 84, 312), pixmap=pix)
    doc.delete_page(1); save(doc, path)


@clean("leftover.redacted-and-compacted", "orphaned.plain",
       "An SSN redacted properly: removed, then saved with garbage collection.",
       mistake="None — this is the correct procedure.")
def redacted_compacted(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"])
    redact(doc, SSN); save(doc, path, garbage=4, deflate=True)


@clean("revision.sticky-note-added", "superseded.plain",
       "A reviewer added a sticky note in an incremental save.")
def incr_annot(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    update(path, lambda d: d[0].add_text_annot((300, 300), "Reviewed, nothing to add"))


@clean("revision.line-added-embedded-font", "superseded.font",
       "An approval line added in an incremental save, embedded font.")
def incr_edit_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, embedded_font(page)); save(doc, path)

    def edit(d: fitz.Document) -> None:
        page = d[0]
        page.insert_text((72, 400), "Approved for circulation", fontname=embedded_font(page))
    update(path, edit)


@clean("revision.line-added", "superseded.plain",
       "An approval line added in an incremental save.")
def incr_edit(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    update(path, lambda d: d[0].insert_text((72, 400), "Approved for circulation"))


@clean("revision.ten-review-notes", "superseded.plain",
       "Ten rounds of review notes, each an incremental save.")
def ten_revisions(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    for i in range(10):
        update(path, lambda d, i=i: d[0].add_text_annot((100 + 10 * i, 500), f"note {i}"))  # type: ignore[misc]


@clean("revision.eof-marker-in-text", "superseded.plain",
       "A page that mentions the %%EOF marker in its text, uncompressed — it must not be "
       "mistaken for the end of a revision.")
def eof_in_text(path: Path) -> None:
    doc = fitz.open()
    body(doc.new_page(), lines=[*FILLER, "A PDF file ends with the marker %%EOF on its own line."])
    save(doc, path)


@clean("revision.form-refilled", "superseded.plain",
       "A form field changed from Draft to Final in an incremental save.")
def form_refill(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    widget = fitz.Widget(); widget.field_name = "status"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = fitz.Rect(72, 300, 300, 320); widget.field_value = "Draft"
    page.add_widget(widget); save(doc, path)

    def refill(d: fitz.Document) -> None:
        for w in d[0].widgets():
            w.field_value = "Final"; w.update()
    update(path, refill)


@clean("revision.encrypted-annotated", "superseded.plain",
       "Encrypted with an owner password only, then annotated incrementally.")
def encrypted(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner",
             permissions=fitz.PDF_PERM_PRINT, no_new_id=True)
    doc.close()
    update(path, lambda d: d[0].add_text_annot((300, 300), "ok"))


@clean("revision.large-images-eight-saves", "superseded.pixels",
       "Three pages of large flat images, annotated in eight incremental saves.")
def big_images(path: Path) -> None:
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(); body(page)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1500, 1500), False)
        pix.set_rect(pix.irect, (200, 180, 160))
        page.insert_image(fitz.Rect(72, 200, 500, 628), pixmap=pix)
    save(doc, path)
    for i in range(8):
        update(path, lambda d, i=i: d[0].add_text_annot((100 + 10 * i, 700), f"note {i}"))  # type: ignore[misc]


@clean("revision.fifty-five-saves", "superseded.plain.revision-cap",
       "Fifty-five incremental saves — more than the 50 earlier revisions scanned.",
       expected=expect(2, warnings=(("REVISION_CAP", "superseded"),)))
def many_revisions(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    for i in range(55):
        update(path, lambda d, i=i: d[0].add_text_annot((60 + 8 * (i % 60), 700), f"n{i}"))  # type: ignore[misc]
