"""3a-8's confirmation (ADR 0001): pikepdf's decrypted, still-filtered
stream read (``Stream.read_raw_bytes``) is the same stage as MuPDF's
``xref_stream_raw`` -- byte for byte, for every stream of every encrypted
case, on the file and on each earlier revision's prefix cut (the cut
verify.scan_earlier_revisions makes). 3a-9's cross-check compares exactly
these two reads; if they ever differ, ADR 0001 reopens (owner).

Not vacuous: the files' filler is absent from their bytes (encrypted on
disk) and present in the reads (so both are decrypted, not two equal
ciphertexts), and the Flate content of the fitz-written case is still
compressed in both (so neither has decoded the filters)."""

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

load()
ENCRYPTED = sorted(cid for cid, c in REGISTRY.items()
                   if cid.startswith(("page.encrypted-", "revision.encrypted-")))


def _revisions(data: bytes) -> list[tuple[str, bytes]]:
    """The file, then each earlier revision's prefix cut, as verify.py
    rebuilds it."""
    out = [("current", data)]
    for number, (offset, end, _changed) in enumerate(verify._earlier_revisions(data), start=1):
        out.append((f"revision {number}", data[:end] + b"\nstartxref\n%d\n%%%%EOF\n" % offset))
    return out


def _reads(data: bytes) -> list[tuple[int, bytes, bytes]]:
    """(object number, pikepdf raw read, MuPDF raw read) for every stream."""
    out = []
    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert pdf.is_encrypted
        doc = fitz.open(stream=data, filetype="pdf")
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                number = obj.objgen[0]
                out.append((number, obj.read_raw_bytes(), doc.xref_stream_raw(number)))
    return out


@pytest.fixture(scope="module")
def built() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        return {cid: build(REGISTRY[cid], Path(tmp) / f"{cid}.pdf").read_bytes()
                for cid in ENCRYPTED}


def test_the_family_covers_every_handler_and_a_change_of_handler():
    assert {"page.encrypted-rc4-40", "page.encrypted-rc4-128", "page.encrypted-aes-128",
            "page.encrypted-aes-256", "revision.encrypted-different-handlers",
            "revision.encrypted-annotated"} <= set(ENCRYPTED)


@pytest.mark.parametrize("cid", ENCRYPTED)
def test_pikepdf_raw_read_is_mupdfs_xref_stream_raw(cid, built):
    data = built[cid]
    compared = 0
    for label, part in _revisions(data):
        for number, pike, mupdf in _reads(part):
            assert pike == mupdf, f"{cid} {label} object {number}: ADR 0001 reopens"
            compared += 1
    assert compared >= 2, cid


@pytest.mark.parametrize("cid", [c for c in ENCRYPTED if REGISTRY[c].writer == "pikepdf"])
def test_the_compared_stage_is_decrypted(cid, built):
    data = built[cid]
    assert FILLER not in data                       # encrypted on disk
    for label, part in _revisions(data):
        reads = _reads(part)
        assert any(FILLER in pike and FILLER in mupdf for _, pike, mupdf in reads), (cid, label)


def test_the_compared_stage_is_not_unfiltered(built):
    # The fitz-written case's streams are Flate-compressed: both reads are
    # still compressed, and decompress to the page's content.
    data = built["revision.encrypted-annotated"]
    flate = 0
    for _number, pike, mupdf in _reads(data):
        try:
            plain = zlib.decompress(pike)
        except zlib.error:
            continue
        assert plain != pike and zlib.decompress(mupdf) == plain
        flate += 1
    assert flate >= 1


def test_the_changed_handler_is_really_different_per_revision(built):
    data = built["revision.encrypted-different-handlers"]
    handlers = []
    for _label, part in _revisions(data):
        with pikepdf.open(io.BytesIO(part)) as pdf:
            handlers.append(int(pdf.encryption.R))
    assert handlers == [6, 3]                       # current AES-256, earlier RC4-128
