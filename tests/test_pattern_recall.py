"""Tests for Phase 5's recall-cost measurement (eval/scorecard/pattern_recall.py)
and the rule-name-to-class mapping it needs in pattern_baseline.reduce_result.

Every value here is fabricated.
"""

from __future__ import annotations

from scorecard.match_shapes import record_key
from scorecard.pattern_baseline import UNJOINED, FileResult, HardFinding, reduce_result
from scorecard.pattern_recall import OPTIONS, CaseOutcome, evaluate, moves
from scorecard.runner import CliResult


def _fr(*hard, shapes=None, exit_code=1):
    return FileResult("case", True, exit_code, tuple(hard),
                      match_shapes={k: frozenset(v) for k, v in (shapes or {}).items()})


def _h(rule, shape):
    return HardFinding(rule, "Text", "live", shape)


def test_a_finding_in_a_kept_shape_never_moves():
    r = _fr(_h("ssn", "formatted"), shapes={"ssn": {"formatted"}})
    for option in OPTIONS.values():
        if option["ssn"]:  # C keeps nothing
            assert not moves(r, False, option, judge_all_matches=True)
    assert moves(r, False, OPTIONS["C"], judge_all_matches=True)


def test_a_value_rule_finding_keeps_the_case_hard_under_every_option():
    r = _fr(_h("ssn", "uuid"), shapes={"ssn": {"uuid"}})
    assert all(not moves(r, True, o, judge_all_matches=False) for o in OPTIONS.values())


def test_only_exit_1_cases_move():
    r = _fr(_h("ssn", "uuid"), shapes={"ssn": {"uuid"}}, exit_code=2)
    assert not moves(r, False, OPTIONS["C"], judge_all_matches=False)


def test_email_and_unknown_shapes_never_move():
    assert not moves(_fr(_h("email", "email-address")), False, OPTIONS["C"],
                     judge_all_matches=False)
    assert not moves(_fr(_h("ssn", UNJOINED)), False, OPTIONS["C"], judge_all_matches=False)


def test_bounds_differ_when_an_unreported_match_has_a_kept_shape():
    # The reported SSN sits in an id; the same class also matched a dashed
    # SSN elsewhere the tool did not report: certainly-moves excludes it.
    r = _fr(_h("ssn", "alnum-id"), shapes={"ssn": {"alnum-id", "formatted"}})
    out = evaluate([CaseOutcome("x.case", 1, r, False)])
    assert out["B"] == {"moved_at_least": [], "moved_at_most": ["x.case"]}
    assert out["A"] == {"moved_at_least": [], "moved_at_most": ["x.case"]}
    assert out["C"] == {"moved_at_least": ["x.case"], "moved_at_most": ["x.case"]}


def test_option_b_moves_bare_runs_but_a_does_not():
    r = _fr(_h("ssn", "bare-digits"), shapes={"ssn": {"bare-digits"}})
    out = evaluate([CaseOutcome("bare.case", 1, r, False)])
    assert out["A"]["moved_at_most"] == []
    assert out["B"]["moved_at_least"] == ["bare.case"]
    assert out["D-recommended"]["moved_at_least"] == ["bare.case"]


def test_the_mixed_options_treat_credit_card_on_its_own():
    r = _fr(_h("credit-card", "bare-digits"), shapes={"credit-card": {"bare-digits"}})
    out = evaluate([CaseOutcome("card.case", 1, r, False)])
    assert out["D-recommended"]["moved_at_most"] == []  # A for credit-card keeps bare runs
    assert out["D-gate-as-written"]["moved_at_least"] == ["card.case"]


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
