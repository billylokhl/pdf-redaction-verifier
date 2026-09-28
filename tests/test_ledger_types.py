"""The ledger types (docs/REDESIGN.md §4) and the run budget
(docs/adr/0006): closed enums pinned, records validated, and a Budget
that exhausts exactly at its limits and never raises."""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from redaction_verifier.budget import GIB, Budget, Counter, Limits
from redaction_verifier.inventory import build_inventory, tile
from redaction_verifier.inventory.lexer import Lexer
from redaction_verifier.inventory.flate import Predictor, flate_decode
from redaction_verifier.inventory.objects import ObjectParser

from .test_inventory_build import CATALOG, _objstm_file, ambiguous_length, replaced_home
from .test_inventory_xref import chain_of, classic, incremental, xref_stream
from redaction_verifier.model import (
    Flag,
    FlagReason,
    NAReason,
    Reason,
    Span,
    Status,
    Unit,
    UnitKind,
    UnitRef,
)


def test_na_reasons_are_exactly_adr_0003s_four() -> None:
    # Extended only by ADR (docs/adr/0003): changing this set needs one.
    assert [r.name for r in NAReason] == [
        "XREF_STREAM_FIELD_DATA", "OBJSTM_HEADER_TABLE",
        "FONT_PROGRAM_SPANNED", "IMAGE_DATA_CONSUMED"]


def test_status_is_closed_and_reason_is_the_union() -> None:
    assert [s.name for s in Status] == [
        "UNEXAMINED", "DECODED", "NOT_APPLICABLE", "FLAGGED", "UNREADABLE", "FAILED"]
    assert isinstance(NAReason.OBJSTM_HEADER_TABLE, Reason)
    assert isinstance(FlagReason.CONTESTED_SPAN, Reason)


_A = UnitRef(UnitKind.OBJECT, 0)

# One scenario per FlagReason, each producing flags through real code. A
# PR that adds a member adds its scenario here, so no reason is dead.
EMITTERS: dict[FlagReason, Callable[[], tuple[Flag, ...]]] = {
    FlagReason.UNINDEXED_NON_WHITESPACE: lambda: tile(b"x", [])[1],
    FlagReason.CONTESTED_SPAN: lambda: tile(
        b"xy", [(_A, Span(0, 2)), (UnitRef(UnitKind.OBJECT, 1), Span(1, 2))])[1],
    FlagReason.CLAIM_OUT_OF_RANGE: lambda: tile(b"x", [(_A, Span(0, 2))])[1],
    FlagReason.CLAIM_INVALID: lambda: tile(b"x", [(UnitRef(UnitKind.UNINDEXED, 0), Span(0, 1))])[1],
    FlagReason.SELF_OVERLAP: lambda: tile(b"xyz", [(_A, Span(0, 2)), (_A, Span(1, 3))])[1],
    FlagReason.BUDGET_EXHAUSTED: lambda: _exhausted_units(),
    FlagReason.UNTERMINATED: lambda: _lexed(b"(abc"),
    FlagReason.INVALID_HEX_DIGIT: lambda: _lexed(b"<4g>"),
    FlagReason.INVALID_NAME_ESCAPE: lambda: _lexed(b"/A#4"),
    FlagReason.STRAY_DELIMITER: lambda: _lexed(b")"),
    FlagReason.UNEXPECTED_TOKEN: lambda: _parsed(b"1 0 obj [foo] endobj"),
    FlagReason.MISSING_VALUE: lambda: _parsed(b"1 0 obj endobj"),
    FlagReason.DUPLICATE_KEY: lambda: _parsed(b"1 0 obj << /A 1 /A 2 >> endobj"),
    FlagReason.NUMBER_OUT_OF_RANGE: lambda: _parsed(b"1 0 obj " + b"9" * 99 + b" endobj"),
    FlagReason.NESTING_LIMIT: lambda: _parsed(b"1 0 obj " + b"[" * 999),
    FlagReason.TOKEN_LIMIT: lambda: _parsed(b"1 0 obj " + b"1 " * 9, Limits(max_tokens_per_object=5)),
    FlagReason.EXTRA_TOKENS: lambda: _parsed(b"1 0 obj 1 2 endobj"),
    FlagReason.MISSING_ENDOBJ: lambda: _parsed(b"1 0 obj 1"),
    FlagReason.STREAM_EOL: lambda: _parsed(b"1 0 obj <<>> stream x\nendstream endobj"),
    FlagReason.ENDSTREAM_JOINED: lambda: _parsed(
        b"1 0 obj << /Length 1 >> stream\nx\nendstreamendobj"),
    FlagReason.LENGTH_MISMATCH: lambda: _parsed(b"1 0 obj <<>> stream\nx\nendstream endobj"),
    FlagReason.STREAM_SLACK: lambda: _parsed(
        b"1 0 obj << /Length 1 >> stream\nx y\nendstream endobj"),
    FlagReason.FLATE_ERROR: lambda: flate_decode(b"not zlib").flags,
    FlagReason.FLATE_TRUNCATED: lambda: flate_decode(zlib.compress(b"x" * 99)[:5]).flags,
    FlagReason.AFTER_STREAM_END: lambda: flate_decode(zlib.compress(b"x") + b"!").flags,
    FlagReason.BAD_DECODE_PARMS: lambda: flate_decode(zlib.compress(b"x"), Predictor(3)).flags,
    FlagReason.PREDICTOR_ERROR: lambda: flate_decode(zlib.compress(b"\x09a"), Predictor(12)).flags,
    FlagReason.XREF_TAIL: lambda: chain_of(classic() + b"x").flags,
    FlagReason.XREF_NOT_FOUND: lambda: chain_of(
        classic().replace(b"startxref\n", b"startxref\n9", 1)).flags,
    FlagReason.XREF_TABLE_MALFORMED: lambda: chain_of(
        classic().replace(b"trailer", b"trailor", 1)).flags,
    FlagReason.XREF_STREAM_MALFORMED: lambda: chain_of(
        xref_stream().replace(b"/W [1 4 2]", b"/W [1 4 9]", 1)).flags,
    FlagReason.UNSUPPORTED_FILTER: lambda: chain_of(
        xref_stream().replace(b"/Filter /FlateDecode", b"/Filter /LZWDecode   ", 1)).flags,
    FlagReason.XREF_CONFLICT: lambda: chain_of(_duplicated_subsection()).flags,
    FlagReason.XREF_OFFSET_MISMATCH: lambda: _offset_mismatch(),
    FlagReason.PREV_CYCLE: lambda: chain_of(_cycle()).flags,
    FlagReason.HEADER_OFFSET: lambda: chain_of(b"x" + classic()).flags,
    FlagReason.XREF_SIZE_MISMATCH: lambda: chain_of(
        classic().replace(b"/Size 4", b"/Size 5", 1)).flags,
    FlagReason.MISSING_ROOT: lambda: chain_of(
        classic().replace(b"/Root 1 0 R", b"/Rook 1 0 R", 1)).flags,
    FlagReason.FLAGS_TRUNCATED: lambda: _parsed(
        b"1 0 obj [foo foo] endobj", Limits(max_flags_per_object=1)),
    FlagReason.XREF_EPILOGUE_MISMATCH: lambda: _inventoried(re.sub(  # names 208, not 209
        rb"startxref\n(\d+)", lambda m: b"startxref\n%d" % (int(m.group(1)) - 1),
        incremental(), count=1)),
    FlagReason.REVISION_AMBIGUOUS: lambda: _inventoried(ambiguous_length()),
    FlagReason.OBJSTM_MALFORMED: lambda: _inventoried(_objstm_file(
        [(1, CATALOG)], {1: (2, 4, 0)}, header=b"1 0 6")),
    FlagReason.OBJSTM_MEMBER_INVALID: lambda: _inventoried(_objstm_file(
        [(1, CATALOG), (6, b"")], {1: (2, 4, 0), 6: (2, 4, 1)})),
    FlagReason.OBJSTM_ENTRY_MISMATCH: lambda: _inventoried(replaced_home(same=False)),
}


def _inventoried(data: bytes) -> tuple[Flag, ...]:
    return build_inventory(data, budget=Budget(file_size=len(data))).flags


def _duplicated_subsection() -> bytes:
    data = classic()
    sub = re.search(rb"1 1\n\d{10} 00000 n \n", data)
    assert sub is not None
    # Object 1's subsection twice (the table's own offset does not move).
    return data.replace(sub.group(0), sub.group(0) * 2, 1)


def _offset_mismatch() -> tuple[Flag, ...]:
    data = classic()
    one = re.search(rb"1 1\n(\d{10})", data)
    assert one is not None
    return chain_of(data.replace(one.group(0), b"1 1\n%010d" % (int(one.group(1)) + 1),
                                   1)).flags


def _cycle() -> bytes:
    data = incremental()
    first, last = (int(n) for n in re.findall(rb"startxref\n(\d+)", data))
    return data.replace(b"/Prev %d" % first, b"/Prev %d" % last, 1)


def _parsed(data: bytes, limits: Limits | None = None) -> tuple[Flag, ...]:
    parsed = ObjectParser(data, limits).parse_indirect_at(0)
    assert parsed is not None
    return parsed.flags


def _lexed(data: bytes) -> tuple[Flag, ...]:
    return tuple(flag for token in Lexer(data) for flag in token.flags)


def _exhausted_units() -> tuple[Flag, ...]:
    budget = Budget(Limits(max_units=0))
    budget.charge_units(1)
    return budget.flags()


def test_every_flag_reason_is_emitted_by_some_scenario() -> None:
    assert set(EMITTERS) == set(FlagReason)
    for reason, emit in EMITTERS.items():
        assert reason in {flag.reason for flag in emit()}, reason


def test_unit_ref_identity_ignores_the_informational_obj_and_gen() -> None:
    inner = UnitRef(UnitKind.OBJSTM_MEMBER, 7, within=UnitRef(UnitKind.OBJECT, 3), obj=9, gen=0)
    same = UnitRef(UnitKind.OBJSTM_MEMBER, 7, within=UnitRef(UnitKind.OBJECT, 3, obj=5))
    assert inner == same and hash(inner) == hash(same)
    assert inner.sort_key() == ((3, list(UnitKind).index(UnitKind.OBJECT)),
                                (7, list(UnitKind).index(UnitKind.OBJSTM_MEMBER)))
    assert UnitRef(UnitKind.OBJECT, 3) != UnitRef(UnitKind.DEAD_BODY, 3)
    unit = Unit(inner, (Span(0, 4),))
    assert unit.flags == () and len(unit.spans[0]) == 4


@given(st.lists(st.builds(UnitRef, kind=st.sampled_from(UnitKind), start=st.integers(0, 3),
                          within=st.none() | st.builds(UnitRef, kind=st.sampled_from(UnitKind),
                                                       start=st.integers(0, 3))),
                min_size=2, max_size=2))
def test_sort_key_is_a_total_order_consistent_with_equality(pair: list[UnitRef]) -> None:
    a, b = pair
    assert (a == b) == (a.sort_key() == b.sort_key())


@pytest.mark.parametrize("kind, start, within, error", [
    ("object", 0, None, TypeError), (UnitKind.OBJECT, -1, None, ValueError),
    (UnitKind.OBJECT, True, None, ValueError), (UnitKind.OBJECT, 1.0, None, ValueError),
    (UnitKind.OBJECT, 0, (UnitKind.OBJECT, 0), TypeError),
])
def test_unit_ref_rejects_malformed_fields(
        kind: Any, start: Any, within: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        UnitRef(kind, start, within)


@pytest.mark.parametrize("start, end", [(-1, 0), (2, 1), (True, 1), (0, 1.0), ("0", 1)])
def test_span_rejects_malformed_bounds(start: Any, end: Any) -> None:
    with pytest.raises(ValueError):
        Span(start, end)


class _Str(str):
    pass


class _Int(int):
    pass


@pytest.mark.parametrize("params", [
    (("n", True),), (("n", b"secret"),), (("n", "1"),), ((1, 1),), (("n", 1, 2),), ("n",),
    [("n", 1)], (("a b", 1),), (("é", 1),), (("", 1),), (("n\n", 1),),
    # Subclasses of str or int (a custom repr could carry document bytes).
    ((_Str("n"), 1),), (("n", _Int(1)),),
])
def test_flag_params_carry_only_named_integers(params: Any) -> None:
    with pytest.raises(TypeError):
        Flag(FlagReason.CONTESTED_SPAN, None, params)
    with pytest.raises(TypeError):
        Flag("contested_span", None, ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Flag(FlagReason.CONTESTED_SPAN, (0, 1), ())  # type: ignore[arg-type]
    assert Flag(FlagReason.CONTESTED_SPAN, Span(0, 1), (("n", 0),)).params == (("n", 0),)


def test_limits_default_to_adr_0006s_placeholders() -> None:
    limits = Limits()
    assert (limits.max_depth, limits.max_units, limits.max_inflated_bytes,
            limits.max_ocr_pixels_per_page) == (25, 200_000, 2 * GIB, 200_000_000)
    assert (limits.max_ocr_run_pixels, limits.max_contested_owners) == (20_000_000_000, 16)
    assert GIB == 1 << 30
    assert limits.ocr_run_pixels(3) == 600_000_000
    assert limits.ocr_run_pixels(10_000) == 20_000_000_000  # the flat ceiling
    assert limits.ocr_run_pixels(0) == limits.ocr_run_pixels(-5) == 0
    with pytest.raises(AttributeError):
        limits.max_units = 1  # type: ignore[misc]


def _counter(flag: Flag) -> int:
    return dict(flag.params)["counter"]


def test_units_and_bytes_exhaust_exactly_at_the_limit_and_stay_exhausted() -> None:
    budget = Budget(Limits(max_units=3, max_inflated_bytes=10))
    assert budget.charge_units() and budget.charge_units(2) and budget.units == 3
    assert not budget.charge_units(1) and not budget.charge_units(0)  # sticky
    assert budget.charge_inflated_bytes(10) and not budget.charge_inflated_bytes(1)
    assert budget.units == 3 and budget.inflated_bytes == 10
    assert budget.flags() == (
        Flag(FlagReason.BUDGET_EXHAUSTED, None,
             (("counter", int(Counter.UNITS)), ("limit", 3), ("used", 3), ("requested", 1))),
        Flag(FlagReason.BUDGET_EXHAUSTED, None,
             (("counter", int(Counter.INFLATED_BYTES)), ("limit", 10), ("used", 10), ("requested", 1))),
    )


def test_depth_allows_the_limit_and_refuses_beyond() -> None:
    budget = Budget()
    assert budget.charge_depth(0) and budget.charge_depth(25) and budget.depth == 25
    assert not budget.charge_depth(26) and not budget.charge_depth(1)
    assert [_counter(f) for f in budget.flags()] == [Counter.DEPTH]


def test_ocr_pixels_per_page_and_derived_run_cap() -> None:
    budget = Budget(Limits(max_ocr_pixels_per_page=10), page_count=2)
    assert budget.charge_ocr_pixels(0, 10) and not budget.charge_ocr_pixels(0, 1)
    assert not budget.charge_ocr_pixels(0, 0)  # page 0 stays refused
    assert not budget.charge_ocr_pixels(2, 1)  # beyond the anchored page count
    assert budget.exhausted(Counter.OCR_PAGE_PIXELS)
    assert budget.charge_ocr_pixels(1, 10)     # page 1 has its own allowance
    assert not budget.charge_ocr_pixels(1, 1)  # the page cap is checked first
    assert budget.ocr_pixels == 20
    assert [_counter(f) for f in budget.flags()] == [Counter.OCR_PAGE_PIXELS]
    run = Budget(Limits(max_ocr_pixels_per_page=10, max_ocr_run_pixels=15), page_count=2)
    assert run.charge_ocr_pixels(0, 10) and not run.charge_ocr_pixels(1, 6)
    assert [_counter(f) for f in run.flags()] == [Counter.OCR_RUN_PIXELS]
    malformed = Budget(page_count=1)
    assert not malformed.charge_ocr_pixels(0, -1)
    assert [_counter(f) for f in malformed.flags()] == [Counter.OCR_PAGE_PIXELS]
    assert not Budget().charge_ocr_pixels(0, 1)  # no anchored pages, no OCR budget


_anything = st.one_of(st.integers(-5, 3 * GIB), st.booleans(), st.none(), st.floats(),
                      st.text(max_size=2), st.binary(max_size=2))


@given(st.builds(Limits, max_depth=st.integers(0, 5), max_units=st.integers(0, 5),
                 max_inflated_bytes=st.integers(0, 50), max_ocr_pixels_per_page=st.integers(0, 50),
                 max_ocr_run_pixels=st.integers(0, 80)),
       _anything,
       st.lists(st.tuples(st.sampled_from(["depth", "units", "bytes", "ocr"]),
                          _anything, _anything), max_size=30))
def test_budget_never_raises_and_never_exceeds_a_limit(
        limits: Limits, page_count: Any, calls: list[tuple[str, Any, Any]]) -> None:
    budget = Budget(limits, page_count)
    for op, x, y in calls:
        ok = {"depth": lambda: budget.charge_depth(x),
              "units": lambda: budget.charge_units(x),
              "bytes": lambda: budget.charge_inflated_bytes(x),
              "ocr": lambda: budget.charge_ocr_pixels(x, y)}[op]()
        assert ok is True or (ok is False and budget.flags())
    assert budget.depth <= limits.max_depth and budget.units <= limits.max_units
    assert budget.inflated_bytes <= limits.max_inflated_bytes
    assert budget.ocr_pixels <= limits.ocr_run_pixels(budget.page_count)
    assert all(n <= limits.max_ocr_pixels_per_page for n in budget.ocr_pixels_by_page.values())
    assert len(budget.flags()) == len({_counter(f) for f in budget.flags()})


def test_int_subclasses_never_reach_a_flag() -> None:
    # Spans take plain ints only; a Budget turns a caller's int subclass
    # into a plain int in its flag instead of raising.
    with pytest.raises(ValueError):
        Span(_Int(0), 1)
    budget = Budget(Limits(max_units=_Int(1)))
    assert not budget.charge_units(_Int(2))
    (flag,) = budget.flags()
    assert all(type(value) is int for _name, value in flag.params)


@pytest.mark.parametrize("label", [{"obj": "1"}, {"obj": -1}, {"gen": 1.0}, {"obj": True},
                                   {"obj": _Int(1)}, {"gen": float("nan")}])
def test_unit_ref_labels_are_plain_ints_or_none(label: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        UnitRef(UnitKind.OBJECT, 0, **label)
    assert UnitRef(UnitKind.OBJECT, 0, obj=0, gen=None).obj == 0
