"""Stable ids for the cells of COVERAGE.md.

A coverage cell is ``<row>.<column>``: where the content is stored (the
table's rows, ids in its first column) × how it is encoded (the columns:
``plain``, ``font``, ``pixels``, ``container``). Where one table cell
mixes outcomes, the ✗ part gets its own id with a suffix. Matching limits
are ``match.*``; known false alarms on clean documents are ``fp.*``.

The status is what COVERAGE.md claims today:
  read    — ✓ the content is searched
  flagged — ⚑ found but unreadable, reported (exit 2)
  gap     — ✗ a secret here can pass as clean
Cases are the evidence: every ``read`` and ``flagged`` cell must have a
case expecting it to work (tests/test_case_library.py enforces this);
``gap`` cells are documented by ``known_gap`` cases.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cell:
    status: str
    description: str


def _cells() -> dict[str, Cell]:
    read, flagged, gap = "read", "flagged", "gap"
    return {
        # Live page content
        "live.plain": Cell(read, "text drawn on a page"),
        "live.font": Cell(read, "font-coded text drawn on a page"),
        "live.pixels": Cell(read, "text as pixels on a page (OCR)"),
        "live.pixels-under-box": Cell(gap, "pixels under a box drawn over an image"),
        "live.after-stream-end": Cell(gap, "page content after a compressed stream's end marker"),
        # Off the visible page
        "off-page.plain": Cell(read, "text outside the crop/media box"),
        "off-page.font": Cell(read, "font-coded text outside the page, font has a Unicode map"),
        "off-page.font-no-unicode": Cell(gap, "font-coded text outside the page, no Unicode map"),
        "off-page.pixels": Cell(gap, "pixels outside the page"),
        # Switched-off optional content
        "oc-off.plain": Cell(read, "plain strings in a switched-off layer"),
        "oc-off.font": Cell(gap, "font-coded text in a switched-off layer"),
        "oc-off.pixels": Cell(gap, "pixels or outlines in a switched-off layer"),
        # Hidden annotation appearance
        "annot-appearance.plain": Cell(read, "plain strings in a hidden annotation's drawing"),
        "annot-appearance.font": Cell(gap, "font-coded text in a hidden annotation's drawing"),
        "annot-appearance.pixels": Cell(gap, "pixels in a hidden annotation's drawing"),
        # Referenced but never drawn
        "unused-resource.plain": Cell(read, "plain strings in an unused page resource"),
        "unused-resource.font": Cell(gap, "font-coded text in an unused page resource"),
        "unused-resource.pixels": Cell(gap, "pixels in an unused page resource"),
        # Orphaned objects
        "orphaned.plain": Cell(read, "plain strings in an object nothing references"),
        "orphaned.font": Cell(flagged, "font-coded text in an orphaned stream"),
        "orphaned.font-ordinary-codes": Cell(gap, "orphaned text whose font maps ordinary-looking codes"),
        "orphaned.pixels": Cell(flagged, "an orphaned image big enough to hold text"),
        "orphaned.pixels-small": Cell(gap, "an orphaned image below the size gate"),
        "orphaned.outlines": Cell(gap, "orphaned text converted to outlines (paths)"),
        "orphaned.container": Cell(flagged, "an orphaned zip/Office/PDF/gzip payload"),
        "orphaned.after-stream-end": Cell(gap, "orphaned data after a compressed stream's end marker"),
        "orphaned.mislabelled": Cell(gap, "orphaned plain text labelled as a font or an image"),
        # Superseded versions
        "superseded.plain": Cell(read, "plain strings an incremental update rewrote"),
        "superseded.font": Cell(flagged, "font-coded text an incremental update rewrote"),
        "superseded.pixels": Cell(flagged, "an image an incremental update replaced"),
        "superseded.container": Cell(flagged, "a container an incremental update replaced"),
        "superseded.revision-cap": Cell(flagged, "more earlier revisions than are scanned"),
        # Metadata
        "metadata.plain": Cell(read, "Info dictionary and XMP"),
        "metadata.pixels": Cell(gap, "XMP thumbnails"),
        "leftover-xmp.plain": Cell(read, "orphaned or superseded XMP"),
        "leftover-xmp.pixels": Cell(gap, "thumbnails in leftover XMP"),
        "thumbnail.pixels": Cell(gap, "page thumbnails (/Thumb)"),
        # Attachments
        "attachment.plain": Cell(read, "a listed or annotation attachment that is text"),
        "attachment.pixels": Cell(flagged, "an attached image"),
        "attachment.container": Cell(flagged, "an attached zip/Office/PDF/gzip"),
        "attachment.text-encoded-container": Cell(gap, "a container encoded as text (base64, data: URI)"),
        "embedded-other.plain": Cell(read, "PDF 2.0 /AF and rich media: known values via Binary"),
        "embedded-other.pattern": Cell(gap, "pattern rules on /AF and rich-media files"),
        "orphaned-attachment.plain": Cell(read, "an orphaned attachment that is text (manual review)"),
        "orphaned-attachment.pixels": Cell(flagged, "an orphaned attached image"),
        "orphaned-attachment.container": Cell(flagged, "an orphaned attached container"),
        # Other non-page content
        "annot-fields.plain": Cell(read, "annotation text, form fields, link targets, layer names"),
        "javascript.plain": Cell(read, "JavaScript in the catalog, named scripts, strings"),
        "javascript.pattern-in-streams": Cell(gap, "pattern rules on scripts stored as streams"),
        "private-data.plain": Cell(read, "/PieceInfo: live known values, leftover manual review"),
        "unindexed.plain": Cell(gap, "bytes after the final %%EOF, in comments, or free entries"),
        # Matching limits
        "match.page-break": Cell(read, "a value split across a page break, halves adjacent"),
        "match.page-break-furniture": Cell(gap, "a page-break split with a header/footer between the halves"),
        "match.line-wrap": Cell(read, "a value wrapped across lines (manual review)"),
        "match.columns": Cell(gap, "a value wrapped inside one column of a multi-column page"),
        "match.undashed-wrap": Cell(gap, "a pattern number without dashes, wrapped"),
        "match.extreme-coordinates": Cell(gap, "text at coordinates PyMuPDF does not return"),
        # Known false alarms on clean documents
        "fp.binary-value-collision": Cell(gap, "a value's digits occurring by chance in binary data"),
    }


CELLS: dict[str, Cell] = _cells()

# Cells COVERAGE.md claims as read or flagged that no case covers yet.
# This list may only shrink: tests fail when a case covers one of them
# (move it out) or when a claimed cell is missing and not listed here.
PENDING: frozenset[str] = frozenset()
