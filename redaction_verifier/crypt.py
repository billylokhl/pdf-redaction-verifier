"""redaction_verifier.crypt -- each revision's decryption, cross-checked
(docs/adr/0001, as amended 2026-10-15; Phase 3a-9a).

pikepdf (libqpdf) decrypts each encrypted revision's prefix cut with the
empty user password; MuPDF decrypts the same cut independently. What the
two agree on is the revision's decrypted view (``Revision.values`` and
``.streams``, for the inventory in 3a-9b); anything else is flagged, never
trusted on one decryption's word:

- ``DECRYPTION_MISMATCH``: the two decryptions differ (``MismatchRule``),
  or a stream the spec leaves unencrypted is not its stored bytes.
- ``DECRYPTION_UNVERIFIED``: no second decryption vouches for it
  (``CryptRule``): a password or a handler neither reads alike, a reader
  that warned or repaired, a crypt filter the readers read differently,
  an embedded file under its own filter, stored bytes that are already
  plain.

Streams the handler encrypts are compared at MuPDF's ``xref_stream_raw``
stage (decrypted, still filtered: 3a-8 confirmed it is pikepdf's raw
read); dictionaries and other objects value by value. Streams the spec
leaves unencrypted (cross-reference streams; the catalog's /Metadata
under /EncryptMetadata false) are checked against their stored bytes:
MuPDF decrypts those anyway (the amendment pins that divergence).

Outside redaction_verifier/inventory (coordinator decision, 2026-10-15;
ADR 0010's child-side import guard is unchanged): this module needs both
readers. It reads the inventory -- each stream's stored span, each object
stream's capped decode -- and hands plain data back. The legacy CLI never
loads it (tests/test_import_boundaries.py).
"""

from __future__ import annotations

import hashlib
import io
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum
from fractions import Fraction
from typing import Any

import pikepdf
import pymupdf as _pymupdf

from .inventory.objects import (
    ObjectParser, PdfArray, PdfBool, PdfDict, PdfInt, PdfName, PdfNull, PdfReal, PdfRef,
    PdfString, PdfValue)
from .inventory.types import Inventory
from .inventory.xref import Chain
from .ledger import Flag, FlagReason, Span, UnitRef

# PyMuPDF is untyped: every use goes through this one Any.
pymupdf: Any = _pymupdf


class CryptRule(IntEnum):
    """Why something was flagged DECRYPTION_UNVERIFIED (its ``rule``)."""

    PASSWORD = 1       # both readers need a user password we do not have
    AUTH_SPLIT = 2     # one reader opens with the empty password, the other does not
    HANDLER = 3        # a security handler other than /Standard
    OPEN_FAILED = 4    # pikepdf cannot open the cut without recovery
    REPAIRED = 5       # MuPDF repaired the cut: nothing in it is trusted
    PERMS = 6          # /Perms does not check out (qpdf warns; ADR 0001)
    WARNING = 7        # a reader warned while reading this object
    CRYPT_FILTER = 8   # a stream-level /Crypt filter (the readers read it differently)
    EFF = 9            # an embedded file under an /EFF other than /StmF
    STORED_PLAIN = 10  # an "encrypted" stream whose stored bytes inflate as they are
    UNLOCATED = 11     # the inventory holds no stored bytes for this stream


class MismatchRule(IntEnum):
    """Why something was flagged DECRYPTION_MISMATCH (its ``rule``)."""

    STREAM = 1         # decrypted stream bytes differ
    VALUE = 2          # a decrypted dictionary or object differs
    PLAINTEXT = 3      # an unencrypted stream is not its stored bytes


Key = tuple[int, int]                  # (object number, generation)


@dataclass(frozen=True)
class Revision:
    """One revision's decryption. ``identity`` is None when the revision
    is not encrypted; otherwise a digest of its /Encrypt dictionary's
    value and its first /ID string (3a-9b keys reuse on it). ``values``
    and ``streams`` hold only what both readers agreed on: each object's
    value (a stream's: its dictionary) and each stream's decrypted,
    still-filtered bytes."""

    revision: int
    identity: tuple[bytes, bytes] | None
    values: dict[Key, object]
    streams: dict[Key, bytes]
    flags: tuple[Flag, ...]


def cut(chain: Chain, data: bytes, revision: int) -> bytes:
    """*data* cut to *revision* (0 = the whole file), as readers open it:
    the bytes through that revision's section, then its own startxref."""
    return data if revision == 0 else (
        data[:chain.revision_end(revision)]
        + b"\nstartxref\n%d\n%%%%EOF\n" % chain.revision_start(revision))


def decrypt(data: bytes, inventory: Inventory, chain: Chain, *,
            max_bytes: int = 1 << 30) -> tuple[Revision, ...]:
    """Every revision's decryption, current first. *max_bytes* caps the
    bytes read from both readers together (BUDGET_EXHAUSTED past it)."""
    budget = [max_bytes]
    shown = pymupdf.TOOLS.mupdf_display_errors()
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        # Explicit conversion: integers, booleans and the like stay pikepdf
        # objects, so a value's form never depends on Python's coercions.
        with pikepdf.explicit_conversion():
            return tuple(_Revision(cut(chain, data, r), r, inventory, budget).run()
                         for r in range(inventory.revisions))
    finally:
        pymupdf.TOOLS.mupdf_display_errors(shown)


def _flag(reason: FlagReason, rule: int, revision: int, key: Key | None,
          span: Span | None = None) -> Flag:
    number, gen = key if key is not None else (-1, -1)
    return Flag(reason, span, (("object", number), ("gen", gen), ("revision", revision),
                               ("rule", int(rule))))


class _Revision:
    def __init__(self, blob: bytes, revision: int, inventory: Inventory, budget: list[int]):
        self.blob, self.revision, self.inv, self.budget = blob, revision, inventory, budget
        self.flags: list[Flag] = []
        self.values: dict[Key, object] = {}
        self.streams: dict[Key, bytes] = {}
        self.spans: dict[UnitRef, Span] | None = None

    def unverified(self, rule: CryptRule, key: Key | None = None) -> None:
        self.flags.append(_flag(FlagReason.DECRYPTION_UNVERIFIED, rule, self.revision, key,
                                self._span(key)))

    def mismatch(self, rule: MismatchRule, key: Key) -> None:
        self.flags.append(_flag(FlagReason.DECRYPTION_MISMATCH, rule, self.revision, key,
                                self._span(key)))

    def done(self, identity: tuple[bytes, bytes] | None, trusted: bool = True) -> Revision:
        if not trusted:
            self.values.clear()
            self.streams.clear()
        return Revision(self.revision, identity, dict(self.values), dict(self.streams),
                        tuple(self.flags))

    def _span(self, key: Key | None) -> Span | None:
        unit = None if key is None else self.inv.body(key[0], self.revision)
        if unit is None or unit.within is not None:
            return None
        if self.spans is None:                  # built once: flags are many on a hostile file
            self.spans = {u.ref: u.spans[0] for u in self.inv.units if u.spans}
        return self.spans.get(unit)

    def charge(self, *parts: bytes) -> bool:
        self.budget[0] -= sum(len(p) for p in parts)
        if self.budget[0] < 0:
            if not any(f.reason is FlagReason.BUDGET_EXHAUSTED for f in self.flags):
                self.flags.append(Flag(FlagReason.BUDGET_EXHAUSTED, None,
                                       (("revision", self.revision),)))
            return False
        return True

    # ── Opening ─────────────────────────────────────────────────────────
    def run(self) -> Revision:
        try:
            doc = pymupdf.open(stream=self.blob, filetype="pdf")
        except Exception:                      # FileDataError and kin: unreadable
            doc = None
        _drain_mupdf()
        try:
            pdf: pikepdf.Pdf | None = pikepdf.open(
                io.BytesIO(self.blob), attempt_recovery=False, inherit_page_attributes=False)
            locked = False
        except pikepdf.PasswordError:
            pdf, locked = None, True
        except Exception:                      # pikepdf.PdfError and kin
            pdf, locked = None, False
        try:
            return self._opened(doc, pdf, locked)
        finally:
            if pdf is not None:
                pdf.close()
            if doc is not None:
                doc.close()

    def _opened(self, doc: Any, pdf: pikepdf.Pdf | None, locked: bool) -> Revision:
        mu_encrypted = doc is not None and bool(doc.needs_pass or doc.metadata.get("encryption"))
        pk_encrypted = locked or (pdf is not None and pdf.is_encrypted)
        if not (mu_encrypted or pk_encrypted):
            if doc is None and pdf is None:
                return self.done(None)          # neither reads it: not this module's to say
            if doc is not None and pdf is not None:
                return self.done(None)          # both read it unencrypted
        identity = _identity(pdf) if pdf is not None else (b"", b"")
        mu_locked = doc is not None and bool(doc.needs_pass)
        if locked and mu_locked:
            self.unverified(CryptRule.PASSWORD)
            return self.done(identity, trusted=False)
        if locked or mu_locked or doc is None or (pk_encrypted != mu_encrypted):
            self.unverified(CryptRule.AUTH_SPLIT if pdf is not None or locked
                            else CryptRule.OPEN_FAILED)
            return self.done(identity, trusted=False)
        if pdf is None:
            self.unverified(CryptRule.OPEN_FAILED)
            return self.done(identity, trusted=False)
        encrypt = pdf.trailer.get("/Encrypt")
        if encrypt is None or encrypt.get("/Filter") != pikepdf.Name.Standard:
            self.unverified(CryptRule.HANDLER)
            return self.done(identity, trusted=False)
        warnings = pdf.get_warnings()
        if any("/Perms" in w for w in warnings):
            self.unverified(CryptRule.PERMS)
            return self.done(identity, trusted=False)
        if warnings:
            self.unverified(CryptRule.WARNING)
            return self.done(identity, trusted=False)
        self._objects(doc, pdf, encrypt)
        _drain_mupdf()
        if doc.is_repaired:                     # repair is lazy: only known after the reads
            self.unverified(CryptRule.REPAIRED)
            return self.done(identity, trusted=False)
        return self.done(identity)

    # ── Objects ─────────────────────────────────────────────────────────
    def _objects(self, doc: Any, pdf: pikepdf.Pdf, encrypt: Any) -> None:
        plain_metadata = _plain_metadata(pdf, encrypt)
        eff = _eff_differs(encrypt)
        for (number, gen), entry in sorted(pdf.get_xref_table().items()):
            key = (number, gen)
            if entry.type == 0:
                continue
            if number >= doc.xref_length():     # pikepdf lists it, MuPDF has no such number
                self.unverified(CryptRule.WARNING, key)
                continue
            if entry.type == 2 and not self._container_decoded(entry):
                continue                        # the inventory flags what it could not decode
            if not self._object(doc, pdf, key, plain_metadata, eff):
                return

    def _container_decoded(self, entry: Any) -> bool:
        home = self.inv.entry(entry.obj_stream_number, self.revision)
        return home is not None and home[0] == 1 and home[1] in self.inv.object_streams

    def _object(self, doc: Any, pdf: pikepdf.Pdf, key: Key, plain_metadata: Key | None,
                eff: bool) -> bool:
        number, gen = key
        try:
            obj = pdf.get_object(number, gen)
            text = doc.xref_object(number, compressed=False)
        except Exception:
            obj, text = None, ""
        if pdf.get_warnings() or _drain_mupdf() or obj is None:
            self.unverified(CryptRule.WARNING, key)
            return True
        ours = _pike_form(obj, top=True)
        theirs = _text_form(text.encode("latin-1"))
        if not _same(ours, theirs):
            self.mismatch(MismatchRule.VALUE, key)
            return True
        if not isinstance(obj, pikepdf.Stream):
            self.values[key] = ours
            return True
        return self._stream(doc, pdf, obj, key, ours, plain_metadata, eff)

    def _stream(self, doc: Any, pdf: pikepdf.Pdf, obj: pikepdf.Stream, key: Key,
                form: object, plain_metadata: Key | None, eff: bool) -> bool:
        number = key[0]
        filters = obj.get("/Filter")
        names = list(filters) if isinstance(filters, pikepdf.Array) else [filters]
        if pikepdf.Name.Crypt in names:
            self.unverified(CryptRule.CRYPT_FILTER, key)
            return True
        if eff and obj.get("/Type") == pikepdf.Name.EmbeddedFile:
            self.unverified(CryptRule.EFF, key)
            return True
        stored = self._stored(key)
        if stored is None:
            self.unverified(CryptRule.UNLOCATED, key)
            return True
        # A stream the spec leaves unencrypted has no second decryption:
        # MuPDF decrypts it anyway (and may warn doing so), so it is not
        # asked; pikepdf's read must be the stored bytes.
        plain = obj.get("/Type") == pikepdf.Name.XRef or key == plain_metadata
        ours: bytes | None
        theirs: bytes | None
        try:
            ours = bytes(obj.read_raw_bytes())
            theirs = stored if plain else doc.xref_stream_raw(number)
        except Exception:
            ours = theirs = None
        warned = bool(pdf.get_warnings()) | _drain_mupdf()
        if not self.charge(stored, ours or b"", b"" if plain else theirs or b""):
            return False
        if warned or ours is None or theirs is None:
            self.unverified(CryptRule.WARNING, key)
            return True
        if plain:
            if ours != stored:
                self.mismatch(MismatchRule.PLAINTEXT, key)
                return True
        elif ours != theirs:
            self.mismatch(MismatchRule.STREAM, key)
            return True
        elif _inflates(stored):
            self.unverified(CryptRule.STORED_PLAIN, key)
            return True
        self.values[key] = form
        self.streams[key] = ours
        return True

    def _stored(self, key: Key) -> bytes | None:
        unit = self.inv.body(key[0], self.revision)
        if unit is None or unit.within is not None:
            return None
        span = self.inv.stream_data.get(unit.start)
        return None if span is None else self.blob[span.start:span.end]


# ── Helpers ─────────────────────────────────────────────────────────────
def _drain_mupdf() -> bool:
    """Whether MuPDF warned since the last drain (and forget it)."""
    return bool(pymupdf.TOOLS.mupdf_warnings(reset=True))


def _identity(pdf: pikepdf.Pdf) -> tuple[bytes, bytes]:
    encrypt = pdf.trailer.get("/Encrypt")
    ident = pdf.trailer.get("/ID")
    first = bytes(ident[0]) if isinstance(ident, pikepdf.Array) and len(ident) else b""
    form = repr(_pike_form(encrypt, top=True)).encode() if encrypt is not None else b""
    return hashlib.sha256(form).digest(), first


def _plain_metadata(pdf: pikepdf.Pdf, encrypt: Any) -> Key | None:
    """The catalog's /Metadata stream, when /EncryptMetadata false leaves
    it unencrypted (ISO 32000-1 7.6.1: the document-level stream only)."""
    flag = encrypt.get("/EncryptMetadata")
    if flag is None or bool(flag):
        return None
    root = pdf.trailer.get("/Root")
    metadata = root.get("/Metadata") if root is not None else None
    if metadata is None or not metadata.is_indirect:
        return None
    number, gen = metadata.objgen
    return number, gen


def _eff_differs(encrypt: Any) -> bool:
    """Whether embedded files have their own crypt filter: both readers
    ignore /EFF and apply /StmF (so a plain attachment reads as garbage)."""
    eff = encrypt.get("/EFF")
    return eff is not None and eff != encrypt.get("/StmF", pikepdf.Name.Identity)


def _inflates(stored: bytes, cap: int = 1 << 16) -> bool:
    """Whether *stored* is a zlib stream that inflates without error (to
    its end or *cap* bytes): plain Flate stored where ciphertext belongs.
    Ciphertext does that essentially never."""
    try:
        z = zlib.decompressobj()
        out = z.decompress(stored, cap)
    except zlib.error:
        return False
    return bool(out) and (z.eof or len(out) >= cap)


# ── Value forms: what both readers decrypted, compared by value ─────────
NULL = ("null",)
_PIKE = pikepdf.ObjectType


def _dict_form(pairs: Iterator[tuple[bytes, object]]) -> object:
    return ("dict", tuple(sorted(((k, v) for k, v in pairs if v != NULL),
                                 key=lambda kv: kv[0])))


def _pike_form(obj: Any, top: bool = False) -> object:
    """A pikepdf object as a plain value, read without resolving: a nested
    indirect object is its reference."""
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
        return ("array", tuple(_pike_form(item) for item in obj))
    if kind in (_PIKE.dictionary, _PIKE.stream):
        return _dict_form((key.encode("utf-8", "surrogateescape")[1:], _pike_form(value))
                          for key, value in obj.items())
    return ("other", str(kind))


def _value_form(value: PdfValue | None, text: bytes) -> object:
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
            return ("array", tuple(_value_form(item, text) for item in items))
        case PdfDict(entries=entries):
            return _dict_form((key.raw, _value_form(item, text)) for key, item in entries)
    return ("other", type(value).__name__)


def _text_form(text: bytes) -> object:
    """MuPDF's printed object (xref_object) as a plain value."""
    return _value_form(ObjectParser(text).parse_value_at(0).value, text)


def _float32_ordinal(value: Fraction) -> int:
    """*value* rounded once (to nearest, ties to even) to an IEEE single,
    as an ordinal: neighbouring floats differ by 1, -0 and +0 are both 0."""
    if value == 0:
        return 0
    sign, size = (-1 if value < 0 else 1), abs(value)
    exponent = size.numerator.bit_length() - size.denominator.bit_length()
    if Fraction(2) ** exponent > size:
        exponent -= 1
    exponent = max(exponent, -126)
    scaled = size / Fraction(2) ** (exponent - 23)
    mantissa = round(scaled)                    # Fraction rounds half to even
    return sign * ((exponent + 127) * (1 << 23) + mantissa - (1 << 23))


def _same(ours: object, mupdf: object) -> bool:
    """pikepdf's value against MuPDF's: equal, except that MuPDF holds a
    real as a 32-bit float (one float32 step allowed) and prints a whole
    real as an integer."""
    if isinstance(ours, tuple) and isinstance(mupdf, tuple) and ours and mupdf:
        if ours[0] == "real" and mupdf[0] in ("real", "int"):
            return abs(_float32_ordinal(ours[1]) - _float32_ordinal(Fraction(mupdf[1]))) <= 1
        if ours[0] == mupdf[0] == "array":
            return len(ours[1]) == len(mupdf[1]) and all(
                _same(a, b) for a, b in zip(ours[1], mupdf[1]))
        if ours[0] == mupdf[0] == "dict":
            return [k for k, _ in ours[1]] == [k for k, _ in mupdf[1]] and all(
                _same(a, b) for (_, a), (_, b) in zip(ours[1], mupdf[1]))
    return ours == mupdf
