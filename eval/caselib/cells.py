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
        "live.pixels": Cell(read, "text as pixels on a page (OCR)"),
        "live.pixels.under-box": Cell(gap, "pixels under a box drawn over an image"),
        # off-page — outside the visible crop/media box
        "off-page.plain": Cell(read, "text outside the crop/media box"),
        "off-page.font": Cell(read, "font-coded text outside the page, font has a Unicode map"),
        "off-page.font.no-unicode": Cell(gap, "font-coded text outside the page, no Unicode map"),
        "off-page.font.straddling": Cell(gap, "font-coded text running across the page edge"),
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
        "orphaned.font.compact-syntax": Cell(gap, "font-coded text written without spaces (BT/F1 …)"),
        "orphaned.pixels": Cell(flagged, "an orphaned image big enough to hold text"),
        "orphaned.pixels.small": Cell(gap, "an orphaned image below the size gate"),
        "orphaned.pixels.outlines": Cell(gap, "orphaned text converted to outlines (paths)"),
        "orphaned.container": Cell(flagged, "an orphaned zip/Office/PDF/gzip payload"),
        # superseded — rewritten by an incremental update
        "superseded.plain": Cell(read, "plain strings an incremental update rewrote"),
        "superseded.plain.revision-cap": Cell(flagged, "more earlier revisions than are scanned"),
        "superseded.font": Cell(flagged, "font-coded text an incremental update rewrote"),
        "superseded.font.compact-syntax": Cell(gap, "rewritten font-coded text without spaces"),
        "superseded.pixels": Cell(flagged, "an image an incremental update replaced"),
        "superseded.container": Cell(flagged, "a container an incremental update replaced"),
        # metadata
        "metadata.plain": Cell(read, "Info dictionary and XMP"),
        "metadata.pixels": Cell(gap, "XMP thumbnails"),
        "leftover-xmp.plain": Cell(read, "orphaned or superseded XMP"),
        "leftover-xmp.pixels": Cell(gap, "thumbnails in leftover XMP"),
        "thumbnail.pixels": Cell(gap, "page thumbnails (/Thumb)"),
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
        "match.page-break": Cell(read, "a value split across a page break, halves adjacent"),
        "match.page-break-furniture": Cell(gap, "a page-break split with a header/footer between"),
        "match.line-wrap": Cell(read, "a value wrapped across lines", tier="review"),
        "match.columns": Cell(gap, "a value wrapped inside one column of a multi-column page"),
        "match.undashed-wrap": Cell(gap, "a pattern number without dashes, wrapped"),
        "match.extreme-coordinates": Cell(gap, "text at coordinates PyMuPDF does not return"),
        # false alarms on clean documents
        "false-alarm.binary-value-collision": Cell(
            "false-alarm", "a value's digits occurring by chance in binary data"),
    }


CELLS: dict[str, Cell] = _cells()

# Gap and false-alarm cells with no case yet. May only shrink (0b-2).
# The binary collision reproduced only with macOS Arial's font program.
UNDOCUMENTED_GAPS: frozenset[str] = frozenset({
    "live.pixels.under-box",
    "off-page.font.no-unicode",
    "off-page.pixels",
    "oc-off.font",
    "annot-appearance.font",
    "annot-appearance.pixels",
    "unused-resource.font",
    "unused-resource.pixels",
    "orphaned.font.ordinary-codes",
    "orphaned.pixels.small",
    "metadata.pixels",
    "leftover-xmp.pixels",
    "thumbnail.pixels",
    "attachment.container.text-encoded",
    "embedded-other.plain.pattern-rules",
    "embedded-other.pixels",
    "embedded-other.container",
    "javascript.plain.pattern-rules-in-streams",
    "private-data.plain.pattern-rules",
    "unindexed.font",
    "unindexed.pixels",
    "unindexed.container",
    "match.columns",
    "match.extreme-coordinates",
    "false-alarm.binary-value-collision",
})


def parts(cell_id: str) -> tuple[str, str | None, str | None]:
    """(row, column, qualifier) of a coverage cell; column is None for
    match.* and false-alarm.* ids."""
    row, _, rest = cell_id.partition(".")
    if row in ("match", "false-alarm"):
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
                               "private-data")},
}
