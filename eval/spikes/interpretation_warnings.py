#!/usr/bin/env python3
"""Measures how often MuPDF's page interpreter itself warns while running
a page's content (as opposed to a parse-level qpdf/MuPDF-open warning,
which measure_corpus.py already covers) -- behind
docs/adr/0009-benign-interpretation-warnings.md.

REDESIGN §4's consumption-witness rule says "any MuPDF warning while
interpreting the stream also means not `DECODED`." This measures the
real cost of that rule as written: every warning `fitz.TOOLS.
mupdf_warnings()` collects while `page.get_texttrace()` runs, over every
page of the real corpus, categorised by a normalised (numbers and object
references stripped) version of the warning text -- never the literal
per-file text, per this directory's privacy rule. Reported on both
denominators (all files, text-bearing files), consistent with the other
Phase 1 measurements.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz  # noqa: E402

import corpus  # noqa: E402

_NUMBER_RE = re.compile(r"\d+")


def _normalise(line: str) -> str:
    """Strip numbers (object numbers, offsets, glyph indices) so the same
    underlying warning collapses to one category regardless of which
    object or glyph triggered it."""
    return _NUMBER_RE.sub("<n>", line.strip())


def _empty_bucket() -> dict[str, Any]:
    return {
        "files_measured": 0,
        "pages_checked": 0,
        "pages_with_warning": 0,
        "files_with_warning": 0,
        "category_files": Counter(),
        "category_pages": Counter(),
    }


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
        file_categories: set[str] = set()
        file_had_warning = False
        file_pages_checked = 0
        file_pages_with_warning = 0
        page_categories_seen: list[set[str]] = []
        for page in doc:
            try:
                fitz.TOOLS.mupdf_warnings()  # clear
                page.get_texttrace()
                warned = fitz.TOOLS.mupdf_warnings()
            except Exception:
                errors += 1
                continue
            file_pages_checked += 1
            lines = {_normalise(ln) for ln in warned.splitlines() if ln.strip()}
            page_categories_seen.append(lines)
            if lines:
                file_pages_with_warning += 1
                file_had_warning = True
                file_categories |= lines
        text_bearing = corpus.is_text_bearing(doc)
        doc.close()

        keys = ["all_files"] + (["text_bearing"] if text_bearing else [])
        for key in keys:
            b = buckets[key]
            b["files_measured"] += 1
            b["pages_checked"] += file_pages_checked
            b["pages_with_warning"] += file_pages_with_warning
            for cats in page_categories_seen:
                b["category_pages"].update(cats)
            if file_had_warning:
                b["files_with_warning"] += 1
                b["category_files"].update(file_categories)

    out: dict[str, Any] = {"encrypted_skipped": encrypted, "errors": errors}
    for key, b in buckets.items():
        n = b["files_measured"]
        out[key] = {
            "files_measured": n,
            "pages_checked": b["pages_checked"],
            "pages_with_warning": b["pages_with_warning"],
            "page_rate": (b["pages_with_warning"] / b["pages_checked"]
                          if b["pages_checked"] else None),
            "files_with_warning": b["files_with_warning"],
            "file_rate": b["files_with_warning"] / n if n else None,
            "top_categories_by_files_affected": dict(b["category_files"].most_common(15)),
            "top_categories_by_pages_affected": dict(b["category_pages"].most_common(15)),
        }
    return out


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = measure(files)
    result["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(result, indent=2))
