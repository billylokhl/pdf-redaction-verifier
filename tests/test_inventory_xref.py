"""The canonical cross-reference chain (Phase 3a-4b, ADR 0010 item 4):
each canonical shape reads without a flag and agrees with qpdf and
MuPDF revision by revision; anything else is flagged."""

from __future__ import annotations

import os
import re
import subprocess
import zlib
from pathlib import Path

import pymupdf
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from redaction_verifier.inventory.xref import COMPRESSED, FREE, IN_USE, Entry, read_chain

from .conftest import requires_qpdf

BODIES = {
    1: b"<< /Type /Catalog /Pages 2 0 R >>",
    2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
}


def _objects(bodies: dict[int, bytes], out: bytearray) -> dict[int, int]:
    offsets = {}
    for num, body in bodies.items():
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    return offsets


def _table(offsets: dict[int, int], size: int, extra: bytes = b"") -> bytes:
    lines = [b"xref\n0 1\n0000000000 65535 f \n"]
    for num in sorted(offsets):
        lines.append(b"%d 1\n%010d 00000 n \n" % (num, offsets[num]))
    return b"".join(lines) + b"trailer\n<< /Size %d /Root 1 0 R%s >>\n" % (size, extra)


def classic() -> bytes:
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = _objects(BODIES, out)
    xref = len(out)
    out += _table(offsets, 4) + b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _stream_xref(entries: dict[int, tuple[int, int, int]], size: int, offset: int,
                 extra: bytes = b"", predictor: bool = False) -> bytes:
    rows = [bytes([k]) + a.to_bytes(4, "big") + b.to_bytes(2, "big")
            for _, (k, a, b) in sorted(entries.items())]
    parms = b""
    if predictor:
        rows = [b"\x00" + r for r in rows]
        parms = b" /DecodeParms << /Predictor 12 /Columns 7 >>"
    packed = zlib.compress(b"".join(rows))
    index = b" ".join(b"%d 1" % n for n in sorted(entries))
    return (b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Index [%s] /Root 1 0 R"
            b" /Filter /FlateDecode%s /Length %d%s >>\nstream\n" % (
                size - 1, size, index, parms, len(packed), extra)
            + packed + b"\nendstream\nendobj\n")


def xref_stream(predictor: bool = False) -> bytes:
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = _objects(BODIES, out)
    xref = len(out)
    entries = {0: (0, 0, 0xFFFF)} | {n: (1, o, 0) for n, o in offsets.items()}
    entries[4] = (1, xref, 0)
    out += _stream_xref(entries, 5, xref, predictor=predictor)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def incremental() -> bytes:
    first = classic()
    xref1 = int(re.search(rb"startxref\n(\d+)", first).group(1))  # type: ignore[union-attr]
    out = bytearray(first)
    offsets = _objects({3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] >>"}, out)
    xref2 = len(out)
    out += (b"xref\n3 1\n%010d 00000 n \ntrailer\n<< /Size 4 /Root 1 0 R /Prev %d >>\n"
            % (offsets[3], xref1) + b"startxref\n%d\n%%%%EOF\n" % xref2)
    return bytes(out)


# ── Canonical shapes ──────────────────────────────────────────────────────
@pytest.mark.parametrize("build", [classic, xref_stream, lambda: xref_stream(True),
                                   incremental])
def test_canonical_chains_read_without_a_flag(build) -> None:  # type: ignore[no-untyped-def]
    chain = read_chain(build())
    assert chain.flags == ()
    objects = chain.object_map()
    assert {n for n, e in objects.items() if e.kind == IN_USE} >= {1, 2, 3}


def test_an_incremental_update_is_two_revisions_newest_winning() -> None:
    data = incremental()
    chain = read_chain(data)
    assert len(chain.revisions) == 2
    assert chain.object_map(0)[3] != chain.object_map(1)[3]
    assert data[chain.object_map(1)[3].a:].startswith(b"3 0 obj")
    assert chain.revision_end(1) < chain.revision_end(0) <= len(data)


# ── Everything else is flagged ────────────────────────────────────────────
def _reasons(data: bytes) -> list[str]:
    return [f.reason.name for f in read_chain(data).flags]


def _mutate(data: bytes, old: bytes, new: bytes) -> bytes:
    assert old in data
    return data.replace(old, new, 1)


@pytest.mark.parametrize(("data", "reason"), [
    (classic() + b"SSN 123-45-6789\n", "XREF_TAIL"),                 # bytes after %%EOF
    (classic()[:-6], "XREF_TAIL"),                                   # no %%EOF
    (_mutate(classic(), b"startxref\n", b"startxref\n9"), "XREF_NOT_FOUND"),
    (_mutate(classic(), b"00000 n \n", b"00000 n\n"), "XREF_TABLE_MALFORMED"),  # 19 bytes
    (_mutate(classic(), b"trailer", b"trailor"), "XREF_TABLE_MALFORMED"),
    (_mutate(classic(), b"/Root 1 0 R >>", b"/Root 1 0 R /Prev 1 >>"), "XREF_NOT_FOUND"),
    (_mutate(xref_stream(), b"/W [1 4 2]", b"/W [1 4 9]"), "XREF_STREAM_MALFORMED"),
    (_mutate(xref_stream(), b"/Type /XRef", b"/Type /XRaf"), "XREF_STREAM_MALFORMED"),
    (_mutate(xref_stream(), b"/Filter /FlateDecode", b"/Filter /LZWDecode   "),
     "UNSUPPORTED_FILTER"),
])
def test_non_canonical_chains_are_flagged(data: bytes, reason: str) -> None:
    assert reason in _reasons(data)


def test_an_entry_off_its_object_is_flagged() -> None:
    data = classic()
    one = re.search(rb"1 1\n(\d{10})", data)
    assert one is not None
    moved = int(one.group(1)) + 1
    bad = data.replace(one.group(0), b"1 1\n%010d" % moved, 1)
    assert "XREF_OFFSET_MISMATCH" in _reasons(bad)


def test_a_prev_cycle_is_flagged() -> None:
    data = incremental()
    first, last = (int(n) for n in re.findall(rb"startxref\n(\d+)", data))
    assert len(str(first)) == len(str(last))  # same-length edit: offsets stay put
    # The newest trailer's /Prev points back at the newest section itself.
    cyc = data.replace(b"/Prev %d" % first, b"/Prev %d" % last, 1)
    assert "PREV_CYCLE" in _reasons(cyc)


def test_a_compressed_entry_needs_an_object_stream_in_use() -> None:
    out = bytearray(b"%PDF-1.7\n")
    offsets = _objects(BODIES, out)
    xref = len(out)
    entries = {0: (0, 0, 0xFFFF)} | {n: (1, o, 0) for n, o in offsets.items()}
    entries[4] = (1, xref, 0)
    entries[5] = (COMPRESSED, 9, 0)  # object stream 9 does not exist
    out += _stream_xref(entries, 6, xref) + b"startxref\n%d\n%%%%EOF\n" % xref
    assert "XREF_OFFSET_MISMATCH" in _reasons(bytes(out))


def test_entries_keep_their_kinds() -> None:
    chain = read_chain(xref_stream())
    assert chain.object_map()[0] == Entry(FREE, 0, 0xFFFF)


@given(st.binary(max_size=300))
def test_never_raises(tail: bytes) -> None:
    for data in (tail, classic()[:len(tail)] + tail, xref_stream() + tail):
        read_chain(data)


# ── Differential: every unflagged revision matches qpdf and MuPDF ─────────
_LINE = re.compile(rb"^(\d+)/(\d+): (?:uncompressed; offset = (\d+)|"
                   rb"compressed; stream = (\d+), index = (\d+))", re.M)


def _qpdf_map(path: Path) -> tuple[dict[int, tuple[str, int, int]], int]:
    result = subprocess.run(["qpdf", "--show-xref", str(path)], capture_output=True,
                            timeout=60)
    found: dict[int, tuple[str, int, int]] = {}
    for m in _LINE.finditer(result.stdout):
        num, gen = int(m.group(1)), int(m.group(2))
        found[num] = (("u", int(m.group(3)), gen) if m.group(3)
                      else ("c", int(m.group(4)), int(m.group(5))))
    return found, result.returncode


def _ours(entries: dict[int, Entry]) -> dict[int, tuple[str, int, int]]:
    return {n: ("u" if e.kind == IN_USE else "c", e.a, e.b)
            for n, e in entries.items() if e.kind != FREE and n != 0}


def _agree(data: bytes, tmp: Path) -> None:
    chain = read_chain(data)
    if chain.flags:
        return
    for revision in range(len(chain.revisions)):
        blob = data if revision == 0 else (
            data[:chain.revision_end(revision)]
            + b"\nstartxref\n%d\n%%%%EOF\n" % chain.revision_start(revision))
        path = tmp / f"r{revision}.pdf"
        path.write_bytes(blob)
        assert _qpdf_map(path) == (_ours(chain.object_map(revision)), 0), revision
        assert not pymupdf.open(stream=blob, filetype="pdf").is_repaired, revision


@requires_qpdf
@pytest.mark.parametrize("build", [classic, xref_stream, lambda: xref_stream(True),
                                   incremental])
def test_canonical_chains_agree_with_the_readers(build, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _agree(build(), tmp_path)


@requires_qpdf
@settings(max_examples=int(os.environ.get("DIFF_FUZZ_EXAMPLES", "40")),
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(st.integers(1, 4), st.booleans(), st.booleans(), st.integers(0, 3))
def test_pymupdf_written_files_agree_with_the_readers(
        tmp_path: Path, pages: int, objstms: bool, garbage: bool, updates: int) -> None:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((50, 50), f"page {i}")
    path = tmp_path / "base.pdf"
    doc.save(path, use_objstms=int(objstms), garbage=3 if garbage else 0)
    for n in range(updates):
        doc = pymupdf.open(path)
        doc[0].insert_text((50, 80 + 20 * n), f"update {n}")
        doc.save(path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    chain = read_chain(path.read_bytes())
    assert chain.flags == (), [f.reason.name for f in chain.flags]
    assert len(chain.revisions) >= 1 + updates
    _agree(path.read_bytes(), tmp_path)


@requires_qpdf
@settings(max_examples=int(os.environ.get("DIFF_FUZZ_EXAMPLES", "60")),
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(st.sampled_from(["classic", "stream", "incremental"]), st.data())
def test_mutated_chains_are_flagged_or_agree_with_the_readers(
        tmp_path: Path, which: str, draw: st.DataObject) -> None:
    data = bytearray({"classic": classic, "stream": xref_stream,
                      "incremental": incremental}[which]())
    # Mutate bytes in the chain's own region (tables, trailers, tail).
    start = data.find(b"xref") if which != "stream" else data.find(b"4 0 obj")
    for _ in range(draw.draw(st.integers(1, 3))):
        pos = draw.draw(st.integers(start, len(data) - 1))
        data[pos] = draw.draw(st.sampled_from(list(b"0123456789 \n\rnfx<>/[]")))
    _agree(bytes(data), tmp_path)
