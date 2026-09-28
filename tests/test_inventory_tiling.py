"""Byte tiling (docs/REDESIGN.md §4, Phase 3a): ``tile`` partitions every
file exactly and ``check_tiling`` is its independent check. Property
tests compare ``tile`` against a per-byte oracle."""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from redaction_verifier.inventory import CONTESTED, Region, check_tiling, tile
from redaction_verifier.inventory.tiling import WHITESPACE
from redaction_verifier.model import Flag, FlagReason, Span, UnitKind, UnitRef

REF_A = UnitRef(UnitKind.OBJECT, 10, obj=1, gen=0)
REF_B = UnitRef(UnitKind.OBJECT, 20, obj=2, gen=0)

_refs = st.builds(
    UnitRef,
    kind=st.sampled_from(UnitKind),
    start=st.integers(0, 4),  # a small pool, so claimants coincide
    within=st.none() | st.builds(UnitRef, kind=st.just(UnitKind.OBJECT), start=st.integers(0, 2)),
)
_raw = st.binary(max_size=60) | st.lists(
    st.sampled_from(list(WHITESPACE + b"A%")), max_size=60).map(bytes)


@st.composite
def _file_and_claims(draw: st.DrawFn) -> tuple[bytes, list[tuple[UnitRef, Span]]]:
    raw = draw(_raw)
    limit = len(raw) + 5  # some claims run past the end
    claims = []
    for _ in range(draw(st.integers(0, 8))):
        start = draw(st.integers(0, limit))
        claims.append((draw(_refs), Span(start, draw(st.integers(start, limit)))))
    return raw, claims


def _claimants(size: int, claims: list[tuple[UnitRef, Span]], offset: int) -> set[UnitRef]:
    return {ref for ref, span in claims if span.start <= offset < min(span.end, size)}


@given(_file_and_claims())
def test_tile_partitions_the_file_like_a_per_byte_oracle(
        case: tuple[bytes, list[tuple[UnitRef, Span]]]) -> None:
    raw, claims = case
    tiling, flags = tile(raw, claims)
    regions = tiling.regions
    assert tiling.size == len(raw)
    assert check_tiling(len(raw), [r.span for r in regions])

    # Sorted, contiguous, non-empty, [0, size) -- checked directly too.
    pos = 0
    for region in regions:
        assert region.span.start == pos < region.span.end
        pos = region.span.end
    assert pos == len(raw)

    for region in regions:
        for offset in range(region.span.start, region.span.end):
            assert set(region.owners) == _claimants(len(raw), claims, offset)
        assert len(set(region.owners)) == len(region.owners)
        body = raw[region.span.start:region.span.end]
        if not region.owners:
            gap = UnitKind.WHITESPACE if all(b in WHITESPACE for b in body) else UnitKind.UNINDEXED
            assert region.kind is gap
        elif len(region.owners) == 1:
            assert region.kind is region.owners[0].kind
        else:
            assert region.kind is CONTESTED
            assert list(region.owners) == sorted(region.owners, key=UnitRef.sort_key)
    for left, right in zip(regions, regions[1:]):  # maximal
        assert (left.kind, left.owners) != (right.kind, right.owners)

    reasons = Counter(flag.reason for flag in flags)
    assert reasons[FlagReason.CLAIM_OUT_OF_RANGE] == sum(s.end > len(raw) for _, s in claims)
    assert reasons[FlagReason.CONTESTED_SPAN] == sum(
        len(r.owners) for r in regions if r.kind is CONTESTED)
    unindexed = [r for r in regions if r.kind is UnitKind.UNINDEXED and not r.owners]
    assert [f for f in flags if f.reason is FlagReason.UNINDEXED_NON_WHITESPACE] == [
        Flag(FlagReason.UNINDEXED_NON_WHITESPACE, r.span, (
            ("first_non_whitespace", next(
                i for i in range(r.span.start, r.span.end) if raw[i] not in WHITESPACE)),
            ("non_whitespace_bytes", sum(
                raw[i] not in WHITESPACE for i in range(r.span.start, r.span.end)))))
        for r in unindexed]


@given(_file_and_claims(), st.data())
def test_claim_order_does_not_change_the_result(
        case: tuple[bytes, list[tuple[UnitRef, Span]]], data: st.DataObject) -> None:
    raw, claims = case
    assert tile(raw, data.draw(st.permutations(claims))) == tile(raw, claims)


@given(_file_and_claims(), st.data())
def test_check_tiling_accepts_tile_output_and_rejects_any_single_perturbation(
        case: tuple[bytes, list[tuple[UnitRef, Span]]], data: st.DataObject) -> None:
    raw, claims = case
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
    assert tiling.regions[:3] == (
        Region(Span(0, 3), UnitKind.OBJECT, (REF_A,)),
        Region(Span(3, 5), CONTESTED, (REF_A, REF_B)),
        Region(Span(5, 32), UnitKind.UNINDEXED, ()),
    )
    assert flags == (
        Flag(FlagReason.CONTESTED_SPAN, Span(3, 5), (("claimant", 0), ("claimants", 2))),
        Flag(FlagReason.CONTESTED_SPAN, Span(3, 5), (("claimant", 1), ("claimants", 2))),
        Flag(FlagReason.UNINDEXED_NON_WHITESPACE, Span(5, 32), (
            ("first_non_whitespace", 7), ("non_whitespace_bytes", 25))),
    )


def test_whitespace_gap_is_not_flagged_and_a_unit_with_two_spans_owns_both() -> None:
    raw = b"ab \r\n\0\t\x0ccd"
    tiling, flags = tile(raw, [(REF_A, Span(0, 2)), (REF_A, Span(8, 10)), (REF_A, Span(1, 2))])
    assert [(r.span, r.kind) for r in tiling.regions] == [
        (Span(0, 2), UnitKind.OBJECT), (Span(2, 8), UnitKind.WHITESPACE),
        (Span(8, 10), UnitKind.OBJECT)]
    assert flags == ()


def test_claim_past_the_end_is_clipped_and_flagged() -> None:
    tiling, flags = tile(b"abc", [(REF_A, Span(1, 9)), (REF_B, Span(7, 8))])
    assert tiling.regions[-1] == Region(Span(1, 3), UnitKind.OBJECT, (REF_A,))
    assert flags[:2] == (
        Flag(FlagReason.CLAIM_OUT_OF_RANGE, None,
             (("claim_start", 1), ("claim_end", 9), ("size", 3))),
        Flag(FlagReason.CLAIM_OUT_OF_RANGE, None,
             (("claim_start", 7), ("claim_end", 8), ("size", 3))),
    )


def test_many_claims_stay_iterative() -> None:
    # 100,000 abutting claims plus a few long overlapping ones: a sweep,
    # not recursion or a pairwise comparison.
    n = 100_000
    claims = [(UnitRef(UnitKind.OBJECT, i), Span(i, i + 1)) for i in range(n)]
    claims += [(UnitRef(UnitKind.DEAD_BODY, i), Span(i, i + 1000)) for i in (0, 50_000)]
    tiling, flags = tile(b" " * n, claims)
    assert check_tiling(n, [r.span for r in tiling.regions])
    assert len(tiling.regions) == n and len(flags) == 4000
