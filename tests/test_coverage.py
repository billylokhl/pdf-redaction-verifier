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

from .conftest import SSN, requires_qpdf

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
        assert NORM in verify.normalize_string(readings[3])   # off-page reading
        assert NORM not in verify.normalize_string(readings[0])

    def test_text_outside_the_cropbox(self) -> None:
        # Cropping is a classic fake redaction: the text is still there.
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "visible")
        page.insert_text((72, 500), f"SSN {SSN}")
        page.set_cropbox(fitz.Rect(0, 0, 300, 300))
        try:
            readings = verify.extract_visual_text(page)
        finally:
            doc.close()
        assert NORM in verify.normalize_string(readings[3])

    def test_off_page_text_does_not_hide_a_page_break_split(self) -> None:
        # Regression: merged into the visible reading, a slug below page 1
        # became its last line and hid a value split across the break.
        doc = fitz.open()
        p1 = doc.new_page()
        p1.insert_text((72, 780), "Employee SSN 123-45-")
        p1.insert_text((72, 900), "JOB-4471 slug")          # below the page
        doc.new_page().insert_text((72, 60), "6789 was on file.")
        report = verify.ScanReport()
        try:
            verify.scan_page_layer(
                doc, verify.SecretMatcher([verify.Secret("SSN", NORM)]), report,
                layer="Text", extractor=verify.extract_visual_text,
                note="visual text layer", patterns=[],
                hard_variants=verify.TEXT_GENUINE_READINGS)
        finally:
            doc.close()
        assert [f.location for f in report.findings] == [
            "across page boundaries, pages 1–2 (visual text layer)"]

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


# ── Adversarial-review regressions for this layer ──────────────────────────

def _png(text: str, width: int = 300, height: int = 60) -> bytes:
    tmp = fitz.open()
    tmp.new_page(width=width, height=height).insert_text((4, height * 0.7), text,
                                                         fontsize=height * 0.5)
    data = tmp[0].get_pixmap().tobytes("png")
    tmp.close()
    return data


def _two_revisions(path: Path, first, second) -> None:
    """Save a document built by *first*, then apply *second* incrementally."""
    doc = fitz.open()
    first(doc)
    doc.save(str(path))
    doc.close()
    doc = fitz.open(path)
    second(doc)
    doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()


def _in_place_rewrite(path: Path) -> None:
    """The canonical in-place redaction: same object, same length, digits
    replaced — uncompressed, so the dictionary (and /Length) is unchanged."""
    def first(doc):
        page = doc.new_page()
        page.insert_text((72, 72), "cover")
        doc.update_stream(page.get_contents()[0],
                          f"BT /helv 12 Tf 72 700 Td (SSN {SSN}) Tj ET".encode(),
                          compress=False)

    def second(doc):
        doc.update_stream(doc[0].get_contents()[0],
                          b"BT /helv 12 Tf 72 700 Td (SSN XXX-XX-XXXX) Tj ET",
                          compress=False)
    _two_revisions(path, first, second)


class TestRevisions:
    def test_same_length_in_place_rewrite(self, tmp_path) -> None:
        path = tmp_path / "inplace.pdf"
        _in_place_rewrite(path)
        doc = fitz.open(path)
        xref = doc[0].get_contents()[0]
        doc.close()
        report = _scan_objects(path, revisions=True)
        assert {f.secret_name for f in report.findings} == {"SSN", "ssn"}
        assert {f.location for f in report.findings} == {
            f"earlier revision 1, object {xref}"}

    def test_cli_reports_the_earlier_revision(self, tmp_path) -> None:
        # The revision scan must be wired into the CLI, not only callable.
        from .conftest import run_verify
        path = tmp_path / "inplace.pdf"
        _in_place_rewrite(path)
        rules = tmp_path / "rules.json"
        rules.write_text(f'[{{"name": "SSN", "value": "{SSN}"}}]')
        result = run_verify(path, rules)
        assert result.returncode == 1, result.stdout
        assert "earlier revision 1" in result.stdout

    def test_unchanged_objects_are_never_superseded(self, tmp_path) -> None:
        def first(doc):
            page = doc.new_page()
            page.insert_text((72, 72), f"SSN {SSN}")
            page.insert_image(fitz.Rect(72, 100, 372, 160), stream=_png("logo"))

        path = tmp_path / "touched.pdf"
        _two_revisions(path, first, lambda doc: doc.set_metadata({"keywords": "x"}))
        report = _scan_objects(path, revisions=True)
        assert not any(f.location.startswith("earlier revision") for f in report.findings)
        assert not any("superseded" in w for w in report.warnings)

    def test_superseded_identity_h_text_is_flagged(self, tmp_path) -> None:
        def first(doc):
            page = doc.new_page()
            page.insert_font(fontname="F0", fontbuffer=fitz.Font("cjk").buffer)
            page.insert_text((72, 72), f"SSN {SSN}", fontname="F0")

        def second(doc):
            doc.update_stream(doc[0].get_contents()[0], b"BT /F0 12 Tf ET")

        path = tmp_path / "cid_rev.pdf"
        _two_revisions(path, first, second)
        report = _scan_objects(path, revisions=True)
        assert any("superseded object(s) draw text this tool cannot decode" in w
                   for w in report.warnings)

    def test_superseded_image_is_flagged(self, tmp_path) -> None:
        def first(doc):
            doc.new_page().insert_image(fitz.Rect(72, 72, 372, 132),
                                        stream=_png(f"SSN {SSN}"))

        def second(doc):
            doc[0].replace_image(doc[0].get_images()[0][0], stream=_png("clean"))

        path = tmp_path / "img_rev.pdf"
        _two_revisions(path, first, second)
        report = _scan_objects(path, revisions=True)
        assert any("superseded image(s)" in w for w in report.warnings)

    def test_revision_without_eof_marker_is_still_found(self, tmp_path) -> None:
        path = tmp_path / "noeof.pdf"
        _in_place_rewrite(path)
        raw = path.read_bytes()
        first_eof = raw.index(b"%%EOF")
        # Blank rev 1's marker in place: offsets must not move.
        path.write_bytes(raw[:first_eof] + b"     " + raw[first_eof + 5:])
        assert {f.secret_name for f in _scan_objects(path, revisions=True).findings} \
            == {"SSN", "ssn"}

    def test_eof_marker_inside_a_stream_is_not_a_revision(self, tmp_path) -> None:
        path = tmp_path / "eoftext.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "cover")
        doc.update_stream(page.get_contents()[0],
                          b"BT /helv 12 Tf 72 700 Td (the %%EOF marker ends a file) Tj ET",
                          compress=False)
        doc.save(str(path))
        doc.close()
        assert path.read_bytes().count(b"%%EOF") == 2           # fixture guard
        report = _scan_objects(path, revisions=True)
        assert report.findings == [] and report.warnings == []

    def test_uncompressed_attached_pdf_is_not_a_revision(self, tmp_path) -> None:
        inner = fitz.open()
        inner.new_page().insert_text((72, 72), f"SSN {SSN}")
        inner_bytes = inner.tobytes()
        inner.close()
        path = tmp_path / "attached.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.embfile_add("inner.pdf", inner_bytes)
        doc.save(str(path), deflate=False, expand=255)
        doc.close()
        report = _scan_objects(path, revisions=True)
        assert not any(f.location.startswith("earlier revision") for f in report.findings)
        assert not any("superseded" in w for w in report.warnings)

    def test_revision_cap_keeps_the_original(self, tmp_path) -> None:
        path = tmp_path / "many.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.set_metadata({"title": f"Case {SSN}"})
        doc.save(str(path))
        doc.close()
        for i in range(verify.MAX_EARLIER_REVISIONS + 3):
            doc = fitz.open(path)
            doc.set_metadata({"title": f"Revision {i}"})
            doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            doc.close()
        report = _scan_objects(path, revisions=True)
        assert any(f.location.startswith("earlier revision 1,") for f in report.findings)
        assert any("only the original and the latest" in w for w in report.warnings)

    def test_revision_dates_are_not_card_numbers(self, tmp_path) -> None:
        # Each save leaves a PDF timestamp; its digits used to pass the
        # card checksum about one time in ten, a false exit 1 per save.
        path = tmp_path / "dates.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.save(str(path))
        doc.close()
        # Each of these dates' digits passes the card checksum.
        card_like = ["D:20240102090702-07'00'", "D:20240102094902-07'00'",
                     "D:20240103092103-07'00'", "D:20240105093505-07'00'"]
        assert all(verify.match_patterns(d, [verify.PatternRule("cc", re.compile(
            verify.BUILTIN_PATTERN_CLASSES["credit-card"][0]), verify._valid_card)])
            for d in card_like)                              # fixture guard
        for date in card_like:
            doc = fitz.open(path)
            doc.set_metadata({"modDate": date})
            doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            doc.close()
        report = verify.ScanReport()
        cc = [verify.PatternRule("cc", re.compile(
            verify.BUILTIN_PATTERN_CLASSES["credit-card"][0]), verify._valid_card)]
        doc = fitz.open(path)
        try:
            verify.scan_pdf_objects(doc, verify.SecretMatcher([]), cc, report)
            verify.scan_earlier_revisions(path, doc, verify.SecretMatcher([]), cc, report)
        finally:
            doc.close()
        assert report.findings == []


class TestLinearized:
    @requires_qpdf
    def test_linearized_file_with_an_incremental_edit(self, tmp_path) -> None:
        # A linearized file's first-page xref section is part of revision
        # 1, not an earlier revision; cutting there opened only by repair
        # and flagged clean files as unreadable.
        import shutil
        import subprocess
        plain = tmp_path / "plain.pdf"
        doc = fitz.open()
        for i in range(3):
            doc.new_page().insert_text((72, 72), f"clean page {i}")
        doc.save(str(plain))
        doc.close()
        path = tmp_path / "linear.pdf"
        subprocess.run([shutil.which("qpdf"), "--linearize", str(plain), str(path)],
                       check=True)
        doc = fitz.open(path)
        doc[0].add_text_annot((100, 100), "reviewed")
        doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()
        report = _scan_objects(path, revisions=True)
        assert report.findings == [] and report.warnings == []


class TestLeftoverChecks:
    def test_mixed_font_stream_is_flagged(self, tmp_path) -> None:
        # Plain text around a glyph-coded secret must not dilute the check.
        body = ("BT /helv 12 Tf 72 700 Td " + "(Ordinary visible text line) Tj " * 12
                + "/F0 12 Tf <00360036003100030039003500320014> Tj ET").encode()
        path = tmp_path / "mixed.pdf"
        _orphan(path, body, "<< >>")
        assert any("draw text this tool cannot decode" in w
                   for w in _scan_objects(path).warnings)

    def test_hex_string_shown_with_quote_operator_is_flagged(self, tmp_path) -> None:
        path = tmp_path / "quote.pdf"
        _orphan(path, b"BT /F1 12 Tf 14 TL 72 720 Td <0036003600310039> ' ET", "<< >>")
        assert any("draw text this tool cannot decode" in w
                   for w in _scan_objects(path).warnings)

    def test_leftover_inline_image_is_flagged(self, tmp_path) -> None:
        pixels = bytes(range(256)) * 40
        body = b"q 300 0 0 40 72 700 cm BI /W 300 /H 40 /BPC 8 /CS /G ID " + pixels + b"\nEI Q"
        path = tmp_path / "inline.pdf"
        _orphan(path, body, "<< >>")
        assert any("image(s) this tool does not read" in w
                   for w in _scan_objects(path).warnings)

    def test_untyped_leftover_attachment_text_is_read(self, tmp_path) -> None:
        path = tmp_path / "untyped.pdf"
        _orphan(path, f"Employee record\nSSN {SSN}\n".encode(), "<< >>")
        assert any("(raw text) contains a sequence matching 'SSN'" in w
                   for w in _scan_objects(path).warnings)

    def test_untyped_leftover_container_is_flagged(self, tmp_path) -> None:
        path = tmp_path / "untyped_zip.pdf"
        _orphan(path, _zip_with(f"SSN {SSN}"), "<< >>")
        assert any("payload(s) this tool cannot read as text" in w
                   for w in _scan_objects(path).warnings)

    def test_random_binary_is_not_flagged_as_text(self, tmp_path) -> None:
        import random
        rng = random.Random(7)
        blob = bytes(rng.randrange(256) for _ in range(300_000))
        path = tmp_path / "blob.pdf"
        _orphan(path, blob, "<< /N 4 /Alternate /DeviceCMYK >>")
        assert not any("draw text" in w for w in _scan_objects(path).warnings)

    def test_leftover_xmp_patterns_are_manual_review(self, tmp_path) -> None:
        # XMP IDs carry digit runs the ssn class accepts by coincidence.
        xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><xmpMM:InstanceID>'
               b"uuid:9f3c-123456789-ab</xmpMM:InstanceID></x:xmpmeta>")
        path = tmp_path / "uuid.pdf"
        _orphan(path, xmp, "<< /Type /Metadata /Subtype /XML >>")
        report = verify.ScanReport()
        doc = fitz.open(path)
        try:
            verify.scan_pdf_objects(doc, verify.SecretMatcher([]), [verify.PatternRule(
                "ssn", re.compile(verify.BUILTIN_PATTERN_CLASSES["ssn"][0]),
                verify._valid_ssn)], report)
        finally:
            doc.close()
        assert report.findings == []
        assert any("(XMP metadata) contains a sequence matching 'ssn'" in w
                   for w in report.warnings)

    def test_leftover_payload_is_read_with_a_size_cap(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(verify, "MAX_ATTACHMENT_BYTES", 1000)
        path = tmp_path / "big.pdf"
        _orphan(path, b"x" * 50_000 + f" SSN {SSN}".encode(), "<< /Type /EmbeddedFile >>")
        warnings = _scan_objects(path).warnings
        assert any("only the start was scanned" in w for w in warnings)
        assert not any("contains a sequence matching" in w for w in warnings)

    def test_dangling_reference_does_not_disable_orphan_checks(self, tmp_path) -> None:
        # A reference past the end of the table is null, per the spec; it
        # used to mark the walk untrusted and switch every orphan check off.
        path = tmp_path / "dangling.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_font(fontname="F0", fontbuffer=fitz.Font("cjk").buffer)
        page.insert_text((72, 72), f"SSN {SSN}", fontname="F0")
        replacement = doc.get_new_xref()
        doc.update_object(replacement, "<< >>")
        doc.update_stream(replacement, b"BT ET")
        doc.xref_set_key(page.xref, "Contents", f"{replacement} 0 R")
        doc.xref_set_key(doc.pdf_catalog(), "PieceInfo", "9999 0 R")
        doc.save(str(path))
        doc.close()
        assert any("draw text this tool cannot decode" in w
                   for w in _scan_objects(path).warnings)

    def test_image_size_gate(self, tmp_path) -> None:
        for width, height, flagged in [(8, 8, False), (31, 31, False), (110, 15, True),
                                       (200, 20, True), (16, 32, True)]:
            path = tmp_path / f"img_{width}x{height}.pdf"
            body = bytes(width * height)
            _orphan(path, body, f"<< /Subtype /Image /Width {width} /Height {height} "
                                "/BitsPerComponent 8 /ColorSpace /DeviceGray >>")
            got = any("image(s)" in w for w in _scan_objects(path).warnings)
            assert got == flagged, (width, height)


class TestLiveContentIsNeverLeftover:
    def test_live_content_raises_no_leftover_flags(self, tmp_path) -> None:
        path = tmp_path / "live.pdf"
        doc = fitz.open()
        p1 = doc.new_page()
        p1.insert_font(fontname="F0", fontbuffer=fitz.Font("cjk").buffer)
        p1.insert_text((72, 72), "名前 and ordinary text", fontname="F0")
        p1.insert_image(fitz.Rect(72, 100, 372, 160), stream=_png("a picture"))
        p2 = doc.new_page()
        p2.insert_text((72, 72), "caption")              # creates its content
        pixels = bytes(range(256)) * 40
        doc.update_stream(p2.get_contents()[0],
                          b"BT /helv 12 Tf 72 700 Td (caption) Tj ET q 300 0 0 40 72 600 cm "
                          b"BI /W 300 /H 40 /BPC 8 /CS /G ID " + pixels + b"\nEI Q")
        doc.embfile_add("archive.zip", _zip_with("nothing"))
        doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
        doc.save(str(path))
        doc.close()
        report = _scan_objects(path, revisions=True)
        assert report.warnings == []


class TestAttachmentFlags:
    def _hidden(self, doc) -> verify.ScanReport:
        report = verify.ScanReport()
        try:
            verify.scan_hidden_objects(
                doc, verify.SecretMatcher([verify.Secret("SSN", NORM)]), [], report)
        finally:
            doc.close()
        return report

    def test_file_attached_to_an_annotation(self) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "clean")
        page.add_file_annot(fitz.Point(100, 100), _zip_with(f"SSN {SSN}"), "records.zip")
        assert any("is not text" in w for w in self._hidden(doc).warnings)

    def test_nested_pdf_is_a_container(self) -> None:
        inner = fitz.open()
        inner.new_page().insert_text((72, 72), f"SSN {SSN}")
        data = inner.tobytes(deflate=True)
        inner.close()
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.embfile_add("receipt.pdf", data)
        assert any("is not text" in w for w in self._hidden(doc).warnings)

    def test_utf16_and_long_cjk_text_are_text(self) -> None:
        for name, data in [("utf16.txt", "notes 名前".encode("utf-16")),
                           ("cjk.txt", ("名前" * 11_000).encode())]:
            if name == "cjk.txt":
                try:
                    data[:65536].decode("utf-8")
                    raise AssertionError("fixture must split a character at 64 KB")
                except UnicodeDecodeError:
                    pass
            doc = fitz.open()
            doc.new_page().insert_text((72, 72), "clean")
            doc.embfile_add(name, data)
            assert self._hidden(doc).warnings == [], name


class TestReadableCodes:
    def test_word_style_glyph_ids_are_unreadable(self) -> None:
        assert verify._shows_unreadable_text(
            "BT /F1 12 Tf <0036005200460044004F0003> Tj ET")

    def test_smart_quotes_and_accents_are_readable(self) -> None:
        for body in ("BT /F1 12 Tf (\\223Quoted\\224 text \\226 dash) Tj ET",
                     "BT /F1 12 Tf (Caf\\351 r\\351sum\\351 na\\357ve) Tj ET"):
            assert not verify._shows_unreadable_text(body), body
