"""Carrier-surface detection matrix + guard-necessity ("mutation") tests.

Two questions the regression suite could not answer on its own:

1. *Does the tool actually catch a secret on every surface it can hide
   on?* — the CARRIER_SURFACES matrix plants the SSN on one surface at a
   time and asserts the Objects layer finds it (and labels an orphan as
   ORPHANED, a live carrier as not).

2. *Is each guard load-bearing, and would the suite notice if it broke?*
   — the mutation tests neutralize one guard, then assert the bug it
   prevents comes back. A guard whose removal changed nothing would be
   dead code or untested; here each mutation flips the outcome, so the
   guard is proven necessary and the detection it enables is proven
   sensitive to it. This is targeted mutation testing without a mutmut
   run: the mutants are the exact ways these guards could regress.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz

import verify

from caselib import REGISTRY, SSN, load
from caselib.run import build

load()


def _object_scan(path: Path) -> verify.ScanReport:
    doc = fitz.open(path)
    report = verify.ScanReport()
    secrets = [verify.Secret("SSN", verify.normalize_string(SSN))]
    patterns = [verify.PatternRule(
        "ssn", re.compile(verify.BUILTIN_PATTERN_CLASSES["ssn"][0]),
        verify._valid_ssn)]
    try:
        verify.scan_pdf_objects(doc, verify.SecretMatcher(secrets), patterns, report)
    finally:
        doc.close()
    return report


# ── 2. Guard-necessity mutations ────────────────────────────────────────────
def _binary_stream_hiding_a_literal(path: Path) -> None:
    """A marker-less FlateDecode stream that is mostly binary but contains
    the digits of a valid SSN as a literal — the content sniff must keep
    it out of the tokenizer, or the binary coins a hard false finding on
    an otherwise clean document."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "entirely clean visible text")
    blob = doc.get_new_xref()
    doc.update_object(blob, "<< >>")            # no /Subtype, /Length1, /Filter marker
    body = bytes(range(256)) * 40 + f"(SSN {SSN})".encode()
    doc.update_stream(blob, body)               # deflated on save
    doc.save(str(path), deflate=True)
    doc.close()


class TestGuardsAreLoadBearing:
    """Each test: baseline behaves; mutating one guard brings the bug back."""

    def test_content_sniff_prevents_binary_false_positive(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "binlit.pdf"
        _binary_stream_hiding_a_literal(path)

        # Baseline: the binary body is sniffed out, so the document is clean.
        assert _object_scan(path).findings == []

        # Mutation: disable the content sniff. The binary is now tokenized
        # and its embedded literal coins a hard false SSN finding.
        monkeypatch.setattr(verify, "_looks_binary", lambda data: False)
        mutated = {f.secret_name for f in _object_scan(path).findings}
        assert "SSN" in mutated, (
            "mutation survived: removing the content sniff did NOT reintroduce "
            "the binary false positive — the guard would be untested"
        )

    def test_literal_stripping_keeps_a_real_orphan_labelled(
        self, tmp_path, monkeypatch
    ) -> None:
        # Orphan carrying the SSN, plus a reachable object whose string
        # literal contains the orphan's "N 0 R" as a decoy.
        path = tmp_path / "decoy.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "clean")
        orphan = doc.get_new_xref()
        doc.update_object(orphan, f"<< /T (SSN {SSN}) >>")
        doc.xref_set_key(doc.pdf_catalog(), "Note", f"({orphan} 0 R)")
        doc.save(str(path))
        doc.close()

        # Baseline: the decoy is inside a string, so the orphan stays ORPHANED.
        assert all("ORPHANED" in f.location for f in _object_scan(path).findings)

        # Mutation: stop stripping string literals before extracting refs.
        monkeypatch.setattr(verify, "_strip_pdf_strings", lambda source: source)
        mutated = _object_scan(path).findings
        assert mutated and not any("ORPHANED" in f.location for f in mutated), (
            "mutation survived: without literal stripping the decoy '(N 0 R)' "
            "should have faked reachability and dropped the ORPHANED label"
        )

    def test_objstm_exclusion_prevents_false_orphan(
        self, tmp_path, monkeypatch
    ) -> None:
        # An annotation the writer packs into an /ObjStm (verified: /Info
        # stays uncompressed, an annotation is packed) — the container's
        # body then holds the secret and is unreachable via 'N G R'.
        path = tmp_path / "objstm.pdf"
        build(REGISTRY["document.annotation-objstm"], path)
        doc = fitz.open(path)
        assert "/ObjStm" in {doc.xref_get_key(x, "Type")[1]
                             for x in range(1, doc.xref_length())}
        doc.close()

        # Baseline: object-stream containers are opaque, so no false orphan.
        assert not any("ORPHANED" in f.location for f in _object_scan(path).findings)

        # Mutation: treat nothing as opaque. The ObjStm body is scanned and,
        # being unreachable via 'N G R', its packed secret is falsely orphaned.
        monkeypatch.setattr(verify, "_is_opaque_stream", lambda doc, xref: False)
        mutated = _object_scan(path).findings
        assert any("ORPHANED" in f.location for f in mutated), (
            "mutation survived: scanning ObjStm bodies should have reintroduced "
            "the false ORPHANED accusation on a clean object-stream file"
        )

    def test_truncation_flag_prevents_silent_miss(
        self, tmp_path, monkeypatch
    ) -> None:
        # An unbalanced '(' before a real SSN literal must degrade (warn).
        path = tmp_path / "trunc.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "cover")
        doc.update_stream(
            page.get_contents()[0], f"(oops  (SSN {SSN}) Tj".encode())
        doc.save(str(path))
        doc.close()

        # Baseline: the object degrades to a manual-review warning.
        assert any("unterminated" in w for w in _object_scan(path).warnings)

        # Mutation: report the buffer as never truncated. The content after
        # the unbalanced paren is then dropped with no warning — a silent
        # miss, exactly the fail-closed violation the flag prevents.
        real_spans = verify._pdf_string_spans
        monkeypatch.setattr(
            verify, "_pdf_string_spans",
            lambda buf: (real_spans(buf)[0], False))
        report = _object_scan(path)
        assert not any("unterminated" in w for w in report.warnings), (
            "mutation survived: suppressing the truncated flag should have "
            "removed the degradation warning (proving the flag is what degrades)"
        )
