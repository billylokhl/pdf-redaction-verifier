"""redaction_verifier.inventory.decrypted -- one revision's cross-checked
decryption as plain data (ADR 0001; Phase 3a-9).

``redaction_verifier.crypt`` produces these: it needs both readers, so it
lives outside this package (ADR 0010's note of 2026-10-15), and it runs
in the 3b child beside the decoders (REDESIGN §4: the child touches the
PDF, the parent holds the rules). The inventory reads them (3a-9b) and
never imports crypt, pikepdf or PyMuPDF. The rule enums live here so
both sides name the same rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from ..ledger import Flag

Key = tuple[int, int]                  # (object number, generation)


class CryptRule(IntEnum):
    """Why something was flagged DECRYPTION_UNVERIFIED (its ``rule``)."""

    PASSWORD = 1       # both readers need a user password we do not have
    AUTH_SPLIT = 2     # one reader opens with the empty password, the other does not
    HANDLER = 3        # a security handler other than /Standard
    OPEN_FAILED = 4    # pikepdf cannot open the cut without recovery
    REPAIRED = 5       # MuPDF repaired the cut: nothing in it is trusted
    PERMS = 6          # /Perms does not check out (qpdf warns; ADR 0001)
    WARNING = 7        # a reader warned reading this object, or lacks it
    CRYPT_FILTER = 8   # a stream-level /Crypt filter (the readers read it differently)
    EFF = 9            # an /EFF other than /StmF: the whole revision
    STORED_PLAIN = 10  # an "encrypted" stream whose stored bytes inflate as they are
    UNLOCATED = 11     # the inventory holds no stored bytes for this stream
    NONCANONICAL = 12  # a security handler outside the closed set (ADR 0001 note)
    XREF_TYPE = 13     # a /Type /XRef stream that is no cross-reference section
    METADATA_CIPHER = 14  # "unencrypted" metadata whose stored bytes are no XMP packet
    TRAILER_SPLIT = 15  # our trailer and the readers disagree on whether it is encrypted


class MismatchRule(IntEnum):
    """Why something was flagged DECRYPTION_MISMATCH (its ``rule``)."""

    STREAM = 1         # decrypted stream bytes differ
    VALUE = 2          # a decrypted dictionary or object differs
    PLAINTEXT = 3      # an unencrypted stream is not its stored bytes


@dataclass(frozen=True)
class Decryption:
    """One revision's decryption. ``identity`` is None when the revision
    is not encrypted; ``(b"", b"")`` when it is but pikepdf could not read
    it so (a flag says why: never reused); otherwise a digest of everything that decides how
    it decrypts (its /Encrypt dictionary resolved, the file key and the
    stream, string and file methods) and its first /ID string: two
    revisions with one identity decrypt any body alike (but for the
    catalog's /Metadata under /EncryptMetadata false, which stream that
    is being the revision's). ``values`` and ``streams`` hold only what
    both readers agreed on: each object's value form (a stream's: its
    dictionary) and each stream's decrypted, still-filtered bytes. Value
    forms are tuples: ("dict", ((key, form), ...)) with sorted keys and
    no nulls, ("array", (form, ...)), ("str", bytes), ("name", bytes),
    ("ref", n, g), ("int", int), ("real", Fraction), ("bool", bool),
    ("null",). ``views`` holds what a reader shows that is not the agreed
    view and that the matcher must scan as well: MuPDF's decryption of the
    catalog's /Metadata under /EncryptMetadata false, which pikepdf (and
    the spec) leave as stored."""

    revision: int
    identity: tuple[bytes, bytes] | None
    values: Mapping[Key, object]
    streams: Mapping[Key, bytes]
    flags: tuple[Flag, ...]
    views: Mapping[Key, bytes] = field(default_factory=dict)


def reuse_token(decryption: Decryption | None, revision: int) -> object:
    """What a revision's reuse keys must agree on before one revision's
    reading stands for another's (3a-9b; coordinator decision 2): None for
    an unencrypted revision; the identity for one decrypted without a flag
    (two revisions with one identity decrypt every body alike); otherwise
    a token no other revision shares -- a flagged revision verified only
    some of its objects, so nothing it read is reused."""
    if decryption is None or decryption.identity is None:
        return None
    if decryption.flags:
        return ("revision", revision)
    return decryption.identity


def reuse_tokens(decryptions: Sequence[Decryption | None] | None,
                 revisions: int) -> tuple[object, ...]:
    """Each revision's ``reuse_token`` (all None without decryptions)."""
    if decryptions is None:
        return (None,) * revisions
    return tuple(reuse_token(decryptions[r] if r < len(decryptions) else None, r)
                 for r in range(revisions))

