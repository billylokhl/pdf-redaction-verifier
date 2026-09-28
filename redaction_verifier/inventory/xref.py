"""The cross-reference chain (ISO 32000-1 §7.5.4-7.5.8), canonical only
(ADR 0010, item 4).

``read_chain`` follows a file's cross-reference sections from the final
``startxref`` through every ``/Prev``, and each hybrid file's
``/XRefStm``, and reports each section's entries. It accepts one closed
set of shapes: the file ends with ``startxref`` N ``%%EOF``; each offset
lands exactly on an ``xref`` table or an ``/XRef`` stream object; a
table's subsection headers and 20-byte entries are exact; a stream's
/W, /Index and /Size agree with its decoded length and it decodes
canonically (3a-4a); every in-use entry lands exactly on its own
``N G obj``; /Prev has no cycle. Anything else is flagged, and the
whole file then cannot be certified -- no reader's repair logic is
emulated. Never raises.

Revisions: section *i* (newest first) and everything older is revision
*i*; its bytes end where the section ends (the prefix cut a reader of
that revision sees), and its object map is the sections merged, newer
entries winning (``Chain.object_map``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ..budget import Budget, Limits
from ..ledger import Flag, FlagReason, Span
from .flate import Predictor, flate_decode
from .lexer import Lexer, TokenKind
from .objects import ObjectParser, PdfArray, PdfDict, PdfInt, PdfName, PdfRef, PdfValue

FREE, IN_USE, COMPRESSED = 0, 1, 2

# The file's end: startxref, its offset, %%EOF, at most one end-of-line.
_TAIL: Final = re.compile(rb"startxref(?:\r\n|\r|\n)(\d{1,20})(?:\r\n|\r|\n)%%EOF(?:\r\n|\r|\n)?\Z")
_EOL: Final = rb"(?:\r\n|\r|\n)"
_TABLE_HEAD: Final = re.compile(rb"xref" + _EOL)
_SUBSECTION: Final = re.compile(rb"(\d{1,10}) (\d{1,10})" + _EOL)
# §7.5.4: exactly 20 bytes, the two-byte end-of-line included.
_ENTRY: Final = re.compile(rb"(\d{10}) (\d{5}) ([nf])(?: \r| \n|\r\n)")
_TRAILER: Final = re.compile(rb"[\x00\t\n\x0c\r ]*trailer[\x00\t\n\x0c\r ]*")


@dataclass(frozen=True)
class Entry:
    """A cross-reference entry. ``kind`` FREE (a: next free object,
    b: generation), IN_USE (a: byte offset, b: generation) or COMPRESSED
    (a: object stream number, b: index in it)."""

    kind: int
    a: int
    b: int


@dataclass(frozen=True)
class Section:
    """One cross-reference section. ``span`` is the table from ``xref``
    to the end of its trailer dictionary, or the whole /XRef stream
    object. ``entries`` maps object number to entry in the order they
    appear; a hybrid table's /XRefStm entries are its own ``stream``."""

    offset: int
    span: Span
    is_stream: bool
    entries: dict[int, Entry]
    trailer: PdfDict | None
    prev: int | None
    xref_stm: Section | None


@dataclass(frozen=True)
class Chain:
    """Every section, newest first; the final startxref; flags.
    ``revisions`` groups section indexes: normally one section per
    revision, but a linearized file's first-page section and the main
    section its /Prev points *forward* to are one revision (§F.3.4)."""

    sections: tuple[Section, ...]
    startxref: int | None
    tail: Span | None
    flags: tuple[Flag, ...]
    revisions: tuple[tuple[int, ...], ...] = ()

    def revision_end(self, revision: int) -> int:
        """Where *revision*'s bytes end: its prefix cut."""
        return max(self.sections[i].span.end for i in self.revisions[revision])

    def revision_start(self, revision: int) -> int:
        """The offset its own startxref names: its newest section."""
        return self.sections[self.revisions[revision][0]].offset

    def object_map(self, revision: int = 0) -> dict[int, Entry]:
        """The objects of *revision* (0 = current): its sections and every
        older one merged, newer entries winning, a hybrid table's own
        entries winning over its /XRefStm's."""
        first = self.revisions[revision][0] if self.revisions else 0
        merged: dict[int, Entry] = {}
        for section in reversed(self.sections[first:]):
            if section.xref_stm is not None:
                merged.update(section.xref_stm.entries)
            merged.update(section.entries)
        return merged


def read_chain(data: bytes, limits: Limits | None = None,
               budget: Budget | None = None) -> Chain:
    limits = limits if limits is not None else Limits()
    flags: list[Flag] = []
    if not data.startswith(b"%PDF-"):
        # Readers find a header after junk and read offsets relative to it;
        # we would read them absolute. Two readings of one file: flagged.
        found = data.find(b"%PDF-", 0, 1024)
        flags.append(Flag(FlagReason.HEADER_OFFSET, None, (("offset", found),)))
    tail = _TAIL.search(data, max(0, len(data) - 64))
    if tail is None:
        flags.append(Flag(FlagReason.XREF_TAIL, Span(max(0, len(data) - 64), len(data))))
        return Chain((), None, None, tuple(flags))
    start = int(tail.group(1))
    parser = ObjectParser(data, limits, budget=budget)
    sections: list[Section] = []
    seen: set[int] = set()
    offset: int | None = start
    while offset is not None:
        if offset in seen:
            flags.append(Flag(FlagReason.PREV_CYCLE, None, (("offset", offset),)))
            break
        if len(sections) >= limits.max_xref_sections:
            flags.append(Flag(FlagReason.PREV_CYCLE, None, (("limit", limits.max_xref_sections),)))
            break
        seen.add(offset)
        section = _section(data, offset, parser, budget, flags, allow_table=True)
        if section is None:
            break
        sections.append(section)
        offset = section.prev
    revisions: list[tuple[int, ...]] = []
    i = 0
    while i < len(sections):
        prev = sections[i].prev
        if prev is not None and prev > sections[i].offset:
            # A forward /Prev: canonical only as a linearized file's
            # first-page section, the newest, pointing at the main one.
            if i != 0 or i + 1 >= len(sections):
                flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, sections[i].span,
                                  (("object", -1),)))
                revisions.append((i,))
                i += 1
                continue
            revisions.append((i, i + 1))
            i += 2
        else:
            revisions.append((i,))
            i += 1
    chain = Chain(tuple(sections), start, Span(tail.start(), len(data)), tuple(flags),
                  tuple(revisions))
    return Chain(chain.sections, chain.startxref, chain.tail,
                 chain.flags + tuple(_check_revisions(data, chain, parser)), chain.revisions)


def _check_revisions(data: bytes, chain: Chain, parser: ObjectParser) -> list[Flag]:
    """Per revision, oldest first, with the object map built up as it
    goes (linear in entries, however many revisions):

    - every in-use entry lands exactly on its own ``N G obj``, and the
      whole object -- through ``endobj`` -- lies within the revision's
      own bytes (before its prefix cut): an object that straddles the
      cut (a stream swallowing the revision's own xref) reads truncated
      in one reader and recovered in another; so does a hybrid
      section's /XRefStm;
    - every compressed entry names an object stream in use in the
      revision; object 0 is never in use (MuPDF warns);
    - the revision's newest trailer names a /Type /Catalog via /Root
      (qpdf: "unable to find /Root dictionary"; a catalog inside an
      object stream is 3a-5's to check) and has /Size exactly one more
      than its highest object number (§7.5.5; qpdf warns otherwise)."""
    flags: list[Flag] = []
    merged: dict[int, Entry] = {}
    highest = 0
    catalogs: dict[int, bool] = {}
    whole: dict[int, int | None] = {}  # offset -> where its object ends (None: never)
    for revision in reversed(range(len(chain.revisions))):
        group = chain.revisions[revision]
        end = chain.revision_end(revision)
        parts: list[Section] = []
        for index in sorted(group, reverse=True):  # oldest section of the group first
            section = chain.sections[index]
            if section.xref_stm is not None:
                parts.append(section.xref_stm)
                if section.xref_stm.span.end > end:
                    flags.append(Flag(FlagReason.XREF_OFFSET_MISMATCH, section.xref_stm.span,
                                      (("revision", revision),)))
            parts.append(section)
        for part in parts:
            merged.update(part.entries)
            if part.entries:
                highest = max(highest, max(part.entries))
        for part in parts:
            for number, entry in part.entries.items():
                if entry.kind == IN_USE:
                    header = _header_at(data, entry.a)
                    if (number == 0 or header is None or header[:2] != (number, entry.b)
                            or not _ends_within(parser, entry.a, end, whole, flags)):
                        flags.append(Flag(FlagReason.XREF_OFFSET_MISMATCH, None, (
                            ("object", number), ("offset", entry.a))))
                elif entry.kind == COMPRESSED:
                    home = merged.get(entry.a)
                    if number == 0 or home is None or home.kind != IN_USE:
                        flags.append(Flag(FlagReason.XREF_OFFSET_MISMATCH, None, (
                            ("object", number), ("stream", entry.a))))
        newest = chain.sections[group[0]]
        trailer = newest.trailer
        root = trailer.get(b"Root") if trailer is not None else None
        home = merged.get(root.num) if isinstance(root, PdfRef) else None
        catalog = False
        if isinstance(root, PdfRef) and home is not None and home.kind == IN_USE:
            if home.a not in catalogs:
                obj = parser.parse_indirect_at(home.a, end)
                catalogs[home.a] = (obj is not None and not obj.flags
                                    and isinstance(obj.value, PdfDict)
                                    and _name(obj.value.get(b"Type")) == b"Catalog")
            catalog = catalogs[home.a] and home.b == root.gen
        elif isinstance(root, PdfRef) and home is not None and home.kind == COMPRESSED:
            catalog = True
        if not catalog:
            flags.append(Flag(FlagReason.MISSING_ROOT, newest.span, (("revision", revision),)))
        size = _int(trailer.get(b"Size")) if trailer is not None else None
        if size != highest + 1:
            flags.append(Flag(FlagReason.XREF_SIZE_MISMATCH, newest.span, (
                ("revision", revision), ("size", -1 if size is None else size),
                ("highest", highest))))
    return flags


def _section(data: bytes, offset: int, parser: ObjectParser, budget: Budget | None,
             flags: list[Flag], allow_table: bool) -> Section | None:
    if not 0 <= offset < len(data):
        flags.append(Flag(FlagReason.XREF_NOT_FOUND, None, (("offset", offset),)))
        return None
    if allow_table and _TABLE_HEAD.match(data, offset):
        return _table(data, offset, parser, budget, flags)
    return _stream(data, offset, parser, budget, flags)


def _table(data: bytes, offset: int, parser: ObjectParser, budget: Budget | None,
           flags: list[Flag]) -> Section | None:
    head = _TABLE_HEAD.match(data, offset)
    assert head is not None
    pos = head.end()
    entries: dict[int, Entry] = {}
    while (sub := _SUBSECTION.match(data, pos)) is not None:
        first, count = int(sub.group(1)), int(sub.group(2))
        pos = sub.end()
        if count == 0:  # MuPDF: "broken xref subsection"
            flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, Span(sub.start(), pos),
                              (("object", first),)))
            return None
        if budget is not None and not budget.charge_work(20 * count + 1):
            flags.append(Flag(FlagReason.BUDGET_EXHAUSTED, Span(offset, pos)))
            return None
        for number in range(first, first + count):
            entry = _ENTRY.match(data, pos)
            if entry is None:
                flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, Span(pos, pos),
                                  (("object", number),)))
                return None
            if number in entries:
                flags.append(Flag(FlagReason.XREF_CONFLICT, Span(pos, entry.end()),
                                  (("object", number),)))
            entries[number] = Entry(IN_USE if entry.group(3) == b"n" else FREE,
                                    int(entry.group(1)), int(entry.group(2)))
            pos = entry.end()
    trailer_kw = _TRAILER.match(data, pos)
    if trailer_kw is None:
        flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, Span(pos, pos), (("object", -1),)))
        return None
    parsed = parser.parse_value_at(trailer_kw.end())
    flags.extend(parsed.flags)
    if not isinstance(parsed.value, PdfDict) or parsed.strays:
        flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, parsed.span, (("object", -1),)))
        return None
    trailer = parsed.value
    if _int(trailer.get(b"Size")) is None:  # qpdf: "trailer dictionary lacks /Size key"
        flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, parsed.span, (("object", -1),)))
        return None
    xref_stm: Section | None = None
    stm = _int(trailer.get(b"XRefStm"))
    if stm is not None:
        xref_stm = _section(data, stm, parser, budget, flags, allow_table=False)
        if xref_stm is not None:
            for number, from_stream in xref_stm.entries.items():
                mine = entries.get(number)
                # Readers disagree on any overlap -- qpdf lets the stream's
                # entry through a free table entry, MuPDF does not -- so an
                # object both define is flagged, whatever the kinds.
                if mine is not None:
                    flags.append(Flag(FlagReason.XREF_CONFLICT, xref_stm.span,
                                      (("object", number),)))
    elif trailer.get(b"XRefStm") is not None:
        flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, parsed.span, (("object", -1),)))
    return Section(offset, Span(offset, parsed.span.end), False, entries, trailer,
                   _prev(trailer, parsed.span, flags), xref_stm)


def _stream(data: bytes, offset: int, parser: ObjectParser, budget: Budget | None,
            flags: list[Flag]) -> Section | None:
    obj = parser.parse_indirect_at(offset)
    if obj is None or obj.span.start != offset:
        flags.append(Flag(FlagReason.XREF_NOT_FOUND, None, (("offset", offset),)))
        return None
    flags.extend(obj.flags)
    header = obj.value
    bad = Flag(FlagReason.XREF_STREAM_MALFORMED, obj.span, (("object", obj.num),))
    if (not isinstance(header, PdfDict) or obj.stream is None or not obj.complete
            or obj.flags or _name(header.get(b"Type")) != b"XRef"):
        flags.append(bad)
        return None
    widths = _ints(header.get(b"W"))
    size = _int(header.get(b"Size"))
    index_value = header.get(b"Index")
    index = _ints(index_value) if index_value is not None else (
        [0, size] if size is not None else None)
    if (widths is None or len(widths) != 3 or any(w < 0 or w > 8 for w in widths)
            or widths[1] == 0 or size is None or index is None or len(index) % 2
            or any(n < 0 for n in index)):
        flags.append(bad)
        return None
    filters = header.get(b"Filter")
    if filters is not None and _name(filters) != b"FlateDecode" and not (
            isinstance(filters, PdfArray) and len(filters.items) == 1
            and _name(filters.items[0]) == b"FlateDecode"):
        flags.append(Flag(FlagReason.UNSUPPORTED_FILTER, obj.span, (("object", obj.num),)))
        return None
    raw = data[obj.stream.data.start:obj.stream.data.end]
    if filters is not None:
        predictor = _predictor(header.get(b"DecodeParms"))
        if predictor is None:
            flags.append(bad)
            return None
        decoded = flate_decode(raw, predictor, budget, obj.stream.data)
        flags.extend(decoded.flags)
        if not decoded.complete:
            return None
        raw = decoded.data
    width = sum(widths)
    total = sum(index[1::2])
    if len(raw) != width * total:
        flags.append(Flag(FlagReason.XREF_STREAM_MALFORMED, obj.span, (
            ("object", obj.num), ("expected", width * total), ("found", len(raw)))))
        return None
    entries: dict[int, Entry] = {}
    pos = 0
    for first, count in zip(index[::2], index[1::2]):
        for number in range(first, first + count):
            fields = []
            for w in widths:
                fields.append(int.from_bytes(raw[pos:pos + w], "big"))
                pos += w
            kind = fields[0] if widths[0] else IN_USE
            if kind not in (FREE, IN_USE, COMPRESSED) or number >= size:
                flags.append(Flag(FlagReason.XREF_STREAM_MALFORMED, obj.span, (
                    ("object", obj.num), ("entry", number))))
                return None
            if number in entries:
                flags.append(Flag(FlagReason.XREF_CONFLICT, obj.span, (("object", number),)))
            entries[number] = Entry(kind, fields[1], fields[2])
    return Section(offset, obj.span, True, entries, header,
                   _prev(header, obj.span, flags), None)


def _prev(trailer: PdfDict, span: Span, flags: list[Flag]) -> int | None:
    value = trailer.get(b"Prev")
    prev = _int(value)
    if value is not None and prev is None:
        flags.append(Flag(FlagReason.XREF_TABLE_MALFORMED, span, (("object", -1),)))
    return prev


def _ends_within(parser: ObjectParser, offset: int, end: int,
                 whole: dict[int, int | None], flags: list[Flag]) -> bool:
    """Does the object at *offset* parse complete, ``endobj`` included,
    before *end*? Each offset is parsed once per distinct outcome: the
    first (oldest, smallest) end it is checked against, and again only
    if that failed -- a failure is never loosened by reuse. The object's
    own parser flags join the chain's: an entry pointing at an object
    readers parse differently is not a clean entry. Budget-charged."""
    stop = whole.get(offset)
    if offset not in whole or stop is None:
        obj = parser.parse_indirect_at(offset, end)
        stop = obj.span.end if obj is not None and obj.complete else None
        if obj is not None and offset not in whole:
            flags.extend(obj.flags)
        whole[offset] = stop
    return stop is not None and stop <= end


def _header_at(data: bytes, offset: int) -> tuple[int, int, int] | None:
    """(num, gen, end of the header) when ``N G obj`` starts exactly at
    *offset*."""
    if not 0 <= offset < len(data):
        return None
    lexer = Lexer(data, offset, min(len(data), offset + 64))
    tokens = [lexer.next_token() for _ in range(3)]
    first, second, third = tokens
    if (first is None or second is None or third is None or first.start != offset
            or first.kind is not TokenKind.INTEGER or second.kind is not TokenKind.INTEGER
            or third.kind is not TokenKind.KEYWORD or data[third.start:third.end] != b"obj"):
        return None
    num, gen = data[first.start:first.end], data[second.start:second.end]
    if not (num.isdigit() and gen.isdigit()):
        return None
    return int(num), int(gen), third.end


def _int(value: PdfValue | None) -> int | None:
    if isinstance(value, PdfInt) and value.value is not None and value.value >= 0:
        return value.value
    return None


def _ints(value: PdfValue | None) -> list[int] | None:
    if not isinstance(value, PdfArray):
        return None
    out = [_int(item) for item in value.items]
    return None if any(v is None for v in out) else [v for v in out if v is not None]


def _name(value: PdfValue | None) -> bytes | None:
    return value.raw if isinstance(value, PdfName) else None


def _predictor(parms: PdfValue | None) -> Predictor | None:
    if parms is None:
        return Predictor()
    if isinstance(parms, PdfArray) and len(parms.items) == 1:
        parms = parms.items[0]
    if not isinstance(parms, PdfDict):
        return None
    values = {}
    for key, default in ((b"Predictor", 1), (b"Colors", 1), (b"BitsPerComponent", 8),
                         (b"Columns", 1)):
        raw = parms.get(key)
        value = default if raw is None else _int(raw)
        if value is None:
            return None
        values[key] = value
    return Predictor(values[b"Predictor"], values[b"Colors"], values[b"BitsPerComponent"],
                     values[b"Columns"])
