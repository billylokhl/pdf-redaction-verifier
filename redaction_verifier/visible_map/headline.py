"""redaction_verifier.visible_map.headline — the map's one-line summary.

Exactly one row of ROWS applies to any run. The row is chosen from the
run's exit code, from J (the --json report was not written, so the exit
code is 2 where the scan alone would give 0) and from five flags:

- V: a solid or hatched box, or an OCR hit (something a page shows);
- I: the map is incomplete (an item it could not place, a page the scan
  never reached, the box cap, a switched-off layer, or a warning code of
  the page, document or scope level below);
- P: a possible match the scan left for review, listed with its page;
- L: content stored in live objects, which a page may draw;
- X: content shown on click, hover, print or open, in a panel, by a
  script, or outside the crop.

Nothing here reads a document or a secret: the flags come in as booleans
and the codes as the scan's own stable warning codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

ROWS: dict[int, str] = {
    1: "VISIBLE SECRETS FOUND",
    2: "MAP INCOMPLETE — a secret may be visible that is not drawn",
    3: "SHOWN ON CLICK / HOVER / PRINT / OPEN / IN PANELS / WHEN UNCROPPED — none drawn on the page",
    4: "LEAK, NOT VISIBLE — the file stores a secret no page shows",
    5: "Nothing visible found; uncertified (exit 2) for: {codes}",
    6: "Nothing visible found — every check ran; JSON report not written (exit 2)",
    7: "Nothing visible found — every check ran (exit 0)",
    8: "NOT CERTIFIED (exit {exit}): {codes}",
}
OCR_ONLY = " by OCR, not positioned"
J_REASON = "JSON report not written (exit 2)"
# Combinations allowed to fall through to row 8, each reviewed. Empty:
# every warning code sets I or P or is an "outside" code (row 5).
ROW_8_ALLOWED: frozenset[tuple[int, bool, frozenset[str]]] = frozenset()


@dataclass(frozen=True)
class Flags:
    V: bool = False
    I: bool = False
    P: bool = False
    L: bool = False
    X: bool = False
    # V comes only from OCR hits, which stage 1 cannot position.
    ocr_only: bool = False

    def names(self) -> frozenset[str]:
        return frozenset(n for n in "VIPLX" if getattr(self, n))


def row(flags: Flags, *, exit_code: int, json_unwritten: bool, codes: Sequence[str],
        outside_only: bool) -> int:
    """The single headline row for a run. *codes* are the run's warning
    codes; *outside_only* is true when every one of them is about content
    stored outside the document (orphaned, unreferenced, superseded)."""
    if flags.V:
        return 1
    if flags.I or flags.P or flags.L:
        return 2
    if flags.X:
        return 3
    if exit_code == 1:
        return 4
    if exit_code == 2 and codes and outside_only:
        return 5
    if exit_code == 2 and json_unwritten and not codes:
        return 6
    if exit_code == 0:
        return 7
    return 8


def text(flags: Flags, *, exit_code: int, json_unwritten: bool, codes: Sequence[str],
         outside_only: bool, detail: str = "", notices: int = 0) -> str:
    """The row's text; *detail* (row 2 only) gives its reasons and how
    many items show on click, hover, print or open. *notices* is the
    report's notice count (a number, never a value; the report lists one
    notice per place a match is found -- layer, rule and location -- not
    one per distinct value): on a "Nothing visible found" row
    of a clean run (6, 7) it says how many notices the verdict does not count."""
    n = row(flags, exit_code=exit_code, json_unwritten=json_unwritten, codes=codes,
            outside_only=outside_only)
    line = ROWS[n].format(codes=", ".join(sorted(set(codes))) or "no warning codes",
                          exit=exit_code)
    if n == 1 and flags.ocr_only:
        line += OCR_ONLY
    if n == 2 and detail:
        line += f" ({detail})"
    if n in (6, 7) and notices > 0:
        line += f"; {notices} notice{'s' if notices != 1 else ''} not counted in the verdict"
    if json_unwritten and J_REASON not in line:
        line += f"; {J_REASON}"
    return line


# ── Warning codes, by what they say about the map's completeness ─────────
# A ratchet test keeps every registered code in exactly one of these; a
# code missing from all of them counts as document-level (fail closed).
# Page coverage: a page the scan did not fully read (PAGE_FAILED), or one
# carrying a thumbnail image it never reads (PAGE_THUMBNAIL, #133). The
# map names the code and its pages as an INCOMPLETE reason and shows
# nothing of the content; storage does not change the level.
PAGE_CODES = frozenset({"PAGE_FAILED", "PAGE_THUMBNAIL"})
DOCUMENT_CODES = frozenset({
    "LAYER_CRASHED", "SKIPPED_FAIL_FAST", "OCR_UNAVAILABLE", "EMPTY_DOCUMENT", "XMP_UNREADABLE",
    "TOOL_MISSING", "TOOL_START_FAILED", "TOOL_TIMEOUT", "TOOL_EXIT_NONZERO", "TOOL_NO_OUTPUT",
    "TOOL_OUTPUT_MALFORMED", "HIDDEN_ITEM_FAILED", "ATTACHMENT_NOT_TEXT", "ATTACHMENT_TOO_LARGE",
    "ATTACHMENT_EMPTY", "PAYLOAD_TRUNCATED",
})
# The Objects layer's coverage codes: document-level when what they could
# not read is live (or not said), "outside" when it is stored outside the
# document.
OBJECT_COVERAGE_CODES = frozenset({
    "XREF_UNREADABLE", "OBJECT_UNREADABLE", "UNTERMINATED_STRING", "LEFTOVER_UNDECODABLE_TEXT",
    "LEFTOVER_IMAGE", "LEFTOVER_CONTAINER", "REVISION_SCAN_FAILED", "REVISION_UNREADABLE",
    "REVISION_CAP", "TRAILER_UNREADABLE",
})
REVIEW_CODES = frozenset({
    "REVIEW_CROSS_LINE", "REVIEW_CROSS_PAGE", "REVIEW_FUSED_PATTERN", "REVIEW_METADATA_RAW",
    "REVIEW_HIDDEN_TEXT",
})
# Review matches in stored content: "live content, may be visible" (L)
# when live, "outside" otherwise.
STORED_REVIEW_CODES = frozenset({"REVIEW_BINARY", "REVIEW_OBJECT_TEXT", "REVIEW_ADJACENT_LITERALS"})
SCOPE_CODES = frozenset({
    "RULES_UNKNOWN_KEY", "RULES_UNQUOTED_VALUE", "RULES_TRUNCATED_PATTERN",
    "RULES_CAPTURING_GROUP", "SCOPE_PARTIAL_ENTITY", "SCOPE_UNVERIFIABLE",
})
OUTSIDE_STORAGE = frozenset({"orphaned", "unreferenced", "superseded"})


def code_level(code: str, storage: str | None) -> str:
    """"page", "document", "scope", "review", "live" or "outside"."""
    if code in PAGE_CODES:
        return "page"
    if code in SCOPE_CODES:
        return "scope"
    if code in REVIEW_CODES:
        return "review"
    if code in STORED_REVIEW_CODES:
        return "outside" if storage in OUTSIDE_STORAGE else "live"
    if code in OBJECT_COVERAGE_CODES:
        return "outside" if storage in OUTSIDE_STORAGE else "document"
    return "document"
