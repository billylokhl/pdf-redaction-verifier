"""The inventory's reader differential (ADR 0010, "readers are the
authority"): one oracle, shared by the tests (tests/test_inventory_*.py)
and the 3a-6 gate harness (`scorecard.inventory_gate`).

`compare(data, workdir)` builds the inventory of *data* and returns an
`Agreement` instead of asserting:

- DISAGREES with the failed `Check` when the inventory does not tile,
  or when it is unflagged and a reader reads the file otherwise -- per
  revision: the object set (qpdf --show-xref), each object's stream data
  (MuPDF's xref_stream_raw), each object-stream member's value (MuPDF
  xref_object, and qpdf --show-object for the first few per revision),
  no qpdf --check structural warning, MuPDF opening it unrepaired and
  without a warning; and the dead bodies exactly the ``N G obj`` bodies
  no reader lists. Anything that stops the comparison itself (a reader
  timing out, an exception) is a disagreement too (ORACLE_ERROR): the
  oracle never reports an agreement it did not check.
- FLAGGED, with the flag reasons, when the inventory flags anything:
  readers may disagree then -- the inventory said so.
- AGREES otherwise. An encrypted revision's bytes and values are not
  compared (the inventory decrypts nothing until 3a-8/9): `encrypted`.

This runs outside the child boundary (it imports pymupdf and runs qpdf);
nothing in redaction_verifier may import it. Reader messages can quote a
document's names and strings: an `Agreement` carries them only as
`scrub()`bed templates, and the gate keeps even those out of its
aggregate output.

`python -m scorecard.inventory FILE WORKDIR` is the harness's oracle
child: it prints the file's Agreement and the harness-side measurements
(`measure`) as one JSON object -- counts, integers and scrubbed
templates, never document bytes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

import pymupdf

from redaction_verifier.budget import Budget
from redaction_verifier.inventory import Inventory, build_inventory
from redaction_verifier.inventory.flate import flate_decode
from redaction_verifier.inventory.objects import (ObjectParser, PdfArray, PdfDict, PdfInt,
                                                   PdfName, PdfNull, PdfRef, PdfValue)
from redaction_verifier.inventory.xref import Chain, read_chain
from redaction_verifier.ledger import FlagReason, Unit, UnitKind, UnitRef

QPDF_TIMEOUT = 60.0


def chain_of(data: bytes) -> Chain:
    """read_chain with a fresh budget for *data* (the budget is required)."""
    return read_chain(data, budget=Budget(file_size=len(data)))


def inventory(data: bytes) -> Inventory:
    return build_inventory(data, budget=Budget(file_size=len(data)))


# ── Reader-side helpers ───────────────────────────────────────────────────
def plain(value: object) -> object:
    """A parsed value as plain data (spans dropped); numbers as floats."""
    from redaction_verifier.inventory import objects as o
    match value:
        case o.PdfNull() | None:
            return None
        case o.PdfBool(value=v):
            return ("bool", v)
        case o.PdfInt(value=v) | o.PdfReal(value=v):
            return ("num", None if v is None else float(v))
        case o.PdfName(raw=raw):
            return ("name", raw)
        case o.PdfString(raw=raw):
            return ("str", raw)
        case o.PdfRef(num=num, gen=gen):
            return ("ref", num, gen)
        case o.PdfArray(items=items):
            return [plain(item) for item in items]
        case o.PdfDict(entries=entries):
            # §7.3.7: an entry whose value is null is the same as no entry
            # (qpdf drops it, MuPDF keeps it): equivalent, not a disagreement.
            return sorted((k.raw, repr(plain(v))) for k, v in entries
                          if not isinstance(v, o.PdfNull))
    return ("other", repr(value))


_LINE = re.compile(rb"^(\d+)/(\d+): (?:uncompressed; offset = (\d+)|"
                   rb"compressed; stream = (\d+), index = (\d+))", re.M)


def qpdf_map(path: Path) -> tuple[dict[int, tuple[str, int, int]], int]:
    """qpdf's object map of *path* (--show-xref): number -> ("u", offset,
    gen) or ("c", stream, index); and qpdf's exit status."""
    result = subprocess.run(["qpdf", "--show-xref", str(path)], capture_output=True,
                            timeout=QPDF_TIMEOUT)
    found: dict[int, tuple[str, int, int]] = {}
    for m in _LINE.finditer(result.stdout):
        num, gen = int(m.group(1)), int(m.group(2))
        found[num] = (("u", int(m.group(3)), gen) if m.group(3)
                      else ("c", int(m.group(4)), int(m.group(5))))
    return found, result.returncode


# qpdf --check warnings about what a page-tree node means, not how the
# file parses: the reference graph (3a-7) owns these, not the xref chain.
# Includes a reference to a free (null) object used as a dictionary: the
# object maps agree; flagging the dangling reference is 3a-7's job.
# (Substring matches: issue #44 comment 1 item 3 tracks narrowing them.)
PAGE_TREE_SEMANTICS = (b"/Type key should be", b"attempted key retrieval",
                       b"Pages tree includes non-dictionary", b"/Kids",
                       b"operation for dictionary attempted on object of type null",
                       b"MediaBox is undefined")
# qpdf --check warnings the inventory does not own: page-tree semantics
# (3a-7's) and a page content stream's own syntax, which --check tokenizes
# ("... stream 5 0 (content, offset 10): unexpected )") -- the content
# decoder's (Phase 4). The stream's raw bytes are still compared.
# (Issue #46 item 3 tracks matching the whole template instead.)
NOT_STRUCTURE = PAGE_TREE_SEMANTICS + (b"(content, offset ",)


def qpdf_warnings(path: Path) -> list[bytes]:
    """Every line of qpdf --check's output on *path*."""
    check = subprocess.run(["qpdf", "--check", str(path)], capture_output=True,
                           timeout=QPDF_TIMEOUT)
    return (check.stdout + check.stderr).splitlines()


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
                           capture_output=True, timeout=QPDF_TIMEOUT)
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
    return plain(_without_dangling(ObjectParser(text).parse_value_at(start, end).value, live))


def revision_blob(chain: Chain, data: bytes, revision: int) -> bytes:
    """*data* cut to *revision* (0 = the whole file), as readers open it."""
    return data if revision == 0 else (
        data[:chain.revision_end(revision)]
        + b"\nstartxref\n%d\n%%%%EOF\n" % chain.revision_start(revision))


# ── Reader messages: a category, or at most a scrubbed template ───────────
def scrub(message: bytes | str) -> str:
    """A reader message as a template: its path prefix, parenthesized and
    quoted text, names, hex strings and numbers replaced by placeholders,
    and of the rest only short lowercase words kept (the readers' own
    vocabulary; a document's tokens in a message are mostly quoted, named
    or capitalized). Reader messages quote a document's names and
    strings; a template keeps the message's shape, not its content."""
    text = message.decode("latin-1") if isinstance(message, bytes) else message
    text = re.sub(r"^WARNING: .*?\.pdf\b", "WARNING:", text)
    text = re.sub(r"\([^)]*\)?", " (_) ", text)
    text = re.sub(r"'[^']*'?|\"[^\"]*\"?|<[^>]*>?|\[[^\]]*\]?", " _ ", text)
    text = re.sub(r"/[^\s/()<>\[\]{}%]*", " /_ ", text)
    text = re.sub(r"\d+", " # ", text)
    words = [w for w in text.split()
             if w in ("WARNING:", "(_)", "_", "/_", "#")
             or re.fullmatch(r"[a-z]{1,16}[:,;.]?", w)]
    return " ".join(words)[:120]


# ── The oracle ────────────────────────────────────────────────────────────
class Status(Enum):
    AGREES = "agrees"
    FLAGGED = "flagged"
    DISAGREES = "disagrees"


class Check(Enum):
    """Which comparison a disagreement failed."""

    TILING = "tiling"                  # the inventory does not tile the file
    OBJECT_SET = "object_set"          # a revision's objects or their bodies
    STREAM_DATA = "stream_data"        # a stream's raw (or an object stream's decoded) bytes
    MEMBER_VALUE = "member_value"      # an object-stream member's value
    DEAD_BODIES = "dead_bodies"        # dead bodies not exactly the unlisted N G obj
    QPDF_CHECK = "qpdf_check"          # a qpdf --check structural warning
    MUPDF_WARNING = "mupdf_warning"    # a MuPDF warning, or MuPDF repaired the file
    NEEDS_PASSWORD = "needs_password"  # a reader cannot open it without a password
    ORACLE_ERROR = "oracle_error"      # the comparison itself failed: nothing verified


@dataclass(frozen=True)
class Agreement:
    """What the oracle found. ``revision`` and ``obj`` locate a
    disagreement; ``templates`` are scrubbed reader messages (or an
    exception's type) -- for local detail only, never an aggregate."""

    status: Status
    reasons: tuple[FlagReason, ...] = ()
    check: Check | None = None
    revision: int | None = None
    obj: int | None = None
    templates: tuple[str, ...] = ()
    encrypted: bool = False
    streams_compared: int = 0
    members_compared: int = 0

    @property
    def agrees(self) -> bool:
        return self.status is Status.AGREES

    @property
    def flagged(self) -> bool:
        return self.status is Status.FLAGGED

    @property
    def disagrees(self) -> bool:
        return self.status is Status.DISAGREES

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status.value, "reasons": [r.value for r in self.reasons],
                "check": self.check.value if self.check else None, "revision": self.revision,
                "obj": self.obj, "templates": list(self.templates),
                "encrypted": self.encrypted, "streams_compared": self.streams_compared,
                "members_compared": self.members_compared}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Agreement:
        return cls(Status(raw["status"]), tuple(FlagReason(r) for r in raw["reasons"]),
                   Check(raw["check"]) if raw["check"] else None, raw["revision"], raw["obj"],
                   tuple(raw["templates"]), raw["encrypted"], raw["streams_compared"],
                   raw["members_compared"])


class _Disagreement(Exception):
    def __init__(self, check: Check, revision: int | None = None, obj: int | None = None,
                 templates: tuple[str, ...] = ()) -> None:
        super().__init__(check.value)
        self.check, self.revision, self.obj, self.templates = check, revision, obj, templates


def compare(data: bytes, workdir: Path, qpdf_members: int = 8,
            inv: Inventory | None = None) -> Agreement:
    """The inventory of *data* against MuPDF and qpdf. Every object-stream
    member's value is compared with MuPDF's; the first *qpdf_members* per
    revision also with qpdf's (one qpdf process each). Revision cuts are
    written to *workdir*. Never raises on a reader's answer: anything that
    stops the comparison is a disagreement (ORACLE_ERROR)."""
    if inv is None:
        inv = inventory(data)
    if not inv.tiles:
        return Agreement(Status.DISAGREES, check=Check.TILING)
    if inv.flags:
        return Agreement(Status.FLAGGED, reasons=tuple(dict.fromkeys(f.reason for f in inv.flags)))
    counts = [0, 0]
    encrypted = [False]
    try:
        _compare(data, workdir, qpdf_members, inv, counts, encrypted)
    except _Disagreement as found:
        return Agreement(Status.DISAGREES, check=found.check, revision=found.revision,
                         obj=found.obj, templates=found.templates, encrypted=encrypted[0],
                         streams_compared=counts[0], members_compared=counts[1])
    except Exception as error:  # fail closed: an unfinished comparison never agrees
        return Agreement(Status.DISAGREES, check=Check.ORACLE_ERROR,
                         templates=(type(error).__name__,), encrypted=encrypted[0],
                         streams_compared=counts[0], members_compared=counts[1])
    return Agreement(Status.AGREES, encrypted=encrypted[0], streams_compared=counts[0],
                     members_compared=counts[1])


def _compare(data: bytes, workdir: Path, qpdf_members: int, inv: Inventory,
             counts: list[int], encrypted_seen: list[bool]) -> None:
    opened: list[pymupdf.Document] = []
    try:
        _compare_revisions(data, workdir, qpdf_members, inv, counts, encrypted_seen, opened)
    finally:
        for doc in opened:
            doc.close()


def _compare_revisions(data: bytes, workdir: Path, qpdf_members: int, inv: Inventory,
                       counts: list[int], encrypted_seen: list[bool],
                       opened: list[pymupdf.Document]) -> None:
    pymupdf.TOOLS.mupdf_warnings()
    listed: set[int] = set()
    chain = chain_of(data)
    units: dict[UnitRef, Unit] = {u.ref: u for u in inv.units}  # refs nest one level at most
    for revision in range(inv.revisions):
        # Readers decrypt what they read; the inventory does not until 3a-8/9,
        # so an encrypted revision's bytes and values are not compared.
        trailer = chain.sections[chain.revisions[revision][0]].trailer
        encrypted = trailer is not None and trailer.get(b"Encrypt") is not None
        encrypted_seen[0] = encrypted_seen[0] or encrypted
        blob = revision_blob(chain, data, revision)
        path = workdir / f"r{revision}.pdf"
        path.write_bytes(blob)
        for previous in opened:  # one revision's document open at a time
            previous.close()
        doc = pymupdf.open(stream=blob, filetype="pdf")
        opened[:] = [doc]
        if doc.needs_pass:
            raise _Disagreement(Check.NEEDS_PASSWORD, revision)
        qmap, code = qpdf_map(path)
        if code != 0:
            raise _Disagreement(Check.OBJECT_SET, revision, templates=(f"qpdf exit {code}",))
        bodies = inv.bodies_by_number(revision)
        if set(bodies) != set(qmap):
            raise _Disagreement(Check.OBJECT_SET, revision,
                                min(set(bodies) ^ set(qmap)))
        lines = qpdf_warnings(path)
        structural = [line for line in lines if line.startswith(b"WARNING")
                      and not any(pattern in line for pattern in NOT_STRUCTURE)
                      and not _catalog_retyped(line, lines, path)]
        if structural:
            raise _Disagreement(Check.QPDF_CHECK, revision,
                                templates=tuple(scrub(line) for line in structural[:3]))
        if doc.is_repaired:
            raise _Disagreement(Check.MUPDF_WARNING, revision,
                                templates=_mupdf_templates() or ("repaired",))
        qpdf_checked = 0
        decoded_homes: dict[int, bytes] = {}
        live = {n: (0 if kind == "c" else gen) for n, (kind, _, gen) in qmap.items()}
        for number, (kind, a, b) in sorted(qmap.items()):
            body = bodies[number]
            if kind == "u":
                listed.add(a)
                if (body.kind, body.start) != (UnitKind.OBJECT, a):
                    raise _Disagreement(Check.OBJECT_SET, revision, number)
                span = inv.stream_data.get(body.start)
                if span is not None:
                    if not encrypted:
                        counts[0] += 1
                        if doc.xref_stream_raw(number) != blob[span.start:span.end]:
                            raise _Disagreement(Check.STREAM_DATA, revision, number)
                elif doc.xref_is_stream(number):
                    raise _Disagreement(Check.STREAM_DATA, revision, number)
                continue
            home = qmap.get(a)
            stream = inv.object_streams.get(home[1]) if home and home[0] == "u" else None
            if (stream is None or body.kind is not UnitKind.OBJSTM_MEMBER
                    or body.within is not stream.ref or b >= len(stream.members)
                    or stream.members[b] is not body):
                raise _Disagreement(Check.OBJECT_SET, revision, number)
            if encrypted:
                continue
            if a not in decoded_homes:
                decoded = doc.xref_stream(a)
                raw_span = inv.stream_data[stream.ref.start]
                ours = flate_decode(blob[raw_span.start:raw_span.end])
                if ours.data != decoded and ours.complete:  # unfiltered: not Flate
                    raise _Disagreement(Check.STREAM_DATA, revision, a)
                decoded_homes[a] = decoded
            decoded = decoded_homes[a]
            counts[1] += 1
            span = units[body].spans[0]
            mine = _value_of(decoded, live, span.start, span.end)
            # MuPDF's tight printer (compressed=True) writes no separator after
            # an empty name: the empty name then 2.5 prints as `/2.5`, and a
            # key `/` with value true as `/true` -- one name, where MuPDF's own
            # object holds two tokens. Its pretty printer separates every token.
            mu_text = doc.xref_object(number, compressed=False).encode("latin-1")
            if _value_of(mu_text, live) != mine:
                raise _Disagreement(Check.MEMBER_VALUE, revision, number)
            if qpdf_checked >= qpdf_members:
                continue
            qpdf_checked += 1
            shown = subprocess.run(["qpdf", f"--show-object={number}", str(path)],
                                   capture_output=True, timeout=QPDF_TIMEOUT)
            if (_value_of(shown.stdout, live), shown.returncode) != (mine, 0):
                raise _Disagreement(Check.MEMBER_VALUE, revision, number)
    _dead_bodies_agree(data, inv, listed)
    warnings = _mupdf_templates()
    if warnings:
        raise _Disagreement(Check.MUPDF_WARNING, templates=warnings)


def _mupdf_templates() -> tuple[str, ...]:
    text = pymupdf.TOOLS.mupdf_warnings()
    return tuple(scrub(line) for line in text.splitlines()[:3]) if text else ()


_ANY_HEADER = re.compile(rb"(?<![^\x00\t\n\x0c\r ()<>\[\]{}/%])\d+[\x00\t\n\x0c\r ]+\d+"
                         rb"[\x00\t\n\x0c\r ]+obj(?![^\x00\t\n\x0c\r ()<>\[\]{}/%])")


def _dead_bodies_agree(data: bytes, inv: Inventory, listed: set[int]) -> None:
    """Dead bodies: exactly the N G obj headers no reader lists, outside
    every other unit (and not inside another dead body)."""
    spans = sorted((s.start, s.end) for u in inv.units
                   if u.ref.within is None and u.ref.kind is not UnitKind.DEAD_BODY
                   for s in u.spans)
    starts = [s for s, _ in spans]
    dead = sorted(((u.spans[0].start, u.spans[0].end) for u in inv.units
                   if u.ref.kind is UnitKind.DEAD_BODY))
    dead_starts = [s for s, _ in dead]

    def inside(pos: int, found: list[tuple[int, int]], keys: list[int], strict: bool) -> bool:
        # Units and dead bodies do not overlap one another (the tiling
        # would flag it): the nearest start at or before pos decides.
        i = bisect_right(keys, pos - (1 if strict else 0)) - 1
        return i >= 0 and found[i][1] > pos and (not strict or found[i][0] < pos)
    expected = []
    for m in _ANY_HEADER.finditer(data):
        if m.start() in listed or inside(m.start(), spans, starts, False):
            continue
        if inside(m.start(), dead, dead_starts, True):
            continue
        expected.append(m.start())
    if expected != dead_starts:
        raise _Disagreement(Check.DEAD_BODIES)


# ── Harness-side measurements (the owner's pending decisions) ─────────────
_LINEARIZED_HEAD = re.compile(rb"[0-9]{1,10}[\x00\t\n\x0c\r ]+[0-9]{1,5}[\x00\t\n\x0c\r ]+obj")
_OBJ_LIKE = re.compile(rb"\d+\s+\d+\s+obj")


def _int_value(value: object) -> int | None:
    return value.value if isinstance(value, PdfInt) else None


def measure(data: bytes, inv: Inventory) -> dict[str, int]:
    """Counts for decisions the owner has pending (issues #44 and #46),
    all integers: linearized files updated incrementally; comment lines
    after the header (beyond the binary marker) or an intermediate %%EOF;
    streams whose indirect /Length lives in an object stream;
    REVISION_AMBIGUOUS flags whose target is in fact equal in every
    revision; dead object streams; and whether the file is encrypted."""
    out: dict[str, int] = {}
    parser = ObjectParser(data)
    # Encrypted: bytes and values not compared until 3a-8/9.
    out["encrypted"] = int(any(section.trailer is not None
                               and section.trailer.get(b"Encrypt") is not None
                               for section in chain_of(data).sections))
    ends = {u.ref.start: u.spans[0].end for u in inv.units
            if u.ref.within is None and u.ref.kind in (UnitKind.OBJECT, UnitKind.DEAD_BODY)}

    def parsed(offset: int) -> PdfValue | None:
        end = ends.get(offset)
        obj = parser.parse_indirect_at(offset, end if end is not None else len(data))
        return obj.value if obj is not None else None

    # Linearized (§F: the first object a /Linearized dictionary) and updated:
    # its /L (the file's length when linearized) is not the file's length.
    head = _LINEARIZED_HEAD.search(data, 0, 1024)
    first = parser.parse_indirect_at(head.start(), min(len(data), head.start() + 4096)) \
        if head else None
    lin = first.value if first is not None and isinstance(first.value, PdfDict) else None
    out["linearized"] = int(lin is not None and lin.get(b"Linearized") is not None)
    length = _int_value(lin.get(b"L")) if lin is not None else None
    out["linearized_updated"] = int(bool(out["linearized"]) and length != len(data))

    # Comment lines claimed after the header (beyond a binary marker) or an
    # intermediate %%EOF (issue #46 item 1).
    lines: list[bytes] = []
    for unit in inv.units:
        if unit.ref.kind is UnitKind.HEADER:
            extra = list(unit.spans[1:])
            if extra and any(byte >= 128 for byte in data[extra[0].start:extra[0].end]):
                extra = extra[1:]  # the binary marker
            out["header_comment_lines"] = out.get("header_comment_lines", 0) + len(extra)
            lines += [data[s.start:s.end] for s in extra]
        elif unit.ref.kind is UnitKind.XREF_EPILOGUE:
            extra = list(unit.spans[1:])
            out["eof_comment_lines"] = out.get("eof_comment_lines", 0) + len(extra)
            lines += [data[s.start:s.end] for s in extra]
    out["comment_longest"] = max((len(line) for line in lines), default=0)
    out["comment_nonprintable"] = sum(any(b < 32 and b != 9 or b >= 127 for b in line)
                                      for line in lines)
    out["comment_obj_like"] = sum(bool(_OBJ_LIKE.search(line)) for line in lines)

    # Streams whose indirect /Length names an object that is compressed in
    # some revision (#46 item 4: not resolved, LENGTH_MISMATCH).
    in_objstm = 0
    object_starts = {ref.start for ref in inv.objects.values()}
    for offset in inv.stream_data:
        if offset not in object_starts:  # a dead body's stream
            continue
        value = parsed(offset)
        target = value.get(b"Length") if isinstance(value, PdfDict) else None
        if isinstance(target, PdfRef) and any(
                kind == 2 for _, kind, _, _ in inv.entries.get(target.num, ())):
            in_objstm += 1
    out["length_in_objstm"] = in_objstm

    # REVISION_AMBIGUOUS whose /Length target reads equal in every revision
    # its stream lives in (#46 item 4: flagged even when equal).
    equal = differ = unresolved = 0
    for flag in inv.flags[:1000]:
        if flag.reason is not FlagReason.REVISION_AMBIGUOUS:
            continue
        params = dict(flag.params)
        stream_ref = inv.objects.get(params.get("offset", -1))  # an xref offset
        length_object = params.get("length_object", -1)
        if stream_ref is None or stream_ref.obj is None:
            unresolved += 1
            continue
        values: set[int | None] = set()
        for revision in range(inv.revisions):
            if inv.entry(stream_ref.obj, revision) != (1, stream_ref.start, stream_ref.gen):
                continue
            entry = inv.entry(length_object, revision)
            values.add(_int_value(parsed(entry[1])) if entry and entry[0] == 1 else None)
        if None in values or not values:
            unresolved += 1
        elif len(values) == 1:
            equal += 1
        else:
            differ += 1
    out["ambiguous_equal"], out["ambiguous_differ"] = equal, differ
    out["ambiguous_unresolved"] = unresolved

    # Dead bodies, and dead object streams (#46 item 4: not decoded).
    dead = [u for u in inv.units if u.ref.kind is UnitKind.DEAD_BODY]
    out["dead_bodies"] = len(dead)
    out["dead_objstms"] = sum(
        1 for u in dead if u.ref.start in inv.stream_data
        and isinstance(value := parsed(u.ref.start), PdfDict)
        and isinstance(kind := value.get(b"Type"), PdfName) and kind.raw == b"ObjStm")
    return out


def regions(inv: Inventory) -> dict[str, dict[str, int]]:
    """UNINDEXED and CONTESTED regions of the file's tiling: how many,
    and how many bytes (reported, not gated: owner decision 3)."""
    found: dict[str, dict[str, int]] = {}
    for region in inv.tiling.regions:
        name = region.kind.value
        if name in ("unindexed", "contested"):
            entry = found.setdefault(name, {"regions": 0, "bytes": 0})
            entry["regions"] += 1
            entry["bytes"] += len(region.span)
    return found


def main(argv: list[str] | None = None) -> int:
    """The gate's oracle child: FILE's Agreement and measurements as JSON."""
    parser = argparse.ArgumentParser(prog="python -m scorecard.inventory")
    parser.add_argument("file", type=Path)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--qpdf-members", type=int, default=8)
    args = parser.parse_args(argv)
    data = args.file.read_bytes()
    inv = inventory(data)
    agreement = compare(data, args.workdir, args.qpdf_members, inv=inv)
    try:
        measures = measure(data, inv)
    except Exception as error:  # a measurement never hides the verdict above
        measures = {"measure_error": 1}
        print(type(error).__name__, file=sys.stderr)
    print(json.dumps({"agreement": agreement.to_json(), "measures": measures,
                      "regions": regions(inv), "flags": [f.reason.value for f in inv.flags]},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
