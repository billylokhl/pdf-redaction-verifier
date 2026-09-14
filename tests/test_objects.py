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
