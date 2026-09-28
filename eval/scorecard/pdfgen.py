"""Generated PDFs for the inventory's reader differential (Phase 3a-5,
moved here in 3a-6 so the gate harness and the tests share one
generator): a small writer whose every offset is right by construction,
`Spec`/`build_pdf` for structurally varied files (classic, xref-stream
and hybrid sections, incremental updates, object streams, dead bodies,
comments and junk in gaps), and the byte mutations the tests fuzz with.

tests/test_inventory_build.py draws `Spec`s with Hypothesis
(`pdf_files()`); `random_case` draws from the same alphabets with a
seeded `random.Random`, for `python -m scorecard inventory fuzz`.
Fabricated content only (SSN 123-45-6789).
"""

from __future__ import annotations

import random
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace

MARKER = b"%\xe2\xe3\xcf\xd3"


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
# repair (3a-7's), only what the inventory owns.
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
        new_page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300]"
                    b" /Resources << >> /Contents %d 0 R >>" % content)
        if update.objstm and update.xref == "stream":
            stm = next_num
            next_num += 1
            changed[stm] = (1, w.objstm(stm, [(pages[0], new_page)]), 0)
            changed[pages[0]] = (2, stm, 0)
        else:
            changed[pages[0]] = (1, w.obj(pages[0], new_page), 0)
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


# The alphabets pdf_files() (tests) and random_case() (the fuzz gate) draw from.
XREF_KINDS = ["table", "stream", "hybrid"]
UPDATE_XREF_KINDS = ["table", "stream"]
JUNK = [b"", b"", b" \n", b"% a comment\n", b"junk", b"(", b"<<",
        b"12 0 obj (x) endobj\n", b"1 0 obj", b"endstream", b"\xff\x00"]
MUTATION_BYTES = list(b"0123456789 \n\r()<>[]/%Rnobjstreamdxulf\x00\xff")
MUTATION_SITES = ["object", "objstm", "gap"]


def flip(positions: list[int], values: list[int]) -> Callable[[bytes], bytes]:
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


def mutated(spec: Spec, where: str, positions: list[int], values: list[int]) -> bytes:
    """*spec* with its probes, bytes overwritten in objects (content streams
    and the probe object), an object stream's header table and probe
    member, or the gaps between objects."""
    from redaction_verifier.budget import Budget
    from redaction_verifier.inventory import build_inventory
    from redaction_verifier.ledger import UnitKind

    spec = replace(spec, probe=True)
    if where == "objstm":
        return build_pdf(replace(spec, xref="stream", objstm=True,
                                 mutate=flip(positions, values)))
    data = build_pdf(spec)
    inv = build_inventory(data, budget=Budget(file_size=len(data)))
    targets = {51} | set(range(3 + spec.pages, 3 + 2 * spec.pages))  # probe, contents
    wanted = UnitKind.OBJECT if where == "object" else UnitKind.WHITESPACE
    pool = [p for r in inv.tiling.regions if r.kind is wanted
            and (where == "gap" or r.owners[0].obj in targets)
            for p in range(r.span.start, r.span.end)]
    out = bytearray(data)
    for pos, value in zip(positions, values):
        out[pool[pos % len(pool)]] = value
    return bytes(out)


def random_spec(rng: random.Random, junk: bool = True) -> Spec:
    """A Spec drawn as tests/test_inventory_build.py's pdf_files() draws one."""
    xref = rng.choice(XREF_KINDS)
    updates = tuple(Update(rng.choice(UPDATE_XREF_KINDS), rng.random() < 0.5, rng.random() < 0.5)
                    for _ in range(rng.randint(0, 3)))
    return Spec(pages=rng.randint(1, 3), xref=xref,
                objstm=rng.random() < 0.5 if xref != "table" else False,
                predictor=rng.random() < 0.5, updates=updates, dead=rng.random() < 0.5,
                comment=rng.random() < 0.5, junk=rng.choice(JUNK) if junk else b"")


def random_case(rng: random.Random) -> tuple[str, bytes]:
    """One fuzz file: half generated with junk in a gap (pdf_files()), half
    junk-free and mutated (the mutation tests). Returns (kind, bytes)."""
    if rng.random() < 0.5:
        return "generated", build_pdf(random_spec(rng))
    spec = random_spec(rng, junk=False)
    where = rng.choice(MUTATION_SITES)
    count = rng.randint(1, 3)
    positions = [rng.randint(0, 10_000) for _ in range(count)]
    values = [rng.choice(MUTATION_BYTES) for _ in range(count)]
    return f"mutated-{where}", mutated(spec, where, positions, values)
