"""Byte tiling (docs/REDESIGN.md §4, "Byte tiling").

``tile`` turns the spans the inventory's units claim into a partition of
the whole file: every byte lands in exactly one region, owned by one
unit, contested by several, or claimed by none. Unclaimed PDF whitespace
is harmless; any other unclaimed byte, any byte claimed twice, any claim
past the end of the file and any claim ``tile`` cannot accept is a Flag.
``check_tiling`` is the pure geometric check the parent re-runs on what
the child reports (3b's anchor); it and the PDF-whitespace predicate
live in ``ledger`` (3b-1), so the parent re-runs them without importing
the inventory, and are re-exported here.
"""

from __future__ import annotations

from bisect import insort
from collections.abc import Iterable
from typing import Final, NoReturn

from ..budget import Limits
from ..ledger import WHITESPACE as WHITESPACE
from ..ledger import Flag, FlagReason, Span, UnitKind, UnitRef, first_non_whitespace
from ..ledger import check_tiling as check_tiling
from ..ledger import is_pdf_whitespace
from .types import CONTESTED, Contested, Region, Tiling

_REASON_ORDER: Final = {reason: i for i, reason in enumerate(FlagReason)}
# Kinds no top-level claim may have: the tiling's own gap kinds, and the
# kinds that only ever sit inside another unit (their spans are in its
# coordinates, not the file's).
_UNCLAIMABLE: Final = (UnitKind.WHITESPACE, UnitKind.UNINDEXED,
                       UnitKind.OBJSTM_MEMBER, UnitKind.STREAM_SLACK)
# The kinds that tile an object stream's decoded bytes (``within``).
_IN_OBJECT_STREAM: Final = (UnitKind.OBJSTM_HEADER, UnitKind.OBJSTM_MEMBER)


def _assert_never(value: NoReturn) -> NoReturn:
    # typing.assert_never is 3.11+; the floor is 3.10.
    raise AssertionError(f"unhandled case: {value!r}")


def tile(raw: bytes, claims: Iterable[tuple[UnitRef, Span]],
         limits: Limits | None = None,
         within: UnitRef | None = None) -> tuple[Tiling, tuple[Flag, ...]]:
    """Partition *raw* into regions by an elementary-interval sweep.

    With *within* (an object stream's unit), *raw* is that stream's
    decoded bytes instead of the file, and every claim must be an
    OBJSTM_HEADER or OBJSTM_MEMBER nested directly in it -- the same
    object, checked by identity so no nested ref is ever hashed before
    it is accepted; anything else is refused as below.

    A claim is a top-level unit and one span of the file it covers.
    Refused and flagged CLAIM_INVALID, before the ref is ever hashed or
    compared: a nested ref (``within`` set; its span is in its parent's
    coordinates, not the file's), a claim of a gap kind (WHITESPACE,
    UNINDEXED) and a top-level claim of a kind that only sits inside
    another unit (OBJSTM_MEMBER, STREAM_SLACK). A claim past the end of the file is clipped to it and
    flagged CLAIM_OUT_OF_RANGE. One unit's claims are merged: an exact
    duplicate span is dropped quietly, a partial overlap is flagged
    SELF_OVERLAP. Where two or more units claim a byte, the run of such
    bytes is one CONTESTED region with one CONTESTED_SPAN flag carrying
    the true claimant count; it lists at most ``max_contested_owners`` of
    them. Regions are maximal. An owned region is labelled with the
    claim whose (obj, gen) is smallest among its claims that cover bytes
    of the file, so the result, flags included,
    does not depend on claim order.

    Iterative; O(n log n) time and O(n) regions and flags for n claims,
    however they overlap, plus one scan of the unclaimed bytes. Never
    raises on any *raw*.
    """
    size = len(raw)
    cap = (limits if limits is not None else Limits()).max_contested_owners
    flags: list[Flag] = []
    spans_by_ref: dict[UnitRef, list[tuple[int, int]]] = {}
    label: dict[UnitRef, UnitRef] = {}
    for ref, span in claims:
        start, end = span.start, span.end
        refused = (ref.kind in _UNCLAIMABLE if within is None
                   else ref.kind not in _IN_OBJECT_STREAM)
        if ref.within is not within or refused:  # before any hashing
            flags.append(Flag(FlagReason.CLAIM_INVALID, Span(min(start, size), min(end, size)), (
                ("claim_start", start), ("claim_end", end),
                ("nested", int(ref.within is not None)))))
            continue
        if end > size:
            flags.append(Flag(FlagReason.CLAIM_OUT_OF_RANGE, Span(min(start, size), size), (
                ("claim_start", start), ("claim_end", end), ("size", size))))
            start, end = min(start, size), size
        if start < end:
            known = label.get(ref)
            if known is None or ref.label_key() < known.label_key():
                label[ref] = ref
            spans_by_ref.setdefault(ref, []).append((start, end))

    # Each unit's spans, merged into disjoint, non-abutting runs.
    owners: list[UnitRef] = []
    events: list[tuple[int, int]] = []  # (offset, i) for a start, (offset, ~i) for an end
    for ref, spans in spans_by_ref.items():
        i = len(owners)
        owners.append(label[ref])
        ordered = sorted(set(spans))
        run_start, run_end = ordered[0]
        for start, end in ordered[1:]:
            if start < run_end:
                flags.append(Flag(FlagReason.SELF_OVERLAP, Span(start, min(end, run_end))))
            if start <= run_end:
                run_end = max(run_end, end)
                continue
            events += [(run_start, i), (run_end, ~i)]
            run_start, run_end = start, end
        events += [(run_start, i), (run_end, ~i)]
    events.sort()

    sweep = _Sweep(raw, owners, cap)
    pos = 0
    started: list[int] = []
    k = 0
    while k < len(events):
        at = events[k][0]
        if at > pos:
            sweep.piece(pos, at, started)
            pos = at
        started = []
        while k < len(events) and events[k][0] == at:
            code = events[k][1]
            if code >= 0:
                sweep.active.add(code)
                started.append(code)
            else:
                sweep.active.discard(~code)
            k += 1
        sweep.compact()
    if pos < size:
        sweep.piece(pos, size, started)

    for region in sweep.regions:
        flags.extend(_region_flags(raw, region))
    flags.sort(key=_flag_key)
    return Tiling(size, tuple(sweep.regions)), tuple(flags)


class _Sweep:
    """The sweep's state: the claimants active between two event offsets
    and the regions so far, including the open contested run's members
    (so each claim joins a run once: O(n) over the whole sweep)."""

    def __init__(self, raw: bytes, owners: list[UnitRef], cap: int) -> None:
        self.raw, self.owners, self.cap = raw, owners, max(cap, 0)
        self.active: set[int] = set()
        self.peak = 0  # the most claimants active since the last compact()
        self.regions: list[Region] = []
        self.members: set[int] = set()
        self.listed: list[UnitRef] = []

    def compact(self) -> None:
        """Rebuild ``active`` once it falls below a quarter of its peak.
        A Python set never shrinks its table on removal, and iterating
        it costs the table's size, not its length: without this, n
        claims all active at once make every later iteration O(n), and
        the sweep quadratic. The rebuild costs the peak, paid for by the
        removals since it, so the sweep stays linear in events."""
        self.peak = max(self.peak, len(self.active))
        if len(self.active) * 4 < self.peak:
            self.active = set(self.active)
            self.peak = len(self.active)

    def piece(self, start: int, end: int, started: list[int]) -> None:
        last = self.regions[-1] if self.regions else None
        if len(self.active) >= 2:
            if last is not None and last.kind is CONTESTED:
                newcomers: Iterable[int] = started
                start = self.regions.pop().span.start
            else:
                # The previous piece had at most one claimant, so the
                # active set is it plus this offset's starts.
                self.members, self.listed = set(), []
                newcomers = self.active
            for i in newcomers:
                if i not in self.members:
                    self.members.add(i)
                    insort(self.listed, self.owners[i], key=UnitRef.sort_key)
                    del self.listed[self.cap:]
            self.regions.append(
                Region(Span(start, end), CONTESTED, tuple(self.listed), len(self.members)))
            return
        region_owners: tuple[UnitRef, ...]
        kind: UnitKind
        if self.active:
            region_owners = (self.owners[next(iter(self.active))],)
            kind = region_owners[0].kind
        else:
            region_owners = ()
            blank = is_pdf_whitespace(self.raw, start, end)
            kind = UnitKind.WHITESPACE if blank else UnitKind.UNINDEXED
        if last is not None and last.kind is kind and last.owners == region_owners:
            start = self.regions.pop().span.start
        self.regions.append(Region(Span(start, end), kind, region_owners, len(region_owners)))


def _region_flags(raw: bytes, region: Region) -> list[Flag]:
    span, kind = region.span, region.kind
    match kind:
        case Contested.CONTESTED:
            return [Flag(FlagReason.CONTESTED_SPAN, span, (("claimants", region.claimants),))]
        case UnitKind.UNINDEXED if not region.owners:
            first = first_non_whitespace(raw, span.start, span.end)
            whitespace = sum(raw.count(byte, span.start, span.end) for byte in WHITESPACE)
            return [Flag(FlagReason.UNINDEXED_NON_WHITESPACE, span, (
                ("first_non_whitespace", first if first is not None else span.start),
                ("non_whitespace_bytes", len(span) - whitespace)))]
        case UnitKind():
            return []
        case _:
            _assert_never(kind)


def _flag_key(flag: Flag) -> tuple[int, int, int, tuple[tuple[str, int], ...]]:
    span = flag.span
    # `is None`, not truthiness: an empty Span has len 0.
    return (span.start if span is not None else -1, span.end if span is not None else -1,
            _REASON_ORDER[flag.reason], flag.params)
