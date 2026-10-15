"""Phase 5's recall cost: which case-library leak cases each demotion
option of ADR 0012 would move from a hard finding (exit 1) to manual
review (exit 2). Measurement only; nothing is demoted.

    PYTHONPATH=eval:. python -m scorecard.pattern_recall [--json OUT]

Every case whose rules include a built-in class is scanned in-process
with `scorecard.pattern_recorder.Recorder` installed (the report is
unchanged: tests/test_pattern_baseline.py's parity test), and each
option is applied to what was recorded. A demotion turns a hard finding
into a review warning and never removes one, so an option can only move a
case from exit 1 to exit 2, never to 0: a case moves when every hard
finding it has today is a class match the option demotes.

Two bounds per option, because the tool reports a rule's first match per
page while a demotion would let it go on looking:

- `moved_at_least` (the cases that certainly move): every class finding
  is demoted judged by the finding's own match *and* no other match of
  that class in a hard-capable call has a kept shape;
- `moved_at_most` (the cases that may move): every class finding is
  demoted judged by the finding's own match alone.

A case in `at_most` but not in `at_least` holds a kept-shape match the
tool did not report as the finding; whether a demotion would surface it
as hard depends on where it sits. Case ids are fabricated, so they are
printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .pattern_baseline import CLASSES, UNKNOWN, FileResult, reduce_result
from .runner import CliResult

ALL_SHAPES_BUT_EMAIL = frozenset({
    "formatted", "uuid", "hex-id", "date-or-time", "alnum-id", "number-list", "bare-digits",
})
# Kept (hard) shapes, per ADR 0012 option. `email-address` and the unknown
# shapes are always kept.
KEEP_A = frozenset({"formatted", "number-list", "bare-digits"})  # demote embedded + dated
KEEP_B = frozenset({"formatted"})                                  # keep only real groupings
KEEP_C: frozenset[str] = frozenset()                               # demote the class
DIGIT_CLASSES = ("ssn", "us-phone", "credit-card")


def _same(keep: frozenset[str]) -> dict[str, frozenset[str]]:
    return {c: keep for c in DIGIT_CLASSES}


OPTIONS: dict[str, dict[str, frozenset[str]]] = {
    "A": _same(KEEP_A),
    "B": _same(KEEP_B),
    "C": _same(KEEP_C),
    # ADR 0012's recommendation: B for ssn and us-phone, A for credit-card.
    "D-recommended": {"ssn": KEEP_B, "us-phone": KEEP_B, "credit-card": KEEP_A},
    # Its fallback if the owner keeps the gate as written: C for ssn.
    "D-gate-as-written": {"ssn": KEEP_C, "us-phone": KEEP_B, "credit-card": KEEP_B},
}


def _kept(option: Mapping[str, frozenset[str]], cls: str) -> frozenset[str]:
    if cls not in option:  # email, or a class no option names: never demoted
        return ALL_SHAPES_BUT_EMAIL | UNKNOWN | {"email-address"}
    return option[cls] | UNKNOWN | {"email-address"}


def moves(result: FileResult, other_hard: bool, option: Mapping[str, frozenset[str]],
          *, judge_all_matches: bool) -> bool:
    """Would *option* move this exit-1 case to exit 2? *other_hard*: the
    case has a hard finding that is not a built-in class match (a value
    rule), which no option touches."""
    if result.exit_code != 1 or other_hard or not result.hard:
        return False
    for h in result.hard:
        keep = _kept(option, h.rule)
        if h.shape in keep:
            return False
        if judge_all_matches and result.match_shapes.get(h.rule, frozenset()) & keep:
            return False
    return True


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    expected_exit: int
    result: FileResult
    other_hard: bool


def evaluate(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """Per option: the expected-exit-1 leak cases it moves, both bounds."""
    out: dict[str, Any] = {}
    for name, option in OPTIONS.items():
        at_least = sorted(o.case_id for o in outcomes
                          if moves(o.result, o.other_hard, option, judge_all_matches=True))
        at_most = sorted(o.case_id for o in outcomes
                         if moves(o.result, o.other_hard, option, judge_all_matches=False))
        out[name] = {"moved_at_least": at_least, "moved_at_most": at_most}
    return out


def _scan_case(case: Any, workdir: Path) -> CaseOutcome:
    from caselib.run import build, scan

    from .pattern_recorder import Recorder

    pdf = build(case, workdir / f"{case.id}.pdf")
    recorder = Recorder("recall")
    recorder.install()
    try:
        s = scan(case, pdf, workdir)
    finally:
        recorder.uninstall()
    records = {
        "joins": [{"key": k, "shape": shape} for k, shape in recorder.joins],
        "shapes": {r: sorted(v) for r, v in recorder.shapes.items()},
    }
    cli = CliResult(returncode=s.exit_code, report=s.report, elapsed=0.0, timed_out=False,
                    stderr_tail="")
    class_of = {r["name"]: r["class"] for r in case.rules if r.get("class") in CLASSES}
    result = reduce_result("case", cli, records, "recall", class_of)
    other_hard = any(f.get("tier") == "hard" and f["rule"] not in class_of
                     for f in s.report.get("findings", []))
    return CaseOutcome(case.id, case.expected.exit, result, other_hard)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scorecard.pattern_recall",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--json", type=Path, default=None)
    args = p.parse_args(argv)

    from caselib import REGISTRY, load
    from caselib.run import available

    load()
    have = available()
    cases = [c for _, c in sorted(REGISTRY.items())
             if c.truth == "leak" and not (c.requires - have) and not c.perf
             and any(r.get("class") in CLASSES for r in c.rules)]
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="pattern-recall-") as tmp:
        for case in cases:
            outcomes.append(_scan_case(case, Path(tmp)))
    options = evaluate(outcomes)
    agg = {
        "cases": len(outcomes),
        "exit_1_today": sum(o.result.exit_code == 1 for o in outcomes),
        "options": options,
    }
    print(f"{agg['cases']} leak cases with a built-in class rule; "
          f"{agg['exit_1_today']} exit 1 today")
    for name, r in options.items():
        print(f"  {name:18} moves 1->2: at least {len(r['moved_at_least'])}, "
              f"at most {len(r['moved_at_most'])}")
        for cid in r["moved_at_most"]:
            mark = "" if cid in r["moved_at_least"] else " (maybe)"
            print(f"    {cid}{mark}")
    if args.json:
        args.json.write_text(json.dumps(agg, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
