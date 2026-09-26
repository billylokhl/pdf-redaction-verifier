"""Clean documents: nothing sensitive anywhere. They measure false
alarms — a clean document must exit 0 unless it holds something the tool
honestly cannot read."""

from __future__ import annotations

import subprocess
from pathlib import Path

import fitz

from ..model import case, expect
from ..pdfkit import (FILLER, body, cjk_font, embedded_font, png_of, save,
                      update, zipbytes)

CLEAN = expect(0)


@case("clean.plain", cells="live.plain", expected=CLEAN,
      story="A one-page memo in a standard font.")
def plain(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)


@case("clean.multipage-compact", cells="live.plain", expected=CLEAN,
      story="Three pages, saved compacted and compressed.")
def multipage(path: Path) -> None:
    doc = fitz.open()
    for i in range(3):
        body(doc.new_page(), lines=[*FILLER, f"Page {i + 1} of 3"])
    save(doc, path, garbage=4, deflate=True)


@case("clean.embedded-font", cells="live.font", expected=CLEAN,
      story="A memo in an embedded font, text stored as glyph codes.")
def embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, embedded_font(page)); save(doc, path)


@case("clean.cjk", cells="live.font", expected=CLEAN,
      story="A memo with a line in Chinese, embedded CJK font.")
def cjk(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page()
    body(page, cjk_font(page), lines=[*FILLER, "季度报告"]); save(doc, path)


@case("clean.live-image", cells="live.pixels", expected=CLEAN, requires=("ocr",),
      story="A memo with a picture containing harmless text.")
def live_image(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_image(fitz.Rect(72, 200, 372, 260), stream=png_of("Harmless picture text"))
    save(doc, path)


@case("clean.incremental-annotation", cells="superseded.plain", expected=CLEAN,
      story="A reviewer added a sticky note in an incremental save.")
def incr_annot(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    update(path, lambda d: d[0].add_text_annot((300, 300), "Reviewed, nothing to add"))


@case("clean.incremental-edit-embedded-font", cells="superseded.font", expected=CLEAN,
      story="An approval line added in an incremental save, embedded font.")
def incr_edit_embedded(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, embedded_font(page)); save(doc, path)

    def edit(d: fitz.Document) -> None:
        page = d[0]
        page.insert_text((72, 400), "Approved for circulation", fontname=embedded_font(page))
    update(path, edit)


@case("clean.incremental-edit", cells="superseded.plain", expected=CLEAN,
      story="An approval line added in an incremental save.")
def incr_edit(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    update(path, lambda d: d[0].insert_text((72, 400), "Approved for circulation"))


@case("clean.linearized", cells="live.font", expected=CLEAN, writer="qpdf",
      requires=("qpdf",), story="A two-page memo linearized for fast web view.")
def linearized(path: Path) -> None:
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(); body(page, embedded_font(page))
    tmp = path.with_suffix(".tmp.pdf"); save(doc, tmp)
    subprocess.run(["qpdf", "--deterministic-id", "--linearize", str(tmp), str(path)],
                   check=True)
    tmp.unlink()


@case("clean.object-streams", cells="live.font", expected=CLEAN,
      story="A memo saved with object streams, as Acrobat and Chrome do.")
def object_streams(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page, embedded_font(page))
    doc.set_metadata({"title": "Ops summary", "author": "Facilities"})
    save(doc, path, garbage=4, deflate=True, use_objstms=1)


@case("clean.slug-outside-crop", cells="off-page.plain", expected=CLEAN,
      story="A printer's slug line outside the crop box.")
def slug(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(width=612, height=792); body(page)
    page.insert_text((72, 780), "SLUG: job 4471 proof 2 printed on press 3")
    page.set_cropbox(fitz.Rect(0, 0, 612, 700)); save(doc, path)


@case("clean.text-attachment", cells="attachment.plain", expected=CLEAN,
      story="A memo with plain-text notes attached.")
def text_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("notes.txt", b"Plain notes, nothing sensitive\n"); save(doc, path)


@case("clean.zip-attachment", cells="attachment.container",
      expected=expect(2, warnings=("ATTACHMENT_NOT_TEXT",)),
      story="A memo with a harmless zip attached — flagged, since zips are not unpacked.")
def zip_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("bundle.zip", zipbytes("nothing sensitive here")); save(doc, path)


@case("clean.png-attachment", cells="attachment.pixels",
      expected=expect(2, warnings=("ATTACHMENT_NOT_TEXT",)),
      story="A memo with a logo attached — flagged, since attached images are not OCR'd.")
def png_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("logo.png", png_of("ACME")); save(doc, path)


@case("clean.utf8-attachment", cells="attachment.plain", expected=CLEAN,
      story="Notes in Japanese and accented Latin, attached as UTF-8.")
def utf8_attachment(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.embfile_add("notes_ja.txt", "会議のメモ。機密情報なし。Café résumé\n".encode())
    save(doc, path)


@case("clean.form", cells="annot-fields.plain", expected=CLEAN,
      story="A form with a harmless name filled in.")
def form(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    widget = fitz.Widget(); widget.field_name = "name"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.rect = fitz.Rect(72, 300, 300, 320); widget.field_value = "Jane Public"
    page.add_widget(widget); save(doc, path)


@case("clean.rotated-text", cells="live.plain", expected=CLEAN,
      story="A memo with a rotated margin note.")
def rotated(path: Path) -> None:
    doc = fitz.open(); page = doc.new_page(); body(page)
    page.insert_text((500, 600), "Internal distribution only", rotate=90); save(doc, path)


@case("clean.orphaned-small-icon", cells="orphaned.pixels-small", expected=CLEAN,
      story="A deleted page left a 12-pixel icon behind — too small to hold text.")
def small_icon(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    page = doc.new_page(); body(page)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 12, 12), False)
    pix.set_rect(pix.irect, (0, 0, 0))
    page.insert_image(fitz.Rect(72, 300, 84, 312), pixmap=pix)
    doc.delete_page(1); save(doc, path)


@case("clean.redacted-and-compacted", cells="orphaned.plain", expected=CLEAN,
      story="An SSN redacted properly: removed, then saved with garbage collection.",
      mistake="None — this is the correct procedure.")
def redacted_compacted(path: Path) -> None:
    from ..model import SSN
    from ..pdfkit import redact
    doc = fitz.open(); body(doc.new_page(), lines=[*FILLER, f"SSN {SSN}"])
    redact(doc, SSN); save(doc, path, garbage=4, deflate=True)


@case("clean.ten-revisions", cells="superseded.plain", expected=CLEAN,
      story="Ten rounds of review notes, each an incremental save.")
def ten_revisions(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    for i in range(10):
        update(path, lambda d, i=i: d[0].add_text_annot((100 + 10 * i, 500), f"note {i}"))


@case("clean.eof-marker-in-text", cells="superseded.plain", expected=CLEAN,
      story="A page that mentions the %%EOF marker in its text, uncompressed.")
def eof_in_text(path: Path) -> None:
    doc = fitz.open()
    body(doc.new_page(), lines=[*FILLER, "A PDF file ends with the marker %%EOF on its own line."])
    save(doc, path)


@case("clean.form-refilled", cells="superseded.plain", expected=CLEAN,
      story="A form field changed from Draft to Final in an incremental save.")
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


@case("clean.encrypted-owner-password", cells="superseded.plain", expected=CLEAN,
      story="Encrypted with an owner password only, then annotated incrementally.")
def encrypted(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page())
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner",
             permissions=fitz.PDF_PERM_PRINT, no_new_id=True)
    doc.close()
    update(path, lambda d: d[0].add_text_annot((300, 300), "ok"))


@case("clean.large-images-eight-revisions", cells="superseded.pixels", expected=CLEAN,
      story="Three pages of large flat images, annotated in eight incremental saves.")
def big_images(path: Path) -> None:
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(); body(page)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1500, 1500), False)
        pix.set_rect(pix.irect, (200, 180, 160))
        page.insert_image(fitz.Rect(72, 200, 500, 628), pixmap=pix)
    save(doc, path)
    for i in range(8):
        update(path, lambda d, i=i: d[0].add_text_annot((100 + 10 * i, 700), f"note {i}"))


@case("clean.attached-pdf-uncompressed", cells="attachment.container",
      expected=expect(2, warnings=("ATTACHMENT_NOT_TEXT",)),
      story="A clean PDF attached, stored uncompressed — flagged, since attached PDFs are not opened.")
def attached_pdf(path: Path) -> None:
    inner = fitz.open(); page = inner.new_page(); body(page, embedded_font(page))
    data = inner.tobytes(no_new_id=True); inner.close()
    doc = fitz.open(); body(doc.new_page()); doc.embfile_add("inner.pdf", data)
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Type")[1] == "/EmbeddedFile":
            doc.update_stream(xref, data, compress=False)
    save(doc, path)


@case("clean.fifty-five-revisions", cells="superseded.revision-cap",
      expected=expect(2, warnings=("REVISION_CAP",)),
      story="Fifty-five incremental saves — more than the 50 earlier revisions scanned.")
def many_revisions(path: Path) -> None:
    doc = fitz.open(); body(doc.new_page()); save(doc, path)
    for i in range(55):
        update(path, lambda d, i=i: d[0].add_text_annot((60 + 8 * (i % 60), 700), f"n{i}"))
