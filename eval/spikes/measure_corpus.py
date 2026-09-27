#!/usr/bin/env python3
"""Phase 1 corpus measurements (docs/REDESIGN.md §7, Phase 1 row):

  * unindexed non-whitespace byte rate      -> docs/adr/0003, 0007
  * orphaned-content-stream rate            -> docs/adr/0007
  * parser-agreement flag rate, raw and     -> docs/adr/0002
    with benign categories excluded

Requires qpdf on PATH (`brew install qpdf`) for the parser-agreement
measurement; the other two only need PyMuPDF. Prints one aggregate JSON
object to stdout -- see eval/spikes/README.md for the privacy rule this
follows (paths are never printed, only counts and rates).

Every rate is reported on two denominators: all files, and the
text-bearing stratum (corpus.is_text_bearing) that docs/adr/0005 and
Phase 5 actually gate on. A fail-closed corpus tool should never let a
majority-non-text corpus dilute a rate that matters for text-bearing
files -- see docs/adr/0007's "Correction" note for why the file-level
orphan rate looked small until this stratification was added.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz  # noqa: E402

import corpus  # noqa: E402
import inventory_lite as inv  # noqa: E402

QPDF = shutil.which("qpdf")


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _stratify(files: list[Path]) -> tuple[list[Path], list[Path], int, int]:
    """(all_files, text_bearing_files, encrypted_count, open_error_count).
    Opens every file once so every measurement below shares one
    text-bearing classification (corpus.is_text_bearing)."""
    text_bearing: list[Path] = []
    encrypted = errors = 0
    for f in files:
        try:
            doc = fitz.open(str(f))
        except Exception:
            errors += 1
            continue
        if doc.needs_pass:
            encrypted += 1
        elif corpus.is_text_bearing(doc):
            text_bearing.append(f)
        doc.close()
    return files, text_bearing, encrypted, errors


# ── (d) unindexed non-whitespace byte rate ──────────────────────────────


def measure_unindexed_bytes(files: list[Path], text_bearing: set[Path]) -> dict[str, Any]:
    total_size = total_unindexed = 0
    tb_size = tb_unindexed = 0
    files_with_unindexed = tb_files_with_unindexed = 0
    errors = 0
    object_counts: list[int] = []
    for f in files:
        try:
            raw = f.read_bytes()
            report = inv.tile(raw)
        except Exception:
            errors += 1
            continue
        object_counts.append(report.object_count)
        total_size += report.size
        total_unindexed += report.unindexed_non_ws
        hit = bool(report.unindexed_non_ws)
        files_with_unindexed += hit
        if f in text_bearing:
            tb_size += report.size
            tb_unindexed += report.unindexed_non_ws
            tb_files_with_unindexed += hit
    object_counts.sort(reverse=True)
    return {
        "all_files": {
            "files_measured": len(files) - errors,
            "total_bytes": total_size,
            "total_unindexed_non_ws_bytes": total_unindexed,
            "byte_rate": _rate(total_unindexed, total_size),
            "files_with_any_unindexed_non_ws_byte": files_with_unindexed,
            "file_rate": _rate(files_with_unindexed, len(files)),
        },
        # Per-file object counts from inventory_lite.tile() -- cited by
        # docs/adr/0006 to size the units-per-run budget.
        "object_count_distribution": {
            "top_10": object_counts[:10],
            "median": object_counts[len(object_counts) // 2] if object_counts else None,
            "p95": object_counts[len(object_counts) // 20] if object_counts else None,
        },
        "text_bearing": {
            "files_measured": len(text_bearing),
            "total_bytes": tb_size,
            "total_unindexed_non_ws_bytes": tb_unindexed,
            "byte_rate": _rate(tb_unindexed, tb_size),
            "files_with_any_unindexed_non_ws_byte": tb_files_with_unindexed,
            "file_rate": _rate(tb_files_with_unindexed, len(text_bearing)),
        },
        "errors": errors,
    }


# ── (b) orphaned-content-stream rate ────────────────────────────────────
#
# Scope, stated plainly (docs/adr/0007's "Correction"): this counts only
# objects that (a) have a stream body and (b) sniff as content via
# verify._is_content_stream (>=1 shown text object). It does NOT count:
#   - a paint-only orphan (fills/strokes/draws an image, no text -- the
#     K5 shape: text converted to outlines in an orphaned stream);
#   - a "dead body" never even in the xref table (no object number at
#     all -- inventory_lite's byte tiler, not this function, is what
#     would find those, as unindexed bytes that happen to look like an
#     "obj ... endobj" span the current xref doesn't index).
# So this is a LOWER BOUND on "orphaned content", not a ceiling.


def measure_orphaned_streams(files: list[Path], text_bearing: set[Path]) -> dict[str, Any]:
    def _empty_bucket() -> dict[str, Any]:
        return {"checked": 0, "files_with_orphan": 0, "total_streams": 0, "counts": []}

    buckets = {"all_files": _empty_bucket(), "text_bearing": _empty_bucket()}
    encrypted = errors = 0
    for f in files:
        try:
            doc = fitz.open(str(f))
        except Exception:
            errors += 1
            continue
        if doc.needs_pass:
            encrypted += 1
            doc.close()
            continue
        try:
            count = inv.orphaned_content_streams(doc)
        except Exception:
            errors += 1
            doc.close()
            continue
        for key in (["all_files"] + (["text_bearing"] if f in text_bearing else [])):
            b = buckets[key]
            b["checked"] += 1
            if count:
                b["files_with_orphan"] += 1
                b["total_streams"] += count
                b["counts"].append(count)
        doc.close()
    out: dict[str, Any] = {"encrypted_skipped": encrypted, "errors": errors}
    for key, b in buckets.items():
        counts = sorted(b.pop("counts"), reverse=True)
        b["file_rate"] = _rate(b["files_with_orphan"], b["checked"])
        b["top_counts"] = counts[:5]
        b["median_count_among_affected_files"] = (
            counts[len(counts) // 2] if counts else None
        )
        b["files_with_exactly_one"] = sum(1 for c in counts if c == 1)
        out[key] = b
    return out


# ── (c) parser-agreement flag rate ──────────────────────────────────────
#
# Benign categories (docs/adr/0002, accepted -- tightened after review):
# a wrong/zero xref offset is benign only when the inventory's own raw
# byte scan finds NO body for that object number anywhere in the file
# (nothing was actually lost); a duplicated dictionary key is benign only
# when both occurrences' values are textually identical (ISO 32000 does
# not define a resolution order for a duplicate key -- ambiguity is the
# default, not "last wins"). Both checks are verified per warning, not
# assumed from qpdf's own wording. Categories the earlier pass called
# benign on qpdf's wording alone ("expected endobj", "input stream is
# complete but output may still be valid" -- actually an unterminated
# Flate stream warning, not an inline-image note) are DROPPED: always
# flag. Any object number or key this script cannot parse out of qpdf's
# message, or cannot verify, counts as NOT benign -- fail closed.

_OFFSET_WARNING_RE = re.compile(
    r"\(object (\d+) \d+\): object has offset \d+ - a common error"
)
_DUP_KEY_RE = re.compile(
    r"\(object (\d+) \d+(?:, offset \d+)?\): dictionary has duplicated key (/[^\s;]+)"
)
_OBJ_BODY_TEMPLATE = rb"(?:^|[^0-9])%d[ \t\r\n]+\d+[ \t\r\n]+obj\b"


def _object_body_span(raw: bytes, number: int) -> tuple[int, int] | None:
    m = re.search(_OBJ_BODY_TEMPLATE % number, raw)
    if not m:
        return None
    start = m.end()
    window = raw[start:start + 20000]
    ends = [x.start() for x in (
        re.search(rb"\bstream\b", window), re.search(rb"\bendobj\b", window)
    ) if x]
    return start, start + (min(ends) if ends else len(window))


def _offset_warning_benign(raw: bytes, line: str) -> bool:
    m = _OFFSET_WARNING_RE.search(line)
    if not m:
        return False  # not this category, or unparseable -- caller won't call this then
    return _object_body_span(raw, int(m.group(1))) is None  # no body anywhere -- nothing lost


def _dup_key_benign(raw: bytes, line: str) -> bool:
    m = _DUP_KEY_RE.search(line)
    if not m:
        return False
    span = _object_body_span(raw, int(m.group(1)))
    if span is None:
        return False  # can't verify -- fail closed
    body = raw[span[0]:span[1]]
    key = re.escape(m.group(2).encode())
    values = [
        vm.group(1)
        for vm in re.finditer(key + rb"\s+(/[^\s/()<>\[\]]+|[^\s/()<>\[\]]+)", body)
    ]
    if len(values) < 2:
        return False  # couldn't recover both occurrences -- fail closed
    return len(set(values)) == 1


_QPDF_SUMMARY_LINE = re.compile(r"operation succeeded with warnings")


def _qpdf_flags(raw: bytes, stderr: str) -> tuple[bool, bool]:
    """(raw_flag, refined_flag) -- raw is true on any warning output at
    all; refined additionally excludes only the two verified-benign
    categories above."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    raw_flag = bool(lines)
    refined_flag = False
    for line in lines:
        if _QPDF_SUMMARY_LINE.search(line):
            continue
        if _OFFSET_WARNING_RE.search(line) and _offset_warning_benign(raw, line):
            continue
        if _DUP_KEY_RE.search(line) and _dup_key_benign(raw, line):
            continue
        refined_flag = True
    return raw_flag, refined_flag


def measure_parser_agreement(files: list[Path], text_bearing: set[Path]) -> dict[str, Any]:
    if QPDF is None:
        return {"error": "qpdf not found on PATH -- install it to run this measurement"}

    def _empty() -> dict[str, int]:
        return {"n": 0, "qpdf_raw": 0, "qpdf_refined": 0, "mupdf": 0, "raw": 0, "refined": 0}

    buckets = {"all_files": _empty(), "text_bearing": _empty()}
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        try:
            proc = subprocess.run(
                [QPDF, str(f), "--object-streams=disable", "/dev/null"],
                capture_output=True, timeout=30, text=True,
            )
            qpdf_raw = proc.returncode != 0 or bool(proc.stderr.strip())
            _, qpdf_refined = _qpdf_flags(raw, proc.stderr) if qpdf_raw else (False, False)
        except Exception:
            qpdf_raw = qpdf_refined = True

        mflag = False
        try:
            fitz.TOOLS.mupdf_warnings()
            doc = fitz.open(str(f))
            warned = fitz.TOOLS.mupdf_warnings()
            if doc.is_repaired or warned.strip():
                mflag = True
            doc.close()
        except Exception:
            mflag = True

        for key in (["all_files"] + (["text_bearing"] if f in text_bearing else [])):
            b = buckets[key]
            b["n"] += 1
            b["qpdf_raw"] += qpdf_raw
            b["qpdf_refined"] += qpdf_refined
            b["mupdf"] += mflag
            b["raw"] += qpdf_raw or mflag
            b["refined"] += qpdf_refined or mflag

    out: dict[str, Any] = {}
    for key, b in buckets.items():
        n = b["n"]
        out[key] = {
            "files_measured": n,
            "qpdf_raw_flag_rate": _rate(b["qpdf_raw"], n),
            "qpdf_refined_flag_rate": _rate(b["qpdf_refined"], n),
            "mupdf_flag_rate": _rate(b["mupdf"], n),
            "combined_raw_flag_rate": _rate(b["raw"], n),
            "combined_refined_flag_rate": _rate(b["refined"], n),
        }
    return out


if __name__ == "__main__":
    t0 = time.time()
    all_files = corpus.discover()
    _, text_bearing_list, encrypted, open_errors = _stratify(all_files)
    text_bearing_set = set(text_bearing_list)
    result = {
        "corpus_size": len(all_files),
        "text_bearing_corpus_size": len(text_bearing_list),
        "encrypted_skipped": encrypted,
        "open_errors": open_errors,
        "unindexed_bytes": measure_unindexed_bytes(all_files, text_bearing_set),
        "orphaned_content_streams": measure_orphaned_streams(all_files, text_bearing_set),
        "parser_agreement": measure_parser_agreement(all_files, text_bearing_set),
        "elapsed_s": round(time.time() - t0, 2),
    }
    print(json.dumps(result, indent=2))
