"""The inventory's reader differential (ADR 0010, "readers are the
authority"): one oracle, shared by the tests (tests/test_inventory_*.py)
and the 3a-6 gate harness (`scorecard.inventory_gate`).

`compare(data, workdir)` builds the inventory of *data* and returns an
`Agreement` instead of asserting:

- DISAGREES with the failed `Check` when the inventory does not tile,
  or when it is unflagged and a reader reads the file otherwise -- per
  revision: the object set (qpdf --show-xref), each object's stream data
  (MuPDF's xref_stream_raw), every live object's value and the trailer's
  (ADR 0010 item 2, as amended in 3a-6b: MuPDF's xref_object, and
  libqpdf's own values through pikepdf -- exact string bytes, exact
  numbers, references by number and generation), no libqpdf warning
  while reading them, no qpdf --check structural warning or error,
  MuPDF opening it unrepaired and without a warning; and the dead bodies
  exactly the ``N G obj`` bodies no reader lists. Anything that stops
  the comparison itself (a reader timing out, an exception, a qpdf CLI
  whose major.minor version is not libqpdf's) is a disagreement too
  (ORACLE_ERROR): the oracle never reports an agreement it did not check.
- FLAGGED, with the flag reasons, when the inventory flags anything:
  readers may disagree then -- the inventory said so.
- AGREES otherwise. An encrypted revision's bytes and values are not
  compared (the inventory decrypts nothing until 3a-8/9): `encrypted`.
  A revision counts as encrypted only when our trailer (an /Encrypt
  entry), MuPDF and qpdf all say so; any split between them -- say
  `/Encrypt null`, which both readers read in plain -- is a
  disagreement (ENCRYPTION), never an exemption.

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
import functools
import io
import json
import re
import subprocess
import sys
import time
from bisect import bisect_right
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

import pikepdf
import pymupdf

from redaction_verifier.budget import Budget
from redaction_verifier.inventory import Inventory, build_inventory
from redaction_verifier.inventory.flate import flate_decode
from redaction_verifier.inventory.objects import (ObjectParser, PdfArray, PdfBool, PdfDict,
                                                   PdfInt, PdfName, PdfNull, PdfReal, PdfRef,
                                                   PdfString, PdfValue)
from redaction_verifier.inventory.annotations import Annotation
from redaction_verifier.inventory.labels import RevisionLabels, StructuralKind
from redaction_verifier.inventory.pages import INHERITED, Box, Page, f32, intersect
from redaction_verifier.inventory.trees import Trees, key_text, qpdf_key_text
from redaction_verifier.inventory.xref import Chain, read_chain
from redaction_verifier.ledger import FlagReason, Unit, UnitKind, UnitRef

from .annotations import compare_annotations

# Per qpdf call. A huge but valid file must not become ORACLE_ERROR on a
# short per-call limit: the oracle child sets this from its own budget
# (--qpdf-timeout, the harness's oracle timeout); the harness's whole-child
# timeout still bounds the file.
QPDF_TIMEOUT = 600.0


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


# qpdf --check warnings the inventory does not own: a page content
# stream's own syntax, which --check tokenizes ("... stream 5 0 (content,
# offset 10): unexpected )") -- the content decoder's (Phase 4). The
# stream's raw bytes are still compared. (Issue #46 item 3 tracks matching
# the whole template instead.) Page-tree semantics were exempt here until
# 3a-7b, which flags any tree off the canonical shape (PAGE_TREE): any
# qpdf page-tree message on an unflagged file is now a disagreement.
NOT_STRUCTURE = (b"(content, offset ",)

# The written allowlist of whole qpdf messages that are not a disagreement
# (ADR 0010; 3a-6b, root-caused on the 2,073-file corpus and in qpdf's
# source, identical in 11.9 and 12.4). Each pattern matches one whole line
# after `WARNING: <the exact path>`; a document cannot write such a line.
#
# Linearization lint: qpdf --check compares a linearized file's hint
# tables (Annex F; only for loading page by page) with the layout it
# computes, from QPDF_linearization.cc's linearizationWarning, which
# prints no position. Readers do not read objects through hint tables:
# a corrupted hint table leaves both readers' objects unchanged
# (tests/test_inventory_gate.py). Applied only after "File is linearized".
LINEARIZATION_LINT = tuple(re.compile(pattern) for pattern in (
    rb"no xref table entry for \d+ 0",
    rb"page \d+: shared object \d+: in hint table but not computed list",
    rb"page \d+: shared object \d+: in computed list but not hint table",
    rb"shared object \d+ length mismatch: hint table = \d+; computed = \d+",
    rb"object count mismatch for page \d+: hint table = \d+; computed = \d+",
    rb"page \d+ has shared identifier entries",
    rb"first page object offset mismatch",
    rb"page length mismatch for page \d+: hint table = \d+; computed length = \d+"
    rb" \(offset = \d+\)",
    rb"part \d+ is empty but nshared_total > nshared_first_page",
    rb"end of first page section \(/E\) mismatch: /E = \d+; computed = \d+\.\.\d+",
    rb"linearized file contains an uncompressed object after a compressed one in a"
    rb" cross-reference stream",
    rb"first shared object offset mismatch: hint table = \d+; computed = \d+",
    rb"first shared object number mismatch: hint table = \d+; computed = \d+",
    rb"incorrect offset in outlines table: hint table = \d+; computed = \d+",
    rb"incorrect object count in outline hint table",
))
# Flate data that ends before zlib's end marker, in a stream the inventory
# does not decode (content, images, forms): qpdf --check decodes every
# stream; decoding content is the content decoder's (Phase 4). The offset
# is the stream's data start (QPDF_objects.cc stream_offset); the raw bytes
# are still compared, and the readers' partial output is the same.
TRUNCATED_FLATE = re.compile(
    rb" \(offset (\d+)\): input stream is complete but output may still be valid")


# An in-use xref entry at offset 0 (xref.no_object): both readers read no
# object; qpdf says so in this message. The oracle drops such an entry from
# qpdf's map only after MuPDF and libqpdf have both read it as null (owner
# decision 7, 2026-10-03).
OFFSET_ZERO = re.compile(
    rb" \(object \d+ \d+\): object has offset 0 - a common error handled correctly by"
    rb" qpdf and most other applications")

# An empty name or number tree root (neither /Names -- or /Nums -- nor
# /Kids, or an empty array): both readers read no entries; qpdf --check
# warns for the trees it walks (/EmbeddedFiles, /Dests, /PageLabels).
# Accepted as empty, owner decision R5 (ADR 0011 item 4); the inventory
# flags an empty node below the root, so on an unflagged file the line is
# a root's. Anchored on the exact path, the whole line (3a-7c).
EMPTY_TREE_ROOT = re.compile(
    rb" \(Name/Number tree node(?: \(object \d+\))?\): name/number tree node has neither"
    rb" non-empty /(?:Names|Nums) nor /Kids")

# A page content stream's own syntax error in a token that starts at byte
# 0: qpdf's QPDFExc constructor turns offset 0 into "no offset", so the
# line reads "(content): ..." instead of NOT_STRUCTURE's "(content, offset
# N)" -- "unexpected )", "EOF while reading token" (an unterminated
# string), and the rest of the content parser's messages (QPDFParser.cc
# Parser::warn, the content description; --check parses page contents
# only). As for that form, content syntax is the content decoder's (Phase
# 4) and the stream's raw bytes are still compared; a decode error is a
# different line, still gated. Anchored on the whole line as --check prints
# it, qpdf's own prefix: the page and its content streams, one or an array
# ("stream 4 0, stream 5 0"); no file path. Issue #88.
CONTENT_AT_ZERO = re.compile(
    rb"WARNING: page object \d+ \d+ stream \d+ \d+(?:, stream \d+ \d+)* \(content\):"
    rb" [^\r\n]+")


def structural(lines: Iterable[bytes], path: Path, *, linearized: bool = False,
               undecoded: Collection[int] = ()) -> list[bytes]:
    """The qpdf messages (WARNING and ERROR lines, from --check or from
    libqpdf) that are a disagreement: all but the allowlists above.
    *undecoded*: data starts of streams the inventory does not decode
    (empty for an encrypted revision, whose raw bytes are not compared)."""
    prefix = b"WARNING: " + str(path).encode()
    found = []
    for line in lines:
        if not line.startswith((b"WARNING", b"ERROR")):
            continue
        if any(pattern in line for pattern in NOT_STRUCTURE):
            continue
        if CONTENT_AT_ZERO.fullmatch(line):
            continue
        rest = line[len(prefix):] if line.startswith(prefix) else None
        if rest is not None:
            if linearized and rest.startswith(b": ") and any(
                    lint.fullmatch(rest[2:]) for lint in LINEARIZATION_LINT):
                continue
            truncated = TRUNCATED_FLATE.fullmatch(rest)
            if truncated and int(truncated.group(1)) in undecoded:
                continue
            if OFFSET_ZERO.fullmatch(rest) or EMPTY_TREE_ROOT.fullmatch(rest):
                continue
        found.append(line)
    return found


def qpdf_warnings(path: Path) -> list[bytes]:
    """Every line of qpdf --check's output on *path*."""
    check = subprocess.run(["qpdf", "--check", str(path)], capture_output=True,
                           timeout=QPDF_TIMEOUT)
    return (check.stdout + check.stderr).splitlines()


@functools.lru_cache(maxsize=1)
def qpdf_versions() -> tuple[str, str]:
    """(the qpdf CLI's version, the libqpdf version inside pikepdf)."""
    out = subprocess.run(["qpdf", "--version"], capture_output=True, text=True,
                         timeout=60).stdout
    found = re.search(r"qpdf version (\d+\.\d+\.\d+)", out)
    return (found.group(1) if found else "unknown"), str(pikepdf.__libqpdf_version__)


def qpdf_versions_match() -> bool:
    """One qpdf behaviour per run: the CLI (object map, --check) and
    libqpdf (values, its warnings) at the same major.minor version. Their
    messages and limits differ across releases (12.3 added the object-0
    warning; 12.0 the id cap), so the allowlists hold only for one."""
    cli, lib = qpdf_versions()
    return cli.split(".")[:2] == lib.split(".")[:2]


def qpdf_encrypted(path: Path) -> bool:
    """qpdf's own view: is *path* encrypted (--is-encrypted: exit 0 yes, 2 no)?"""
    result = subprocess.run(["qpdf", "--is-encrypted", str(path)], capture_output=True,
                            timeout=QPDF_TIMEOUT)
    if result.returncode not in (0, 2):
        raise RuntimeError("qpdf --is-encrypted failed")
    return result.returncode == 0


def mupdf_encrypted(doc: pymupdf.Document) -> bool:
    """MuPDF's own view. ``is_encrypted`` turns False once MuPDF has
    authenticated an empty user password (an owner-password-only file):
    the metadata's "encryption" names the handler either way."""
    return bool(doc.needs_pass or (doc.metadata or {}).get("encryption"))


# ── Values in one comparable form: ours, MuPDF's, libqpdf's ───────────────
# Kinds kept ("int" vs "real"), integers and reals exact (a real is the
# fraction its text writes, never a float), strings and names as bytes.
# References as written: since 3a-7 every reference in an unflagged file
# names a live object of its generation (a dangling one is REF_DANGLING, a
# generation mismatch REF_GENERATION), so none is read as null here (#52
# item 1). §7.3.7: a dictionary entry whose value is null is the same as no
# entry.
NULL = ("null",)


def _dict_form(pairs: Iterable[tuple[bytes, object]]) -> object:
    return ("dict", tuple(sorted(((k, v) for k, v in pairs if v != NULL),
                                 key=lambda kv: kv[0])))


def value_form(value: PdfValue | None, text: bytes) -> object:
    """A value we parsed out of *text* in the comparison's form."""
    match value:
        case None | PdfNull():
            return NULL
        case PdfBool(value=v):
            return ("bool", v)
        case PdfInt(value=v):
            return ("int", v)
        case PdfReal(start=start, end=end):
            return ("real", Fraction(Decimal(text[start:end].decode("ascii"))))
        case PdfName(raw=raw):
            return ("name", raw)
        case PdfString(raw=raw):
            return ("str", raw)
        case PdfRef(num=num, gen=gen):
            return ("ref", num, gen)
        case PdfArray(items=items):
            return ("array", tuple(value_form(item, text) for item in items))
        case PdfDict(entries=entries):
            return _dict_form((key.raw, value_form(item, text)) for key, item in entries)
    return ("other", type(value).__name__)


def text_form(text: bytes) -> object:
    """A reader's printed value (MuPDF's pretty print) in that form."""
    return value_form(ObjectParser(text).parse_value_at(0).value, text)


_PIKE = pikepdf.ObjectType


def libqpdf_form(obj: Any, top: bool = False) -> object:
    """A pikepdf object (opened in explicit-conversion mode) in that form,
    read without resolving: a nested indirect object is its reference.
    Nothing here touches .pages, docinfo or repr, which run qpdf's
    page-tree repair."""
    if obj is None:
        return NULL
    if not top and obj.is_indirect:
        return ("ref", *obj.objgen)
    kind = obj._type_code
    if kind is _PIKE.null:
        return NULL
    if kind is _PIKE.boolean:
        return ("bool", bool(obj))
    if kind is _PIKE.integer:
        return ("int", int(obj))
    if kind is _PIKE.real:
        return ("real", Fraction(obj.as_decimal()))
    if kind is _PIKE.name_:
        return ("name", bytes(obj)[1:])
    if kind is _PIKE.string:
        return ("str", bytes(obj))
    if kind is _PIKE.array:
        return ("array", tuple(libqpdf_form(item) for item in obj))
    if kind in (_PIKE.dictionary, _PIKE.stream):
        # items(), not get(key): a key that is not UTF-8 (/A#FF) comes back
        # surrogate-escaped, and pikepdf will not look such a string up.
        return _dict_form((key.encode("utf-8", "surrogateescape")[1:], libqpdf_form(value))
                          for key, value in obj.items())
    return ("other", str(kind))


def float32_ordinal(value: Fraction) -> int:
    """*value* rounded once (to nearest, ties to even) to an IEEE single,
    as an ordinal: neighbouring floats differ by 1, -0 and +0 are both 0,
    and the sign is the ordinal's. Exact: Python has no float32, and going
    through a float64 would round twice."""
    if value == 0:
        return 0
    sign, size = (-1 if value < 0 else 1), abs(value)
    exponent = size.numerator.bit_length() - size.denominator.bit_length()
    if Fraction(2) ** exponent > size:
        exponent -= 1  # now 2^exponent <= size < 2^(exponent + 1)
    if exponent < -126:  # subnormal: multiples of 2^-149
        return sign * round(size * 2 ** 149)
    mantissa = round(size * Fraction(2) ** (23 - exponent))
    if mantissa == 1 << 24:
        mantissa, exponent = 1 << 23, exponent + 1
    if exponent > 127:
        return sign * (0xFF << 23)  # infinity
    return sign * ((exponent + 127) << 23 | (mantissa - (1 << 23)))


def same_as_mupdf(ours: object, mupdf: object) -> bool:
    """Our form against MuPDF's: equal, except that MuPDF holds a real as a
    32-bit float and converts it without correct rounding (at most 9
    significant digits), so a real may be one float32 step from ours
    rounded once -- measured over 18,127 random reals (17,946 exact, 181
    one step), the magnitudes where it goes further flagged by the
    inventory (NumberRule). MuPDF prints a whole real as an integer, so
    there the kinds are not compared; libqpdf compares them exactly."""
    if isinstance(ours, tuple) and isinstance(mupdf, tuple) and ours and mupdf:
        if ours[0] == "real" and mupdf[0] in ("real", "int"):
            return abs(float32_ordinal(ours[1]) - float32_ordinal(Fraction(mupdf[1]))) <= 1
        if ours[0] == mupdf[0] == "array":
            return len(ours[1]) == len(mupdf[1]) and all(
                same_as_mupdf(a, b) for a, b in zip(ours[1], mupdf[1]))
        if ours[0] == mupdf[0] == "dict":
            return [k for k, _ in ours[1]] == [k for k, _ in mupdf[1]] and all(
                same_as_mupdf(a, b) for (_, a), (_, b) in zip(ours[1], mupdf[1]))
    return ours == mupdf


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
    OBJECT_VALUE = "object_value"      # an uncompressed object's value (MuPDF or libqpdf)
    MEMBER_VALUE = "member_value"      # an object-stream member's value (MuPDF or libqpdf)
    TRAILER_VALUE = "trailer_value"    # a revision's trailer (MuPDF or libqpdf)
    LIBQPDF_WARNING = "libqpdf_warning"  # libqpdf warned while reading a value
    DEAD_BODIES = "dead_bodies"        # dead bodies not exactly the unlisted N G obj
    QPDF_CHECK = "qpdf_check"          # a qpdf --check structural warning or error
    MUPDF_WARNING = "mupdf_warning"    # a MuPDF warning, or MuPDF repaired the file
    NEEDS_PASSWORD = "needs_password"  # a reader cannot open it without a password
    ENCRYPTION = "encryption"          # our trailer, MuPDF and qpdf differ on encryption
    PAGE_LIST = "page_list"            # a revision's pages, in order (MuPDF or libqpdf)
    PAGE_ATTRIBUTE = "page_attribute"  # a page's inherited attribute (MuPDF or libqpdf)
    NAME_TREE = "name_tree"            # the /EmbeddedFiles entries (MuPDF or libqpdf)
    ANNOTATION_LIST = "annotation_list"  # a revision's annotations, page by page (MuPDF)
    ANNOTATION = "annotation"          # an annotation synthesized or not, as MuPDF draws it
    REACHABLE = "reachable"            # a revision's reachable objects (MuPDF or libqpdf)
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
    qpdf_check_errors: int = 0     # revisions where qpdf --check printed ERROR (gated)
    values_compared: int = 0       # objects and trailers compared with both readers
    annotations_compared: int = 0  # annotations listed and classified against MuPDF (3a-7d)
    reachable_compared: int = 0    # revisions whose reachable set both readers confirmed (3a-7e)
    reachable_ms: int = 0          # milliseconds the reachable comparison took (#52 item 4)

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
                "members_compared": self.members_compared,
                "qpdf_check_errors": self.qpdf_check_errors,
                "values_compared": self.values_compared,
                "annotations_compared": self.annotations_compared,
                "reachable_compared": self.reachable_compared,
                "reachable_ms": self.reachable_ms}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Agreement:
        return cls(Status(raw["status"]), tuple(FlagReason(r) for r in raw["reasons"]),
                   Check(raw["check"]) if raw["check"] else None, raw["revision"], raw["obj"],
                   tuple(raw["templates"]), raw["encrypted"], raw["streams_compared"],
                   raw["members_compared"], raw["qpdf_check_errors"],
                   raw["values_compared"], raw.get("annotations_compared", 0),
                   raw.get("reachable_compared", 0), raw.get("reachable_ms", 0))


class _Disagreement(Exception):
    def __init__(self, check: Check, revision: int | None = None, obj: int | None = None,
                 templates: tuple[str, ...] = ()) -> None:
        super().__init__(check.value)
        self.check, self.revision, self.obj, self.templates = check, revision, obj, templates


def compare(data: bytes, workdir: Path, inv: Inventory | None = None) -> Agreement:
    """The inventory of *data* against MuPDF and qpdf (CLI and libqpdf).
    Every live object's value, each object-stream member's and each
    revision's trailer are compared with both readers. Revision cuts are
    written to *workdir*. Never raises on a reader's answer: anything that
    stops the comparison is a disagreement (ORACLE_ERROR)."""
    if inv is None:
        inv = inventory(data)
    if not inv.tiles:
        return Agreement(Status.DISAGREES, check=Check.TILING)
    if inv.flags:
        return Agreement(Status.FLAGGED, reasons=tuple(dict.fromkeys(f.reason for f in inv.flags)))
    stats = _Stats()
    try:
        if not qpdf_versions_match():
            return Agreement(Status.DISAGREES, check=Check.ORACLE_ERROR,
                             templates=("qpdf version mismatch",))
        _compare(data, workdir, inv, stats)
    except _Disagreement as found:
        return Agreement(Status.DISAGREES, check=found.check, revision=found.revision,
                         obj=found.obj, templates=found.templates, **stats.fields())
    except Exception as error:  # fail closed: an unfinished comparison never agrees
        return Agreement(Status.DISAGREES, check=Check.ORACLE_ERROR,
                         templates=(type(error).__name__,), **stats.fields())
    return Agreement(Status.AGREES, **stats.fields())


@dataclass
class _Stats:
    encrypted: bool = False
    streams: int = 0
    members: int = 0
    qpdf_errors: int = 0
    values: int = 0
    annotations: int = 0
    reachable: int = 0
    reachable_seconds: float = 0.0

    def fields(self) -> dict[str, Any]:
        return {"encrypted": self.encrypted, "streams_compared": self.streams,
                "members_compared": self.members, "qpdf_check_errors": self.qpdf_errors,
                "values_compared": self.values, "annotations_compared": self.annotations,
                "reachable_compared": self.reachable,
                "reachable_ms": round(1000 * self.reachable_seconds)}


def _compare(data: bytes, workdir: Path, inv: Inventory, stats: _Stats) -> None:
    opened: list[Any] = []  # one revision's MuPDF and pikepdf documents at a time
    try:
        with pikepdf.explicit_conversion():
            _compare_revisions(data, workdir, inv, stats, opened)
    finally:
        for doc in opened:
            doc.close()


class _Libqpdf:
    """libqpdf's reading of one revision (pikepdf): exactly what qpdf
    reads -- no recovery, no page-attribute pushing (which rewrites page
    objects) -- and every warning it gives, drained after each read
    (pikepdf empties the list on each call)."""

    def __init__(self, path: Path, undecoded: Collection[int]) -> None:
        self.path, self.undecoded = path, undecoded
        self.pdf = pikepdf.Pdf.open(path, attempt_recovery=False,
                                    inherit_page_attributes=False)

    def warnings(self) -> list[bytes]:
        lines = [b"WARNING: " + w.encode("utf-8", "surrogateescape")
                 for w in self.pdf.get_warnings()]
        return structural(lines, self.path, undecoded=self.undecoded)

    def close(self) -> None:
        self.pdf.close()


def _undecoded(inv: Inventory, chain: Chain) -> set[int]:
    """Data starts of the streams the inventory does not decode: all but
    object streams and cross-reference streams."""
    decoded = set(inv.object_streams)
    for section in chain.sections:
        for part in (section, section.xref_stm):
            if part is not None and part.is_stream:
                decoded.add(part.offset)
    return {span.start for start, span in inv.stream_data.items() if start not in decoded}


def _compare_revisions(data: bytes, workdir: Path, inv: Inventory, stats: _Stats,
                       opened: list[Any]) -> None:
    pymupdf.TOOLS.mupdf_warnings()
    listed: set[int] = set()
    chain = chain_of(data)
    units: dict[UnitRef, Unit] = {u.ref: u for u in inv.units}  # refs nest one level at most
    undecoded = _undecoded(inv, chain)
    for revision in range(inv.revisions):
        trailer = chain.sections[chain.revisions[revision][0]].trailer
        ours_encrypted = trailer is not None and trailer.get(b"Encrypt") is not None
        blob = revision_blob(chain, data, revision)
        path = workdir / f"r{revision}.pdf"
        path.write_bytes(blob)
        for previous in opened:
            previous.close()
        opened.clear()
        doc = pymupdf.open(stream=blob, filetype="pdf")
        opened.append(doc)
        if doc.needs_pass:
            # The gate flags a needs_password file rather than failing it
            # (owner decision, 2026-10-12), so only when our trailer reads
            # it encrypted too; otherwise the readers split on encryption.
            raise _Disagreement(Check.NEEDS_PASSWORD if ours_encrypted else Check.ENCRYPTION,
                                revision)
        # Readers decrypt what they read; the inventory does not until 3a-8/9,
        # so an encrypted revision's bytes and values are not compared -- but
        # only when the readers themselves read it encrypted, as we do.
        if not ours_encrypted == mupdf_encrypted(doc) == qpdf_encrypted(path):
            raise _Disagreement(Check.ENCRYPTION, revision)
        encrypted = ours_encrypted
        stats.encrypted = stats.encrypted or encrypted
        qmap, code = qpdf_map(path)
        if code != 0:
            raise _Disagreement(Check.OBJECT_SET, revision, templates=(f"qpdf exit {code}",))
        libqpdf = _Libqpdf(path, () if encrypted else undecoded)
        opened.append(libqpdf)
        _no_libqpdf_warning(libqpdf, revision)
        _drop_offset_zero(qmap, doc, libqpdf, revision)
        bodies = inv.bodies_by_number(revision)
        if set(bodies) != set(qmap):
            raise _Disagreement(Check.OBJECT_SET, revision,
                                min(set(bodies) ^ set(qmap)))
        lines = qpdf_warnings(path)
        stats.qpdf_errors += any(line.startswith(b"ERROR") for line in lines)
        found = structural(lines, path, linearized=b"File is linearized" in lines,
                           undecoded=() if encrypted else undecoded)
        if found:
            raise _Disagreement(Check.QPDF_CHECK, revision,
                                templates=tuple(scrub(line) for line in found[:3]))
        if doc.is_repaired:
            raise _Disagreement(Check.MUPDF_WARNING, revision,
                                templates=_mupdf_templates() or ("repaired",))
        decoded_homes: dict[int, bytes] = {}
        parser = ObjectParser(blob)
        for number, (kind, a, b) in sorted(qmap.items()):
            body = bodies[number]
            if kind == "u":
                listed.add(a)
                if (body.kind, body.start) != (UnitKind.OBJECT, a):
                    raise _Disagreement(Check.OBJECT_SET, revision, number)
                span = inv.stream_data.get(body.start)
                if span is not None:
                    if not encrypted:
                        stats.streams += 1
                        if doc.xref_stream_raw(number) != blob[span.start:span.end]:
                            raise _Disagreement(Check.STREAM_DATA, revision, number)
                elif doc.xref_is_stream(number):
                    raise _Disagreement(Check.STREAM_DATA, revision, number)
                if encrypted:
                    continue
                held = parser.parse_indirect_at(a, max(s.end for s in units[body].spans))
                mine = value_form(held.value if held is not None else None, blob)
                _values_agree(doc, libqpdf, number, b, mine, Check.OBJECT_VALUE, revision)
                stats.values += 1
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
            stats.members += 1
            span = units[body].spans[0]
            member = ObjectParser(decoded).parse_value_at(span.start, span.end).value
            _values_agree(doc, libqpdf, number, 0, value_form(member, decoded),
                          Check.MEMBER_VALUE, revision)
            stats.values += 1
        # The trailer: not encrypted even in an encrypted revision.
        mine = value_form(trailer, data)
        mu = text_form(doc.pdf_trailer(compressed=False).encode("latin-1"))
        theirs = libqpdf_form(libqpdf.pdf.trailer, top=True)
        _no_libqpdf_warning(libqpdf, revision)
        if not same_as_mupdf(mine, mu) or theirs != mine:
            raise _Disagreement(Check.TRAILER_VALUE, revision)
        stats.values += 1
        _pages_agree(doc, path, inv.pages[revision], revision, encrypted, stats)
        _embedded_files_agree(doc, libqpdf, inv.trees[revision], revision, encrypted, stats)
        _annotations_agree(doc, blob, inv.annotations[revision], revision, stats)
        _reachable_agree(blob, path, inv.labels[revision], revision,
                         () if encrypted else undecoded, stats)
    _dead_bodies_agree(data, inv, listed)
    warnings = _mupdf_templates()
    if warnings:
        raise _Disagreement(Check.MUPDF_WARNING, templates=warnings)


def _kind(form: object) -> object:
    return form[:1] if isinstance(form, tuple) else form


def _box(form: object) -> Box | None:
    """A box in the comparison's form (four numbers) as MuPDF holds it."""
    if not (isinstance(form, tuple) and form[:1] == ("array",) and len(form[1]) == 4):
        return None
    values = [float(v[1]) for v in form[1] if isinstance(v, tuple) and v[0] in ("int", "real")]
    if len(values) != 4:
        return None
    x0, y0, x1, y1 = (f32(v) for v in values)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _close(ours: Iterable[float], theirs: Iterable[float]) -> bool:
    """Equal within two 32-bit float steps each (MuPDF computes in floats)."""
    return all(abs(float32_ordinal(Fraction(a)) - float32_ordinal(Fraction(b))) <= 2
               for a, b in zip(ours, theirs, strict=True))


def _effective_agree(page: pymupdf.Page, inherited: dict[str, object]) -> bool:
    """MuPDF's page as shown -- its media box, its crop box (flipped
    against the media box's top), its rotation and its rectangle (the
    crop box clipped to the media box, rotated) -- against what the
    canonical attributes say it is (ADR 0011 item 8): a box MuPDF
    replaced (a 1x1 page, 612x792) or scaled (/UserUnit) disagrees."""
    media = _box(inherited["MediaBox"])
    if media is None:
        return False
    crop = _box(inherited["CropBox"]) or media
    rotate = inherited["Rotate"]
    turns = (rotate[1] if isinstance(rotate, tuple) and rotate[:1] == ("int",) else 0) % 360
    flipped = (crop[0], f32(media[3] - crop[3]), crop[2], f32(media[3] - crop[1]))
    clipped = intersect(crop, media)
    w, h = clipped[2] - clipped[0], clipped[3] - clipped[1]
    shown = (0.0, 0.0, h, w) if turns in (90, 270) else (0.0, 0.0, w, h)
    return (_close(media, page.mediabox) and _close(flipped, page.cropbox)
            and page.rotation == turns and _close(shown, page.rect))


def _pages_agree(doc: pymupdf.Document, path: Path, pages: tuple[Page, ...] | None,
                 revision: int, encrypted: bool, stats: _Stats) -> None:
    """The revision's page list and each page's inherited attributes
    (ADR 0011 item 8): as written, ours against MuPDF's own inheritance
    (pdf_dict_get_inheritable) and libqpdf's, read from a separate
    instance that pushes the attributes down to each page (owner decision
    9, P2: pikepdf reads an inherited /Resources no other way), whose
    warnings are gated like the first's -- the push makes an inherited
    direct value a new object there, compared by value; and as MuPDF
    shows the page (_effective_agree). An encrypted revision's direct
    /Resources holds encrypted strings in our reading: compared by kind
    there."""
    if pages is None:  # the inventory flagged the tree: never reached unflagged
        raise _Disagreement(Check.PAGE_LIST, revision, templates=("no page list",))
    mine = [(page.number, page.gen) for page in pages]
    pdf = pymupdf._as_pdf_document(doc)
    mupdf = pymupdf.mupdf
    shown = [doc[i] for i in range(doc.page_count)]  # loads the tree first
    objects = [mupdf.pdf_lookup_page_obj(pdf, i) for i in range(mupdf.pdf_count_pages(pdf))]
    if [(mupdf.pdf_to_num(o), mupdf.pdf_to_gen(o)) for o in objects] != mine:
        raise _Disagreement(Check.PAGE_LIST, revision, templates=("mupdf",))
    with pikepdf.explicit_conversion(), pikepdf.Pdf.open(
            path, attempt_recovery=False, inherit_page_attributes=True) as pushed:
        theirs = [page.obj.objgen for page in pushed.pages]
        warnings = structural([b"WARNING: " + w.encode("utf-8", "surrogateescape")
                               for w in pushed.get_warnings()], path)
        if warnings:
            raise _Disagreement(Check.LIBQPDF_WARNING, revision,
                                templates=tuple(scrub(line) for line in warnings[:3]))
        if theirs != mine:
            raise _Disagreement(Check.PAGE_LIST, revision, templates=("libqpdf",))
        for page, obj, qpage, view in zip(pages, objects, pushed.pages, shown):
            resolved: dict[str, object] = {}
            for key in (k.decode() for k in INHERITED):
                found = page.attributes.get(key.encode())
                ours = value_form(found.value, found.text) if found is not None else NULL
                inherited = mupdf.pdf_dict_get_inheritable(obj, mupdf.pdf_new_name(key))
                text = mupdf.pdf_sprint_obj(None, 0, inherited, 0, 0)[0]
                mu = NULL if mupdf.pdf_is_null(inherited) else text_form(
                    text.encode("latin-1"))
                value = mupdf.pdf_resolve_indirect(inherited)
                resolved[key] = text_form(
                    mupdf.pdf_sprint_obj(None, 0, value, 0, 0)[0].encode("latin-1"))
                # Pushing makes an inherited direct value a new indirect object
                # in qpdf's copy: compared by value then. A reference the file
                # writes stays a reference, compared as one.
                got = qpage.obj.get("/" + key)
                direct = not (isinstance(ours, tuple) and ours[:1] == ("ref",))
                qp = libqpdf_form(got, top=direct)
                if (encrypted and key == "Resources" and isinstance(ours, tuple)
                        and ours[:1] == ("dict",)):
                    agree = _kind(mu) == _kind(qp) == ("dict",)
                else:
                    agree = same_as_mupdf(ours, mu) and qp == ours
                if not agree:
                    raise _Disagreement(Check.PAGE_ATTRIBUTE, revision, page.number,
                                        templates=(key,))
                stats.values += 1
            if not _effective_agree(view, resolved):
                raise _Disagreement(Check.PAGE_ATTRIBUTE, revision, page.number,
                                    templates=("as shown",))


def _embedded_files_agree(doc: pymupdf.Document, libqpdf: _Libqpdf, trees: Trees | None,
                          revision: int, encrypted: bool, stats: _Stats) -> None:
    """The /EmbeddedFiles name tree's entries (ADR 0011 item 8): ours, in
    order, against MuPDF's pdf_load_name_tree (a dictionary: compared as
    a set) and libqpdf's name-tree helper (in order) -- keys as text, as
    each reads them (a trailing NUL: MuPDF cuts it, libqpdf keeps it),
    values as written. An encrypted revision's keys are encrypted in our
    reading and its non-empty trees flagged (P4): not compared."""
    if trees is None:
        raise _Disagreement(Check.NAME_TREE, revision, templates=("no trees",))
    if encrypted:
        return
    entries = trees.get(b"EmbeddedFiles", ())
    ours = [(key_text(e.key) if isinstance(e.key, bytes) else None,
             value_form(e.value, e.text)) for e in entries]
    # libqpdf keeps a key's trailing NUL, which MuPDF cuts (key_text).
    ours_qpdf = [(qpdf_key_text(e.key) if isinstance(e.key, bytes) else None, value)
                 for e, (_, value) in zip(entries, ours)]
    mupdf = pymupdf.mupdf
    pdf = pymupdf._as_pdf_document(doc)
    loaded = mupdf.pdf_load_name_tree(pdf, mupdf.pdf_new_name("EmbeddedFiles"))
    mu = []
    for i in range(mupdf.pdf_dict_len(loaded)):
        key = mupdf.pdf_to_name(mupdf.pdf_dict_get_key(loaded, i))
        value = mupdf.pdf_sprint_obj(None, 0, mupdf.pdf_dict_get_val(loaded, i), 0, 0)[0]
        mu.append((key, text_form(value.encode("latin-1"))))
    names = libqpdf.pdf.Root.get("/Names")
    tree = names.get("/EmbeddedFiles") if names is not None else None
    qp = ([(key, libqpdf_form(value)) for key, value in pikepdf.NameTree(tree).items()]
          if tree is not None else [])
    _no_libqpdf_warning(libqpdf, revision)
    # MuPDF's dictionary has no order: matched by key (keys are unique and
    # in order on an unflagged tree), values within MuPDF's float32 reals.
    by_key = dict(mu)
    if (len(mu) != len(ours) or len(by_key) != len(mu)
            or not all(key in by_key and same_as_mupdf(value, by_key[key])
                       for key, value in ours)
            or qp != ours_qpdf):
        raise _Disagreement(Check.NAME_TREE, revision, templates=("EmbeddedFiles",))
    stats.values += len(ours)


def _annotations_agree(doc: pymupdf.Document, blob: bytes,
                       annotations: tuple[Annotation, ...] | None, revision: int,
                       stats: _Stats) -> None:
    """Each page's annotations as MuPDF lists them, and each one's
    classification against MuPDF's drawing (ADR 0011 items 5 and 8,
    owner decision 11; eval/scorecard/annotations.py). MuPDF's warnings
    so far -- the file's own, its pages loaded and their appearances
    synthesized -- are gated first, those of the pages reloaded for the
    list too; only those it gives while loading and drawing the gate's two
    copies are not the file's and are discarded."""
    if annotations is None:  # the page list is flagged: never reached unflagged
        raise _Disagreement(Check.ANNOTATION_LIST, revision, templates=("no annotations",))

    def gate_warnings() -> None:
        warnings = _mupdf_templates()
        if warnings:
            raise _Disagreement(Check.MUPDF_WARNING, revision, templates=warnings)

    def fail(what: str, number: int | None) -> Exception:
        check = Check.ANNOTATION_LIST if what == "list" else Check.ANNOTATION
        return _Disagreement(check, revision, number, templates=(what,))
    gate_warnings()
    stats.annotations += compare_annotations(doc, blob, annotations, fail, gate_warnings)


def _reachable_agree(blob: bytes, path: Path, labels: RevisionLabels | None, revision: int,
                     undecoded: Collection[int], stats: _Stats) -> None:
    """The revision's reachable objects (ADR 0011 item 5; coordinator
    decision Q5, 2026-10-15): ours, exactly, against what each reader
    keeps when it writes the revision with unused objects dropped -- MuPDF
    garbage-collecting (`tobytes(garbage=1)`: numbers kept, the written
    table's in-use entries) and libqpdf in QDF mode (each object's
    `%% Original object ID` comment), every stream's data as it is. A
    writer drops what it rewrites, though its reader reads it: a stream's
    indirect /Length (both write it direct), the trailer's /Encrypt (both
    write the revision unencrypted) and /ID (qpdf writes a new one, MuPDF a
    new second element) and, in qpdf, a stream's /DecodeParms without
    /Filter -- a key libqpdf reads as written (it is in the stream's
    dictionary as libqpdf reads it, its reference resolved; the value
    comparison checks it), which its writer drops because no filter uses
    it. Those references are added back as each reader reads
    them, and each such target must hold no reference of its own (an
    integer /Length on an unflagged file): otherwise the reader's set is
    not exact there and the comparison fails closed. The 3a-7e spike
    (ADR 0011's notes) found the rest is every reference from the
    trailer, /Parent included, and nothing else."""
    if labels is None:  # no page list: flagged, never reached unflagged
        raise _Disagreement(Check.REACHABLE, revision, templates=("no labels",))
    started = time.perf_counter()
    ours = labels.reachable
    for name, theirs in (("mupdf", _mupdf_reachable(blob)),
                         ("libqpdf", _libqpdf_reachable(path, undecoded, revision))):
        if theirs is None:
            raise _Disagreement(Check.REACHABLE, revision, templates=(name, "not exact"))
        if theirs != ours:
            raise _Disagreement(Check.REACHABLE, revision, min(theirs ^ ours), templates=(name,))
    stats.reachable += 1
    stats.reachable_seconds += time.perf_counter() - started


_WRITTEN_TAIL = re.compile(rb"startxref\r?\n(\d+)\r?\n%%EOF\s*\Z")
_WRITTEN_TABLE = re.compile(rb"xref\r?\n")
_TABLE_SUBSECTION = re.compile(rb"(\d+) (\d+)\r?\n")
_TABLE_ENTRY = re.compile(rb"\d{10} \d{5} ([nf])(?: \r| \n|\r\n)")


def _in_use(written: bytes) -> set[int]:
    """The in-use numbers of the one classic table MuPDF wrote: the one
    its final startxref names (a stream's data may hold a table too, an
    embedded PDF's), with no /Prev."""
    tail = _WRITTEN_TAIL.search(written, max(0, len(written) - 64))
    if tail is None:
        raise ValueError("no startxref")
    head = _WRITTEN_TABLE.match(written, int(tail.group(1)))
    trailer = written.find(b"trailer", head.end()) if head is not None else -1
    if head is None or trailer < 0 or b"/Prev" in written[trailer:tail.start()]:
        raise ValueError("not one classic table")
    found: set[int] = set()
    at = head.end()
    while (sub := _TABLE_SUBSECTION.match(written, at)) is not None:
        first, count = int(sub.group(1)), int(sub.group(2))
        at = sub.end()
        for i in range(count):
            entry = _TABLE_ENTRY.match(written, at)
            if entry is None:
                raise ValueError("malformed table")
            if entry.group(1) == b"n" and first + i:
                found.add(first + i)
            at = entry.end()
    return found


def _refs_in(text: bytes) -> list[PdfRef]:
    """Every reference in one value as a reader prints it."""
    value = ObjectParser(text).parse_value_at(0, len(text)).value
    found: list[PdfRef] = []
    stack: list[PdfValue] = [value] if value is not None else []
    while stack:
        item = stack.pop()
        if isinstance(item, PdfRef):
            found.append(item)
        elif isinstance(item, PdfDict):
            stack.extend(v for _, v in item.entries)
        elif isinstance(item, PdfArray):
            stack.extend(item.items)
    return found


def _mupdf_reachable(blob: bytes) -> set[int] | None:
    """MuPDF's reachable set (_reachable_agree), from a document of its
    own: its writer rewrites /Length, /Encrypt and /ID in the document it
    writes, so they are read first."""
    with pymupdf.open(stream=blob, filetype="pdf") as doc:
        rewritten: dict[int, list[PdfRef]] = {}  # holder (-1: the trailer) -> references
        # /Encrypt: written unencrypted. /ID: MuPDF writes a new second
        # element (a direct /ID [6 0 R 7 0 R] keeps 6, drops 7).
        rewritten[-1] = []
        for key in ("Encrypt", "ID"):
            kind, value = doc.xref_get_key(-1, key)
            if kind != "null":
                rewritten[-1] += _refs_in(value.encode("latin-1"))
        for number in range(1, doc.xref_length()):
            if doc.xref_is_stream(number):
                kind, value = doc.xref_get_key(number, "Length")
                if kind == "xref":
                    rewritten[number] = _refs_in(value.encode("latin-1"))
        for ref in (r for refs in rewritten.values() for r in refs):
            if _refs_in(doc.xref_object(ref.num, compressed=True).encode("latin-1")):
                return None
        kept = _in_use(doc.tobytes(garbage=1))
    return kept | {r.num for holder, refs in rewritten.items()
                   if holder == -1 or holder in kept for r in refs}


def _pike_refs(*values: Any) -> list[tuple[int, int]]:
    """Every reference in libqpdf values (their direct parts walked; a
    value that is itself a reference is one)."""
    found: list[tuple[int, int]] = []
    stack = list(values)
    while stack:
        item = stack.pop()
        if isinstance(item, pikepdf.Object) and item.is_indirect:
            found.append(item.objgen)
        elif isinstance(item, pikepdf.Stream):
            stack.extend(item.stream_dict.values())
        elif isinstance(item, pikepdf.Dictionary):
            stack.extend(item.values())
        elif isinstance(item, pikepdf.Array):
            stack.extend(item)
    return found


def _libqpdf_reachable(path: Path, undecoded: Collection[int], revision: int) -> set[int] | None:
    """libqpdf's reachable set (_reachable_agree), from an instance of its
    own (no page-attribute pushing, which makes new objects), its
    warnings gated like the first's."""
    libqpdf = _Libqpdf(path, undecoded)
    try:
        pdf = libqpdf.pdf
        out = io.BytesIO()
        pdf.save(out, qdf=True, object_stream_mode=pikepdf.ObjectStreamMode.disable,
                 compress_streams=False, stream_decode_level=pikepdf.StreamDecodeLevel.none,
                 fix_metadata_version=False, preserve_pdfa=False)
        kept = {(int(n), int(g)) for n, g in re.findall(
            rb"(?m)^%% Original object ID: (\d+) (\d+)$", out.getvalue())}
        rewritten: list[tuple[int, int]] = []
        # qpdf writes a new /ID; both write the revision unencrypted.
        rewritten += _pike_refs(pdf.trailer.get("/Encrypt"), pdf.trailer.get("/ID"))
        for objgen in sorted(kept):
            held = pdf.get_object(objgen)
            if not isinstance(held, pikepdf.Stream):
                continue
            stream = held.stream_dict
            rewritten += _pike_refs(stream.get("/Length"))
            if "/Filter" not in stream:
                rewritten += _pike_refs(stream.get("/DecodeParms"))
        for objgen in rewritten:
            held = pdf.get_object(objgen)
            inside = (held.stream_dict.values() if isinstance(held, pikepdf.Stream)
                      else held.values() if isinstance(held, pikepdf.Dictionary)
                      else held if isinstance(held, pikepdf.Array) else ())
            if _pike_refs(*inside):
                return None
        _no_libqpdf_warning(libqpdf, revision)
        return {number for number, _ in kept} | {number for number, _ in rewritten}
    finally:
        libqpdf.close()


def _drop_offset_zero(qmap: dict[int, tuple[str, int, int]], doc: pymupdf.Document,
                      libqpdf: _Libqpdf, revision: int) -> None:
    """qpdf --show-xref lists an in-use entry at offset 0 as an object; the
    inventory reads no object there (xref.no_object). Drop it from qpdf's
    map once both readers read it as null -- MuPDF without a warning,
    libqpdf with only OFFSET_ZERO's -- and disagree otherwise."""
    for number, (kind, offset, gen) in sorted(qmap.items()):
        if kind != "u" or offset != 0 or number == 0:
            continue
        mu = doc.xref_object(number, compressed=True).strip()
        got = libqpdf.pdf.get_object((number, gen))
        _no_libqpdf_warning(libqpdf, revision, number)
        if mu != "null" or got is not None:
            raise _Disagreement(Check.OBJECT_SET, revision, number)
        del qmap[number]


def _no_libqpdf_warning(libqpdf: _Libqpdf, revision: int, number: int | None = None) -> None:
    found = libqpdf.warnings()
    if found:
        raise _Disagreement(Check.LIBQPDF_WARNING, revision, number,
                            templates=tuple(scrub(line) for line in found[:3]))


def _values_agree(doc: pymupdf.Document, libqpdf: _Libqpdf, number: int, gen: int,
                  mine: object, check: Check, revision: int) -> None:
    """Object *number*'s value, ours (*mine*) against MuPDF's pretty print
    (its tight printer runs an empty name into the next token: `/ 2.5`
    prints `/2.5`) and libqpdf's. libqpdf answers None for a null, a
    missing object and an object it failed to read alike: None counts as
    null only when it gave no warning and our value is null too."""
    mu_text = doc.xref_object(number, compressed=False).encode("latin-1")
    got = libqpdf.pdf.get_object((number, gen))
    theirs = libqpdf_form(got, top=True)
    _no_libqpdf_warning(libqpdf, revision, number)
    if not same_as_mupdf(mine, text_form(mu_text)) or theirs != mine:
        raise _Disagreement(check, revision, number)


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
    all integers: linearized files with more than one revision (updated
    incrementally) and with /L not the file's length; comment lines
    after the header (beyond the binary marker) or an intermediate %%EOF;
    streams whose indirect /Length lives in an object stream;
    REVISION_AMBIGUOUS flags whose target is in fact equal in every
    revision; dead object streams; and whether the file is encrypted."""
    out: dict[str, int] = {}
    parser = ObjectParser(data)
    # Encrypted as MuPDF reads it (bytes and values not compared until
    # 3a-8/9), and whether our trailers carry an /Encrypt entry at all.
    out["encrypt_entry"] = int(any(section.trailer is not None
                                   and section.trailer.get(b"Encrypt") is not None
                                   for section in chain_of(data).sections))
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            out["encrypted"] = int(mupdf_encrypted(doc))
    except Exception:
        out["encrypted"], out["mupdf_unopenable"] = 0, 1
    ends = {u.ref.start: u.spans[0].end for u in inv.units
            if u.ref.within is None and u.ref.kind in (UnitKind.OBJECT, UnitKind.DEAD_BODY)}

    def parsed(offset: int) -> PdfValue | None:
        end = ends.get(offset)
        obj = parser.parse_indirect_at(offset, end if end is not None else len(data))
        return obj.value if obj is not None else None

    # Linearized (§F: the first object a /Linearized dictionary). Updated:
    # more than one revision (the first-page pair is one). Separately, /L
    # (the file's length when linearized) not the file's length -- also
    # true of mere trailing bytes, so not the signal on its own.
    head = _LINEARIZED_HEAD.search(data, 0, 1024)
    first = parser.parse_indirect_at(head.start(), min(len(data), head.start() + 4096)) \
        if head else None
    lin = first.value if first is not None and isinstance(first.value, PdfDict) else None
    out["linearized"] = int(lin is not None and lin.get(b"Linearized") is not None)
    length = _int_value(lin.get(b"L")) if lin is not None else None
    out["linearized_updated"] = int(bool(out["linearized"]) and inv.revisions > 1)
    out["linearized_length_mismatch"] = int(bool(out["linearized"]) and length != len(data))

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

    # SYNTHESIZED units (3a-7d, ADR 0011 item 5): in the current revision;
    # (body, /Annots index) pairs of older revisions' units not among the
    # current revision's units (a body rewritten in a later revision counts,
    # though its widget may be synthesized now); and ADR 0011's own count:
    # widgets synthesized in an older revision whose widget, by object
    # number, is not synthesized in the current one.
    current = inv.synthesized[0] if inv.synthesized else ()
    now = {(u.body, u.index) for u in current}
    out["synthesized_current"] = len(current)
    out["synthesized_widgets_current"] = sum(
        1 for a in (inv.annotations[0] or () if inv.annotations else ())
        if a.synthesized and a.widget)
    out["synthesized_older_only"] = len({(u.body, u.index) for units in inv.synthesized[1:]
                                         for u in units} - now)
    widgets_now = {a.number for a in (inv.annotations[0] or () if inv.annotations else ())
                   if a.synthesized and a.widget and a.number is not None}
    out["synthesized_older_widgets"] = len(
        {a.number for found in inv.annotations[1:] for a in (found or ())
         if a.synthesized and a.widget and a.number is not None} - widgets_now)

    # Labels (3a-7e-1, ADR 0011 item 5): the current revision's STRUCTURAL
    # streams by kind; hint streams whose linearization /L is stale (an
    # updated file: coordinator decision Q8, counted apart); revisions
    # without labels; and the reference graph's work (item 7).
    current_labels = inv.labels[0] if inv.labels else None
    by_kind: dict[StructuralKind, int] = {kind: 0 for kind in StructuralKind}
    for records in (current_labels.structural.values() if current_labels else ()):
        for label in {record.kind for record in records}:
            by_kind[label] += 1
    for label, count in by_kind.items():
        out[f"structural_{label.value}"] = count
    out["hint_stream_stale"] = int(by_kind[StructuralKind.HINT_STREAM] > 0
                                   and length != len(data))
    out["labels_missing"] = sum(1 for found in inv.labels if found is None)
    out["reachable_current"] = len(current_labels.reachable) if current_labels else 0
    out["graph_work"] = inv.graph_work

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
    global QPDF_TIMEOUT
    parser = argparse.ArgumentParser(prog="python -m scorecard.inventory")
    parser.add_argument("file", type=Path)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--qpdf-timeout", type=float, default=QPDF_TIMEOUT)
    args = parser.parse_args(argv)
    QPDF_TIMEOUT = args.qpdf_timeout
    # MuPDF prints some messages itself; stdout carries only the JSON.
    pymupdf.set_messages(fd=2)
    data = args.file.read_bytes()
    inv = inventory(data)
    agreement = compare(data, args.workdir, inv=inv)
    try:
        measures = measure(data, inv)
    except Exception as error:  # a measurement never hides the verdict above
        measures = {"measure_error": 1}
        print(type(error).__name__, file=sys.stderr)
    # One line, the last on stdout (the harness reads the last line).
    print("\n" + json.dumps({"agreement": agreement.to_json(), "measures": measures,
                             "regions": regions(inv),
                             "flags": [f.reason.value for f in inv.flags]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
