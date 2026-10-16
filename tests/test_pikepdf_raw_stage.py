"""3a-8's confirmation (ADR 0001): for every stream the security handler
encrypts, pikepdf's decrypted, still-filtered read
(``Stream.read_raw_bytes``) is the same stage as MuPDF's
``xref_stream_raw`` -- byte for byte, for every encrypted case, on the
file and on each earlier revision's prefix cut (the cut
verify.scan_earlier_revisions makes). 3a-9's cross-check compares exactly
these two reads.

Streams the handler does not encrypt (ISO 32000-1 §7.6.1 and §7.6.3):
cross-reference streams, and /Metadata under /EncryptMetadata false.
pikepdf returns their stored plaintext, as the spec says; MuPDF decrypts
them anyway (garbage under RC4, nothing under AES). That divergence is
pinned below, so a change in either library is seen. ADR 0001's
amendment (owner, 2026-10-15): 3a-9 cross-checks only streams the handler
encrypts and checks these against the file's own stored bytes.

Not vacuous: the files' filler is absent from their bytes (encrypted on
disk) and present in both reads (so both are decrypted, not two equal
ciphertexts), and each file's Flate-compressed content stream is still
compressed in both (so neither has applied the filters)."""

from __future__ import annotations

import io
import sys
import tempfile
import zlib
from pathlib import Path

import pikepdf
import pymupdf as fitz
import pytest

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "eval"))
sys.path.insert(0, str(REPO_ROOT))

import verify  # noqa: E402
from caselib import REGISTRY, load  # noqa: E402
from caselib.families.encrypted import FILLER  # noqa: E402
from caselib.run import build  # noqa: E402
from redaction_verifier.crypt import _plain_metadata  # noqa: E402

load()
ENCRYPTED = sorted(cid for cid, c in REGISTRY.items()
                   if cid.split(".")[1].startswith("encrypted-"))


def _revisions(data: bytes) -> list[tuple[str, bytes]]:
    """The file, then each earlier revision's prefix cut, as verify.py
    rebuilds it."""
    out = [("current", data)]
    for number, (offset, end, _changed) in enumerate(verify._earlier_revisions(data), start=1):
        out.append((f"revision {number}", data[:end] + b"\nstartxref\n%d\n%%%%EOF\n" % offset))
    return out


def _handler_encrypts(pdf: pikepdf.Pdf, stream: pikepdf.Stream) -> bool:
    """Whether the standard security handler encrypts *stream*."""
    if stream.get("/Type") == "/XRef":
        return False
    # Only the catalog's /Metadata goes unencrypted under /EncryptMetadata
    # false (ISO 32000-1 7.6.1; qpdf) -- not any /Type /Metadata stream:
    # crypt.py's own rule, shared.
    encrypt = pdf.trailer.get("/Encrypt")
    if encrypt is not None and tuple(stream.objgen) == _plain_metadata(pdf, encrypt):
        return False
    # A stream's own /Crypt filter decides only when it names /Identity
    # (or no name, which means /Identity: ISO 32000-1 §7.4.10); under a
    # named crypt filter such as /StdCF the stream is encrypted.
    filters = stream.get("/Filter")
    names = list(filters) if isinstance(filters, pikepdf.Array) else [filters]
    if pikepdf.Name.Crypt in names:
        parms = stream.get("/DecodeParms")
        position = names.index(pikepdf.Name.Crypt)
        if isinstance(parms, pikepdf.Array):
            parms = parms[position] if position < len(parms) else None
        name = parms.get("/Name") if isinstance(parms, pikepdf.Dictionary) else None
        return name is not None and name != "/Identity"
    return True


def _reads(data: bytes) -> list[tuple[int, bool, str, bytes, bytes]]:
    """(object number, encrypted by the handler, /Type, pikepdf raw read,
    MuPDF raw read) for every stream; asserts both opened it cleanly."""
    out = []
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert pdf.is_encrypted
        doc = fitz.open(stream=data, filetype="pdf")
        assert not doc.is_repaired and not doc.needs_pass
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                number = obj.objgen[0]
                out.append((number, _handler_encrypts(pdf, obj), str(obj.get("/Type", "")),
                            obj.read_raw_bytes(), doc.xref_stream_raw(number)))
        doc.close()
    return out


@pytest.fixture(scope="module")
def built() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        return {cid: build(REGISTRY[cid], Path(tmp) / f"{cid}.pdf").read_bytes()
                for cid in ENCRYPTED}


def test_the_family_covers_every_handler_and_each_layout():
    assert {"page.encrypted-rc4-40", "page.encrypted-rc4-128", "page.encrypted-aes-128",
            "page.encrypted-aes-256", "page.encrypted-aes-256-xref-streams",
            "document.encrypted-plain-metadata", "revision.encrypted-different-handlers",
            "revision.encrypted-annotated"} <= set(ENCRYPTED)


@pytest.mark.parametrize("cid", ENCRYPTED)
def test_pikepdf_raw_read_is_mupdfs_xref_stream_raw(cid, built):
    compared = 0
    for label, part in _revisions(built[cid]):
        for number, encrypted, _type, pike, mupdf in _reads(part):
            if encrypted:
                assert pike == mupdf, f"{cid} {label} object {number}: ADR 0001 reopens"
                compared += 1
    assert compared >= 2, cid


@pytest.mark.parametrize("cid", [c for c in ENCRYPTED if REGISTRY[c].writer == "pikepdf"])
def test_the_compared_stage_is_decrypted(cid, built):
    data = built[cid]
    assert FILLER not in data                       # encrypted on disk
    for label, part in _revisions(data):
        assert any(enc and FILLER in pike and FILLER in mupdf
                   for _, enc, _, pike, mupdf in _reads(part)), (cid, label)


@pytest.mark.parametrize("cid", ENCRYPTED)
def test_the_compared_stage_is_not_unfiltered(cid, built):
    # Every file's content stream is Flate-compressed: both reads are
    # still compressed, and inflate to the same text.
    for label, part in _revisions(built[cid]):
        flate = 0
        for _number, enc, _type, pike, mupdf in _reads(part):
            if not enc:
                continue
            try:
                plain = zlib.decompress(pike)
            except zlib.error:
                continue
            assert plain != pike and zlib.decompress(mupdf) == plain, (cid, label)
            flate += 1
        assert flate >= 1, (cid, label)


def test_streams_the_handler_does_not_encrypt_diverge_as_pinned(built):
    """pikepdf returns the stored plaintext of a cross-reference stream and
    of /Metadata under /EncryptMetadata false; MuPDF's xref_stream_raw
    decrypts them anyway. Pinned: if either library changes, this fails
    and ADR 0001's amendment is revisited."""
    seen = set()
    for cid in ("page.encrypted-aes-256-xref-streams", "document.encrypted-plain-metadata"):
        data = built[cid]
        for _number, enc, kind, pike, mupdf in _reads(data):
            if enc:
                continue
            seen.add(kind)
            assert pike in data, (cid, kind)        # stored as is: no decryption applies
            assert pike != mupdf, (cid, kind)       # MuPDF's read is not that plaintext
            if kind == "/Metadata":
                assert b"Notice for SSN" in pike
    assert seen == {"/XRef", "/Metadata"}


def test_each_revision_has_its_own_handler_and_id(built):
    data = built["revision.encrypted-different-handlers"]
    handlers, ids = [], []
    for _label, part in _revisions(data):
        with pikepdf.open(io.BytesIO(part)) as pdf:
            handlers.append(int(pdf.encryption.R))
            ids.append(bytes(pdf.trailer.ID[0]))
    assert handlers == [6, 3]                       # current AES-256, earlier RC4-128
    assert ids[0] != ids[1]                         # R3's key depends on its own /ID
