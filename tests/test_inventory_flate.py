"""Capped FlateDecode with predictors (Phase 3a-4a): canonical input
decodes without a flag and agrees with MuPDF and qpdf; everything else
still yields its output but is flagged; a bomb stops at the budget."""

from __future__ import annotations

import os
import subprocess
import zlib

import pymupdf
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from redaction_verifier.budget import Budget, Limits
from redaction_verifier.inventory.flate import Predictor, flate_decode

from .conftest import requires_qpdf


def _reasons(decoded: object) -> list[str]:
    return [f.reason.name for f in decoded.flags]  # type: ignore[attr-defined]


# ── A reference PNG/TIFF predictor encoder, independent of the decoder ────
def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _png_encode(raw: bytes, row: int, bpp: int, kinds: list[int]) -> bytes:
    out = bytearray()
    prev = bytes(row)
    for r, start in enumerate(range(0, len(raw), row)):
        cur = raw[start:start + row]
        kind = kinds[r % len(kinds)]
        enc = bytearray()
        for i, x in enumerate(cur):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pred = (0, a, b, (a + b) >> 1, _paeth(a, b, c))[kind]
            enc.append((x - pred) & 0xFF)
        out += bytes([kind]) + enc
        prev = cur
    return bytes(out)


def _tiff_encode(raw: bytes, row: int, bpp: int) -> bytes:
    out = bytearray(raw)
    for start in range(0, len(raw), row):
        for i in range(min(start + row, len(raw)) - 1, start + bpp - 1, -1):
            out[i] = (raw[i] - raw[i - bpp]) & 0xFF
    return bytes(out)


# ── Canonical input ───────────────────────────────────────────────────────
@given(st.binary(max_size=3000), st.integers(0, 9))
def test_canonical_streams_round_trip_without_a_flag(raw: bytes, level: int) -> None:
    packed = zlib.compress(raw, level)
    decoded = flate_decode(packed)
    assert (decoded.data, decoded.consumed, decoded.complete, decoded.flags) == (
        raw, len(packed), True, ())


def test_large_streams_cross_every_step_boundary() -> None:
    raw = os.urandom(300_000) + bytes(3_000_000)
    packed = zlib.compress(raw)
    decoded = flate_decode(packed)
    assert decoded.data == raw and decoded.complete and decoded.consumed == len(packed)


@given(st.data())
def test_png_predictors_round_trip(draw: st.DataObject) -> None:
    colors = draw.draw(st.integers(1, 5))
    bits = draw.draw(st.sampled_from([1, 2, 4, 8, 16]))
    columns = draw.draw(st.integers(1, 12))
    row = (colors * bits * columns + 7) // 8
    raw = draw.draw(st.binary(min_size=row, max_size=row * 6).map(
        lambda b: b[:len(b) - len(b) % row]))
    kinds = draw.draw(st.lists(st.integers(0, 4), min_size=1, max_size=6))
    bpp = -(-colors * bits // 8)  # whole bytes per pixel, rounded up (PNG spec)
    packed = zlib.compress(_png_encode(raw, row, bpp, kinds))
    decoded = flate_decode(packed, Predictor(draw.draw(st.integers(10, 15)), colors, bits,
                                             columns))
    assert decoded.data == raw and decoded.complete


@given(st.integers(1, 4), st.integers(1, 12), st.data())
def test_tiff_predictor_round_trips(colors: int, columns: int, draw: st.DataObject) -> None:
    row = colors * columns
    raw = draw.draw(st.binary(max_size=row * 5).map(lambda b: b[:len(b) - len(b) % row]))
    packed = zlib.compress(_tiff_encode(raw, row, colors))
    decoded = flate_decode(packed, Predictor(2, colors, 8, columns))
    assert decoded.data == raw and decoded.complete


# ── Everything else: output kept, flagged ─────────────────────────────────
def test_a_truncated_stream_keeps_its_output_and_is_flagged() -> None:
    raw = os.urandom(5000)
    packed = zlib.compress(raw)
    decoded = flate_decode(packed[:len(packed) // 2])
    assert raw.startswith(decoded.data) and not decoded.complete
    assert decoded.consumed == len(packed) // 2
    assert _reasons(decoded) == ["FLATE_TRUNCATED"]


@pytest.mark.parametrize(("extra", "non_ws"), [(b"\n", 0), (b"  \r\n", 0), (b"xyz\n", 3)])
def test_bytes_after_the_end_marker_are_flagged(extra: bytes, non_ws: int) -> None:
    packed = zlib.compress(b"hello")
    decoded = flate_decode(packed + extra)
    assert decoded.data == b"hello" and decoded.consumed == len(packed)
    assert not decoded.complete
    assert [(f.reason.name, dict(f.params)) for f in decoded.flags] == [
        ("AFTER_STREAM_END", {"consumed": len(packed), "extra": len(extra),
                              "non_whitespace": non_ws})]


def test_a_bad_checksum_and_raw_deflate_are_errors() -> None:
    packed = bytearray(zlib.compress(b"hello world"))
    packed[-1] ^= 0xFF
    assert "FLATE_ERROR" in _reasons(flate_decode(bytes(packed)))
    raw_deflate = zlib.compress(b"hello")[2:-4]
    assert _reasons(flate_decode(raw_deflate)) == ["FLATE_ERROR"]


def test_a_bomb_stops_at_the_inflated_bytes_budget() -> None:
    packed = zlib.compress(bytes(200_000_000), 9)
    budget = Budget(Limits(max_inflated_bytes=10_000_000))
    decoded = flate_decode(packed, budget=budget)
    assert len(decoded.data) <= 10_000_000 and "BUDGET_EXHAUSTED" in _reasons(decoded)
    assert budget.inflated_bytes <= 10_000_000


@pytest.mark.parametrize("parms", [
    Predictor(3), Predictor(9), Predictor(16), Predictor(2, bits=4), Predictor(10, colors=0),
    Predictor(10, bits=3), Predictor(12, columns=0),
])
def test_a_predictor_we_cannot_undo_is_flagged(parms: Predictor) -> None:
    decoded = flate_decode(zlib.compress(b"\x00abc"), parms)
    assert _reasons(decoded) == ["BAD_DECODE_PARMS"] and decoded.data == b"\x00abc"


def test_a_tiff_partial_row_is_flagged() -> None:
    # qpdf pads the last row, MuPDF does not: readers disagree.
    decoded = flate_decode(zlib.compress(bytes(17)), Predictor(2, 1, 8, 5))
    assert [(f.reason.name, dict(f.params)) for f in decoded.flags] == [
        ("PREDICTOR_ERROR", {"partial_row_bytes": 2})]


def test_a_huge_row_never_allocates_more_than_the_data() -> None:
    import tracemalloc
    tracemalloc.start()
    flate_decode(zlib.compress(b"\x02"), Predictor(12, 32, 16, 1 << 24))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 10_000_000


def test_paeth_ties_prefer_left_then_up() -> None:
    # a=b=c: pa=pb=pc=0, so the left neighbour wins; row 2 predicts from row 1.
    raw = bytes([10, 10, 10, 10])
    packed = zlib.compress(_png_encode(raw, 2, 1, [4]))
    assert flate_decode(packed, Predictor(12, 1, 8, 2)).data == raw
    tie = bytes([4, 5, 3, 0])   # a=3 b=5 c=4 after decoding: pa=1 pb=1 pc=0 -> c
    assert flate_decode(zlib.compress(_png_encode(tie, 2, 1, [4])),
                        Predictor(12, 1, 8, 2)).data == tie


def test_bad_row_types_and_partial_rows_are_flagged() -> None:
    decoded = flate_decode(zlib.compress(b"\x07abc\x00de"), Predictor(12, columns=3))
    assert [(f.reason.name, dict(f.params)) for f in decoded.flags] == [
        ("PREDICTOR_ERROR", {"partial_row_bytes": 2}),
        ("PREDICTOR_ERROR", {"bad_row_types": 1})]


@given(st.binary(max_size=400), st.integers(-3, 20), st.integers(-1, 40),
       st.integers(-1, 17), st.integers(-1, 50))
def test_never_raises(data: bytes, p: int, colors: int, bits: int, columns: int) -> None:
    decoded = flate_decode(data, Predictor(p, colors, bits, columns))
    assert 0 <= decoded.consumed <= len(data)
    assert decoded.complete == (decoded.flags == ())


# ── Differential: canonical ⇒ MuPDF and qpdf decode the same bytes ───────
EXAMPLES = int(os.environ.get("DIFF_FUZZ_EXAMPLES", "150"))


@st.composite
def _flate_cases(draw: st.DrawFn) -> tuple[bytes, bytes]:
    raw = draw(st.binary(max_size=200) | st.sampled_from([b"BT /F1 12 Tf (x) Tj ET", b""]))
    parms = b""
    body = raw
    mode = draw(st.sampled_from(["none", "png", "png", "tiff"]))
    if mode != "none":
        columns = draw(st.integers(1, 8))
        colors = draw(st.integers(1, 5))
        bits = 8 if mode == "tiff" else draw(st.sampled_from([1, 2, 4, 8, 16]))
        row = (colors * bits * columns + 7) // 8
        whole = raw[:len(raw) - len(raw) % row] or bytes(row)
        # Sometimes a partial last row: readers disagree on it, so it must flag.
        body = raw = whole + (raw[:draw(st.integers(0, row - 1))] if draw(st.booleans())
                              else b"")
        if mode == "tiff":
            body = _tiff_encode(raw, row, colors)
            p = 2
        else:
            kinds = draw(st.lists(st.integers(0, 4), min_size=1, max_size=4))
            body = _png_encode(raw, row, -(-colors * bits // 8), kinds)
            p = draw(st.integers(10, 15))
        parms = (b" /DecodeParms << /Predictor %d /Colors %d /BitsPerComponent %d"
                 b" /Columns %d >>" % (p, colors, bits, columns))
    packed = zlib.compress(body, draw(st.integers(0, 9)))
    packed = draw(st.sampled_from([
        packed, packed, packed, packed[:-1], packed[:-4], packed + b"\n", packed + b"xx",
        packed[:2] + bytes([packed[2] ^ 1]) + packed[3:] if len(packed) > 3 else packed,
    ]))
    return packed, parms


def _pdf(stream: bytes, parms: bytes) -> bytes:
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        4: (b"<< /Length %d /Filter /FlateDecode%s >>\nstream\n" % (len(stream), parms)
            + stream + b"\nendstream"),
    }
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num, text in objects.items():
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + text + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n"
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, 5))
    out += b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


@requires_qpdf
@settings(max_examples=EXAMPLES, suppress_health_check=[HealthCheck.too_slow])
@given(_flate_cases())
def test_a_canonical_decode_matches_every_reader(
        tmp_path_factory: pytest.TempPathFactory, case: tuple[bytes, bytes]) -> None:
    packed, parms = case
    predictor = None
    if parms:
        fields = parms.split()
        predictor = Predictor(int(fields[3]), int(fields[5]), int(fields[7]), int(fields[9]))
    ours = flate_decode(packed, predictor)
    if not ours.complete:
        return
    data = _pdf(packed, parms)
    pymupdf.TOOLS.mupdf_warnings()
    doc = pymupdf.open(stream=data, filetype="pdf")
    assert (doc.xref_stream(4), pymupdf.TOOLS.mupdf_warnings()) == (ours.data, ""), case
    path = tmp_path_factory.mktemp("flate") / "case.pdf"
    path.write_bytes(data)
    result = subprocess.run(["qpdf", "--show-object=4", "--filtered-stream-data", str(path)],
                            capture_output=True, timeout=30)
    assert (result.stdout, result.returncode) == (ours.data, 0), (case, result.stderr)

