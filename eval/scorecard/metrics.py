"""Scorecard metrics, defined against case labels (docs/REDESIGN.md §5):

| Metric          | Definition                              | Measured on            |
| --------------- | ---------------------------------------- | ----------------------- |
| Silent miss     | Leak case, actual exit 0                 | Labelled cases only     |
| Downgrade       | Expected 1, actual 2                     | Labelled cases only     |
| False hard      | Clean case with a hard finding           | Labelled clean + corpus |
| Review rate     | Clean case, actual exit 2                | Labelled clean + corpus |
| Crash / timeout | As named                                 | All                     |
| Runtime         | Per file, p50 / p95                      | All                     |

`CaseMetricRow` is the input row shape; it is deliberately independent of
`scorecard.differential.CaseDiff` so the metrics can also be computed for
a candidate-only run (no reference needed) and, with `truth` fixed to
"clean", for the real-world corpus (`scorecard.corpus`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class CaseMetricRow:
    """One file's outcome, reduced to what the metrics need."""

    case_id: str
    truth: str  # "leak" or "clean"
    expected_exit: int | None  # None when there is no label (real corpus)
    actual_exit: int | None  # None if crashed or timed out
    has_hard_finding: bool
    crashed: bool
    timed_out: bool
    elapsed: float


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = q * (len(sorted_values) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


@dataclass(frozen=True)
class Scorecard:
    total: int
    leak_cases: int
    clean_cases: int
    silent_miss: int
    downgrade: int
    false_hard: int
    review_rate: int
    crashes: int
    timeouts: int
    runtime_p50: float
    runtime_p95: float

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "leak_cases": self.leak_cases,
            "clean_cases": self.clean_cases,
            "silent_miss": self.silent_miss,
            "downgrade": self.downgrade,
            "false_hard": self.false_hard,
            "review_rate": self.review_rate,
            "crashes": self.crashes,
            "timeouts": self.timeouts,
            "runtime_p50_s": round(self.runtime_p50, 3),
            "runtime_p95_s": round(self.runtime_p95, 3),
        }


def compute_metrics(rows: Sequence[CaseMetricRow]) -> Scorecard:
    leak = [r for r in rows if r.truth == "leak"]
    clean = [r for r in rows if r.truth == "clean"]

    silent_miss = sum(
        1 for r in leak if not r.crashed and not r.timed_out and r.actual_exit == 0
    )
    downgrade = sum(
        1
        for r in leak
        if not r.crashed
        and not r.timed_out
        and r.expected_exit == 1
        and r.actual_exit == 2
    )
    false_hard = sum(
        1 for r in clean if not r.crashed and not r.timed_out and r.has_hard_finding
    )
    review_rate = sum(
        1 for r in clean if not r.crashed and not r.timed_out and r.actual_exit == 2
    )
    crashes = sum(1 for r in rows if r.crashed and not r.timed_out)
    timeouts = sum(1 for r in rows if r.timed_out)

    times = sorted(r.elapsed for r in rows)
    return Scorecard(
        total=len(rows),
        leak_cases=len(leak),
        clean_cases=len(clean),
        silent_miss=silent_miss,
        downgrade=downgrade,
        false_hard=false_hard,
        review_rate=review_rate,
        crashes=crashes,
        timeouts=timeouts,
        runtime_p50=_percentile(times, 0.50),
        runtime_p95=_percentile(times, 0.95),
    )
