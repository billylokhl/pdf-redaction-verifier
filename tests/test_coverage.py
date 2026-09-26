"""Formerly silent misses: content the tool found but could not read.

Each test here is a leak that used to certify clean (exit 0). The fix is
one of two kinds: the tool now *reads* the content (earlier revisions,
off-page text, orphaned XMP and attachment text), or — where reading it
needs real decoding work — it *flags* it as a manual-review warning
(exit 2) instead of passing it silently. Controls check the flags do not
fire on ordinary clean content.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import fitz

import verify

from .conftest import SSN

NORM = verify.normalize_string(SSN)


def _scan_objects(path: Path, *, revisions: bool = False) -> verify.ScanReport:
    report = verify.ScanReport()
    matcher = verify.SecretMatcher([verify.Secret("SSN", NORM)])
    patterns = [verify.PatternRule(
        "ssn", re.compile(verify.BUILTIN_PATTERN_CLASSES["ssn"][0]), verify._valid_ssn)]
    doc = fitz.open(path)
    try:
        verify.scan_pdf_objects(doc, matcher, patterns, report)
        if revisions:
            verify.scan_earlier_revisions(path, doc, matcher, patterns, report)
    finally:
        doc.close()
    return report


def _orphan(path: Path, body: bytes, dictionary: str) -> None:
    """A clean page plus one stream object that nothing references."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "clean visible page")
    xref = doc.get_new_xref()
    doc.update_object(xref, dictionary)
    doc.update_stream(xref, body)
    doc.save(str(path))                  # no garbage collection: kept
    doc.close()


def _zip_with(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("note.txt", text)
    return buf.getvalue()


class TestNowRead:
    def test_object_overwritten_by_an_incremental_update(self, tmp_path) -> None:
        # The redaction rewrote the content stream under the same object
        # number and saved incrementally: the original revision still holds
        # the SSN, but PyMuPDF and qpdf read only the newest.
        path = tmp_path / "incremental.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"SSN {SSN}")
        doc.save(str(path))
        doc.close()
        doc = fitz.open(path)
        doc.update_stream(doc[0].get_contents()[0],
                          b"BT /helv 12 Tf 72 720 Td (clean) Tj ET")
        doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()

        assert _scan_objects(path).findings == []            # current file: clean
        report = _scan_objects(path, revisions=True)
        assert {f.secret_name for f in report.findings} == {"SSN", "ssn"}
        assert all(f.location.startswith("earlier revision 1, object")
                   for f in report.findings)

    def test_unchanged_revisions_add_nothing(self, tmp_path) -> None:
        path = tmp_path / "touched.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean text")
        doc.save(str(path))
        doc.close()
        doc = fitz.open(path)
        doc.set_metadata({"keywords": "revised"})
        doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()
        report = _scan_objects(path, revisions=True)
        assert report.findings == [] and report.warnings == []

    def test_text_placed_outside_the_page(self) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "visible")
        page.insert_text((72, -40), f"SSN {SSN}")           # above the page
        try:
            readings = verify.extract_visual_text(page)
        finally:
            doc.close()
        assert NORM in verify.normalize_string(readings[0])

    def test_orphaned_xmp_packet(self, tmp_path) -> None:
        path = tmp_path / "xmp.pdf"
        xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc:description>SSN {SSN}'
               "</dc:description></x:xmpmeta>").encode()
        _orphan(path, xmp, "<< /Type /Metadata /Subtype /XML >>")
        report = _scan_objects(path)
        assert any(f.secret_name == "SSN" and "(XMP metadata)" in f.location
                   for f in report.findings)

    def test_orphaned_attachment_text(self, tmp_path) -> None:
        path = tmp_path / "attach.pdf"
        _orphan(path, f"Employee SSN {SSN}".encode(), "<< /Type /EmbeddedFile >>")
        report = _scan_objects(path)
        assert report.findings == []                         # arbitrary text: not hard
        assert any("(attachment) contains a sequence matching 'SSN'" in w
                   for w in report.warnings)


class TestNowFlagged:
    def test_orphaned_text_in_an_identity_h_font(self, tmp_path) -> None:
        # CJK text is embedded with Identity-H encoding: the content stream
        # holds glyph numbers, so decoding its strings cannot find anything.
        path = tmp_path / "cid.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_font(fontname="F0", fontbuffer=fitz.Font("cjk").buffer)
        page.insert_text((72, 72), f"SSN {SSN}", fontname="F0")
        old = page.get_contents()[0]
        assert re.search(rb"<[0-9a-fA-F]{8,}>", doc.xref_stream(old))  # glyph IDs
        replacement = doc.get_new_xref()
        doc.update_object(replacement, "<< >>")
        doc.update_stream(replacement, b"BT /F0 12 Tf 72 720 Td ET")
        doc.xref_set_key(page.xref, "Contents", f"{replacement} 0 R")  # orphans old
        doc.save(str(path))
        doc.close()
        report = _scan_objects(path)
        assert report.findings == []
        assert any("draw text this tool cannot decode" in w and f"object {old}" in w
                   for w in report.warnings)

    def test_orphaned_content_dominated_by_an_inline_image(self, tmp_path) -> None:
        path = tmp_path / "inline.pdf"
        body = (f"BT /helv 12 Tf 72 700 Td (SSN {SSN}) Tj ET\n"
                "BI /W 20 /H 20 /BPC 8 /CS /G ID ").encode() + bytes(range(256)) * 2 + b"\nEI"
        _orphan(path, body, "<< >>")
        report = _scan_objects(path)
        assert any("draw text this tool cannot decode" in w for w in report.warnings)

    def test_orphaned_binary_attachment(self, tmp_path) -> None:
        path = tmp_path / "zip.pdf"
        _orphan(path, _zip_with(f"SSN {SSN}"), "<< /Type /EmbeddedFile >>")
        report = _scan_objects(path)
        assert any("payload(s) this tool cannot read as text" in w
                   for w in report.warnings)

    def test_listed_binary_attachment(self) -> None:
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.embfile_add("records.zip", _zip_with(f"SSN {SSN}"))
        report = verify.ScanReport()
        try:
            verify.scan_hidden_objects(
                doc, verify.SecretMatcher([verify.Secret("SSN", NORM)]), [], report)
        finally:
            doc.close()
        assert any("is not text" in w and "NOT scanned" in w for w in report.warnings)


    def test_leftover_image(self, tmp_path) -> None:
        # e.g. the original scan a redaction replaced: its pixels can hold
        # the secret, and stored images are not OCR'd.
        path = tmp_path / "image.pdf"
        tmp = fitz.open()
        tmp.new_page(width=300, height=60).insert_text((10, 40), f"SSN {SSN}")
        png = tmp[0].get_pixmap().tobytes("png")
        tmp.close()
        doc = fitz.open()
        page = doc.new_page()
        page.insert_image(fitz.Rect(50, 50, 350, 110), stream=png)
        image = page.get_images()[0][0]
        page.set_contents(page.get_contents()[0])      # keep content, then
        doc.xref_set_key(page.xref, "Resources", "<< >>")   # drop the image ref
        doc.save(str(path))
        doc.close()
        report = _scan_objects(path)
        assert any("image(s) this tool does not read" in w and f"object {image}" in w
                   for w in report.warnings)


class TestNoFalseFlags:
    def test_plain_orphan_is_read_not_flagged(self, tmp_path) -> None:
        path = tmp_path / "plain.pdf"
        _orphan(path, b"BT /helv 12 Tf 72 700 Td (Nothing sensitive) Tj ET", "<< >>")
        report = _scan_objects(path)
        assert report.findings == [] and report.warnings == []

    def test_non_english_text_attachment_is_text(self) -> None:
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.embfile_add("notes.txt", "Café résumé — 名前 und Grüße".encode())
        report = verify.ScanReport()
        try:
            verify.scan_hidden_objects(
                doc, verify.SecretMatcher([verify.Secret("SSN", NORM)]), [], report)
        finally:
            doc.close()
        assert report.warnings == []
