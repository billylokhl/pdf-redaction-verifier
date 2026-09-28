"""The inventory's PDF lexer (ISO 32000-1 §7.2-7.3).

``Lexer`` turns bytes into tokens that carry only their kind and their
byte span; the bytes themselves are decoded on demand (``strings.py``),
so nothing here copies document content. Every byte of the range it
scans lands in exactly one token or in a run of PDF whitespace between
tokens. It never raises on input bytes: a malformed token is still a
token (``complete`` False when it ran off the end of the range), and
each anomaly is a Flag on the token itself carrying only a span and
integers -- at most two per token, so flags never outgrow tokens. Linear in the bytes
scanned: every loop advances, and each byte is examined a bounded number
of times.

The lexer knows nothing about objects or streams: it never skips stream
data by itself. The object parser (3a-3) seeks past a stream's bytes
with ``seek``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..ledger import Flag, FlagReason, Span

# ISO 32000-1 §7.2.2, Tables 1 (white-space) and 2 (delimiters).
WHITESPACE: Final = b"\0\t\n\f\r "
DELIMITERS: Final = b"()<>[]{}/%"

_WS_RUN: Final = re.compile(rb"[\x00\t\n\x0c\r ]*")
# A run of regular characters: neither white-space nor a delimiter.
_REGULAR_RUN: Final = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_EOL: Final = re.compile(rb"[\r\n]")
_PAREN_OR_BACKSLASH: Final = re.compile(rb"[()\\]")
_NOT_HEX_OR_WS: Final = re.compile(rb"[^0-9A-Fa-f\x00\t\n\x0c\r ]")
# A '#' in a name not followed by two hexadecimal digits (§7.3.5).
_BAD_NAME_ESCAPE: Final = re.compile(rb"#(?![0-9A-Fa-f]{2})")
# §7.3.3: an integer, or a real with a '.' and at least one digit.
_INTEGER: Final = re.compile(rb"[+-]?[0-9]+")
_REAL: Final = re.compile(rb"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)")


class TokenKind(Enum):
    INTEGER = "integer"
    REAL = "real"
    NAME = "name"                      # /Name, the span includes the '/'
    LITERAL_STRING = "literal_string"  # (...), the span includes both parens
    HEX_STRING = "hex_string"          # <...>, the span includes both brackets
    ARRAY_OPEN = "array_open"          # [
    ARRAY_CLOSE = "array_close"        # ]
    DICT_OPEN = "dict_open"            # <<
    DICT_CLOSE = "dict_close"          # >>
    PROC_OPEN = "proc_open"            # {  (PostScript calculator functions)
    PROC_CLOSE = "proc_close"          # }
    KEYWORD = "keyword"                # any other run of regular characters:
                                       # obj, R, true, null, 1.2.3, ...
    COMMENT = "comment"                # % to the end of the line, EOL excluded
    STRAY = "stray"                    # a lone ')' or '>' (flagged)


@dataclass(frozen=True)
class Token:
    """A token's kind and its byte span in the lexed data. ``complete``
    is False only for a string that ran off the end of the range
    without its closing delimiter (flagged UNTERMINATED)."""

    kind: TokenKind
    start: int
    end: int
    complete: bool = True
    flags: tuple[Flag, ...] = ()

    @property
    def span(self) -> Span:
        return Span(self.start, self.end)


_SINGLE: Final = {
    ord("["): TokenKind.ARRAY_OPEN,
    ord("]"): TokenKind.ARRAY_CLOSE,
    ord("{"): TokenKind.PROC_OPEN,
    ord("}"): TokenKind.PROC_CLOSE,
}


class Lexer:
    """Tokens of ``data[pos:end]``, one at a time.

    ``next_token`` returns None at the end of the range. ``seek`` moves
    to any offset in the range (clamped). The lexer keeps no state but
    its position, so a token re-lexed after a seek is the same token.
    """

    def __init__(self, data: bytes, pos: int = 0, end: int | None = None) -> None:
        self.data = data
        self.end = len(data) if end is None else max(0, min(end, len(data)))
        self.pos = 0
        self.seek(pos)

    def seek(self, pos: int) -> None:
        self.pos = max(0, min(pos, self.end))

    def __iter__(self) -> Iterator[Token]:
        while (token := self.next_token()) is not None:
            yield token

    def next_token(self) -> Token | None:
        data, end = self.data, self.end
        match = _WS_RUN.match(data, self.pos, end)
        pos = match.end() if match else self.pos
        if pos >= end:
            self.pos = end
            return None
        c = data[pos]
        if c == 0x2F:  # '/'
            token = self._name(pos)
        elif c == 0x28:  # '('
            token = self._literal(pos)
        elif c == 0x3C:  # '<'
            if pos + 1 < end and data[pos + 1] == 0x3C:
                token = Token(TokenKind.DICT_OPEN, pos, pos + 2)
            else:
                token = self._hex(pos)
        elif c == 0x3E:  # '>'
            if pos + 1 < end and data[pos + 1] == 0x3E:
                token = Token(TokenKind.DICT_CLOSE, pos, pos + 2)
            else:
                token = self._stray(pos)
        elif c == 0x29:  # ')'
            token = self._stray(pos)
        elif c == 0x25:  # '%'
            eol = _EOL.search(data, pos, end)
            token = Token(TokenKind.COMMENT, pos, eol.start() if eol else end)
        elif c in _SINGLE:
            token = Token(_SINGLE[c], pos, pos + 1)
        else:
            run = _REGULAR_RUN.match(data, pos, end)
            stop = run.end() if run else pos + 1
            text = data[pos:stop]
            if _INTEGER.fullmatch(text):
                kind = TokenKind.INTEGER
            elif _REAL.fullmatch(text):
                kind = TokenKind.REAL
            else:
                kind = TokenKind.KEYWORD
            token = Token(kind, pos, stop)
        self.pos = token.end
        return token

    def _stray(self, pos: int) -> Token:
        flag = Flag(FlagReason.STRAY_DELIMITER, Span(pos, pos + 1))
        return Token(TokenKind.STRAY, pos, pos + 1, flags=(flag,))

    def _name(self, pos: int) -> Token:
        run = _REGULAR_RUN.match(self.data, pos + 1, self.end)
        stop = run.end() if run else pos + 1
        flags = _spread(FlagReason.INVALID_NAME_ESCAPE,
                        _BAD_NAME_ESCAPE.finditer(self.data, pos + 1, stop))
        return Token(TokenKind.NAME, pos, stop, flags=flags)

    def _literal(self, pos: int) -> Token:
        """Balanced parentheses; a backslash escapes the next byte. (An
        escaped CR's LF needs no special case: LF ends nothing here.)"""
        data, end = self.data, self.end
        depth = 1
        i = pos + 1
        while True:
            hit = _PAREN_OR_BACKSLASH.search(data, i, end)
            if hit is None:
                flag = Flag(FlagReason.UNTERMINATED, Span(pos, end))
                return Token(TokenKind.LITERAL_STRING, pos, end, complete=False,
                             flags=(flag,))
            i = hit.start()
            c = data[i]
            if c == 0x5C:  # '\\'
                i = min(i + 2, end)
                continue
            depth += 1 if c == 0x28 else -1
            i += 1
            if depth == 0:
                return Token(TokenKind.LITERAL_STRING, pos, i)

    def _hex(self, pos: int) -> Token:
        data, end = self.data, self.end
        close = data.find(b">", pos + 1, end)
        stop = end if close < 0 else close
        flags = _spread(FlagReason.INVALID_HEX_DIGIT,
                        _NOT_HEX_OR_WS.finditer(data, pos + 1, stop))
        if close < 0:
            flags += (Flag(FlagReason.UNTERMINATED, Span(pos, end)),)
            return Token(TokenKind.HEX_STRING, pos, end, complete=False, flags=flags)
        return Token(TokenKind.HEX_STRING, pos, close + 1, flags=flags)


def _spread(reason: FlagReason, hits: Iterator[re.Match[bytes]]) -> tuple[Flag, ...]:
    """One flag for all of a token's bad spots: from the first to the end
    of the last, with their count. No spots, no flag."""
    first = last = count = 0
    for hit in hits:
        if not count:
            first = hit.start()
        last = hit.end()
        count += 1
    if not count:
        return ()
    return (Flag(reason, Span(first, last), (("count", count),)),)
