"""Tests for the structural object-walk pass.

The Binary layer used to find PDF string literals by running a
PDF-syntax regex over qpdf's whole byte stream, which interleaves
structure with image data. A "(" byte inside a JPEG stalled the parser
exactly as a real unclosed string would; the 64KB carry cap and its
warning existed to contain that. Walking objects removes the category
error instead of compensating for it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import fitz
import pytest

import verify

from .conftest import run_verify

SSN = "123-45-6789"


@pytest.fixture
def ssn_rules(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([
        {"name": "Target SSN", "value": SSN},
        {"name": "Any SSN", "class": "ssn"},
    ]))
    return path


def _scan(path: Path):
    """Run only the structural pass, so nothing else can mask it."""
    doc = fitz.open(path)
    report = verify.ScanReport()
    secrets = [verify.Secret("Target SSN", verify.normalize_string(SSN))]
    patterns = [verify.PatternRule(
        "Any SSN", re.compile(verify.BUILTIN_PATTERN_CLASSES["ssn"][0]),
        verify._valid_ssn)]
    try:
        verify.scan_pdf_objects(doc, verify.SecretMatcher(secrets), patterns, report)
    finally:
        doc.close()
    return report


def _jpeg_bytes(text: str = "x" * 40) -> bytes:
    tmp = fitz.open()
    page = tmp.new_page(width=400, height=400)
    page.insert_text((20, 200), text, fontsize=30)
    data = page.get_pixmap(dpi=150).tobytes("jpg")
    tmp.close()
    return data


class TestStructuralScan:
    def test_finds_a_literal_in_a_content_stream(self, tmp_path) -> None:
        path = tmp_path / "plain.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"Applicant SSN: {SSN}")
        doc.save(path)
        doc.close()
        names = {f.secret_name for f in _scan(path).findings}
        assert names == {"Target SSN", "Any SSN"}

    def test_finds_a_literal_in_an_object_dictionary(self, tmp_path) -> None:
        # Info-dictionary strings live in the dict, not a stream.
        path = tmp_path / "meta.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean body")
        doc.set_metadata({"subject": f"case {SSN}"})
        doc.save(path)
        doc.close()
        assert {f.secret_name for f in _scan(path).findings} >= {"Target SSN"}

    def test_image_bytes_never_stall_the_parser(self, tmp_path) -> None:
        # The whole point: a JPEG holds "(" bytes roughly 1 in 256, which
        # used to stall the literal scanner and force a carry warning.
        path = tmp_path / "img.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"Applicant SSN: {SSN}")
        page.insert_image(fitz.Rect(50, 300, 400, 600), stream=_jpeg_bytes())
        doc.save(path)
        doc.close()
        report = _scan(path)
        assert {f.secret_name for f in report.findings} >= {"Target SSN"}
        assert not any("carry" in w for w in report.warnings)

    def test_no_carry_machinery_remains(self, tmp_path) -> None:
        # Each object is a bounded unit, so an unclosed "(" anywhere can
        # never accumulate across reads.
        path = tmp_path / "unclosed.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        xref = doc[0].get_contents()[0]
        doc.update_stream(xref, doc.xref_stream(xref) + b"\n( unclosed " + b"A" * 200_000)
        broken = tmp_path / "unclosed2.pdf"
        doc.save(broken)
        doc.close()
        report = _scan(broken)
        assert not any("carry" in w or "truncat" in w for w in report.warnings)

    def test_pattern_rules_do_not_fuse_across_literals(self, tmp_path) -> None:
        # Two harmless values in one object must not synthesize a hard
        # pattern finding. (Value rules DO fuse across adjacent literals
        # by design, to catch a known secret split across them — that is
        # pre-existing behaviour and is why this checks patterns only.)
        path = tmp_path / "fence.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.set_metadata({"subject": "123", "keywords": "456789"})
        doc.save(path)
        doc.close()
        report = _scan(path)
        assert "Any SSN" not in {f.secret_name for f in report.findings}

    def test_procset_image_names_do_not_hide_a_form_xobject(self, tmp_path) -> None:
        # /ProcSet[/PDF/Text/ImageB/ImageC/ImageI] is boilerplate on
        # ordinary text-bearing Form XObjects. A substring test for
        # "/Image" against the dictionary source classified those as
        # image data and skipped their streams — a silent miss.
        path = tmp_path / "form.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "cover page only")
        xref = doc.get_new_xref()
        doc.update_object(xref, (
            "<< /Type /XObject /Subtype /Form /BBox [0 0 200 100]"
            " /Resources << /ProcSet [/PDF /Text /ImageB /ImageC /ImageI] >> >>"
        ))
        doc.update_stream(xref, f"BT 10 50 Td (SSN {SSN}) Tj ET".encode())
        doc.save(path)
        doc.close()

        assert "/Image" in fitz.open(path).xref_object(xref)   # the trap
        assert {f.secret_name for f in _scan(path).findings} == {"Target SSN", "Any SSN"}

    def test_stream_kind_is_read_from_the_object_keys(self, tmp_path) -> None:
        # Both halves of the structural test, including a /Filter
        # pipeline — PyMuPDF renders those with no separators, so a
        # whitespace split would miss the codec.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        image = doc.get_new_xref()
        doc.update_object(image, "<< /Subtype /Image /Width 1 /Height 1 >>")
        pipeline = doc.get_new_xref()
        doc.update_object(pipeline, "<< /Filter [/ASCII85Decode /DCTDecode] >>")
        form = doc.get_new_xref()
        doc.update_object(form, "<< /Subtype /Form /Filter /FlateDecode >>")

        assert verify._is_opaque_stream(doc, image)
        assert verify._is_opaque_stream(doc, pipeline)
        assert not verify._is_opaque_stream(doc, form)
        assert not verify._is_opaque_stream(doc, doc[0].get_contents()[0])
        doc.close()

    def test_indirect_filter_is_resolved(self, tmp_path) -> None:
        # /Filter may be an indirect reference; xref_get_key then returns
        # kind 'xref', which used to fall through to non-opaque, so an
        # image behind an indirect filter was parsed as text. (Written as
        # raw bytes: MuPDF collapses an indirect ref built in memory, so
        # only an ingested file reproduces the 'xref' kind.)
        path = tmp_path / "indirect.pdf"
        path.write_bytes(
            b"%PDF-1.7\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
            b"/Resources<<>>>>endobj\n"
            b"4 0 obj/DCTDecode endobj\n"
            b"5 0 obj<</Type/XObject/Subtype/Image/Width 1/Height 1"
            b"/Filter 4 0 R/Length 4>>stream\n\xff\xd8\xff\xe0\nendstream endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF"
        )
        doc = fitz.open(path)
        assert doc.xref_get_key(5, "Filter")[0] == "xref"   # the trap
        assert verify._stream_key(doc, 5, "Filter") == ("name", "/DCTDecode")
        assert verify._is_opaque_stream(doc, 5)
        doc.close()

    def test_content_sniff_skips_markerless_binary(self, tmp_path) -> None:
        # A binary stream with no image/font/embedded marker slips the key
        # pre-filter; the content sniff on the decompressed body keeps its
        # bytes out of the tokenizer (where "(" bytes coin false findings).
        blob = bytes(range(256)) * 20
        assert verify._looks_binary(blob)
        assert not verify._looks_binary(b"BT /F1 12 Tf (hello world) Tj ET")

    def test_object_and_xref_streams_are_opaque(self, tmp_path) -> None:
        path = tmp_path / "os.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.save(path, deflate=True, use_objstms=1)
        doc.close()
        doc = fitz.open(path)
        seen = set()
        for xref in range(1, doc.xref_length()):
            t = doc.xref_get_key(xref, "Type")[1]
            if t in ("/ObjStm", "/XRef"):
                seen.add(t)
                assert verify._is_opaque_stream(doc, xref), f"{t} scanned as text"
        doc.close()
        assert "/ObjStm" in seen and "/XRef" in seen

    def test_nested_parens_do_not_truncate_a_literal(self, tmp_path) -> None:
        # "(SSN (mine): 123-45-6789)" is ONE literal — the spec only
        # requires escaping unbalanced parens. A regex that forbade "("
        # in the body matched the inner "(mine)" and silently dropped
        # everything after it, secret included.
        path = tmp_path / "nested.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "cover")
        doc.update_stream(
            page.get_contents()[0],
            f"BT /F1 12 Tf 50 700 Td (SSN (mine): {SSN} done) Tj ET".encode())
        doc.save(path)
        doc.close()
        assert {f.secret_name for f in _scan(path).findings} == {"Target SSN", "Any SSN"}

    def test_unterminated_literal_yields_nothing(self) -> None:
        # Its extent is unknowable; guessing one would fuse the rest of
        # the object into a token hard pattern rules could match across.
        assert list(verify._iter_pdf_strings("(closed) (dangling")) == [("(closed)", 8)]

    def test_unterminated_literal_degrades_not_silent(self, tmp_path) -> None:
        # An unbalanced '(' swallows the following literals; that content
        # must degrade the verdict (a warning -> exit 2), never be dropped
        # silently on a document that could still be leaking.
        spans, truncated = verify._pdf_string_spans(f"(oops  BT (SSN {SSN}) Tj")
        assert truncated and spans == []
        path = tmp_path / "trunc.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "cover")
        doc.update_stream(page.get_contents()[0], f"(oops  (SSN {SSN}) Tj".encode())
        doc.save(path)
        doc.close()
        report = _scan(path)
        assert any("unterminated string literal" in w for w in report.warnings)
        assert report.degraded

    def test_tokenizer_is_linear_not_quadratic(self) -> None:
        # A run of unescaped "(" (crafted, or binary that slipped the
        # opaque-stream filter) once rescanned from start+1 per paren:
        # 200KB took 13 minutes, 1MB hours — a DoS on a tool whose job
        # is to answer. One pass finishes 1M parens well under a second.
        import time

        start = time.perf_counter()
        assert list(verify._iter_pdf_strings("(" * 1_000_000)) == []
        assert time.perf_counter() - start < 2.0

    def test_font_programs_are_not_parsed_as_text(self, tmp_path) -> None:
        # A TrueType glyf table tokenizes into literals whose bytes
        # normalize to digit runs — that produced two hard SSN findings
        # on a clean document. /Length1 marks a font program and
        # nothing else.
        path = tmp_path / "font.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        xref = doc.get_new_xref()
        doc.update_object(xref, "<< /Length1 64 >>")
        doc.update_stream(xref, f"({SSN})".encode())
        doc.save(path)
        doc.close()
        assert _scan(path).findings == []

    def test_attachments_are_left_to_the_hidden_layer(self, tmp_path) -> None:
        # The Hidden layer scans embedded files as manual-review
        # warnings, the honest tier for arbitrary binary. Reporting them
        # here as confirmed leaks would both duplicate and overstate.
        path = tmp_path / "attach.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        doc.embfile_add("note.bin", f"SSN {SSN}".encode())
        doc.save(path)
        doc.close()
        assert [f.layer for f in _scan(path).findings] == []


class TestOrphanLabel:
    """"ORPHANED" means "left behind by a redaction" — it must be earned."""

    def test_annotations_are_reachable(self, tmp_path) -> None:
        # Annotations, form fields and appearance streams hang off the
        # catalog, not the page tree. A page-only walk called them all
        # orphaned — 61 of 73 findings on a real document.
        path = tmp_path / "annot.pdf"
        doc = fitz.open()
        doc.new_page().add_freetext_annot(fitz.Rect(50, 50, 300, 90), f"SSN {SSN}")
        doc.save(path)
        doc.close()
        locations = {f.location for f in _scan(path).findings}
        assert locations and not any("ORPHANED" in loc for loc in locations)

    def test_object_streams_do_not_fake_orphans(self, tmp_path) -> None:
        # On PDF 1.5+ (Acrobat/Ghostscript/qpdf/Chrome default), dict
        # objects are packed into /ObjStm containers linked only by the
        # xref stream's binary offsets, never by an 'N G R' token. Scanning
        # an ObjStm body re-found every packed secret and, since the
        # container is never reachable via a textual ref, stamped it
        # ORPHANED on a clean, fully-referenced document.
        path = tmp_path / "objstm.pdf"
        doc = fitz.open()
        doc.new_page().add_freetext_annot(fitz.Rect(50, 50, 300, 90), f"SSN {SSN}")
        doc.save(path, deflate=True, use_objstms=1)
        doc.close()
        # The container really is present and unreachable-by-ref...
        reloaded = fitz.open(path)
        types = {reloaded.xref_get_key(x, "Type")[1]
                 for x in range(1, reloaded.xref_length())}
        reloaded.close()
        assert "/ObjStm" in types                     # the layout under test
        # ...yet nothing is falsely accused.
        assert not any("ORPHANED" in f.location for f in _scan(path).findings)

    def test_a_real_orphan_is_still_named(self, tmp_path) -> None:
        path = tmp_path / "orphan.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean")
        xref = doc.get_new_xref()                      # referenced by nothing
        doc.update_object(xref, f"<< /T ({SSN}) >>")
        doc.save(path)
        doc.close()
        assert all("ORPHANED" in f.location for f in _scan(path).findings)

    def test_ref_shaped_string_does_not_confer_reachability(self, tmp_path) -> None:
        # A genuine orphan must keep its ORPHANED label even when its
        # object number appears as an 'N G R'-shaped run inside some
        # reachable object's string literal. References are read from the
        # source with literals stripped, so the decoy cannot fake an edge.
        path = tmp_path / "decoy.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "clean")
        orphan = doc.get_new_xref()                    # referenced by nothing
        doc.update_object(orphan, f"<< /T ({SSN}) >>")
        doc.xref_set_key(doc.pdf_catalog(), "Note", f"({orphan} 0 R)")
        doc.save(path)
        doc.close()
        locations = {f.location for f in _scan(path).findings}
        assert locations and all("ORPHANED" in loc for loc in locations)

    def test_partial_reachability_walk_does_not_accuse(self, tmp_path) -> None:
        # If the walk cannot render an object it hits (a truncated set,
        # not an empty one), the bool()-of-set guard used to stay True and
        # every object reached only through the failed node was stamped
        # ORPHANED. A partial walk must be treated as untrusted.
        path = tmp_path / "hub.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"SSN {SSN}")
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        root = doc.pdf_catalog()
        real = doc.xref_object

        def flaky(xref, *a, **k):
            if xref == root:
                raise RuntimeError("unrenderable hub")
            return real(xref, *a, **k)

        doc.xref_object = flaky
        report = verify.ScanReport()
        verify.scan_pdf_objects(
            doc, verify.SecretMatcher([verify.Secret("T", verify.normalize_string(SSN))]),
            (), report)
        doc.close()
        assert report.findings
        assert not any("ORPHANED" in f.location for f in report.findings)

    def test_unknown_reachability_does_not_accuse(self, tmp_path) -> None:
        # An unreadable trailer means "unknown", not "everything is
        # orphaned" — fall back to the plain label.
        path = tmp_path / "plain.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"SSN {SSN}")
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        doc.pdf_trailer = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
        report = verify.ScanReport()
        verify.scan_pdf_objects(
            doc, verify.SecretMatcher([verify.Secret("Target SSN",
                                                     verify.normalize_string(SSN))]),
            (), report)
        doc.close()
        assert report.findings
        assert not any("ORPHANED" in f.location for f in report.findings)


class TestStructuralScanErrors:
    def test_unreadable_objects_degrade_loudly(self) -> None:
        report = verify.ScanReport()

        class Hostile:
            def xref_length(self): raise RuntimeError("no xref")

        verify.scan_pdf_objects(
            Hostile(), verify.SecretMatcher([verify.Secret("x", "y")]), (), report)
        assert report.findings == []
        assert report.degraded


class TestQpdfIsNowOrphanBackstopOnly:
    def test_raw_matches_are_warnings_not_findings(self, tmp_path, ssn_rules) -> None:
        # The structural pass owns hard findings; a byte-level match can
        # be coincidence, so qpdf can only ever raise manual review.
        path = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"SSN {SSN}")
        doc.save(path)
        doc.close()
        result = run_verify(path, ssn_rules)
        assert result.returncode == 1
        assert "LAYER: Objects" in result.stdout
        assert "LAYER: Binary" not in result.stdout
