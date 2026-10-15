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

from scorecard.match_shapes import OUT_ENV, SHAPES, classify
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
        {"rule": "ssn", "sample": "****6789", "shape": "uuid", "collapsed": False},
        {"rule": "email", "sample": "****.org", "shape": "email-address", "collapsed": False},
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
    assert tb["unions"]["ssn|us-phone"]["hard_files"] == 2
    assert tb["unions"]["ssn|us-phone"]["hard_files_only_uuid_hex_date"] == 1
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
    # No matched text is written: only rule, masked sample, shape, flag.
    assert all(set(r) == {"rule", "sample", "shape", "collapsed"} for r in records)
    assert all(r["sample"].startswith("****") for r in records)
    by_rule = {(r["rule"], r["shape"]) for r in records}
    assert ("email", "email-address") in by_rule
    assert ("us-phone", "formatted") in by_rule
    joined = reduce_result("text-bearing", recorded, records)
    assert joined.hard and all(h.shape != UNJOINED for h in joined.hard)
