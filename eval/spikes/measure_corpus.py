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
"""

from __future__ import annotations

import json
import re
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

# ── (d) unindexed non-whitespace byte rate ──────────────────────────────


def measure_unindexed_bytes(files: list[Path]) -> dict[str, Any]:
    total_size = total_unindexed = 0
    files_with_unindexed = 0
    errors = 0
    for f in files:
        try:
            raw = f.read_bytes()
            report = inv.tile(raw)
        except Exception:
            errors += 1
            continue
        total_size += report.size
        total_unindexed += report.unindexed_non_ws
        if report.unindexed_non_ws:
            files_with_unindexed += 1
    return {
        "files_measured": len(files) - errors,
        "errors": errors,
        "total_bytes": total_size,
        "total_unindexed_non_ws_bytes": total_unindexed,
        "byte_rate": total_unindexed / total_size if total_size else None,
        "files_with_any_unindexed_non_ws_byte": files_with_unindexed,
        "file_rate": files_with_unindexed / len(files) if files else None,
    }


# ── (b) orphaned-content-stream rate ────────────────────────────────────


def measure_orphaned_streams(files: list[Path]) -> dict[str, Any]:
    files_with_orphan = 0
    total_orphan_streams = 0
    encrypted = 0
    errors = 0
    checked = 0
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
        checked += 1
        if count:
            files_with_orphan += 1
            total_orphan_streams += count
        doc.close()
    return {
        "files_measured": checked,
        "encrypted_skipped": encrypted,
        "errors": errors,
        "files_with_orphaned_content_stream": files_with_orphan,
        "file_rate": files_with_orphan / checked if checked else None,
        "total_orphaned_content_streams": total_orphan_streams,
    }


# ── (c) parser-agreement flag rate ──────────────────────────────────────

# "Benign" qpdf parse-level warning categories -- see docs/adr/0002 for
# the reasoning and the corpus evidence behind each one. None of these
# come from `qpdf --check`'s linearization lint (REDESIGN §4 explicitly
# excludes that): this always runs `--object-streams=disable` instead.
_BENIGN_QPDF_PATTERNS = [
    re.compile(r"object has offset \d+ - a common error handled correctly"),
    re.compile(r"dictionary has duplicated key"),
    re.compile(r"expected endobj"),
    re.compile(r"input stream is complete but output may still be valid"),
]
_QPDF_SUMMARY_LINE = re.compile(r"operation succeeded with warnings")


def _qpdf_flags(stderr: str) -> tuple[bool, bool]:
    """(raw_flag, refined_flag) -- raw is true on any warning output at
    all; refined excludes the benign categories."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    raw = bool(lines)
    refined = False
    for line in lines:
        if _QPDF_SUMMARY_LINE.search(line):
            continue
        if any(p.search(line) for p in _BENIGN_QPDF_PATTERNS):
            continue
        refined = True
    return raw, refined


def measure_parser_agreement(files: list[Path]) -> dict[str, Any]:
    n = 0
    qpdf_raw = qpdf_refined = 0
    mupdf_flag = 0
    either_raw = either_refined = 0
    qpdf_unavailable = 0
    for f in files:
        n += 1
        try:
            proc = subprocess.run(
                ["qpdf", str(f), "--object-streams=disable", "/dev/null"],
                capture_output=True, timeout=30, text=True,
            )
            raw_flag = proc.returncode != 0 or bool(proc.stderr.strip())
            _, refined_flag = _qpdf_flags(proc.stderr) if raw_flag else (False, False)
        except FileNotFoundError:
            qpdf_unavailable += 1
            raw_flag = refined_flag = False
        except Exception:
            raw_flag = refined_flag = True

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

        qpdf_raw += raw_flag
        qpdf_refined += refined_flag
        mupdf_flag += mflag
        either_raw += raw_flag or mflag
        either_refined += refined_flag or mflag

    if qpdf_unavailable == n:
        return {"error": "qpdf not found on PATH -- install it to run this measurement"}
    return {
        "files_measured": n,
        "qpdf_raw_flag_rate": qpdf_raw / n,
        "qpdf_refined_flag_rate": qpdf_refined / n,
        "mupdf_flag_rate": mupdf_flag / n,
        "combined_raw_flag_rate": either_raw / n,
        "combined_refined_flag_rate": either_refined / n,
    }


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = {
        "corpus_size": len(files),
        "unindexed_bytes": measure_unindexed_bytes(files),
        "orphaned_content_streams": measure_orphaned_streams(files),
        "parser_agreement": measure_parser_agreement(files),
        "elapsed_s": round(time.time() - t0, 2),
    }
    print(json.dumps(result, indent=2))
