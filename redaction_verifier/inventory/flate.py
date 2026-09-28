"""Capped FlateDecode with predictors (ISO 32000-1 §7.4.4) for the
inventory's own reads: xref streams and object streams (3a-4b, 3a-5).

Readers are the authority (ADR 0010): the canonical case -- a complete
zlib stream, checksum good, every input byte consumed, rows whole and
predictor bytes valid -- decodes without a flag. Anything else still
yields whatever output was produced, and is flagged: a zlib error, a
stream that ends before zlib's end marker, bytes after it, a bad or
unsupported predictor, a partial last row. Output is charged to the
run's Budget (inflated bytes) as it is produced, in bounded steps, so a
decompression bomb stops at the cap. Never raises.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Final

from ..budget import Budget, Counter
from ..ledger import Flag, FlagReason, Span

_IN_STEP: Final = 1 << 16    # input fed to zlib per call
_OUT_STEP: Final = 1 << 20   # output zlib may produce per call
_WHITESPACE: Final = b"\0\t\n\f\r "


@dataclass(frozen=True)
class Predictor:
    """/DecodeParms for a predictor (§7.4.4.4, Table 8). Defaults are
    the spec's. ``predictor`` 1 is none, 2 TIFF, 10-15 PNG."""

    predictor: int = 1
    colors: int = 1
    bits: int = 8
    columns: int = 1


@dataclass(frozen=True)
class Decoded:
    """What a decode produced. ``consumed`` counts the input bytes zlib
    used (up to and including its end marker when found); ``complete``
    is True only for the canonical case, with no flag."""

    data: bytes
    consumed: int
    complete: bool
    flags: tuple[Flag, ...]


def flate_decode(data: bytes, predictor: Predictor | None = None,
                 budget: Budget | None = None, where: Span | None = None) -> Decoded:
    """Inflate *data*, then undo *predictor*. *where* is the input's span
    in the file, for flags (defaults to 0..len(data))."""
    span = where if where is not None else Span(0, len(data))
    flags: list[Flag] = []
    out = bytearray()
    inflater = zlib.decompressobj()
    pos = 0
    pending = b""
    failed = False
    while not inflater.eof:
        if pending:
            chunk = pending
        elif pos < len(data):
            chunk, pos = data[pos:pos + _IN_STEP], min(pos + _IN_STEP, len(data))
        else:
            break
        try:
            piece = inflater.decompress(chunk, _OUT_STEP)
        except zlib.error:
            flags.append(Flag(FlagReason.FLATE_ERROR, span, (("output", len(out)),)))
            failed = True
            break
        pending = inflater.unconsumed_tail
        if budget is not None and not budget.charge_inflated_bytes(len(piece)):
            flags.append(Flag(FlagReason.BUDGET_EXHAUSTED, span,
                              (("counter", int(Counter.INFLATED_BYTES)),)))
            failed = True
            break
        out += piece
    if inflater.eof:
        leftover = inflater.unused_data + data[pos:]
        consumed = len(data) - len(leftover)
        if leftover:
            non_ws = sum(1 for b in leftover if b not in _WHITESPACE)
            flags.append(Flag(FlagReason.AFTER_STREAM_END, span, (
                ("consumed", consumed), ("extra", len(leftover)), ("non_whitespace", non_ws))))
    else:
        consumed = pos - len(pending)
        if not failed:
            flags.append(Flag(FlagReason.FLATE_TRUNCATED, span, (("output", len(out)),)))
    decoded = bytes(out)
    if predictor is not None and predictor.predictor != 1:
        decoded, predictor_flags = _unpredict(decoded, predictor, span)
        flags.extend(predictor_flags)
    return Decoded(decoded, consumed, not flags, tuple(flags))


def _unpredict(data: bytes, parms: Predictor,
               span: Span) -> tuple[bytes, tuple[Flag, ...]]:
    p, colors, bits, columns = parms.predictor, parms.colors, parms.bits, parms.columns
    valid = (p in (2, 10, 11, 12, 13, 14, 15) and 1 <= colors <= 32
             and bits in (1, 2, 4, 8, 16) and 1 <= columns <= 1 << 24)
    if not valid or (p == 2 and bits != 8):
        # A predictor we cannot undo exactly: return the bytes as they are.
        return data, (Flag(FlagReason.BAD_DECODE_PARMS, span, (
            ("predictor", p if isinstance(p, int) else -1),)),)
    row = (colors * bits * columns + 7) // 8
    if p == 2:
        return _tiff(data, row, colors), ()
    return _png(data, row, max(1, colors * bits // 8), span)


def _tiff(data: bytes, row: int, bpp: int) -> bytes:
    out = bytearray(data)
    for start in range(0, len(out), row):
        for i in range(start + bpp, min(start + row, len(out))):
            out[i] = (out[i] + out[i - bpp]) & 0xFF
    return bytes(out)


def _png(data: bytes, row: int, bpp: int, span: Span) -> tuple[bytes, tuple[Flag, ...]]:
    """Undo PNG row filters (§7.4.4.4; PNG spec §6). Every row carries its
    own filter-type byte whatever /Predictor 10-15 says, as readers do."""
    out = bytearray()
    prev = bytearray(row)
    flags: list[Flag] = []
    bad_types = 0
    stride = row + 1
    for start in range(0, len(data), stride):
        kind = data[start]
        cur = bytearray(data[start + 1:start + stride])
        n = len(cur)
        if n < row:  # filtered as far as it goes; never padded with bytes
            flags.append(Flag(FlagReason.PREDICTOR_ERROR, span, (("partial_row_bytes", n),)))
        if kind == 1:
            for i in range(bpp, n):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(n):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(n):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(n):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        elif kind != 0:
            bad_types += 1
        out += cur
        prev = cur + bytearray(row - n)
    if bad_types:
        flags.append(Flag(FlagReason.PREDICTOR_ERROR, span, (("bad_row_types", bad_types),)))
    return bytes(out), tuple(flags)
