"""build_inventory (Phase 3a-5): every byte of a file attributed to a
unit or flagged, every object's body found revision by revision -- and,
readers being the authority (ADR 0010), every unflagged inventory agrees
with MuPDF and qpdf: per revision, the object set, each object's stream
data (MuPDF's xref_stream_raw) and each object-stream member's value
(qpdf --show-object, MuPDF xref_object); and the dead bodies are exactly
the ``N G obj`` bodies neither reader lists.

``pdf_files()`` generates structurally varied files (classic, xref
stream and hybrid sections, incremental updates, object streams, dead
bodies, comments and junk in gaps); the example counts are small in CI,
set DIFF_FUZZ_EXAMPLES for a long local run. RUN_INVENTORY_CASES=1 runs
the differential over the whole case library (slow: every case built).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import zlib
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pymupdf
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from redaction_verifier.budget import Budget, Counter, Limits
from redaction_verifier.inventory import Inventory, build_inventory, check_tiling
from redaction_verifier.inventory.flate import flate_decode
from redaction_verifier.inventory.objects import (ObjectParser, PdfArray, PdfDict, PdfName,
                                                   PdfNull, PdfRef, PdfValue)
from redaction_verifier.ledger import FlagReason, Unit, UnitKind, UnitRef

from .conftest import REPO_ROOT, requires_qpdf
from .test_inventory_differential import _plain
from .test_inventory_xref import BODIES, _PAGE_TREE_SEMANTICS, _objects, _qpdf_map, chain_of

EXAMPLES = int(os.environ.get("DIFF_FUZZ_EXAMPLES", "30"))
MARKER = b"%\xe2\xe3\xcf\xd3"


def inventory(data: bytes, limits: Limits | None = None) -> Inventory:
    return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)))


def reasons(data: bytes) -> list[str]:
    return [flag.reason.name for flag in inventory(data).flags]


# ── A small PDF writer: every offset right by construction ────────────────
class Writer:
    def __init__(self, comment: bool = False) -> None:
        self.out = bytearray(b"%PDF-1.7\n" + MARKER + b"\n")
        self.comment = comment
        if comment:
            self.out += b"% Written by a test\n"

    def at(self) -> int:
        return len(self.out)

    def obj(self, num: int, body: bytes) -> int:
        at = self.at()
        self.out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
        return at

    def stream(self, num: int, extra: bytes, data: bytes) -> int:
        return self.obj(num, b"<< /Length %d%s >>\nstream\n" % (len(data), extra)
                        + data + b"\nendstream")

    def objstm(self, num: int, members: list[tuple[int, bytes]],
               mutate: Callable[[bytes], bytes] | None = None, header: bytes | None = None,
               filters: bytes = b" /Filter /FlateDecode", count: int | None = None) -> int:
        offsets, payload = [], b""
        for number, body in members:
            offsets.append(b"%d %d" % (number, len(payload)))
            payload += body + b"\n"
        table = header if header is not None else b" ".join(offsets)
        packed = table + b"\n" + payload
        if mutate is not None:
            packed = mutate(packed)
        data = zlib.compress(packed) if filters else packed
        return self.stream(num, b" /Type /ObjStm /N %d /First %d%s" % (
            len(members) if count is None else count, len(table) + 1, filters), data)

    def table(self, entries: dict[int, tuple[int, int, int]], trailer: bytes) -> int:
        at = self.at()
        self.out += b"xref\n"
        numbers = sorted(entries)
        runs: list[list[int]] = []
        for n in numbers:
            if runs and runs[-1][-1] + 1 == n:
                runs[-1].append(n)
            else:
                runs.append([n])
        for run in runs:
            self.out += b"%d %d\n" % (run[0], len(run))
            for n in run:
                kind, a, b = entries[n]
                self.out += b"%010d %05d %s \n" % (a, b, b"n" if kind == 1 else b"f")
        self.out += b"trailer\n<< " + trailer + b" >>\n"
        return at

    def xref_stream(self, num: int, entries: dict[int, tuple[int, int, int]],
                    trailer: bytes, predictor: bool = False) -> int:
        at = self.at()
        entries = entries | {num: (1, at, 0)}
        rows = [bytes([k]) + a.to_bytes(4, "big") + b.to_bytes(2, "big")
                for _, (k, a, b) in sorted(entries.items())]
        parms = b""
        if predictor:
            rows = [b"\x00" + r for r in rows]
            parms = b" /DecodeParms << /Predictor 12 /Columns 7 >>"
        index = b" ".join(b"%d 1" % n for n in sorted(entries))
        self.stream(num, b" /Type /XRef /W [1 4 2] /Index [%s] /Filter /FlateDecode%s %s" % (
            index, parms, trailer), zlib.compress(b"".join(rows)))
        return at

    def epilogue(self, section: int) -> None:
        self.out += b"startxref\n%d\n%%%%EOF\n" % section


@dataclass(frozen=True)
class Update:
    xref: str = "table"          # "table" or "stream"
    objstm: bool = False         # the new page dictionary in a new object stream
    dead: bool = False           # a dead body among the update's objects


@dataclass(frozen=True)
class Spec:
    pages: int = 1
    xref: str = "table"          # "table", "stream" or "hybrid"
    objstm: bool = False         # page tree (and, but for hybrids, the catalog) compressed
    predictor: bool = False
    updates: tuple[Update, ...] = ()
    dead: bool = False
    comment: bool = False        # "% Written by ..." after the header and each %%EOF
    junk: bytes = b""            # bytes between two objects: flagged unless whitespace
    probe: bool = False          # PROBE as object 50 in the object stream, and object 51
    mutate: Callable[[bytes], bytes] | None = field(default=None, compare=False)


PROBE = b"<< /Probe (SSN 123-45-6789) /L [1 2.5 /N <41>] /D << /K true >> >>"
# Nothing refers to the probes: mutating them cannot wake qpdf's page-tree
# repair (3a-7's), only what this PR owns.
PROBE_STREAM = (b"<< /Probe (SSN 123-45-6789) /L [1 2.5 /N <41>] /Length 11 >>\n"
                b"stream\nhello world\nendstream")


def build_pdf(spec: Spec) -> bytes:
    w = Writer(spec.comment)
    pages = [3 + i for i in range(spec.pages)]
    contents = [3 + spec.pages + i for i in range(spec.pages)]
    kids = b" ".join(b"%d 0 R" % p for p in pages)
    dicts = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
             2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, spec.pages)}
    for page, content in zip(pages, contents):
        dicts[page] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                       b" /Resources << >> /Contents %d 0 R >>" % content)
    entries: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for i, content in enumerate(contents):
        entries[content] = (1, w.stream(content, b"", b"BT (page %d) Tj ET" % i), 0)
        if i == 0 and spec.junk:
            w.out += spec.junk
    if spec.dead:
        w.obj(90, b"<< /Dead (SSN 123-45-6789) >>")
    if spec.probe:
        entries[51] = (1, w.obj(51, PROBE_STREAM), 0)
    next_num = max(3 + 2 * spec.pages, 52 if spec.probe else 0)
    compressed = [n for n in dicts if spec.objstm and (spec.xref == "stream" or n != 1)]
    for n, body in dicts.items():
        if n not in compressed:
            entries[n] = (1, w.obj(n, body), 0)
    if compressed:
        stm = next_num
        next_num += 1
        members = [(n, dicts[n]) for n in compressed] + ([(50, PROBE)] if spec.probe else [])
        entries[stm] = (1, w.objstm(stm, members, spec.mutate), 0)
        for i, (n, _) in enumerate(members):
            entries[n] = (2, stm, i)
    xref_num = next_num
    next_num += 1
    size = next_num
    if spec.xref == "table":
        section = w.table(entries, b"/Size %d /Root 1 0 R" % (max(entries) + 1))
    elif spec.xref == "stream":
        section = w.xref_stream(xref_num, entries, b"/Size %d /Root 1 0 R" % size,
                                spec.predictor)
    else:
        streamed = {n: e for n, e in entries.items() if e[0] == 2}
        stm_at = w.xref_stream(xref_num, streamed, b"/Size %d" % size, spec.predictor)
        table = {n: e for n, e in entries.items() if e[0] != 2}
        section = w.table(table, b"/Size %d /Root 1 0 R /XRefStm %d" % (size, stm_at))
    w.epilogue(section)
    for k, update in enumerate(spec.updates):
        if spec.comment:  # as MuPDF begins each update
            w.out += b"\n% Written by a test\n\n"
        changed: dict[int, tuple[int, int, int]] = {}
        content = next_num
        next_num += 1
        changed[content] = (1, w.stream(content, b"", b"BT (update %d) Tj ET" % k), 0)
        if update.dead:
            w.obj(91 + k, b"<< /Dead (update %d) >>" % k)
        page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300]"
                b" /Resources << >> /Contents %d 0 R >>" % content)
        if update.objstm and update.xref == "stream":
            stm = next_num
            next_num += 1
            changed[stm] = (1, w.objstm(stm, [(pages[0], page)]), 0)
            changed[pages[0]] = (2, stm, 0)
        else:
            changed[pages[0]] = (1, w.obj(pages[0], page), 0)
        if update.xref == "stream":
            xref_num = next_num
            next_num += 1
            size = next_num
            section_at = w.xref_stream(xref_num, changed, b"/Size %d /Root 1 0 R /Prev %d" % (
                size, section))
        else:
            size = max(size, next_num)
            section_at = w.table(changed, b"/Size %d /Root 1 0 R /Prev %d" % (size, section))
        section = section_at
        w.epilogue(section)
    return bytes(w.out)


_UPDATES = st.builds(Update, xref=st.sampled_from(["table", "stream"]), objstm=st.booleans(),
                     dead=st.booleans())
_JUNK = st.sampled_from([b"", b"", b" \n", b"% a comment\n", b"junk", b"(", b"<<",
                         b"12 0 obj (x) endobj\n", b"1 0 obj", b"endstream", b"\xff\x00"])


@st.composite
def pdf_files(draw: st.DrawFn, junk: bool = True) -> tuple[Spec, bytes]:
    """Structurally varied files. With *junk*, some gap holds junk (then
    the file is flagged, unless it is whitespace or a dead body)."""
    xref = draw(st.sampled_from(["table", "stream", "hybrid"]))
    spec = Spec(pages=draw(st.integers(1, 3)), xref=xref,
                objstm=draw(st.booleans()) if xref != "table" else False,
                predictor=draw(st.booleans()), updates=tuple(draw(st.lists(_UPDATES, max_size=3))),
                dead=draw(st.booleans()), comment=draw(st.booleans()),
                junk=draw(_JUNK) if junk else b"")
    return spec, build_pdf(spec)


# ── Canonical shapes: no flag, every byte attributed ──────────────────────
CANONICAL = [Spec(), Spec(xref="stream"), Spec(xref="stream", predictor=True),
             Spec(xref="stream", objstm=True), Spec(xref="hybrid", objstm=True),
             Spec(pages=3, updates=(Update(), Update("stream"), Update("stream", objstm=True))),
             Spec(xref="stream", objstm=True, updates=(Update("stream", objstm=True),)),
             Spec(comment=True, updates=(Update(),))]


@pytest.mark.parametrize("spec", CANONICAL)
def test_canonical_files_inventory_without_a_flag(spec: Spec) -> None:
    data = build_pdf(spec)
    inv = inventory(data)
    assert inv.flags == (), [(f.reason.name, f.params) for f in inv.flags]
    assert inv.tiles and inv.revisions == 1 + len(spec.updates)
    kinds = {r.kind for r in inv.tiling.regions}
    assert kinds <= {UnitKind.HEADER, UnitKind.OBJECT, UnitKind.XREF_TABLE,
                     UnitKind.XREF_EPILOGUE, UnitKind.WHITESPACE}
    # Every revision's object map has a body for every object in use.
    chain = chain_of(data)
    for revision in range(inv.revisions):
        live = {n for n, e in chain.object_map(revision).items() if e.kind != 0}
        assert set(inv.bodies_by_number(revision)) == live


def test_every_revision_epilogue_is_claimed_and_must_name_its_revision() -> None:
    data = build_pdf(Spec(updates=(Update(), Update())))
    epilogues = [u for u in inventory(data).units if u.ref.kind is UnitKind.XREF_EPILOGUE]
    assert len(epilogues) == 3
    first = re.search(rb"startxref\n(\d+)", data)
    assert first is not None
    wrong = b"startxref\n%0*d" % (len(first.group(1)), int(first.group(1)) - 1)
    bad = data.replace(first.group(0), wrong, 1)
    assert "XREF_EPILOGUE_MISMATCH" in reasons(bad)


def test_the_header_and_comment_lines_after_it_are_the_header_unit() -> None:
    data = build_pdf(Spec(comment=True))
    (header,) = [u for u in inventory(data).units if u.ref.kind is UnitKind.HEADER]
    assert [data[s.start:s.end] for s in header.spans] == [
        b"%PDF-1.7", MARKER, b"% Written by a test"]
    # A comment anywhere else is unclaimed: flagged.
    assert "UNINDEXED_NON_WHITESPACE" in reasons(build_pdf(Spec(junk=b"% a comment\n")))
    # So is anything else on the header line (same length: offsets stay).
    assert "UNINDEXED_NON_WHITESPACE" in reasons(
        build_pdf(Spec()).replace(b"%PDF-1.7\n", b"%PDF-1.7x", 1))


def test_a_dead_body_is_claimed_and_junk_is_flagged() -> None:
    data = build_pdf(Spec(dead=True))
    inv = inventory(data)
    assert inv.flags == ()
    (dead,) = [u for u in inv.units if u.ref.kind is UnitKind.DEAD_BODY]
    assert (dead.ref.obj, data[dead.spans[0].start:dead.spans[0].end][:6]) == (90, b"90 0 o")
    assert 90 not in inv.bodies_by_number()
    flagged = inventory(build_pdf(Spec(junk=b"junk")))
    assert [f.reason.name for f in flagged.flags] == ["UNINDEXED_NON_WHITESPACE"]
    assert flagged.tiles


def test_a_dead_body_in_a_gap_is_parsed_within_the_gap_only() -> None:
    # An unterminated dead body stops at its gap's end, never in the next
    # object; the next object is still read whole.
    inv = inventory(build_pdf(Spec(junk=b"12 0 obj (never closed\n")))
    (dead,) = [u for u in inv.units if u.ref.kind is UnitKind.DEAD_BODY]
    assert "UNTERMINATED" in {f.reason.name for f in dead.flags}
    assert set(inv.bodies_by_number()) >= {1, 2, 3, 4}
    assert inv.tiles


# ── Object streams ────────────────────────────────────────────────────────
def test_an_object_stream_is_tiled_in_decoded_coordinates() -> None:
    data = build_pdf(Spec(xref="stream", objstm=True))
    inv = inventory(data)
    ((offset, stream),) = inv.object_streams.items()
    assert inv.objects[offset] is stream.ref
    assert [r.kind for r in stream.tiling.regions if r.kind is not UnitKind.WHITESPACE] == [
        UnitKind.OBJSTM_HEADER] + [UnitKind.OBJSTM_MEMBER] * 3
    assert check_tiling(stream.tiling.size, (r.span for r in stream.tiling.regions))
    assert [m.obj for m in stream.members if m is not None] == [1, 2, 3]
    assert inv.body(1) is stream.members[0]
    members = [u for u in inv.units if u.ref.kind is UnitKind.OBJSTM_MEMBER]
    assert all(u.ref.within is stream.ref for u in members)


def _objstm_file(members: list[tuple[int, bytes]], entries: dict[int, tuple[int, int, int]],
                 header: bytes | None = None, filters: bytes = b" /Filter /FlateDecode",
                 root: int = 1, count: int | None = None) -> bytes:
    """Catalog 1 (or as *entries* say), pages 2, page 3 in the file;
    object stream 4 holds *members*; *entries* adds compressed ones."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in ((1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                    (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                    (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] /Resources << >> >>")):
        if n not in entries:
            table[n] = (1, w.obj(n, body), 0)
    table[4] = (1, w.objstm(4, members, header=header, filters=filters, count=count), 0)
    table |= entries
    xref = max(table) + 1
    section = w.xref_stream(xref, table, b"/Size %d /Root %d 0 R" % (xref + 1, root))
    w.epilogue(section)
    return bytes(w.out)


CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"


def test_a_clean_object_stream_file_has_no_flag() -> None:
    data = _objstm_file([(1, CATALOG), (6, b"(SSN 123-45-6789)")],
                        {1: (2, 4, 0), 6: (2, 4, 1)})
    assert reasons(data) == []


@pytest.mark.parametrize(("members", "entries", "expected"), [
    # The entry's index lists another object: the home does not hold it.
    ([(1, CATALOG), (6, b"(x)")], {1: (2, 4, 0), 6: (2, 4, 0)}, "OBJSTM_ENTRY_MISMATCH"),
    # An index past the header's N.
    ([(1, CATALOG)], {1: (2, 4, 0), 6: (2, 4, 7)}, "OBJSTM_ENTRY_MISMATCH"),
    # A compressed /Root that is not a catalog.
    ([(1, b"<< /Type /Pages >>")], {1: (2, 4, 0)}, "MISSING_ROOT"),
    # Members may not be streams, nor object streams themselves.
    ([(1, CATALOG), (6, b"<< /Length 1 >> stream\nx\nendstream")],
     {1: (2, 4, 0), 6: (2, 4, 1)}, "OBJSTM_MEMBER_INVALID"),
    ([(1, CATALOG), (6, b"<< /Type /ObjStm /N 0 /First 0 >>")],
     {1: (2, 4, 0), 6: (2, 4, 1)}, "OBJSTM_MEMBER_INVALID"),
    ([(1, CATALOG), (6, b"")], {1: (2, 4, 0), 6: (2, 4, 1)}, "OBJSTM_MEMBER_INVALID"),
    # Junk between members is unclaimed in the decoded tiling.
    ([(1, CATALOG + b" junk")], {1: (2, 4, 0)}, "UNINDEXED_NON_WHITESPACE"),
])
def test_object_stream_problems_are_flagged(
        members: list[tuple[int, bytes]], entries: dict[int, tuple[int, int, int]],
        expected: str) -> None:
    assert expected in reasons(_objstm_file(members, entries))


@pytest.mark.parametrize("header", [b"1 0 6", b"1 0 x 5", b"1 0 1 3", b"1 5 6 0", b"1 0 6 9999",
                                    b"1 0 %c\n6 5"])
def test_a_malformed_object_stream_header_is_flagged(header: bytes) -> None:
    data = _objstm_file([(1, CATALOG), (6, b"(x)")], {1: (2, 4, 0), 6: (2, 4, 1)},
                        header=header.ljust(len(b"1 0 6 %d" % (len(CATALOG) + 1))))
    assert "OBJSTM_MALFORMED" in reasons(data)
    huge = [(f.reason.name, dict(f.params)) for f in inventory(_huge_objstm(100)).flags]
    assert ("OBJSTM_MALFORMED", {"object": 4, "pair": 101}) in huge


def test_an_object_stream_with_another_filter_is_unsupported() -> None:
    data = _objstm_file([(1, CATALOG)], {1: (2, 4, 0)}, filters=b" /Filter /LZWDecode")
    assert "UNSUPPORTED_FILTER" in reasons(data)
    plain = _objstm_file([(1, CATALOG)], {1: (2, 4, 0)}, filters=b"")
    assert reasons(plain) == []


def replaced_home(same: bool) -> bytes:
    """Revision 1: object 6 in object stream 4. Revision 0 replaces stream
    4 -- with one that still lists 6 at index 0 (*same*), or with object
    7 there -- and never re-lists 6 itself."""
    first = _objstm_file([(1, CATALOG), (6, b"(old)")], {1: (2, 4, 0), 6: (2, 4, 1)})
    w = Writer()
    w.out = bytearray(first)
    prev = int(re.findall(rb"startxref\n(\d+)", first)[-1])
    members = [(1, CATALOG), (6 if same else 7, b"(new)")]
    at = w.objstm(4, members)
    changed = {4: (1, at, 0)} | ({} if same else {7: (2, 4, 1)})
    size = max(chain_of(first).object_map()) + 1
    xref = max(size, max(changed) + 1)
    section = w.xref_stream(xref, changed, b"/Size %d /Root 1 0 R /Prev %d" % (xref + 1, prev))
    w.epilogue(section)
    return bytes(w.out)


def test_a_replaced_home_is_checked_for_every_member_still_living_in_it() -> None:
    assert reasons(replaced_home(same=True)) == []
    flags = [(f.reason.name, dict(f.params)) for f in inventory(replaced_home(False)).flags]
    assert ("OBJSTM_ENTRY_MISMATCH",
            {"object": 6, "stream": 4, "index": 1, "revision": 0}) in flags


def test_a_non_stream_home_is_flagged() -> None:
    # issue #44: a newer revision redefines the object stream as `4 0 obj null`.
    first = _objstm_file([(1, CATALOG), (6, b"(x)")], {1: (2, 4, 0), 6: (2, 4, 1)})
    w = Writer()
    w.out = bytearray(first)
    prev = int(re.findall(rb"startxref\n(\d+)", first)[-1])
    xref = max(chain_of(first).object_map()) + 1
    section = w.xref_stream(xref, {4: (1, w.obj(4, b"null"), 0)},
                            b"/Size %d /Root 1 0 R /Prev %d" % (xref + 1, prev))
    w.epilogue(section)
    assert "OBJSTM_ENTRY_MISMATCH" in reasons(bytes(w.out))


# ── Ambiguity across revisions, and the chain's own shapes (#44) ──────────
def ambiguous_length() -> bytes:
    """Stream 4's /Length is 5 0 R; revision 0 changes object 5 but not 4,
    so object 4 reads with a different extent in each revision."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, CATALOG), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9]"
                          b" /Resources << >> /Contents 4 0 R >>"), 0)
    table[4] = (1, w.obj(4, b"<< /Length 5 0 R >>\nstream\nBT (x) Tj ET\nendstream"), 0)
    table[5] = (1, w.obj(5, b"12"), 0)
    section = w.table(table, b"/Size 6 /Root 1 0 R")
    w.epilogue(section)
    changed = {5: (1, w.obj(5, b"11"), 0)}
    w.epilogue(w.table(changed, b"/Size 6 /Root 1 0 R /Prev %d" % section))
    return bytes(w.out)


def test_an_indirect_length_that_changes_between_revisions_is_flagged() -> None:
    assert "REVISION_AMBIGUOUS" in reasons(ambiguous_length())
    # Fail closed: rewritten with the same value, it is still flagged (a
    # comparison per revision could cost streams x revisions)...
    assert "REVISION_AMBIGUOUS" in reasons(ambiguous_length().replace(b"11", b"12"))
    # ...but an unchanged /Length object resolves.
    one_revision = ambiguous_length()[:ambiguous_length().index(b"%%EOF") + 6]
    assert reasons(one_revision) == []


def linearized_section_in_stream() -> bytes:
    """issue #44: a linearized forward pair whose first-page section lies
    inside object 8's stream data. The chain reads it clean (object 8 is
    whole within the revision) and readers agree on the object maps."""
    preamble = b"%PDF-1.7\n" + MARKER + b"\n"

    def build(at: tuple[int, ...]) -> tuple[bytes, tuple[int, ...]]:
        out = bytearray(preamble)
        out += b"9 0 obj\n<< /Linearized 1 /L %010d >>\nendobj\n" % at[-1]
        eight = len(out)
        section = (b"xref\n3 1\n%010d 00000 n \ntrailer\n<< /Size 10 /Root 1 0 R /Prev %010d >>\n"
                   % (at[4], at[1]))
        data = section + b"BT (SSN 123-45-6789) Tj ET"
        head = b"8 0 obj\n<< /Length %d >>\nstream\n" % len(data)
        first = eight + len(head)
        out += head + data + b"\nendstream\nendobj\n"
        offsets = _objects(BODIES, out)
        main = len(out)
        out += (b"xref\n0 3\n0000000000 65535 f \n%010d 00000 n \n%010d 00000 n \n"
                b"8 2\n%010d 00000 n \n%010d 00000 n \ntrailer\n<< /Size 10 /Root 1 0 R >>\n"
                % (offsets[1], offsets[2], eight, len(preamble)))
        out += b"startxref\n%d\n%%%%EOF\n" % first
        return bytes(out), (first, main, offsets[1], offsets[2], offsets[3], len(out))
    data, at = build((0,) * 6)
    for _ in range(3):  # fixed-width offsets: this converges
        data, at = build(at)
    return data


def test_a_section_inside_an_objects_stream_data_is_flagged() -> None:
    data = linearized_section_in_stream()
    assert chain_of(data).flags == ()  # the chain alone cannot see it
    inv = inventory(data)
    assert inv.tiles and "UNTERMINATED" in {f.reason.name for f in inv.flags}


def test_a_newer_section_inside_an_older_trailer_is_contested() -> None:
    # issue #44: the newer table starts inside a string of the older
    # trailer; a comment in the newer trailer skips the older one's close,
    # so revision ends still increase. The overlap is CONTESTED.
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, CATALOG), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [] /Count 0 >>"), 0)
    older = w.table(table, b"/Size 3 /Root 1 0 R /Pad (")
    w.out = w.out[:-len(b" >>\n")]  # the trailer stays open inside its string
    newer = w.at()
    w.out += (b"xref\n0 1\n0000000000 65535 f \ntrailer\n<< /Size 3 /Root 1 0 R /Prev %d /Q 1 %%"
              % older + b") >>\n>>\n")
    w.epilogue(newer)
    chain = chain_of(bytes(w.out))
    assert len(chain.revisions) == 2 and chain.sections[0].span.start < chain.sections[1].span.end
    assert "CONTESTED_SPAN" in reasons(bytes(w.out))


# ── Never raises; tiles; stays within the work budget ─────────────────────
@settings(max_examples=max(EXAMPLES, 100), suppress_health_check=[HealthCheck.too_slow])
@given(pdf_files())
def test_every_generated_file_inventories_and_tiles(case: tuple[Spec, bytes]) -> None:
    spec, data = case
    budget = Budget(file_size=len(data))
    inv = build_inventory(data, budget=budget)
    assert inv.tiles and inv.tiling.size == len(data)
    assert not budget.exhausted(Counter.WORK)
    assert budget.work <= 16 * len(data) + 1000, budget.work / len(data)
    if not spec.junk.strip():
        assert inv.flags == (), (spec, [(f.reason.name, f.params) for f in inv.flags])


@given(st.binary(max_size=400), st.integers(0, 2000), st.sampled_from(CANONICAL))
def test_never_raises_on_any_bytes(noise: bytes, cut: int, spec: Spec) -> None:
    data = build_pdf(spec)
    for sample in (noise, data[:cut] + noise, data[:cut], noise + data):
        inv = inventory(sample)
        assert inv.tiles


def test_the_budget_is_required() -> None:
    with pytest.raises(TypeError):
        build_inventory(b"%PDF-1.7\n")  # type: ignore[call-arg]


def test_flags_are_capped_per_file() -> None:
    data = build_pdf(Spec(junk=b"".join(b"%d 0 obj endobj\nx\n" % n for n in range(50))))
    found = inventory(data, Limits(max_flags_per_file=3)).flags
    assert len(found) == 4 and found[-1].reason is FlagReason.FLAGS_TRUNCATED
    assert dict(found[-1].params)["limit"] == 3


# ── Differential: every unflagged inventory agrees with the readers ───────
# qpdf --check warnings the inventory does not own: page-tree semantics
# (3a-7's) and a page content stream's own syntax, which --check tokenizes
# ("... stream 5 0 (content, offset 10): unexpected )") -- the content
# decoder's (Phase 4). The stream's raw bytes are still compared below.
_NOT_STRUCTURE = _PAGE_TREE_SEMANTICS + (b"(content, offset ",)


def _catalog_retyped(line: bytes, lines: list[bytes], path: Path) -> bool:
    """qpdf's "catalog /Type entry missing or invalid" when it comes only
    from qpdf's own page-tree repair: the page tree reaches the catalog, qpdf
    overrides its /Type to /Page (a page-tree warning, 3a-7's) -- while
    qpdf's own read of the catalog is a /Type /Catalog dictionary."""
    if b"catalog /Type entry missing or invalid" not in line:
        return False
    trailer = chain_of(path.read_bytes()).sections[0].trailer
    root = trailer.get(b"Root") if trailer is not None else None
    if not isinstance(root, PdfRef):
        return False
    retyped = re.compile(rb"object %d %d at offset \d+: /Type key should be /Page but is not"
                         % (root.num, root.gen))
    shown = subprocess.run(["qpdf", f"--show-object={root.num}", str(path)],
                           capture_output=True, timeout=60)
    value = ObjectParser(shown.stdout).parse_value_at(0).value
    return (any(retyped.search(other) for other in lines) and isinstance(value, PdfDict)
            and isinstance(value.get(b"Type"), PdfName)
            and getattr(value.get(b"Type"), "raw", None) == b"Catalog")


def _without_dangling(value: PdfValue | None, live: dict[int, int]) -> PdfValue | None:
    """A reference to an object with no body in the revision reads as null
    (§7.3.10): qpdf shows null, we keep the reference. Flagging dangling
    references is the reference graph's (3a-7)."""
    if isinstance(value, PdfRef) and live.get(value.num) != value.gen:
        return PdfNull(value.start, value.end)
    if isinstance(value, PdfArray):
        return replace(value, items=tuple(_without_dangling(item, live) or item
                                          for item in value.items))
    if isinstance(value, PdfDict):
        return replace(value, entries=tuple((key, _without_dangling(item, live) or item)
                                            for key, item in value.entries))
    return value


def _value_of(text: bytes, live: dict[int, int], start: int = 0,
              end: int | None = None) -> object:
    return _plain(_without_dangling(ObjectParser(text).parse_value_at(start, end).value, live))


def _units_by_ref(inv: Inventory) -> dict[UnitRef, Unit]:
    return {u.ref: u for u in inv.units}  # refs nest one level deep at most


def _member_value(inv: Inventory, ref: UnitRef, decoded: bytes, live: dict[int, int]) -> object:
    span = _units_by_ref(inv)[ref].spans[0]
    return _value_of(decoded, live, span.start, span.end)


def _revision_blob(data: bytes, revision: int) -> bytes:
    chain = chain_of(data)
    return data if revision == 0 else (
        data[:chain.revision_end(revision)]
        + b"\nstartxref\n%d\n%%%%EOF\n" % chain.revision_start(revision))


def inventory_agrees(data: bytes, tmp: Path, members_per_revision: int = 8) -> bool:
    """False when flagged (readers may disagree then: we said so), else
    assert agreement with qpdf and MuPDF and return True."""
    inv = inventory(data)
    assert inv.tiles
    if inv.flags:
        return False
    pymupdf.TOOLS.mupdf_warnings()
    listed: set[int] = set()
    chain = chain_of(data)
    for revision in range(inv.revisions):
        # Readers decrypt what they read; the inventory does not until 3a-8/9,
        # so an encrypted revision's bytes and values are not compared.
        trailer = chain.sections[chain.revisions[revision][0]].trailer
        encrypted = trailer is not None and trailer.get(b"Encrypt") is not None
        blob = _revision_blob(data, revision)
        path = tmp / f"r{revision}.pdf"
        path.write_bytes(blob)
        qmap, code = _qpdf_map(path)
        assert code == 0, revision
        bodies = inv.bodies_by_number(revision)
        assert set(bodies) == set(qmap), (revision, set(bodies) ^ set(qmap))
        check = subprocess.run(["qpdf", "--check", str(path)], capture_output=True, timeout=60)
        lines = (check.stdout + check.stderr).splitlines()
        structural = [line for line in lines if line.startswith(b"WARNING")
                      and not any(pattern in line for pattern in _NOT_STRUCTURE)
                      and not _catalog_retyped(line, lines, path)]
        assert structural == [], (revision, structural[:3])
        doc = pymupdf.open(stream=blob, filetype="pdf")
        assert not doc.is_repaired, revision
        checked = 0
        live = {n: (0 if kind == "c" else gen) for n, (kind, _, gen) in qmap.items()}
        for number, (kind, a, b) in sorted(qmap.items()):
            body = bodies[number]
            if kind == "u":
                listed.add(a)
                assert (body.kind, body.start) == (UnitKind.OBJECT, a), number
                span = inv.stream_data.get(body.start)
                if span is not None:
                    assert encrypted or (
                        doc.xref_stream_raw(number) == blob[span.start:span.end]), number
                else:
                    assert not doc.xref_is_stream(number), number
                continue
            home = qmap[a]
            stream = inv.object_streams[home[1]]
            assert body.kind is UnitKind.OBJSTM_MEMBER and body.within is stream.ref
            assert stream.members[b] is body, number
            if checked >= members_per_revision or encrypted:
                continue
            checked += 1
            decoded = doc.xref_stream(a)
            raw_span = inv.stream_data[stream.ref.start]
            ours = flate_decode(blob[raw_span.start:raw_span.end])
            assert ours.data == decoded or not ours.complete, a  # unfiltered: not Flate
            mine = _member_value(inv, body, decoded, live)
            mu_text = doc.xref_object(number, compressed=True).encode("latin-1")
            assert _value_of(mu_text, live) == mine, number
            shown = subprocess.run(["qpdf", f"--show-object={number}", str(path)],
                                   capture_output=True, timeout=60)
            assert (_value_of(shown.stdout, live), shown.returncode) == (mine, 0), number
    # Dead bodies: exactly the N G obj headers no reader lists, outside
    # every other unit.
    spans = sorted((s.start, s.end) for u in inv.units
                   if u.ref.within is None and u.ref.kind is not UnitKind.DEAD_BODY
                   for s in u.spans)
    starts = [s for s, _ in spans]

    def inside(pos: int) -> bool:
        i = bisect_right(starts, pos) - 1
        return i >= 0 and spans[i][1] > pos
    dead = sorted((u.spans[0] for u in inv.units if u.ref.kind is UnitKind.DEAD_BODY),
                  key=lambda span: span.start)
    expected = []
    for m in re.finditer(rb"(?<![^\x00\t\n\x0c\r ()<>\[\]{}/%])\d+[\x00\t\n\x0c\r ]+\d+"
                         rb"[\x00\t\n\x0c\r ]+obj(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])", data):
        if m.start() in listed or inside(m.start()):
            continue
        if any(d.start < m.start() < d.end for d in dead):
            continue
        expected.append(m.start())
    assert expected == [d.start for d in dead]
    assert pymupdf.TOOLS.mupdf_warnings() == ""
    return True


@requires_qpdf
@pytest.mark.parametrize("spec", CANONICAL + [Spec(dead=True, updates=(Update(dead=True),))])
def test_canonical_files_agree_with_the_readers(spec: Spec, tmp_path: Path) -> None:
    assert inventory_agrees(build_pdf(spec), tmp_path)


@requires_qpdf
def test_object_stream_edge_cases_agree_or_are_flagged(tmp_path: Path) -> None:
    for data in (replaced_home(same=True), replaced_home(same=False),
                 _objstm_file([(1, CATALOG), (6, b"(SSN 123-45-6789)")],
                              {1: (2, 4, 0), 6: (2, 4, 1)})):
        inventory_agrees(data, tmp_path)
    assert inventory_agrees(replaced_home(same=True), tmp_path)


@requires_qpdf
@settings(max_examples=EXAMPLES, suppress_health_check=[
    HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(pdf_files())
def test_generated_files_are_flagged_or_agree_with_the_readers(
        tmp_path: Path, case: tuple[Spec, bytes]) -> None:
    spec, data = case
    agreed = inventory_agrees(data, tmp_path)
    assert agreed or spec.junk.strip(), spec


def _flip(positions: list[int], values: list[int]) -> Callable[[bytes], bytes]:
    """Overwrite bytes of an object stream's payload, only in its header
    table and its PROBE member: mutating the page tree's members makes
    qpdf's page-tree repair speak (3a-7's), not the object stream's."""
    def mutate(packed: bytes) -> bytes:
        out = bytearray(packed)
        probe = packed.index(PROBE)
        pool = list(range(packed.index(b"\n") + 1)) + list(range(probe, len(packed)))
        for pos, value in zip(positions, values):
            out[pool[pos % len(pool)]] = value
        return bytes(out)
    return mutate


_BYTES = st.sampled_from(list(b"0123456789 \n\r()<>[]/%Rnobjstreamdxulf\x00\xff"))


@requires_qpdf
@settings(max_examples=EXAMPLES * 2, suppress_health_check=[
    HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(pdf_files(junk=False), st.sampled_from(["object", "objstm", "gap"]), st.data())
def test_mutated_files_are_flagged_or_agree_with_the_readers(
        tmp_path: Path, case: tuple[Spec, bytes], where: str, draw: st.DataObject) -> None:
    """Bytes overwritten in objects (content streams and the probe object),
    an object stream's header table and probe member, or the gaps."""
    spec = replace(case[0], probe=True)
    data = build_pdf(spec)
    positions = draw.draw(st.lists(st.integers(0, 10_000), min_size=1, max_size=3))
    values = draw.draw(st.lists(_BYTES, min_size=len(positions), max_size=len(positions)))
    if where == "objstm":
        spec = replace(spec, xref="stream", objstm=True, probe=True,
                       mutate=_flip(positions, values))
        data = build_pdf(spec)
    else:
        inv = inventory(data)
        targets = {51} | set(range(3 + spec.pages, 3 + 2 * spec.pages))  # probe, contents
        wanted = UnitKind.OBJECT if where == "object" else UnitKind.WHITESPACE
        pool = [p for r in inv.tiling.regions if r.kind is wanted
                and (where == "gap" or r.owners[0].obj in targets)
                for p in range(r.span.start, r.span.end)]
        out = bytearray(data)
        for pos, value in zip(positions, values):
            out[pool[pos % len(pool)]] = value
        data = bytes(out)
    inventory_agrees(data, tmp_path)


# ── Member values and bounds (PR #45 review) ──────────────────────────────
_MEMBER_VALUES = [b"null", b"true", b"false", b"3 0 R", b"123", b"-4", b"1.5", b"/Foo",
                  b"/Footrue", b"(x)", b"<41>", b"<< /K 1 >>", b"[1 2]"]


def _members_at(values: list[bytes], seps: list[bytes], head_sep: bytes,
                shifts: list[int], first_shift: int) -> bytes:
    """Object stream 4 holds members 6 and 7 (*values*) then the catalog,
    with the header's offsets and /First shifted off the true bounds."""
    payload, offsets = b"", []
    for value, sep in zip(values + [CATALOG], seps + [b"\n"]):
        offsets.append(len(payload))
        payload += value + sep
    shifted = [max(0, o + d) for o, d in zip(offsets, shifts + [0])]
    header = b"6 %d 7 %d 1 %d" % tuple(shifted)
    packed = header + head_sep + payload
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] "
                            b"/Resources << >> /Annots [6 0 R] >>"), 0)
    table[4] = (1, w.stream(4, b" /Type /ObjStm /N 3 /First %d /Filter /FlateDecode" % max(
        0, len(header) + len(head_sep) + first_shift), zlib.compress(packed)), 0)
    table |= {6: (2, 4, 0), 7: (2, 4, 1), 1: (2, 4, 2)}
    section = w.xref_stream(8, table, b"/Size 9 /Root 1 0 R")
    w.epilogue(section)
    return bytes(w.out)


@pytest.mark.parametrize(("values", "seps", "head_sep", "shifts", "first_shift"), [
    ([b"null", b"1"], [b"\n", b"\n"], b"\n", [0, 0], 0),         # MuPDF: missing, repairs
    ([b"3 0 R", b"1"], [b"\n", b"\n"], b"\n", [0, 0], 0),        # readers: the integer 3
    ([b"123", b"1"], [b"\n", b"\n"], b"", [0, 0], -1),           # /First inside the header
    ([b"123", b"1"], [b"", b"\n"], b"\n", [0, -1], 0),           # `12|3`
    ([b"/Footrue", b"1"], [b"", b"\n"], b"\n", [0, -4], 0),      # `/Foo|true`
])
def test_members_readers_read_otherwise_are_flagged(
        values: list[bytes], seps: list[bytes], head_sep: bytes, shifts: list[int],
        first_shift: int) -> None:
    found = reasons(_members_at(values, seps, head_sep, shifts, first_shift))
    assert {"OBJSTM_MEMBER_INVALID", "OBJSTM_MALFORMED"} & set(found), found


def test_clean_members_at_their_bounds_have_no_flag() -> None:
    assert reasons(_members_at([b"(x)", b"/Foo"], [b"\n", b" "], b"\n", [0, 0], 0)) == []


@requires_qpdf
@settings(max_examples=max(EXAMPLES, 60), suppress_health_check=[
    HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(st.lists(st.sampled_from(_MEMBER_VALUES), min_size=2, max_size=2),
       st.lists(st.sampled_from([b"", b" ", b"\n"]), min_size=2, max_size=2),
       st.sampled_from([b"", b"\n"]), st.lists(st.integers(-3, 3), min_size=2, max_size=2),
       st.integers(-2, 2))
def test_members_at_any_bound_are_flagged_or_agree_with_the_readers(
        tmp_path: Path, values: list[bytes], seps: list[bytes], head_sep: bytes,
        shifts: list[int], first_shift: int) -> None:
    """Top-level scalars (null, a lone `N G R`) and /First or member
    offsets anywhere, mid-token included: flagged, or read as the readers
    read them."""
    inventory_agrees(_members_at(values, seps, head_sep, shifts, first_shift), tmp_path)


# ── Allowlisted leniency: comment lines after the header and %%EOF ────────
@requires_qpdf
def test_comment_lines_after_the_header_and_each_eof_read_alike(tmp_path: Path) -> None:
    # Claimed as HEADER / XREF_EPILOGUE spans without a flag: both readers
    # skip them, and read the file exactly as without them.
    plain = build_pdf(Spec(updates=(Update(),)))
    commented = build_pdf(Spec(comment=True, updates=(Update(),)))
    assert reasons(commented) == []
    assert inventory_agrees(commented, tmp_path)
    for data in (plain, commented):
        doc = pymupdf.open(stream=data, filetype="pdf")
        assert (doc.is_repaired, doc.page_count, doc[0].read_contents()) == (
            False, 1, b"BT (update 0) Tj ET")


# ── The case library ──────────────────────────────────────────────────────
CASE_SUBSET = ["page.memo-linearized", "page.content-stream-objstm", "document.clean-objstm",
               "document.form-field-objstm", "document.clean-incremental",
               "revision.redacted-incremental", "revision.raw-rewritten-stream-objstm",
               "leftover.raw-orphaned-objstm", "page.raw-minimal-objstm",
               "attachment.notes-text", "file.after-final-eof", "layout.form-boxes-ssn",
               "revision.encrypted-annotated"]


def _case_pdf(case_id: str, tmp: Path) -> bytes:
    from caselib import load
    from caselib.run import build
    return build(load()[case_id], tmp / "case.pdf").read_bytes()


@requires_qpdf
@pytest.mark.parametrize("case_id", CASE_SUBSET)
def test_case_library_files_inventory_and_agree(case_id: str, tmp_path: Path) -> None:
    data = _case_pdf(case_id, tmp_path)
    agreed = inventory_agrees(data, tmp_path)
    # Only the after-%%EOF leak case is flagged (XREF_TAIL; its tail unclaimed).
    assert agreed != case_id.startswith("file.after-final-eof"), case_id


@requires_qpdf
@pytest.mark.skipif(os.environ.get("RUN_INVENTORY_CASES") != "1",
                    reason="every case built: set RUN_INVENTORY_CASES=1")
def test_every_case_library_file_inventories_and_agrees(tmp_path: Path) -> None:
    from caselib import load
    from caselib.run import available, build
    have = available()
    flagged = []
    for case_id, case in sorted(load().items()):
        if case.requires - have:
            continue
        data = build(case, tmp_path / "case.pdf").read_bytes()
        if not inventory_agrees(data, tmp_path):
            flagged.append(case_id)
    assert all(c.startswith("file.after-final-eof") for c in flagged), flagged


# ── Linear time: work counted, and wall clock on adversarial inputs ───────
def _pages(n: int) -> bytes:
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, CATALOG), 0)
    kids = b" ".join(b"%d 0 R" % (3 + 2 * i) for i in range(n))
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)), 0)
    for i in range(n):
        page, content = 3 + 2 * i, 4 + 2 * i
        table[content] = (1, w.stream(content, b"", b"BT (page %d) Tj ET" % i), 0)
        table[page] = (1, w.obj(page, b"<< /Type /Page /Parent 2 0 R /Contents %d 0 R >>"
                                % content), 0)
        w.obj(100_000 + i, b"<< /Dead (x) >>")  # a dead body in every gap
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R" % (3 + 2 * n)))
    return bytes(w.out)


def test_work_is_linear_in_the_file() -> None:
    small, big = _pages(200), _pages(1600)
    work = []
    for data in (small, big):
        budget = Budget(file_size=len(data))
        inv = build_inventory(data, budget=budget)
        assert inv.flags == () and inv.tiles
        work.append(budget.work)
    assert work[1] <= 8 * work[0] * 1.1, work
    assert work[1] <= 8 * len(big), work[1] / len(big)


def _timed(data: bytes) -> tuple[float, Inventory, Budget]:
    budget = Budget(file_size=len(data))
    started = time.process_time()
    inv = build_inventory(data, budget=budget)
    return time.process_time() - started, inv, budget


def _shared_offsets(n: int, inside_string: bool) -> bytes:
    """n xref entries all on one offset, or each inside one unterminated
    string."""
    w = Writer()
    at = w.at()
    w.out += b"1 0 obj\n(" + b"x" * (8 * n) + b"\n"
    table = {0: (0, 0, 65535)} | {
        k: (1, at + (9 + 8 * k if inside_string else 0), 0) for k in range(1, n)}
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R" % n))
    return bytes(w.out)


def _fake_headers(n: int, header: bytes) -> bytes:
    """n `N G obj` headers in one gap: each parsed short (a dead body
    per header), or the second opening a string the rest nests in."""
    data = build_pdf(Spec())
    return data.replace(b"endobj\n", b"endobj\n" + header * n, 1)


def _huge_objstm(n: int) -> bytes:
    """/N a billion over a header table of n pairs, all distinct: read to
    its end before the count is found wrong."""
    return _objstm_file([(1, CATALOG)], {1: (2, 4, 0)}, count=999_999_999,
                        header=b"1 0 " + b"".join(b"%d 0 " % (7 + i) for i in range(n)))


def _many_members(n: int, nested: bool) -> bytes:
    body = b"[" * 300 + b"]" * 300 if nested else b"(m)"
    members = [(1, CATALOG)] + [(10 + i, body) for i in range(n)]
    return _objstm_file(members, {1: (2, 4, 0)} | {10 + i: (2, 4, 1 + i) for i in range(n)})


@pytest.mark.parametrize(("build", "n"), [
    (lambda n: _shared_offsets(n, False), 20_000), (lambda n: _shared_offsets(n, True), 20_000),
    (lambda n: _fake_headers(n, b"1 0 obj [ 2 0 obj (\n"), 20_000),
    (lambda n: _fake_headers(n, b"1 0 obj\n"), 20_000),
    (lambda n: _fake_headers(n, b"1 0 obj <</L 2 0 R>> stream\n"), 20_000),
    (_huge_objstm, 50_000),
    (lambda n: _many_members(n, False), 20_000), (lambda n: _many_members(n, True), 2_000),
])
def test_adversarial_inputs_stay_linear(build: Callable[[int], bytes], n: int) -> None:
    small_t, _, _ = _timed(build(n // 8))
    big_t, inv, budget = _timed(build(n))
    assert inv.tiles
    # Quadratic would be 64x; allow noise on tiny timings.
    assert big_t < 30 and big_t <= 16 * small_t + 1.0, (small_t, big_t)
    assert not budget.exhausted(Counter.WORK)


# ── The dev CLI ───────────────────────────────────────────────────────────
def test_the_dev_cli_prints_counts_and_no_document_bytes(tmp_path: Path) -> None:
    path = tmp_path / "f.pdf"
    path.write_bytes(build_pdf(Spec(dead=True, xref="stream", objstm=True, updates=(Update(),))))
    result = subprocess.run([sys.executable, "-m", "redaction_verifier.inventory", str(path)],
                            cwd=REPO_ROOT, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert (summary["tiles"], summary["revisions"], summary["flags"]) == (True, 2, {})
    assert summary["units"]["dead_body"] == 1 and summary["object_streams"] == 1
    assert b"123-45-6789" not in result.stdout and str(tmp_path).encode() not in result.stdout
    assert set(summary) == {"size", "revisions", "tiles", "units", "regions", "flags",
                            "object_streams", "work"}
    bad = subprocess.run([sys.executable, "-m", "redaction_verifier.inventory"],
                         cwd=REPO_ROOT, capture_output=True, timeout=60)
    assert bad.returncode == 2
