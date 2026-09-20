"""Producer-layout corpus: the tool must behave on the file shapes real
producers emit, not just on fitz's default classic-xref save.

The ObjStm false-ORPHANED bug shipped green because every earlier fixture
used the one layout that hid it. These tests scan the SAME planted secret
across classic, object-stream, incremental and garbage-collected layouts,
and assert two things per layout: a real leak is found, and a clean file
is not falsely accused of orphaned content.

Real vendor PDFs (Acrobat/Word/scanner output) are exercised too when
present — drop `foo.pdf` + `foo.pdf.secrets.json` into tests/corpus_pdfs/.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytest

import verify

from . import corpus_builders as cb
from .conftest import requires_full_env, run_verify


def _object_scan(path: Path) -> verify.ScanReport:
    """Objects layer only — fast, needs no external tools."""
    doc = fitz.open(path)
    report = verify.ScanReport()
    secrets = [verify.Secret("SSN", verify.normalize_string(cb.SSN))]
    patterns = [verify.PatternRule(
        "ssn", re.compile(verify.BUILTIN_PATTERN_CLASSES["ssn"][0]),
        verify._valid_ssn)]
    try:
        verify.scan_pdf_objects(doc, verify.SecretMatcher(secrets), patterns, report)
    finally:
        doc.close()
    return report


@pytest.mark.parametrize("layout", sorted(cb.PRODUCER_LAYOUTS))
class TestProducerLayouts:
    def test_leak_is_found_in_every_layout(self, layout, tmp_path) -> None:
        path = tmp_path / f"{layout}.pdf"
        cb.build_layout(layout, path)
        found = {f.secret_name for f in _object_scan(path).findings}
        assert "SSN" in found, f"{layout}: dict-resident secret not detected"

    def test_clean_file_is_not_falsely_orphaned(self, layout, tmp_path) -> None:
        path = tmp_path / f"clean-{layout}.pdf"
        cb.build_clean_layout(layout, path)
        report = _object_scan(path)
        assert not any("ORPHANED" in f.location for f in report.findings), (
            f"{layout}: clean file falsely accused of orphaned content"
        )
        assert report.findings == []


class TestObjectStreamLayoutSpecifics:
    def test_packed_secret_is_not_double_reported_as_orphan(self, tmp_path) -> None:
        # The headline regression: on an object-stream file the /ObjStm
        # container is unreachable via 'N G R', so scanning its body once
        # stamped every packed secret ORPHANED. Uses an annotation, which
        # the writer genuinely packs into the ObjStm (/Info is kept out),
        # so this test regresses if the container exclusion is reverted.
        path = tmp_path / "objstm.pdf"
        cb.plant_objstm_packed(path)
        doc = fitz.open(path)
        packed = any(
            doc.xref_get_key(x, "Type")[1] == "/ObjStm"
            and verify.normalize_string(cb.SSN)
            in verify.normalize_string(doc.xref_stream(x).decode("latin-1"))
            for x in range(1, doc.xref_length())
            if doc.xref_is_stream(x)
        )
        doc.close()
        assert packed, "fixture invalid: secret is not inside an ObjStm body"
        report = _object_scan(path)
        assert "SSN" in {f.secret_name for f in report.findings}
        assert not any("ORPHANED" in f.location for f in report.findings)


@requires_full_env
@pytest.mark.parametrize("layout", sorted(cb.PRODUCER_LAYOUTS))
def test_cli_verdict_matches_layout(layout, tmp_path, secrets_file) -> None:
    # End-to-end through every layer: a planted secret must exit 1, and a
    # clean file of the same layout must certify (exit 0).
    leaky = tmp_path / f"{layout}.pdf"
    cb.build_layout(layout, leaky)
    assert run_verify(leaky, secrets_file).returncode == 1

    clean = tmp_path / f"clean-{layout}.pdf"
    cb.build_clean_layout(layout, clean)
    assert run_verify(clean, secrets_file).returncode == 0


_REAL_CORPUS = list(cb.iter_real_corpus()) or [
    pytest.param(
        None, None,
        marks=pytest.mark.skip(reason="no real vendor PDFs in tests/corpus_pdfs/"),
    )
]


@pytest.mark.parametrize(
    "pdf,expected",
    _REAL_CORPUS,
    ids=lambda v: Path(v).name if isinstance(v, Path) else "",
)
def test_real_vendor_corpus(pdf, expected, tmp_path) -> None:
    # Only runs when real files are dropped into tests/corpus_pdfs/. Each
    # expected secret (from the sidecar) must be caught by some layer.
    rules = tmp_path / "rules.json"
    import json
    rules.write_text(json.dumps(
        [{"name": name, "value": val} for name, val in expected]))
    result = run_verify(pdf, rules)
    assert result.returncode == 1, f"{pdf.name}: expected secrets not detected"
