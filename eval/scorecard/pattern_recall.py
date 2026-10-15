"""Phase 5's recall cost: which case-library leak cases each demotion
option of ADR 0012 would move from a hard finding (exit 1) to manual
review (exit 2). Measurement only; nothing is demoted.

    PYTHONPATH=eval:. python -m scorecard.pattern_recall [--json OUT]

Every leak case whose rules include a built-in class is scanned
in-process with `scorecard.pattern_recorder.Recorder` installed (the
report is unchanged: tests/test_pattern_baseline.py's parity test), and
each option is applied to what was recorded. A demotion turns a hard
finding into a review warning and never removes one, so an option can
only move a case from exit 1 to exit 2, never to 0: a case moves when
every hard finding it has today is a class match the option demotes.

Judged on cases that exit 1 today. A case whose label expects exit 1 but
that exits otherwise today (a pinned known gap, or a label for another
macOS version) cannot be judged here and is listed by id, so a count is
never read as covering it.

Two bounds per option, because the tool reports a rule's first match per
page while a demotion would let it go on looking, and because a
finding's shape can be unknown or ambiguous:

- `moved_at_least` (certainly moves): every class finding is demoted
  whichever of its recorded shapes is used, none is unknown, and no other
  match of that class in a hard-capable call has a kept shape;
- `moved_at_most` (may move): every class finding has some recorded shape
  the option demotes, or no known shape at all.

Also reported: per class, how many cases were measured (a class with none
has no measured recall); how many exit-1 cases a value rule holds at exit
1 (no option touches a value rule) against how many have class findings
only; `moved_if_class_rules_only`, the upper bound judged as if the cases'
value rules were absent; and the exit-1 cases with an unknown or
ambiguous finding shape. `main` exits 1 if any finding's shape is
unknown, as the baseline treats an unjoined finding. Case ids are
fabricated, so they are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .match_shapes import record_key
from .pattern_baseline import CLASSES, UNKNOWN

ALL_SHAPES_BUT_EMAIL = frozenset({
    "formatted", "uuid", "hex-id", "date-or-time", "alnum-id", "number-list", "bare-digits",
})
# Kept (hard) shapes, per ADR 0012 option; an email address is always kept.
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
    # B for ssn and us-phone, A for credit-card.
    "B-ssn-phone-A-card": {"ssn": KEEP_B, "us-phone": KEEP_B, "credit-card": KEEP_A},
    # C for ssn, B for the others: the only mix that meets the gate as written.
    "C-ssn-B-others": {"ssn": KEEP_C, "us-phone": KEEP_B, "credit-card": KEEP_B},
}


@dataclass(frozen=True)
class ClassFinding:
    cls: str
    shapes: frozenset[str]  # every shape joined to the finding; empty = unjoined


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    expected_exit: int
    exit_today: int
    findings: tuple[ClassFinding, ...]  # today's hard class findings
    match_shapes: Mapping[str, frozenset[str]]  # class -> every recorded match's shape
    other_hard: bool  # a hard finding of a non-class rule (a value rule)
    classes: frozenset[str]  # the built-in classes the case's rules name
    truth: str = "leak"
    policy1_exit: int | None = None  # the exit with class matches informational only
    unattributed: int = 0  # warnings naming a class rule in text but not in `rule`
    # The case labels ("expected", "known_gap.today", "older_macos.today")
    # this scan's report satisfies exactly (caselib.run.judge).
    labels_matched: frozenset[str] = frozenset()


def policy1_exit(report: Mapping[str, Any], class_names: frozenset[str],
                 have: frozenset[str] | None) -> int:
    """The exit code under the owner's policy 1 (ADR 0012): a built-in class
    match affects nothing, so its findings and its review warnings (those
    whose `rule` is a class rule) are dropped, and the exit is recomputed
    by the tool's own formula. An error report exits 1 if a non-class
    finding remains, else 2 (it never certifies). *have* drops
    environmental warnings as `keys.effective_exit` does; compare it with
    `keys.effective_exit(report, have)`, never with the raw exit."""
    from .keys import is_environmental

    findings = [f for f in report["findings"] if f.get("rule") not in class_names]
    if report.get("error"):
        # The run failed after these findings; it never certifies.
        return 1 if findings else 2
    warnings = [w for w in report["warnings"]
                if w.get("rule") not in class_names
                and not (have is not None and is_environmental(w, have))]
    return 1 if findings else 2 if warnings else 0


def policy1_label(label: Any, class_names: frozenset[str], class_only: bool) -> int | None:
    """A case label's exit under policy 1, or None when the label cannot
    say. A label lists findings by rule but warnings only by code, so a
    review warning (the kind matching raises) is the class's when the
    case's rules are all built-in classes, and undetermined otherwise; a
    coverage warning is never a class's and keeps the label at 2."""
    from redaction_verifier.model import WARNING_CODES

    if any(r not in class_names for r, _ in label.findings):
        return 1
    review = [c for c, _ in label.warnings if WARNING_CODES.get(c) == "review"]
    other = [c for c, _ in label.warnings if WARNING_CODES.get(c) != "review"]
    if other:
        return 2
    if label.exit == 0:
        return 0
    if not class_only:
        return None  # a value or custom rule may have raised the review
    if label.exit == 2 and not review:
        return None  # exit 2 with no warning listed: the label cannot say why
    return 0


def label_changes(registry: Mapping[str, Any],
                  scanned: Mapping[str, CaseOutcome] | None = None) -> dict[str, list[str]]:
    """Every case label (expected, known_gap.today, older_macos.today) of
    every case with a built-in class rule -- built here or not -- that
    policy 1 changes, or cannot judge. A label the codes alone cannot
    judge is settled by this machine's scan when the case's exit today is
    the label's (its report carries each warning's rule)."""
    scanned = scanned or {}
    changed: list[str] = []
    unknown: list[str] = []
    settled: list[str] = []
    for cid, case in sorted(registry.items()):
        class_names = frozenset(r["name"] for r in case.rules if r.get("class") in CLASSES)
        if not class_names:
            continue
        class_only = len(class_names) == len(case.rules)
        labels = [("expected", case.expected)]
        if case.known_gap is not None:
            labels.append(("known_gap.today", case.known_gap.today))
        if case.older_macos is not None and case.older_macos.today is not None:
            labels.append(("older_macos.today", case.older_macos.today))
        for which, label in labels:
            new = policy1_label(label, class_names, class_only)
            o = scanned.get(cid)
            if new is None and o is not None and which in o.labels_matched:
                # The scan reports exactly what the label expects, and the
                # report names each warning's rule: policy 1 on it is the
                # label's policy 1.
                new = o.policy1_exit
                settled.append(f"{cid} [{case.truth}] {which}: {label.exit} -> {new}")
                which += " (settled by this scan)"
            if new is None:
                unknown.append(f"{cid} [{case.truth}] {which}: {label.exit} -> ?")
            elif new != label.exit:
                changed.append(f"{cid} [{case.truth}] {which}: {label.exit} -> {new}")
    return {"changed": changed, "undetermined": unknown, "settled_by_scan": settled}


def unattributed_class_warnings(report: Mapping[str, Any], class_names: frozenset[str]) -> int:
    """Warnings that name a class rule in their message but carry no `rule`
    field: policy 1 could not drop them, so they must stay 0."""
    return sum(1 for w in report["warnings"] if not w.get("rule")
               and any(repr(n) in w.get("message", "") for n in class_names))


def _kept(option: Mapping[str, frozenset[str]], cls: str) -> frozenset[str]:
    if cls not in option:  # email, or a class no option names: never demoted
        return ALL_SHAPES_BUT_EMAIL | {"email-address"}
    return option[cls] | {"email-address"}


def moves(o: CaseOutcome, option: Mapping[str, frozenset[str]], *, upper: bool,
          ignore_value_rules: bool = False) -> bool:
    """Would *option* move this exit-1 case to exit 2? The lower bound
    (`upper=False`) needs every finding certainly demoted; the upper bound
    needs each finding possibly demoted, an unknown shape counting as
    demoted."""
    if o.exit_today != 1 or not o.findings or (o.other_hard and not ignore_value_rules):
        return False
    for f in o.findings:
        keep = _kept(option, f.cls)
        known = f.shapes - UNKNOWN
        if upper:
            if f.shapes and known == f.shapes and known <= keep:
                return False  # every shape it could have is kept
        else:
            if not known or known != f.shapes or known & keep:
                return False
            if o.match_shapes.get(f.cls, frozenset()) & (keep | UNKNOWN):
                return False
    return True


def evaluate(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """Per option, the exit-1-today cases it moves (both bounds), plus the
    coverage counts the module docstring lists."""
    all_outcomes = outcomes
    outcomes = [o for o in outcomes if o.truth == "leak"]  # A/B/C: leak cases only
    exit1 = [o for o in outcomes if o.exit_today == 1]
    per_option: dict[str, Any] = {}
    for name, option in OPTIONS.items():
        per_option[name] = {
            "moved_at_least": sorted(o.case_id for o in exit1 if moves(o, option, upper=False)),
            "moved_at_most": sorted(o.case_id for o in exit1 if moves(o, option, upper=True)),
            "moved_if_class_rules_only": sorted(
                o.case_id for o in exit1
                if moves(o, option, upper=True, ignore_value_rules=True)),
        }
    measured = Counter(c for o in outcomes for c in o.classes)
    return {
        "cases": len(outcomes),
        "cases_per_class": {c: measured.get(c, 0) for c in CLASSES},
        "exit_1_today": len(exit1),
        "exit_1_held_by_a_value_rule": sum(o.other_hard for o in exit1),
        "exit_1_with_class_findings_only": sum(bool(o.findings) and not o.other_hard
                                               for o in exit1),
        "expected_1_not_judged": sorted(
            f"{o.case_id} (exit {o.exit_today} today)" for o in outcomes
            if o.expected_exit == 1 and o.exit_today != 1),
        "unknown_shape_cases": sorted(
            o.case_id for o in exit1
            if any(not (f.shapes - UNKNOWN) or f.shapes & UNKNOWN for f in o.findings)),
        "ambiguous_shape_cases": sorted(
            o.case_id for o in exit1 if any(len(f.shapes - UNKNOWN) > 1 for f in o.findings)),
        "options": per_option,
        "policy1": _policy1(all_outcomes),
    }


def _policy1(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """Every case whose exit policy 1 changes, with its labels: a leak case
    that reaches 0 is a loosening that needs the owner's per-case sign-off."""
    changed = [o for o in outcomes if o.policy1_exit is not None
               and o.policy1_exit != o.exit_today]
    return {
        "leak_to_0": sorted(f"{o.case_id}: {o.exit_today} -> 0 (expected {o.expected_exit})"
                            for o in changed if o.truth == "leak" and o.policy1_exit == 0),
        "leak_other": sorted(f"{o.case_id}: {o.exit_today} -> {o.policy1_exit}"
                             f" (expected {o.expected_exit})"
                             for o in changed if o.truth == "leak" and o.policy1_exit != 0),
        "clean_changed": sorted(f"{o.case_id}: {o.exit_today} -> {o.policy1_exit}"
                                f" (expected {o.expected_exit})"
                                for o in changed if o.truth != "leak"),
        "unattributed_class_warnings": sum(o.unattributed for o in outcomes),
    }


def _scan_case(case: Any, workdir: Path, have: frozenset[str] | None = None) -> CaseOutcome:
    from caselib.run import build, scan

    from .pattern_recorder import Recorder

    pdf = build(case, workdir / f"{case.id}.pdf")
    recorder = Recorder("recall")
    recorder.install()
    try:
        s = scan(case, pdf, workdir)
    finally:
        recorder.uninstall()
    joins: dict[str, set[str]] = {}
    for key, shape in recorder.joins:
        joins.setdefault(key, set()).add(shape)
    class_of = {r["name"]: r["class"] for r in case.rules if r.get("class") in CLASSES}
    findings = []
    other_hard = False
    for f in s.report.get("findings", []):
        if f.get("tier") != "hard":
            continue
        cls = class_of.get(f["rule"])
        if cls is None:
            other_hard = True
            continue
        key = record_key("recall", f["rule"], f.get("sample", ""))
        findings.append(ClassFinding(cls, frozenset(joins.get(key, ()))))
    shapes: dict[str, frozenset[str]] = {}
    for name, v in recorder.shapes.items():
        if name in class_of:
            shapes[class_of[name]] = shapes.get(class_of[name], frozenset()) | frozenset(v)
    from .keys import effective_exit

    from caselib.run import judge

    labels = {"expected": case.expected}
    if case.known_gap is not None:
        labels["known_gap.today"] = case.known_gap.today
    if case.older_macos is not None and case.older_macos.today is not None:
        labels["older_macos.today"] = case.older_macos.today
    matched = frozenset(which for which, want in labels.items()
                        if judge(case, s, have, want=want).ok)
    names = frozenset(class_of)
    # The same environment filter on both sides of policy 1's comparison.
    today = effective_exit(s.report, have) if have is not None else s.exit_code
    return CaseOutcome(case.id, case.expected.exit, today, tuple(findings), shapes,
                       other_hard, frozenset(class_of.values()), case.truth,
                       policy1_exit(s.report, names, have),
                       unattributed_class_warnings(s.report, names), matched)


def select_cases(registry: Mapping[str, Any], have: frozenset[str], *,
                 any_requires: bool = False) -> list[Any]:
    """Cases (leak and clean) with a built-in class rule that this machine
    can build (*any_requires*: whatever they require). The A/B/C options
    are judged on the leak cases only."""
    return [c for _, c in sorted(registry.items())
            if (any_requires or not (c.requires - have)) and not c.perf
            and any(r.get("class") in CLASSES for r in c.rules)]


def render(agg: Mapping[str, Any]) -> str:
    lines = [
        f"{agg['cases']} leak cases with a built-in class rule, per class: "
        f"{agg['cases_per_class']} (a class with 0 has no measured recall)",
        f"{agg['exit_1_today']} exit 1 today: {agg['exit_1_held_by_a_value_rule']} held there "
        f"by a value rule (no option touches it), {agg['exit_1_with_class_findings_only']} "
        "by class findings only",
        f"expected exit 1 but not judged: {agg['expected_1_not_judged'] or 'none'}",
        f"unknown finding shape: {agg['unknown_shape_cases'] or 'none'}; "
        f"ambiguous: {agg['ambiguous_shape_cases'] or 'none'}",
    ]
    if "not_scanned_here" in agg:
        lines.append(f"not scanned here (missing requirements): "
                     f"{agg['not_scanned_here'] or 'none'}")
    if "policy1_labels" in agg:
        lab = agg["policy1_labels"]
        lines.append(f"policy 1 on the case labels (every case with a class rule, scanned "
                     f"or not): {len(lab['changed'])} label(s) change, "
                     f"{len(lab['undetermined'])} undetermined, "
                     f"{len(lab['settled_by_scan'])} settled by this scan (listed last)")
        lines.extend(f"    {x}" for x in lab["changed"] + lab["undetermined"])
        lines.extend(f"    settled: {x}" for x in lab["settled_by_scan"])
    p1 = agg["policy1"]
    lines.append(f"policy 1 (class matches informational): {len(p1['leak_to_0'])} leak "
                 f"case(s) to exit 0 (each needs the owner's sign-off), "
                 f"{len(p1['leak_other'])} other leak change(s), "
                 f"{len(p1['clean_changed'])} clean case(s) changed; "
                 f"unattributed class warnings: {p1['unattributed_class_warnings']}")
    for line in p1["leak_to_0"] + p1["leak_other"] + p1["clean_changed"]:
        lines.append(f"    {line}")
    for name, r in agg["options"].items():
        lines.append(f"  {name:20} moves 1->2: at least {len(r['moved_at_least'])}, "
                     f"at most {len(r['moved_at_most'])}; "
                     f"if class rules only: {len(r['moved_if_class_rules_only'])}")
        for cid in r["moved_at_most"]:
            mark = "" if cid in r["moved_at_least"] else " (maybe)"
            lines.append(f"    {cid}{mark}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scorecard.pattern_recall",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--json", type=Path, default=None)
    args = p.parse_args(argv)

    from caselib import REGISTRY, load
    from caselib.run import available

    from .differential import normalization_have

    load()
    have = available()
    cases = select_cases(REGISTRY, have)
    skipped = sorted(c.id for c in select_cases(REGISTRY, have, any_requires=True)
                     if c not in cases)
    with tempfile.TemporaryDirectory(prefix="pattern-recall-") as tmp:
        outcomes = [_scan_case(case, Path(tmp), normalization_have(have)) for case in cases]
    agg = evaluate(outcomes)
    agg["not_scanned_here"] = skipped
    agg["policy1_labels"] = label_changes(REGISTRY, {o.case_id: o for o in outcomes})
    print(render(agg))
    if args.json:
        args.json.write_text(json.dumps(agg, indent=2, sort_keys=True) + "\n")
    if agg["policy1"]["unattributed_class_warnings"]:
        print("ERROR: a warning names a class rule but has no rule field; policy 1's "
              "exits cannot be trusted.", file=sys.stderr)
        return 1
    if agg["policy1_labels"]["undetermined"]:
        print("ERROR: some case labels cannot be judged under policy 1 (see above).",
              file=sys.stderr)
        return 1
    if agg["unknown_shape_cases"]:
        print("ERROR: a class finding has no known shape (see above); the bounds cannot "
              "be trusted until it is joined.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
