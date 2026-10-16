"""ADR 0012's policy 1 in the case library (eval/caselib/policy1.py): the
default-mode twins are exactly the owner's signed-off list, and no other
case's verdict moves when its class rules lose "strict" (the default mode).
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from caselib import REGISTRY, load
from caselib.policy1 import (
    SIGNED_OFF,
    SIGNED_OFF_BY_EXIT,
    SIGNED_OFF_BY_LABEL,
    SIGNED_OFF_CLEAN,
    SIGNED_OFF_OTHER_CLASSES,
    TWIN_SUFFIX,
)
from caselib.run import available, build, scan, today_label
from scorecard.differential import normalization_have
from scorecard.keys import effective_exit

load()
_HAVE = available()


def test_the_sign_off_list_is_the_owners():
    # 24 by today's exit, 11 known gaps by their label, 3 other-class cases
    # (a second sign-off), 2 clean relabels: 38 leak cases + 2 clean.
    assert (len(SIGNED_OFF_BY_EXIT), len(SIGNED_OFF_BY_LABEL), len(SIGNED_OFF_OTHER_CLASSES),
            len(SIGNED_OFF_CLEAN)) == (24, 11, 3, 2)
    assert len(set(SIGNED_OFF)) == len(SIGNED_OFF) == 40


def test_every_signed_off_case_has_exactly_one_twin_and_nothing_else_does():
    twins = {cid for cid in REGISTRY if cid.endswith(TWIN_SUFFIX)}
    assert twins == {cid + TWIN_SUFFIX for cid in SIGNED_OFF}
    for cid in SIGNED_OFF:
        tw = REGISTRY[cid + TWIN_SUFFIX]
        assert tw.truth == "clean" and tw.expected.exit == 0 and tw.known_gap is None
        assert tw.build is REGISTRY[cid].build
        assert all("strict" not in r for r in tw.rules)


@pytest.mark.parametrize("cid", [c for c in SIGNED_OFF if c not in SIGNED_OFF_BY_LABEL])
def test_a_twin_reports_its_class_match_as_a_notice(cid, tmp_path):
    """ADR 0012 decision 7: default mode expects exit 0 *plus a notice*.
    (The 11 known gaps are left out: today's tool misses their value, so
    there is no match to report.)"""
    twin = REGISTRY[cid + TWIN_SUFFIX]
    missing = twin.requires - _HAVE
    if missing:
        pytest.skip(f"needs {', '.join(sorted(missing))}")
    pdf = build(twin, tmp_path / f"{twin.id}.pdf")
    result = scan(twin, pdf, tmp_path)
    rules = {r["name"] for r in twin.rules}
    assert any(n["kind"] == "class-match" and n["rule"] in rules
               for n in result.report["notices"]), cid


def test_every_original_class_rule_is_strict():
    for cid, case in REGISTRY.items():
        if cid.endswith(TWIN_SUFFIX):
            continue
        for rule in case.rules:
            if "class" in rule:
                assert rule.get("strict") is True, cid


def _others():
    for cid, case in sorted(REGISTRY.items()):
        if cid.endswith(TWIN_SUFFIX) or cid in SIGNED_OFF:
            continue
        if not any("class" in r for r in case.rules):
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
        yield pytest.param(case, id=cid, marks=marks)


@pytest.mark.parametrize("case", list(_others()))
def test_no_case_outside_the_list_changes_its_exit_in_default_mode(case, tmp_path):
    """The owner signed off only the cases above: every other case with a
    class rule must give the exit it gives today when the rule is not
    strict -- above all, never move to 0."""
    default = dataclasses.replace(
        case, rules=tuple({k: v for k, v in r.items() if k != "strict"} for r in case.rules))
    pdf = build(default, tmp_path / f"{case.id}.pdf")
    result = scan(default, pdf, tmp_path)
    today = (today_label(case) or case.expected).exit
    # Judged like the case library's labels: this machine's missing tools
    # (no OCR on Linux CI) set aside.
    exit_code = effective_exit(result.report, normalization_have(_HAVE))
    assert exit_code == today, (case.id, exit_code, today)
