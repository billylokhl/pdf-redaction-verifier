"""Byte tiling (docs/REDESIGN.md §4, "Byte tiling").

``tile`` turns the spans the inventory's units claim into a partition of
the whole file: every byte lands in exactly one region, owned by one
unit, contested by several, or claimed by none. Unclaimed PDF whitespace
is harmless; any other unclaimed byte, any byte claimed twice and any
claim past the end of the file is a Flag. ``check_tiling`` is the pure
check the parent re-runs on what the child reports (3b's anchor).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, NoReturn

from ..model import Flag, FlagReason, Span, UnitKind, UnitRef
from .types import CONTESTED, Contested, Region, Tiling

# PDF white-space characters (ISO 32000-1 §7.2.2, Table 1).
WHITESPACE: Final = b"\0\t\n\f\r "
_NON_WHITESPACE: Final = re.compile(rb"[^\x00\t\n\x0c\r ]")
_REASON_ORDER: Final = {reason: i for i, reason in enumerate(FlagReason)}


def _assert_never(value: NoReturn) -> NoReturn:
    # typing.assert_never is 3.11+; the floor is 3.10.
    raise AssertionError(f"unhandled case: {value!r}")


def tile(raw: bytes, claims: Iterable[tuple[UnitRef, Span]]) -> tuple[Tiling, tuple[Flag, ...]]:
    """Partition *raw* into regions by an elementary-interval sweep.

    A claim is a unit and one span it covers; a unit claiming the same
    byte twice still owns it alone (claimants are distinct UnitRefs). A
    claim reaching past the end of the file is clipped to it and flagged
    CLAIM_OUT_OF_RANGE. Adjacent pieces with the same kind and owners are
    merged, so regions are maximal. The result, flags included, does not
    depend on claim order.

    Iterative; O(n log n) in the number of claims n, plus one scan of the
    unclaimed bytes and the size of the contested regions' owner tuples
    (none in a well-formed file). Never raises on any *raw*.
    """
    size = len(raw)
    flags: list[Flag] = []
    refs: list[UnitRef] = []
    events: list[tuple[int, int]] = []  # (offset, claim index); starts and ends alike
    for ref, span in claims:
        start, end = span.start, span.end
        if end > size:
            flags.append(Flag(FlagReason.CLAIM_OUT_OF_RANGE, None, (
                ("claim_start", start), ("claim_end", end), ("size", size))))
            start, end = min(start, size), size
        if start < end:
            events.append((start, len(refs)))
            events.append((end, ~len(refs)))  # an end: the index, bit-inverted
            refs.append(ref)
    events.sort()

    active: dict[UnitRef, int] = {}  # claimant -> how many of its claims cover here
    regions: list[Region] = []
    pos = 0
    for at, code in events:
        if at > pos:
            _append(regions, raw, pos, at, active)
            pos = at
        ref = refs[code if code >= 0 else ~code]
        count = active.get(ref, 0) + (1 if code >= 0 else -1)
        if count:
            active[ref] = count
        else:
            del active[ref]
    if pos < size:
        _append(regions, raw, pos, size, active)

    for region in regions:
        flags.extend(_region_flags(raw, region))
    flags.sort(key=_flag_key)
    return Tiling(size, tuple(regions)), tuple(flags)


def _append(regions: list[Region], raw: bytes, start: int, end: int,
            active: dict[UnitRef, int]) -> None:
    owners: tuple[UnitRef, ...]
    kind: UnitKind | Contested
    if not active:
        owners = ()
        found = _NON_WHITESPACE.search(raw, start, end) is not None
        kind = UnitKind.UNINDEXED if found else UnitKind.WHITESPACE
    elif len(active) == 1:
        owners = tuple(active)
        kind = owners[0].kind
    else:
        owners = tuple(sorted(active, key=UnitRef.sort_key))
        kind = CONTESTED
    if regions and regions[-1].kind == kind and regions[-1].owners == owners:
        start = regions.pop().span.start
    regions.append(Region(Span(start, end), kind, owners))


def _region_flags(raw: bytes, region: Region) -> list[Flag]:
    span, kind = region.span, region.kind
    match kind:
        case Contested.CONTESTED:
            total = len(region.owners)
            return [Flag(FlagReason.CONTESTED_SPAN, span, (("claimant", i), ("claimants", total)))
                    for i in range(total)]
        case UnitKind.UNINDEXED if not region.owners:
            first = _NON_WHITESPACE.search(raw, span.start, span.end)
            whitespace = sum(raw.count(byte, span.start, span.end) for byte in WHITESPACE)
            return [Flag(FlagReason.UNINDEXED_NON_WHITESPACE, span, (
                ("first_non_whitespace", first.start() if first else span.start),
                ("non_whitespace_bytes", len(span) - whitespace)))]
        case UnitKind():
            return []
        case _:
            _assert_never(kind)


def _flag_key(flag: Flag) -> tuple[int, int, int, tuple[tuple[str, int], ...]]:
    span = flag.span
    return (span.start if span else -1, span.end if span else -1,
            _REASON_ORDER[flag.reason], flag.params)


def check_tiling(size: int, spans: Iterable[Span]) -> bool:
    """Do *spans*, in order, tile ``[0, size)`` exactly: each non-empty
    and starting where the previous one ended? Pure; never raises on
    well-typed input and answers False to anything malformed."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return False
    pos = 0
    for span in spans:
        if not isinstance(span, Span) or span.start != pos or span.end <= span.start:
            return False
        pos = span.end
    return pos == size
