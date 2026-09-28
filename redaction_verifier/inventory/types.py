"""The inventory's own records: a file's byte tiling (docs/REDESIGN.md §4)."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
from typing import Final, TypeAlias

from ..ledger import Flag, Span, Unit, UnitKind, UnitRef


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


@dataclass(frozen=True)
class ObjectStream:
    """An object stream's decoded bytes, tiled in their own coordinates:
    its header's offset pairs (one OBJSTM_HEADER unit) and each member
    (OBJSTM_MEMBER), both ``within`` the stream's OBJECT unit, ``ref``.
    ``members`` is indexed as the header lists them (the index a
    compressed xref entry names), None where no member could be read."""

    ref: UnitRef
    tiling: Tiling
    members: tuple[UnitRef | None, ...]


# An xref entry's history for one object number: (revision, kind, a, b)
# at each revision where it changes, oldest (highest revision) first;
# kind and fields as in xref.Entry.
History: TypeAlias = tuple[tuple[int, int, int, int], ...]
_FREE, _IN_USE, _COMPRESSED = 0, 1, 2


@dataclass(frozen=True)
class Inventory:
    """A file's inventory (docs/REDESIGN.md §4): every byte in exactly
    one region of ``tiling`` (``tiles``: ``check_tiling`` holds on it and
    on every object stream's tiling), the units, flags, and each object
    number's body revision by revision.

    ``units`` holds every unit, sorted by ``UnitRef.sort_key``: the
    top-level ones (their spans in the file), STREAM_SLACK within its
    object (also file offsets: slack sits in the raw bytes) and the
    object-stream units (in the stream's decoded coordinates).
    ``stream_data`` maps the start of each OBJECT or DEAD_BODY unit that
    is a stream to its raw data's span. ``objects`` maps each xref
    offset to the OBJECT unit read there; ``object_streams`` maps the
    offset of each object stream to its decoded tiling.
    ``revisions`` counts the chain's revisions (0 = current)."""

    size: int
    tiling: Tiling
    tiles: bool
    units: tuple[Unit, ...]
    flags: tuple[Flag, ...]
    revisions: int
    stream_data: dict[int, Span]
    objects: dict[int, UnitRef]
    object_streams: dict[int, ObjectStream]
    entries: dict[int, History]

    def entry(self, number: int, revision: int = 0) -> tuple[int, int, int] | None:
        """Object *number*'s xref entry (kind, a, b) in *revision*."""
        history = self.entries.get(number)
        if not history:
            return None
        i = bisect_right(history, -revision, key=lambda change: -change[0]) - 1
        return history[i][1:] if i >= 0 else None

    def body(self, number: int, revision: int = 0) -> UnitRef | None:
        """The unit holding object *number*'s body in *revision*: an
        OBJECT, or an OBJSTM_MEMBER whose home in that same revision lists
        it at the entry's index. None when free, absent or not found."""
        entry = self.entry(number, revision)
        if entry is None or entry[0] == _FREE:
            return None
        kind, a, b = entry
        if kind == _IN_USE:
            return self.objects.get(a)
        home = self.entry(a, revision)
        stream = self.object_streams.get(home[1]) if home and home[0] == _IN_USE else None
        member = stream.members[b] if stream is not None and b < len(stream.members) else None
        return member if member is not None and member.obj == number else None

    def bodies_by_number(self, revision: int = 0) -> dict[int, UnitRef]:
        """Every object number with a body in *revision*, and that body."""
        found = {n: self.body(n, revision) for n in self.entries}
        return {n: ref for n, ref in found.items() if ref is not None}
