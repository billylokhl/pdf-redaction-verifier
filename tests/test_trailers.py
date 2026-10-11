"""Tests for the trailer scan (K40, K41).

A classic trailer dictionary is not an object, so the object walk never
read its strings, and qpdf rewrites only the current trailer with the
document /ID re-encoded or replaced: a secret there was certified clean.
The owner's tier decision (option D): every trailer string is matched at
the hard tier, except that a pattern match in a /ID element written as
plain hex digits (a generated identifier written as text) is manual
review.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
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

from .conftest import requires_qpdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
from caselib.rawpdf import build, build_objstm, incremental_update, one_page, stream, text  # noqa: E402

SSN = "123-45-6789"
PAGE = stream(b"", text("Quarterly report"))
PATTERNS = [PatternRule("Any SSN", re.compile(BUILTIN_PATTERN_CLASSES["ssn"][0]), _valid_ssn)]
VALUES = SecretMatcher([Secret("Target SSN", normalize_string(SSN))])
NO_VALUES = SecretMatcher([])


def _with_trailer(keys: bytes, pdf: bytes | None = None) -> bytes:
    raw = build(one_page(PAGE)) if pdf is None else pdf
    marker = b"/Root 1 0 R >>"
    assert raw.count(marker) == 1
    return raw.replace(marker, b"/Root 1 0 R " + keys + b" >>")


def _trailers(path: Path, matcher: SecretMatcher = VALUES) -> ScanReport:
    report = ScanReport()
    doc = fitz.open(path)
    try:
        verify.scan_trailers(path, doc, matcher, PATTERNS, report)
    finally:
        doc.close()
    return report


def _objects(path: Path, matcher: SecretMatcher = VALUES, *, with_path: bool = True) -> ScanReport:
    report = ScanReport()
    doc = fitz.open(path)
    try:
        verify.scan_pdf_objects(doc, matcher, PATTERNS, report,
                                pdf_path=path if with_path else None)
    finally:
        doc.close()
    return report


def _found(report: ScanReport) -> list[tuple[str, str, str]]:
    return sorted((f.secret_name, f.storage, f.location) for f in report.findings)


def _warned(report: ScanReport) -> list[tuple[str, str | None]]:
    return sorted((w.code, w.fields.get("storage")) for w in report.warnings)


class TestIdElements:
    def test_elements_of_an_id_array(self) -> None:
        buf = "<< /ID [(a) <62>] /Note (c) /IDx [(d)] >>"
        spans, _ = verify._pdf_string_spans(buf)
        got = {buf[s:e] for s, e in verify._id_element_spans(buf, spans)}
        assert got == {"(a)", "<62>"}

    def test_an_id_key_inside_a_string_is_not_a_key(self) -> None:
        buf = "<< /Note (/ID [) /X [(b)] >>"
        spans, _ = verify._pdf_string_spans(buf)
        assert verify._id_element_spans(buf, spans) == frozenset()

    def test_an_unclosed_array_yields_nothing(self) -> None:
        buf = "<< /ID [(a) (b) >>"
        spans, _ = verify._pdf_string_spans(buf)
        assert verify._id_element_spans(buf, spans) == frozenset()

    @pytest.mark.parametrize("buf", [
        "<< /Priv << /ID [(a)] >> >>",          # a nested dictionary's key
        "<< /K [/ID [(a)]] >>",                 # inside an array
        "<< /Priv %/ID\n[(a)] >>",              # a key inside a comment
        "<< /I#44 [(a)] >>",                    # escaped spelling: stays hard
    ])
    def test_only_the_dictionary_s_own_id_key(self, buf: str) -> None:
        spans, _ = verify._pdf_string_spans(buf)
        assert verify._id_element_spans(buf, spans) == frozenset()

    def test_a_comment_between_key_and_array(self) -> None:
        buf = "<< /ID % generated\n[(a)] >>"
        spans, _ = verify._pdf_string_spans(buf)
        assert {buf[s:e] for s, e in verify._id_element_spans(buf, spans)} == {"(a)"}

    @pytest.mark.parametrize("unit", ["<< /ID [", "% c\n", "<< /X [/ID [(a)] "])
    def test_linear(self, unit: str) -> None:
        def cost(k: int) -> float:
            buf = unit * k
            spans, _ = verify._pdf_string_spans(buf)
            start = time.monotonic()
            verify._id_element_spans(buf, spans)
            return time.monotonic() - start
        small, big = cost(20000), cost(200000)
        assert big < max(small, 0.01) * 30, (small, big)


class TestTierByShape:
    """Owner decision D: only a pattern match in a /ID element that is
    nothing but hex digits and dashes is manual review."""

    @pytest.mark.parametrize("element", [
        b"(123-45-6789)",                                   # only digits and dashes
        b"(0a1b2c123456789def0a1b2c3d4e5f60)",              # generated id, as hex text
        b"<" + b"123456789".hex().encode() + b">",           # the same, hex-encoded text
    ])
    def test_hex_shaped_id_pattern_match_is_review(self, tmp_path: Path, element: bytes) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(b"/ID [" + element + b" <44556677>]"))
        report = _trailers(path, NO_VALUES)
        assert report.findings == []
        assert _warned(report) == [("REVIEW_OBJECT_TEXT", "live")]
        assert report.warnings[0].fields["adjacency"] == "NOISY_SOURCE"

    def test_a_known_value_in_a_hex_shaped_element_is_hard(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(b"/ID [(123-45-6789) <44556677>]"))
        assert _found(_trailers(path)) == [("Target SSN", "live", "trailer")]

    @pytest.mark.parametrize("keys", [
        b"/ID [(SSN 123-45-6789) <44556677>]",               # readable text in /ID
        b"/ID [<44556677> <" + b"SSN 123-45-6789".hex().encode() + b">]",
        b"/Note (123-45-6789)",                              # any other key: hard
        b"/Note [(123456789)]",
    ])
    def test_everything_else_is_hard(self, tmp_path: Path, keys: bytes) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(keys))
        report = _trailers(path, NO_VALUES)
        assert _found(report) == [("Any SSN", "live", "trailer")]
        assert report.warnings == []

    @pytest.mark.parametrize("keys", [
        b"/Priv << /ID [(123-45-6789)] >>",      # not the trailer's own /ID
        b"/Priv %/ID\n[(123-45-6789)]",
    ])
    def test_a_nested_or_commented_id_key_is_not_downgraded(self, tmp_path: Path,
                                                             keys: bytes) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(keys))
        report = _trailers(path, NO_VALUES)
        assert _found(report) == [("Any SSN", "live", "trailer")]

    def test_a_value_split_across_id_elements_is_reviewed(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(b"/ID [(123-45-) (6789)]"))
        report = _trailers(path, NO_VALUES)
        assert report.findings == []
        assert _warned(report) == [("REVIEW_ADJACENT_LITERALS", "live")]

    def test_a_clean_hex_id_is_quiet(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(b"/ID [<0a1b2c3d4e5f60718293a4b5c6d7e8f9> "
                                       b"(0a1b2c3d4e5f60718293a4b5c6d7e8f9)]"))
        report = _trailers(path)
        assert report.findings == [] and report.warnings == []


class TestWhichTrailers:
    def test_an_earlier_revision_s_trailer_is_superseded(self, tmp_path: Path) -> None:
        first = _with_trailer(b"/ID [<44556677> (SSN 123-45-6789)]")
        updated = incremental_update(first, {4: stream(b"", text("Revised"))})
        path = tmp_path / "t.pdf"
        path.write_bytes(updated)
        report = _trailers(path)
        assert _found(report) == [(rule, "superseded", "earlier revision 1, trailer")
                                  for rule in ("Any SSN", "Target SSN")]
        assert {f.revision for f in report.findings} == {1}

    def test_a_trailer_off_the_chain_is_still_read(self, tmp_path: Path) -> None:
        raw = build(one_page(PAGE)) + b"\ntrailer\n<< /Note (SSN 123-45-6789) >>\n"
        path = tmp_path / "t.pdf"
        path.write_bytes(raw)
        report = _trailers(path)
        pos = raw.rindex(b"<< /Note")
        assert _found(report) == [
            (rule, "unreferenced", f"trailer at byte {pos} (not on the cross-reference chain)")
            for rule in ("Any SSN", "Target SSN")]

    @pytest.mark.parametrize("keys", [
        b"% >>\n/ID [(SSN 123-45-6789) <00112233>]",        # '>>' inside a comment
        b"% note (\n/ID [(SSN 123-45-6789) <00112233>]",    # '(' inside a comment
    ])
    def test_comments_do_not_cut_a_trailer_short(self, tmp_path: Path, keys: bytes) -> None:
        raw = _with_trailer(keys).replace(b"trailer\n<<", b"trailer % x <<\n<<", 1)
        path = tmp_path / "t.pdf"
        path.write_bytes(raw)
        doc = fitz.open(path)
        assert not doc.is_repaired and "SSN" in doc.xref_get_key(-1, "ID")[1]
        doc.close()
        assert ("Target SSN", "live", "trailer") in _found(_trailers(path))

    def test_a_comment_in_a_trailer_is_read_for_review(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(b"% SSN 123-45-6789\n"))
        report = _trailers(path)
        assert report.findings == []
        assert _warned(report) == [("REVIEW_OBJECT_TEXT", "live")] * 2   # value and pattern

    def test_an_unreadable_chain_trailer_is_reported(self, tmp_path: Path) -> None:
        raw = build(one_page(PAGE)).replace(b"trailer\n<<", b"trailer\n<< /X (", 1)
        path = tmp_path / "t.pdf"
        path.write_bytes(raw)
        codes = [w.code for w in _trailers(path).warnings]
        assert "TRAILER_UNREADABLE" in codes

    def test_many_startxrefs_to_one_bad_section_stay_linear(self, tmp_path: Path) -> None:
        def cost(k: int) -> float:
            path = tmp_path / f"t{k}.pdf"
            good = build(one_page(PAGE))
            bad = len(good)
            path.write_bytes(good + b"xref\ntrailer\n<< (" + b"startxref %d\n" % bad * k)
            start = time.monotonic()
            _trailers(path)
            return time.monotonic() - start
        small, big = cost(1000), cost(10000)
        assert big < max(small, 0.01) * 30, (small, big)

    def test_an_unterminated_string_in_a_trailer_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = build(one_page(PAGE)) + b"<< /X (abc >>"
        path = tmp_path / "t.pdf"
        path.write_bytes(raw)
        start = raw.rindex(b"<< /X")
        monkeypatch.setattr(verify, "_chain_trailers", lambda _raw: (
            (verify._TrailerDict(start, len(raw), None, "live", None),), ()))
        assert "UNTERMINATED_STRING" in [w.code for w in _trailers(path).warnings]

    def test_revision_numbers_follow_the_earlier_revision_cap(self) -> None:
        earlier = [(i, i + 1, None) for i in range(60)]
        numbers = verify._revision_numbers(earlier)
        assert numbers[0] == 1 and numbers[11] == 2 and numbers[59] == 50
        assert 5 not in numbers

    def test_an_unclosed_trailer_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(build(one_page(PAGE)) + b"\ntrailer\n<< /Note (x) /D << ")
        assert _warned(_trailers(path)) == [("TRAILER_UNREADABLE", "unreferenced")]

    def test_many_trailer_keywords_stay_linear(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(build(one_page(PAGE)) + b"trailer<<" * 20000 + b">>" * 20000)
        start = time.monotonic()
        _trailers(path)
        assert time.monotonic() - start < 5

    def test_an_unreadable_file_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(build(one_page(PAGE)))
        doc = fitz.open(path)
        report = ScanReport()
        try:
            verify.scan_trailers(tmp_path / "missing.pdf", doc, VALUES, PATTERNS, report)
        finally:
            doc.close()
        assert _warned(report) == [("TRAILER_UNREADABLE", None)]

    @requires_qpdf
    def test_a_linearized_file_s_first_page_trailer_is_live(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Quarterly report")
        doc.new_page().insert_text((72, 72), "Second page")
        doc.save(str(plain))
        doc.close()
        lin = tmp_path / "lin.pdf"
        qpdf = shutil.which("qpdf")
        assert qpdf
        subprocess.run([qpdf, "--linearize", "--deterministic-id", str(plain), str(lin)],
                       check=True)
        raw = lin.read_bytes()
        first = re.search(rb"/ID \[<([0-9a-f]{32})>", raw)
        assert first and first.start() < len(raw) // 2       # the first-page trailer
        width = len(first.group(0)) - len(b"/ID [")             # same length: no offsets move
        planted = b"(SSN 123-45-6789" + b"x" * (width - 17) + b")"
        assert len(planted) == width
        raw = raw[:first.start() + 5] + planted + raw[first.end():]
        lin.write_bytes(raw)
        check = subprocess.run([qpdf, "--check-linearization", str(lin)], capture_output=True)
        assert check.returncode == 0, check.stdout
        assert ("Target SSN", "live", "trailer") in _found(_trailers(lin))


class TestCrossReferenceStreams:
    def _xref_stream_file(self, keys: bytes) -> bytes:
        raw = build_objstm(one_page(PAGE))
        marker = b"/Type /XRef /W [1 4 2]"
        assert raw.count(marker) == 1
        return raw.replace(marker, b"/Type /XRef " + keys + b" /W [1 4 2]")

    def test_labelled_as_the_trailer(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(self._xref_stream_file(b"/ID [(SSN 123-45-6789) <44556677>]"))
        assert _found(_objects(path)) == [
            (rule, "live", "cross-reference stream (trailer), object 8")
            for rule in ("Any SSN", "Target SSN")]

    def test_hex_shaped_id_pattern_match_is_review(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(self._xref_stream_file(b"/ID [(123456789) <44556677>]"))
        report = _objects(path, NO_VALUES)
        assert report.findings == []
        assert _warned(report) == [("REVIEW_OBJECT_TEXT", "live")]

    def test_without_the_file_the_old_label_and_tier_stay(self, tmp_path: Path) -> None:
        path = tmp_path / "t.pdf"
        path.write_bytes(self._xref_stream_file(b"/ID [(123456789) <44556677>]"))
        report = _objects(path, NO_VALUES, with_path=False)
        assert _found(report) == [("Any SSN", "orphaned", "ORPHANED object 8")]

    def test_an_earlier_revision_s_stream_is_superseded(self, tmp_path: Path) -> None:
        import struct
        base = self._xref_stream_file(b"/ID [(SSN 123-45-6789) <44556677>]")
        prev = int(re.findall(rb"startxref\s+(\d+)", base)[-1])
        out = bytearray(base)
        off4 = len(out)
        out += b"4 0 obj\n" + stream(b"", text("Revised")) + b"\nendobj\n"
        xoff = len(out)
        entry = struct.Struct(">BIH")
        entries = entry.pack(1, off4, 0) + entry.pack(1, xoff, 0)
        dictionary = (b"/Type /XRef /W [1 4 2] /Size 10 /Root 1 0 R /Index [4 1 9 1] "
                      b"/Prev %d /ID [<44556677> <8899aabb>]" % prev)
        out += b"9 0 obj\n" + stream(dictionary, entries) + b"\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % xoff
        path = tmp_path / "t.pdf"
        path.write_bytes(bytes(out))
        assert _found(_objects(path)) == [
            (rule, "superseded", "earlier revision 1, cross-reference stream (trailer), object 8")
            for rule in ("Any SSN", "Target SSN")]

    def test_a_reused_xref_stream_number_is_an_ordinary_object(self, tmp_path: Path) -> None:
        # The chain says object 8 was a cross-reference stream; a later
        # revision redefines 8 as an ordinary dictionary. MuPDF's object 8
        # is that dictionary: it must keep the hard tier, not the /ID one.
        import struct
        base = self._xref_stream_file(b"")
        prev = int(re.findall(rb"startxref\s+(\d+)", base)[-1])
        out = bytearray(base)
        off8 = len(out)
        out += b"8 0 obj\n<< /ID [(123-45-6789)] >>\nendobj\n"
        xoff = len(out)
        entry = struct.Struct(">BIH")
        entries = entry.pack(1, off8, 0) + entry.pack(1, xoff, 0)
        dictionary = b"/Type /XRef /W [1 4 2] /Size 10 /Root 1 0 R /Index [8 2] /Prev %d" % prev
        out += b"9 0 obj\n" + stream(dictionary, entries) + b"\nendobj\n"
        out += b"startxref\n%d\n%%%%EOF\n" % xoff
        path = tmp_path / "t.pdf"
        path.write_bytes(bytes(out))
        report = _objects(path, NO_VALUES)
        assert [f.location for f in report.findings] == ["ORPHANED object 8"]
        assert [w.code for w in report.warnings] == []

    def test_a_hybrid_file_s_stream_half_is_the_trailer(self, tmp_path: Path) -> None:
        # A classic table whose trailer names a cross-reference stream
        # (/XRefStm): the stream is part of the same, current revision.
        import struct
        raw = bytearray(build(one_page(PAGE)))
        startxref = raw.rindex(b"startxref")
        xref_at = raw.rindex(b"xref\n0 ")
        stm_off = xref_at
        body = struct.Struct(">BIH").pack(0, 0, 65535)
        stm = (b"7 0 obj\n" + stream(b"/Type /XRef /W [1 4 2] /Size 8 /Index [0 1] "
                                     b"/ID [(SSN 123-45-6789) <44556677>]", body)
               + b"\nendobj\n")
        tail = bytes(raw[xref_at:startxref])
        tail = tail.replace(b"0 7\n", b"0 8\n", 1).replace(b"/Size 7", b"/Size 8", 1)
        shift = len(stm)
        entry7 = b"%010d 00000 n \n" % stm_off
        table_end = tail.index(b"trailer")
        tail = tail[:table_end] + entry7 + tail[table_end:]
        tail = tail.replace(b"/Root 1 0 R", b"/Root 1 0 R /XRefStm %d" % stm_off, 1)
        out = bytes(raw[:xref_at]) + stm + tail + b"startxref\n%d\n%%%%EOF\n" % (xref_at + shift)
        path = tmp_path / "t.pdf"
        path.write_bytes(out)
        assert ("Target SSN", "live", "cross-reference stream (trailer), object 7") \
            in _found(_objects(path))
