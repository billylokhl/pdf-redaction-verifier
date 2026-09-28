"""Byte tiling (docs/REDESIGN.md §4, Phase 3a): ``tile`` partitions every
file exactly and ``check_tiling`` is its independent check. Property
tests compare ``tile`` against a per-byte oracle."""

from __future__ import annotations

import time
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from redaction_verifier.budget import Limits
from redaction_verifier.inventory import CONTESTED, Region, check_tiling, tile
from redaction_verifier.inventory.tiling import WHITESPACE
from redaction_verifier.model import Flag, FlagReason, Span, UnitKind, UnitRef

REF_A = UnitRef(UnitKind.OBJECT, 10, obj=1, gen=0)
REF_B = UnitRef(UnitKind.OBJECT, 20, obj=2, gen=0)
UNCLAIMABLE = (UnitKind.WHITESPACE, UnitKind.UNINDEXED, UnitKind.OBJSTM_MEMBER,
               UnitKind.STREAM_SLACK)

Claims = list[tuple[UnitRef, Span]]

_refs = st.builds(
    UnitRef,
    kind=st.sampled_from(UnitKind),  # unclaimable kinds included: tile must refuse them
    start=st.integers(0, 4),  # a small pool, so claimants coincide
    within=st.none() | st.none() | st.builds(UnitRef, kind=st.just(UnitKind.OBJECT),
                                             start=st.integers(0, 2)),
    obj=st.none() | st.integers(0, 3),
    gen=st.none() | st.integers(0, 1),
)
_raw = st.binary(max_size=60) | st.lists(
    st.sampled_from(list(WHITESPACE + b"A%")), max_size=60).map(bytes)


@st.composite
def _case(draw: st.DrawFn) -> tuple[bytes, Claims, int]:
    raw = draw(_raw)
    limit = len(raw) + 5  # some claims run past the end
    claims = []
    for _ in range(draw(st.integers(0, 10))):
        start = draw(st.integers(0, limit))
        claims.append((draw(_refs), Span(start, draw(st.integers(start, limit)))))
    return raw, claims, draw(st.sampled_from([0, 1, 2, 16]))


def _valid(ref: UnitRef) -> bool:
    return ref.within is None and ref.kind not in UNCLAIMABLE


def _clipped(size: int, claims: Claims) -> Claims:
    """The valid claims, clipped to the file, non-empty ones only."""
    out = []
    for ref, span in claims:
        start, end = min(span.start, size), min(span.end, size)
        if _valid(ref) and start < end:
            out.append((ref, Span(start, end)))
    return out


@given(_case())
def test_tile_partitions_the_file_like_a_per_byte_oracle(case: tuple[bytes, Claims, int]) -> None:
    raw, claims, cap = case
    size = len(raw)
    tiling, flags = tile(raw, claims, Limits(max_contested_owners=cap))
    regions = tiling.regions
    assert tiling.size == size
    assert check_tiling(size, [r.span for r in regions])

    # Sorted, contiguous, non-empty, [0, size) -- checked directly too.
    pos = 0
    for region in regions:
        assert region.span.start == pos < region.span.end
        pos = region.span.end
    assert pos == size

    valid = _clipped(size, claims)
    for region in regions:
        at = [{ref for ref, s in valid if s.start <= i < s.end}
              for i in range(region.span.start, region.span.end)]
        union = set().union(*at)
        body = raw[region.span.start:region.span.end]
        if region.kind is CONTESTED:
            assert all(len(c) >= 2 for c in at)
            assert region.claimants == len(union)
            assert list(region.owners) == sorted(union, key=UnitRef.sort_key)[:cap]
            continue
        assert region.claimants == len(region.owners) == len(union) <= 1
        assert all(c == union for c in at)
        if region.owners:
            owner = region.owners[0]
            assert region.kind is owner.kind
            assert owner.label_key() == min(r.label_key() for r, _ in valid if r == owner)
        else:
            gap = UnitKind.WHITESPACE if all(b in WHITESPACE for b in body) else UnitKind.UNINDEXED
            assert region.kind is gap
    for left, right in zip(regions, regions[1:]):  # maximal
        assert not (left.kind is CONTESTED and right.kind is CONTESTED)
        assert (left.kind, left.owners) != (right.kind, right.owners)

    reasons = Counter(flag.reason for flag in flags)
    assert reasons[FlagReason.CLAIM_INVALID] == sum(not _valid(r) for r, _ in claims)
    assert reasons[FlagReason.CLAIM_OUT_OF_RANGE] == sum(
        s.end > size for r, s in claims if _valid(r))
    assert [f for f in flags if f.reason is FlagReason.CONTESTED_SPAN] == [
        Flag(FlagReason.CONTESTED_SPAN, r.span, (("claimants", r.claimants),))
        for r in regions if r.kind is CONTESTED]
    unindexed = [r for r in regions if r.kind is UnitKind.UNINDEXED and not r.owners]
    assert [f for f in flags if f.reason is FlagReason.UNINDEXED_NON_WHITESPACE] == [
        Flag(FlagReason.UNINDEXED_NON_WHITESPACE, r.span, (
            ("first_non_whitespace", next(
                i for i in range(r.span.start, r.span.end) if raw[i] not in WHITESPACE)),
            ("non_whitespace_bytes", sum(
                raw[i] not in WHITESPACE for i in range(r.span.start, r.span.end)))))
        for r in unindexed]
    self_overlap = any(
        a == b and sa != sb and sa.start < sb.end and sb.start < sa.end
        for i, (a, sa) in enumerate(valid) for b, sb in valid[i + 1:])
    assert (reasons[FlagReason.SELF_OVERLAP] > 0) == self_overlap
    assert len(flags) <= 2 * len(claims) + len(regions)  # O(n), never O(n^2)


@given(_case(), st.data())
def test_claim_order_does_not_change_the_result(
        case: tuple[bytes, Claims, int], data: st.DataObject) -> None:
    raw, claims, cap = case
    limits = Limits(max_contested_owners=cap)
    # repr, not ==, so the informational obj/gen labels must agree too.
    assert repr(tile(raw, data.draw(st.permutations(claims)), limits)) == repr(
        tile(raw, claims, limits))


@given(_case(), st.data())
def test_check_tiling_accepts_tile_output_and_rejects_any_single_perturbation(
        case: tuple[bytes, Claims, int], data: st.DataObject) -> None:
    raw, claims, _ = case
    size = len(raw)
    spans = [r.span for r in tile(raw, claims)[0].regions]
    assert check_tiling(size, spans)
    assert check_tiling(size, iter(spans))  # any iterable
    assert not check_tiling(size + 1, spans)
    if not spans:
        assert not check_tiling(size, [Span(0, 1)])
        return
    i = data.draw(st.integers(0, len(spans) - 1))
    span = spans[i]
    delta = data.draw(st.sampled_from([-1, 1]))
    perturbed = []
    if span.start + delta >= 0 and span.start + delta <= span.end:
        perturbed.append(spans[:i] + [Span(span.start + delta, span.end)] + spans[i + 1:])
    if span.end + delta >= span.start:
        perturbed.append(spans[:i] + [Span(span.start, span.end + delta)] + spans[i + 1:])
    perturbed.append(spans[:i] + spans[i + 1:])             # a span dropped
    perturbed.append(spans[:i] + [span, span] + spans[i + 1:])  # a span repeated
    if len(spans) > 1:
        j = (i + 1) % len(spans)
        swapped = list(spans)
        swapped[i], swapped[j] = swapped[j], swapped[i]
        perturbed.append(swapped)
    for bad in perturbed:
        assert not check_tiling(size, bad)
    assert not check_tiling(size - 1, spans)


@pytest.mark.parametrize("size, spans", [
    (-1, []), (True, [Span(0, 1)]), (1.0, [Span(0, 1)]), (1, [(0, 1)]), (1, [None]),
    (2, [Span(0, 1), Span(1, 1), Span(1, 2)]),  # an empty span
])
def test_check_tiling_answers_false_to_malformed_input(size: object, spans: list[object]) -> None:
    assert check_tiling(size, spans) is False  # type: ignore[arg-type]


def test_empty_file_tiles_to_no_regions() -> None:
    tiling, flags = tile(b"", [(REF_A, Span(0, 0))])
    assert (tiling.size, tiling.regions, flags) == (0, (), ())
    assert check_tiling(0, [])


def test_owned_contested_and_gap_regions() -> None:
    raw = b"%PDF\n  junk" + b"x" * 21
    tiling, flags = tile(raw, [(REF_A, Span(0, 5)), (REF_B, Span(3, 5))])
    assert tiling.regions == (
        Region(Span(0, 3), UnitKind.OBJECT, (REF_A,), 1),
        Region(Span(3, 5), CONTESTED, (REF_A, REF_B), 2),
        Region(Span(5, 32), UnitKind.UNINDEXED, (), 0),
    )
    assert flags == (
        Flag(FlagReason.CONTESTED_SPAN, Span(3, 5), (("claimants", 2),)),
        Flag(FlagReason.UNINDEXED_NON_WHITESPACE, Span(5, 32), (
            ("first_non_whitespace", 7), ("non_whitespace_bytes", 25))),
    )


def test_separate_contested_runs_count_their_own_claimants() -> None:
    c = UnitRef(UnitKind.DEAD_BODY, 5)
    tiling, _ = tile(b"x" * 10, [(REF_A, Span(0, 10)), (REF_B, Span(0, 2)), (c, Span(5, 7))])
    assert [(r.span, r.claimants, r.owners) for r in tiling.regions if r.kind is CONTESTED] == [
        (Span(0, 2), 2, (REF_A, REF_B)), (Span(5, 7), 2, (c, REF_A))]  # sort_key: start 5 < 10


def test_whitespace_gap_is_not_flagged_and_a_unit_with_two_spans_owns_both() -> None:
    raw = b"ab \r\n\0\t\x0ccd"
    tiling, flags = tile(raw, [(REF_A, Span(0, 2)), (REF_A, Span(8, 10)), (REF_A, Span(0, 2))])
    assert [(r.span, r.kind) for r in tiling.regions] == [
        (Span(0, 2), UnitKind.OBJECT), (Span(2, 8), UnitKind.WHITESPACE),
        (Span(8, 10), UnitKind.OBJECT)]
    assert flags == ()  # the exact duplicate is dropped quietly


def test_partial_self_overlap_is_flagged_and_owned_once() -> None:
    tiling, flags = tile(b"abcdef", [(REF_A, Span(0, 4)), (REF_A, Span(2, 6))])
    assert tiling.regions == (Region(Span(0, 6), UnitKind.OBJECT, (REF_A,), 1),)
    assert flags == (Flag(FlagReason.SELF_OVERLAP, Span(2, 4)),)


def test_owner_label_is_the_smallest_obj_gen_whatever_the_order() -> None:
    labels = [UnitRef(UnitKind.OBJECT, 0, obj=o, gen=g) for o, g in [(5, 0), (2, 1), (2, 0)]]
    for claims in ([(r, Span(0, 1)) for r in labels], [(r, Span(0, 1)) for r in labels[::-1]]):
        owner = tile(b"x", claims)[0].regions[0].owners[0]
        assert (owner.obj, owner.gen) == (2, 0)


def test_claim_past_the_end_is_clipped_and_flagged() -> None:
    tiling, flags = tile(b"abc", [(REF_A, Span(1, 9)), (REF_B, Span(7, 8))])
    assert tiling.regions[-1] == Region(Span(1, 3), UnitKind.OBJECT, (REF_A,), 1)
    assert tuple(f for f in flags if f.reason is FlagReason.CLAIM_OUT_OF_RANGE) == (
        Flag(FlagReason.CLAIM_OUT_OF_RANGE, Span(1, 3),
             (("claim_start", 1), ("claim_end", 9), ("size", 3))),
        Flag(FlagReason.CLAIM_OUT_OF_RANGE, Span(3, 3),
             (("claim_start", 7), ("claim_end", 8), ("size", 3))),
    )


@pytest.mark.parametrize("ref", [
    UnitRef(UnitKind.UNINDEXED, 0), UnitRef(UnitKind.WHITESPACE, 0),
    UnitRef(UnitKind.OBJSTM_MEMBER, 0, within=UnitRef(UnitKind.OBJECT, 0)),
    # Nested-only kinds claimed at top level, without a parent.
    UnitRef(UnitKind.OBJSTM_MEMBER, 0), UnitRef(UnitKind.STREAM_SLACK, 0),
])
def test_unclaimable_and_nested_claims_are_refused_and_flagged(ref: UnitRef) -> None:
    tiling, flags = tile(b"SECRET", [(ref, Span(0, 6))])
    assert tiling.regions == (Region(Span(0, 6), UnitKind.UNINDEXED, (), 0),)
    assert sorted(f.reason.name for f in flags) == [
        "CLAIM_INVALID", "UNINDEXED_NON_WHITESPACE"]


def test_a_deep_within_chain_is_refused_without_hashing_it() -> None:
    ref = UnitRef(UnitKind.OBJECT, 0)
    for _ in range(100_000):  # == or hash on this would overflow the stack
        ref = UnitRef(UnitKind.OBJSTM_MEMBER, 0, within=ref)
    _, flags = tile(b"x", [(ref, Span(0, 1)), (ref, Span(0, 1))])
    assert [f.reason for f in flags].count(FlagReason.CLAIM_INVALID) == 2


def test_nested_claims_stay_linear() -> None:
    # 10^4 claims all overlapping (like n "N 0 obj" headers sharing one
    # endobj): one contested region and one flag, not n^2 of either.
    n = 10_000
    claims = [(UnitRef(UnitKind.OBJECT, i), Span(i, 2 * n - i)) for i in range(n)]
    tiling, flags = tile(b" " * (2 * n), claims)
    assert [(r.span, r.kind, r.claimants, len(r.owners)) for r in tiling.regions] == [
        (Span(0, 1), UnitKind.OBJECT, 1, 1), (Span(1, 2 * n - 1), CONTESTED, n - 1 + 1, 16),
        (Span(2 * n - 1, 2 * n), UnitKind.OBJECT, 1, 1)]
    assert flags == (Flag(FlagReason.CONTESTED_SPAN, Span(1, 2 * n - 1), (("claimants", n),)),)
    shared_end = [(UnitRef(UnitKind.OBJECT, 8 * i), Span(8 * i, 8 * n)) for i in range(n)]
    tiling, flags = tile(b" " * (8 * n), shared_end)
    assert len(tiling.regions) == 2 and len(flags) == 1


@pytest.mark.parametrize("shape", ["single_owner_after", "pairs_after"])
def test_a_crowd_of_claimants_does_not_slow_the_rest_of_the_sweep(shape: str) -> None:
    # n claimants active at once, then 10^5 more pieces: a set that kept
    # its peak-sized table made each later piece O(n), the sweep
    # quadratic (~70 s here); linear takes a few seconds.
    n = 200_000
    claims = [(UnitRef(UnitKind.OBJECT, i), Span(0, 1)) for i in range(n)]
    if shape == "single_owner_after":
        claims += [(UnitRef(UnitKind.OBJECT, n + j), Span(1 + j, 2 + j)) for j in range(n)]
    else:
        for j in range(n // 2):
            a = 2 + 3 * j
            claims += [(UnitRef(UnitKind.OBJECT, n + 2 * j), Span(a, a + 2)),
                       (UnitRef(UnitKind.OBJECT, n + 2 * j + 1), Span(a + 1, a + 3))]
    started = time.process_time()
    tiling, _flags = tile(b"x" * (3 * n + 5), claims)
    assert time.process_time() - started < 30
    assert check_tiling(3 * n + 5, [r.span for r in tiling.regions])


def test_many_claims_stay_iterative() -> None:
    n = 20_000
    claims = [(UnitRef(UnitKind.OBJECT, i), Span(i, i + 1)) for i in range(n)]
    claims += [(UnitRef(UnitKind.DEAD_BODY, i), Span(i, i + 1000)) for i in (0, 10_000)]
    tiling, flags = tile(b" " * n, claims)
    assert check_tiling(n, [r.span for r in tiling.regions])
    assert len(tiling.regions) == n - 2 * 1000 + 2 and len(flags) == 2
