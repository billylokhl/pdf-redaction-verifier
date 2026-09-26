"""The normalised comparison key (docs/REDESIGN.md §5, Phase 0c row).

Two scans of the same file are "the same" for the differential only when
this key matches: exit code, `error.code`, the set of hard findings as
(rule, tier, storage), review warnings as (rule, "review", storage,
adjacency), and every other warning as (code, layer, tool). Exact legacy
warning codes, messages, samples, object/revision/page numbers and the
tool's own return code are deliberately left out — the redesign is
expected to change them without changing the verdict, and the reference
tag predates several of the current stable codes entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FindingKey = tuple[str, str, str]                    # rule, tier, storage
ReviewWarningKey = tuple[str, str, str | None, str | None]   # rule, "review", storage, adjacency
OtherWarningKey = tuple[str, str | None, str | None]         # code, layer, tool
WarningKey = ReviewWarningKey | OtherWarningKey


@dataclass(frozen=True)
class NormalizedKey:
    """The comparison key for one `--json` report."""

    exit: int
    error: str | None
    findings: frozenset[FindingKey]
    warnings: frozenset[WarningKey]

    def to_jsonable(self) -> dict[str, Any]:
        """A stable, sorted, JSON/YAML-friendly form (for caching and for
        `accepted_diffs.yaml`)."""
        return {
            "exit": self.exit,
            "error": self.error,
            "findings": sorted([list(f) for f in self.findings]),
            "warnings": sorted([list(w) for w in self.warnings], key=repr),
        }

    @staticmethod
    def from_jsonable(data: dict[str, Any]) -> "NormalizedKey":
        return NormalizedKey(
            exit=data["exit"],
            error=data.get("error"),
            findings=frozenset(tuple(f) for f in data.get("findings", [])),
            warnings=frozenset(tuple(w) for w in data.get("warnings", [])),
        )

    def diff(self, other: "NormalizedKey") -> tuple[str, ...]:
        """Human-readable description of every difference from *self* (the
        reference) to *other* (the candidate); empty when they agree."""
        problems: list[str] = []
        if self.exit != other.exit:
            problems.append(f"exit {self.exit} -> {other.exit}")
        if self.error != other.error:
            problems.append(f"error {self.error!r} -> {other.error!r}")
        for f in sorted(self.findings - other.findings):
            problems.append(f"finding removed: {f}")
        for f in sorted(other.findings - self.findings):
            problems.append(f"finding added: {f}")
        for w in sorted(self.warnings - other.warnings, key=repr):
            problems.append(f"warning removed: {w}")
        for w in sorted(other.warnings - self.warnings, key=repr):
            problems.append(f"warning added: {w}")
        return tuple(problems)


def normalize(report: dict[str, Any]) -> NormalizedKey:
    """Reduce a `--json` report (verify.py's `build_json_report` shape) to
    its normalised comparison key."""
    error = report.get("error")
    error_code = error["code"] if error else None

    findings: set[FindingKey] = {
        (f["rule"], f["tier"], f["storage"]) for f in report["findings"]
    }

    warnings: set[WarningKey] = set()
    for w in report["warnings"]:
        if w.get("kind") == "review":
            warnings.add((w["rule"], "review", w.get("storage"), w.get("adjacency")))
        else:
            warnings.add((w["code"], w.get("layer"), w.get("tool")))

    return NormalizedKey(
        exit=report["exit_code"],
        error=error_code,
        findings=frozenset(findings),
        warnings=frozenset(warnings),
    )
