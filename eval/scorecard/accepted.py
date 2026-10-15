"""`eval/accepted_diffs.yaml`: every intended per-case change between the
pinned reference (`eval-ref-0`) and the candidate (docs/REDESIGN.md §5).

Each entry pins a case id, the reference's normalised key ("old"), the
candidate's normalised key ("new"), the reason for the change, and the
COVERAGE.md cell and PR it belongs to. The differential
(`scorecard.differential`) fails on any per-case difference that is not
listed here with an exactly matching old/new pair — any unlisted
difference is a regression, not noise.

An entry may be scoped to one environment with `env` (`ocr` or
`no-ocr`, the case library's `requires` vocabulary): the same change
can normalise to different keys where OCR adds its own warnings to both
sides. Absent, it applies everywhere. An entry out of scope for this
run neither matches nor counts as stale here; it is checked where it is
in scope (`entry_in_scope`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .keys import NormalizedKey

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "accepted_diffs.yaml"

# The environments an entry may be scoped to: the case library's own
# OCR-availability vocabulary (`caselib.model.REQUIREMENTS`), which
# `caselib.run.available()` always reports exactly one of.
ENVIRONMENTS = frozenset({"ocr", "no-ocr"})

# This project's verdict severity order (README: "the severity order is
# 0 < 2 < 1" — clean, then cannot-certify, then confirmed leak).
_SEVERITY: dict[int, int] = {0: 0, 2: 1, 1: 2}


def is_weaker(old_exit: int, new_exit: int) -> bool:
    """True when the candidate's exit is less certain/safe than the
    reference's under the severity order above — i.e. 1->2, 1->0, or
    2->0. Every accepted entry that is weaker must say so explicitly
    (`weaker: true`), so a change that quietly loosens the verdict can
    never hide among ordinary accepted diffs."""
    return _SEVERITY[new_exit] < _SEVERITY[old_exit]


@dataclass(frozen=True)
class AcceptedDiff:
    case: str
    old: NormalizedKey
    new: NormalizedKey
    reason: str
    cell: str | None = None
    pr: str | None = None
    weaker: bool = False
    env: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "case": self.case,
            "old": self.old.to_jsonable(),
            "new": self.new.to_jsonable(),
            "reason": self.reason,
        }
        if self.cell is not None:
            entry["cell"] = self.cell
        if self.pr is not None:
            entry["pr"] = self.pr
        if self.weaker:
            entry["weaker"] = True
        if self.env is not None:
            entry["env"] = self.env
        return entry


def _parse_entry(raw: dict[str, Any]) -> AcceptedDiff:
    missing = {"case", "old", "new", "reason"} - raw.keys()
    if missing:
        raise ValueError(f"accepted_diffs.yaml entry missing field(s) {sorted(missing)}: {raw}")
    old = NormalizedKey.from_jsonable(raw["old"])
    new = NormalizedKey.from_jsonable(raw["new"])
    raw_weaker = raw.get("weaker", False)
    if not isinstance(raw_weaker, bool):
        raise ValueError(
            f"accepted_diffs.yaml entry for {raw['case']!r}: `weaker` must be a real YAML "
            f"boolean (true/false), not {raw_weaker!r}"
        )
    weaker = raw_weaker
    env = raw.get("env")
    if "env" in raw and (not isinstance(env, str) or env not in ENVIRONMENTS):
        raise ValueError(
            f"accepted_diffs.yaml entry for {raw['case']!r}: `env` must be one of "
            f"{sorted(ENVIRONMENTS)}, not {env!r} (omit it for an entry that applies "
            "in every environment)"
        )
    actually_weaker = is_weaker(old.exit, new.exit)
    if actually_weaker and not weaker:
        raise ValueError(
            f"accepted_diffs.yaml entry for {raw['case']!r} moves exit {old.exit} -> "
            f"{new.exit}, which is LESS strict under this project's severity order "
            "(0 < 2 < 1) — mark it `weaker: true` if that is really intended"
        )
    if weaker and not actually_weaker:
        raise ValueError(
            f"accepted_diffs.yaml entry for {raw['case']!r} is marked `weaker: true`, but "
            f"exit {old.exit} -> {new.exit} is not actually less strict under this "
            "project's severity order (0 < 2 < 1) — remove the flag, it's misleading"
        )
    return AcceptedDiff(
        case=raw["case"],
        old=old,
        new=new,
        reason=raw["reason"],
        cell=raw.get("cell"),
        pr=raw.get("pr"),
        weaker=weaker,
        env=env,
    )


def load_accepted_diffs(path: Path = DEFAULT_PATH) -> dict[str, list[AcceptedDiff]]:
    """Load `accepted_diffs.yaml` into a {case_id: [AcceptedDiff, ...]} map.
    A missing file is treated as empty (nothing accepted yet)."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a YAML list of entries")
    by_case: dict[str, list[AcceptedDiff]] = {}
    for item in raw:
        entry = _parse_entry(item)
        by_case.setdefault(entry.case, []).append(entry)
    for case_id, entries in by_case.items():
        scoped = {e.env for e in entries if e.env is not None}
        if scoped >= ENVIRONMENTS and any(e.env is None for e in entries):
            raise ValueError(
                f"accepted_diffs.yaml: {case_id!r} has entries scoped to every environment "
                f"({sorted(scoped)}), so its unscoped entry is in scope nowhere and could "
                "never be checked for staleness"
            )
    return by_case


def entry_in_scope(
    accepted: dict[str, list[AcceptedDiff]], entry: AcceptedDiff, have: frozenset[str]
) -> bool:
    """Whether *entry* applies to a run whose capabilities are *have*
    (`caselib.run.available()`).

    A scoped entry applies where its `env` is in *have*. An unscoped one
    applies everywhere, except where an entry for the same case is scoped
    to this environment: that entry takes its place here. This is how an
    existing entry is narrowed without editing it (the file is add-only,
    docs/REDESIGN.md §7a): append an entry scoped to the environment where
    the old one does not hold. The old entry is still matched and checked
    for staleness in every other environment.

    Scoping can only remove an entry from a run, never add one, so it
    cannot accept a difference that the unscoped file would have
    refused."""
    if entry.env is not None:
        return entry.env in have
    return not any(
        other.env is not None and other.env in have for other in accepted.get(entry.case, ())
    )


def find_accepted(
    accepted: dict[str, list[AcceptedDiff]],
    case_id: str,
    old: NormalizedKey,
    new: NormalizedKey,
    *,
    have: frozenset[str],
) -> AcceptedDiff | None:
    """The accepted-diffs entry in scope for *have* (`entry_in_scope`)
    that exactly covers this case's reference -> candidate change, or
    None if this difference is unlisted here."""
    for entry in accepted.get(case_id, ()):
        if entry.old == old and entry.new == new and entry_in_scope(accepted, entry, have):
            return entry
    return None


def dump_accepted_diffs(entries: list[AcceptedDiff], path: Path = DEFAULT_PATH) -> None:
    """Write entries back out, sorted by case id then reason, for a
    reproducible diff when the file is regenerated or extended."""
    ordered = sorted(entries, key=lambda e: (e.case, e.reason))
    payload = [e.to_jsonable() for e in ordered]
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=88)
    path.write_text(text)
