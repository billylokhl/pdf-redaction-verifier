"""Tests for Phase 5's false-hard baseline (eval/scorecard/match_shapes.py,
pattern_recorder.py, pattern_baseline.py): the shape classifier, the
join of findings to shapes, the counts, and that the recording wrapper
leaves the CLI's verdict and report exactly as verify.py's own.

Every value here is fabricated.
"""

from __future__ import annotations

import json
import os
import sys

import pymupdf as fitz
import pytest

from caselib import REGISTRY, load
from caselib.run import available, build, scan
from scorecard.match_shapes import OUT_ENV, SHAPES, classify, record_key
from scorecard.pattern_baseline import (
    BUILTIN_RULES,
    CLASSES,
    RECORDER,
    UNJOINED,
    FileResult,
    HardFinding,
    aggregate,
    reduce_result,
)
from scorecard.pattern_recorder import Recorder
from scorecard.runner import CliResult, run_cli

from .conftest import REPO_ROOT

# ── match_shapes.classify ───────────────────────────────────────────────


def _shape(cls: str, text: str, matched: str) -> str:
    start = text.index(matched)
    return classify(cls, text, start, start + len(matched))


@pytest.mark.parametrize(
    ("cls", "text", "matched", "shape"),
    [
        ("email", "write to a.b@example.org today", "a.b@example.org", "email-address"),
        # nine digits inside a UUID's last group, and a dashed run across groups
        ("ssn", "id 1b4e28ba-2fa1-11d2-883f-016123456789 x", "123456789", "uuid"),
        ("ssn", "hash a9f3c2123456789e0 end", "123456789", "hex-id"),
        ("ssn", "built 202301159 ok", "202301159", "date-or-time"),
        ("ssn", "stamp 120230115 ok", "120230115", "date-or-time"),
        ("ssn", "(D:202301159)", "202301159", "date-or-time"),
        ("us-phone", "t=2023456789 s", "2023456789", "date-or-time"),
        ("credit-card", "ms 1700000000000 x", "1700000000000", "date-or-time"),
        ("ssn", "part XK123456789Z", "123456789", "alnum-id"),
        ("ssn", "no. 201-01-1234 filed", "201-01-1234", "formatted"),
        ("us-phone", "call (212) 555-0147 now", "(212) 555-0147", "formatted"),
        ("ssn", "total 987654321 units", "987654321", "bare-digits"),
        ("credit-card", "card 4111 1111 1111 1111 x", "4111 1111 1111 1111", "formatted"),
        ("credit-card", "m [0 1 0 0 1 2 0 0 1 0 0 1 3] x", "0 1 0 0 1 2 0 0 1 0 0 1 3", "number-list"),
        ("us-phone", "v 212 5550147 x", "212 5550147", "number-list"),
        ("us-phone", "t +1 212-555-0147", "1 212-555-0147", "formatted"),
    ],
)
def test_classify_shapes(cls, text, matched, shape):
    assert shape in SHAPES
    assert _shape(cls, text, matched) == shape


def test_formatted_digits_that_spell_a_date_stay_formatted():
    # 201-01-1234's digits begin 20101123, a valid YYYYMMDD; the dashes
    # make it look like a real SSN, which is the shape that matters.
    assert _shape("ssn", "x 201-01-1234 y", "201-01-1234") == "formatted"


# ── pattern_baseline.reduce_result / aggregate ──────────────────────────


def _result(findings=(), warnings=(), exit_code=1, returncode=None):
    report = {"exit_code": exit_code, "findings": list(findings), "warnings": list(warnings)}
    return CliResult(returncode=exit_code if returncode is None else returncode,
                     report=report, elapsed=0.1, timed_out=False, stderr_tail="")


def _finding(rule, layer="Text", sample="****6789", storage="live", tier="hard"):
    return {"rule": rule, "layer": layer, "sample": sample, "storage": storage, "tier": tier}


def test_reduce_joins_shape_by_rule_and_masked_sample():
    result = _result([_finding("ssn"), _finding("email", sample="****.org")])
    records = [
        {"key": record_key("ssn", "****6789"), "shape": "uuid", "collapsed": False},
        {"key": record_key("email", "****.org"), "shape": "email-address", "collapsed": False},
        # same masked sample, other rule: must not join to the ssn finding
        {"key": record_key("us-phone", "****6789"), "shape": "formatted", "collapsed": False},
    ]
    r = reduce_result("text-bearing", result, records)
    assert r.measured
    assert {(h.rule, h.shape) for h in r.hard} == {("ssn", "uuid"), ("email", "email-address")}


def test_reduce_counts_a_finding_without_a_record_as_unjoined():
    r = reduce_result("text-bearing", _result([_finding("ssn")]), [])
    assert [h.shape for h in r.hard] == [UNJOINED]


def test_reduce_ignores_non_class_rules_and_keeps_review_rules():
    result = _result([_finding("Target SSN")],
                     warnings=[{"rule": "us-phone", "code": "X"}, {"rule": None, "code": "Y"}],
                     exit_code=1)
    r = reduce_result("text-bearing", result, [])
    assert r.hard == ()
    assert r.review_rules == frozenset({"us-phone"})


def test_reduce_marks_a_crash_unmeasured():
    crashed = CliResult(returncode=None, report=None, elapsed=1.0, timed_out=True, stderr_tail="")
    assert not reduce_result("text-bearing", crashed, []).measured


def test_aggregate_counts_files_per_class_layer_shape_and_union():
    results = [
        FileResult("text-bearing", True, 1, (
            HardFinding("ssn", "Text", "live", "uuid"),
            HardFinding("ssn", "Objects", "live", "uuid"),
        )),
        FileResult("text-bearing", True, 1, (HardFinding("us-phone", "Metadata", None, "formatted"),)),
        FileResult("text-bearing", True, 1, (HardFinding("email", "Text", "live", "email-address"),)),
        FileResult("text-bearing", True, 0),
        FileResult("text-bearing", False, None),
        FileResult("text-free", True, 0),
    ]
    agg = aggregate(results)
    tb = agg["text-bearing"]
    assert (tb["files"], tb["measured"], tb["unmeasured"]) == (5, 4, 1)
    ssn = tb["classes"]["ssn"]
    assert ssn["hard_files"] == 1 and ssn["hard_pct"] == 25.0
    assert ssn["hard_files_by_layer"] == {"Objects": 1, "Text": 1}
    assert ssn["hard_files_by_shape"] == {"uuid": 1}
    assert ssn["hard_findings_by_shape"] == {"uuid": 2}
    assert ssn["hard_files_by_layer_and_shape"] == {"Objects": {"uuid": 1}, "Text": {"uuid": 1}}
    assert ssn["hard_pct_by_layer"] == {"Objects": 25.0, "Text": 25.0}
    assert tb["unions"]["ssn|us-phone"]["hard_files"] == 2
    residual = tb["unions"]["ssn|us-phone"]["residual_hard_files"]
    # uuid is demoted under every option; the formatted phone never is.
    assert {k: v["files"] for k, v in residual.items()} == {
        "demote-embedded-and-dates": 1, "also-number-lists": 1, "also-bare-digits": 1}
    assert tb["classes"]["ssn"]["residual_hard_files"]["also-bare-digits"]["files"] == 0
    assert tb["unions"]["any built-in"]["hard_files"] == 3
    assert tb["hard_files_by_layer"] == {"Metadata": 1, "Objects": 1, "Text": 2}
    assert agg["all"]["measured"] == 5
    assert set(agg) == {"all", "text-bearing", "text-free"}


def test_builtin_rules_file_is_exactly_the_four_classes():
    rules = json.loads(BUILTIN_RULES.read_text())
    assert sorted(r["class"] for r in rules) == sorted(CLASSES)
    assert all(r["name"] == r["class"] for r in rules)  # findings' rule == class name


# ── pattern_recorder: the CLI, unchanged ────────────────────────────────


def test_recorder_report_and_exit_match_verify_exactly(tmp_path):
    pdf = tmp_path / "fabricated.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 80), "Ref 1b4e28ba-2fa1-11d2-883f-016123456789 and 987654321")
    page.insert_text((50, 120), "Call (212) 555-0147 or write to a.b@example.org")
    doc.save(str(pdf))
    doc.close()
    rules = json.loads(BUILTIN_RULES.read_text())
    sidecar = tmp_path / "records.json"
    plain = run_cli(sys.executable, REPO_ROOT / "verify.py", pdf, rules, tmp_path / "a", "f",
                    timeout=300)
    recorded = run_cli(sys.executable, RECORDER, pdf, rules, tmp_path / "b", "f", timeout=300,
                       env={**os.environ, OUT_ENV: str(sidecar)})
    assert not plain.crashed and not recorded.crashed
    assert plain.returncode == recorded.returncode == 1
    assert plain.report["findings"] == recorded.report["findings"]
    assert plain.report["warnings"] == recorded.report["warnings"]
    records = json.loads(sidecar.read_text())
    assert records, "the recorder wrote no records"
    # Neither matched text nor masked sample is written: a key, a shape, a flag.
    assert all(set(r) == {"key", "shape", "collapsed"} for r in records)
    assert "example" not in sidecar.read_text() and "0147" not in sidecar.read_text()
    shapes = {(r["key"], r["shape"]) for r in records}
    assert (record_key("email", "****.org"), "email-address") in shapes
    assert (record_key("us-phone", "****0147"), "formatted") in shapes
    joined = reduce_result("text-bearing", recorded, records)
    assert joined.hard and all(h.shape != UNJOINED for h in joined.hard)


# ── Recorder parity over the case library ──────────────────────────────
# Every case with a pattern rule, scanned in-process with and without the
# recorder installed: the report must be identical, so the measurement
# can never be of a different tool than the one that ships.

load()
_HAVE = available()


def _pattern_cases():
    for case_id, case in sorted(REGISTRY.items()):
        if not any("class" in r or "pattern" in r for r in case.rules):
            continue
        marks = []
        missing = case.requires - _HAVE
        if missing:
            marks.append(pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}"))
        if case.grid:
            marks.append(pytest.mark.grid)
        if case.perf:
            marks.append(pytest.mark.perf)
            if os.environ.get("RUN_PERF") != "1":
                marks.append(pytest.mark.skip(reason="perf case; set RUN_PERF=1 to run"))
        yield pytest.param(case, id=case_id, marks=marks)


@pytest.mark.parametrize("case", list(_pattern_cases()))
def test_recorder_leaves_the_report_unchanged(case, tmp_path):
    pdf = build(case, tmp_path / f"{case.id}.pdf")
    plain = scan(case, pdf, tmp_path)
    recorder = Recorder()
    recorder.install()
    try:
        recorded = scan(case, pdf, tmp_path)
    finally:
        recorder.uninstall()
    assert recorded.exit_code == plain.exit_code
    for field in ("exit_code", "error", "findings", "warnings"):
        assert recorded.report[field] == plain.report[field], field
    # And every class finding joins to a shape.
    keys = {key for key, _, _ in recorder.records}
    for f in recorded.report["findings"]:
        if f["rule"] in {r["name"] for r in case.rules if "class" in r}:
            assert record_key(f["rule"], f["sample"]) in keys, f


def test_recorder_uninstall_restores_match_patterns():
    import verify
    from redaction_verifier import matching
    from redaction_verifier.matching import patterns

    before = (patterns.match_patterns, matching.match_patterns, verify.match_patterns)
    recorder = Recorder()
    recorder.install()
    assert patterns.match_patterns is not before[0]
    recorder.uninstall()
    assert (patterns.match_patterns, matching.match_patterns, verify.match_patterns) == before


def test_recorder_returns_the_hits_object_untouched_even_if_classifying_fails(monkeypatch):
    from redaction_verifier.matching import patterns
    import scorecard.match_shapes as shapes

    rule = patterns.PatternRule("ssn", __import__("re").compile(r"\d{9}"))
    expected = patterns.match_patterns("n 987654321", [rule])
    monkeypatch.setattr(shapes, "classify", lambda *a: 1 / 0)
    recorder = Recorder()
    recorder.install()
    try:
        got = patterns.match_patterns("n 987654321", [rule])
    finally:
        recorder.uninstall()
    assert got == expected
    assert [shape for _, shape, _ in recorder.records] == ["classifier-error"]


def test_residuals_keep_bare_digits_until_the_last_option_and_never_demote_unknown():
    results = [
        FileResult("text-bearing", True, 1, (HardFinding("ssn", "Text", "live", "bare-digits"),)),
        FileResult("text-bearing", True, 1, (HardFinding("ssn", "Text", "live", "number-list"),)),
        FileResult("text-bearing", True, 1, (HardFinding("ssn", "Text", "live", UNJOINED),)),
    ]
    residual = aggregate(results)["text-bearing"]["classes"]["ssn"]["residual_hard_files"]
    assert {k: v["files"] for k, v in residual.items()} == {
        "demote-embedded-and-dates": 3, "also-number-lists": 2, "also-bare-digits": 1}


def test_residuals_never_demote_an_email_address():
    results = [FileResult("text-bearing", True, 1,
                          (HardFinding("email", "Text", "live", "email-address"),))]
    union = aggregate(results)["text-bearing"]["unions"]["any built-in"]["residual_hard_files"]
    assert all(v["files"] == 1 for v in union.values())
