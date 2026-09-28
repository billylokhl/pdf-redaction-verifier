"""The inventory's object parser (docs/REDESIGN.md §4, Phase 3a-3):
values round-trip with their spans, ambiguity is flagged (never resolved
silently), limits hold, and nothing raises or goes superlinear."""

from __future__ import annotations

import time
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from redaction_verifier.budget import Limits
from redaction_verifier.inventory.objects import (
    IndirectObject,
    ObjectParser,
    PdfArray,
    PdfBool,
    PdfDict,
    PdfInt,
    PdfKeyword,
    PdfName,
    PdfNull,
    PdfReal,
    PdfRef,
    PdfString,
    PdfValue,
)
from redaction_verifier.model import FlagReason, Span


def _obj(data: bytes, **kwargs: Any) -> IndirectObject:
    parsed = ObjectParser(data, **kwargs).parse_indirect_at(0)
    assert parsed is not None
    return parsed


def _reasons(parsed: IndirectObject) -> list[str]:
    return [flag.reason.name for flag in parsed.flags]


def _plain(value: PdfValue | None) -> Any:
    """A value as plain Python, spans dropped, for comparison."""
    match value:
        case PdfNull() | None:
            return None
        case PdfBool(value=v) | PdfInt(value=v) | PdfReal(value=v):
            return v
        case PdfName(raw=raw):
            return ("name", raw)
        case PdfString(raw=raw):
            return ("str", raw)
        case PdfRef(num=num, gen=gen):
            return ("ref", num, gen)
        case PdfKeyword(raw=raw):
            return ("kw", raw)
        case PdfArray(items=items):
            return [_plain(item) for item in items]
        case PdfDict(entries=entries):
            return [(k.raw, _plain(v)) for k, v in entries]
    raise AssertionError(value)


# ── Round trips: serialize a random value, parse it back ──────────────────
_names = st.binary(min_size=1, max_size=6)
_scalars = st.one_of(
    st.none().map(lambda _: (b"null", None)),
    st.booleans().map(lambda b: (b"true" if b else b"false", b)),
    st.integers(-10**12, 10**12).map(lambda i: (str(i).encode(), i)),
    st.integers(0, 10**6).flatmap(lambda n: st.integers(0, 99).map(
        lambda g: (f"{n} {g} R".encode(), ("ref", n, g)))),
    st.integers(-10**6, 10**6).map(lambda i: (f"{i / 100:.2f}".encode(), round(i / 100, 2))),
    _names.map(lambda raw: (b"/" + b"".join(b"#%02x" % c for c in raw), ("name", raw))),
    st.binary(max_size=12).map(lambda raw: (b"<" + raw.hex().encode() + b">", ("str", raw))),
    st.binary(max_size=12).map(lambda raw: (
        b"(" + b"".join(b"\\%03o" % c for c in raw) + b")", ("str", raw))),
)


def _array(children: list[tuple[bytes, Any]]) -> tuple[bytes, Any]:
    return b"[" + b" ".join(text for text, _ in children) + b"]", [v for _, v in children]


def _dict(children: list[tuple[bytes, tuple[bytes, Any]]]) -> tuple[bytes, Any]:
    body = b" ".join(b"/K" + key.hex().encode() + b" " + text for key, (text, _) in children)
    return b"<<" + body + b">>", [(b"K" + key.hex().encode(), v) for key, (_, v) in children]


_values = st.recursive(_scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=5).map(_array),
    st.lists(st.tuples(st.binary(min_size=1, max_size=3), inner), max_size=5,
             unique_by=lambda kv: kv[0]).map(_dict),
), max_leaves=30)


@given(_values, st.sampled_from([b" ", b"\n", b"\r\n", b"\t%comment\n"]))
def test_values_round_trip(value: tuple[bytes, Any], gap: bytes) -> None:
    text, expected = value
    data = b"7 0 obj" + gap + text + gap + b"endobj"
    parsed = _obj(data)
    assert parsed.flags == () and parsed.strays == () and parsed.complete
    assert (parsed.num, parsed.gen, parsed.span) == (7, 0, Span(0, len(data)))
    assert _plain(parsed.value) == expected
    assert parsed.value is not None
    assert data[parsed.value.start:parsed.value.end] == text


_noise = st.lists(st.sampled_from([
    b"1", b"0", b"obj", b"endobj", b"R", b"<<", b">>", b"[", b"]", b"(", b")", b"<", b">",
    b"/N", b"stream\n", b"\nendstream", b"/Length 3", b"%", b"\n", b" ", b"-2.5", b"{",
    b"x", b"\\", b"#", b"true", b"null", b"xref", b"trailer",
]), max_size=40).map(b" ".join)


@given(_noise | st.binary(max_size=80))
def test_never_raises_and_stays_in_bounds(body: bytes) -> None:
    data = b"1 0 obj " + body
    parser = ObjectParser(data, Limits(max_flags_per_object=4))
    parsed = parser.parse_indirect_at(0)
    assert parsed is not None
    assert 0 <= parsed.span.start <= parsed.span.end <= len(data)
    assert len(parsed.flags) <= 5
    for flag in parsed.flags:
        assert flag.span is None or flag.span.end <= len(data)
    if parsed.stream is not None:
        assert parsed.span.start <= parsed.stream.data.start <= parsed.stream.data.end
        assert parsed.stream.data.end <= parsed.span.end
    value = parser.parse_value_at(0)
    assert value.span.end <= len(data)


def test_a_non_header_offset_is_refused() -> None:
    for data in (b"x 0 obj 1 endobj", b"1 obj", b"-1 0 obj 1 endobj", b"1 0 R", b""):
        assert ObjectParser(data).parse_indirect_at(0) is None


def test_the_header_may_follow_whitespace_but_the_span_starts_at_it() -> None:
    parsed = _obj(b"\n  12 3 obj null endobj")
    assert (parsed.num, parsed.gen, parsed.span) == (12, 3, Span(3, 23))


# ── Ambiguity ─────────────────────────────────────────────────────────────
def test_duplicate_keys_are_flagged_with_a_textual_comparison() -> None:
    parsed = _obj(b"1 0 obj << /A [1  2] /A [1 %c\n 2] /B (a) /B <61> >> endobj")
    assert [(f.reason.name, f.params) for f in parsed.flags] == [
        ("DUPLICATE_KEY", (("identical", 1),)), ("DUPLICATE_KEY", (("identical", 0),))]
    assert isinstance(parsed.value, PdfDict)
    # Identical repeats resolve; different ones never do.
    assert _plain(parsed.value.get(b"A")) == [1, 2]
    assert parsed.value.get(b"B") is None and parsed.value.ambiguous == {b"B"}
    assert len(parsed.value.entries) == 4


def test_a_stream_whose_length_agrees() -> None:
    data = b"1 0 obj << /Length 5 >> stream\r\nhello\nendstream\nendobj"
    parsed = _obj(data)
    assert parsed.flags == () and parsed.complete and parsed.stream is not None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"hello"
    assert parsed.stream.slack is None and parsed.span == Span(0, len(data))


def test_bytes_past_length_before_endstream_are_slack() -> None:
    data = b"1 0 obj << /Length 3 >> stream\nabc 123-45-6789\nendstream endobj"
    parsed = _obj(data)
    assert parsed.stream is not None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"abc"
    assert parsed.stream.slack is not None
    assert data[parsed.stream.slack.start:parsed.stream.slack.end] == b" 123-45-6789\n"
    assert _reasons(parsed) == ["STREAM_SLACK"]


@pytest.mark.parametrize(("header", "declared"), [
    (b"<< /Length 99 >>", 99),      # runs past endstream
    (b"<< >>", -1),                 # absent
    (b"<< /Length -4 >>", -1),      # invalid
    (b"<< /Length 2 0 R >>", -1),   # indirect, nothing to resolve it
    (b"<< /Length 5 /Length 6 >>", -1),  # ambiguous
])
def test_a_disagreeing_length_falls_back_to_the_scan(header: bytes, declared: int) -> None:
    data = b"1 0 obj " + header + b" stream\nhello\r\nendstream endobj"
    parsed = _obj(data)
    assert parsed.stream is not None and parsed.complete
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"hello"
    mismatch = [f for f in parsed.flags if f.reason is FlagReason.LENGTH_MISMATCH]
    assert [dict(f.params) for f in mismatch] == [{"declared": declared, "found": 5}]


def test_an_indirect_length_is_resolved_by_the_callback() -> None:
    data = b"1 0 obj << /Length 2 0 R >> stream\nab\nendstream endobj"
    seen: list[tuple[int, int]] = []

    def resolve(num: int, gen: int) -> int | None:
        seen.append((num, gen))
        return 2

    parsed = _obj(data, resolve_length=resolve)
    assert parsed.flags == () and seen == [(2, 0)]
    assert parsed.stream is not None and parsed.stream.declared_length == 2


def test_endstream_inside_the_data_does_not_end_it_when_length_says_otherwise() -> None:
    body = b"xx endstream yy"
    data = b"1 0 obj << /Length %d >> stream\n" % len(body) + body + b"\nendstream endobj"
    parsed = _obj(data)
    assert parsed.flags == () and parsed.stream is not None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == body


def test_a_stream_with_no_endstream_is_unterminated() -> None:
    parsed = _obj(b"1 0 obj << /Length 2 >> stream\nab")
    assert parsed.stream is not None and not parsed.stream.terminated
    assert not parsed.complete and _reasons(parsed) == ["UNTERMINATED"]


def test_a_missing_eol_after_the_stream_keyword_is_flagged() -> None:
    parsed = _obj(b"1 0 obj << /Length 2 >> stream ab\nendstream endobj")
    assert "STREAM_EOL" in _reasons(parsed)


def test_a_stream_after_a_non_dictionary_is_flagged() -> None:
    parsed = _obj(b"1 0 obj 5 stream\nab\nendstream endobj")
    assert parsed.stream is not None and "UNEXPECTED_TOKEN" in _reasons(parsed)


# ── Structure problems ────────────────────────────────────────────────────
@pytest.mark.parametrize(("data", "reasons", "complete"), [
    (b"1 0 obj 42 (extra) foo endobj", ["EXTRA_TOKENS"], True),
    (b"1 0 obj << /A 1 >> 2 0 obj 5 endobj", ["MISSING_ENDOBJ"], False),
    (b"1 0 obj << /A 1 >> xref", ["MISSING_ENDOBJ"], False),
    (b"1 0 obj << /A 1 >>", ["MISSING_ENDOBJ"], False),
    (b"1 0 obj endobj", ["MISSING_VALUE"], True),
    (b"1 0 obj << /A [1 2 >> endobj", ["UNEXPECTED_TOKEN", "UNTERMINATED"], True),
    (b"1 0 obj << /A (x endobj", ["UNTERMINATED", "UNTERMINATED", "MISSING_ENDOBJ"], False),
    (b"1 0 obj << (key) 1 /B >> endobj", ["UNEXPECTED_TOKEN", "UNEXPECTED_TOKEN",
                                          "MISSING_VALUE"], True),
    (b"1 0 obj [1 ) foo 2] endobj", ["STRAY_DELIMITER", "UNEXPECTED_TOKEN",
                                     "UNEXPECTED_TOKEN"], True),
    (b"1 0 obj 1 endstream endobj", ["EXTRA_TOKENS"], True),
])
def test_structure_problems_are_flagged(data: bytes, reasons: list[str], complete: bool) -> None:
    parsed = _obj(data)
    assert _reasons(parsed) == reasons
    assert parsed.complete is complete


def test_strays_keep_their_decoded_bytes() -> None:
    # A string where no value fits is still decoded, never dropped.
    parsed = _obj(b"1 0 obj << <3132332d34352d36373839> 1 >> (tail) endobj")
    assert ("str", b"123-45-6789") in [_plain(s) for s in parsed.strays]
    assert ("str", b"tail") in [_plain(s) for s in parsed.strays]


def test_a_following_header_ends_an_open_container() -> None:
    parsed = _obj(b"1 0 obj [1 2 3 0 obj 4 endobj")
    assert _plain(parsed.value) == [1, 2] and not parsed.complete
    assert _reasons(parsed) == ["UNTERMINATED", "MISSING_ENDOBJ"]


def test_parse_value_at_reads_one_value() -> None:
    data = b"  << /Size 3 /Root 1 0 R >> startxref"
    parsed = ObjectParser(data).parse_value_at(0)
    assert _plain(parsed.value) == [(b"Size", 3), (b"Root", ("ref", 1, 0))]
    assert parsed.span == Span(2, 27) and parsed.flags == ()


# ── Limits ────────────────────────────────────────────────────────────────
def test_long_numbers_are_flagged_not_converted() -> None:
    parsed = _obj(b"1 0 obj [" + b"9" * 5000 + b" 1." + b"5" * 5000 + b"] endobj")
    assert _plain(parsed.value) == [None, None]
    assert _reasons(parsed) == ["NUMBER_OUT_OF_RANGE"] * 2


def test_nesting_past_the_limit_stops_the_parse() -> None:
    parsed = _obj(b"1 0 obj " + b"[" * 300 + b"]" * 300 + b" endobj")
    assert "NESTING_LIMIT" in _reasons(parsed) and not parsed.complete
    shallow = _obj(b"1 0 obj " + b"[" * 5 + b"]" * 5 + b" endobj", limits=Limits())
    assert shallow.flags == ()


def test_tokens_past_the_limit_stop_the_parse() -> None:
    parsed = _obj(b"1 0 obj [" + b"1 " * 50 + b"] endobj",
                  limits=Limits(max_tokens_per_object=20))
    assert "TOKEN_LIMIT" in _reasons(parsed) and not parsed.complete


def test_flags_past_the_cap_are_counted() -> None:
    parsed = _obj(b"1 0 obj [" + b") " * 100 + b"] endobj",
                  limits=Limits(max_flags_per_object=10))
    assert len(parsed.flags) == 11
    assert parsed.flags[-1].reason is FlagReason.FLAGS_TRUNCATED
    # 100 strays, each flagged by the lexer and the parser: 200, 10 kept.
    assert dict(parsed.flags[-1].params) == {"dropped": 190}


@pytest.mark.parametrize("data", [
    b"1 0 obj [" + b"1 " * 400_000 + b"] endobj",
    b"1 0 obj <<" + b"/K 1 " * 200_000 + b">> endobj",
    b"1 0 obj <<" + b"/K (x) " * 100_000 + b">> endobj",   # 100k duplicate keys
    b"1 0 obj " + b"[" * 200 + b"(" * 500_000,
    b"1 0 obj [" + b"1 0 " * 200_000 + b"] endobj",           # lookahead never a ref
])
def test_large_objects_parse_in_linear_time(data: bytes) -> None:
    started = time.process_time()
    _obj(data)
    assert time.process_time() - started < 30


def test_many_streams_without_endstream_scan_linearly() -> None:
    one = b"%d 0 obj << /Length 1 >> stream\nx\n"
    data = b"".join(one % i for i in range(1, 20_001))
    parser = ObjectParser(data)
    started = time.process_time()
    offset = 0
    for i in range(1, 20_001):
        parsed = parser.parse_indirect_at(offset)
        assert parsed is not None and parsed.num == i
        offset += len(one % i)
    assert time.process_time() - started < 30


def test_a_large_first_value_repeated_many_times_stays_linear() -> None:
    # Each key's first value is tokenized once, not once per repeat
    # (quadratic, this n took about a minute; linear, well under a second).
    n = 6_000
    data = (b"1 0 obj << /K [" + b"1 " * n + b"] " + b"/K 1 " * n + b">> endobj")
    started = time.process_time()
    parsed = _obj(data)
    assert time.process_time() - started < 30
    assert _reasons(parsed) == ["DUPLICATE_KEY"] * 64 + ["FLAGS_TRUNCATED"]


def test_a_length_past_endstream_never_reaches_into_the_next_object() -> None:
    first = b"1 0 obj <</Length 40>> stream\nab\nendstream endobj\n"
    data = first + b"2 0 obj <</Length 2>> stream\ncd\nendstream endobj"
    parsed = _obj(data)
    assert parsed.span == Span(0, len(first) - 1) and parsed.complete
    assert parsed.stream is not None and parsed.stream.slack is None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"ab"
    assert _reasons(parsed) == ["LENGTH_MISMATCH"]


def test_endstream_directly_followed_by_endobj_ends_the_stream_but_is_flagged() -> None:
    # MuPDF ends the stream there; qpdf reads `endstreamendobj` as one word
    # and recovers a longer stream. Found either way, flagged either way.
    data = b"1 0 obj <</Length 2>> stream\nab\nendstreamendobj"
    parsed = _obj(data)
    assert parsed.complete and parsed.span == Span(0, len(data))
    assert parsed.stream is not None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"ab"
    assert [(f.reason.name, f.span) for f in parsed.flags] == [
        ("ENDSTREAM_JOINED", Span(len(data) - 15, len(data)))]


def test_a_lone_cr_ends_the_stream_keyword_line_but_is_flagged() -> None:
    # The spec allows CRLF or LF; MuPDF skips a lone CR, qpdf warns.
    data = b"1 0 obj <</Length 2>> stream\rab\rendstream endobj"
    parsed = _obj(data)
    assert _reasons(parsed) == ["STREAM_EOL"] and parsed.stream is not None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"ab"


def test_endstream_must_be_a_whole_keyword() -> None:
    data = b"1 0 obj <<>> stream\na endstreamx b\nendstream endobj"
    parsed = _obj(data)
    assert parsed.stream is not None and parsed.complete
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"a endstreamx b"


def test_an_endstream_cut_by_the_range_end_does_not_count() -> None:
    data = b"1 0 obj <</Length 2>> stream\nab\nendstream endobj"
    parsed = ObjectParser(data).parse_indirect_at(0, len(data) - 12)
    assert parsed is not None and parsed.stream is not None
    assert not parsed.stream.terminated and "UNTERMINATED" in _reasons(parsed)


def test_a_raising_length_resolver_counts_as_unresolved() -> None:
    def boom(num: int, gen: int) -> int | None:
        raise RuntimeError("resolver failed")

    parsed = _obj(b"1 0 obj <</Length 2 0 R>> stream\nab\nendstream endobj",
                  resolve_length=boom)
    assert [dict(f.params) for f in parsed.flags] == [{"declared": -1, "found": 2}]


def test_the_digit_limit_never_exceeds_what_int_accepts() -> None:
    parsed = _obj(b"1 0 obj " + b"7" * 5000 + b" endobj",
                  limits=Limits(max_number_digits=10_000))
    assert _plain(parsed.value) is None and _reasons(parsed) == ["NUMBER_OUT_OF_RANGE"]


@given(_noise | st.binary(max_size=80), st.integers(-5, 90), st.integers(-5, 90),
       st.sampled_from([None, 0, -1, 3, 10**30, "x", 2.5, True]))
def test_never_raises_at_any_offset_bound_or_resolver_answer(
        body: bytes, offset: int, end: int, answer: Any) -> None:
    data = b"1 0 obj " + body
    parser = ObjectParser(data, Limits(max_tokens_per_object=30, max_container_depth=3),
                          resolve_length=lambda num, gen: answer)
    for parsed in (parser.parse_indirect_at(offset, end), parser.parse_indirect_at(offset)):
        if parsed is not None:
            assert 0 <= parsed.span.start <= parsed.span.end <= len(data)
    value = parser.parse_value_at(offset, end)
    assert 0 <= value.span.start <= value.span.end <= len(data)


@pytest.mark.parametrize("gap", [b"", b"\n", b"\r", b"\r\n"])
def test_one_end_of_line_before_endstream_is_not_slack(gap: bytes) -> None:
    parsed = _obj(b"1 0 obj <</Length 2>> stream\nab" + gap + b"endstream endobj")
    assert parsed.flags == () and parsed.stream is not None and parsed.stream.slack is None


@pytest.mark.parametrize("gap", [b" \x00" * 50 + b"\n", b"\n\n", b"  \n", b"\x0c\n", b"\t"])
def test_more_whitespace_before_endstream_is_slack(gap: bytes) -> None:
    # MuPDF reads these bytes as stream data (spaces and NULs are valid
    # image samples); qpdf does not. Readers disagree, so it is flagged.
    data = b"1 0 obj <</Length 2>> stream\nab" + gap + b"endstream endobj"
    parsed = _obj(data)
    assert parsed.stream is not None and parsed.stream.slack is not None
    assert data[parsed.stream.data.start:parsed.stream.data.end] == b"ab"
    assert data[parsed.stream.slack.start:parsed.stream.slack.end] == gap
    assert _reasons(parsed) == ["STREAM_SLACK"]


def test_a_comment_between_length_and_endstream_is_slack() -> None:
    parsed = _obj(b"1 0 obj <</Length 2>> stream\nab\n%x\nendstream endobj")
    assert _reasons(parsed) == ["STREAM_SLACK"]


def test_the_digit_cap_is_inclusive() -> None:
    at_cap = _obj(b"1 0 obj " + b"7" * 64 + b" endobj")
    assert at_cap.flags == () and _plain(at_cap.value) == int("7" * 64)
    past = _obj(b"1 0 obj " + b"7" * 65 + b" endobj")
    assert _reasons(past) == ["NUMBER_OUT_OF_RANGE"]


@pytest.mark.parametrize("text", [b"-1 0 R", b"1 -0 R", b"+1 0 R", b"1 0.0 R"])
def test_signed_or_real_numbers_are_not_references(text: bytes) -> None:
    parsed = _obj(b"1 0 obj [" + text + b"] endobj")
    assert all(not isinstance(item, PdfRef) for item in
               (parsed.value.items if isinstance(parsed.value, PdfArray) else ()))
    assert "UNEXPECTED_TOKEN" in _reasons(parsed)  # the lone R
