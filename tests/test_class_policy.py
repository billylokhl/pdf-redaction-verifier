"""ADR 0012's class policy, PR 3a: built-in class matches as informational
notices under --no-strict-classes; strict mode (still the default here)
unchanged. Every value is fabricated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

from redaction_verifier.model import VerifyError
from redaction_verifier.matching.policy import CLASS_KINDS, informational_rules
from redaction_verifier.model import ScanReport
from redaction_verifier.rules import _load_rules_json

from .conftest import REPO_ROOT

# ── matching.policy ──────────────────────────────────────────────────────

KINDS = {"Any SSN": "class", "entity_types:ssn": "entity_types", "Target": "value",
         "Ref": "pattern", "My SSN": "class"}


def test_strict_mode_makes_nothing_informational():
    assert informational_rules(KINDS, strict=True) == frozenset()


def test_without_strict_every_class_rule_is_informational_and_nothing_else():
    assert informational_rules(KINDS, strict=False) == frozenset(
        {"Any SSN", "entity_types:ssn", "My SSN"})
    assert CLASS_KINDS == frozenset({"class", "entity_types"})


def test_a_strict_class_rule_stays_in_the_verdict():
    assert informational_rules(KINDS, strict=False, strict_rules={"My SSN"}) == frozenset(
        {"Any SSN", "entity_types:ssn"})


# ── rules: the "strict" key ──────────────────────────────────────────────


def _rules(tmp_path, entries):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(entries))
    return _load_rules_json(p)


def test_strict_key_is_recorded(tmp_path):
    rs = _rules(tmp_path, [{"name": "A", "class": "ssn", "strict": True},
                           {"name": "B", "class": "email", "strict": False},
                           {"name": "C", "class": "us-phone"}])
    assert rs.strict_rules == frozenset({"A"})


@pytest.mark.parametrize("entry", [
    {"name": "V", "value": "x", "strict": True},
    {"name": "P", "pattern": "x", "strict": True},
    {"name": "S", "class": "ssn", "strict": "yes"},
])
def test_strict_key_is_refused_where_it_cannot_apply(tmp_path, entry):
    with pytest.raises(VerifyError):
        _rules(tmp_path, [entry])


# ── model: routing to notices ────────────────────────────────────────────


def test_informational_findings_are_routed_as_they_are_recorded():
    r = ScanReport(informational=frozenset({"Any SSN"}))
    r.record("Text", "Any SSN", "page 1", "234-56-7890")
    r.record("Text", "Target", "page 1", "x")
    assert [f.secret_name for f in r.findings] == ["Target"]
    assert [n.secret_name for n in r.notices] == ["Any SSN"]  # type: ignore[union-attr]
    assert r.leaked


def test_settle_notices_moves_only_warnings_of_informational_rules():
    r = ScanReport(informational=frozenset({"Any SSN"}))
    r.warn("REVIEW_FUSED_PATTERN", "Text", "same text", rule="Any SSN")
    r.warn("REVIEW_FUSED_PATTERN", "Text", "same text", rule="Ref")  # equal text, other rule
    r.warn("LAYER_CRASHED", "OCR", "OCR: layer crashed")
    r.settle_notices()
    r.settle_notices()  # idempotent
    assert [w.fields.get("rule") for w in r.warnings] == ["Ref", None]  # type: ignore[attr-defined]
    assert [n.fields.get("rule") for n in r.notices] == ["Any SSN"]  # type: ignore[union-attr]
    assert r.degraded


def test_strict_report_routes_nothing():
    r = ScanReport()
    r.record("Text", "Any SSN", "page 1", "234-56-7890")
    r.warn("REVIEW_FUSED_PATTERN", "Text", "w", rule="Any SSN")
    r.settle_notices()
    assert r.findings and r.warnings and not r.notices


# ── the CLI, end to end ──────────────────────────────────────────────────


def _pdf(path: Path, text: str) -> Path:
    doc = fitz.open()
    doc.new_page().insert_text((50, 80), text)
    doc.save(str(path))
    doc.close()
    return path


def _run(tmp_path, text, rules, *flags):
    pdf = _pdf(tmp_path / "f.pdf", text)
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps(rules))
    out = tmp_path / "o.json"
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "verify.py"), "--target", str(pdf),
                           "--secrets", str(rp), "--json", str(out), *flags],
                          capture_output=True, text=True, timeout=300)
    return proc, json.loads(out.read_text())


SSN_CLASS = [{"name": "Any SSN", "class": "ssn"}]
TEXT = "Applicant 234-56-7890 on file"


def test_default_is_strict_today(tmp_path):
    proc, data = _run(tmp_path, TEXT, SSN_CLASS)
    assert proc.returncode == 1 and data["findings"] and data["notices"] == []
    assert data["schema_version"] == 2


def test_no_strict_classes_turns_class_matches_into_notices(tmp_path):
    proc, data = _run(tmp_path, TEXT, SSN_CLASS, "--no-strict-classes")
    assert proc.returncode == data["exit_code"]
    assert data["findings"] == [] and data["notices"]
    assert all(n["kind"] == "class-match" and n["rule"] == "Any SSN" for n in data["notices"])
    assert all(n["sample"].startswith("****") for n in data["notices"] if n["tier"] == "hard")
    assert "NOT COUNTED IN THE VERDICT" in proc.stdout
    assert "7890" not in json.dumps(data["notices"]).replace("****7890", "")


def test_a_value_rule_still_decides_beside_an_informational_class(tmp_path):
    rules = SSN_CLASS + [{"name": "Target", "value": "234-56-7890"}]
    proc, data = _run(tmp_path, TEXT, rules, "--no-strict-classes")
    assert proc.returncode == 1
    assert {f["rule"] for f in data["findings"]} == {"Target"}


def test_a_strict_class_rule_and_a_custom_pattern_still_decide(tmp_path):
    proc, _ = _run(tmp_path, TEXT, [{"name": "Any SSN", "class": "ssn", "strict": True}],
                   "--no-strict-classes")
    assert proc.returncode == 1
    proc, _ = _run(tmp_path, TEXT, [{"name": "Ref", "pattern": r"\d{3}-\d{2}-\d{4}"}],
                   "--no-strict-classes")
    assert proc.returncode == 1


def test_fail_fast_does_not_stop_on_a_notice(tmp_path):
    _, data = _run(tmp_path, TEXT, SSN_CLASS, "--no-strict-classes", "--fail-fast")
    assert data["findings"] == []
    assert not any(w["code"].startswith("SKIPPED_FAIL_FAST") for w in data["warnings"])
