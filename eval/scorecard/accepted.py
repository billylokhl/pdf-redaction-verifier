"""`eval/accepted_diffs.yaml`: every intended per-case change between the
pinned reference (`eval-ref-0`) and the candidate (docs/REDESIGN.md §5).

Each entry pins a case id, the reference's normalised key ("old"), the
candidate's normalised key ("new"), the reason for the change, and the
COVERAGE.md cell and PR it belongs to. The differential
(`scorecard.differential`) fails on any per-case difference that is not
listed here with an exactly matching old/new pair — any unlisted
difference is a regression, not noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .keys import NormalizedKey

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "accepted_diffs.yaml"


@dataclass(frozen=True)
class AcceptedDiff:
    case: str
    old: NormalizedKey
    new: NormalizedKey
    reason: str
    cell: str | None = None
    pr: str | None = None

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
        return entry


def _parse_entry(raw: dict[str, Any]) -> AcceptedDiff:
    missing = {"case", "old", "new", "reason"} - raw.keys()
    if missing:
        raise ValueError(f"accepted_diffs.yaml entry missing field(s) {sorted(missing)}: {raw}")
    return AcceptedDiff(
        case=raw["case"],
        old=NormalizedKey.from_jsonable(raw["old"]),
        new=NormalizedKey.from_jsonable(raw["new"]),
        reason=raw["reason"],
        cell=raw.get("cell"),
        pr=raw.get("pr"),
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
    return by_case


def find_accepted(
    accepted: dict[str, list[AcceptedDiff]], case_id: str, old: NormalizedKey, new: NormalizedKey
) -> AcceptedDiff | None:
    """The accepted-diffs entry that exactly covers this case's reference
    -> candidate change, or None if this difference is unlisted."""
    for entry in accepted.get(case_id, ()):
        if entry.old == old and entry.new == new:
            return entry
    return None


def dump_accepted_diffs(entries: list[AcceptedDiff], path: Path = DEFAULT_PATH) -> None:
    """Write entries back out, sorted by case id then reason, for a
    reproducible diff when the file is regenerated or extended."""
    ordered = sorted(entries, key=lambda e: (e.case, e.reason))
    payload = [e.to_jsonable() for e in ordered]
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=88)
    path.write_text(text)
