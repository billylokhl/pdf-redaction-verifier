#!/usr/bin/env python3
"""Measures the built-in pattern classes' false-hard rate on the real
corpus, behind docs/adr/0005-pattern-class-default-tier.md.

Runs verify.py's own BUILTIN_PATTERN_CLASSES regexes and validators
(imported, not reimplemented) against each page's plain-text extraction,
per line -- the same granularity verify.scan_page_layer already uses for
its hard-finding tier. None of the corpus files are redaction targets,
so any validated match is necessarily a false positive with respect to
the classes it claims to detect.
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

import verify  # noqa: E402
import corpus  # noqa: E402

CLASSES = ("ssn", "credit-card", "email", "us-phone")


def measure(files: list[Path]) -> dict[str, Any]:
    rules = {c: verify._make_class_rule(f"class:{c}", c) for c in CLASSES}
    hit_files = {c: 0 for c in CLASSES}
    hit_lines = {c: 0 for c in CLASSES}
    union_files = 0
    files_checked = 0
    for f in files:
        try:
            doc = fitz.open(str(f))
        except Exception:
            continue
        if doc.needs_pass:
            doc.close()
            continue
        files_checked += 1
        file_hit: set[str] = set()
        for page in doc:
            try:
                text = page.get_text("text")
            except Exception:
                continue
            for line in text.splitlines():
                norm = verify._fold_for_patterns(line)
                for name, rule in rules.items():
                    for m in rule.regex.finditer(norm):
                        if rule.validator and not rule.validator(m.group()):
                            continue
                        hit_lines[name] += 1
                        file_hit.add(name)
        for name in file_hit:
            hit_files[name] += 1
        if file_hit:
            union_files += 1
        doc.close()
    return {
        "files_measured": files_checked,
        "hit_files": hit_files,
        "hit_lines": hit_lines,
        "file_rate": {c: hit_files[c] / files_checked if files_checked else None
                      for c in CLASSES},
        "union_files_with_any_class": union_files,
        "union_file_rate": union_files / files_checked if files_checked else None,
    }


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = measure(files)
    result["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(result, indent=2))
