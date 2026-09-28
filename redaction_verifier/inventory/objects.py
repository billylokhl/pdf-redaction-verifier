"""The inventory's object parser (ISO 32000-1 §7.3, §7.3.8, §7.3.10).

``ObjectParser`` reads values and indirect objects from a file's bytes
on top of the lexer. Every value keeps its byte span. Nothing here ever
raises on input bytes: whatever does not fit the grammar is flagged and
kept (a token that fits nowhere becomes a *stray* value, decoded like any
other, so a string in the wrong place is never lost), and parsing is
iterative, with the tokens, nesting depth and flags of one object capped
by ``Limits``. Stream data is never lexed: the parser seeks past it.

Ambiguity is flagged, never resolved silently (REDESIGN §4, "Ambiguity
detection"): a repeated dictionary key (with whether the values are
textually identical, ADR 0002), a /Length that disagrees with where
``endstream`` is, bytes between the two (STREAM_SLACK), tokens after an
object's value, a missing ``endobj``. ``PdfDict.get`` refuses a key
whose repeats disagree.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias

from ..budget import Limits
from ..ledger import Flag, FlagReason, Span
from .lexer import Lexer, Token, TokenKind
from .strings import hex_bytes, literal_bytes, name_bytes


@dataclass(frozen=True)
class PdfNull:
    start: int
    end: int


@dataclass(frozen=True)
class PdfBool:
    start: int
    end: int
    value: bool


@dataclass(frozen=True)
class PdfInt:
    """``value`` is None when the token has more than
    ``Limits.max_number_digits`` digits (flagged NUMBER_OUT_OF_RANGE)."""

    start: int
    end: int
    value: int | None


@dataclass(frozen=True)
class PdfReal:
    start: int
    end: int
    value: float | None


@dataclass(frozen=True)
class PdfName:
    start: int
    end: int
    raw: bytes  # after #xx escapes, without the '/'


@dataclass(frozen=True)
class PdfString:
    start: int
    end: int
    raw: bytes  # the bytes the literal or hex string denotes
    hex: bool


@dataclass(frozen=True)
class PdfRef:
    start: int
    end: int
    num: int
    gen: int


@dataclass(frozen=True)
class PdfKeyword:
    """A token no value grammar accepts here (an unknown keyword, a lone
    delimiter, a brace): only ever a stray."""

    start: int
    end: int
    raw: bytes


@dataclass(frozen=True)
class PdfArray:
    start: int
    end: int
    items: tuple[PdfValue, ...]


@dataclass(frozen=True)
class PdfDict:
    """Entries in file order, repeats included. ``ambiguous`` holds the
    keys repeated with textually different values."""

    start: int
    end: int
    entries: tuple[tuple[PdfName, PdfValue], ...]
    ambiguous: frozenset[bytes] = frozenset()

    def get(self, key: bytes) -> PdfValue | None:
        """The value of *key*: its first occurrence when every occurrence
        is textually identical, None when absent or ambiguous."""
        if key in self.ambiguous:
            return None
        for name, value in self.entries:
            if name.raw == key:
                return value
        return None


PdfValue: TypeAlias = (PdfNull | PdfBool | PdfInt | PdfReal | PdfName | PdfString
                       | PdfRef | PdfKeyword | PdfArray | PdfDict)


@dataclass(frozen=True)
class ParsedValue:
    """One value (None when nothing parseable was there), the span the
    parse consumed, strays and flags."""

    value: PdfValue | None
    span: Span
    strays: tuple[PdfValue, ...]
    flags: tuple[Flag, ...]


@dataclass(frozen=True)
class StreamInfo:
    """Where a stream's bytes are. ``data`` is what /Length delimits when
    an ``endstream`` keyword follows it (perhaps after slack), else what
    the scan for ``endstream`` found. ``slack`` holds
    any bytes past /Length before ``endstream`` other than one end-of-line
    (flagged STREAM_SLACK): readers disagree there -- MuPDF reads such
    bytes, even spaces and NULs, as data, and an image can draw text with
    them; qpdf does not. Slack runs to the first ``endstream`` keyword
    after /Length, which may lie in a later object (flagged either way).
    A /Length that runs past an ``endstream`` keyword is a
    LENGTH_MISMATCH and the scan decides.
    """

    data: Span
    slack: Span | None
    declared_length: int | None
    terminated: bool


@dataclass(frozen=True)
class IndirectObject:
    """``N G obj ... endobj``. ``span`` runs from the object number to the
    end of ``endobj`` -- or, when ``complete`` is False (no ``endobj``),
    to the end of the last byte the parse consumed."""

    num: int
    gen: int
    span: Span
    value: PdfValue | None
    stream: StreamInfo | None
    strays: tuple[PdfValue, ...]
    flags: tuple[Flag, ...]
    complete: bool


# Keywords that end an object's value: an open container ends there
# unterminated. `N G obj` (a following object's header) does too.
_STOP: Final = frozenset({b"endobj", b"stream", b"endstream", b"xref", b"trailer", b"startxref"})
# `endstream` as a keyword: followed by a delimiter, whitespace or the end
# -- or directly by `endobj`. Readers disagree there (MuPDF ends the
# stream; qpdf lexes `endstreamendobj` as one word, warns, and recovers a
# length that includes the keyword), so that case is found but flagged
# ENDSTREAM_JOINED. Nothing can hide in the difference: it is only the
# end-of-line and the keyword itself.
_ENDSTREAM: Final = re.compile(
    rb"endstream(?:(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])|(?=endobj))")
# Python's int() refuses longer digit strings (sys.int_info's default).
_INT_DIGITS_CEILING: Final = 4300
_WS_RUN: Final = re.compile(rb"[\x00\t\n\x0c\r ]*")
# What may sit between a stream's /Length and `endstream` unflagged.
_ONE_EOL: Final = (b"", b"\n", b"\r", b"\r\n")
_OPENERS: Final = (TokenKind.ARRAY_OPEN, TokenKind.DICT_OPEN)
_SCALARS: Final = frozenset({TokenKind.INTEGER, TokenKind.REAL, TokenKind.NAME,
                             TokenKind.LITERAL_STRING, TokenKind.HEX_STRING,
                             TokenKind.KEYWORD})
_CLOSERS: Final = {TokenKind.ARRAY_CLOSE: TokenKind.ARRAY_OPEN,
                   TokenKind.DICT_CLOSE: TokenKind.DICT_OPEN}

LengthResolver: TypeAlias = Callable[[int, int], int | None]


class ObjectParser:
    """Parses values and indirect objects out of *data*. *resolve_length*
    answers an indirect /Length (``N G R``) with an integer or None (one
    that raises counts as None); the inventory supplies it once it knows
    the xref (3a-4/5)."""

    def __init__(self, data: bytes, limits: Limits | None = None,
                 resolve_length: LengthResolver | None = None) -> None:
        self.data = data
        self.limits = limits if limits is not None else Limits()
        self.resolve_length = resolve_length
        self._endstreams: list[int] | None = None

    def parse_value_at(self, pos: int, end: int | None = None) -> ParsedValue:
        """One value at *pos* (an object-stream member, a trailer)."""
        run = _Run(self, pos, end)
        start = run.peek_start()
        value = run.value()
        return ParsedValue(value, Span(start, max(start, run.last_end)), tuple(run.strays),
                           run.flags.result())

    def parse_indirect_at(self, offset: int, end: int | None = None) -> IndirectObject | None:
        """The indirect object whose header starts at the first token at
        or after *offset* (clamped to the data), or None when that is not
        ``N G obj`` (the caller flags a bad xref offset).

        A caller parsing many objects must bound *end* by the next known
        object's offset: an unterminated string otherwise runs to the end
        of the data from every offset, which makes a whole-file parse
        quadratic."""
        run = _Run(self, offset, end)
        header = run.header()
        if header is None:
            return None
        num, gen, start = header
        value = run.value()
        stream: StreamInfo | None = None
        nxt = run.peek()
        if value is None and not run.flags.stopped:
            run.flags.add(FlagReason.MISSING_VALUE, start, run.last_end)
        if nxt is not None and run.is_keyword(nxt, b"stream"):
            if not isinstance(value, PdfDict):
                run.flags.add(FlagReason.UNEXPECTED_TOKEN, nxt.start, nxt.end)
            stream = run.stream(value if isinstance(value, PdfDict) else None)
        complete = stream is None or stream.terminated
        if complete:
            complete = run.finish_object()
        return IndirectObject(num, gen, Span(start, run.last_end), value, stream,
                              tuple(run.strays), run.flags.result(), complete)

    def _endstream_after(self, pos: int, end: int) -> int | None:
        """The first ``endstream`` keyword starting in [pos, end). Every
        occurrence is found once per parser, so repeated scans (many
        streams without one) stay linear."""
        if self._endstreams is None:
            self._endstreams = [m.start() for m in _ENDSTREAM.finditer(self.data)]
        i = bisect_left(self._endstreams, pos)
        if i < len(self._endstreams) and self._endstreams[i] < end:
            found = self._endstreams[i]
            # The keyword must end within the range too.
            return found if found + 9 <= end else None
        return None


class _Flags:
    """An object's flags, capped: past the cap they are only counted."""

    def __init__(self, cap: int) -> None:
        self.cap = max(cap, 0)
        self.kept: list[Flag] = []
        self.dropped = 0
        self.stopped = False  # a limit ended the parse

    def add(self, reason: FlagReason, start: int, end: int,
            params: tuple[tuple[str, int], ...] = ()) -> None:
        self.extend((Flag(reason, Span(start, max(start, end)), params),))

    def extend(self, flags: tuple[Flag, ...]) -> None:
        for flag in flags:
            if len(self.kept) < self.cap:
                self.kept.append(flag)
            else:
                self.dropped += 1

    def result(self) -> tuple[Flag, ...]:
        if self.dropped:
            return (*self.kept, Flag(FlagReason.FLAGS_TRUNCATED, None,
                                     (("dropped", self.dropped),)))
        return tuple(self.kept)


@dataclass
class _Frame:
    opener: TokenKind
    start: int
    items: list[PdfValue]
    entries: list[tuple[PdfName, PdfValue]]
    key: PdfName | None = None


class _Run:
    """One parse: a lexer with a small lookahead buffer, a token count
    and the flags and strays found so far."""

    def __init__(self, parser: ObjectParser, pos: int, end: int | None) -> None:
        self.parser = parser
        self.data = parser.data
        self.limits = parser.limits
        self.lexer = Lexer(parser.data, pos, end)
        self.end = self.lexer.end
        self.buffer: list[Token] = []
        self.tokens = 0
        self.last_end = self.lexer.pos
        self.flags = _Flags(self.limits.max_flags_per_object)
        self.strays: list[PdfValue] = []

    # ── tokens ────────────────────────────────────────────────────────
    def _fill(self, n: int) -> bool:
        """Lex until the buffer holds *n* tokens; False at the end of the
        range or once a limit stopped the parse. Comments are skipped but
        counted; lexer flags are collected as tokens are read."""
        while len(self.buffer) < n:
            if self.flags.stopped:
                return False
            token = self.lexer.next_token()
            if token is None:
                return False
            self.tokens += 1
            if self.tokens > self.limits.max_tokens_per_object:
                self.flags.add(FlagReason.TOKEN_LIMIT, token.start, token.end,
                               (("limit", self.limits.max_tokens_per_object),))
                self.flags.stopped = True
                return False
            self.flags.extend(token.flags)
            if token.kind is not TokenKind.COMMENT:
                self.buffer.append(token)
        return True

    def peek(self, i: int = 0) -> Token | None:
        return self.buffer[i] if self._fill(i + 1) else None

    def peek_start(self) -> int:
        token = self.peek()
        return token.start if token is not None else self.last_end

    def take(self) -> Token:
        token = self.buffer.pop(0)
        self.last_end = token.end
        return token

    def text(self, token: Token) -> bytes:
        return self.data[token.start:token.end]

    def is_keyword(self, token: Token, word: bytes) -> bool:
        return token.kind is TokenKind.KEYWORD and self.text(token) == word

    def _unsigned(self, token: Token | None) -> bool:
        return (token is not None and token.kind is TokenKind.INTEGER
                and self.text(token).isdigit()
                and len(token.span) <= self._max_digits())

    def at_stop(self) -> bool:
        """Is the next token one that ends a value: a stop keyword or the
        header of a following object?"""
        token = self.peek()
        if token is None:
            return True
        if token.kind is TokenKind.KEYWORD and self.text(token) in _STOP:
            return True
        return self._unsigned(token) and self._unsigned(self.peek(1)) and (
            (third := self.peek(2)) is not None and self.is_keyword(third, b"obj"))

    # ── values ────────────────────────────────────────────────────────
    def header(self) -> tuple[int, int, int] | None:
        first, second, third = self.peek(), self.peek(1), self.peek(2)
        if not (self._unsigned(first) and self._unsigned(second) and third is not None
                and self.is_keyword(third, b"obj")):
            return None
        assert first is not None and second is not None
        num, gen = int(self.text(first)), int(self.text(second))
        start = first.start
        for _ in range(3):
            self.take()
        return num, gen, start

    def value(self) -> PdfValue | None:
        """One value, iteratively. Returns None without consuming when the
        next token cannot start one (a stop, a closer, the end)."""
        stack: list[_Frame] = []
        while True:
            if self.at_stop():
                if not stack:
                    return None
                return self._close_all(stack)
            token = self.peek()
            assert token is not None
            kind = token.kind
            if kind in _CLOSERS:
                if not stack:
                    return None
                self.take()
                if stack[-1].opener is not _CLOSERS[kind]:
                    self._stray(PdfKeyword(token.start, token.end, self.text(token)))
                    continue
                done = self._build(stack.pop(), token.end)
            elif kind in _OPENERS:
                if len(stack) >= self.limits.max_container_depth:
                    self.flags.add(FlagReason.NESTING_LIMIT, token.start, token.end,
                                   (("limit", self.limits.max_container_depth),))
                    self.flags.stopped = True
                    return self._close_all(stack) if stack else None
                self.take()
                stack.append(_Frame(kind, token.start, [], []))
                continue
            else:
                scalar = self._scalar()
                if scalar is None:
                    if not stack:
                        return None
                    bad = self.take()
                    self._stray(PdfKeyword(bad.start, bad.end, self.text(bad)))
                    continue
                done = scalar
            if not stack:
                return done
            self._attach(stack[-1], done)

    def _scalar(self) -> PdfValue | None:
        """Consume and return one scalar value, or None (consuming
        nothing) when the next token is not one."""
        token = self.peek()
        assert token is not None
        kind, raw = token.kind, self.text(token)
        if kind is TokenKind.KEYWORD and raw not in (b"true", b"false", b"null"):
            return None
        if kind not in _SCALARS:
            return None
        if kind is TokenKind.INTEGER:
            second, third = self.peek(1), self.peek(2)
            if (self._unsigned(token) and self._unsigned(second) and third is not None
                    and self.is_keyword(third, b"R")):
                assert second is not None
                for _ in range(3):
                    self.take()
                return PdfRef(token.start, third.end, int(raw), int(self.text(second)))
            self.take()
            return PdfInt(token.start, token.end, int(raw) if self._fits(token) else None)
        self.take()
        if kind is TokenKind.REAL:
            return PdfReal(token.start, token.end, float(raw) if self._fits(token) else None)
        if kind is TokenKind.NAME:
            return PdfName(token.start, token.end, name_bytes(raw[1:]))
        if kind is TokenKind.LITERAL_STRING:
            body = raw[1:-1] if token.complete else raw[1:]
            return PdfString(token.start, token.end, literal_bytes(body), False)
        if kind is TokenKind.HEX_STRING:
            body = raw[1:-1] if token.complete else raw[1:]
            return PdfString(token.start, token.end, hex_bytes(body), True)
        if raw == b"null":
            return PdfNull(token.start, token.end)
        return PdfBool(token.start, token.end, raw == b"true")

    def _fits(self, token: Token) -> bool:
        """Is a number token short enough to convert? (int() refuses
        past 4,300 digits; no real PDF number comes close to the cap.)"""
        if len(token.span) > self._max_digits():
            self.flags.add(FlagReason.NUMBER_OUT_OF_RANGE, token.start, token.end,
                           (("digits", len(token.span)),))
            return False
        return True

    def _max_digits(self) -> int:
        return min(self.limits.max_number_digits, _INT_DIGITS_CEILING)

    def _stray(self, value: PdfValue) -> None:
        self.flags.add(FlagReason.UNEXPECTED_TOKEN, value.start, value.end)
        self.strays.append(value)

    def _attach(self, frame: _Frame, value: PdfValue) -> None:
        if frame.opener is TokenKind.ARRAY_OPEN:
            frame.items.append(value)
        elif frame.key is None:
            if isinstance(value, PdfName):
                frame.key = value
            else:
                self._stray(value)  # a dictionary key must be a name
        else:
            frame.entries.append((frame.key, value))
            frame.key = None

    def _build(self, frame: _Frame, end: int) -> PdfValue:
        if frame.opener is TokenKind.ARRAY_OPEN:
            return PdfArray(frame.start, end, tuple(frame.items))
        if frame.key is not None:
            self.flags.add(FlagReason.MISSING_VALUE, frame.key.start, frame.key.end)
            self.strays.append(frame.key)
        return self._dict(frame.start, end, frame.entries)

    def _dict(self, start: int, end: int,
              entries: list[tuple[PdfName, PdfValue]]) -> PdfDict:
        # Each key's first value is tokenized at most once, however often
        # the key repeats: linear in the dictionary's size.
        first: dict[bytes, PdfValue] = {}
        first_tokens: dict[bytes, tuple[tuple[TokenKind, bytes], ...]] = {}
        ambiguous: set[bytes] = set()
        for key, value in entries:
            if key.raw not in first:
                first[key.raw] = value
                continue
            if key.raw not in first_tokens:
                first_tokens[key.raw] = self._tokens(first[key.raw])
            same = first_tokens[key.raw] == self._tokens(value)
            self.flags.add(FlagReason.DUPLICATE_KEY, key.start, value.end,
                           (("identical", int(same)),))
            if not same:
                ambiguous.add(key.raw)
        return PdfDict(start, end, tuple(entries), frozenset(ambiguous))

    def _tokens(self, value: PdfValue) -> tuple[tuple[TokenKind, bytes], ...]:
        """A value's text as tokens, comments and whitespace aside: what
        ADR 0002's "textually identical" compares."""
        return tuple((t.kind, self.data[t.start:t.end])
                     for t in Lexer(self.data, value.start, value.end)
                     if t.kind is not TokenKind.COMMENT)

    def _close_all(self, stack: list[_Frame]) -> PdfValue:
        """Close every open container at the last consumed byte,
        unterminated (flagged once, from the outermost opener)."""
        self.flags.add(FlagReason.UNTERMINATED, stack[0].start, self.last_end)
        done: PdfValue | None = None
        while stack:
            frame = stack.pop()
            if done is not None:
                self._attach(frame, done)
            done = self._build(frame, self.last_end)
        assert done is not None
        return done

    # ── objects ───────────────────────────────────────────────────────
    def stream(self, header: PdfDict | None) -> StreamInfo:
        """Locate a stream's data after the `stream` keyword, then leave
        the lexer just past `endstream`."""
        keyword = self.take()
        data, end = self.data, self.end
        start = keyword.end
        if data.startswith(b"\r\n", start):
            start += 2
        elif data[start:start + 1] == b"\n":
            start += 1
        elif data[start:start + 1] == b"\r":
            # §7.3.8.1 allows CRLF or LF only. MuPDF skips a lone CR; qpdf
            # warns. Skipped, and flagged.
            start += 1
            self.flags.add(FlagReason.STREAM_EOL, keyword.start, keyword.end + 1)
        else:
            self.flags.add(FlagReason.STREAM_EOL, keyword.start, keyword.end)
        start = min(start, end)
        declared = self._length(header)
        found = self.parser._endstream_after
        slack: Span | None = None
        stop: int | None = None  # where `endstream` begins
        data_end = start
        if declared is not None and start + declared <= end:
            after = start + declared
            ws = _WS_RUN.match(data, after, end)
            gap_end = ws.end() if ws else after
            inside = found(start, end)
            if data.startswith(b"endstream", gap_end) and found(gap_end, end) == gap_end:
                stop, data_end = gap_end, after
                if data[after:gap_end] not in _ONE_EOL:
                    # More than one end-of-line: MuPDF reads these bytes as
                    # data (NULs and spaces are valid samples; an image can
                    # draw text with them), qpdf does not. Flag, never guess.
                    slack = Span(after, gap_end)
                    self.flags.add(FlagReason.STREAM_SLACK, after, gap_end)
            elif inside is not None and inside < after:
                pass  # /Length runs past an endstream: the scan below decides
            elif (later := found(after, end)) is not None:
                stop, data_end = later, after
                slack = Span(after, later)
                self.flags.add(FlagReason.STREAM_SLACK, after, later)
        if stop is None:
            stop = found(start, end)
            if stop is None:
                self.flags.add(FlagReason.UNTERMINATED, keyword.start, end)
                self.last_end = end
                self.flags.stopped = True
                return StreamInfo(Span(start, end), None, declared, False)
            data_end = _before_eol(data, start, stop)
            self.flags.add(FlagReason.LENGTH_MISMATCH, start, stop, (
                ("declared", -1 if declared is None else declared),
                ("found", data_end - start)))
        if data.startswith(b"endobj", stop + len(b"endstream")):
            self.flags.add(FlagReason.ENDSTREAM_JOINED, stop, stop + len(b"endstreamendobj"))
        self.buffer.clear()
        self.lexer.seek(stop + len(b"endstream"))
        self.last_end = stop + len(b"endstream")
        return StreamInfo(Span(start, data_end), slack, declared, True)

    def _length(self, header: PdfDict | None) -> int | None:
        value = header.get(b"Length") if header is not None else None
        length: int | None = None
        if isinstance(value, PdfInt):
            length = value.value
        elif isinstance(value, PdfRef) and self.parser.resolve_length is not None:
            try:
                length = self.parser.resolve_length(value.num, value.gen)
            except Exception:  # never raise on input: unresolved, so scanned
                length = None
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            return None
        return length

    def finish_object(self) -> bool:
        """After the value (and stream): `endobj`, else extra tokens up to
        it (flagged) or a missing `endobj` (flagged). True when `endobj`
        was found."""
        extra_start: int | None = None
        while True:
            token = self.peek()
            if token is not None and self.is_keyword(token, b"endobj"):
                self._extras(extra_start)
                self.take()
                return True
            if token is None or (self.at_stop() and not self.is_keyword(token, b"endstream")):
                self._extras(extra_start)
                self.flags.add(FlagReason.MISSING_ENDOBJ, self.last_end, self.last_end)
                return False
            if extra_start is None:
                extra_start = token.start
            value = self.value()
            if value is None:
                bad = self.take()
                self.strays.append(PdfKeyword(bad.start, bad.end, self.text(bad)))
            else:
                self.strays.append(value)

    def _extras(self, start: int | None) -> None:
        if start is not None:
            self.flags.add(FlagReason.EXTRA_TOKENS, start, self.last_end)


def _before_eol(data: bytes, start: int, stop: int) -> int:
    """Where stream data ends when found by scanning: before the one
    end-of-line that precedes `endstream`, if any (§7.3.8.1)."""
    if stop - 2 >= start and data[stop - 2:stop] == b"\r\n":
        return stop - 2
    if stop - 1 >= start and data[stop - 1:stop] in (b"\n", b"\r"):
        return stop - 1
    return stop
