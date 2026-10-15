"""Tests for Phase 5's recall-cost measurement (eval/scorecard/pattern_recall.py)
and the rule-name-to-class mapping it shares with pattern_baseline.reduce_result.

Every value here is fabricated.
"""

from __future__ import annotations

import pytest

from caselib import REGISTRY, load
from caselib.run import available
from scorecard.match_shapes import record_key
from scorecard.pattern_baseline import UNJOINED, reduce_result
from scorecard.pattern_recall import (
    OPTIONS,
    CaseOutcome,
    ClassFinding,
    _scan_case,
    evaluate,
    moves,
    policy1_exit,
    select_cases,
    unattributed_class_warnings,
)
from scorecard.runner import CliResult


def _o(*findings, shapes=None, exit_today=1, other_hard=False, expected=1, cid="x.case",
       truth="leak", p1=None):
    return CaseOutcome(cid, expected, exit_today,
                       tuple(ClassFinding(c, frozenset(s)) for c, s in findings),
                       {k: frozenset(v) for k, v in (shapes or {}).items()},
                       other_hard, frozenset(c for c, _ in findings) or frozenset({"ssn"}),
                       truth, p1)


# ── moves: the two bounds ────────────────────────────────────────────────


def test_a_finding_in_a_kept_shape_never_moves_except_under_c():
    o = _o(("ssn", {"formatted"}), shapes={"ssn": {"formatted"}})
    for name in ("A", "B", "B-ssn-phone-A-card"):
        assert not moves(o, OPTIONS[name], upper=True)
    assert moves(o, OPTIONS["C"], upper=False)


def test_a_value_rule_finding_keeps_the_case_hard_unless_judged_class_only():
    o = _o(("ssn", {"uuid"}), shapes={"ssn": {"uuid"}}, other_hard=True)
    assert not any(moves(o, opt, upper=True) for opt in OPTIONS.values())
    assert moves(o, OPTIONS["A"], upper=True, ignore_value_rules=True)


def test_only_exit_1_cases_move():
    assert not moves(_o(("ssn", {"uuid"}), exit_today=2), OPTIONS["C"], upper=True)


def test_email_never_moves():
    assert not moves(_o(("email", {"email-address"})), OPTIONS["C"], upper=True)


def test_an_unknown_shape_is_demoted_by_the_upper_bound_and_kept_by_the_lower():
    o = _o(("ssn", set()))  # unjoined
    assert moves(o, OPTIONS["A"], upper=True)
    assert not moves(o, OPTIONS["C"], upper=False)
    o = _o(("ssn", {UNJOINED}))
    assert moves(o, OPTIONS["A"], upper=True) and not moves(o, OPTIONS["A"], upper=False)


def test_an_ambiguous_finding_uses_the_most_demotable_shape_for_the_upper_bound():
    o = _o(("ssn", {"formatted", "uuid"}), shapes={"ssn": {"formatted", "uuid"}})
    assert moves(o, OPTIONS["B"], upper=True)
    assert not moves(o, OPTIONS["B"], upper=False)


def test_an_unreported_kept_match_holds_the_lower_bound_only():
    o = _o(("ssn", {"alnum-id"}), shapes={"ssn": {"alnum-id", "formatted"}})
    out = evaluate([o])["options"]
    assert out["B"]["moved_at_least"] == [] and out["B"]["moved_at_most"] == ["x.case"]
    assert out["C"]["moved_at_least"] == ["x.case"]


def test_option_b_moves_bare_runs_but_a_does_not():
    o = _o(("ssn", {"bare-digits"}), shapes={"ssn": {"bare-digits"}})
    out = evaluate([o])["options"]
    assert out["A"]["moved_at_most"] == []
    assert out["B"]["moved_at_least"] == ["x.case"]


def test_the_mixed_options_treat_credit_card_on_its_own():
    o = _o(("credit-card", {"bare-digits"}), shapes={"credit-card": {"bare-digits"}})
    out = evaluate([o])["options"]
    assert out["B-ssn-phone-A-card"]["moved_at_most"] == []
    assert out["C-ssn-B-others"]["moved_at_least"] == ["x.case"]


# ── evaluate: coverage counts ───────────────────────────────────────────


def test_evaluate_reports_coverage_and_unjudged_cases():
    outcomes = [
        _o(("ssn", {"uuid"}), cid="a"),
        _o(("ssn", {"uuid"}), cid="b", other_hard=True),
        _o(("ssn", {"formatted", "uuid"}), cid="c"),
        _o(("ssn", set()), cid="d"),
        _o(cid="gap", exit_today=2, expected=1),
        _o(("ssn", {"uuid"}), cid="clean", truth="clean", exit_today=1, expected=0),
    ]
    agg = evaluate(outcomes)
    assert agg["cases"] == 5  # leak cases only
    assert agg["cases_per_class"] == {"ssn": 5, "credit-card": 0, "email": 0, "us-phone": 0}
    assert agg["exit_1_today"] == 4
    assert agg["exit_1_held_by_a_value_rule"] == 1
    assert agg["exit_1_with_class_findings_only"] == 3
    assert agg["expected_1_not_judged"] == ["gap (exit 2 today)"]
    assert agg["unknown_shape_cases"] == ["d"]
    assert agg["ambiguous_shape_cases"] == ["c"]


# ── policy 1: class matches informational ───────────────────────────────


def _report(findings=(), warnings=(), exit_code=None, error=None):
    findings, warnings = list(findings), list(warnings)
    if exit_code is None:
        exit_code = 1 if findings else 2 if warnings else 0
    return {"exit_code": exit_code, "error": error, "findings": findings, "warnings": warnings}


def test_policy1_drops_class_findings_and_class_review_warnings():
    names = frozenset({"Any SSN"})
    only_class = _report([{"rule": "Any SSN"}], [{"rule": "Any SSN", "code": "R"}])
    assert policy1_exit(only_class, names, None) == 0
    with_value = _report([{"rule": "Any SSN"}, {"rule": "Target"}])
    assert policy1_exit(with_value, names, None) == 1
    with_coverage = _report([{"rule": "Any SSN"}], [{"rule": None, "code": "LAYER_CRASHED"}])
    assert policy1_exit(with_coverage, names, None) == 2


def test_policy1_keeps_an_error_reports_exit_and_drops_environmental_warnings():
    names = frozenset({"Any SSN"})
    assert policy1_exit(_report(exit_code=2, error={"code": "PDF_PASSWORD"}), names, None) == 2
    env = _report([{"rule": "Any SSN"}], [{"rule": None, "code": "TOOL_MISSING",
                                          "tool": "exiftool"}])
    assert policy1_exit(env, names, frozenset({"qpdf"})) == 0
    assert policy1_exit(env, names, None) == 2


def test_unattributed_class_warnings_are_counted():
    names = frozenset({"Any SSN"})
    r = _report(warnings=[{"rule": None, "message": "matches pattern rule 'Any SSN' here"},
                          {"rule": "Any SSN", "message": "matches pattern rule 'Any SSN'"}])
    assert unattributed_class_warnings(r, names) == 1


def test_evaluate_lists_policy1_changes_for_sign_off():
    outcomes = [_o(("ssn", {"uuid"}), cid="leak.only-class", p1=0),
                _o(("ssn", {"uuid"}), cid="leak.mixed", other_hard=True, p1=1),
                _o(("ssn", {"uuid"}), cid="clean.noise", truth="clean", expected=0, p1=0)]
    p1 = evaluate(outcomes)["policy1"]
    assert p1["leak_to_0"] == ["leak.only-class: 1 -> 0 (expected 1)"]
    assert p1["leak_other"] == []
    assert p1["clean_changed"] == ["clean.noise: 1 -> 0 (expected 0)"]


# ── reduce_result: rule names to classes ────────────────────────────────


def test_reduce_maps_rule_names_to_classes():
    report = {"exit_code": 1, "error": None, "warnings": [{"rule": "Any SSN", "code": "X"}],
              "findings": [{"rule": "Any SSN", "layer": "Text", "sample": "****6789",
                            "storage": "live", "tier": "hard"},
                           {"rule": "Target", "layer": "Text", "sample": "",
                            "storage": "live", "tier": "hard"}]}
    cli = CliResult(returncode=1, report=report, elapsed=0.0, timed_out=False, stderr_tail="")
    records = {"joins": [{"key": record_key("s", "Any SSN", "****6789"), "shape": "uuid"}],
               "shapes": {"Any SSN": ["uuid"], "Other": ["formatted"]}}
    r = reduce_result("case", cli, records, "s", {"Any SSN": "ssn"})
    assert [(h.rule, h.shape) for h in r.hard] == [("ssn", "uuid")]
    assert r.match_shapes == {"ssn": frozenset({"uuid"})}
    assert r.review_rules == frozenset({"ssn"})


# ── the scan path, on real case-library cases ───────────────────────────

load()
_HAVE = available()


def test_select_cases_takes_leak_and_clean_cases_with_a_class_rule():
    selected = select_cases(REGISTRY, _HAVE)
    ids = {c.id for c in selected}
    assert "layout.single-line-pattern-only" in ids
    assert all(any(r.get("class") for r in c.rules) for c in selected)
    assert not any(c.perf for c in selected)


@pytest.mark.parametrize(("case_id", "other_hard"), [
    ("document.trailer-key-pattern-rule", False),  # the class rule alone
    ("layout.single-line-ssn-pattern", True),      # a value rule beside it
])
def test_scan_case_joins_every_class_finding(case_id, other_hard, tmp_path):
    case = REGISTRY[case_id]
    o = _scan_case(case, tmp_path, frozenset(_HAVE))
    assert o.exit_today == 1 and o.classes == frozenset({"ssn"})
    assert o.findings, "no class finding recorded"
    assert all(f.shapes and not (f.shapes & {UNJOINED, "classifier-error"}) for f in o.findings)
    assert o.other_hard is other_hard
    assert o.unattributed == 0
    assert moves(o, OPTIONS["C"], upper=False) is not other_hard
    assert o.policy1_exit == (1 if other_hard else 0)
    assert "expected" in o.labels_matched  # the scan satisfies the case's own label


# ── policy 1 on case labels ──────────────────────────────────────────────

import dataclasses  # noqa: E402

from caselib.model import expect  # noqa: E402
from scorecard.pattern_recall import label_changes, policy1_label  # noqa: E402

SSN = frozenset({"Any SSN"})


def test_label_with_only_class_findings_goes_to_0_or_2():
    assert policy1_label(expect(1, (("Any SSN", "live"),)), SSN, True) == 0
    assert policy1_label(expect(1, (("Any SSN", "live"),), (("LAYER_CRASHED", None),)),
                         SSN, True) == 2


def test_label_with_a_value_finding_stays_1():
    assert policy1_label(expect(1, (("Any SSN", "live"), ("Target", "live"))), SSN, False) == 1


def test_label_review_warning_is_the_class_only_in_class_only_cases():
    review_only = expect(2, warnings=(("REVIEW_FUSED_PATTERN", "live"),))
    assert policy1_label(review_only, SSN, class_only=True) == 0
    assert policy1_label(review_only, SSN, class_only=False) is None


def test_label_exit_2_with_no_listed_warning_is_undetermined():
    assert policy1_label(expect(2), SSN, class_only=True) is None


def test_label_changes_cover_known_gaps_and_settle_mixed_cases_by_scan():
    class _Gap:
        today = expect(0)

    class _Case:
        def __init__(self, truth, rules, expected, known_gap=None):
            self.truth, self.rules, self.expected = truth, rules, expected
            self.known_gap, self.older_macos = known_gap, None

    cls = {"name": "Any SSN", "class": "ssn"}
    val = {"name": "Target", "value": "x"}
    review = expect(2, warnings=(("REVIEW_FUSED_PATTERN", "live"),))
    registry = {
        "gap.class-only": _Case("leak", (cls,), review, _Gap()),
        "mixed.settled": _Case("leak", (cls, val), review),
        "mixed.unscanned": _Case("leak", (cls, val), review),
    }
    registry["mixed.exit-only"] = _Case("leak", (cls, val), review)
    settled = _o(("ssn", {"uuid"}), cid="mixed.settled", exit_today=2, p1=2)
    settled = dataclasses.replace(settled, labels_matched=frozenset({"expected"}))
    # Same exit as the label, but the scan does not match it exactly.
    exit_only = _o(("ssn", {"uuid"}), cid="mixed.exit-only", exit_today=2, p1=2)
    out = label_changes(registry, {"mixed.settled": settled, "mixed.exit-only": exit_only})
    assert out["changed"] == ["gap.class-only [leak] expected: 2 -> 0"]
    assert out["undetermined"] == ["mixed.exit-only [leak] expected: 2 -> ?",
                                   "mixed.unscanned [leak] expected: 2 -> ?"]
    assert out["settled_by_scan"] == ["mixed.settled [leak] expected: 2 -> 2"]


def test_policy1_on_an_error_report_never_certifies():
    err = _report([{"rule": "Any SSN"}], exit_code=1, error={"code": "INTERNAL_ERROR"})
    assert policy1_exit(err, SSN, None) == 2
    err = _report([{"rule": "Target"}], exit_code=1, error={"code": "INTERNAL_ERROR"})
    assert policy1_exit(err, SSN, None) == 1
