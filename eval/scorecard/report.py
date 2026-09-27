"""Render the scorecard as a text table and as JSON."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .accepted import AcceptedDiff
from .differential import CaseDiff
from .metrics import Scorecard


def render_metrics_table(scorecard: Scorecard) -> str:
    rows = [
        ("cases", str(scorecard.total)),
        ("leak cases", str(scorecard.leak_cases)),
        ("clean cases", str(scorecard.clean_cases)),
        ("silent miss", f"{scorecard.silent_miss} / {scorecard.leak_cases}"),
        ("downgrade", f"{scorecard.downgrade} / {scorecard.leak_cases}"),
        ("false hard", f"{scorecard.false_hard} / {scorecard.clean_cases}"),
        ("review rate", f"{scorecard.review_rate} / {scorecard.clean_cases}"),
        ("crashes", str(scorecard.crashes)),
        ("timeouts", str(scorecard.timeouts)),
        ("runtime p50", f"{scorecard.runtime_p50:.3f}s"),
        ("runtime p95", f"{scorecard.runtime_p95:.3f}s"),
    ]
    width = max(len(name) for name, _ in rows)
    lines = [f"{name:<{width}}  {value}" for name, value in rows]
    return "\n".join(lines)


def render_stratified_table(strata: Mapping[str, Scorecard]) -> str:
    blocks = []
    for name in sorted(strata):
        blocks.append(f"[{name}]\n{render_metrics_table(strata[name])}")
    return "\n\n".join(blocks)


def render_differential_summary(
    diffs: list[CaseDiff], stale: Sequence[AcceptedDiff] = ()
) -> str:
    total = len(diffs)
    changed = [d for d in diffs if d.changed]
    unlisted = [d for d in diffs if d.unlisted]
    crashed = [d for d in diffs if d.crashed]
    weaker = [d for d in diffs if d.accepted is not None and d.accepted.weaker]
    lines = [
        f"cases compared : {total}",
        f"changed        : {len(changed)} ({len(changed) - len(unlisted)} accepted, "
        f"{len(unlisted)} unlisted)",
        f"crashed        : {len(crashed)}",
        f"stale accepted : {len(stale)}",
    ]
    if weaker:
        lines.append("")
        lines.append("WEAKER (accepted, but the verdict got LESS strict — 1->2, 1->0, or 2->0):")
        for d in weaker:
            lines.append(f"  {d.case_id}: {d.describe()}")
    if unlisted:
        lines.append("")
        lines.append("UNLISTED DIFFERENCES (not in eval/accepted_diffs.yaml):")
        for d in unlisted:
            lines.append(f"  {d.case_id}: {d.describe()}")
    if crashed:
        lines.append("")
        lines.append("CRASHES:")
        for d in crashed:
            lines.append(f"  {d.case_id}: {d.describe()}")
    if stale:
        lines.append("")
        lines.append("STALE accepted_diffs.yaml entries (no case in this run produced them):")
        for entry in stale:
            lines.append(f"  {entry.case}: {entry.reason}")
    return "\n".join(lines)


def differential_to_jsonable(
    diffs: list[CaseDiff], stale: Sequence[AcceptedDiff] = ()
) -> dict[str, Any]:
    return {
        "total": len(diffs),
        "changed": sum(1 for d in diffs if d.changed),
        "unlisted": sum(1 for d in diffs if d.unlisted),
        "crashed": sum(1 for d in diffs if d.crashed),
        "stale_accepted": [entry.case for entry in stale],
        "cases": [
            {
                "case": d.case_id,
                "reference": d.reference.key.to_jsonable() if d.reference.key else None,
                "candidate": d.candidate.key.to_jsonable() if d.candidate.key else None,
                "changed": d.changed,
                "accepted": d.accepted is not None,
                "weaker": bool(d.accepted and d.accepted.weaker),
                "reason": d.accepted.reason if d.accepted else None,
                "crashed": d.crashed,
                "description": d.describe() if (d.changed or d.crashed) else None,
            }
            for d in diffs
        ],
    }


def dump_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)
