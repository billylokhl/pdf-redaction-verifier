"""Stable ids for the cells of COVERAGE.md.

Coverage cells are ``<row>.<column>[.<qualifier>]``: the row id from the
table's first column (where the content is stored) × the column (``plain``,
``font``, ``pixels``, ``container``). A qualifier names a part of a table
cell with a different outcome — the ✗ parts of a mixed cell. Matching
limits are ``match.<slug>``; known false alarms on clean documents are
``false-alarm.<slug>``.

Status is what COVERAGE.md claims today:
  read        ✓ searched (tier: hard, or review = manual-review warning)
  flagged     ⚑ found but unreadable, reported (exit 2)
  gap         ✗ a secret here can pass as clean
  false-alarm a clean document wrongly flagged or accused
tests/test_case_library.py checks this file against COVERAGE.md's glyphs,
requires a leak case for every read and flagged cell, and a known-gap
case for every gap cell not listed in UNDOCUMENTED_GAPS.
"""

from __future__ import annotations

from dataclasses import dataclass

COLUMNS = ("plain", "font", "pixels", "container")
STATUSES = frozenset({"read", "flagged", "gap", "false-alarm"})


@dataclass(frozen=True)
class Cell:
    status: str
    description: str
    tier: str = "hard"          # for read cells: how a match is reported

    def __post_init__(self) -> None:
        if self.status not in STATUSES or self.tier not in ("hard", "review"):
            raise ValueError(f"bad cell {self}")


def _cells() -> dict[str, Cell]:
    read, flagged, gap = "read", "flagged", "gap"
    return {
        # live — drawn on a page
        "live.plain": Cell(read, "text drawn on a page"),
        "live.plain.after-stream-end": Cell(gap, "page content after a compressed stream's end marker"),
        "live.font": Cell(read, "font-coded text drawn on a page"),
        "live.font.overprinted": Cell(
            gap, "font-coded text drawn starting at the same point as other text"),
        "live.pixels": Cell(read, "text as pixels on a page (OCR)"),
        "live.pixels.under-box": Cell(gap, "pixels under a box drawn over an image"),
        "live.pixels.hidden-samples": Cell(
            read, "a value hidden in a drawn image's raw sample or codec bytes, not rendered as "
                  "glyphs (Binary layer, raw byte sweep)", tier="review"),
        "live.pixels.hidden-samples.pattern-rules": Cell(
            gap, "a pattern rule over a drawn image's hidden raw sample or codec bytes"),
        "live.pixels.extra-rows": Cell(
            gap, "pixel rows beyond a drawn image's declared height, never drawn or OCR'd"),
        "live.pixels.exif-thumbnail": Cell(
            gap, "a JPEG's EXIF/APP1 thumbnail, beyond the main decoded picture"),
        "live.pixels.char-stack": Cell(
            read, "a value one character per line in a scanned page, which Vision reads as one "
                  "vertical line: OCR unstacks it (a reconstruction)", tier="review"),
        "live.pixels.ocr-dropped-line": Cell(
            gap, "a line of pixels OCR reports reading but leaves out of its result"),
        "live.pixels.oversized-page": Cell(
            flagged, "pixels on a page whose OCR render is too large for Vision to read whole"),
        "live.pixels.oversized-image": Cell(
            gap, "a drawn image with far more detail than the page's OCR render resolves"),
        # off-page — outside the visible crop/media box
        "off-page.plain": Cell(read, "text outside the crop/media box"),
        "off-page.font": Cell(read, "font-coded text outside the page, font has a Unicode map"),
        "off-page.font.no-unicode": Cell(gap, "font-coded text outside the page, no Unicode map"),
        "off-page.font.straddling": Cell(read, "font-coded text running across the page edge"),
        "off-page.font.vertical": Cell(read, "font-coded text written vertically outside the page"),
        "off-page.pixels": Cell(gap, "pixels outside the page"),
        # oc-off — switched-off optional content
        "oc-off.plain": Cell(read, "plain strings in a switched-off layer"),
        "oc-off.font": Cell(gap, "font-coded text in a switched-off layer"),
        "oc-off.pixels": Cell(gap, "pixels or outlines in a switched-off layer"),
        # annot-appearance — a hidden annotation's drawing
        "annot-appearance.plain": Cell(read, "plain strings in a hidden annotation's drawing"),
        "annot-appearance.font": Cell(gap, "font-coded text in a hidden annotation's drawing"),
        "annot-appearance.pixels": Cell(gap, "pixels in a hidden annotation's drawing"),
        # unused-resource — referenced but never drawn
        "unused-resource.plain": Cell(read, "plain strings in an unused page resource"),
        "unused-resource.font": Cell(gap, "font-coded text in an unused page resource"),
        "unused-resource.pixels": Cell(gap, "pixels in an unused page resource"),
        # orphaned — stored, referenced by nothing
        "orphaned.plain": Cell(read, "plain strings in an object nothing references"),
        "orphaned.plain.after-stream-end": Cell(gap, "data after a compressed stream's end marker"),
        "orphaned.plain.mislabelled": Cell(gap, "plain text in a stream labelled as a font or image"),
        "orphaned.font": Cell(flagged, "font-coded text in an orphaned stream"),
        "orphaned.font.ordinary-codes": Cell(gap, "a font mapping ordinary-looking codes to other glyphs"),
        "orphaned.font.single-glyph-strings": Cell(gap, "glyph codes shown one character per Tj"),
        "orphaned.font.compact-syntax": Cell(flagged, "font-coded text written without spaces (BT/F1 …)"),
        "orphaned.pixels": Cell(flagged, "an orphaned image big enough to hold text"),
        "orphaned.pixels.small": Cell(gap, "an orphaned image below the size gate"),
        "orphaned.pixels.outlines": Cell(gap, "orphaned text converted to outlines (paths)"),
        "orphaned.container": Cell(flagged, "an orphaned zip/Office/PDF/gzip payload"),
        # superseded — rewritten by an incremental update
        "superseded.plain": Cell(read, "plain strings an incremental update rewrote"),
        "superseded.plain.revision-cap": Cell(flagged, "more earlier revisions than are scanned"),
        "superseded.font": Cell(flagged, "font-coded text an incremental update rewrote"),
        "superseded.font.compact-syntax": Cell(flagged, "rewritten font-coded text without spaces"),
        "superseded.pixels": Cell(flagged, "an image an incremental update replaced"),
        "superseded.container": Cell(flagged, "a container an incremental update replaced"),
        # metadata
        "metadata.plain": Cell(read, "Info dictionary and XMP"),
        "metadata.pixels": Cell(gap, "XMP thumbnails"),
        "leftover-xmp.plain": Cell(read, "orphaned or superseded XMP"),
        "leftover-xmp.pixels": Cell(gap, "thumbnails in leftover XMP"),
        "thumbnail.pixels": Cell(flagged, "page thumbnails (/Thumb)"),
        # trailer — the document trailer dictionary (not an object)
        "trailer.plain": Cell(read, "the trailer's /ID and other trailer strings"),
        # attachments
        "attachment.plain": Cell(read, "an attachment that is text", tier="review"),
        "attachment.pixels": Cell(flagged, "an attached image"),
        "attachment.container": Cell(flagged, "an attached zip/Office/PDF/gzip"),
        "attachment.container.text-encoded": Cell(gap, "a container encoded as text (base64, data: URI)"),
        "embedded-other.plain": Cell(read, "PDF 2.0 /AF, rich media: known values via Binary", tier="review"),
        "embedded-other.plain.pattern-rules": Cell(gap, "pattern rules on /AF and rich-media files"),
        "embedded-other.pixels": Cell(gap, "images in /AF and rich-media files"),
        "embedded-other.container": Cell(gap, "containers in /AF and rich-media files"),
        "orphaned-attachment.plain": Cell(read, "an orphaned text attachment", tier="review"),
        "orphaned-attachment.plain.operator-words": Cell(
            gap, "untyped text with three stand-alone content-operator words"),
        "orphaned-attachment.pixels": Cell(flagged, "an orphaned attached image"),
        "orphaned-attachment.container": Cell(flagged, "an orphaned attached container"),
        # other non-page content
        "annot-fields.plain": Cell(read, "annotation text, form fields, link targets, layer names"),
        "javascript.plain": Cell(read, "JavaScript in the catalog, named scripts, strings"),
        "javascript.plain.pattern-rules-in-streams": Cell(gap, "pattern rules on scripts stored as streams"),
        "private-data.plain": Cell(read, "/PieceInfo: live known values via Binary", tier="review"),
        "private-data.plain.pattern-rules": Cell(gap, "pattern rules on live /PieceInfo streams"),
        "unindexed.plain": Cell(gap, "plain text after the final %%EOF, in comments, or free entries"),
        "unindexed.font": Cell(gap, "font-coded text outside any indexed object"),
        "unindexed.pixels": Cell(gap, "images outside any indexed object"),
        "unindexed.container": Cell(gap, "containers outside any indexed object"),
        # matching limits
        # hostile -- input made to cost a reader time or memory (Phase 3d)
        "hostile.flate-bomb": Cell(flagged, "a value behind a decompression bomb"),
        "hostile.deep-nesting": Cell(flagged, "a value in a file with deeply nested objects"),
        "hostile.page-tree-cycle": Cell(flagged, "a value on a page in a cyclic page tree"),
        "hostile.huge-page-count": Cell(
            flagged, "a value in a file whose page tree claims an absurd page count"),
        "hostile.operator-flood": Cell(read, "a value among a flood of content operators"),
        "match.page-break": Cell(read, "a value split across a page break, halves adjacent"),
        "match.page-break-furniture": Cell(
            read, "a page-break split with a header/footer between", tier="review"),
        "match.page-break-off-page-half": Cell(
            read, "a page-break split one of whose halves lies wholly off the page",
            tier="review"),
        "match.line-wrap": Cell(read, "a value wrapped across lines", tier="review"),
        "match.letter-spaced-stack": Cell(
            gap, "a letter-spaced value read as one-character lines around other text"),
        "match.mixed-direction-split": Cell(
            gap, "a page-break split whose halves are written in different directions"),
        "match.tight-fine-print": Cell(
            gap, "a value wrapped over two fine-print lines closer than the line tolerance"),
        "match.columns": Cell(
            read, "a value wrapped inside one column of a multi-column page", tier="review"),
        "match.undashed-wrap": Cell(gap, "a pattern number without dashes, wrapped"),
        "match.pattern-split": Cell(
            gap, "a pattern-rule value split at a page break in form boxes, a stack, rotated "
                 "text, or with mixed separators: no reading joins it in a form the rule accepts"),
        "match.extreme-coordinates": Cell(gap, "text at coordinates PyMuPDF does not return"),
        # false alarms on clean documents
        "false-alarm.binary-value-collision": Cell(
            "false-alarm", "a value's digits occurring by chance in binary data"),
        "false-alarm.slug-beside-visible-line": Cell(
            "false-alarm", "an off-page slug a word space from visible text on its baseline, "
                           "read as one line onto the next page's first line"),
        "false-alarm.generated-id-digits": Cell(
            "false-alarm", "a digit run in a /ID written as hex text (manual review)"),
    }


CELLS: dict[str, Cell] = _cells()

# Gap and false-alarm cells with no case yet. May only shrink (0b-2).
# All pinned in eval/caselib/families/gaps.py (docs/REDESIGN.md K12-K35).
UNDOCUMENTED_GAPS: frozenset[str] = frozenset()


# Claimed (read/flagged) cells with no non-fitz caught leak case yet — a
# leak, no known_gap, writer != fitz (docs/REDESIGN.md §5's same-author-bias
# guard). May only shrink; empty today.
NONFITZ_PENDING: frozenset[str] = frozenset()


# A blind red-team case (caselib/redteam/, model.Case origin="redteam") may
# report finding a secret in a place this table has no row for. Rather than
# rejecting it outright, its case may use the placeholder namespace
# "new.<slug>" — but only once listed here, mapped to the GitHub issue that
# tracks giving it a real COVERAGE.md row. This is the whole gate: a
# placeholder id used anywhere else (a non-redteam case, or a redteam case
# not listed here) is rejected by model.Case at construction time. Once
# COVERAGE.md gains the row, add the real cell above, update the case to
# use it, and delete the entry here — never leave a graduated id listed.
NEW_CELL_ALLOWLIST: dict[str, int] = {}


def parts(cell_id: str) -> tuple[str, str | None, str | None]:
    """(row, column, qualifier) of a coverage cell; column is None for
    match.*, false-alarm.* and hostile.* ids."""
    row, _, rest = cell_id.partition(".")
    if row in ("match", "false-alarm", "hostile"):
        return row, None, rest
    column, _, qualifier = rest.partition(".")
    return row, column, qualifier or None


# Where a secret in each row is stored, as the report's storage class.
ROW_STORAGE: dict[str, str] = {
    "orphaned": "orphaned", "orphaned-attachment": "orphaned",
    "superseded": "superseded",
    **{row: "live" for row in ("live", "off-page", "oc-off", "annot-appearance",
                               "unused-resource", "metadata", "attachment",
                               "embedded-other", "annot-fields", "javascript",
                               "private-data", "trailer", "thumbnail")},
}
