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

It then measures docs/adr/0009's accepted rule on the same warned pages,
with pages as units and s1b_consumption_witness.witness(unit_only=True)
as the per-unit witness:

* **naive rule** -- a warned page is excused when the witness balances
  (code count == glyph count), whatever the warning was;
* **guarded rule** -- the naive rule plus three exit-0 guards: (a) a
  filter/decode-error warning is never excused (the witness counts
  MuPDF's own decoded bytes, so a truncated stream loses text on both
  sides equally and still balances); (b) 0 == 0 is not balance; (c) an
  image-decoder warning is never vouched for by a text-code count (only
  by the image's own witness, Phase 4b). A page the witness cannot
  measure (it draws a Form XObject, or uses an unmodelled CMap) stays
  flagged under both.

Pages as units is an approximation of REDESIGN §4's per-unit design:
the witness deletes a page's annotations and widgets before tracing (so
it compares the page's own content stream), but the warnings are
collected with them present, so a warning that fired while interpreting
an annotation's appearance stream would be excused by the page
content's witness. The count of such pages, and the files the guarded
rule would still flag if they were not excused, are reported alongside.

**Call order matters.** MuPDF emits some warnings only the first time a
resource is loaded, so a pass that has already run over a page (e.g.
`get_text()`) hides them from a later `get_texttrace()`. The rule is
measured in the order it must really run in -- `get_texttrace()` first,
on a freshly opened document, so no warning is consumed by an earlier
pass -- and the warned-file count is also reported for the other order
(`get_text()` over the whole document first) to size the dependence.
Text-bearing classification uses a separate, fresh open of the file.
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
import s1b_consumption_witness as s1b  # noqa: E402

_NUMBER_RE = re.compile(r"\d+")

# Families of normalised warning lines, first match wins. Aggregate labels
# only. "... repeated <n> times" is MuPDF's own repeat suppression (it
# stands for more copies of the preceding warning), not a category.
REPEAT = "repeat-suppression notice (not a category)"
IMAGE = "image decoder (JPEG 2000 / JPEG / JBIG2)"
FILTER = "filter / decode error"
_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (REPEAT, re.compile(r"repeated <n> times", re.I)),
    (IMAGE, re.compile(r"jpx|openjpeg|jp2|jpeg|dct|jbig2", re.I)),
    (FILTER, re.compile(r"flate|zlib|inflate|premature end|lzw|ascii85|asciihex|"
                        r"runlength|ccitt|decod|corrupt|truncat|unexpected (end|eof)",
                        re.I)),
    ("font: FT_Get_Advance invalid glyph index", re.compile(r"FT_Get_Advance")),
    ("font: ascent/descent", re.compile(r"ascent|descent", re.I)),
)


def _normalise(line: str) -> str:
    """Strip numbers (object numbers, offsets, glyph indices) so the same
    underlying warning collapses to one category regardless of which
    object or glyph triggered it."""
    return _NUMBER_RE.sub("<n>", line.strip())


def family(category: str) -> str:
    for name, pattern in _FAMILIES:
        if pattern.search(category):
            return name
    return "other"


def _empty_bucket() -> dict[str, Any]:
    return {
        "files_measured": 0,
        "pages_checked": 0,
        "pages_with_warning": 0,
        "files_with_warning": 0,
        "category_files": Counter(),
        "category_pages": Counter(),
        "family_files": Counter(),
        "files_with_warning_get_text_first": 0,
        "family_files_get_text_first": Counter(),
        "rule": {
            "naive": {"files_still_flagged": 0, "pages_excused": 0,
                      "pages_flagged_by_reason": Counter()},
            "guarded": {"files_still_flagged": 0, "pages_excused": 0,
                        "pages_flagged_by_reason": Counter()},
            "naive_excused_pages_with_filter_warning": 0,
            "naive_excused_pages_with_filter_warning_at_0_eq_0": 0,
            "naive_excused_pages_at_0_eq_0": 0,
            "naive_excused_pages_with_image_decoder_warning": 0,
            "warned_pages_with_annotations_or_widgets": 0,
            "guarded_excused_pages_with_annotations_or_widgets": 0,
            "guarded_files_still_flagged_if_those_were_not_excused": 0,
        },
    }


def _warnings_per_page(doc: fitz.Document) -> list[set[str]]:
    """Normalised warning categories for each page's get_texttrace()
    (the caller counts an exception as a file-level error)."""
    out: list[set[str]] = []
    for page in doc:
        fitz.TOOLS.mupdf_warnings()  # clear
        page.get_texttrace()
        warned = fitz.TOOLS.mupdf_warnings()
        out.append({_normalise(ln) for ln in warned.splitlines() if ln.strip()})
    return out


def _judge(page: fitz.Page, categories: set[str]) -> dict[str, Any]:
    """Both rules' outcome for one warned page ("excused" or a reason)."""
    families = {family(c) for c in categories}
    has_annots = page.first_annot is not None or page.first_widget is not None
    try:
        code, glyph, matches, _ = s1b.witness(page, unit_only=True)
    except s1b._SkipPage as skip:
        reason = f"unmeasured page: {skip.reason}"
        return {"naive": reason, "guarded": reason, "families": families,
                "zero": False, "annots": has_annots}
    except Exception:
        return {"naive": "witness error", "guarded": "witness error",
                "families": families, "zero": False, "annots": has_annots}
    zero = code == 0 and glyph == 0
    naive = "excused" if matches else "witness mismatch"
    if FILTER in families:
        guarded = "filter/decode warning (never excused)"
    elif IMAGE in families:
        guarded = "image-decoder warning (unwitnessed image XObject)"
    elif zero:
        guarded = "0 == 0 is not balance"
    elif not matches:
        guarded = "witness mismatch"
    else:
        guarded = "excused"
    return {"naive": naive, "guarded": guarded, "families": families, "zero": zero,
            "annots": has_annots}


def measure(files: list[Path]) -> dict[str, Any]:
    buckets = {"all_files": _empty_bucket(), "text_bearing": _empty_bucket()}
    encrypted = errors = 0
    for f in files:
        # Pass 1, the order the rule must run in: get_texttrace() first,
        # on a fresh document, then the witness on each warned page (after
        # every page's warnings are collected -- the witness deletes a
        # page's annotations in memory).
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
            per_page = _warnings_per_page(doc)
        except Exception:
            errors += 1
            doc.close()
            continue
        judged = [_judge(doc[i], cats) for i, cats in enumerate(per_page) if cats]
        doc.close()

        # Pass 2, a separate fresh open: get_text() over every page first
        # (which also gives the text-bearing classification), then
        # get_texttrace() -- the other call order.
        try:
            doc = fitz.open(str(f))
            text_bearing = corpus.is_text_bearing(doc)
            per_page_after_text = _warnings_per_page(doc)
            doc.close()
        except Exception:
            errors += 1
            continue

        file_categories = set().union(*per_page)
        file_categories_after_text = set().union(*per_page_after_text)
        keys = ["all_files"] + (["text_bearing"] if text_bearing else [])
        for key in keys:
            b = buckets[key]
            b["files_measured"] += 1
            b["pages_checked"] += len(per_page)
            b["pages_with_warning"] += sum(1 for cats in per_page if cats)
            for cats in per_page:
                b["category_pages"].update(cats)
            if file_categories:
                b["files_with_warning"] += 1
                b["category_files"].update(file_categories)
                b["family_files"].update({family(c) for c in file_categories})
            if file_categories_after_text:
                b["files_with_warning_get_text_first"] += 1
                b["family_files_get_text_first"].update(
                    {family(c) for c in file_categories_after_text})
            r = b["rule"]
            for name in ("naive", "guarded"):
                outcomes = [j[name] for j in judged]
                r[name]["pages_excused"] += outcomes.count("excused")
                r[name]["pages_flagged_by_reason"].update(
                    o for o in outcomes if o != "excused")
                if any(o != "excused" for o in outcomes):
                    r[name]["files_still_flagged"] += 1
            if any(j["guarded"] != "excused" or j["annots"] for j in judged):
                r["guarded_files_still_flagged_if_those_were_not_excused"] += 1
            for j in judged:
                if j["annots"]:
                    r["warned_pages_with_annotations_or_widgets"] += 1
                    if j["guarded"] == "excused":
                        r["guarded_excused_pages_with_annotations_or_widgets"] += 1
                if j["naive"] != "excused":
                    continue
                if FILTER in j["families"]:
                    r["naive_excused_pages_with_filter_warning"] += 1
                    if j["zero"]:
                        r["naive_excused_pages_with_filter_warning_at_0_eq_0"] += 1
                if IMAGE in j["families"]:
                    r["naive_excused_pages_with_image_decoder_warning"] += 1
                if j["zero"]:
                    r["naive_excused_pages_at_0_eq_0"] += 1

    out: dict[str, Any] = {"encrypted_skipped": encrypted, "errors": errors}
    for key, b in buckets.items():
        n = b["files_measured"]
        warned = b["files_with_warning"]
        r = b["rule"]
        out[key] = {
            "files_measured": n,
            "pages_checked": b["pages_checked"],
            "pages_with_warning": b["pages_with_warning"],
            "page_rate": (b["pages_with_warning"] / b["pages_checked"]
                          if b["pages_checked"] else None),
            "files_with_warning": warned,
            "file_rate": warned / n if n else None,
            "family_files": dict(b["family_files"].most_common()),
            "top_categories_by_files_affected": dict(b["category_files"].most_common(15)),
            "top_categories_by_pages_affected": dict(b["category_pages"].most_common(15)),
            "get_text_first": {
                "files_with_warning": b["files_with_warning_get_text_first"],
                "file_rate": (b["files_with_warning_get_text_first"] / n) if n else None,
                "family_files": dict(b["family_files_get_text_first"].most_common()),
            },
            "rule_outcome_get_texttrace_first": {
                name: {
                    "files_still_flagged": r[name]["files_still_flagged"],
                    "of_warned_files": (r[name]["files_still_flagged"] / warned
                                        if warned else None),
                    "of_all_files_in_bucket": (r[name]["files_still_flagged"] / n
                                               if n else None),
                    "warned_pages_excused": r[name]["pages_excused"],
                    "warned_pages_flagged_by_reason":
                        dict(r[name]["pages_flagged_by_reason"].most_common()),
                }
                for name in ("naive", "guarded")
            } | {k: v for k, v in r.items() if k not in ("naive", "guarded")},
        }
    return out


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = measure(files)
    result["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(result, indent=2))
