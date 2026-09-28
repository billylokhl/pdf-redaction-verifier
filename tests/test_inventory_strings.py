"""The inventory's string and name decoding (Phase 3a-2): ISO 32000-1
§7.3.4-7.3.5 and §7.9.2, including the two end-of-line rules the legacy
``verify._decode_pdf_string`` gets wrong."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import verify
from redaction_verifier.inventory.lexer import Lexer, TokenKind
from redaction_verifier.inventory.strings import (
    TextEncoding,
    hex_bytes,
    literal_bytes,
    name_bytes,
    text_string,
    token_bytes,
)


@pytest.mark.parametrize(("body", "expected"), [
    (rb"a\nb\rc\td\be\ff\(\)\\", b"a\nb\rc\td\be\ff()\\"),
    (rb"(nested) (twice (deep))", b"(nested) (twice (deep))"),
    (rb"\0\53\053\0053", b"\x00++\x053"),
    (rb"\400\777", b"\x00\xff"),        # high-order overflow is ignored
    (rb"\q\8", b"q8"),                   # an unknown escape: the backslash is ignored
    # §7.3.4.2: backslash + end-of-line is a continuation, nothing at all...
    (b"123-45-\\\n6789", b"123-45-6789"),
    (b"123-45-\\\r6789", b"123-45-6789"),
    (b"123-45-\\\r\n6789", b"123-45-6789"),
    # ...and an unescaped end-of-line of any kind is one LF.
    (b"a\r\nb\rc\nd\n\re", b"a\nb\nc\nd\n\ne"),
    (b"end\\", b"end"),                  # a lone trailing backslash
    (b"", b""),
])
def test_literal_bytes(body: bytes, expected: bytes) -> None:
    assert literal_bytes(body) == expected


@pytest.mark.parametrize(("body", "legacy", "fixed"), [
    (b"123-45-\\\n6789", "123-45-\n6789", "123-45-6789"),
    (b"a\r\nb", "a\r\nb", "a\nb"),
])
def test_the_two_legacy_end_of_line_bugs_are_fixed_here_only(
        body: bytes, legacy: str, fixed: str) -> None:
    token = b"(" + body + b")"
    # The legacy decoder stays frozen (a 3a ground rule); only the new one follows the spec.
    assert verify._decode_pdf_string(token.decode("latin-1")) == legacy
    assert text_string(literal_bytes(body)).text == fixed


_ascii_body = st.lists(st.sampled_from(list("abc 019()-/%<>#\t") + [
    "\\n", "\\t", "\\(", "\\)", "\\\\", "\\12", "\\101", "\\q"]), max_size=40).map("".join).filter(
    lambda s: s.count("(") - s.count("\\(") == s.count(")") - s.count("\\)"))


@given(_ascii_body)
def test_matches_the_legacy_decoder_away_from_end_of_line_escapes(body: str) -> None:
    # Where neither end-of-line rule applies (no CR, no backslash-EOL) and
    # the text is ASCII, the two decoders must agree exactly.
    legacy = verify._decode_pdf_string("(" + body + ")")
    assert text_string(literal_bytes(body.encode("ascii"))).text == legacy


def _escape(raw: bytes) -> bytes:
    out = bytearray()
    for b in raw:
        if b in b"()\\":
            out += b"\\" + bytes([b])
        elif b == 0x0D:
            out += b"\\r"
        else:
            out.append(b)
    return bytes(out)


@given(st.binary(max_size=60))
def test_literal_round_trips(raw: bytes) -> None:
    assert literal_bytes(_escape(raw)) == raw
    assert literal_bytes(b"".join(b"\\%03o" % b for b in raw)) == raw
    (token,) = Lexer(b"(" + _escape(raw) + b")")
    assert token.kind is TokenKind.LITERAL_STRING and token.complete
    assert token_bytes(b"(" + _escape(raw) + b")", token) == raw


@given(st.binary(max_size=60))
def test_hex_round_trips(raw: bytes) -> None:
    assert hex_bytes(raw.hex().encode()) == raw
    assert hex_bytes(" ".join(raw.hex().upper()).encode()) == raw


@given(st.binary(max_size=80))
def test_decoders_never_raise(data: bytes) -> None:
    literal_bytes(data)
    hex_bytes(data)
    name_bytes(data)
    text_string(data)
    for token in Lexer(data):
        token_bytes(data, token)


@pytest.mark.parametrize(("body", "expected"), [
    (b"4", b"\x40"), (b"41 42\n4", b"AB\x40"), (b"", b""), (b"4g1", b"\x41"),
])
def test_hex_bytes(body: bytes, expected: bytes) -> None:
    assert hex_bytes(body) == expected


@pytest.mark.parametrize(("body", "expected"), [
    (b"A#20B", b"A B"), (b"#2f#2F", b"//"), (b"A#4", b"A#4"), (b"A#G1", b"A#G1"),
    (b"", b""),
])
def test_name_bytes(body: bytes, expected: bytes) -> None:
    assert name_bytes(body) == expected


def test_token_bytes_by_kind() -> None:
    data = b"(a\\)b) <4142> /N#41 12 (open"
    got = [token_bytes(data, t) for t in Lexer(data)]
    assert got == [b"a)b", b"AB", b"NA", None, b"open"]


def test_text_string_encodings_keep_the_raw_bytes() -> None:
    utf16 = b"\xfe\xff" + "SSN 123".encode("utf-16-be")
    assert text_string(utf16).text == "SSN 123"
    assert text_string(utf16).raw == utf16
    assert text_string(utf16).encoding is TextEncoding.UTF16BE
    utf8 = b"\xef\xbb\xbf" + "café".encode()
    assert (text_string(utf8).text, text_string(utf8).encoding) == ("café", TextEncoding.UTF8)
    pdfdoc = text_string(b"\x80\x84\x92\xa0\xe9\x18\x7f\x9f\xad")
    assert pdfdoc.encoding is TextEncoding.PDFDOC
    # PDFDocEncoding's own characters; undefined bytes keep their Latin-1 code point.
    assert pdfdoc.text == "•—™€é˘\x7f\x9f\xad"


def test_utf16_language_tags_are_removed_and_odd_bytes_replaced() -> None:
    tagged = "\x1benUS\x1b123-45-\x1bfr\x1b6789".encode("utf-16-be")
    assert text_string(b"\xfe\xff" + tagged).text == "123-45-6789"
    # An ESC that is not a tag stays.
    assert text_string(b"\xfe\xff" + "a\x1bb".encode("utf-16-be")).text == "a\x1bb"
    assert text_string(b"\xfe\xff\x00A\x00").text == "A�"


def test_pdfdoc_matches_latin1_outside_the_table() -> None:
    raw = bytes(range(256))
    text = text_string(b"x" + raw).text[1:]  # a leading x: no byte-order mark
    differs = {i for i in range(256) if text[i] != chr(i)}
    assert differs == set(range(0x18, 0x20)) | set(range(0x80, 0x9F)) | {0xA0}
