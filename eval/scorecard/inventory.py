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
import json
import re
import subprocess
import sys
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
from redaction_verifier.inventory.xref import Chain, read_chain
from redaction_verifier.ledger import FlagReason, Unit, UnitKind, UnitRef

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
# A reference to object 0 (always free): both readers read null (qpdf
# 12.3+ warns; MuPDF resolves no object 0). Only object 0: MuPDF resolves
# `1 5 R` by its number where qpdf reads null. Flagging dangling
# references is the reference graph's (3a-7), which removes this entry.
OBJECT_ZERO_REFERENCE = re.compile(
    rb" \(object \d+ \d+, offset \d+\): treating bad indirect reference \(0 \d+ R\) as null")


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
        rest = line[len(prefix):] if line.startswith(prefix) else None
        if rest is not None:
            if linearized and rest.startswith(b": ") and any(
                    lint.fullmatch(rest[2:]) for lint in LINEARIZATION_LINT):
                continue
            truncated = TRUNCATED_FLATE.fullmatch(rest)
            if truncated and int(truncated.group(1)) in undecoded:
                continue
            if OBJECT_ZERO_REFERENCE.fullmatch(rest):
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


# ── Values in one comparable form: ours, MuPDF's, libqpdf's ───────────────
# Kinds kept ("int" vs "real"), integers and reals exact (a real is the
# fraction its text writes, never a float), strings and names as bytes.
# §7.3.10: a reference to an object number with no body in the revision is
# null -- by NUMBER only, so a generation mismatch stays a difference (MuPDF
# resolves `1 5 R` by its number; libqpdf reads null). §7.3.7: a
# dictionary entry whose value is null is the same as no entry.
NULL = ("null",)


def _dict_form(pairs: Iterable[tuple[bytes, object]]) -> object:
    return ("dict", tuple(sorted(((k, v) for k, v in pairs if v != NULL),
                                 key=lambda kv: kv[0])))


def value_form(value: PdfValue | None, text: bytes, live: Collection[int]) -> object:
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
            return ("ref", num, gen) if num in live else NULL
        case PdfArray(items=items):
            return ("array", tuple(value_form(item, text, live) for item in items))
        case PdfDict(entries=entries):
            return _dict_form((key.raw, value_form(item, text, live)) for key, item in entries)
    return ("other", type(value).__name__)


def text_form(text: bytes, live: Collection[int]) -> object:
    """A reader's printed value (MuPDF's pretty print) in that form."""
    return value_form(ObjectParser(text).parse_value_at(0).value, text, live)


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
                "values_compared": self.values_compared}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Agreement:
        return cls(Status(raw["status"]), tuple(FlagReason(r) for r in raw["reasons"]),
                   Check(raw["check"]) if raw["check"] else None, raw["revision"], raw["obj"],
                   tuple(raw["templates"]), raw["encrypted"], raw["streams_compared"],
                   raw["members_compared"], raw["qpdf_check_errors"],
                   raw["values_compared"])


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

    def fields(self) -> dict[str, Any]:
        return {"encrypted": self.encrypted, "streams_compared": self.streams,
                "members_compared": self.members, "qpdf_check_errors": self.qpdf_errors,
                "values_compared": self.values}


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
            raise _Disagreement(Check.NEEDS_PASSWORD, revision)
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
        bodies = inv.bodies_by_number(revision)
        if set(bodies) != set(qmap):
            raise _Disagreement(Check.OBJECT_SET, revision,
                                min(set(bodies) ^ set(qmap)))
        lines = qpdf_warnings(path)
        stats.qpdf_errors += any(line.startswith(b"ERROR") for line in lines)
        found = [line for line in structural(
                     lines, path, linearized=b"File is linearized" in lines,
                     undecoded=() if encrypted else undecoded)
                 if not _catalog_retyped(line, lines, path)]
        if found:
            raise _Disagreement(Check.QPDF_CHECK, revision,
                                templates=tuple(scrub(line) for line in found[:3]))
        if doc.is_repaired:
            raise _Disagreement(Check.MUPDF_WARNING, revision,
                                templates=_mupdf_templates() or ("repaired",))
        libqpdf = _Libqpdf(path, () if encrypted else undecoded)
        opened.append(libqpdf)
        _no_libqpdf_warning(libqpdf, revision)
        decoded_homes: dict[int, bytes] = {}
        live = set(qmap)
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
                mine = value_form(held.value if held is not None else None, blob, live)
                _values_agree(doc, libqpdf, number, b, mine, live, Check.OBJECT_VALUE, revision)
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
            _values_agree(doc, libqpdf, number, 0, value_form(member, decoded, live), live,
                          Check.MEMBER_VALUE, revision)
            stats.values += 1
        # The trailer: not encrypted even in an encrypted revision.
        mine = value_form(trailer, data, live)
        mu = text_form(doc.pdf_trailer(compressed=False).encode("latin-1"), live)
        theirs = libqpdf_form(libqpdf.pdf.trailer, top=True)
        _no_libqpdf_warning(libqpdf, revision)
        if not same_as_mupdf(mine, mu) or theirs != mine:
            raise _Disagreement(Check.TRAILER_VALUE, revision)
        stats.values += 1
    _dead_bodies_agree(data, inv, listed)
    warnings = _mupdf_templates()
    if warnings:
        raise _Disagreement(Check.MUPDF_WARNING, templates=warnings)


def _no_libqpdf_warning(libqpdf: _Libqpdf, revision: int, number: int | None = None) -> None:
    found = libqpdf.warnings()
    if found:
        raise _Disagreement(Check.LIBQPDF_WARNING, revision, number,
                            templates=tuple(scrub(line) for line in found[:3]))


def _values_agree(doc: pymupdf.Document, libqpdf: _Libqpdf, number: int, gen: int,
                  mine: object, live: Collection[int], check: Check, revision: int) -> None:
    """Object *number*'s value, ours (*mine*) against MuPDF's pretty print
    (its tight printer runs an empty name into the next token: `/ 2.5`
    prints `/2.5`) and libqpdf's. libqpdf answers None for a null, a
    missing object and an object it failed to read alike: None counts as
    null only when it gave no warning and our value is null too."""
    mu_text = doc.xref_object(number, compressed=False).encode("latin-1")
    got = libqpdf.pdf.get_object((number, gen))
    theirs = libqpdf_form(got, top=True)
    _no_libqpdf_warning(libqpdf, revision, number)
    if not same_as_mupdf(mine, text_form(mu_text, live)) or theirs != mine:
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
