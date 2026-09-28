"""The inventory's lexer (docs/REDESIGN.md §4, Phase 3a-2): every byte
lands in one token or in whitespace between tokens, tokens are what ISO
32000-1 §7.2-7.3 says they are, anomalies are flags on the token, and
nothing raises or goes superlinear on any input."""

from __future__ import annotations

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from redaction_verifier.inventory.lexer import DELIMITERS, WHITESPACE, Lexer, Token, TokenKind
from redaction_verifier.model import Flag, FlagReason, Span

K = TokenKind


def _lex(data: bytes) -> list[tuple[TokenKind, bytes]]:
    return [(t.kind, data[t.start:t.end]) for t in Lexer(data)]


# Bytes weighted toward the ones the lexer treats specially.
_pdfish = st.lists(st.sampled_from(list(b"()<>[]{}/%\\#\r\n \t\x00\x0c+-.019aFgR")),
                   max_size=80).map(bytes)
_any = _pdfish | st.binary(max_size=80)


@given(_any)
def test_tokens_and_whitespace_partition_the_bytes(data: bytes) -> None:
    pos = 0
    for token in Lexer(data):
        assert token.start < token.end <= len(data)
        assert all(b in WHITESPACE for b in data[pos:token.start])
        pos = token.end
    assert all(b in WHITESPACE for b in data[pos:])


_ATOMS = (TokenKind.INTEGER, TokenKind.REAL, TokenKind.KEYWORD, TokenKind.NAME)


@given(_any)
def test_regular_runs_hold_no_whitespace_or_delimiter(data: bytes) -> None:
    for token in Lexer(data):
        if token.kind in _ATOMS:
            body = data[token.start + (token.kind is TokenKind.NAME):token.end]
            assert not any(b in WHITESPACE or b in DELIMITERS for b in body)


@given(_any, st.data())
def test_a_token_relexed_from_its_start_is_the_same_token(data: bytes, draw: st.DataObject) -> None:
    tokens = list(Lexer(data))
    if tokens:
        token = draw.draw(st.sampled_from(tokens))
        assert Lexer(data, token.start).next_token() == token


@given(_any, st.integers(0, 80), st.integers(0, 80))
def test_a_subrange_is_lexed_as_if_it_were_the_whole_input(data: bytes, a: int, b: int) -> None:
    start, end = min(a, b), max(a, b)
    inner = [(t.kind, t.start + start, t.end + start, t.complete)
             for t in Lexer(data[start:end])]
    assert inner == [(t.kind, t.start, t.end, t.complete) for t in Lexer(data, start, end)]


@given(_any)
def test_flags_sit_inside_their_token_and_never_exceed_two(data: bytes) -> None:
    for token in Lexer(data):
        assert len(token.flags) <= 2
        for flag in token.flags:
            assert flag.span is not None
            assert token.start <= flag.span.start <= flag.span.end <= token.end
        assert token.complete == (FlagReason.UNTERMINATED not in {f.reason for f in token.flags})


@pytest.mark.parametrize(("data", "expected"), [
    (b"1 0 obj << /Type /Page >> endobj", [
        (K.INTEGER, b"1"), (K.INTEGER, b"0"), (K.KEYWORD, b"obj"), (K.DICT_OPEN, b"<<"),
        (K.NAME, b"/Type"), (K.NAME, b"/Page"), (K.DICT_CLOSE, b">>"), (K.KEYWORD, b"endobj")]),
    (b"[-.5 +3 4. 1.2.3 --1 12abc]", [
        (K.ARRAY_OPEN, b"["), (K.REAL, b"-.5"), (K.INTEGER, b"+3"), (K.REAL, b"4."),
        (K.KEYWORD, b"1.2.3"), (K.KEYWORD, b"--1"), (K.KEYWORD, b"12abc"),
        (K.ARRAY_CLOSE, b"]")]),
    # Delimiters end a regular run; a lone '/' is the empty name.
    (b"/A/B(x)<41>/ 5 0 R 5R", [
        (K.NAME, b"/A"), (K.NAME, b"/B"), (K.LITERAL_STRING, b"(x)"),
        (K.HEX_STRING, b"<41>"), (K.NAME, b"/"), (K.INTEGER, b"5"), (K.INTEGER, b"0"),
        (K.KEYWORD, b"R"), (K.KEYWORD, b"5R")]),
    # Nested parens, escaped parens and a '%' inside a literal.
    (rb"(a (b) \) % c) d", [(K.LITERAL_STRING, rb"(a (b) \) % c)"), (K.KEYWORD, b"d")]),
    # A backslash escapes CR LF as one end-of-line, not just the CR.
    (b"(a\\\r\n) x", [(K.LITERAL_STRING, b"(a\\\r\n)"), (K.KEYWORD, b"x")]),
    (b"%PDF-1.7\r\n%%EOF", [(K.COMMENT, b"%PDF-1.7"), (K.COMMENT, b"%%EOF")]),
    (b"{ 2 mul }", [(K.PROC_OPEN, b"{"), (K.INTEGER, b"2"), (K.KEYWORD, b"mul"),
                    (K.PROC_CLOSE, b"}")]),
    (b"<< >> > )", [(K.DICT_OPEN, b"<<"), (K.DICT_CLOSE, b">>"), (K.STRAY, b">"),
                    (K.STRAY, b")")]),
    (b"<4 1\n4>", [(K.HEX_STRING, b"<4 1\n4>")]),
    # NUL and form feed are whitespace: they end a regular run.
    (b"a\x00b\x0cc", [(K.KEYWORD, b"a"), (K.KEYWORD, b"b"), (K.KEYWORD, b"c")]),
    (b"<41\x0c42>", [(K.HEX_STRING, b"<41\x0c42>")]),
])
def test_tokens(data: bytes, expected: list[tuple[TokenKind, bytes]]) -> None:
    assert _lex(data) == expected


def test_unterminated_strings_run_to_the_end_of_the_range_and_are_flagged() -> None:
    assert list(Lexer(b"(a(b)")) == [Token(K.LITERAL_STRING, 0, 5, complete=False, flags=(
        Flag(FlagReason.UNTERMINATED, Span(0, 5)),))]
    assert list(Lexer(b"<41 (x)")) == [Token(K.HEX_STRING, 0, 7, complete=False, flags=(
        Flag(FlagReason.INVALID_HEX_DIGIT, Span(4, 7), (("count", 3),)),
        Flag(FlagReason.UNTERMINATED, Span(0, 7))))]
    # ...the end of the range, not of the data.
    assert list(Lexer(b"x (ab) y", 2, 4)) == [Token(K.LITERAL_STRING, 2, 4, complete=False, flags=(
        Flag(FlagReason.UNTERMINATED, Span(2, 4)),))]
    # A trailing backslash cannot escape past the end.
    assert list(Lexer(b"(\\")) == [Token(K.LITERAL_STRING, 0, 2, complete=False, flags=(
        Flag(FlagReason.UNTERMINATED, Span(0, 2)),))]


def test_one_flag_covers_all_of_a_tokens_bad_spots() -> None:
    (token,) = Lexer(b"<4x1y>")
    assert token.flags == (Flag(FlagReason.INVALID_HEX_DIGIT, Span(2, 5), (("count", 2),)),)
    (token,) = Lexer(b"/A#4G#20#")
    assert token.flags == (Flag(FlagReason.INVALID_NAME_ESCAPE, Span(2, 9), (("count", 2),)),)
    (token,) = Lexer(b"/A#20")
    assert token.flags == ()


def test_seek_is_clamped_to_the_data_and_the_end() -> None:
    lexer = Lexer(b"1 2 3", 1, 4)
    lexer.seek(-5)
    assert lexer.pos == 0  # pos is where lexing starts, not a floor
    lexer.seek(99)
    assert lexer.next_token() is None and lexer.pos == 4
    assert list(Lexer(b"1 2", 9)) == [] and list(Lexer(b"1 2", 0, -3)) == []


@pytest.mark.parametrize("data", [
    b"(" * 1_000_000,
    b"\\(" + b"\\" * 1_000_000,
    b"<" + b"(" * 1_000_000,
    b"<a" * 500_000,
    b"/" + b"#" * 1_000_000,
    b")" * 200_000,
    b"%" * 1_000_000,
    b"1" * 1_000_000,
    b"(" + b"\\\r\n" * 300_000,
])
def test_adversarial_inputs_lex_in_linear_time(data: bytes) -> None:
    started = time.process_time()
    count = sum(1 for _ in Lexer(data))
    assert count >= 1
    # A quadratic lexer takes minutes on these; a linear one well under a second.
    assert time.process_time() - started < 10


def test_every_whitespace_byte_is_whitespace_inside_a_hex_string() -> None:
    (token,) = Lexer(b"<4\x001\t4\n2\x0c4\r3 >")
    assert token.kind is K.HEX_STRING and token.complete and token.flags == ()
