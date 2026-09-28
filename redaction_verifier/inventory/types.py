"""The inventory's own records: a file's byte tiling (docs/REDESIGN.md §4)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..ledger import Span, UnitKind, UnitRef


class Contested(Enum):
    """Sentinel kind for bytes two or more units claim (a flag, §4)."""

    CONTESTED = "contested"


CONTESTED: Final = Contested.CONTESTED


@dataclass(frozen=True)
class Region:
    """A maximal run of bytes: unclaimed (kind WHITESPACE or UNINDEXED,
    no owners), owned by one unit (kind = its kind), or claimed by two or
    more (CONTESTED). Adjacent contested bytes form one region whatever
    their claimant sets; ``claimants`` counts every distinct unit
    claiming any of its bytes, and ``owners`` lists the first
    ``Limits.max_contested_owners`` of them in ``UnitRef.sort_key``
    order. For the other kinds ``claimants == len(owners)``."""

    span: Span
    kind: UnitKind | Contested
    owners: tuple[UnitRef, ...]
    claimants: int


@dataclass(frozen=True)
class Tiling:
    """Every byte of a *size*-byte file in exactly one region: sorted,
    contiguous, non-empty, from 0 to size (``check_tiling`` holds)."""

    size: int
    regions: tuple[Region, ...]
