#!/usr/bin/env python3
"""Measures the built-in pattern classes' false-hard rate on the real
corpus, behind docs/adr/0005-pattern-class-default-tier.md.

Runs verify.py's own BUILTIN_PATTERN_CLASSES regexes and validators
(imported, not reimplemented) against each page's plain-text extraction,
per line -- the same granularity verify.scan_page_layer already uses for
its hard-finding tier. None of the corpus files are redaction targets,
so any validated match is necessarily a false positive with respect to
the classes it claims to detect.

Scope, stated plainly: this scans page TEXT only. It does not scan
Metadata (XMP/Info, exiftool's sweep) or Objects (string literals in
dictionaries, decoded stream bodies) -- both of which today's tool also
runs pattern classes over. The measured rate is therefore a lower bound
on the false-hard rate the real tool would show, not a full measurement.
Reported on two denominators: all files, and the text-bearing stratum
(corpus.is_text_bearing) that a false-hard finding can actually occur
in -- a file with no extractable text cannot produce one via this path,
so diluting the rate by the whole (76%-plus text-free) corpus understates
what Phase 5 actually has to fix. `credit-card`'s 0% here is a sample
size limit (no corpus file happens to contain a Luhn-valid, non-date
13-19 digit run) -- it is evidence the validator rejects what's present,
not proof it would reject a real card-shaped false positive.
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
from redaction_verifier.matching import _fold_for_patterns  # noqa: E402
from redaction_verifier.rules import _make_class_rule  # noqa: E402

CLASSES = ("ssn", "credit-card", "email", "us-phone")


FALSE_HARD_CLASSES = ("ssn", "us-phone")  # excludes email/credit-card, see below


def _empty_bucket() -> dict[str, Any]:
    return {
        "checked": 0, "hit_files": {c: 0 for c in CLASSES},
        "union_files": 0, "false_hard_union_files": 0,
    }


def measure(files: list[Path]) -> dict[str, Any]:
    rules = {c: _make_class_rule(f"class:{c}", c) for c in CLASSES}
    hit_lines = {c: 0 for c in CLASSES}
    buckets = {"all_files": _empty_bucket(), "text_bearing": _empty_bucket()}
    encrypted = 0
    for f in files:
        try:
            doc = fitz.open(str(f))
        except Exception:
            continue
        if doc.needs_pass:
            encrypted += 1
            doc.close()
            continue
        file_hit: set[str] = set()
        for page in doc:
            try:
                text = page.get_text("text")
            except Exception:
                continue
            for line in text.splitlines():
                norm = _fold_for_patterns(line)
                for name, rule in rules.items():
                    for m in rule.regex.finditer(norm):
                        if rule.validator and not rule.validator(m.group()):
                            continue
                        hit_lines[name] += 1
                        file_hit.add(name)
        text_bearing = corpus.is_text_bearing(doc)
        doc.close()
        for key in (["all_files"] + (["text_bearing"] if text_bearing else [])):
            b = buckets[key]
            b["checked"] += 1
            for name in file_hit:
                b["hit_files"][name] += 1
            if file_hit:
                b["union_files"] += 1
            if file_hit & set(FALSE_HARD_CLASSES):
                b["false_hard_union_files"] += 1

    out: dict[str, Any] = {"encrypted_skipped": encrypted, "hit_lines": hit_lines}
    for key, b in buckets.items():
        n = b["checked"]
        out[key] = {
            "files_measured": n,
            "hit_files": b["hit_files"],
            "file_rate": {c: (b["hit_files"][c] / n if n else None) for c in CLASSES},
            "union_files_with_any_class": b["union_files"],
            "union_file_rate": b["union_files"] / n if n else None,
            # ssn|us-phone only: 0005 argues a validated `email` match is a
            # true positive ("there is an email here"), not a pattern
            # miscoloring unrelated digits the way an SSN-shaped date is --
            # so it does not belong in a "false hard" headline number.
            "false_hard_union_files_ssn_or_phone": b["false_hard_union_files"],
            "false_hard_union_file_rate": (
                b["false_hard_union_files"] / n if n else None
            ),
        }
    return out


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = measure(files)
    result["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(result, indent=2))
