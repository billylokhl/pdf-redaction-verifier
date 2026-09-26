#!/usr/bin/env python3
"""A spike-quality byte tiler, standing in for the real Phase 3a inventory
just far enough to measure two Phase 1 numbers on the real corpus:

  * the unindexed non-whitespace byte rate (ADR 0003 / 0007), and
  * the orphaned-content-stream rate (ADR 0007), the latter reusing
    verify.py's own reachability walk and content-stream sniff rather
    than re-deriving them.

This is NOT the real inventory: object spans are recovered by scanning
for "N G obj" headers and matching /Length (falling back to a literal
endstream search), not by a validated grammar with ambiguity detection.
See eval/spikes/README.md for the known limitations. Do not import this
from verify.py or eval/caselib — it is throwaway.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import verify  # noqa: E402  (path insert must come first)

# PDF whitespace per spec: NUL, HT, LF, FF, CR, SP.
_PDF_WS = frozenset(b"\x00\t\n\x0c\r ")

_OBJ_HEADER_FULL_RE = re.compile(rb"(?:^|[^0-9])(\d+)[ \t\r\n]+(\d+)[ \t\r\n]+obj\b")
_STREAM_KW_RE = re.compile(rb"stream\r?\n")
_ENDSTREAM_RE = re.compile(rb"endstream")
_ENDOBJ_RE = re.compile(rb"endobj\b")
_INDIRECT_LENGTH_RE = re.compile(rb"/Length\s+(\d+)\s+(\d+)\s+R\b")
_DIRECT_LENGTH_RE = re.compile(rb"/Length\s+(\d+)(?!\s*\d)")
_XREFSTM_RE = re.compile(rb"/XRefStm\s+(\d+)")
# A PDF line may end in CR, LF, or CRLF (spec allows all three).
_EOL_RE = re.compile(rb"\r\n|\r|\n")


@dataclass
class TileReport:
    size: int
    covered: int
    unindexed_non_ws: int
    object_count: int
    ambiguous_objects: int  # /Length disagreed with endstream, or no endobj found


def _object_span(raw: bytes, header_end: int) -> tuple[int, bool]:
    """(end offset just past this object, ambiguous) for the object whose
    header ends at *header_end* ('obj' keyword just matched)."""
    n = len(raw)
    j = header_end
    dict_end = None
    while j < n and raw[j] in _PDF_WS:
        j += 1
    if raw.startswith(b"<<", j):
        dict_end = verify._dict_end(raw, j)
    search_from = dict_end if dict_end is not None else j
    # A stream keyword can only follow a dictionary; look in a short window
    # (whitespace/comments between '>>' and 'stream' are rare and small).
    window_end = min(n, search_from + 32)
    stream_m = _STREAM_KW_RE.search(raw, search_from, window_end)
    if stream_m is None:
        end_m = _ENDOBJ_RE.search(raw, search_from, min(n, search_from + 1 << 20))
        return (end_m.end(), False) if end_m else (n, True)

    body_start = stream_m.end()
    dict_bytes = raw[j:dict_end] if dict_end is not None else b""
    ambiguous = False
    endstream_pos = None
    if not _INDIRECT_LENGTH_RE.search(dict_bytes):
        m = _DIRECT_LENGTH_RE.search(dict_bytes)
        if m:
            declared_end = body_start + int(m.group(1))
            # Confirm 'endstream' appears where /Length says it should,
            # allowing 0-2 bytes of EOL slack before it.
            for slack in (0, 1, 2):
                if raw.startswith(b"endstream", declared_end + slack):
                    endstream_pos = declared_end + slack
                    break
    if endstream_pos is None:
        # /Length missing, indirect, or disagreeing with 'endstream':
        # fall back to a literal scan, and record the ambiguity.
        found = _ENDSTREAM_RE.search(raw, body_start)
        ambiguous = True
        if found is None:
            return n, True
        endstream_pos = found.start()
    tail = endstream_pos + len(b"endstream")
    end_m = _ENDOBJ_RE.search(raw, tail, min(n, tail + 64))
    return (end_m.end() if end_m else tail), ambiguous


def _header_span(raw: bytes) -> int:
    """Bytes covered by the '%PDF-x.y' line and every '%...' comment line
    right after it (the binary marker, and any producer comment some
    writers add before the first object). A line may end in CR, LF, or
    CRLF (all three appear in the corpus)."""
    m = _EOL_RE.search(raw, 0, 64)
    end = m.end() if m else min(len(raw), 16)
    while raw[end:end + 1] == b"%":
        m = _EOL_RE.search(raw, end, end + 256)
        if m is None:
            break
        end = m.end()
    return end


def _xref_chain_spans(raw: bytes) -> list[tuple[int, int]]:
    """[start, end) for every xref/trailer section (current + /Prev chain,
    plus hybrid-file /XRefStm sections) and its startxref+%%EOF epilogue."""
    spans: list[tuple[int, int]] = []
    starts = [m.group(1) for m in verify._STARTXREF_RE.finditer(raw)]
    if not starts:
        return spans
    seen: set[int] = set()
    pending = [int(starts[-1])]
    while pending:
        offset = pending.pop()
        if offset in seen or not (0 <= offset < len(raw)):
            continue
        seen.add(offset)
        found = verify._xref_section(raw, offset)
        if found is None:
            continue
        end, prev, _objects = found
        spans.append((offset, end))
        if prev is not None:
            pending.append(prev)
        # Hybrid file: a classic xref section may carry /XRefStm pointing
        # at a cross-reference stream holding compressed-object entries.
        if raw.startswith(b"xref", offset):
            trailer = raw.find(b"trailer", offset)
            opening = raw.find(b"<<", trailer) if trailer >= 0 else -1
            dict_end = verify._dict_end(raw, opening) if opening >= 0 else None
            if dict_end is not None:
                m = _XREFSTM_RE.search(raw[opening:dict_end])
                if m:
                    pending.append(int(m.group(1)))
    # startxref NNNN ... %%EOF epilogues.
    for m in verify._STARTXREF_RE.finditer(raw):
        eof = raw.find(b"%%EOF", m.end(), m.end() + 64)
        spans.append((m.start(), eof + 5 if eof >= 0 else m.end()))
    return spans


def tile(raw: bytes) -> TileReport:
    n = len(raw)
    covered = bytearray(n)  # 0/1 per byte; small files only (spike scale)

    def mark(a: int, b: int) -> None:
        a, b = max(0, a), min(n, b)
        if a < b:
            covered[a:b] = b"\x01" * (b - a)

    header_end = _header_span(raw)
    mark(0, header_end)
    for a, b in _xref_chain_spans(raw):
        mark(a, b)

    object_count = 0
    ambiguous_objects = 0
    pos = 0
    while True:
        m = _OBJ_HEADER_FULL_RE.search(raw, pos)
        if m is None:
            break
        header_start = m.start(1)
        end, ambiguous = _object_span(raw, m.end())
        mark(header_start, end)
        object_count += 1
        ambiguous_objects += ambiguous
        pos = max(end, m.end())

    unindexed_non_ws = sum(
        1 for i in range(n) if not covered[i] and raw[i] not in _PDF_WS
    )
    return TileReport(
        size=n,
        covered=sum(covered),
        unindexed_non_ws=unindexed_non_ws,
        object_count=object_count,
        ambiguous_objects=ambiguous_objects,
    )


def orphaned_content_streams(doc: "verify.fitz.Document") -> int:
    """Count of objects in the current revision that are (a) unreachable
    from the trailer (a trustworthy walk) and (b) sniff as a content
    stream (>=1 shown text object) via verify._scan_content — the same
    sniff verify.py's own Objects layer uses to tell page content from a
    text payload. Reuses verify._reachable_from_sources verbatim."""
    try:
        xref_count = doc.xref_length()
        trailer = doc.pdf_trailer()
    except Exception:
        return 0
    sources: dict[int, str] = {}
    for xref in range(1, xref_count):
        try:
            sources[xref] = doc.xref_object(xref, compressed=True)
        except Exception:
            pass
    reachable, trusted = verify._reachable_from_sources(trailer, sources, xref_count)
    if not trusted:
        return 0
    count = 0
    for xref in sources:
        if xref in reachable:
            continue
        try:
            body = doc.xref_stream(xref)
        except Exception:
            continue
        if not body:
            continue
        try:
            text = body.decode("latin-1")
        except Exception:
            continue
        if verify._is_content_stream(text):
            count += 1
    return count


if __name__ == "__main__":
    # Smoke test on a scratch PDF, not part of the measured corpus.
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "hello", fontname="helv")
    raw = doc.tobytes()
    report = tile(raw)
    print(report)
