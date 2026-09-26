"""The normalised comparison key (docs/REDESIGN.md §5, Phase 0c row).

Two scans of the same file are "the same" for the differential only when
this key matches: exit code, `error.code`, the *multiset* of hard
findings as (rule, tier, storage), review warnings as (rule, "review",
storage, adjacency), and every other warning as (code, layer, tool) —
a multiset, not a set, so losing one of two otherwise-identical findings
(e.g. the SSN in two separate orphaned streams) is a real, visible
difference rather than silently absorbed by set deduplication. Exact
legacy warning codes, messages, samples, object/revision/page numbers
and the tool's own return code are deliberately left out — the redesign
is expected to change them without changing the verdict, and the
reference tag predates several of the current stable codes entirely.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

FindingKey = tuple[str, str, str]                    # rule, tier, storage
ReviewWarningKey = tuple[str, str, str | None, str | None]   # rule, "review", storage, adjacency
OtherWarningKey = tuple[str, str | None, str | None]         # code, layer, tool
WarningKey = ReviewWarningKey | OtherWarningKey
FindingCounts = frozenset[tuple[FindingKey, int]]            # (key, count) pairs
WarningCounts = frozenset[tuple[WarningKey, int]]


def _counts(items: Iterable[Any]) -> frozenset[tuple[Any, int]]:
    """A hashable multiset: (item, count) pairs, one per distinct item."""
    return frozenset(Counter(items).items())

# A warning whose presence only reflects a missing local tool, not a real
# coverage gap — dropped from the key (and the exit code recomputed
# without it), exactly as `caselib.run.judge`/`_environmental` does, so
# the same case compares equal whether the reference and candidate ran
# with or without OCR/qpdf/exiftool. Value is the tool name the code is
# about, or None to use the warning's own "tool" field
# (docs/REDESIGN.md §5, Phase 0c: "drop environment-only warnings as
# caselib.run does").
_ENVIRONMENTAL_CODES: dict[str, str | None] = {"OCR_UNAVAILABLE": "ocr", "TOOL_MISSING": None}


def is_environmental(warning: dict[str, Any], have: frozenset[str]) -> bool:
    """True for a warning that only says this machine lacks a tool, per
    *have* — this is a pure function of its two arguments, nothing else
    (in particular, it never reads the process environment: a caller on a
    full-environment job that wants "no tool is ever environmental here"
    gets that by passing `have=None` through to `normalize`/
    `effective_exit`, not by this function noticing REQUIRE_FULL_ENV
    itself)."""
    code = warning.get("code")
    if code not in _ENVIRONMENTAL_CODES:
        return False
    tool = _ENVIRONMENTAL_CODES[code] or warning.get("tool")
    return tool not in have


@dataclass(frozen=True)
class NormalizedKey:
    """The comparison key for one `--json` report. *findings* and
    *warnings* are multisets — (key, count) pairs — not sets: two
    identical findings (e.g. the same rule in two separate orphaned
    streams) collapsing to one is itself a regression the differential
    must see."""

    exit: int
    error: str | None
    findings: FindingCounts
    warnings: WarningCounts

    def to_jsonable(self) -> dict[str, Any]:
        """A stable, sorted, JSON/YAML-friendly form (for caching and for
        `accepted_diffs.yaml`)."""
        return {
            "exit": self.exit,
            "error": self.error,
            "findings": sorted([[list(f), n] for f, n in self.findings]),
            "warnings": sorted([[list(w), n] for w, n in self.warnings], key=repr),
        }

    @staticmethod
    def from_jsonable(data: dict[str, Any]) -> "NormalizedKey":
        return NormalizedKey(
            exit=data["exit"],
            error=data.get("error"),
            findings=frozenset((tuple(f), n) for f, n in data.get("findings", [])),
            warnings=frozenset((tuple(w), n) for w, n in data.get("warnings", [])),
        )

    def diff(self, other: "NormalizedKey") -> tuple[str, ...]:
        """Human-readable description of every difference from *self* (the
        reference) to *other* (the candidate); empty when they agree."""
        problems: list[str] = []
        if self.exit != other.exit:
            problems.append(f"exit {self.exit} -> {other.exit}")
        if self.error != other.error:
            problems.append(f"error {self.error!r} -> {other.error!r}")
        problems.extend(_diff_counts("finding", self.findings, other.findings))
        problems.extend(_diff_counts("warning", self.warnings, other.warnings))
        return tuple(problems)


def _diff_counts(
    label: str, before: frozenset[tuple[Any, int]], after: frozenset[tuple[Any, int]]
) -> list[str]:
    before_counts = dict(before)
    after_counts = dict(after)
    problems: list[str] = []
    for key in sorted({*before_counts, *after_counts}, key=repr):
        a, b = before_counts.get(key, 0), after_counts.get(key, 0)
        if a and not b:
            suffix = f" x{a}" if a > 1 else ""
            problems.append(f"{label} removed: {key}{suffix}")
        elif b and not a:
            suffix = f" x{b}" if b > 1 else ""
            problems.append(f"{label} added: {key}{suffix}")
        elif a != b:
            problems.append(f"{label} count changed: {key} {a} -> {b}")
    return problems


def filtered_warnings(
    report: dict[str, Any], have: frozenset[str] | None = None
) -> list[dict[str, Any]]:
    """report["warnings"], minus any environmental ones — unfiltered
    (every warning kept) when *have* is None."""
    if have is None:
        return list(report["warnings"])
    return [w for w in report["warnings"] if not is_environmental(w, have)]


def effective_exit(report: dict[str, Any], have: frozenset[str] | None = None) -> int:
    """The reported exit code, or — when *have* is given and dropping
    environmental warnings would change the verdict — the exit code the
    file would have gotten without them (mirrors `caselib.run.judge`)."""
    if have is None:
        return int(report["exit_code"])
    kept = filtered_warnings(report, have)
    if len(kept) == len(report["warnings"]):
        return int(report["exit_code"])
    return 1 if report["findings"] else 2 if kept else 0


def normalize(report: dict[str, Any], *, have: frozenset[str] | None = None) -> NormalizedKey:
    """Reduce a `--json` report (verify.py's `build_json_report` shape) to
    its normalised comparison key. *have* (the tools/OCR this machine has
    — `caselib.run.available()`'s shape) drops environmental warnings and
    recomputes the exit code without them, so the key is comparable
    across environments; omit it to keep the report exactly as reported
    (e.g. in a unit test building a report dict by hand)."""
    error = report.get("error")
    error_code = error["code"] if error else None

    finding_keys: list[FindingKey] = [
        (f["rule"], f["tier"], f["storage"]) for f in report["findings"]
    ]

    warning_keys: list[WarningKey] = []
    for w in filtered_warnings(report, have):
        if w.get("kind") == "review":
            warning_keys.append((w["rule"], "review", w.get("storage"), w.get("adjacency")))
        else:
            warning_keys.append((w["code"], w.get("layer"), w.get("tool")))

    return NormalizedKey(
        exit=effective_exit(report, have),
        error=error_code,
        findings=_counts(finding_keys),
        warnings=_counts(warning_keys),
    )
