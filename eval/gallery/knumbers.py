"""Each known-gap case's docs/REDESIGN.md §8 "K-number".

An earlier version of this module derived the mapping automatically —
matching each case's story text against §8's table by word overlap. A
review found that fuzzy match untested and non-deterministic (it
happened to get all 35 mappings right, but nothing proved it always
would, and a coincidental word-overlap match failing silently is exactly
the kind of bug that's invisible until it isn't). ``CASE_TO_K`` below is
the same 35 mappings, now an explicit, hand-verified table instead —
small, stable (``UNDOCUMENTED_GAPS`` may only shrink, so this list of
ids doesn't grow casually either), and testable:
``tests/test_gallery.py`` checks it against ``documented_k_numbers``
(parsed straight from §8) so the two can never silently drift apart.

K1-K11's cases (``caselib/families/raw.py``) also write their K-number
into the story itself ("K7: plain text appended after the file's final
%%EOF."); that's the source this table was read from for those ids.
K12-K36's (``caselib/families/gaps.py``) don't carry that prefix, so
these were matched by hand against §8's table, cell id by cell id.

K8 has no case: §8 says it was fixed in the current tool before this
gallery existed.
"""

from __future__ import annotations

import re

_SECTION_START = "## 8. Known gaps found by this review"
_SECTION_END = "## 9. Risks"
_ROW_RE = re.compile(r"^\|\s*(K\d+)\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)

#: case id -> K-number, hand-verified against docs/REDESIGN.md §8.
CASE_TO_K: dict[str, int] = {
    "page.data-after-stream-end": 1,
    "leftover.data-after-stream-end": 2,
    "leftover.text-labelled-font": 3,
    "leftover.text-labelled-image": 4,
    "leftover.outlines": 5,
    "page.hidden-layer-outlines": 6,
    "file.after-final-eof": 7,
    # K8: fixed in the current tool (§8) — no case.
    "page.runs-off-right-edge-embedded-font": 9,
    "leftover.single-glyph-show-runs": 10,
    "leftover.untyped-text-operator-words": 11,
    "page.pixels-under-box": 12,
    "page.off-page-no-unicode-font": 13,
    "page.pixels-off-page": 14,
    "page.hidden-layer-font-coded": 15,
    "page.hidden-annotation-font-coded": 16,
    "page.hidden-annotation-pixels": 17,
    "page.unused-form-resource-font-coded": 18,
    "page.unused-form-resource-pixels": 19,
    "leftover.ordinary-looking-codes": 20,
    "leftover.small-image": 21,
    "document.xmp-thumbnail": 22,
    "leftover.xmp-thumbnail": 23,
    "page.thumb-image": 24,
    "attachment.base64-zip": 25,
    "document.af-pattern-rule": 26,
    "document.af-image": 27,
    "document.af-container": 28,
    "document.js-stream-pattern-rule": 29,
    "document.piece-info-pattern-rule": 30,
    "file.after-final-eof-font-coded": 31,
    "file.after-final-eof-pixels": 32,
    "file.after-final-eof-container": 33,
    "layout.two-column-wrap": 34,
    "page.extreme-coordinates": 35,
    "page.overprinted-embedded-font": 36,
}


def documented_k_numbers(redesign_text: str) -> set[int]:
    """Every K-number §8 documents as a still-open gap (excludes K8,
    which §8 itself says was fixed) — the source of truth
    ``CASE_TO_K``'s values are checked against."""
    try:
        start = redesign_text.index(_SECTION_START)
        end = redesign_text.index(_SECTION_END, start)
    except ValueError:
        return set()
    section = redesign_text[start:end]
    out: set[int] = set()
    for label, description in _ROW_RE.findall(section):
        if label == "ID":            # the header row's own two cells
            continue
        if re.search(r"\bfixed\b", description, re.IGNORECASE):
            continue
        out.add(int(label[1:]))
    return out
