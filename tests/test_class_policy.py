"""ADR 0012's class policy: built-in class matches as informational
notices by default (PR 3b; PR 3a made them opt-in); strict mode
(--strict-classes) unchanged. Every value is fabricated.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

from redaction_verifier.matching.policy import CLASS_KINDS, informational_rules
from redaction_verifier.model import ScanReport
from redaction_verifier.rules import _load_rules_json

from .conftest import REPO_ROOT

# ── matching.policy ──────────────────────────────────────────────────────

KINDS = {"Any SSN": "class", "Other SSN": "class", "Target": "value",
         "Ref": "pattern", "My SSN": "class", "exact_values[0]": "exact_values",
         "patterns[0]": "patterns"}


def test_strict_mode_makes_nothing_informational():
    assert informational_rules(KINDS, strict=True) == frozenset()


def test_without_strict_every_class_rule_is_informational_and_nothing_else():
    assert informational_rules(KINDS, strict=False) == frozenset(
        {"Any SSN", "Other SSN", "My SSN"})
    # A redactor config's entity_types are not read as classes (owner
    # decision 2026-10-16): they make no rules, so no kind for them.
    assert CLASS_KINDS == frozenset({"class"})


def test_a_strict_class_rule_stays_in_the_verdict():
    assert informational_rules(KINDS, strict=False, strict_rules={"My SSN"}) == frozenset(
        {"Any SSN", "Other SSN"})


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
    {"name": "V", "value": "x", "strict": False},
    {"name": "V", "value": "x", "strict": True},
    {"name": "P", "pattern": "x", "strict": "anything"},
])
def test_strict_key_is_ignored_on_other_rules_as_unknown_keys_always_were(tmp_path, entry):
    rs = _rules(tmp_path, [entry])
    assert rs.strict_rules == frozenset() and rs.strict_invalid == frozenset()


def test_a_malformed_strict_on_a_class_rule_still_loads(tmp_path):
    # Refused by default (not under --strict-classes; verify.py), where it matters.
    rs = _rules(tmp_path, [{"name": "S", "class": "ssn", "strict": "yes"}])
    assert rs.strict_invalid == frozenset({"S"}) and rs.strict_rules == frozenset()


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


def _pdf(path: Path, *pages: str) -> Path:
    doc = fitz.open()
    for text in pages:
        doc.new_page().insert_text((50, 80), text)
    doc.save(str(path))
    doc.close()
    return path


def _run(tmp_path, text, rules, *flags):
    pages = text if isinstance(text, tuple) else (text,)
    pdf = _pdf(tmp_path / "f.pdf", *pages)
    if isinstance(rules, str):  # a redactor YAML config
        rp = tmp_path / "r.yaml"
        rp.write_text(rules)
    else:
        rp = tmp_path / "r.json"
        rp.write_text(json.dumps(rules))
    out = tmp_path / "o.json"
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "verify.py"), "--target", str(pdf),
                           "--secrets", str(rp), "--json", str(out), *flags],
                          capture_output=True, text=True, timeout=300)
    return proc, json.loads(out.read_text())


SSN_CLASS = [{"name": "Any SSN", "class": "ssn"}]
TEXT = "Applicant 234-56-7890 on file"


def test_default_is_policy_1_and_strict_classes_restores_today(tmp_path):
    proc, data = _run(tmp_path, TEXT, SSN_CLASS)
    assert data["findings"] == [] and data["notices"]  # PR 3b: notices by default
    assert data["schema_version"] == 2
    proc, data = _run(tmp_path, TEXT, SSN_CLASS, "--strict-classes")
    assert proc.returncode == 1 and data["findings"] and data["notices"] == []


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


def test_fail_fast_reads_on_past_a_notice_to_a_listed_value(tmp_path):
    # A class hit on page 1 must not end the scan: the listed value is on
    # page 2, in an embedded Identity-H font, so only the Text (or OCR)
    # layer can read it (review round 1, finding 1: this exited 0).
    from caselib.pdfkit import embedded_font

    pdf = tmp_path / "f.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((50, 80), "Applicant 234-56-7890")
    page = doc.new_page()
    page.insert_text((50, 80), "Codename Zebraquokka", fontname=embedded_font(page))
    doc.save(str(pdf))
    doc.close()
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps(SSN_CLASS + [{"name": "Target", "value": "Zebraquokka"}]))
    out = tmp_path / "o.json"
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "verify.py"), "--target", str(pdf),
                           "--secrets", str(rp), "--json", str(out),
                           "--no-strict-classes", "--fail-fast"],
                          capture_output=True, text=True, timeout=300)
    data = json.loads(out.read_text())
    assert proc.returncode == 1
    assert {f["rule"] for f in data["findings"]} == {"Target"}
    assert ("Target", "Text") in {(f["rule"], f["layer"]) for f in data["findings"]}


def test_a_malformed_strict_is_refused_only_when_classes_inform(tmp_path):
    rules = [{"name": "Any SSN", "class": "ssn", "strict": "yes"}]
    proc, data = _run(tmp_path, TEXT, rules, "--strict-classes")
    assert proc.returncode == 1  # strict mode: loads as before
    proc, data = _run(tmp_path, TEXT, rules)  # the default: classes inform
    assert proc.returncode == 2 and data["error"]["code"] == "RULES_INVALID"


# ── a redactor config's entity_types: not checked (owner, 2026-10-16) ────

INFO_LINE = "only your listed exact_values and patterns are"


def _clean_here(data):
    """The run's exit once this machine's missing tools are set aside (no
    OCR on Linux CI gives 2 for that reason alone)."""
    from caselib.run import available
    from scorecard.differential import normalization_have
    from scorecard.keys import effective_exit

    return effective_exit(data, normalization_have(available()))


@pytest.mark.parametrize("flag", ["--no-strict-classes", "--strict-classes"])
def test_listed_entity_types_give_clean_plus_the_info_line(tmp_path, flag):
    # person_name used to add SCOPE_UNVERIFIABLE (exit 2) and ssn a class
    # rule (a notice, or exit 1 under --strict-classes). Now neither is
    # checked, with or without --strict-classes: only the listed value is,
    # and it is not in the file.
    cfg = 'entity_types:\n  - person_name\n  - ssn\n  - phone\nexact_values:\n  - "Q7-ZEBRA"\n'
    proc, data = _run(tmp_path, TEXT, cfg, flag)
    assert proc.returncode == data["exit_code"]
    assert _clean_here(data) == 0
    assert data["findings"] == [] and data["notices"] == []
    assert not any(w["code"].startswith("SCOPE_") for w in data["warnings"])
    (info,) = data["info"]
    assert info["code"] == "ENTITY_TYPES_NOT_CHECKED"
    assert info["entity_types"] == ["person_name", "ssn", "phone"]
    assert info["by_default"] is False and INFO_LINE in info["message"]
    flat = " ".join(proc.stdout.split())
    assert "FOR YOUR INFORMATION (does not change the result above)" in flat
    assert "Entity types in your config (person_name, ssn, phone) are not checked" in flat
    # After the verdict banner, never one of its lines.
    assert flat.index("FOR YOUR INFORMATION") > flat.index("=" * 70)
    assert "STILL PRESENT" not in proc.stdout


def test_omitted_entity_types_give_the_info_line_for_all_ten(tmp_path):
    proc, data = _run(tmp_path, TEXT, 'exact_values:\n  - "Q7-ZEBRA"\n')
    assert _clean_here(data) == 0 and data["notices"] == []
    (info,) = data["info"]
    assert info["by_default"] is True and len(info["entity_types"]) == 10
    flat = " ".join(proc.stdout.split())
    assert "does not list entity_types, which a redaction tool reads as all ten" in flat
    assert INFO_LINE in flat


def test_an_empty_entity_types_list_gives_no_info_line(tmp_path):
    proc, data = _run(tmp_path, TEXT, 'entity_types: []\nexact_values:\n  - "Q7-ZEBRA"\n')
    assert data["info"] == [] and "FOR YOUR INFORMATION" not in proc.stdout


def test_a_listed_exact_value_is_still_found_beside_entity_types(tmp_path):
    # The info line never stands in for a listed value: still exit 1.
    cfg = 'entity_types:\n  - person_name\n  - ssn\nexact_values:\n  - "234-56-7890"\n'
    proc, data = _run(tmp_path, TEXT, cfg)
    assert proc.returncode == 1
    assert {f["rule"] for f in data["findings"]} == {"exact_values[0]"}
    assert data["info"] and data["info"][0]["code"] == "ENTITY_TYPES_NOT_CHECKED"


def test_a_listed_pattern_is_still_found_beside_entity_types(tmp_path):
    cfg = "entity_types: [ssn]\npatterns:\n  - '\\d{3}-\\d{2}-\\d{4}'\n"
    proc, data = _run(tmp_path, TEXT, cfg)
    assert proc.returncode == 1
    assert {f["rule"] for f in data["findings"]} == {"patterns[0]"}


def test_a_json_class_rule_is_unchanged(tmp_path):
    # Policy 1 (ADR 0012) for this tool's own rules format: a notice by
    # default, a finding under --strict-classes; never an info line.
    proc, data = _run(tmp_path, TEXT, SSN_CLASS)
    assert data["findings"] == [] and {n["rule"] for n in data["notices"]} == {"Any SSN"}
    assert data["info"] == []
    proc, data = _run(tmp_path, TEXT, SSN_CLASS, "--strict-classes")
    assert proc.returncode == 1 and {f["rule"] for f in data["findings"]} == {"Any SSN"}


def test_a_warning_notice_keeps_every_warning_field():
    from redaction_verifier.report.json import build_json_report

    r = ScanReport(informational=frozenset({"Any SSN"}))
    r.warn("REVIEW_METADATA_RAW", "Metadata", "raw output matches pattern rule 'Any SSN' "
           "(sample ****7890)", rule="Any SSN", tool="exiftool", adjacency="NOISY_SOURCE")
    r.record("Text", "Any SSN", "page 1", "234-56-7890")
    r.settle_notices()
    data = build_json_report(r, 0, target=Path("f.pdf"), tool_version="x",
                             ocr_available=True)
    hard, review = sorted(data["notices"], key=lambda n: n["tier"])
    assert set(hard) == set(review)  # one shape
    assert review["tier"] == "review" and review["warning_kind"] == "review"
    assert review["code"] == "REVIEW_METADATA_RAW"
    assert review["tool"] == "exiftool" and review["adjacency"] == "NOISY_SOURCE"
    assert hard["tier"] == "hard" and hard["warning_kind"] is None
    assert hard["sample"] == "****7890"
    assert data["findings"] == [] and data["warnings"] == []


def test_the_text_report_lists_a_warning_notice_after_the_verdict(capsys):
    from redaction_verifier.report.text import print_report

    r = ScanReport(informational=frozenset({"Any SSN"}))
    r.warn("REVIEW_FUSED_PATTERN", "Text", "Text: sequence matching pattern rule 'Any SSN'",
           rule="Any SSN")
    r.settle_notices()
    print_report(r, Path("f.pdf"))
    out = capsys.readouterr().out
    assert out.index("[PASS]") < out.index("NOT COUNTED IN THE VERDICT")
    assert "sequence matching pattern rule" in out.split("NOT COUNTED")[1]
    assert "STILL PRESENT" not in out


def test_a_notice_never_reveals_part_of_a_listed_value(tmp_path):
    # The class matches the listed value's own text (and a decoy ending in
    # its digits): a notice is masked like a finding, so no tail shows.
    rules = SSN_CLASS + [{"name": "Target", "value": "Zebra 6789"}]
    proc, data = _run(tmp_path, "Applicant 234-56-7890 and Zebra 6789", rules,
                      "--no-strict-classes")
    hard = [n for n in data["notices"] if n["tier"] == "hard"]
    assert hard and all(n["sample"] == "****" for n in hard)
    notices_text = proc.stdout.split("NOT COUNTED IN THE VERDICT")[1]
    assert "6789" not in notices_text and "7890" not in notices_text
