"""``build_inventory``: every byte of a file attributed to a unit, or
flagged (docs/REDESIGN.md §4, "Inventory" and "Byte tiling"; 3a-5).

The claims come from the canonical xref chain (xref.py), never from a
scan of the whole file: the header line and its binary-marker comment;
each classic table section; every revision's ``startxref N %%EOF``;
every distinct in-use offset of every revision (a cross-reference
stream is an object like any other), parsed once and bounded by the
next known start, so an unterminated string cannot run on into the rest
of the file from every offset (quadratic). Stream slack is a unit nested
in its object. Only the gaps left over are scanned, once and linearly,
for ``N G obj`` bodies no xref reaches (DEAD_BODY): a classic place for
leftover content to hide. Every in-use object stream is decoded (Flate
only, 3a-4a), its header table and members tiled in its own
coordinates, and every compressed entry of every revision must find its
member there.

Readers are the authority (ADR 0010): whatever this cannot attribute
exactly is a flag -- an unclaimed non-whitespace byte, a byte two units
claim, an object that reads differently in two revisions it lives in, a
compressed entry its home stream does not list. Never raises on input
bytes; linear, and charged to the file's work budget.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from ..budget import Budget, Limits
from ..ledger import Flag, FlagReason, Span, Unit, UnitKind, UnitRef
from .flate import flate_decode
from .lexer import DELIMITERS, WHITESPACE, Lexer, TokenKind
from .objects import IndirectObject, ObjectParser, PdfArray, PdfDict, PdfInt, PdfNull, PdfRef
from .tiling import check_tiling, tile
from .types import Inventory, ObjectStream, Region
from .xref import COMPRESSED, IN_USE, Chain, Entry, _int, _name, _predictor, read_chain

_WS: Final = rb"[\x00\t\n\x0c\r ]"
_EOL: Final = rb"(?:\r\n|\r|\n)"
# The header line (ISO 32000-1 §7.5.2), then the comment lines before the
# first object: the binary marker and a writer's own note ("% Written by
# ..."), which some writers repeat after each update's previous %%EOF. Readers skip comments; tests/test_inventory_build.py's allowlist
# test shows both read such a file alike. Comments anywhere else are left
# unclaimed, so flagged.
_HEADER: Final = re.compile(rb"%PDF-[0-9]\.[0-9](?=\r\n|\r|\n)")
_PREAMBLE_COMMENT: Final = re.compile(_WS + rb"+(%[^\r\n]*)(?=\r\n|\r|\n)")
# A revision's epilogue (§7.5.5), directly after its last section.
_EPILOGUE: Final = re.compile(_WS + rb"*(startxref" + _EOL + rb"(\d{1,20})" + _EOL + rb"%%EOF)")
# `obj` as a keyword after whitespace: where an object header may end. A
# header is then looked for in a short window before it, so a long digit
# or whitespace run is never rescanned from every position (quadratic).
_OBJ_KEYWORD: Final = re.compile(rb"(?<=[\x00\t\n\x0c\r ])obj(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])")
_DEAD_HEADER: Final = re.compile(
    rb"(?<![^\x00\t\n\x0c\r ()<>\[\]{}/%])[0-9]{1,20}" + _WS + rb"+[0-9]{1,10}" + _WS + rb"+obj")
_LOOKBACK: Final = 64
# After a member's value: `stream` would make it a stream (§7.5.7).
_STREAM_KEYWORD: Final = re.compile(_WS + rb"*stream(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])")
# Work charged per compressed entry re-checked against a replaced home.
_CHECK_WORK: Final = 64


@dataclass
class _Member:
    ref: UnitRef
    number: int
    catalog: bool  # a clean dictionary with /Type /Catalog


def build_inventory(data: bytes, limits: Limits | None = None, *,
                    budget: Budget) -> Inventory:
    """The inventory of *data*. *budget* is required: every parse and
    scan here is charged to it, so a superlinear path is flagged
    BUDGET_EXHAUSTED instead of hanging (ADR 0010). Never raises."""
    return _Builder(data, limits if limits is not None else Limits(), budget).build()


class _Flags:
    """The file's flags: distinct, in the order found, at most
    ``max_flags_per_file`` (the rest counted in one FLAGS_TRUNCATED)."""

    def __init__(self, cap: int) -> None:
        self.cap = max(cap, 0)
        self.seen: set[Flag] = set()
        self.kept: list[Flag] = []
        self.dropped = 0

    def extend(self, flags: Iterable[Flag]) -> None:
        for flag in flags:
            if flag in self.seen:
                continue
            self.seen.add(flag)
            if len(self.kept) < self.cap:
                self.kept.append(flag)
            else:
                self.dropped += 1

    def add(self, reason: FlagReason, span: Span | None = None,
            params: tuple[tuple[str, int], ...] = ()) -> None:
        self.extend((Flag(reason, span, params),))

    def result(self) -> tuple[Flag, ...]:
        if self.dropped:
            return (*self.kept, Flag(FlagReason.FLAGS_TRUNCATED, None, (
                ("dropped", self.dropped), ("limit", self.cap))))
        return tuple(self.kept)


class _Builder:
    def __init__(self, data: bytes, limits: Limits, budget: Budget) -> None:
        self.data, self.limits, self.budget = data, limits, budget
        self.flags = _Flags(limits.max_flags_per_file)
        self.claims: list[tuple[UnitRef, Span]] = []
        self.units: list[Unit] = []
        self.stream_data: dict[int, Span] = {}
        self.parser = ObjectParser(data, limits, self._resolve_length, budget)
        # Per object number, its entry at each revision where it changes,
        # oldest first; and per revision, the numbers it changes.
        self.history: dict[int, list[tuple[int, Entry]]] = {}
        self.changes: list[list[int]] = []
        # Each in-use offset: the revision ranges (lo, hi) it lives in.
        self.ranges: dict[int, list[tuple[int, int]]] = {}
        self.parsed: dict[int, IndirectObject | None] = {}
        self.objects: dict[int, UnitRef] = {}
        self.lengths: dict[int, int | None] = {}  # a /Length target's answer
        self.current: int | None = None  # the offset being parsed
        self.resolving = False
        self.starts: list[int] = []
        self.members: dict[int, list[_Member | None]] = {}  # object-stream offset
        self.streams: dict[int, ObjectStream] = {}

    # ── the whole ─────────────────────────────────────────────────────
    def build(self) -> Inventory:
        data = self.data
        chain = read_chain(data, self.limits, budget=self.budget)
        self.flags.extend(chain.flags)
        self._history(chain)
        self._header()
        epilogues = self._epilogues(chain)
        sections = [s for section in chain.sections
                    for s in (section, section.xref_stm) if s is not None]
        for section in sections:
            if not section.is_stream:
                self._claim(UnitRef(UnitKind.XREF_TABLE, section.offset), section.span)
        offsets = set(self.ranges) | {s.offset for s in sections if s.is_stream}
        self.starts = sorted(offsets | {s.offset for s in sections} | set(epilogues))
        for offset in sorted(offsets):
            self._object(offset)
        for offset in sorted(self.objects):
            self._object_stream(offset)
        self._compressed(chain)
        tiling, _ = tile(data, self.claims, self.limits)
        self._dead_bodies(tiling.regions)
        tiling, tile_flags = tile(data, self.claims, self.limits)
        self.flags.extend(tile_flags)
        self.flags.extend(self.budget.flags())
        tiles = check_tiling(len(data), (r.span for r in tiling.regions)) and all(
            check_tiling(s.tiling.size, (r.span for r in s.tiling.regions))
            for s in self.streams.values())
        entries = {n: tuple((r, e.kind, e.a, e.b) for r, e in h)
                   for n, h in self.history.items()}
        return Inventory(len(data), tiling, tiles,
                         tuple(sorted(self.units, key=lambda u: u.ref.sort_key())),
                         self.flags.result(), len(chain.revisions), self.stream_data,
                         self.objects, self.streams, entries)

    def _claim(self, ref: UnitRef, span: Span, flags: tuple[Flag, ...] = ()) -> None:
        self.claims.append((ref, span))
        self.units.append(Unit(ref, (span,), flags))

    # ── the chain, revision by revision ───────────────────────────────
    def _history(self, chain: Chain) -> None:
        """Each object number's entry history, oldest revision first
        (xref.Chain.object_map's merge order, built once: linear however
        many revisions), and the revision ranges each in-use offset lives
        in."""
        count = len(chain.revisions)
        self.changes = [[] for _ in range(count)]
        for revision in reversed(range(count)):
            for index in sorted(chain.revisions[revision], reverse=True):
                section = chain.sections[index]
                parts = (section.xref_stm, section) if section.xref_stm else (section,)
                for part in parts:
                    for number, entry in part.entries.items():
                        history = self.history.setdefault(number, [])
                        if history and history[-1][0] == revision:
                            history[-1] = (revision, entry)
                        elif not history or history[-1][1] != entry:
                            history.append((revision, entry))
                            self.changes[revision].append(number)
        for history in self.history.values():
            for i, (revision, entry) in enumerate(history):
                if entry.kind == IN_USE:
                    lo = history[i + 1][0] + 1 if i + 1 < len(history) else 0
                    self.ranges.setdefault(entry.a, []).append((lo, revision))

    def _entry(self, number: int, revision: int) -> Entry | None:
        history = self.history.get(number)
        if not history:
            return None
        i = bisect_right(history, -revision, key=lambda change: -change[0]) - 1
        return history[i][1] if i >= 0 else None

    def _header(self) -> None:
        """The header line, wherever read_chain found it (a late one is
        flagged HEADER_OFFSET there), and the comment lines after it;
        anything else on the header line is left unclaimed, so flagged."""
        found = self.data.find(b"%PDF-", 0, 1024)
        header = _HEADER.match(self.data, found) if found >= 0 else None
        if header is None:
            return
        self._claim_with_comments(UnitRef(UnitKind.HEADER, header.start()),
                                  Span(header.start(), header.end()))

    def _claim_with_comments(self, ref: UnitRef, span: Span) -> None:
        """Claim *span* and the comment lines right after it (the header's,
        or an update's after the previous ``%%EOF``) as one unit."""
        spans = [span]
        while (comment := _PREAMBLE_COMMENT.match(self.data, spans[-1].end)) is not None:
            if not self.budget.charge_work(comment.end() - spans[-1].end):
                break
            spans.append(Span(comment.start(1), comment.end(1)))
        self.claims += [(ref, each) for each in spans]
        self.units.append(Unit(ref, tuple(spans)))

    def _epilogues(self, chain: Chain) -> dict[int, int]:
        """``startxref N %%EOF`` right after each section; every revision
        must end with one naming it (the last is the chain's own tail).
        Any other one found there (a linearized file's first-page section
        is followed by ``startxref 0``) must name 0 or a section. Returns
        each epilogue's start and the offset it names."""
        data = self.data
        found: dict[int, tuple[int, int]] = {}  # section end -> (start, named)
        ends = {s.span.end for section in chain.sections
                for s in (section, section.xref_stm) if s is not None}
        tail = chain.tail.start if chain.tail is not None else -1
        for end in sorted(ends):
            match = _EPILOGUE.match(data, end)
            if match is None or not self.budget.charge_work(match.end() - end + 1):
                continue
            start = match.start(1)
            if start != tail:  # the tail is claimed below, its end-of-line included
                self._claim_with_comments(UnitRef(UnitKind.XREF_EPILOGUE, start),
                                          Span(start, match.end(1)))
            found[end] = (start, int(match.group(2)))
        required = {chain.revision_end(r): chain.revision_start(r)
                    for r in range(len(chain.revisions))}
        offsets = {0} | {s.offset for s in chain.sections}
        for end, (start, named) in found.items():
            if end not in required and named not in offsets:
                self.flags.add(FlagReason.XREF_EPILOGUE_MISMATCH, Span(start, start),
                               (("revision", -1), ("named", named)))
        for revision in range(1, len(chain.revisions)):
            end = chain.revision_end(revision)
            if found.get(end, (0, -1))[1] != required[end]:
                self.flags.add(FlagReason.XREF_EPILOGUE_MISMATCH, Span(end, end),
                               (("revision", revision), ("named", found.get(end, (0, -1))[1])))
        if chain.tail is not None:  # the chain checked what the tail names
            self._claim(UnitRef(UnitKind.XREF_EPILOGUE, chain.tail.start), chain.tail)
        return {start: named for start, named in found.values()} | (
            {chain.tail.start: chain.startxref or 0} if chain.tail is not None else {})

    # ── objects at xref offsets ───────────────────────────────────────
    def _bound(self, offset: int) -> int:
        """The next known start after *offset*: where its parse must end."""
        i = bisect_right(self.starts, offset)
        return self.starts[i] if i < len(self.starts) else len(self.data)

    def _object(self, offset: int) -> None:
        if not 0 <= offset < len(self.data):
            self.flags.add(FlagReason.XREF_OFFSET_MISMATCH, None, (("offset", offset),))
            return
        obj = self.parsed.get(offset)
        if offset not in self.parsed:
            self.current = offset
            obj = self.parser.parse_indirect_at(offset, self._bound(offset))
            self.current = None
            self.parsed[offset] = obj
        if obj is None:  # not N G obj here, or the budget is spent
            self.flags.add(FlagReason.XREF_OFFSET_MISMATCH, None, (("offset", offset),))
            return
        ref = UnitRef(UnitKind.OBJECT, obj.span.start, obj=obj.num, gen=obj.gen)
        self.objects[offset] = ref
        self._body(ref, obj)

    def _body(self, ref: UnitRef, obj: IndirectObject) -> None:
        self.flags.extend(obj.flags)
        self._claim(ref, obj.span, obj.flags)
        if obj.stream is not None:
            self.stream_data[ref.start] = obj.stream.data
            if obj.stream.slack is not None:
                slack = obj.stream.slack
                self.units.append(Unit(UnitRef(UnitKind.STREAM_SLACK, slack.start, within=ref),
                                       (slack,)))

    def _resolve_length(self, number: int, gen: int) -> int | None:
        """An indirect /Length, read in every revision the object being
        parsed lives in: one entry for *number* across all of them, or
        flagged REVISION_AMBIGUOUS and unresolved. A /Length inside an
        object stream is not resolved (unresolved: the stream is found by
        scanning, and flagged LENGTH_MISMATCH). Nested lookups (a /Length
        target's own /Length) answer None, so resolution never recurses."""
        offset = self.current
        if self.resolving or offset is None:
            return None
        history = self.history.get(number, [])
        seen: set[Entry | None] = set()
        changes = False
        for lo, hi in self.ranges.get(offset, ()):
            newest = bisect_right(history, -lo, key=lambda change: -change[0]) - 1
            oldest = bisect_right(history, -hi, key=lambda change: -change[0]) - 1
            changes = changes or newest != oldest  # it changes while the object lives
            seen.add(history[oldest][1] if oldest >= 0 else None)
        if changes or len(seen) > 1:
            self.flags.add(FlagReason.REVISION_AMBIGUOUS, None,
                           (("offset", offset), ("length_object", number)))
            return None
        entry = next(iter(seen), None)
        if entry is None or entry.kind != IN_USE or entry.b != gen:
            return None
        if entry.a not in self.lengths:
            self.resolving = True
            target = self.parsed.get(entry.a)
            if entry.a not in self.parsed and 0 <= entry.a < len(self.data):
                target = self.parser.parse_indirect_at(entry.a, self._bound(entry.a))
                if target is None or target.stream is None:
                    # No /Length of its own was needed: the same parse the
                    # main loop would make. A stream is parsed there again.
                    self.parsed[entry.a] = target
            self.resolving = False
            clean = (target is not None and not target.flags and target.complete
                     and target.span.start == entry.a and target.num == number
                     and isinstance(target.value, PdfInt) and target.value.value is not None)
            self.lengths[entry.a] = (target.value.value if clean and target is not None
                                     and isinstance(target.value, PdfInt) else None)
        return self.lengths[entry.a]

    # ── object streams ────────────────────────────────────────────────
    def _object_stream(self, offset: int) -> None:
        obj, ref = self.parsed.get(offset), self.objects[offset]
        if (obj is None or obj.stream is None or not isinstance(obj.value, PdfDict)
                or _name(obj.value.get(b"Type")) != b"ObjStm"):
            return
        header, where, number = obj.value, obj.stream.data, obj.num
        raw = self.data[where.start:where.end]
        filters = header.get(b"Filter")
        if filters is not None:
            if _name(filters) != b"FlateDecode" and not (
                    isinstance(filters, PdfArray) and len(filters.items) == 1
                    and _name(filters.items[0]) == b"FlateDecode"):
                self.flags.add(FlagReason.UNSUPPORTED_FILTER, where, (("object", number),))
                return
            predictor = _predictor(header.get(b"DecodeParms"))
            if predictor is None:
                self.flags.add(FlagReason.BAD_DECODE_PARMS, where, (("object", number),))
                return
            decoded = flate_decode(raw, predictor, self.budget, where)
            self.flags.extend(decoded.flags)
            raw = decoded.data
        elif header.get(b"DecodeParms") is not None:
            self.flags.add(FlagReason.BAD_DECODE_PARMS, where, (("object", number),))
        members, claims = self._read_object_stream(raw, header, ref, where, number)
        tiling, flags = tile(raw, claims, self.limits, within=ref)
        self.flags.extend(_nested(flag, where, number) for flag in flags)
        self.members[offset] = members
        self.streams[offset] = ObjectStream(ref, tiling,
                                            tuple(m.ref if m else None for m in members))

    def _read_object_stream(self, raw: bytes, header: PdfDict, ref: UnitRef, where: Span,
                            number: int) -> tuple[list[_Member | None],
                                                  list[tuple[UnitRef, Span]]]:
        """The header's N offset pairs, then each member, bounded by the
        next member's offset. Anything off the canonical shape is flagged:
        /N or /First missing or out of range, the header not exactly 2N
        unsigned integers, a repeated number, offsets not increasing."""
        claims: list[tuple[UnitRef, Span]] = []
        count, first = _int(header.get(b"N")), _int(header.get(b"First"))

        def malformed(pair: int) -> tuple[list[_Member | None], list[tuple[UnitRef, Span]]]:
            self.flags.add(FlagReason.OBJSTM_MALFORMED, where,
                           (("object", number), ("pair", pair)))
            return [], claims
        if count is None or first is None or first > len(raw):
            return malformed(-1)
        numbers: list[int] = []
        lexer = Lexer(raw, 0, first)
        table_start, table_end = first, 0
        while (token := lexer.next_token()) is not None:
            text = raw[token.start:token.end]
            if (not self.budget.charge_work(len(text) + 1) or token.kind is not TokenKind.INTEGER
                    or not text.isdigit() or len(text) > self.limits.max_number_digits
                    or len(numbers) >= 2 * count):
                return malformed(len(numbers) // 2)
            numbers.append(int(text))
            table_start, table_end = min(table_start, token.start), token.end
        if len(numbers) != 2 * count or _glued(raw, first):
            return malformed(len(numbers) // 2)
        if numbers:
            claims.append((UnitRef(UnitKind.OBJSTM_HEADER, table_start, within=ref),
                           Span(table_start, table_end)))
            self.units.append(Unit(claims[-1][0], (claims[-1][1],)))
        pairs = list(zip(numbers[::2], numbers[1::2]))
        seen: set[int] = set()
        for i, (member, at) in enumerate(pairs):
            previous = pairs[i - 1][1] if i else -1
            if member in seen or at <= previous or first + at > len(raw):
                return malformed(i)
            seen.add(member)
        parser = ObjectParser(raw, self.limits, budget=self.budget)
        members: list[_Member | None] = []
        for i, (member, at) in enumerate(pairs):
            end = first + pairs[i + 1][1] if i + 1 < len(pairs) else len(raw)
            parsed = parser.parse_value_at(first + at, end)
            flags = tuple(_nested(flag, where, number) for flag in parsed.flags)
            self.flags.extend(flags)
            # Bytes after the value, up to the next member, are left
            # unclaimed: the decoded tiling flags any but whitespace.
            value = parsed.value
            # A member is canonical only as a value both readers read as
            # we do: MuPDF takes a null member for a missing object (and
            # repairs the whole file), both read a lone `N G R` as the
            # integer N, and a token running across the bound is read
            # whole by the readers but cut here.
            invalid = (value is None or isinstance(value, (PdfNull, PdfRef))
                       or _glued(raw, first + at) or _glued(raw, end)
                       or _STREAM_KEYWORD.match(raw, parsed.span.end, end) is not None
                       or (isinstance(value, PdfDict)
                           and _name(value.get(b"Type")) in (b"ObjStm", b"XRef")))
            if invalid:
                self.flags.add(FlagReason.OBJSTM_MEMBER_INVALID, where, (
                    ("object", member), ("stream", number), ("index", i)))
            if value is None:
                members.append(None)
                continue
            unit = UnitRef(UnitKind.OBJSTM_MEMBER, parsed.span.start, within=ref,
                           obj=member, gen=0)
            claims.append((unit, parsed.span))
            self.units.append(Unit(unit, (parsed.span,), flags))
            catalog = (not flags and not invalid and isinstance(value, PdfDict)
                       and _name(value.get(b"Type")) == b"Catalog")
            members.append(None if invalid else _Member(unit, member, catalog))
        return members, claims

    def _home(self, entry: Entry, revision: int) -> _Member | None:
        """The member a compressed *entry* names in *revision*: its home
        in use there, an object stream we read, listing it at the index."""
        home = self._entry(entry.a, revision)
        members = self.members.get(home.a) if home and home.kind == IN_USE else None
        return members[entry.b] if members and entry.b < len(members) else None

    def _compressed(self, chain: Chain) -> None:
        """Oldest revision first, with each object stream's live members
        kept as it goes: every compressed entry a revision sets must find
        its member, listed under its own number at its index, in the home
        of that revision -- and when a revision replaces a home, every
        member still living in it is checked again (charged to the work
        budget: members x replacements can be quadratic, so a hostile file
        exhausts the budget instead). Every revision's /Root, when
        compressed, must be a clean /Type /Catalog dictionary."""
        live: dict[int, set[int]] = {}
        current: dict[int, Entry] = {}
        for revision in reversed(range(len(self.changes))):
            check: set[int] = set()
            for number in self.changes[revision]:
                entry = self._entry(number, revision)
                before = current.get(number)
                if before is not None and before.kind == COMPRESSED:
                    live.get(before.a, set()).discard(number)
                if entry is None:
                    continue
                current[number] = entry
                if entry.kind == COMPRESSED:
                    live.setdefault(entry.a, set()).add(number)
                    check.add(number)
            # Homes replaced in this revision: their members, checked again.
            replaced = [live[number] for number in self.changes[revision] if live.get(number)]
            work = len(check) + _CHECK_WORK * sum(len(members) for members in replaced)
            if not self.budget.charge_work(work):
                check, replaced = set(), []  # unaffordable: the budget's flag stands
            for members in replaced:
                check |= members
            for number in sorted(check):
                entry = current[number]
                member = self._home(entry, revision) if number != 0 else None
                if member is None or member.number != number:
                    self.flags.add(FlagReason.OBJSTM_ENTRY_MISMATCH, None, (
                        ("object", number), ("stream", entry.a), ("index", entry.b),
                        ("revision", revision)))
            trailer = chain.sections[chain.revisions[revision][0]].trailer
            root = trailer.get(b"Root") if trailer is not None else None
            entry = self._entry(root.num, revision) if isinstance(root, PdfRef) else None
            if isinstance(root, PdfRef) and entry is not None and entry.kind == COMPRESSED:
                member = self._home(entry, revision)
                if (root.gen != 0 or member is None or member.number != root.num
                        or not member.catalog):
                    self.flags.add(FlagReason.MISSING_ROOT, None, (("revision", revision),))

    # ── the gaps ──────────────────────────────────────────────────────
    def _dead_bodies(self, regions: Iterable[Region]) -> None:
        """Scan each unclaimed non-whitespace gap, once, for ``N G obj``
        bodies no xref reaches, each parsed bounded by its gap; the scan
        resumes after each body, so it is linear in the gap."""
        data = self.data
        for region in regions:
            if region.kind is not UnitKind.UNINDEXED:
                continue
            start, end = region.span.start, region.span.end
            if not self.budget.charge_work(end - start):
                return
            pos = start
            for keyword in _OBJ_KEYWORD.finditer(data, start, end):
                if keyword.start() < pos:
                    continue
                window = max(pos, keyword.start() - _LOOKBACK)
                if not self.budget.charge_work(keyword.end() - window):
                    return
                head = None
                for candidate in _DEAD_HEADER.finditer(data, window, keyword.end()):
                    if candidate.end() == keyword.end():
                        head = candidate
                        break
                if head is None:
                    continue
                obj = self.parser.parse_indirect_at(head.start(), end)
                if obj is None or obj.span.start != head.start():
                    continue
                self._body(UnitRef(UnitKind.DEAD_BODY, obj.span.start, obj=obj.num,
                                   gen=obj.gen), obj)
                pos = obj.span.end


def _nested(flag: Flag, where: Span, stream: int) -> Flag:
    """A flag found in an object stream's decoded bytes, placed in the
    file: at the stream's raw data, with the decoded span as integers."""
    span = flag.span if flag.span is not None else Span(0, 0)
    return Flag(flag.reason, where, flag.params + (
        ("stream", stream), ("decoded_start", span.start), ("decoded_end", span.end)))


_SEPARATORS: Final = frozenset(WHITESPACE + DELIMITERS)
_OPENERS: Final = frozenset(b"/")


def _glued(raw: bytes, bound: int) -> bool:
    """Does a token run across *bound* (an object stream's /First or a
    member's offset)? We lex up to the bound; the readers lex the token
    whole (`12|3`, `/Foo|true`, `/|Foo`). A string, hex string, array or
    dictionary cut there is already flagged unterminated."""
    return (0 < bound < len(raw) and raw[bound] not in _SEPARATORS
            and (raw[bound - 1] not in _SEPARATORS or raw[bound - 1] in _OPENERS))
