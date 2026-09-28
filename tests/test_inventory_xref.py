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
    entries[4] = (COMPRESSED, 9, 0)  # object stream 9 does not exist
    entries[5] = (1, xref, 0)        # the xref stream itself, object 5
    out += _stream_xref(entries, 6, xref) + b"startxref\n%d\n%%%%EOF\n" % xref
    reasons = [(f.reason.name, dict(f.params)) for f in read_chain(bytes(out)).flags]
    assert reasons == [("XREF_OFFSET_MISMATCH", {"object": 4, "stream": 9})]


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


# qpdf --check warnings about what a page-tree node means, not how the
# file parses: the reference graph (3a-7) owns these, not the xref chain.
# Includes a reference to a free (null) object used as a dictionary: the
# object maps agree; flagging the dangling reference is 3a-7's job.
_PAGE_TREE_SEMANTICS = (b"/Type key should be", b"attempted key retrieval",
                        b"Pages tree includes non-dictionary", b"/Kids",
                        b"operation for dictionary attempted on object of type null",
                        b"MediaBox is undefined")


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
        # --check parses every object, not just the table: it sees an object
        # that runs past the prefix cut (--show-xref and opening do not).
        # Only page-tree semantics are exempt: the reference graph's (3a-7).
        check = subprocess.run(["qpdf", "--check", str(path)], capture_output=True,
                               timeout=60)
        structural = [line for line in (check.stdout + check.stderr).splitlines()
                      if line.startswith(b"WARNING")
                      and not any(pattern in line for pattern in _PAGE_TREE_SEMANTICS)]
        assert structural == [], (revision, structural[:3])
        pymupdf.TOOLS.mupdf_warnings()
        doc = pymupdf.open(stream=blob, filetype="pdf")
        assert (doc.is_repaired, pymupdf.TOOLS.mupdf_warnings()) == (False, ""), revision


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
    # Mutate only the chain's own bytes (sections, trailers, the tail):
    # object bodies are the object parser's and reference graph's to test.
    chain = read_chain(bytes(data))
    spans = [section.span for section in chain.sections]
    assert chain.tail is not None
    spans.append(chain.tail)
    positions = [p for span in spans for p in range(span.start, span.end)]
    for _ in range(draw.draw(st.integers(1, 3))):
        pos = draw.draw(st.sampled_from(positions))
        data[pos] = draw.draw(st.sampled_from(list(b"0123456789 \n\rnfx<>/[]")))
    _agree(bytes(data), tmp_path)


# ── Shapes found by review: header offset, hybrids, revision bounds ───────
def hybrid(overlap: bool) -> bytes:
    """A table for objects 1-4 plus /XRefStm for 5 (compressed in object
    stream 4) and 6 (the xref stream). With *overlap*, the table also
    lists 5 and 6 as free, as some writers do -- readers then disagree."""
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = _objects({1: b"<< /Type /Catalog /Pages 2 0 R /Extra 5 0 R >>",
                        2: BODIES[2], 3: BODIES[3]}, out)
    members = b"5 0 "
    packed = members + b"<< /K (v) >>"
    offsets[4] = len(out)
    out += (b"4 0 obj\n<< /Type /ObjStm /N 1 /First %d /Length %d >>\nstream\n"
            % (len(members), len(packed)) + packed + b"\nendstream\nendobj\n")
    stm = len(out)
    out += _stream_xref({5: (2, 4, 0), 6: (1, stm, 0)}, 7, stm)
    table = len(out)
    rows = b"".join(b"%010d 00000 n \n" % offsets[n] for n in (1, 2, 3, 4))
    if overlap:
        head = b"xref\n0 7\n0000000000 65535 f \n" + rows + b"0000000000 00001 f \n" * 2
    else:
        head = b"xref\n0 5\n0000000000 65535 f \n" + rows
    out += head + b"trailer\n<< /Size 7 /Root 1 0 R /XRefStm %d >>\n" % stm
    out += b"startxref\n%d\n%%%%EOF\n" % table
    return bytes(out)


def test_a_header_after_junk_is_flagged() -> None:
    # Readers read offsets relative to a late header; we would not.
    assert "HEADER_OFFSET" in _reasons(b"junk\n" + classic())


def test_a_hybrid_overlap_is_flagged() -> None:
    assert "XREF_CONFLICT" in _reasons(hybrid(overlap=True))


@requires_qpdf
def test_a_hybrid_without_overlap_agrees_with_the_readers(tmp_path: Path) -> None:
    data = hybrid(overlap=False)
    chain = read_chain(data)
    assert chain.flags == ()
    assert chain.object_map()[5] == Entry(COMPRESSED, 4, 0)
    _agree(data, tmp_path)


def test_an_older_revision_cannot_point_past_its_own_end() -> None:
    first = classic()
    xref1 = int(re.findall(rb"startxref\n(\d+)", first)[-1])
    out = bytearray(first)
    later = len(out)
    out += b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] >>\nendobj\n"
    # Rewrite revision 1's entry for object 3 to point into revision 0's bytes.
    old = re.search(rb"3 1\n(\d{10})", bytes(out))
    assert old is not None
    out = bytearray(bytes(out).replace(old.group(0), b"3 1\n%010d" % later, 1))
    xref2 = len(out)
    out += (b"xref\n3 1\n%010d 00000 n \ntrailer\n<< /Size 4 /Root 1 0 R /Prev %d >>\n"
            % (later, xref1) + b"startxref\n%d\n%%%%EOF\n" % xref2)
    assert "XREF_OFFSET_MISMATCH" in _reasons(bytes(out))


def test_object_zero_in_use_and_empty_subsections_are_flagged() -> None:
    zero = classic().replace(b"0000000000 65535 f \n", b"0000000009 00000 n \n", 1)
    assert "XREF_OFFSET_MISMATCH" in _reasons(zero)
    data = classic()
    empty = data.replace(b"xref\n0 1\n", b"xref\n9 0\n0 1\n", 1)
    assert "XREF_TABLE_MALFORMED" in _reasons(empty)


def test_many_revisions_over_many_objects_stay_linear() -> None:
    import time
    out = bytearray(b"%PDF-1.7\n")
    bodies = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: BODIES[2], 3: BODIES[3]}
    bodies |= {n: b"null" for n in range(4, 100_000)}
    offsets = _objects(bodies, out)
    prev = len(out)
    out += _table(offsets, 100_000) + b"startxref\n%d\n%%%%EOF\n" % prev
    for _ in range(5_000):
        at = len(out)
        out += b"4 0 obj\nnull\nendobj\n"
        xref = len(out)
        out += (b"xref\n4 1\n%010d 00000 n \ntrailer\n<< /Size 100000 /Root 1 0 R /Prev %d >>\n"
                % (at, prev) + b"startxref\n%d\n%%%%EOF\n" % xref)
        prev = xref
    started = time.process_time()
    chain = read_chain(bytes(out))
    assert time.process_time() - started < 30  # linear ~8 s; quadratic ~53 s
    assert chain.flags == () and len(chain.revisions) == 5_001


def straddle() -> bytes:
    """Revision 1's object 4 is a stream whose data contains revision 1's
    own xref, trailer and startxref, then more content: the object runs
    past revision 1's cut. Revision 0 replaces object 4."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = _objects({1: BODIES[1], 2: BODIES[2],
                        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                           b" /Contents 4 0 R >>"}, out)
    at = len(out)
    head = b"4 0 obj\n<< /Length %05d >>\nstream\n"
    table_at = at + len(head % 0)
    table = (b"xref\n0 5\n0000000000 65535 f \n"
             + b"".join(b"%010d 00000 n \n" % v for v in (*offsets.values(), at))
             + b"trailer\n<< /Size 5 /Root 1 0 R >>\n")
    body = table + b"startxref\n%d\n%%%%EOF\n" % table_at + b"BT (SECRET-TAIL) Tj ET\n"
    out += head % len(body) + body + b"\nendstream\nendobj\n"
    newer = len(out)
    out += b"4 0 obj\n<< /Length 13 >>\nstream\nBT (ok) Tj ET\nendstream\nendobj\n"
    xref = len(out)
    out += (b"xref\n0 1\n0000000000 65535 f \n4 1\n%010d 00000 n \ntrailer" % newer
            + b"\n<< /Size 5 /Root 1 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n"
            % (table_at, xref))
    return bytes(out)


def test_an_object_straddling_its_revisions_cut_is_flagged() -> None:
    # MuPDF reads it truncated on revision 1's cut, qpdf recovers it:
    # the superseded "SECRET-TAIL" would sit outside the revision.
    assert "XREF_OFFSET_MISMATCH" in _reasons(straddle())
