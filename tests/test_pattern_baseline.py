"""Tests for Phase 5's false-hard baseline (eval/scorecard/match_shapes.py,
pattern_recorder.py, pattern_baseline.py): the shape classifier, the
join of findings to shapes, the counts and residual estimates, and that
the recording wrapper leaves the CLI's verdict and report exactly as
verify.py's own.

Every value here is fabricated.
"""

from __future__ import annotations

import json
import os
import re
import sys

import pymupdf as fitz
import pytest

from caselib import REGISTRY, load
from caselib.run import available, build, scan
from scorecard.match_shapes import OUT_ENV, SALT_ENV, SHAPES, classify, record_key
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
        ("ssn", "id 1b4e28ba-2fa1-11d2-883f-016123456789 x", "123456789", "uuid"),
        ("ssn", "hash a9f3c2123456789e0 end", "123456789", "hex-id"),
        ("ssn", "Fee123456789 due", "123456789", "hex-id"),
        ("ssn", "part XK123456789Z", "123456789", "alnum-id"),
        # dates need context: a PDF D: prefix, a timestamp token, epoch ms
        ("ssn", "(D:202301159)", "202301159", "date-or-time"),
        ("credit-card", "ms 1700000000000 x", "1700000000000", "date-or-time"),
        ("ssn", "built 202301159 ok", "202301159", "bare-digits"),
        ("us-phone", "t=2023456789 s", "2023456789", "bare-digits"),  # epoch s, or a phone
        ("us-phone", "call 2125550147 now", "2125550147", "bare-digits"),
        ("ssn", "total 987654321 units", "987654321", "bare-digits"),
        ("ssn", "ID-123456789", "123456789", "bare-digits"),  # a token stops at '-'
        # real groupings are "formatted", even with a letter or word touching
        ("ssn", "no. 201-01-1234 filed", "201-01-1234", "formatted"),
        ("ssn", "SSN123-45-6789", "123-45-6789", "formatted"),
        ("ssn", "ID-123-45-6789", "123-45-6789", "formatted"),
        ("ssn", "Fee123-45-6789", "123-45-6789", "formatted"),
        ("us-phone", "212-555-0147-ext", "212-555-0147", "formatted"),
        ("us-phone", "call (212) 555-0147 now", "(212) 555-0147", "formatted"),
        ("us-phone", "t +1 212-555-0147", "1 212-555-0147", "formatted"),
        ("credit-card", "card 4111 1111 1111 1111 x", "4111 1111 1111 1111", "formatted"),
        ("credit-card", "m [0 1 0 0 1 2 0 0 1 0 0 1 3] x", "0 1 0 0 1 2 0 0 1 0 0 1 3",
         "number-list"),
        ("us-phone", "v 212 5550147 x", "212 5550147", "number-list"),
    ],
)
def test_classify_shapes(cls, text, matched, shape):
    assert shape in SHAPES
    assert _shape(cls, text, matched) == shape


def test_record_key_depends_on_salt_rule_and_sample():
    k = record_key("s", "ssn", "****6789")
    assert k != record_key("t", "ssn", "****6789")
    assert k != record_key("s", "us-phone", "****6789")
    assert k != record_key("s", "ssn", "****6780")
    assert "6789" not in k


# ── pattern_baseline.reduce_result / aggregate ──────────────────────────


def _result(findings=(), warnings=(), exit_code=1, error=None):
    report = {"exit_code": exit_code, "error": error,
              "findings": list(findings), "warnings": list(warnings)}
    return CliResult(returncode=exit_code, report=report, elapsed=0.1, timed_out=False,
                     stderr_tail="")


def _finding(rule, layer="Text", sample="****6789", storage="live", tier="hard"):
    return {"rule": rule, "layer": layer, "sample": sample, "storage": storage, "tier": tier}


def _joins(*triples, salt="s"):
    return [{"key": record_key(salt, rule, sample), "shape": shape}
            for rule, sample, shape in triples]


def test_reduce_joins_shape_by_salted_rule_and_masked_sample():
    result = _result([_finding("ssn"), _finding("email", sample="****.org")])
    records = {"joins": _joins(("ssn", "****6789", "uuid"),
                               ("email", "****.org", "email-address"),
                               # same masked sample, other rule: must not join to ssn
                               ("us-phone", "****6789", "formatted")),
               "shapes": {"ssn": ["uuid", "bare-digits"]}}
    r = reduce_result("text-bearing", result, records, "s")
    assert r.measured
    assert {(h.rule, h.shape) for h in r.hard} == {("ssn", "uuid"), ("email", "email-address")}
    assert r.match_shapes == {"ssn": frozenset({"uuid", "bare-digits"})}
    # the wrong salt joins nothing
    assert {h.shape for h in reduce_result("text-bearing", result, records, "x").hard} == {UNJOINED}


def test_reduce_takes_the_shape_kept_longest_when_a_key_has_several():
    records = {"joins": _joins(("ssn", "****6789", "hex-id"), ("ssn", "****6789", "bare-digits"),
                               ("ssn", "****6789", "uuid"))}
    r = reduce_result("text-bearing", _result([_finding("ssn")]), records, "s")
    assert [h.shape for h in r.hard] == ["bare-digits"]


def test_reduce_counts_a_finding_without_a_record_as_unjoined():
    r = reduce_result("text-bearing", _result([_finding("ssn")]), {}, "s")
    assert [h.shape for h in r.hard] == [UNJOINED]


def test_reduce_ignores_non_class_rules_and_keeps_review_rules():
    result = _result([_finding("Target SSN")],
                     warnings=[{"rule": "us-phone", "code": "X"}, {"rule": None, "code": "Y"}])
    r = reduce_result("text-bearing", result, {}, "s")
    assert r.hard == ()
    assert r.review_rules == frozenset({"us-phone"})


def test_reduce_marks_a_crash_or_cli_error_unmeasured():
    crashed = CliResult(returncode=None, report=None, elapsed=1.0, timed_out=True, stderr_tail="")
    r = reduce_result("text-bearing", crashed, {}, "s")
    assert (r.measured, r.unmeasured_reason) == (False, "crash")
    locked = _result(exit_code=2, error={"code": "PDF_PASSWORD", "message": "x"})
    r = reduce_result("text-bearing", locked, {}, "s")
    assert (r.measured, r.unmeasured_reason) == (False, "PDF_PASSWORD")


def test_reduce_keeps_an_error_report_that_has_findings_measured():
    late_crash = _result([_finding("ssn")], exit_code=1,
                         error={"code": "INTERNAL_ERROR", "message": "x"})
    r = reduce_result("text-bearing", late_crash, {}, "s")
    assert r.measured and [h.rule for h in r.hard] == ["ssn"]


def _fr(*hard, shapes=None, stratum="text-bearing", exit_code=1):
    return FileResult(stratum, True, exit_code, tuple(hard),
                      match_shapes={k: frozenset(v) for k, v in (shapes or {}).items()})


def test_aggregate_counts_files_per_class_layer_shape_and_union():
    results = [
        _fr(HardFinding("ssn", "Text", "live", "uuid"),
            HardFinding("ssn", "Objects", "live", "uuid"), shapes={"ssn": {"uuid"}}),
        _fr(HardFinding("us-phone", "Metadata", None, "formatted"),
            shapes={"us-phone": {"formatted"}}),
        _fr(HardFinding("email", "Text", "live", "email-address")),
        _fr(exit_code=0),
        FileResult("text-bearing", False, None, unmeasured_reason="crash"),
        _fr(stratum="text-free", exit_code=0),
    ]
    agg = aggregate(results)
    tb = agg["text-bearing"]
    assert (tb["files"], tb["measured"], tb["unmeasured"]) == (5, 4, 1)
    assert tb["unmeasured_by_reason"] == {"crash": 1}
    ssn = tb["classes"]["ssn"]
    assert ssn["hard_files"] == 1 and ssn["hard_pct"] == 25.0
    assert ssn["hard_files_by_layer"] == {"Objects": 1, "Text": 1}
    assert ssn["hard_pct_by_layer"] == {"Objects": 25.0, "Text": 25.0}
    assert ssn["hard_files_by_shape"] == {"uuid": 1}
    assert ssn["hard_findings_by_shape"] == {"uuid": 2}
    assert ssn["hard_files_by_layer_and_shape"] == {"Objects": {"uuid": 1}, "Text": {"uuid": 1}}
    assert tb["unions"]["ssn|us-phone"]["hard_files"] == 2
    residual = tb["unions"]["ssn|us-phone"]["residual_hard_files"]
    # uuid is demoted under every option; the formatted phone never is.
    assert {k: v["files"] for k, v in residual.items()} == {
        "demote-embedded-and-dates": 1, "also-number-lists": 1, "also-bare-digits": 1}
    assert ssn["residual_hard_files"]["also-bare-digits"]["files"] == 0
    assert tb["unions"]["any built-in"]["hard_files"] == 3
    assert tb["hard_files_by_layer"] == {"Metadata": 1, "Objects": 1, "Text": 2}
    assert agg["all"]["measured"] == 5
    assert set(agg) == {"all", "text-bearing", "text-free"}


def test_residuals_read_every_match_not_only_the_reported_one():
    # The finding's own sample sits in a UUID, but the same class also
    # matched a formatted SSN later (the tool reports only its first match
    # per page): demoting embedded shapes would still leave the file hard.
    results = [_fr(HardFinding("ssn", "Text", "live", "uuid"),
                   shapes={"ssn": {"uuid", "formatted"}})]
    residual = aggregate(results)["text-bearing"]["classes"]["ssn"]["residual_hard_files"]
    assert all(v["files"] == 1 for v in residual.values())


def test_residuals_by_option_and_unknown_shapes_are_never_demoted():
    results = [
        _fr(HardFinding("ssn", "Text", "live", "bare-digits"), shapes={"ssn": {"bare-digits"}}),
        _fr(HardFinding("ssn", "Text", "live", "number-list"), shapes={"ssn": {"number-list"}}),
        _fr(HardFinding("ssn", "Text", "live", UNJOINED)),
        _fr(HardFinding("ssn", "Text", "live", "hex-id"), shapes={"ssn": {"classifier-error"}}),
    ]
    residual = aggregate(results)["text-bearing"]["classes"]["ssn"]["residual_hard_files"]
    assert {k: v["files"] for k, v in residual.items()} == {
        "demote-embedded-and-dates": 4, "also-number-lists": 3, "also-bare-digits": 2}


def test_residuals_never_demote_an_email_address():
    results = [_fr(HardFinding("email", "Text", "live", "email-address"))]
    union = aggregate(results)["text-bearing"]["unions"]["any built-in"]["residual_hard_files"]
    assert all(v["files"] == 1 for v in union.values())


def test_residuals_only_count_files_hard_today():
    # A kept-shape match with no hard finding of the class (a review-tier
    # call only) does not make a file hard.
    results = [_fr(shapes={"ssn": {"formatted"}}, exit_code=2)]
    residual = aggregate(results)["text-bearing"]["classes"]["ssn"]["residual_hard_files"]
    assert all(v["files"] == 0 for v in residual.values())


def test_builtin_rules_file_is_exactly_the_four_classes():
    rules = json.loads(BUILTIN_RULES.read_text())
    assert sorted(r["class"] for r in rules) == sorted(CLASSES)
    assert all(r["name"] == r["class"] for r in rules)  # findings' rule == class name


# ── pattern_recorder: the CLI, unchanged ────────────────────────────────


def _pdf(path, lines=(), password=None):
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((50, 80 + 40 * i), line)
    if password:
        doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password,
                 owner_pw=password + "-owner")
    else:
        doc.save(str(path))
    doc.close()
    return path


def _both(tmp_path, pdf):
    rules = json.loads(BUILTIN_RULES.read_text())
    sidecar = tmp_path / "records.json"
    plain = run_cli(sys.executable, REPO_ROOT / "verify.py", pdf, rules, tmp_path / "a", "f",
                    timeout=300)
    recorded = run_cli(sys.executable, RECORDER, pdf, rules, tmp_path / "b", "f", timeout=300,
                       env={**os.environ, OUT_ENV: str(sidecar), SALT_ENV: "s"})
    assert not plain.crashed and not recorded.crashed
    assert plain.returncode == recorded.returncode
    for name in ("exit_code", "error", "findings", "warnings"):
        assert plain.report[name] == recorded.report[name], name
    return recorded, sidecar


def test_recorder_cli_matches_verify_on_a_finding(tmp_path):
    pdf = _pdf(tmp_path / "f.pdf", [
        "Ref 1b4e28ba-2fa1-11d2-883f-a234567890ab and 345678901",
        "Call (212) 555-0147 or write to a.b@example.org"])
    recorded, sidecar = _both(tmp_path, pdf)
    assert recorded.returncode == 1
    text = sidecar.read_text()
    records = json.loads(text)
    # Neither matched text nor masked sample is written: salted keys and shapes.
    assert set(records) == {"joins", "shapes"}
    assert all(set(r) == {"key", "shape"} for r in records["joins"])
    for leak in ("example", "0147", "6789", "****"):
        assert leak not in text
    joins = {(r["key"], r["shape"]) for r in records["joins"]}
    assert (record_key("s", "email", "****.org"), "email-address") in joins
    assert (record_key("s", "us-phone", "****0147"), "formatted") in joins
    assert {"uuid", "bare-digits"} <= set(records["shapes"]["ssn"])
    joined = reduce_result("text-bearing", recorded, records, "s")
    assert joined.hard and all(h.shape != UNJOINED for h in joined.hard)


def test_recorder_cli_matches_verify_on_a_clean_file(tmp_path):
    recorded, sidecar = _both(tmp_path, _pdf(tmp_path / "c.pdf", ["Nothing to see here."]))
    assert recorded.returncode == 0
    assert json.loads(sidecar.read_text()) == {"joins": [], "shapes": {}}


def test_recorder_cli_matches_verify_on_a_file_it_cannot_open(tmp_path):
    pdf = _pdf(tmp_path / "e.pdf", ["123-45-6789"], password="fabricated")
    recorded, _ = _both(tmp_path, pdf)
    assert recorded.returncode == 2
    assert recorded.report["error"]["code"] == "PDF_PASSWORD"


def test_recorder_records_every_validated_match_not_only_the_first():
    from redaction_verifier.matching import patterns

    regex, validator = patterns.BUILTIN_PATTERN_CLASSES["ssn"]
    ssn = patterns.PatternRule("ssn", re.compile(regex), validator)
    recorder = Recorder("s")
    recorder.install()
    try:
        hits = patterns.match_patterns("id deadbeef234567890 then 234-56-7890", [ssn])
        # a rule already hit is not reported again, but its matches are seen
        assert patterns.match_patterns("later 345-67-8901", [ssn], {"ssn"}) == {}
        patterns.match_patterns("collapsed 456-78-9012", [ssn], collapse_separators=True)
    finally:
        recorder.uninstall()
    assert hits == {"ssn": "234567890"}
    assert recorder.shapes == {"ssn": {"hex-id", "formatted"}}  # collapsed calls ignored
    assert set(recorder.joins) == {(record_key("s", "ssn", "****7890"), "hex-id")}


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
    recorder = Recorder("s")
    recorder.install()
    try:
        recorded = scan(case, pdf, tmp_path)
    finally:
        recorder.uninstall()
    assert recorded.exit_code == plain.exit_code
    for name in ("exit_code", "error", "findings", "warnings"):
        assert recorded.report[name] == plain.report[name], name
    # And every class finding joins to a shape.
    keys = {key for key, _ in recorder.joins}
    classes = {r["name"] for r in case.rules if "class" in r}
    for f in recorded.report["findings"]:
        if f["rule"] in classes:
            assert record_key("s", f["rule"], f["sample"]) in keys, f


def test_recorder_uninstall_restores_match_patterns(tmp_path):
    import verify
    from redaction_verifier import matching
    from redaction_verifier.matching import patterns

    before = (patterns.match_patterns, matching.match_patterns)
    recorder = Recorder()
    recorder.install()
    assert patterns.match_patterns is not before[0]
    recorder.uninstall()
    assert (patterns.match_patterns, matching.match_patterns) == before
    # verify.py's own binding (its call sites look the name up there) is
    # restored too: a full scan after uninstall records nothing.
    pdf = _pdf(tmp_path / "u.pdf", ["Call (212) 555-0147"])
    code = verify.main(["--target", str(pdf), "--secrets", str(BUILTIN_RULES),
                        "--json", str(tmp_path / "u.json")])
    assert code == 1
    assert not recorder.joins and not recorder.shapes


def test_recorder_returns_the_hits_object_untouched_even_if_classifying_fails(monkeypatch):
    import scorecard.match_shapes as shapes
    from redaction_verifier.matching import patterns

    rule = patterns.PatternRule("ssn", re.compile(r"\d{9}"))
    expected = patterns.match_patterns("n 987654321", [rule])
    monkeypatch.setattr(shapes, "classify", lambda *a: 1 / 0)
    recorder = Recorder()
    recorder.install()
    try:
        got = patterns.match_patterns("n 987654321", [rule])
    finally:
        recorder.uninstall()
    assert got == expected
    assert recorder.shapes == {"ssn": {"classifier-error"}}
    assert [shape for _, shape in recorder.joins] == ["classifier-error"]


def test_keep_order_ranks_every_shape():
    from scorecard.pattern_baseline import KEEP_ORDER

    assert set(KEEP_ORDER) == set(SHAPES) | {"classifier-error"}
