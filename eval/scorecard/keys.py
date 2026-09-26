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

# The only tool names `have` (caselib.run.available()'s shape) ever uses.
# A warning's own "tool" field is untrusted input as far as this module is
# concerned — it must match one of these *exactly* to be treated as
# environmental at all; anything else (None, an absolute path, different
# casing, a future tool this code doesn't know about) is kept as a real
# warning instead of being silently dropped just because it fails to
# appear in `have`.
_KNOWN_TOOLS: frozenset[str] = frozenset({"qpdf", "exiftool", "ocr"})


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
    return tool in _KNOWN_TOOLS and tool not in have


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
    """The reported exit code, or — when *have* is given, the report is
    self-consistent, and dropping environmental warnings would change the
    verdict — the exit code the file would have gotten without them
    (mirrors `caselib.run.judge`).

    Two guards keep this from ever manufacturing a "corrected" exit that
    hides a real bug:

    - An error report (`report["error"]` set) is never touched: the
      crash escape hatch does not follow the findings/warnings formula
      at all (verify.py can raise TOOL_MISSING before ever reaching
      fitz.open, for instance), so recomputing from it would turn a
      genuine `PDF_PASSWORD`/`INTERNAL_ERROR` exit into a bogus `0`.
    - The *unfiltered* report must already satisfy this project's own
      exit formula (`1` if findings, else `2` if any warning, else `0`).
      If it does not — a fail-open bug reporting `0` despite a warning
      being present, say — the mismatch is real information: keeping the
      reported (wrong) exit lets it surface as a difference instead of
      being silently "fixed" by recomputing from the same warnings that
      the report itself failed to act on.
    """
    reported = int(report["exit_code"])
    if report.get("error"):
        return reported
    if have is None:
        return reported
    findings = report["findings"]
    all_warnings = report["warnings"]
    self_consistent = reported == (1 if findings else 2 if all_warnings else 0)
    if not self_consistent:
        return reported
    kept = filtered_warnings(report, have)
    if len(kept) == len(all_warnings):
        return reported
    return 1 if findings else 2 if kept else 0


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
