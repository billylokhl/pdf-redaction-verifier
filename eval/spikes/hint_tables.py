#!/usr/bin/env python3
"""Hint-table spike (owner decision 2026-10-15, option (a) staged, stage 1):
decode a linearized file's hint stream bit by bit against ISO 32000-1
Annex F and account for every value against the file's own layout.

The result per file is ``accounted`` (every bit of every table, every
padding bit, every byte of the decoded stream, every key of the hint
stream's and linearization dictionary's, is a value the layout fixes
under a stated reading) or the list of what is not: each finding names a
table, a field and a class, never a value's meaning. Nothing is
tolerated: a writer's deviation is accepted only as an explicit rule
(``RULES``) that fixes the value exactly, and each rule is bound to one
writer profile (qpdf's or Acrobat's), so a file must match one whole
profile and no value can take the other's reading. A rule is
"documented" (qpdf's source, libqpdf/QPDF_linearization.cc v12.4.1, or
the PDF Reference implementation notes 123-131 it cites) or "observed"
(an undocumented writer convention, derived on one half of the warned
files and measured on the other: ``half``); the report separates them.
Observed rules that read content (page 0's entries, later pages'
numerators and order, form identifier order) use the page's decoded
content streams and resources.

The layout is computed without reading the tables: the base revision
(the linearized pair, owner decision 7) is cut at its own end; object
offsets come from its cross-reference sections and are checked to start
``N G obj``; each object's extent runs to the end of the whitespace after
``endobj`` (qpdf's ``end_after_space``); compressed objects stand for
their object stream (qpdf's reading); pages, and which objects each page
and each document-level key reaches, come from the reference graph
(this repository's own parser, on the cut), walked as qpdf's
``updateObjectMaps`` walks it. pikepdf is used only to write the
fabricated fixtures.

Steps (aggregate output only; per-file records, keyed by SHA-256 prefix,
stay in a local ``--out``):

    PYTHONPATH=eval:. python eval/spikes/hint_tables.py run \\
        --root eval/scorecard/real_corpus/links --out <local dir>
    PYTHONPATH=eval:. python eval/spikes/hint_tables.py report --out <local dir>
    PYTHONPATH=eval:. python eval/spikes/hint_tables.py fixtures

``run`` decodes every unflagged hint-stream file of the 3a-12 records
(``--records``, the ``unit_budget.py`` output); ``report`` prints the
counts for the go/no-go population (warned, text-bearing, new), all hint
files and the stale-/L updated ones; ``fixtures`` builds fabricated
linearized files with qpdf, hides bits in them, and shows each is caught.
"""

from __future__ import annotations

import argparse
import io
import itertools
import json
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))

from redaction_verifier.budget import Budget  # noqa: E402
from redaction_verifier.inventory.flate import flate_decode  # noqa: E402
from redaction_verifier.inventory.objects import (  # noqa: E402
    ObjectParser,
    PdfArray,
    PdfDict,
    PdfInt,
    PdfName,
    PdfRef,
    PdfValue,
)
from redaction_verifier.inventory.xref import COMPRESSED, IN_USE, Chain, read_chain  # noqa: E402

_WS = b" \t\n\r\x0b\x0c"  # C isspace(): what qpdf skips after endobj


# ── findings ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Finding:
    """One value the layout does not account for. ``table`` and ``field``
    name it as Annex F does; ``klass`` groups findings for the report."""

    table: str
    field: str
    klass: str
    detail: str = ""


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    rules: Counter[str] = field(default_factory=Counter)
    tables: list[str] = field(default_factory=list)
    error: str | None = None
    profile: str | None = None

    def bad(self, table: str, name: str, klass: str, detail: str = "") -> None:
        self.findings.append(Finding(table, name, klass, detail))

    def rule(self, name: str) -> None:
        self.rules[name] += 1

    @property
    def accounted(self) -> bool:
        return self.error is None and not self.findings


class Stop(Exception):
    """Decoding cannot go on: the file fails closed with this reason."""


# ── bits ─────────────────────────────────────────────────────────────────

class Bits:
    """Big-endian bit reader over the decoded hint stream (Annex F: items
    are packed most significant bit first)."""

    def __init__(self, data: bytes, start: int = 0, pads: list[tuple[int, int]] | None = None
                 ) -> None:
        self.data = data
        self.pos = start * 8
        self.pads = pads if pads is not None else []  # (bit position, width) of each padding

    def read(self, n: int) -> int:
        if n < 0 or n > 32:
            raise Stop(f"bit width {n} out of range")
        if self.pos + n > len(self.data) * 8:
            raise Stop("table runs past the decoded stream")
        value = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def align(self) -> int:
        """Skip to the next byte boundary; the padding skipped, as an int
        (must be 0)."""
        rest = (-self.pos) % 8
        if rest:
            self.pads.append((self.pos, rest))
        return self.read(rest) if rest else 0

    @property
    def byte(self) -> int:
        return self.pos >> 3


def nbits(value: int) -> int:
    """Bits needed for *value* (qpdf's nbits; 0 for 0)."""
    return value.bit_length()


# ── the tables, as read ─────────────────────────────────────────────────

PAGE_HEADER = ("min_nobjects", "first_page_offset", "nbits_delta_nobjects",
               "min_page_length", "nbits_delta_page_length", "min_content_offset",
               "nbits_delta_content_offset", "min_content_length",
               "nbits_delta_content_length", "nbits_nshared_objects",
               "nbits_shared_identifier", "nbits_shared_numerator", "shared_denominator")
PAGE_WIDTHS = (32, 32, 16, 32, 16, 32, 16, 32, 16, 16, 16, 16, 16)
SHARED_HEADER = ("first_shared_obj", "first_shared_offset", "nshared_first_page",
                 "nshared_total", "nbits_nobjects", "min_group_length",
                 "nbits_delta_group_length")
SHARED_WIDTHS = (32, 32, 32, 32, 16, 32, 16)
GENERIC = ("first_object", "first_object_offset", "nobjects", "group_length")

# Hint stream dictionary keys (Annex F, Table F.2) and the format each
# table takes. /T (thumbnails) has its own format, not decoded here; /A
# (threads), /E (named destinations), /R (renditions) none of the corpus
# files carry. Generic = Table F.11's four items; extended = F.12: the
# generic items, then the number of shared object references (32), the
# bits per identifier (16) and the identifiers (the reading this spike
# tests on the files that carry one).
TABLE_FORMAT = {b"O": "generic", b"A": "generic", b"E": "generic", b"I": "generic",
                b"L": "generic", b"V": "extended", b"C": "extended", b"R": "extended",
                b"B": "extended", b"T": "thumbnail"}
STREAM_KEYS = {b"Length", b"Filter", b"S"} | set(TABLE_FORMAT)


@dataclass
class Tables:
    page: dict[str, int]
    pages: list[dict[str, Any]]
    shared: dict[str, int]
    groups: list[dict[str, int]]
    generic: dict[str, dict[str, Any]]
    spans: dict[str, tuple[int, int]]  # table -> decoded byte range
    size: int
    pads: list[tuple[int, int]] = field(default_factory=list)


def read_page_table(bits: Bits, npages: int, res: Result) -> tuple[dict[str, int], list[dict[str, Any]]]:
    head = {name: bits.read(w) for name, w in zip(PAGE_HEADER, PAGE_WIDTHS)}
    pages: list[dict[str, Any]] = [{} for _ in range(npages)]

    def column(name: str, width: int) -> None:
        for p in pages:
            p[name] = bits.read(width)
        if bits.align():
            res.bad("page offset", f"{name} padding", "padding bits not zero")

    column("delta_nobjects", head["nbits_delta_nobjects"])
    column("delta_page_length", head["nbits_delta_page_length"])
    column("nshared_objects", head["nbits_nshared_objects"])
    for name, width in (("shared_identifiers", head["nbits_shared_identifier"]),
                        ("shared_numerators", head["nbits_shared_numerator"])):
        for p in pages:
            p[name] = [bits.read(width) for _ in range(p["nshared_objects"])]
        if bits.align():
            res.bad("page offset", f"{name} padding", "padding bits not zero")
    column("delta_content_offset", head["nbits_delta_content_offset"])
    column("delta_content_length", head["nbits_delta_content_length"])
    return head, pages


def read_shared_table(bits: Bits, res: Result) -> tuple[dict[str, int], list[dict[str, int]]]:
    head = {name: bits.read(w) for name, w in zip(SHARED_HEADER, SHARED_WIDTHS)}
    n = head["nshared_total"]
    if n > 10_000_000:
        raise Stop("shared object count implausible")
    groups: list[dict[str, int]] = [{} for _ in range(n)]
    for g in groups:
        g["delta_group_length"] = bits.read(head["nbits_delta_group_length"])
    if bits.align():
        res.bad("shared object", "delta_group_length padding", "padding bits not zero")
    for g in groups:
        g["signature_present"] = bits.read(1)
    if bits.align():
        res.bad("shared object", "signature_present padding", "padding bits not zero")
    for g in groups:
        if g["signature_present"]:
            # 128-bit MD5 signature: Acrobat never writes one (note 129).
            res.bad("shared object", "signature", "signature present")
            for _ in range(4):
                bits.read(32)
    for g in groups:
        g["nobjects_minus_one"] = bits.read(head["nbits_nobjects"])
    if bits.align():
        res.bad("shared object", "nobjects padding", "padding bits not zero")
    return head, groups


def read_generic(bits: Bits, extended: bool, res: Result, table: str) -> dict[str, Any]:
    out: dict[str, Any] = {name: bits.read(32) for name in GENERIC}
    if extended:
        out["nshared"] = bits.read(32)
        out["nbits_identifier"] = bits.read(16)
        if out["nshared"] > 1_000_000:
            raise Stop(f"{table}: shared reference count implausible")
        out["identifiers"] = [bits.read(out["nbits_identifier"]) for _ in range(out["nshared"])]
        if bits.align():
            res.bad(table, "identifiers padding", "padding bits not zero")
    return out


# ── the file ─────────────────────────────────────────────────────────────

@dataclass
class Obj:
    num: int
    gen: int
    offset: int
    extent: int  # offset .. end of the whitespace after endobj
    gap: int     # bytes between that end and the next object's offset (0 when none)


@dataclass
class Layout:
    data: bytes             # the base revision's bytes
    chain: Chain
    lin: PdfDict
    lin_offset: int
    hint_offset: int
    hint_length: int        # /H[1]
    hint_num: int
    objects: dict[int, Obj]          # uncompressed objects of the base revision
    order: list[int]                 # their numbers in file order
    compressed: dict[int, int]       # member -> object stream number
    pages: list[int]                 # page object numbers, in page order
    users: dict[int, set[tuple[str, Any]]]  # object -> its users (qpdf's ObjUser)
    page_objects: list[set[int]]     # objects each page reaches
    root: int
    info: int | None
    first_section: tuple[int, ...]   # object numbers the first-page xref section lists
    main_section_offset: int
    first_xref_item: int             # offset of the main xref table's first entry


def _int(v: PdfValue | None) -> int | None:
    return v.value if isinstance(v, PdfInt) else None


def _extent(data: bytes, parser: ObjectParser, offset: int, limit: int) -> int | None:
    obj = parser.parse_indirect_at(offset, limit)
    if obj is None or obj.span.start != offset or not obj.complete:
        return None
    end = obj.span.end
    while end < len(data) and data[end] in _WS:
        end += 1
    return end - offset


def build_layout(data: bytes) -> Layout:
    chain = read_chain(data, budget=Budget())
    lin = chain.linearization
    if lin is None or not chain.revisions:
        raise Stop("no accepted linearized pair")
    base = len(chain.revisions) - 1
    # The pair ends after its own startxref and %%EOF (and one end of line).
    base_end = chain.revision_end(base)
    eof = data.find(b"%%EOF", base_end)
    if eof < 0:
        raise Stop("no %%EOF after the linearized pair")
    base_end = eof + 5
    for eol in (b"\r\n", b"\r", b"\n"):
        if data.startswith(eol, base_end):
            base_end += len(eol)
            break
    cut = data[:base_end]
    first_index, main_index = chain.revisions[base]
    first, main = chain.sections[first_index], chain.sections[main_index]
    entries = chain.object_map(base)
    parser = ObjectParser(cut, budget=Budget())
    hint = lin.get(b"H")
    if not isinstance(hint, PdfArray) or len(hint.items) not in (2, 4):
        raise Stop("/H is not a 2- or 4-item array")
    if len(hint.items) == 4:
        raise Stop("overflow hint stream present (not decoded by the spike)")
    h_off, h_len = (_int(x) for x in hint.items)
    if h_off is None or h_len is None:
        raise Stop("/H items are not integers")

    offsets = sorted((e.a, n) for n, e in entries.items()
                     if e.kind == IN_USE and n != 0 and 0 < e.a < base_end)
    objects: dict[int, Obj] = {}
    for i, (off, num) in enumerate(offsets):
        limit = offsets[i + 1][0] if i + 1 < len(offsets) else first.offset if off < first.offset \
            else base_end
        if limit <= off:
            limit = base_end
        ext = _extent(cut, parser, off, max(limit, off + 1) if limit > off else base_end)
        if ext is None:
            ext = _extent(cut, parser, off, base_end)
        if ext is None:
            raise Stop("an xref offset does not start a complete object")
        nxt = offsets[i + 1][0] if i + 1 < len(offsets) else None
        objects[num] = Obj(num, entries[num].b, off, ext, 0 if nxt is None else nxt - off - ext)
    order = [n for _, n in offsets]
    compressed = {n: e.a for n, e in entries.items() if e.kind == COMPRESSED}

    hint_num = next((n for n in order if objects[n].offset == h_off), None)
    if hint_num is None:
        raise Stop("/H offset does not start an object")
    lin_num = order[0]
    # The trailer as a reader merges it: the first-page section's keys win.
    trailer: dict[bytes, PdfValue] = {}
    for s in (main, first):
        if s.trailer is not None:
            trailer.update({n.raw: v for n, v in s.trailer.entries})
    for drop in (b"Size", b"Prev", b"Index", b"W", b"Type", b"Length", b"Filter",
                 b"DecodeParms", b"XRefStm"):
        trailer.pop(drop, None)
    root_ref = trailer.get(b"Root")
    if not isinstance(root_ref, PdfRef):
        raise Stop("no /Root")
    info_ref = trailer.get(b"Info")

    first_nums = tuple(sorted(first.entries))
    values = Values(cut, entries, objects)
    pages, users, page_objects = _graph(values, trailer, root_ref)
    first_xref_item = _first_xref_item(cut, main.offset, main.is_stream)
    return Layout(cut, chain, lin, objects[lin_num].offset, h_off, h_len, hint_num, objects,
                  order, compressed, pages, users, page_objects, root_ref.num,
                  info_ref.num if isinstance(info_ref, PdfRef) else None, first_nums,
                  main.offset, first_xref_item)


def _first_xref_item(data: bytes, offset: int, is_stream: bool) -> int:
    """Where the main cross-reference table's first entry starts (qpdf's
    first_xref_item_offset): after ``xref``, its end of line, and the first
    subsection header. For a stream, its offset."""
    if is_stream:
        return offset
    pos = offset + 4
    while data[pos:pos + 1] in (b"\r", b"\n", b" "):
        pos += 1
    while data[pos:pos + 1] not in (b"\r", b"\n"):
        pos += 1
    while data[pos:pos + 1] in (b"\r", b"\n", b" "):
        pos += 1
    return pos


class Values:
    """Each in-use object's value in the base revision, parsed by this
    repository's own parser (no qpdf): uncompressed bodies at their xref
    offsets, compressed members out of their (Flate) object streams."""

    def __init__(self, cut: bytes, entries: dict[int, Any], objects: dict[int, Obj]) -> None:
        self.cut = cut
        self.entries = entries
        self.objects = objects
        self.parser = ObjectParser(cut, budget=Budget())
        self.cache: dict[int, PdfValue | None] = {}
        self.streams: dict[int, dict[int, PdfValue | None]] = {}

    def get(self, ref: PdfRef) -> PdfValue | None:
        entry = self.entries.get(ref.num)
        if entry is None:
            return None
        if entry.kind == IN_USE and ref.num in self.objects and entry.b == ref.gen:
            if ref.num not in self.cache:
                o = self.objects[ref.num]
                obj = self.parser.parse_indirect_at(o.offset, o.offset + o.extent)
                self.cache[ref.num] = obj.value if obj is not None else None
            return self.cache[ref.num]
        if entry.kind == COMPRESSED and ref.gen == 0:
            return self._member(entry.a, ref.num)
        return None

    def _member(self, stream: int, num: int) -> PdfValue | None:
        if stream not in self.streams:
            self.streams[stream] = {}
            o = self.objects.get(stream)
            if o is None:
                return None
            obj = self.parser.parse_indirect_at(o.offset, o.offset + o.extent)
            if obj is None or obj.stream is None or not isinstance(obj.value, PdfDict):
                return None
            raw = self.cut[obj.stream.data.start:obj.stream.data.end]
            data = flate_decode(raw, None, Budget()).data
            n, first = _int(obj.value.get(b"N")), _int(obj.value.get(b"First"))
            if n is None or first is None:
                return None
            sub = ObjectParser(data, budget=Budget())
            pos, header = 0, []
            for _ in range(2 * n):
                pv = sub.parse_value_at(pos, first)
                header.append(_int(pv.value))
                pos = pv.span.end
            for i in range(n):
                number, rel = header[2 * i], header[2 * i + 1]
                if number is not None and rel is not None:
                    self.streams[stream][number] = sub.parse_value_at(first + rel).value
        return self.streams[stream].get(num)


def _type(value: PdfValue | None) -> bytes | None:
    if isinstance(value, PdfDict):
        t = value.get(b"Type")
        return t.raw if isinstance(t, PdfName) else None
    return None


def _graph(values: Values, trailer: dict[bytes, PdfValue], root: PdfRef
           ) -> tuple[list[int], dict[int, set[tuple[str, Any]]], list[set[int]]]:
    """Pages and object users as qpdf's optimize() computes them on the
    base revision: each page (inherited attributes pushed down), each
    trailer key but /Root, each root key, the root itself."""
    users: dict[int, set[tuple[str, Any]]] = {}
    root_value = values.get(root)
    if not isinstance(root_value, PdfDict):
        raise Stop("catalog is not a dictionary")
    pages: list[int] = []
    inherited: list[dict[bytes, PdfValue]] = []
    # The page tree, depth first, inheritable attributes carried down.
    stack: list[tuple[PdfValue | None, dict[bytes, PdfValue], int]] = [
        (root_value.get(b"Pages"), {}, 0)]
    seen_nodes: set[int] = set()
    while stack:
        node_ref, inh, depth = stack.pop()
        if not isinstance(node_ref, PdfRef) or node_ref.num in seen_nodes or depth > 64:
            raise Stop("page tree is malformed")
        seen_nodes.add(node_ref.num)
        node = values.get(node_ref)
        if not isinstance(node, PdfDict):
            raise Stop("page tree node is not a dictionary")
        here = dict(inh)
        for key in (b"Resources", b"MediaBox", b"CropBox", b"Rotate"):
            v = node.get(key)
            if v is not None:
                here[key] = v
        if _type(node) == b"Page":
            pages.append(node_ref.num)
            inherited.append({k: v for k, v in here.items() if node.get(k) is None})
            continue
        kids = node.get(b"Kids")
        if not isinstance(kids, PdfArray):
            raise Stop("page tree node without /Kids")
        for kid in reversed(kids.items):
            stack.append((kid, here, depth + 1))
    page_objects: list[set[int]] = []
    for i, num in enumerate(pages):
        seen: set[int] = set()
        start: list[tuple[PdfValue | None, bool, tuple[str, Any]]] = [
            (PdfRef(0, 0, num, values.entries[num].b if values.entries[num].kind == IN_USE else 0),
             True, ("page", i))]
        start += [(v, False, ("page", i)) for v in inherited[i].values()]
        _walk(values, start, users, seen)
        page_objects.append(seen)
    for key, value in trailer.items():
        if key != b"Root":
            _walk(values, [(value, False, ("trailer", key))], users, set())
    for name, value in root_value.entries:
        _walk(values, [(value, False, ("root_key", name.raw))], users, set())
    users.setdefault(root.num, set()).add(("root", None))
    return pages, users, page_objects


def _walk(values: Values, start: list[Any], users: dict[int, set[tuple[str, Any]]],
          seen: set[int]) -> None:
    pending = list(start)
    visited: set[int] = set()
    while pending:
        item, top, ou = pending.pop()
        num = None
        if isinstance(item, PdfRef):
            num = item.num
            item = values.get(item)
            if item is None:
                continue
        is_page = _type(item) == b"Page"
        if is_page and not top:
            continue
        if num is not None:
            if num in visited:
                continue
            visited.add(num)
            users.setdefault(num, set()).add(ou)
            if ou[0] == "page":
                seen.add(num)
        if isinstance(item, PdfArray):
            pending.extend((v, False, ou) for v in item.items)
        elif isinstance(item, PdfDict):
            for name, value in item.entries:
                if is_page and name.raw == b"Thumb":
                    pending.append((value, False, ("thumb", ou[1])))
                elif is_page and name.raw == b"Parent":
                    continue
                else:
                    pending.append((value, False, ou))


# ── the hint stream ──────────────────────────────────────────────────────

def decode_stream(lay: Layout, res: Result) -> Tables:
    """The hint stream's decoded tables. Every key of its dictionary must
    be a table offset or a stream key; every decoded byte must belong to
    exactly one table, with nothing between or after them."""
    parser = ObjectParser(lay.data, budget=Budget())
    obj = parser.parse_indirect_at(lay.hint_offset, len(lay.data))
    if obj is None or obj.stream is None or not isinstance(obj.value, PdfDict):
        raise Stop("/H does not name a stream")
    d = obj.value
    if d.ambiguous:
        res.bad("hint stream", "dictionary", "repeated key")
    for entry, _ in d.entries:
        if entry.raw not in STREAM_KEYS:
            res.bad("hint stream", f"/{entry.raw.decode('latin-1')}", "unknown dictionary key")
    filt = d.get(b"Filter")
    raw = lay.data[obj.stream.data.start:obj.stream.data.end]
    if obj.stream.slack is not None:
        res.bad("hint stream", "stream data", "bytes past /Length")
    if filt is None:
        decoded = raw
    elif isinstance(filt, PdfName) and filt.raw == b"FlateDecode":
        out = flate_decode(raw, None, Budget())
        if not out.complete:
            res.bad("hint stream", "stream data", "flate not clean (trailing or truncated)")
        decoded = out.data
    else:
        raise Stop("hint stream filter other than FlateDecode")
    # /H[1]: the hint stream object's length; Acrobat's runs through an
    # indirect /Length object placed right after it (qpdf's readHintStream).
    end = lay.hint_offset + lay.hint_length
    ext = lay.objects[lay.hint_num]
    hi = lay.hint_offset + ext.extent
    lo = hi - _trailing_ws(lay.data, hi)
    length = d.get(b"Length")
    if isinstance(length, PdfRef) and length.num in lay.objects:
        lobj = lay.objects[length.num]
        hi = lobj.offset + lobj.extent
        lo = hi - _trailing_ws(lay.data, hi)
        res.rule("H-length-indirect")
    if not lo <= end <= hi:
        res.bad("linearization dictionary", "/H length", "mismatch")
    elif end != hi:
        res.bad("linearization dictionary", "/H length", "inside the whitespace after endobj")

    starts: dict[str, int] = {"P": 0}
    for key in sorted(TABLE_FORMAT.keys() | {b"S"}):
        v = d.get(key)
        if v is None:
            continue
        o = _int(v)
        if o is None or not 0 <= o < len(decoded):
            res.bad("hint stream", f"/{key.decode()}", "table offset out of range")
            continue
        starts[key.decode()] = o
    if "S" not in starts:
        raise Stop("no shared object hint table offset")
    order = sorted(starts.items(), key=lambda kv: kv[1])
    spans: dict[str, tuple[int, int]] = {}
    generic: dict[str, dict[str, Any]] = {}
    page: dict[str, int] = {}
    pages: list[dict[str, Any]] = []
    shared: dict[str, int] = {}
    groups: list[dict[str, int]] = []
    pads: list[tuple[int, int]] = []
    for i, (name, start) in enumerate(order):
        nxt = order[i + 1][1] if i + 1 < len(order) else len(decoded)
        bits = Bits(decoded, start, pads)
        if name == "P":
            page, pages = read_page_table(bits, len(lay.pages), res)
            label = "page offset"
        elif name == "S":
            shared, groups = read_shared_table(bits, res)
            label = "shared object"
        elif name == "T":
            res.bad("thumbnail", "table", "thumbnail table not decoded by the spike")
            spans[name] = (start, nxt)
            continue
        else:
            label = f"/{name} table"
            try:
                generic[name] = read_generic(bits, TABLE_FORMAT[name.encode()] == "extended",
                                             res, label)
            except Stop:
                res.bad(label, "table", "no reading decodes it")
                spans[name] = (start, nxt)
                continue
        if bits.align():
            res.bad(label, "final padding", "padding bits not zero")
        spans[name] = (start, bits.byte)
        if bits.byte > nxt:
            res.bad(label, "extent", "overlaps the next table")
        elif bits.byte < nxt:
            gap = decoded[bits.byte:nxt]
            res.bad(label, "bytes after table",
                    "unread bytes, zero" if not gap.strip(b"\x00") else "unread bytes, non-zero",
                    f"{len(gap)}")
    return Tables(page, pages, shared, groups, generic, spans, len(decoded), pads)


def _trailing_ws(data: bytes, end: int) -> int:
    n = 0
    while end - n - 1 >= 0 and data[end - n - 1] in _WS:
        n += 1
    return n


# ── accounting ───────────────────────────────────────────────────────────
#
# Each value is accounted only when it equals what the layout fixes under
# Annex F, or under one explicit rule below that fixes it exactly, never a
# range. Rules are bound to one writer profile: a file must match one whole
# profile, so no value can take the other profile's reading (review #180,
# b1). Each rule says where it comes from: "documented" (qpdf's source,
# v12.4.1, or the PDF Reference implementation notes it cites) or
# "observed" (an undocumented writer convention found in the corpus, derived
# on one half of the warned files and measured on the other; an owner
# question).

PROFILES = ("qpdf", "acrobat")

RULES: dict[str, tuple[str, str, str]] = {
    # name: (profile, documented|observed, what it fixes)
    "N126-content-offset-zero": ("both", "documented",
                                 "note 126: min_content_offset 0, its width 0"),
    "N128-nobjects-width-zero": ("both", "documented",
                                 "notes 125/128/130: one object per shared group, width 0"),
    "N131-no-part8-zero": ("both", "documented",
                           "note 131: first_shared_obj/offset meaningless without part 8; "
                           "accepted only as 0"),
    "H-length-indirect": ("both", "documented",
                          "readHintStream: /H[1] runs through an indirect /Length placed "
                          "after the stream"),
    "Q-first-page-at-page": ("qpdf", "documented",
                             "Annex F item 2 (qpdf's writer): the first-page section starts "
                             "at the first page's page object"),
    "Q-content-length-as-page": ("qpdf", "documented",
                                 "qpdf's writer: min_content_length = min_page_length, widths "
                                 "and deltas as the page length's"),
    "Q-identifier-width-total": ("qpdf", "documented",
                                 "qpdf's writer: identifier width = nbits(nshared_total)"),
    "Q-numerators-zero": ("qpdf", "documented", "qpdf's writer: every numerator 0, width 0"),
    "Q-denominator-4": ("qpdf", "documented",
                        "qpdf's writer: shared_denominator 4 ('doesn't matter')"),
    "Q-no-page0-entries": ("qpdf", "documented", "Annex F, qpdf's writer: page 0 has none"),
    "Q-identifiers-ascending": ("qpdf", "documented",
                                "qpdf's writer: a page's identifiers ascending"),
    "N127-content-length": ("acrobat", "documented",
                            "note 127: min_content_length 0, its width = item 5's, each "
                            "delta_content_length = delta_page_length"),
    "QR-private-resource-in-part8": ("acrobat", "documented",
                                     "checkHPageOffset: Acrobat puts a page's private "
                                     "resources with the shared objects (part 8)"),
    "A-first-page-section": ("acrobat", "observed",
                             "the first-page section starts after the catalog, /OpenAction's "
                             "objects and the hint stream's /Length; every object in it is "
                             "a shared group"),
    "A-identifier-width-pages": ("acrobat", "observed",
                                 "identifier width = nbits(greatest identifier pages after "
                                 "the first use); 0 for one page"),
    "A-numerator-width-denominator": ("acrobat", "observed",
                                      "numerator width = nbits(denominator)"),
    "A-denominator-8": ("acrobat", "observed", "shared_denominator 8"),
    "A-page0-entries": ("acrobat", "observed",
                        "page 0's entries: the first-page section's objects page 0 reaches "
                        "that something else reaches too, plus page 0's indirect /Font and "
                        "/ColorSpace resources; numerator floor(first use x (d-1) / content "
                        "length) for a resource (0 if unused), 0 for a widget annotation or "
                        "a form named in an annotation's /AP, d otherwise; ascending"),
    "A-later-numerators": ("acrobat", "observed",
                           "a later page's numerator: the least floor(first use x (d-1) / "
                           "content length) of the page's resources the group holds (or, for "
                           "a part-8 group, that reach it); 0 if unused; d if none"),
    "A-later-order": ("acrobat", "observed",
                      "a later page's identifiers by numerator, ties in the order a "
                      "depth-first walk of the page (written key order) first meets them"),
    "A-form-order": ("acrobat", "observed",
                     "/V and /C identifiers in the order a stack walk of the key's object "
                     "discovers them (written key order)"),
}

NAME_END = rb"(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])"
_WALK_SKIP = (b"P", b"Parent", b"Pg")


def _adj(lay: Layout, offset: int) -> int:
    """A hint-table offset as a file offset: Annex F's offsets disregard
    the hint stream itself (qpdf's adjusted_offset)."""
    return offset + lay.hint_length if offset >= lay.hint_offset else offset


def _offset_hint(lay: Layout, raw: int, actual: int) -> str:
    """For a mismatched offset: where the value points, relative to the
    object it should name (a delta in objects, not a value)."""
    by_off = {o.offset: n for n, o in lay.objects.items()}
    pos = {n: i for i, n in enumerate(lay.order)}
    for label, off in (("adjusted", _adj(lay, raw)), ("raw", raw)):
        if off in by_off and actual in by_off:
            return f"{label} names an object {pos[by_off[off]] - pos[by_off[actual]]:+d} away"
    return "names no object"


class Doc:
    """The base revision's values, for the content-derived rules."""

    def __init__(self, lay: Layout) -> None:
        self.lay = lay
        self.values = Values(lay.data, lay.chain.object_map(len(lay.chain.revisions) - 1),
                             lay.objects)

    def ref(self, num: int) -> PdfRef:
        e = self.values.entries.get(num)
        return PdfRef(0, 0, num, e.b if e is not None and e.kind == IN_USE else 0)

    def get(self, v: PdfValue | None) -> PdfValue | None:
        return self.values.get(v) if isinstance(v, PdfRef) else v

    def stream(self, num: int) -> bytes:
        o = self.lay.objects.get(num)
        if o is None:
            raise Stop("a content stream is compressed")
        obj = self.values.parser.parse_indirect_at(o.offset, o.offset + o.extent)
        if obj is None or obj.stream is None or not isinstance(obj.value, PdfDict):
            raise Stop("a content stream is not a stream")
        raw = self.lay.data[obj.stream.data.start:obj.stream.data.end]
        f = obj.value.get(b"Filter")
        if isinstance(f, PdfArray) and len(f.items) == 1:
            f = f.items[0]
        if f is None:
            return raw
        if isinstance(f, PdfName) and f.raw == b"FlateDecode" and obj.value.get(
                b"DecodeParms") is None:
            return flate_decode(raw, None, Budget()).data
        raise Stop("a content stream filter the spike does not decode")

    def content(self, page: int) -> bytes:
        pd = self.get(self.ref(page))
        c = pd.get(b"Contents") if isinstance(pd, PdfDict) else None
        c2 = self.get(c)
        refs = list(c2.items) if isinstance(c2, PdfArray) else [c] if c is not None else []
        return b"".join(self.stream(r.num) for r in refs if isinstance(r, PdfRef))

    def resources(self, page: int) -> list[tuple[bytes, bytes, PdfValue]]:
        pd = self.get(self.ref(page))
        res = self.get(pd.get(b"Resources")) if isinstance(pd, PdfDict) else None
        out: list[tuple[bytes, bytes, PdfValue]] = []
        if isinstance(res, PdfDict):
            for cat, raw_sub in res.entries:
                sub = self.get(raw_sub)
                if isinstance(sub, PdfDict):
                    out.extend((cat.raw, name.raw, v) for name, v in sub.entries)
        return out

    def kids(self, v: PdfValue | None) -> list[PdfValue]:
        if isinstance(v, PdfArray):
            return list(v.items)
        if isinstance(v, PdfDict):
            return [x for k, x in v.entries if k.raw not in _WALK_SKIP]
        return []

    def preorder(self, start: PdfValue) -> list[int]:
        """Objects in the order a depth-first walk in written key order
        first meets them, not into another page."""
        seen: set[int] = set()
        out: list[int] = []
        stack: list[PdfValue | None] = [start]
        while stack:
            v = stack.pop()
            if isinstance(v, PdfRef):
                if v.num in seen:
                    continue
                seen.add(v.num)
                out.append(v.num)
                v = self.get(v)
                if _type(v) == b"Page":
                    continue
            stack.extend(reversed(self.kids(v)))
        return out

    def discovery(self, start: PdfValue | None) -> list[int]:
        """Objects in the order a stack walk discovers them: expanding a
        value lists each unseen reference in written order and pushes it."""
        seen: set[int] = set()
        out: list[int] = []
        stack: list[PdfValue | None] = [start]
        if isinstance(start, PdfRef):
            seen.add(start.num)
            out.append(start.num)
            stack = [self.get(start)]
        while stack:
            v = stack.pop()
            if _type(v) == b"Page":
                continue
            for ch in self.kids(v):
                if isinstance(ch, PdfRef):
                    if ch.num not in seen:
                        seen.add(ch.num)
                        out.append(ch.num)
                        stack.append(self.get(ch))
                elif isinstance(ch, (PdfArray, PdfDict)):
                    stack.append(ch)
        return out

    def reach(self, start: int) -> set[int]:
        seen = {start}
        stack: list[PdfValue | None] = [self.get(self.ref(start))]
        while stack:
            v = stack.pop()
            if _type(v) == b"Page":
                continue
            for x in self.kids(v):
                if isinstance(x, PdfRef):
                    if x.num not in seen:
                        seen.add(x.num)
                        stack.append(self.get(x))
                elif isinstance(x, (PdfArray, PdfDict)):
                    stack.append(x)
        return seen

    def first_use(self, dec: bytes, name: bytes, d: int) -> int | None:
        m = re.search(re.escape(b"/" + name) + NAME_END, dec)
        return None if m is None or not dec else m.start() * (d - 1) // len(dec)

    def ap_forms(self, page: int) -> set[int]:
        out: set[int] = set()
        pd = self.get(self.ref(page))
        annots = self.get(pd.get(b"Annots")) if isinstance(pd, PdfDict) else None
        for a in (annots.items if isinstance(annots, PdfArray) else ()):
            ad = self.get(a)
            ap = self.get(ad.get(b"AP")) if isinstance(ad, PdfDict) else None
            if not isinstance(ap, PdfDict):
                continue
            for _, v in ap.entries:
                vv = self.get(v)
                if isinstance(v, PdfRef) and isinstance(vv, PdfDict) and vv.get(b"Length") \
                        is not None:
                    out.add(v.num)
                elif isinstance(vv, PdfDict):
                    out.update(x.num for _, x in vv.entries if isinstance(x, PdfRef))
        return out

    def is_widget(self, num: int) -> bool:
        v = self.get(self.ref(num))
        return isinstance(v, PdfDict) and _type(v) == b"Annot" and isinstance(
            v.get(b"Subtype"), PdfName) and v.get(b"Subtype").raw == b"Widget"  # type: ignore[union-attr]


def page0_entries(doc: Doc, sect0: list[int], d: int) -> list[int]:
    """A-page0-entries: the numerators Acrobat's page 0 entries hold."""
    lay = doc.lay
    p0 = lay.pages[0]
    shared = {o for o in lay.page_objects[0]
              if o in set(sect0) and o != p0 and len(lay.users.get(o, ())) > 1}
    dec = doc.content(p0)
    res: dict[int, int | None] = {}
    for cat, name, v in doc.resources(p0):
        if cat in (b"Font", b"ColorSpace") and isinstance(v, PdfRef):
            res[v.num] = doc.first_use(dec, name, d)
    forms = doc.ap_forms(p0)
    out = []
    for o in shared | set(res):
        if o in res:
            out.append(res[o] or 0)
        elif doc.is_widget(o) or o in forms:
            out.append(0)
        else:
            out.append(d)
    return sorted(out)


def later_numerators(doc: Doc, i: int, first: set[int], d: int) -> dict[int, int | None]:
    """A-later-numerators: group -> numerator source on page *i*."""
    lay = doc.lay
    dec = doc.content(lay.pages[i])
    out: dict[int, int | None] = {}
    for _, name, v in doc.resources(lay.pages[i]):
        if not isinstance(v, PdfRef):
            continue
        n = doc.first_use(dec, name, d)
        for num in doc.reach(v.num):
            key = lay.compressed.get(num, num)
            if num != v.num and key in first:
                continue
            old = out.get(key, -1)
            if key not in out or (n is not None and (old is None or n < old)):
                out[key] = n
    return out


def check(lay: Layout, t: Tables, res: Result, profile: str) -> None:
    """Account for every value under one writer *profile*."""
    def rule(name: str) -> None:
        assert RULES[name][0] in (profile, "both"), name
        res.rule(name)

    acro = profile == "acrobat"
    objects, order = lay.objects, lay.order
    pos = {n: i for i, n in enumerate(order)}
    users: dict[int, set[tuple[str, Any]]] = {}
    for n, us in lay.users.items():
        users.setdefault(lay.compressed.get(n, n), set()).update(us)

    def ext(nums: list[int]) -> int:
        return sum(objects[n].extent for n in nums)

    first = set(lay.first_section) - {order[0], lay.hint_num}
    npages = len(lay.pages)
    if any(p not in objects for p in lay.pages):
        res.bad("page offset", "pages", "a page object is compressed")
        return
    listed = [n for n in order[1:] if n in lay.first_section]
    if not listed or any(pos[b] != pos[a] + 1 for a, b in zip(listed, listed[1:])):
        res.bad("layout", "first-page section", "not contiguous")
    first_run = [n for n in listed if n in first]
    p0 = lay.pages[0]
    length_obj = {n for n in objects if not users.get(n) and n in first}

    def part4(n: int) -> bool:
        us = users.get(n, set())
        return n == lay.root or n in length_obj or (
            bool(us) and all(u == ("root_key", b"OpenAction") for u in us))
    if acro:
        i = 0
        while i < len(first_run) and part4(first_run[i]):
            i += 1
        sect0 = first_run[i:]
        rule("A-first-page-section")
    else:
        sect0 = first_run[first_run.index(p0):] if p0 in first_run else []
        rule("Q-first-page-at-page")
    page, pages, shared, groups = t.page, t.pages, t.shared, t.groups
    if not sect0:
        res.bad("page offset", "first-page section", "empty under this profile")
        return
    if _adj(lay, page["first_page_offset"]) != objects[sect0[0]].offset:
        res.bad("page offset", "first_page_offset", "mismatch")

    sections = [sect0]
    for i in range(1, npages):
        start = lay.pages[i]
        run = [start]
        j = pos[start] + 1
        while j < len(order) and users.get(order[j], set()) == {("page", i)}:
            run.append(order[j])
            j += 1
        sections.append(run)
    counts = [len(s) for s in sections]
    lengths = [ext(s) for s in sections]
    if page["min_nobjects"] != min(counts):
        res.bad("page offset", "min_nobjects", "mismatch")
    if page["min_page_length"] != min(lengths):
        res.bad("page offset", "min_page_length", "mismatch")
    if page["nbits_delta_nobjects"] != nbits(max(counts) - min(counts)):
        res.bad("page offset", "nbits_delta_nobjects", "not minimal or wrong")
    if page["nbits_delta_page_length"] != nbits(max(lengths) - min(lengths)):
        res.bad("page offset", "nbits_delta_page_length", "not minimal or wrong")
    for i, e in enumerate(pages):
        if e["delta_nobjects"] + page["min_nobjects"] != counts[i]:
            res.bad("page offset", f"page {'0' if i == 0 else 'N'} nobjects", "mismatch")
        if e["delta_page_length"] + page["min_page_length"] != lengths[i]:
            res.bad("page offset", f"page {'0' if i == 0 else 'N'} page_length", "mismatch")

    if page["min_content_offset"] == 0 and page["nbits_delta_content_offset"] == 0:
        rule("N126-content-offset-zero")
    else:
        res.bad("page offset", "content offset", "no rule fixes it")
    same = (page["nbits_delta_content_length"] == page["nbits_delta_page_length"]
            and all(e["delta_content_length"] == e["delta_page_length"] for e in pages))
    want_min = 0 if acro else page["min_page_length"]
    if same and page["min_content_length"] == want_min:
        rule("N127-content-length" if acro else "Q-content-length-as-page")
    else:
        res.bad("page offset", "content length", "not this profile's reading")

    # The shared object table.
    nfp, total = shared["nshared_first_page"], shared["nshared_total"]
    if nfp != len(sect0):
        res.bad("shared object", "nshared_first_page", "mismatch")
    last = sections[-1][-1] if npages > 1 else sect0[-1]
    part8: list[int] = []
    j = pos[last] + 1
    while j < len(order):
        pg = {u[1] for u in users.get(order[j], set()) if u[0] == "page"}
        if not pg or 0 in pg:
            break
        if len(pg) == 1:
            if not acro:
                break
            rule("QR-private-resource-in-part8")
        part8.append(order[j])
        j += 1
    group_objs = sect0 + part8
    if total != len(group_objs):
        res.bad("shared object", "nshared_total", "mismatch")
    if shared["nbits_nobjects"] == 0:
        rule("N128-nobjects-width-zero")
    else:
        res.bad("shared object", "nbits_nobjects", "no rule fixes it")
    if part8:
        if shared["first_shared_obj"] != part8[0]:
            res.bad("shared object", "first_shared_obj", "mismatch")
        if _adj(lay, shared["first_shared_offset"]) != objects[part8[0]].offset:
            res.bad("shared object", "first_shared_offset", "mismatch",
                    _offset_hint(lay, shared["first_shared_offset"], objects[part8[0]].offset))
    elif shared["first_shared_obj"] == 0 and shared["first_shared_offset"] == 0:
        rule("N131-no-part8-zero")
    else:
        res.bad("shared object", "first_shared_obj/offset", "no part 8, values not zero")
    glens = [objects[n].extent for n in group_objs]
    if glens and len(groups) == len(glens):
        if shared["min_group_length"] != min(glens):
            res.bad("shared object", "min_group_length", "mismatch")
        if shared["nbits_delta_group_length"] != nbits(max(glens) - min(glens)):
            res.bad("shared object", "nbits_delta_group_length", "not minimal or wrong")
        bad = sum(1 for g, n in zip(groups, glens)
                  if g["delta_group_length"] + shared["min_group_length"] != n)
        if bad:
            res.bad("shared object", "group lengths", "mismatch", str(bad))

    # Shared references per page: widths, page 0, later pages.
    width, denom = page["nbits_shared_numerator"], page["shared_denominator"]
    if denom == (8 if acro else 4):
        rule("A-denominator-8" if acro else "Q-denominator-4")
    else:
        res.bad("page offset", "shared_denominator", "not this profile's value")
    nums = [x for e in pages for x in e["shared_numerators"]]
    if acro:
        if width == nbits(denom):
            rule("A-numerator-width-denominator")
        else:
            res.bad("page offset", "nbits_shared_numerator", "not this profile's width")
    elif width == 0 and not any(nums):
        rule("Q-numerators-zero")
    else:
        res.bad("page offset", "numerators", "not this profile's (all 0, width 0)")
    if any(x > denom for x in nums):
        res.bad("page offset", "numerators", "numerator above the denominator")
    max_n = max(e["nshared_objects"] for e in pages)
    if page["nbits_nshared_objects"] != nbits(max_n):
        res.bad("page offset", "nbits_nshared_objects", "not minimal")
    later_ids = [x for e in pages[1:] for x in e["shared_identifiers"]]
    if acro:
        if page["nbits_shared_identifier"] == nbits(max(later_ids, default=0)):
            rule("A-identifier-width-pages")
        else:
            res.bad("page offset", "nbits_shared_identifier", "not this profile's width")
    elif page["nbits_shared_identifier"] == nbits(total):
        rule("Q-identifier-width-total")
    else:
        res.bad("page offset", "nbits_shared_identifier", "not this profile's width")

    doc = Doc(lay)
    e0 = pages[0]
    if not acro:
        if e0["nshared_objects"]:
            res.bad("page offset", "page 0 entries", "present (qpdf profile: none)")
        else:
            rule("Q-no-page0-entries")
    elif any(e0["shared_identifiers"]):
        res.bad("page offset", "page 0 identifiers", "not zero")
    else:
        try:
            if page0_entries(doc, sect0, denom) == e0["shared_numerators"]:
                rule("A-page0-entries")
            else:
                res.bad("page offset", "page 0 entries", "not the observed rule's")
        except Stop as err:
            res.bad("page offset", "page 0 entries", str(err))

    idx_obj = dict(enumerate(group_objs))
    first_groups = set(group_objs[:nfp])
    for i in range(1, npages):
        e = pages[i]
        ids, enums = e["shared_identifiers"], e["shared_numerators"]
        hinted = {idx_obj.get(x, -1) for x in ids}
        if len(hinted) != len(ids):
            res.bad("page offset", "page N identifiers", "repeated identifier")
        reach = {n for n in (lay.compressed.get(m, m) for m in lay.page_objects[i])
                 if n in set(group_objs)}
        if -1 in hinted:
            res.bad("page offset", "page N identifiers", "identifier past the table")
            continue
        if hinted - reach:
            res.bad("page offset", "page N identifiers", "names an object the page does not reach")
        if reach - hinted:
            res.bad("page offset", "page N identifiers", "omits a shared object the page reaches")
        if not acro:
            if ids == sorted(ids):
                rule("Q-identifiers-ascending")
            else:
                res.bad("page offset", "page N identifiers", "order: not ascending")
            continue
        try:
            src = later_numerators(doc, i, first_groups, denom)
        except Stop as err:
            res.bad("page offset", "page N numerators", str(err))
            continue
        pred = [(src[idx_obj[x]] or 0) if idx_obj[x] in src else denom for x in ids]
        if pred == enums:
            rule("A-later-numerators")
        else:
            res.bad("page offset", "page N numerators", "not the observed rule's")
        pd = doc.get(doc.ref(lay.pages[i]))
        top = PdfArray(0, 0, tuple(v for k, v in pd.entries if k.raw != b"Parent")) \
            if isinstance(pd, PdfDict) else PdfArray(0, 0, ())
        walk: dict[int, int] = {}
        for k, n in enumerate(doc.preorder(top)):
            walk.setdefault(lay.compressed.get(n, n), k)
        want = sorted(ids, key=lambda x: (enums[ids.index(x)], walk.get(idx_obj[x], 1 << 30)))
        if want == ids:
            rule("A-later-order")
        else:
            res.bad("page offset", "page N identifiers", "order: not the observed rule's")

    _check_generic(lay, t, res, users, doc, profile)
    _check_lindict(lay, sect0, res)


_GENERIC_KEY = {"O": b"Outlines", "V": b"AcroForm", "L": b"PageLabels",
                "C": b"StructTreeRoot", "B": b"Names", "E": b"Dests", "A": b"Threads"}


def _check_generic(lay: Layout, t: Tables, res: Result,
                   users: dict[int, set[tuple[str, Any]]], doc: Doc, profile: str) -> None:
    """A generic table names a group of consecutive objects: every object
    in it reached by the table's key, every object the key reaches that is
    in no shared group in it, the offset its first object's, the length
    the group's. An extended table's identifiers must name exactly the
    shared groups the key reaches (excluding groups only the key reaches),
    in the profile's order: ascending (qpdf, which never writes these
    tables) or the stack walk's discovery order (Acrobat, observed)."""
    objects, order = lay.objects, lay.order
    pos = {n: i for i, n in enumerate(order)}
    group_objs = _group_objects(lay, t)
    in_groups = set(group_objs)
    for name, g in t.generic.items():
        label = f"/{name} table"
        if name == "I":
            owner: tuple[str, Any] = ("trailer", b"Info")
        elif name in _GENERIC_KEY:
            owner = ("root_key", _GENERIC_KEY[name])
        else:
            res.bad(label, "table", "no reading for this table")
            continue
        fo = g["first_object"]
        n = g["nobjects"]
        if fo not in pos:
            res.bad(label, "first_object", "not an object of the file")
            continue
        run = order[pos[fo]:pos[fo] + n]
        if len(run) != n:
            res.bad(label, "nobjects", "runs past the file")
            continue
        if any(owner not in users.get(m, ()) for m in run):
            res.bad(label, "nobjects", "the group holds an object its key does not reach")
        reached = {m for m, us in users.items() if owner in us and m in objects}
        outside = reached - set(run) - in_groups
        if outside:
            res.bad(label, "nobjects", "the key reaches objects in neither the group nor a "
                    "shared group", str(len(outside)))
        # qpdf's checkHOutlines records Acrobat writing the next object's
        # offset in /O; no corpus file does, so neither profile takes it.
        if _adj(lay, g["first_object_offset"]) != objects[fo].offset:
            res.bad(label, "first_object_offset", "mismatch",
                    _offset_hint(lay, g["first_object_offset"], objects[fo].offset))
        if g["group_length"] != sum(objects[m].extent for m in run):
            res.bad(label, "group_length", "mismatch")
        if "identifiers" in g:
            ids = g["identifiers"]
            if g["nbits_identifier"] != nbits(max(ids, default=0)):
                res.bad(label, "nbits_identifier", "not minimal")
            want = sorted(i for i, m in enumerate(group_objs)
                          if owner in users.get(m, ()) and len(users.get(m, ())) > 1)
            if any(x >= len(group_objs) for x in ids):
                res.bad(label, "identifiers", "identifier past the table")
            elif sorted(ids) != want:
                res.bad(label, "identifiers", "not the shared groups the key reaches")
            elif profile == "qpdf":
                if ids != want:
                    res.bad(label, "identifiers", "order: not ascending")
            else:
                gi = {m: i for i, m in enumerate(group_objs)}
                root = doc.get(doc.ref(lay.root))
                key = _GENERIC_KEY[name]
                start = root.get(key) if isinstance(root, PdfDict) else None
                found = [gi[m] for m in doc.discovery(start) if m in gi and gi[m] in set(ids)]
                if found == ids:
                    res.rule("A-form-order")
                else:
                    res.bad(label, "identifiers", "order: not the observed rule's")


def _group_objects(lay: Layout, t: Tables) -> list[int]:
    """The objects the shared table's groups stand for, as read from the
    table itself (first-page groups from the first-page offset, then part
    8 from first_shared_obj): the table's own lengths were checked above."""
    objects, order = lay.objects, lay.order
    by_off = {objects[n].offset: n for n in order}
    start = by_off.get(_adj(lay, t.page["first_page_offset"]))
    out: list[int] = []
    if start is not None:
        i = order.index(start)
        out = order[i:i + t.shared["nshared_first_page"]]
    rest = t.shared["nshared_total"] - t.shared["nshared_first_page"]
    if rest > 0 and t.shared["first_shared_obj"] in objects:
        i = order.index(t.shared["first_shared_obj"])
        out += order[i:i + rest]
    return out


def _root_target(lay: Layout, key: bytes) -> int | None:
    """The object number the catalog's *key* names, when it is a reference."""
    values = Values(lay.data, lay.chain.object_map(len(lay.chain.revisions) - 1), lay.objects)
    root = values.get(PdfRef(0, 0, lay.root, lay.objects[lay.root].gen))
    if not isinstance(root, PdfDict):
        return None
    v = root.get(key)
    if key == b"Names" and isinstance(v, PdfRef):
        names = values.get(v)
        ef = names.get(b"EmbeddedFiles") if isinstance(names, PdfDict) else None
        return ef.num if isinstance(ef, PdfRef) else None
    return v.num if isinstance(v, PdfRef) else None


def _check_lindict(lay: Layout, sect0: list[int], res: Result) -> None:
    """Every key of the linearization dictionary, against the layout."""
    lin = lay.lin
    allowed = {b"Linearized", b"L", b"H", b"O", b"E", b"N", b"T", b"P"}
    for name, _ in lin.entries:
        if name.raw not in allowed:
            res.bad("linearization dictionary", f"/{name.raw.decode('latin-1')}", "unknown key")
    if lin.ambiguous:
        res.bad("linearization dictionary", "keys", "repeated key")
    if _int(lin.get(b"L")) != len(lay.data):
        res.bad("linearization dictionary", "/L", "not the linearized pair's length")
    if _int(lin.get(b"O")) != lay.pages[0]:
        res.bad("linearization dictionary", "/O", "not the first page's object")
    if _int(lin.get(b"N")) != len(lay.pages):
        res.bad("linearization dictionary", "/N", "not the page count")
    p = lin.get(b"P")
    if p is not None and _int(p) != 0:
        res.bad("linearization dictionary", "/P", "not 0")
    v = lin.get(b"Linearized")
    if not (isinstance(v, PdfInt) and v.value == 1) and not (
            hasattr(v, "value") and getattr(v, "value", None) == 1.0):
        res.bad("linearization dictionary", "/Linearized", "not 1")
    end = sect0[-1]
    e_end = lay.objects[end].offset + lay.objects[end].extent
    e = _int(lin.get(b"E"))
    if e == e_end:
        pass
    elif e is not None and e_end - _trailing_ws(lay.data, e_end) <= e < e_end:
        res.bad("linearization dictionary", "/E", "inside the whitespace after endobj")
    else:
        res.bad("linearization dictionary", "/E", "not the end of the first-page section")
    t = _int(lin.get(b"T"))
    if t is None or not t < lay.first_xref_item or lay.data[t:lay.first_xref_item].strip(_WS):
        res.bad("linearization dictionary", "/T", "not before the main xref's first entry")
    elif t != lay.first_xref_item - 1:
        res.bad("linearization dictionary", "/T", "earlier than the last whitespace byte")


def account(data: bytes) -> Result:
    """Decode one file's hint stream and account for it under each writer
    profile; the file is accounted when one whole profile accounts for
    every value. The result is that profile's, else the profile with the
    fewest findings (ties: Acrobat's, the corpus's writer)."""
    base = Result()
    try:
        lay = build_layout(data)
        tables = decode_stream(lay, base)
    except Stop as e:
        base.error = str(e)
        return base
    except (IndexError, KeyError, ValueError, RecursionError) as e:
        base.error = f"decoder error: {type(e).__name__}"
        return base
    results = []
    for profile in ("acrobat", "qpdf"):
        res = Result(list(base.findings), Counter(base.rules), sorted(tables.spans))
        res.profile = profile
        try:
            check(lay, tables, res, profile)
        except Stop as e:
            res.error = str(e)
        except (IndexError, KeyError, ValueError, RecursionError) as e:
            res.error = f"decoder error: {type(e).__name__}"
        results.append(res)
    return min(results, key=lambda r: (not r.accounted, r.error is not None, len(r.findings)))


# ── fabricated fixtures ──────────────────────────────────────────────────

def fabricated(npages: int, outlines: bool = True) -> bytes:
    """A fabricated document linearized by qpdf (through pikepdf), hint
    stream uncompressed so bits can be hidden in place: *npages* pages
    sharing one font, each with a private graphics state, an /Info, and
    optionally an outline (qpdf then writes the /O table)."""
    import pikepdf

    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                                                Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    # Shared by the later pages only: qpdf puts it in part 8.
    later = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                                                 Subtype=pikepdf.Name.Type1,
                                                 BaseFont=pikepdf.Name.Courier))
    for i in range(npages):
        gs = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.ExtGState, CA=1))
        fonts = pikepdf.Dictionary(F1=font)
        text = f"/G0 gs BT /F1 12 Tf 72 720 Td (Fabricated page {i + 1}) Tj ET"
        if i:
            fonts.F2 = later
            text += " BT /F2 10 Tf 72 700 Td (Fabricated) Tj ET"
        content = pikepdf.Stream(pdf, text.encode())
        page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
                                  Contents=pdf.make_indirect(content),
                                  Resources=pikepdf.Dictionary(
                                      Font=fonts, ExtGState=pikepdf.Dictionary(G0=gs)))
        pdf.pages.append(pikepdf.Page(page))
    pdf.docinfo["/Title"] = "Fabricated hint-table fixture"
    if outlines:
        with pdf.open_outline() as outline:
            outline.root.append(pikepdf.OutlineItem("Fabricated", 0))
    out = io.BytesIO()
    pdf.save(out, linearize=True, compress_streams=False, deterministic_id=True,
             object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return out.getvalue()


def _hint_data(data: bytes) -> tuple[int, Tables]:
    """Where the (uncompressed) hint stream's data starts, and its tables."""
    lay = build_layout(data)
    obj = ObjectParser(lay.data, budget=Budget()).parse_indirect_at(lay.hint_offset)
    if obj is None or obj.stream is None:
        raise Stop("no hint stream")
    return obj.stream.data.start, decode_stream(lay, Result())


def _set_bits(data: bytes, bitpos: int, width: int, value: int) -> bytes:
    buf = bytearray(data)
    for i in range(width):
        bit = (value >> (width - 1 - i)) & 1
        p = bitpos + i
        if bit:
            buf[p >> 3] |= 0x80 >> (p & 7)
        else:
            buf[p >> 3] &= ~(0x80 >> (p & 7)) & 0xFF
    return bytes(buf)


def mutations(data: bytes) -> dict[str, bytes]:
    """Hide bits in a fabricated file's hint stream, in place (the
    stream is uncompressed, so no offset moves): in padding, in a field,
    in unused entries, in a value no layout fixes."""
    start, t = _hint_data(data)
    bit = start * 8
    s_bit = bit + 8 * t.spans["S"][0]
    out: dict[str, bytes] = {}
    if t.pads:
        pos, width = t.pads[0]
        out["padding: one bit after a page-offset column"] = _set_bits(
            data, bit + pos + width - 1, 1, 1)
    out["field: min_page_length + 1"] = _set_bits(data, bit + 32 + 32 + 16, 32,
                                                  t.page["min_page_length"] + 1)
    out["unused: min_content_offset (note 126) = 0x5a"] = _set_bits(data, bit + 128, 32, 0x5A)
    out["free value: shared_denominator = 0x1234"] = _set_bits(data, bit + 272, 16, 0x1234)
    if t.shared["nshared_total"] == t.shared["nshared_first_page"]:
        out["unused: first_shared_obj with no part 8 (note 131)"] = _set_bits(data, s_bit, 32, 7)
    else:
        out["field: first_shared_offset + 1"] = _set_bits(
            data, s_bit + 32, 32, t.shared["first_shared_offset"] + 1)
    if "O" in t.spans:
        o_bit = bit + 8 * t.spans["O"][0]
        out["field: outline group_length + 2"] = _set_bits(
            data, o_bit + 96, 32, t.generic["O"]["group_length"] + 2)
    # The other profile's reading of single values (review #180, b1): each
    # alone, and every combination, must fail, since a file matches one
    # whole profile.
    lay = build_layout(data)
    alt: dict[str, tuple[int, int, int]] = {
        "min_content_length 0 (Acrobat's, note 127)": (bit + 176, 32, 0),
        "shared_denominator 8 (Acrobat's)": (bit + 272, 16, 8),
    }
    if t.shared["nshared_total"] > t.shared["nshared_first_page"]:
        alt["first_shared_offset as a file offset"] = (
            s_bit + 32, 32, t.shared["first_shared_offset"] + lay.hint_length)
    if "O" in t.spans:
        alt["/O first_object_offset as a file offset"] = (
            bit + 8 * t.spans["O"][0] + 32, 32,
            t.generic["O"]["first_object_offset"] + lay.hint_length)
    names = sorted(alt)
    for size in range(1, len(names) + 1):
        for combo in itertools.combinations(names, size):
            m = data
            for name in combo:
                m = _set_bits(m, *alt[name])
            out["other profile: " + " + ".join(combo)] = m
    return out


def run_fixtures() -> dict[str, Any]:
    report: dict[str, Any] = {}
    for label, npages, outlines in (("three pages, outline", 3, True),
                                    ("one page, no outline", 1, False)):
        data = fabricated(npages, outlines)
        clean = account(data)
        caught = {}
        for name, mutated in mutations(data).items():
            r = account(mutated)
            caught[name] = {"caught": not r.accounted,
                            "by": sorted({f"{f.table}: {f.field}: {f.klass}" for f in r.findings})
                            or ([r.error] if r.error else [])}
        report[label] = {"clean_accounted": clean.accounted,
                         "clean_findings": [f"{f.table}: {f.field}: {f.klass}"
                                            for f in clean.findings],
                         "clean_rules": sorted(clean.rules), "mutations": caught}
    return report


# ── the corpus ───────────────────────────────────────────────────────────

def measure(path: str) -> dict[str, Any]:
    """One file's record: no values, no names -- findings by table, field
    and class, the rules used, the tables present."""
    res = account(Path(path).read_bytes())
    return {"accounted": res.accounted, "error": res.error, "tables": res.tables,
            "profile": res.profile, "rules": sorted(res.rules),
            "findings": sorted({(f.table, f.field, f.klass) for f in res.findings})}


def populations(records: Path, manifest: list[Any]) -> dict[str, list[str]]:
    """The 3a-12 populations, per manifest entry (a file under two paths
    counts twice, as 3a-12 counts): every unflagged hint-stream file; the
    go/no-go set (qpdf warns, text-bearing, newly exit 2); the stale-/L
    updated text-bearing new ones."""
    def load(name: str) -> dict[str, dict[str, Any]]:
        out = {}
        for line in (records / name).read_text().splitlines():
            row = json.loads(line)
            out[row.pop("id")] = row
        return out

    units, cli, levers = load("units.jsonl"), load("cli.jsonl"), load("levers.jsonl")
    pops: dict[str, list[str]] = {"all": [], "go": [], "stale": [], "warned": []}
    for e in manifest:
        k = e.sha256[:16]
        u = units.get(k, {})
        if ("error" in u or u.get("flags") or not u.get("classes", {}).get("hint_stream")
                or k not in cli):
            continue
        lv = levers.get(k, {})
        new = cli[k].get("exit") == 0
        pops["all"].append(k)
        if lv.get("qpdf_linearization") == 3:
            pops["warned"].append(k)
            if e.text_bearing and new:
                pops["go"].append(k)
        if lv.get("hint_stale") and e.text_bearing and new:
            pops["stale"].append(k)
    return pops


OBSERVED_CONTENT_RULES = {
    "A-page0-entries": ("page offset: page 0 entries: not the observed rule's",),
    "A-later-numerators": ("page offset: page N numerators: not the observed rule's",),
    "A-later-order": ("page offset: page N identifiers: order: not the observed rule's",),
    "A-form-order": ("/V table: identifiers: order: not the observed rule's",
                     "/C table: identifiers: order: not the observed rule's"),
}


def half(key: str) -> str:
    """The held-out split, fixed before any rule was derived: a distinct
    file's SHA-256 prefix, even -> derive, odd -> held out."""
    return "derive" if int(key, 16) % 2 == 0 else "held_out"


def _classes(row: dict[str, Any]) -> set[str]:
    return {f"{t}: {f}: {c}" for t, f, c in row["findings"]}


def summarize(keys: list[str], rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    present = [k for k in keys if k in rows]
    by_class: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    for k in present:
        r = rows[k]
        by_class.update(_classes(r))
        if r["error"]:
            by_class["error: " + r["error"]] += 1
        rules.update(r["rules"])

    def observed(k: str) -> bool:
        return any(RULES[name][1] == "observed" for name in rows[k]["rules"])

    acc = [k for k in present if rows[k]["accounted"]]
    out: dict[str, Any] = {
        "files": len(present),
        "distinct": len(set(present)),
        "accounted": len(acc),
        "accounted_documented_rules_only": sum(1 for k in acc if not observed(k)),
        "accounted_with_observed_rules": sum(1 for k in acc if observed(k)),
        "accounted_by_profile": dict(Counter(str(rows[k]["profile"]) for k in acc)),
        "not_accounted": len(present) - len(acc),
        "errors": sum(1 for k in present if rows[k]["error"]),
        "findings_by_class": dict(by_class.most_common()),
        "rules_used": dict(rules.most_common()),
    }
    # Held-out discipline, once per distinct file: each observed content
    # rule's exact-match rate on the half it was derived on and the other.
    split: dict[str, str] = {}
    distinct = sorted(set(present))
    for name, fails in OBSERVED_CONTENT_RULES.items():
        for h in ("derive", "held_out"):
            ks = [k for k in distinct if half(k) == h]
            miss = [k for k in ks if _classes(rows[k]) & set(fails)]
            hit = [k for k in ks if name in rows[k]["rules"] and k not in miss]
            split[name + " " + h] = f"{len(hit)} of {len(hit) + len(miss)}"
    for h in ("derive", "held_out"):
        ks = [k for k in distinct if half(k) == h]
        n = sum(1 for k in ks if rows[k]["accounted"])
        split["fully accounted, distinct, " + h] = f"{n} of {len(ks)}"
    out["held_out"] = split
    return out


def main(argv: list[str] | None = None) -> int:
    from scorecard import corpus as corpus_mod

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=("run", "report", "fixtures"))
    parser.add_argument("--root", type=Path, help="the corpus root (run)")
    parser.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    parser.add_argument("--records", type=Path, help="3a-12's unit_budget.py --out directory")
    parser.add_argument("--out", type=Path, default=corpus_mod.REAL_CORPUS_DIR / "hint-tables")
    parser.add_argument("--allow-outside", action="store_true",
                        help="--out outside the gitignored real_corpus/ (keep it local)")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if args.step == "fixtures":
        print(json.dumps(run_fixtures(), indent=2, sort_keys=True))
        return 0
    out = corpus_mod.ensure_local_only(args.out, allow_outside=args.allow_outside)
    out.mkdir(parents=True, exist_ok=True)
    manifest = corpus_mod.load_manifest(args.manifest)
    if args.records is None:
        parser.error("--records is required")
    pops = populations(args.records, manifest)
    target = out / "hint_tables.jsonl"
    if args.step == "run":
        if args.root is None:
            parser.error("--root is required for run")
        done: set[str] = set()
        if target.exists():
            done = {json.loads(line)["id"] for line in target.read_text().splitlines()}
        paths = {e.sha256[:16]: str(args.root / e.path) for e in manifest}
        todo = sorted(set(pops["all"]) - done)
        with ProcessPoolExecutor(args.workers) as pool:
            for key, row in zip(todo, pool.map(measure, [paths[k] for k in todo], chunksize=2)):
                with target.open("a") as f:
                    f.write(json.dumps({"id": key, **row}, sort_keys=True) + "\n")
        print(f"run: {len(todo)} file(s) recorded in {target.name}", file=sys.stderr)
        return 0
    rows = {}
    for line in target.read_text().splitlines():
        row = json.loads(line)
        row["findings"] = [tuple(f) for f in row["findings"]]
        rows[row.pop("id")] = row
    print(json.dumps({name: summarize(keys, rows) for name, keys in pops.items()},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
