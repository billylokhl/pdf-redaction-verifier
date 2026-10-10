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

import pymupdf as fitz
import pytest

import verify

from redaction_verifier.matching import (
    BUILTIN_PATTERN_CLASSES,
    PatternRule,
    SecretMatcher,
    _valid_ssn,
    normalize_string,
)
from redaction_verifier.model import ScanReport, Secret

from .conftest import report_layers, requires_qpdf, run_verify

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
    report = ScanReport()
    secrets = [Secret("Target SSN", normalize_string(SSN))]
    patterns = [PatternRule(
        "Any SSN", re.compile(BUILTIN_PATTERN_CLASSES["ssn"][0]),
        _valid_ssn)]
    try:
        verify.scan_pdf_objects(doc, SecretMatcher(secrets), patterns, report)
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
        report = ScanReport()
        verify.scan_pdf_objects(
            doc, SecretMatcher([Secret("T", normalize_string(SSN))]),
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
        report = ScanReport()
        verify.scan_pdf_objects(
            doc, SecretMatcher([Secret("Target SSN", normalize_string(SSN))]),
            (), report)
        doc.close()
        assert report.findings
        assert not any("ORPHANED" in f.location for f in report.findings)


class TestStructuralScanErrors:
    def test_unreadable_objects_degrade_loudly(self) -> None:
        report = ScanReport()

        class Hostile:
            def xref_length(self): raise RuntimeError("no xref")

        verify.scan_pdf_objects(
            Hostile(), SecretMatcher([Secret("x", "y")]), (), report)
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
        assert "Objects" in report_layers(result.stdout)
        assert "Binary" not in report_layers(result.stdout)


def _qdf_tail(first: str, second: str, extra: str = "") -> bytes:
    """The end of qpdf's QDF output, exactly as qpdf writes it."""
    return (
        b"0000001585 00000 n \ntrailer <<\n  /Root 1 0 R\n  /Size 16\n"
        + extra.encode()
        + f"  /ID [<{first}><{second}>]\n>>\nstartxref\n1692\n%%EOF\n".encode()
    )


def _collect(data: bytes, input_ids: frozenset[str] | None, value: str = "4567") -> list[str]:
    import subprocess
    import sys
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(data)
    proc.stdin.close()
    report = ScanReport()
    verify._collect_qpdf(proc, SecretMatcher([Secret("short", value)]), report, input_ids)
    return [w.code for w in report.warnings]


ORIGINAL = "31415926535897932384626433832795"
LUCKY = "0123abcd0123abcd9e4567f0deadbeef"     # a generated /ID that spells 4567


class TestQpdfGeneratedId:
    """qpdf writes a document /ID of its own (time-based by default): the
    input's first element is kept, the second (both, if the input has
    none) is new. Those bytes are not in the file, so the Binary layer must
    not scan them, or a short value rule matches at random and the same
    file gets a different verdict from one run to the next."""

    def test_input_ids_are_every_id_array_in_the_file(self) -> None:
        raw = b"<< /ID [<ABcd 12> <ef01>] >> ... /ID[<00><1>] /IDx [<99><99>]"
        assert verify._input_document_ids(raw) == frozenset({"abcd12", "ef01", "00", "10"})
        assert verify._input_document_ids(b"no ids here") == frozenset()

    def test_literal_string_ids_are_decoded(self) -> None:
        # MuPDF writes /ID as one hex and one literal string.
        # octal escapes, escaped parens and backslash, an unknown escape
        # (\q is just q), a line continuation, and CR LF read as one LF.
        raw = b"/ID[<C2B0>(E\\033Z\\(\\)\\\\(n)\\101\\\nx\\qz\r\ny)]"
        expected = (b"E\x1bZ()\\(n)Axqz\ny").hex()
        assert verify._input_document_ids(raw) == frozenset({"c2b0", expected})

    @pytest.mark.parametrize("raw", [
        b"/ID 5 0 R", b"/ID [<aa>]", b"/ID [<aa><bb><cc>]", b"/ID [<zz><bb>]",
        b"/ID [(unclosed <bb>]", b"/ID [<aa> /Name]",
    ])
    def test_an_id_that_cannot_be_read_means_nothing_is_stripped(self, raw: bytes) -> None:
        assert verify._input_document_ids(raw) is None

    def test_only_the_generated_element_is_blanked(self) -> None:
        tail = _qdf_tail(ORIGINAL, LUCKY)
        out = verify._strip_generated_ids(tail, frozenset({ORIGINAL}))
        assert out == tail.replace(LUCKY.encode(), b"")
        # No /ID in the input: both elements are qpdf's own.
        both = verify._strip_generated_ids(_qdf_tail(LUCKY, LUCKY), frozenset())
        assert LUCKY.encode() not in both and b"  /ID [<><>]\n" in both

    @pytest.mark.parametrize("tail, input_ids", [
        (_qdf_tail(ORIGINAL, LUCKY), None),                                  # input unreadable
        (_qdf_tail(ORIGINAL, LUCKY), frozenset({"abcd"})),       # qpdf's first is not the input's
        # No /ID found in the input, yet qpdf kept a first element of its
        # own (an /ID key spelled in a way the parser missed): not generated.
        (_qdf_tail(ORIGINAL, LUCKY), frozenset()),
        # The rest would otherwise be blanked (both elements generated).
        (_qdf_tail(LUCKY, LUCKY, extra=f"  /X (/ID [<{LUCKY}><{LUCKY}>])\n"), frozenset()),
        (_qdf_tail(LUCKY, LUCKY) + b"more", frozenset()),                    # not at the end
        (_qdf_tail(LUCKY, LUCKY).replace(b"  /Size", b"/Size"), frozenset()),  # not QDF's shape
    ], ids=["ids-unknown", "first-not-input-s", "no-input-id-but-elements-differ",
            "two-ids-in-trailer", "not-at-end", "not-qdf-shape"])
    def test_anything_unexpected_is_left_as_it_is(
        self, tail: bytes, input_ids: frozenset[str] | None
    ) -> None:
        assert verify._strip_generated_ids(tail, input_ids) == tail

    def test_a_generated_id_is_not_scanned(self) -> None:
        assert _collect(b"%PDF-1.7\n" + _qdf_tail(ORIGINAL, LUCKY), frozenset({ORIGINAL})) == []

    def test_the_input_s_own_id_still_is(self) -> None:
        data = b"%PDF-1.7\n" + _qdf_tail(LUCKY, ORIGINAL)
        assert _collect(data, frozenset({LUCKY})) == ["REVIEW_BINARY"]
        assert _collect(data, None) == ["REVIEW_BINARY"]

    def test_the_held_back_tail_is_scanned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Nothing qpdf wrote may go unscanned: not the held tail, and not a
        # match across the point where the tail was held back.
        tail = _qdf_tail(ORIGINAL, ORIGINAL)
        hold = len(tail) + 16
        monkeypatch.setattr(verify, "QPDF_TAIL_HOLD_BYTES", hold)
        body = b"x" * 100
        assert _collect(body + b"45-67" + tail, frozenset({ORIGINAL})) == ["REVIEW_BINARY"]
        for cut in range(1, 4):     # "4567" split by the hold boundary at every offset
            data = body + b"4567"[:cut] + b"4567"[cut:] + b"y" * (16 - (4 - cut)) + tail
            assert data[: len(data) - hold].endswith(b"4567"[:cut]), cut  # really straddles
            assert _collect(data, frozenset({ORIGINAL})) == ["REVIEW_BINARY"], cut

    def test_nested_id_arrays_are_read_in_linear_time(self) -> None:
        # Each /ID key is read once: resuming the search inside an array's
        # strings made this about 4000 times slower (20 s for 88 KB),
        # before qpdf's own deadline even starts.
        import time

        raw = b""
        for _ in range(8000):
            raw = b"/ID [(" + raw + b") ()]"
        start = time.monotonic()
        ids = verify._input_document_ids(raw)
        assert time.monotonic() - start < 2
        assert ids is not None and len(ids) == 2       # the outer array's two strings

    def test_output_of_a_qpdf_that_did_not_finish_is_never_stripped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # qpdf times out after writing what looks like a final trailer (it
        # could be the inside of a stream): nothing may be blanked, since
        # the end of qpdf's output was never seen.
        import subprocess
        import sys

        monkeypatch.setattr(verify, "SUBPROCESS_TIMEOUT_S", 1)
        written = tmp_path / "partial.qdf"
        written.write_bytes(b"%PDF-1.7\n" + _qdf_tail(LUCKY, LUCKY))
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); "
             "sys.stdout.flush(); time.sleep(30)", str(written)],
            stdout=subprocess.PIPE,
        )
        import select
        select.select([proc.stdout], [], [], 30)    # the 1 s deadline covers only the stall
        report = ScanReport()
        verify._collect_qpdf(proc, SecretMatcher([Secret("short", "4567")]), report, frozenset())
        assert sorted(w.code for w in report.warnings) == [
            "REVIEW_BINARY", "TOOL_EXIT_NONZERO", "TOOL_TIMEOUT"]

    @requires_qpdf
    @pytest.mark.parametrize("new_id", [False, True], ids=["no-input-id", "mupdf-literal-id"])
    def test_qpdf_s_own_id_does_not_reach_the_report(self, tmp_path: Path, new_id: bool) -> None:
        # End to end with the real qpdf: a value found only in the /ID
        # element qpdf generates for this file is never reported, while one
        # from the input's own /ID still is; the same on every run.
        import subprocess

        path = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "nothing to see")
        doc.save(path, no_new_id=not new_id)
        doc.close()
        assert (b"/ID" in path.read_bytes()) is new_id
        qdf = subprocess.run(
            ["qpdf", "--qdf", "--object-streams=disable", "--deterministic-id", str(path), "-"],
            capture_output=True, check=True,
        ).stdout
        first, second = (g.decode() for g in re.search(
            rb"/ID \[<([0-9a-f]+)><([0-9a-f]+)>\]", qdf).groups())
        normalized = normalize_string(qdf.decode("latin-1"))
        generated = second[4:16]
        assert normalized.count(generated) == (1 if new_id else 2)   # only in the /ID
        rules = [{"name": "from-generated-id", "value": generated}]
        if new_id:
            own = first[4:16]                                       # the input's own element
            assert first in verify._input_document_ids(path.read_bytes())
            rules.append({"name": "from-own-id", "value": own})
        rules_path = tmp_path / "rules.json"
        rules_path.write_text(json.dumps(rules))
        seen = []
        for _ in range(2):
            out = tmp_path / "report.json"
            run_verify(path, rules_path, "--json", str(out))
            warnings = json.loads(out.read_text())["warnings"]
            binary = sorted(w["rule"] for w in warnings if w["code"] == "REVIEW_BINARY")
            assert binary == (["from-own-id"] if new_id else [])
            seen.append(sorted((w["code"], w.get("rule")) for w in warnings))
        assert seen[0] == seen[1]

    @requires_qpdf
    @pytest.mark.parametrize("key", [b"/I#44 [", b"/ID\x00["], ids=["hex-escaped-key", "nul-after-key"])
    def test_an_id_key_the_parser_misses_does_not_unblank_the_file(
        self, tmp_path: Path, key: bytes
    ) -> None:
        # qpdf reads these spellings as /ID and keeps the file's first
        # element; the parser does not see them and reports no /ID. The
        # file's element must still be scanned (a review warning, as on
        # the tool before qpdf's own /ID was blanked).
        path = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "nothing to see")
        doc.save(path, no_new_id=True)
        doc.close()
        ids = b"<123456789000000000000000000000ab><00000000000000000000000000000000>]"
        path.write_bytes(path.read_bytes().replace(b"/Root 1 0 R", b"/Root 1 0 R" + key + ids, 1))
        assert verify._input_document_ids(path.read_bytes()) == frozenset()
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "SSN", "value": SSN}]))
        out = tmp_path / "report.json"
        run_verify(path, rules, "--json", str(out))
        warnings = json.loads(out.read_text())["warnings"]
        assert [w["rule"] for w in warnings if w["code"] == "REVIEW_BINARY"] == ["SSN"]
