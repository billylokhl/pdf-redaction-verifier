"""redaction_verifier.crypt -- each revision's decryption, cross-checked
(docs/adr/0001, as amended 2026-10-15; Phase 3a-9a).

pikepdf (libqpdf) decrypts each encrypted revision's prefix cut with the
empty user password; MuPDF decrypts the same cut independently. What the
two agree on is the revision's decrypted view (``Decryption.values`` and
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
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pikepdf
import pymupdf as _pymupdf

from .inventory.objects import (
    ObjectParser, PdfArray, PdfBool, PdfDict, PdfInt, PdfName, PdfNull, PdfReal, PdfRef,
    PdfString, PdfValue)
from .inventory.decrypted import CryptRule, Decryption, Key, MismatchRule
from .budget import Budget, Limits
from .inventory.build import build_inventory
from .inventory.types import Inventory
from .inventory.xref import Chain, read_chain
from .ledger import Flag, FlagReason, Span, UnitRef

# PyMuPDF is untyped: every use goes through this one Any.
pymupdf: Any = _pymupdf


# Every read of one decrypt() call: the byte cap, and what both readers
# verified, by body and identity (a body two revisions share under one
# identity decrypts alike: verified once).
_Verified = dict[tuple[UnitRef, int, tuple[bytes, bytes]], tuple[object, bytes | None]]


@dataclass
class _Shared:
    left: int                  # bytes still to read
    entries: int               # xref entries still to visit, over all revisions
    verified: _Verified        # only from revisions found trusted
    xref_streams: frozenset[int]   # the chain's cross-reference streams, by offset
    # The plain-metadata check, by stored offset and identity: once per body.
    packets: dict[tuple[int, tuple[bytes, bytes] | None], bool] = field(default_factory=dict)


def cut(chain: Chain, data: bytes, revision: int) -> bytes:
    """*data* cut to *revision* (0 = the whole file), as readers open it:
    the bytes through that revision's section, then its own startxref."""
    return data if revision == 0 else (
        data[:chain.revision_end(revision)]
        + b"\nstartxref\n%d\n%%%%EOF\n" % chain.revision_start(revision))


def decrypt(data: bytes, inventory: Inventory, chain: Chain, *,
            max_bytes: int = 1 << 30, max_entries: int = 1_000_000) -> tuple[Decryption, ...]:
    """Every revision's decryption, current first. *max_bytes* caps the
    bytes read -- every object's printed value and every stream, from both
    readers and the file -- and *max_entries* the xref entries visited,
    reused or not, over all revisions: each revision walks its whole table,
    so the work is revisions x entries (BUDGET_EXHAUSTED past either).
    Flags are one per object at most, bounded the same way."""
    xref_streams = frozenset(part.offset for section in chain.sections
                             for part in (section, section.xref_stm)
                             if part is not None and part.is_stream)
    shared = _Shared(max_bytes, max_entries, {}, xref_streams)
    shown = pymupdf.TOOLS.mupdf_display_errors()
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        # Explicit conversion: integers, booleans and the like stay pikepdf
        # objects, so a value's form never depends on Python's coercions.
        with pikepdf.explicit_conversion():
            return tuple(_Revision(cut(chain, data, r), r, inventory, shared,
                                   _ours_encrypted(chain, r)).run()
                         for r in range(inventory.revisions))
    finally:
        pymupdf.TOOLS.mupdf_display_errors(shown)


def inventory(data: bytes, limits: Limits | None = None) -> Inventory:
    """The inventory of *data*, encrypted or not (3a-9b): the pipeline
    3b's child runs. An unencrypted file (no revision's trailer, as we read
    it, has a non-null /Encrypt) is built once. An encrypted one is built
    twice: a structural pass (units, spans, stream data, object streams --
    no reference graph) that crypt.py locates bytes by, then decrypt(),
    then the full build with each revision's Decryption (its flags, and
    the reuse tokens). Each pass has its own budget: the structural pass
    costs about one parse more (ADR 0006's note)."""
    limits = limits if limits is not None else Limits()
    chain = read_chain(data, limits, budget=Budget(limits, file_size=len(data)))
    if not any(_ours_encrypted(chain, r) for r in range(len(chain.revisions))):
        return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)))
    first = build_inventory(data, limits, budget=Budget(limits, file_size=len(data)),
                            structure_only=True)
    decryptions = decrypt(data, first, chain)
    return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)),
                           decryption=decryptions)


def _flag(reason: FlagReason, rule: int, revision: int, key: Key | None,
          span: Span | None = None) -> Flag:
    number, gen = key if key is not None else (-1, -1)
    return Flag(reason, span, (("object", number), ("gen", gen), ("revision", revision),
                               ("rule", int(rule))))


class _Revision:
    def __init__(self, blob: bytes, revision: int, inventory: Inventory, shared: _Shared,
                 ours_encrypted: bool):
        self.blob, self.revision, self.inv, self.shared = blob, revision, inventory, shared
        self.ours_encrypted = ours_encrypted
        self.identity: tuple[bytes, bytes] | None = None
        self.staged: _Verified = {}             # joins shared.verified only if trusted
        self.flags: list[Flag] = []
        self.values: dict[Key, object] = {}
        self.streams: dict[Key, bytes] = {}
        self.views: dict[Key, bytes] = {}
        self.spans: dict[UnitRef, Span] | None = None

    def unverified(self, rule: CryptRule, key: Key | None = None) -> None:
        self.flags.append(_flag(FlagReason.DECRYPTION_UNVERIFIED, rule, self.revision, key,
                                self._span(key)))

    def mismatch(self, rule: MismatchRule, key: Key) -> None:
        self.flags.append(_flag(FlagReason.DECRYPTION_MISMATCH, rule, self.revision, key,
                                self._span(key)))

    def done(self, identity: tuple[bytes, bytes] | None, trusted: bool = True) -> Decryption:
        if not trusted:
            self.values.clear()
            self.streams.clear()
        else:
            self.shared.verified.update(self.staged)
        return Decryption(self.revision, identity, dict(self.values), dict(self.streams),
                          tuple(self.flags), dict(self.views))

    def _span(self, key: Key | None) -> Span | None:
        unit = None if key is None else self.inv.body(key[0], self.revision)
        if unit is None or unit.within is not None:
            return None
        if self.spans is None:                  # built once: flags are many on a hostile file
            self.spans = {u.ref: u.spans[0] for u in self.inv.units if u.spans}
        return self.spans.get(unit)

    def charge(self, *parts: bytes, entries: int = 0, extra: int = 0) -> bool:
        self.shared.left -= sum(len(p) for p in parts) + extra
        self.shared.entries -= entries
        if self.shared.left < 0 or self.shared.entries < 0:
            if not any(f.reason is FlagReason.BUDGET_EXHAUSTED for f in self.flags):
                self.flags.append(Flag(FlagReason.BUDGET_EXHAUSTED, None,
                                       (("revision", self.revision),)))
            return False
        return True

    # ── Opening ─────────────────────────────────────────────────────────
    def run(self) -> Decryption:
        if not self.charge(self.blob):          # each revision's cut is read whole, twice
            return self.done(None, trusted=False)
        try:
            doc = pymupdf.open(stream=self.blob, filetype="pdf")
        except Exception:                      # FileDataError and kin: unreadable
            doc = None
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

    def _opened(self, doc: Any, pdf: pikepdf.Pdf | None, locked: bool) -> Decryption:
        mu_encrypted = doc is not None and bool(doc.needs_pass or doc.metadata.get("encryption"))
        opening_warned = _drain_mupdf()         # opening, and reading the metadata
        pk_encrypted = locked or (pdf is not None and pdf.is_encrypted)
        if not (mu_encrypted or pk_encrypted or self.ours_encrypted):
            return self.done(None)              # nobody reads it encrypted: not this module's
        readers = pk_encrypted or mu_encrypted
        identity = _identity(pdf) if pdf is not None and pdf.is_encrypted else (b"", b"")
        if self.ours_encrypted != readers:
            # Our trailer and the readers disagree (an /Encrypt naming a null
            # or missing object; one we do not see): the inventory's view and
            # theirs would not be one file's.
            self.unverified(CryptRule.TRAILER_SPLIT)
            return self.done(identity, trusted=False)
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
        if not _canonical(_resolved_form(encrypt)):
            self.unverified(CryptRule.NONCANONICAL)
            return self.done(identity, trusted=False)
        if _eff_differs(encrypt):               # readers ignore /EFF; /Type is optional
            self.unverified(CryptRule.EFF)
            return self.done(identity, trusted=False)
        warnings = pdf.get_warnings()
        if any("/Perms" in w for w in warnings):
            self.unverified(CryptRule.PERMS)
            return self.done(identity, trusted=False)
        if warnings or opening_warned:
            self.unverified(CryptRule.WARNING)
            return self.done(identity, trusted=False)
        self.identity = identity
        self._objects(doc, pdf, encrypt)
        _drain_mupdf()
        if doc.is_repaired:                     # repair is lazy: only known after the reads
            self.unverified(CryptRule.REPAIRED)
            return self.done(identity, trusted=False)
        return self.done(identity)

    # ── Objects ─────────────────────────────────────────────────────────
    def _objects(self, doc: Any, pdf: pikepdf.Pdf, encrypt: Any) -> None:
        plain_metadata = _plain_metadata(pdf, encrypt)
        identity_streams = encrypt.get("/StmF") == pikepdf.Name.Identity
        for (number, gen), entry in sorted(pdf.get_xref_table().items()):
            key = (number, gen)
            if not self.charge(entries=1):
                return
            if entry.type == 0:
                continue
            if number >= doc.xref_length():     # pikepdf lists it, MuPDF has no such number
                self.unverified(CryptRule.WARNING, key)
                continue
            if entry.type == 2 and not self._container_decoded(entry):
                continue                        # the inventory flags what it could not decode
            if not self._object(doc, pdf, key, plain_metadata, identity_streams):
                return

    def _container_decoded(self, entry: Any) -> bool:
        """Whether the inventory decoded the object stream holding a
        compressed entry within its budget: the readers inflate it whole,
        uncapped, to read one member. An encrypted object stream the
        inventory cannot inflate yet (it reads ciphertext until 3a-9b)
        still lands in ``object_streams``, without members: its members
        are read here (tests/test_crypt.py pins that)."""
        home = self.inv.entry(entry.obj_stream_number, self.revision)
        return home is not None and home[0] == 1 and home[1] in self.inv.object_streams

    def _object(self, doc: Any, pdf: pikepdf.Pdf, key: Key, plain_metadata: Key | None,
                identity_streams: bool) -> bool:
        number, gen = key
        unit = self.inv.body(number, self.revision)
        reuse = None if unit is None or self.identity is None or key == plain_metadata \
            else (unit, gen, self.identity)
        if reuse is not None and reuse in self.shared.verified:
            form, data = self.shared.verified[reuse]   # an earlier revision verified this body
            self.values[key] = form
            if data is not None:
                self.streams[key] = data
            return True
        try:
            obj = pdf.get_object(number, gen)
            text = doc.xref_object(number, compressed=False)
        except Exception:
            obj, text = None, ""
        if not self.charge(text.encode("latin-1", "replace")):
            return False
        if bool(pdf.get_warnings()) | _drain_mupdf() or obj is None:
            self.unverified(CryptRule.WARNING, key)
            return True
        ours = _pike_form(obj, top=True)
        theirs = _text_form(text.encode("latin-1"))
        if not _same(ours, theirs):
            self.mismatch(MismatchRule.VALUE, key)
            return True
        if not isinstance(obj, pikepdf.Stream):
            self.values[key] = ours
        elif not self._stream(doc, pdf, obj, key, ours, plain_metadata, identity_streams):
            return False
        if reuse is not None and key in self.values:
            self.staged[reuse] = (self.values[key], self.streams.get(key))
        return True

    def _stream(self, doc: Any, pdf: pikepdf.Pdf, obj: pikepdf.Stream, key: Key,
                form: object, plain_metadata: Key | None, identity_streams: bool) -> bool:
        number = key[0]
        filters = obj.get("/Filter")
        names = list(filters) if isinstance(filters, pikepdf.Array) else [filters]
        if pikepdf.Name.Crypt in names:
            self.unverified(CryptRule.CRYPT_FILTER, key)
            return True
        unit = self.inv.body(number, self.revision)
        is_xref = obj.get("/Type") == pikepdf.Name.XRef
        if is_xref and (unit is None or unit.start not in self.shared.xref_streams):
            # qpdf leaves any /Type /XRef stream undecrypted, MuPDF decrypts
            # it: only the chain's own sections are exempt (ISO 32000-1 7.5.8).
            self.unverified(CryptRule.XREF_TYPE, key)
            return True
        stored = self._stored(key)
        if stored is None:
            self.unverified(CryptRule.UNLOCATED, key)
            return True
        # A stream the spec leaves unencrypted (a cross-reference section;
        # the catalog's /Metadata under /EncryptMetadata false) has no
        # second decryption: MuPDF decrypts it anyway, so pikepdf's read
        # must be the stored bytes. For the metadata, MuPDF's decryption is
        # still read: if it is text, the stored bytes were ciphertext that a
        # reader shows decrypted. Under /StmF /Identity both readers leave
        # every stream as stored: all three must agree.
        plain = is_xref or key == plain_metadata
        ours: bytes | None
        theirs: bytes | None
        try:
            ours = bytes(obj.read_raw_bytes())
            theirs = None if is_xref else doc.xref_stream_raw(number)
        except Exception:
            ours = theirs = None
            is_xref = False
        pk_warned = bool(pdf.get_warnings())
        warned = pk_warned | _drain_mupdf()
        if not self.charge(stored, ours or b"", theirs or b""):
            return False
        if key == plain_metadata:
            # MuPDF decrypts it anyway (and may warn doing so): its warnings
            # are expected, pikepdf's are not. What it shows is scanned too.
            warned = pk_warned
            if theirs is not None and theirs != stored:
                self.views[key] = theirs
            unit = self.inv.body(key[0], self.revision)
            # Sound only because the verdict rests on the stored bytes and a
            # direct /Filter and /DecodeParms: an indirect one never passes
            # (_xmp_packet), so a later revision cannot change it unseen.
            memo = None if unit is None else (unit.start, self.identity)
            packet = self.shared.packets.get(memo) if memo is not None else None
            if packet is None:
                packet, read = _xmp_packet(stored, obj)
                if not self.charge(b"", entries=0, extra=read):
                    return False
                if memo is not None:
                    self.shared.packets[memo] = packet
            if not packet:
                self.unverified(CryptRule.METADATA_CIPHER, key)
                return True
        if warned or ours is None or (theirs is None and not is_xref):
            self.unverified(CryptRule.WARNING, key)
            return True
        if plain:
            if ours != stored:
                self.mismatch(MismatchRule.PLAINTEXT, key)
                return True
        elif identity_streams:
            if not ours == theirs == stored:
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
    """Everything that decides how the revision decrypts: its /Encrypt
    dictionary with every indirect part resolved (a later revision may
    rewrite only an indirect /CF), the file key pikepdf derived and the
    stream, string and file methods -- and its first /ID string."""
    encrypt = pdf.trailer.get("/Encrypt")
    ident = pdf.trailer.get("/ID")
    first = bytes(ident[0]) if isinstance(ident, pikepdf.Array) and len(ident) else b""
    info = pdf.encryption
    parts = (repr(_resolved_form(encrypt)), bytes(info.encryption_key).hex(),
             str(info.stream_method), str(info.string_method), str(info.file_method))
    return hashlib.sha256(repr(parts).encode()).digest(), first


def _resolved_form(obj: Any, depth: int = 0) -> object:
    """*obj*'s value form with indirect objects resolved, to a small depth
    (an /Encrypt dictionary is shallow; a deeper one is cut off, which only
    makes two revisions' identities differ where they need not)."""
    if obj is None or depth > 8:
        return NULL
    kind = obj._type_code
    if kind is _PIKE.array:
        return ("array", tuple(_resolved_form(item, depth + 1) for item in obj))
    if kind in (_PIKE.dictionary, _PIKE.stream):
        return _dict_form((key.encode("utf-8", "surrogateescape")[1:],
                           _resolved_form(value, depth + 1)) for key, value in obj.items())
    return _pike_form(obj, top=True)


def _canonical(form: object) -> bool:
    """Whether a resolved /Encrypt form is in ADR 0001's closed set (its
    canonical-form note): the standard handler's V1/R2, V2/R3 with its
    /Length written, V4/R4 (V2 or AESV2 filters) and V5/R6 (AESV3), with
    nothing left to a reader's default. Anything else is not trusted,
    however the readers read it."""
    if not (isinstance(form, tuple) and form[0] == "dict"):
        return False
    d = dict(form[1])
    v, r, length = d.get(b"V"), d.get(b"R"), d.get(b"Length")
    if d.get(b"Filter") != ("name", b"Standard") or b"SubFilter" in d:
        return False
    if d.get(b"EncryptMetadata", ("bool", True))[0] != "bool":
        return False
    if v == ("int", 1) and r == ("int", 2):
        return length in (None, ("int", 40)) and not {b"CF", b"StmF", b"StrF", b"EFF"} & set(d)
    if v == ("int", 2) and r == ("int", 3):
        return (isinstance(length, tuple) and length[0] == "int" and 40 <= length[1] <= 128
                and length[1] % 8 == 0 and not {b"CF", b"StmF", b"StrF", b"EFF"} & set(d))
    if (v, r) == (("int", 4), ("int", 4)):
        methods, bits = {b"V2", b"AESV2"}, (None, ("int", 128))
    elif (v, r) == (("int", 5), ("int", 6)):
        methods, bits = {b"AESV3"}, (None, ("int", 256))
    else:
        return False
    if length not in bits:
        return False
    filters = d.get(b"CF", ("dict", ()))
    if filters[0] != "dict":
        return False
    names = {b"Identity"}
    for name, cf in filters[1]:
        f = dict(cf[1]) if isinstance(cf, tuple) and cf[0] == "dict" else None
        if f is None or f.get(b"CFM", ("name", None))[1] not in methods:
            return False
        if f.get(b"AuthEvent", ("name", b"DocOpen")) != ("name", b"DocOpen"):
            return False
        if f.get(b"Length") not in (("int", 16 if v == ("int", 4) else 32),
                                     ("int", 128 if v == ("int", 4) else 256)):
            return False
        if name == b"Identity":                 # reserved (ISO 32000-1 7.6.5)
            return False
        names.add(name)
    chosen = [d.get(key, ("name", b"Identity")) for key in (b"StmF", b"StrF")]
    eff = d.get(b"EFF")
    return (all(c[0] == "name" and c[1] in names for c in chosen)
            and (eff is None or eff == d.get(b"StmF", ("name", b"Identity"))
                 or (eff[0] == "name" and eff[1] in names)))


def _ours_encrypted(chain: Chain, revision: int) -> bool:
    """Whether the revision's own trailer (as the inventory reads it) has a
    non-null /Encrypt: decided by our reading, not a reader's."""
    trailer = chain.sections[chain.revisions[revision][0]].trailer
    value = trailer.get(b"Encrypt") if trailer is not None else None
    return value is not None and not isinstance(value, PdfNull)


_MARKERS = ("<?xpacket", "<x:xmpmeta")


_PACKET_CAP = 1 << 20      # real XMP packets run a few KB to ~100 KB


def _xmp_packet(stored: bytes, obj: Any) -> tuple[bool, int]:
    """Whether the catalog's unencrypted /Metadata is, as stored, wholly an
    XMP packet -- unfiltered or Flate alone (inflated to 1 MiB), then text
    in one Unicode encoding throughout (trailing NULs and whitespace
    allowed), holding an XMP marker -- and how many bytes that read. Not
    proof: any printable string can be ciphertext, and an object number or
    /ID can be ground until it is (#178, round 3); MuPDF's decryption is
    carried in ``views`` for the matcher for that reason. Latin-1 packets
    and other encodings are not trusted (fail closed)."""
    filters = obj.get("/Filter")
    names = list(filters) if isinstance(filters, pikepdf.Array) else \
        ([] if filters is None else [filters])
    parms = obj.get("/DecodeParms")
    parms_empty = parms is None or (isinstance(parms, pikepdf.Dictionary) and not len(parms)) \
        or (isinstance(parms, pikepdf.Array) and all(p is None for p in parms))
    data = stored
    if names == [pikepdf.Name.FlateDecode] and parms_empty:
        z = zlib.decompressobj()
        try:
            data = z.decompress(stored, _PACKET_CAP)
        except zlib.error:
            return False, 0
        if not z.eof or z.unconsumed_tail or z.unused_data.strip(b"\r\n \t\0"):
            return False, len(data)
    elif names:
        return False, 0
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32"):
        try:
            text = data.decode(encoding).rstrip("\0 \t\r\n")
        except UnicodeDecodeError:
            continue
        if any(m in text for m in _MARKERS) and all(
                c.isprintable() or c.isspace() or c == "\ufeff" for c in text):
            return True, len(data)
    return False, len(data)


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
