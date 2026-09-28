"""The inventory's own records: a file's byte tiling (docs/REDESIGN.md §4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..model import Span, UnitKind, UnitRef


class Contested(Enum):
    """Sentinel kind for bytes two or more units claim (a flag, §4)."""

    CONTESTED = "contested"


CONTESTED: Final = Contested.CONTESTED


@dataclass(frozen=True)
class Region:
    """A maximal run of bytes with one claimant set. ``owners`` is empty
    for an unclaimed gap (kind WHITESPACE or UNINDEXED), the one claimant
    for an owned region (kind = its kind), and every claimant, in
    ``UnitRef.sort_key`` order, for a CONTESTED one."""

    span: Span
    kind: UnitKind | Contested
    owners: tuple[UnitRef, ...]


@dataclass(frozen=True)
class Tiling:
    """Every byte of a *size*-byte file in exactly one region: sorted,
    contiguous, non-empty, from 0 to size (``check_tiling`` holds)."""

    size: int
    regions: tuple[Region, ...]
