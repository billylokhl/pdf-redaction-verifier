#!/usr/bin/env python3
"""Measures how many stored images fall below docs/adr/0004's 8 px
envelope floor, on the real corpus -- behind docs/adr/0004's claim that
"no tiny images" exist, which review found false.

Walks every object in every file (doc.xref_length()), checks
/Subtype /Image, and reads /Width and /Height directly via
doc.xref_get_key -- cheap, no decompression needed. An image counts as
"tiny" when either dimension is below 8 px (docs/adr/0004's floor).
Reported on both denominators; aggregate counts only.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz  # noqa: E402

import corpus  # noqa: E402

TINY_FLOOR = 8


def _empty_bucket() -> dict[str, Any]:
    return {"files_measured": 0, "files_with_tiny_image": 0, "total_tiny_images": 0}


def measure(files: list[Path]) -> dict[str, Any]:
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
        tiny_count = 0
        try:
            n = doc.xref_length()
        except Exception:
            n = 0
        for xref in range(1, n):
            try:
                if doc.xref_get_key(xref, "Subtype") != ("name", "/Image"):
                    continue
                w = int(doc.xref_get_key(xref, "Width")[1])
                h = int(doc.xref_get_key(xref, "Height")[1])
            except Exception:
                continue
            if w < TINY_FLOOR or h < TINY_FLOOR:
                tiny_count += 1
        text_bearing = corpus.is_text_bearing(doc)
        doc.close()
        for key in (["all_files"] + (["text_bearing"] if text_bearing else [])):
            b = buckets[key]
            b["files_measured"] += 1
            if tiny_count:
                b["files_with_tiny_image"] += 1
                b["total_tiny_images"] += tiny_count

    out: dict[str, Any] = {"encrypted_skipped": encrypted, "errors": errors,
                            "tiny_floor_px": TINY_FLOOR}
    for key, b in buckets.items():
        n = b["files_measured"]
        out[key] = {
            "files_measured": n,
            "files_with_tiny_image": b["files_with_tiny_image"],
            "file_rate": b["files_with_tiny_image"] / n if n else None,
            "total_tiny_images": b["total_tiny_images"],
        }
    return out


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = measure(files)
    result["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(result, indent=2))
