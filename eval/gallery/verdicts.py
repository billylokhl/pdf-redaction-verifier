"""Today's verdict for a case, for the gallery — reusing whatever already
ran the case rather than a third runner (eval/README.md's "The
scorecard"): a case-id-keyed ``scorecard diff --json`` report if one is
given, otherwise the case's own label (``expected``, or ``known_gap.today``
for a known gap), clearly marked either way as measured or not."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caselib.model import Case, Expect

MEASURED = "measured this run (scorecard diff --json)"
FROM_KNOWN_GAP = "today's pinned known-gap label, not measured this run"
FROM_EXPECTED = "the case's expected label, not measured this run"


@dataclass(frozen=True)
class Verdict:
    exit: int
    findings: tuple[str, ...]
    warnings: tuple[str, ...]
    source: str
    crashed: bool = False


def load_results(path: Path) -> dict[str, dict[str, Any] | None]:
    """``{case_id: candidate-key-dict-or-None}`` from a
    ``scorecard diff --json`` report (``report.differential_to_jsonable``'s
    shape: a top-level ``"cases"`` list of
    ``{"case", "candidate", "crashed", ...}``).

    A row's ``"crashed"`` is ``reference.crashed or candidate.crashed``
    (``differential.CaseDiff.crashed``, ``report.differential_to_jsonable``)
    — it is true even when only the *reference* crashed and the candidate
    ran fine. ``"candidate"`` is already exactly what we want: it is
    ``None`` if and only if the candidate itself crashed (its own key is
    None only then). Keying off ``"crashed"`` instead used to launder a
    reference-only crash into "crashed" here too, hiding a real candidate
    miss (exit 0) as a crash instead of surfacing it as a miss."""
    data = json.loads(path.read_text())
    out: dict[str, dict[str, Any] | None] = {}
    for row in data.get("cases", ()):
        out[row["case"]] = row.get("candidate")
    return out


REFERENCE_CRASHED_NOTE = ("the reference crashed running this case this run; the measured "
                          "verdict above is from the candidate only")


def reference_crashed_cases(path: Path) -> frozenset[str]:
    """Case ids from a ``scorecard diff --json`` report at *path* where
    the reference crashed but the candidate did not — worth flagging
    distinctly now that ``load_results`` deliberately keys off the
    candidate alone (a reference-only crash must never be allowed to hide
    a real candidate result — see ``load_results``'s docstring). A row's
    own ``"reference"`` key is the candidate/reference key dict or None;
    None here means that side crashed (``CaseRun.key`` is None only when
    crashed or timed out)."""
    data = json.loads(path.read_text())
    return frozenset(
        row["case"] for row in data.get("cases", ())
        if row.get("crashed") and row.get("reference") is None and row.get("candidate") is not None
    )


def _fmt_finding(entry: Any) -> str:
    # differential key: [rule, tier, storage]; a plain (rule, storage) pair
    # (Expect.findings) is handled the same way by padding.
    parts = list(entry)
    if len(parts) == 3:
        rule, _tier, storage = parts
    else:
        rule, storage = parts
    return f"{rule} ({storage})"


def _fmt_warning(entry: Any) -> str:
    parts = [p for p in entry if p is not None]
    return " / ".join(str(p) for p in parts)


def verdict_for(case: Case, results: dict[str, dict[str, Any] | None] | None) -> Verdict:
    if results is not None and case.id in results:
        candidate = results[case.id]
        if candidate is None:
            return Verdict(exit=-1, findings=(), warnings=(), source=MEASURED, crashed=True)
        findings = tuple(_fmt_finding(f) for f, _n in candidate.get("findings", []))
        warnings = tuple(_fmt_warning(w) for w, _n in candidate.get("warnings", []))
        return Verdict(exit=candidate["exit"], findings=findings, warnings=warnings, source=MEASURED)

    want: Expect = case.known_gap.today if case.known_gap else case.expected
    source = FROM_KNOWN_GAP if case.known_gap else FROM_EXPECTED
    findings = tuple(f"{rule} ({storage})" for rule, storage in sorted(want.findings))
    warnings = tuple(f"{code} ({storage})" for code, storage in sorted(want.warnings, key=str))
    return Verdict(exit=want.exit, findings=findings, warnings=warnings, source=source)


def is_miss(case: Case, verdict: Verdict) -> bool:
    """The gallery's most important marker: today's tool actually
    certifies this leak clean, per *verdict* — the measured result when
    ``--results`` covers this case, the pinned label otherwise
    (``verdict_for``'s own fallback: ``expected`` for an ordinary case
    never has exit 0 for a leak, so this reduces to the old
    known-gap-only check when nothing was measured). A crashed run is
    never a miss — it's a crash."""
    return case.truth == "leak" and not verdict.crashed and verdict.exit == 0


def is_undocumented_miss(case: Case, verdict: Verdict) -> bool:
    """A miss with no ``known_gap`` pinning it — the dangerous direction:
    a real gap nobody has documented yet (or a measured run finding a
    miss on a case believed caught). Always called out distinctly from
    an already-known, pinned gap."""
    return is_miss(case, verdict) and case.known_gap is None


def gap_disagreement(case: Case, verdict: Verdict) -> str | None:
    """A short note for the *reassuring* disagreement: ``--results``
    measured this known-gap case as caught (exit != 0) even though its
    pinned label says today's tool misses it (exit 0) — the gap may
    already be closed, or this run's environment/version differs from
    the one the label was pinned against. None when there's nothing to
    flag (no measured result for this case, no known_gap, a crash, or
    the measured verdict agrees with the label)."""
    if verdict.source != MEASURED or case.known_gap is None or verdict.crashed:
        return None
    if verdict.exit == 0:
        return None
    return (f"Label says known gap (pinned exit 0); this run's measured verdict "
            f"caught it instead (exit {verdict.exit}) — the gap may already be closed.")
