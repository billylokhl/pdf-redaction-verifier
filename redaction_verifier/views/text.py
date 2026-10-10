"""redaction_verifier.views.text — layout-aware page text extraction.

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §6 ("Views are the existing page readings and OCR, moved
[not wrapped] in Phase 2"). Behaviour is byte-identical to the code this
replaced; verify.py re-exports every name here so existing imports,
`verify.X` references and the CLI keep working unchanged.

This module imports `fitz` (PyMuPDF) plainly, without its own guard: a
failure here propagates up through `redaction_verifier.views` and
`redaction_verifier`'s own import, where verify.py's single guarded
re-export block catches it (`except BaseException as exc:
_fatal_import("redaction_verifier", exc)`) and exits 2 — the same
fail-closed contract as a missing PyMuPDF, just caught one frame further
out now that the code that needs it lives in the package.
"""

from __future__ import annotations

from typing import Any

import pymupdf as fitz

# Floor for the visual-line clustering tolerance; the effective tolerance
# scales with the median glyph size on the page so large form-box digits
# with baseline jitter still cluster into one line.
MIN_LINE_TOLERANCE_PT: float = 4.0
# Ceiling: prevents a page dominated by large glyphs (watermarks,
# headers) from inflating the tolerance so much that fine-print lines
# get merged, scrambling their text and causing false negatives.
MAX_LINE_TOLERANCE_PT: float = 12.0

# How many of extract_visual_text's readings are genuine reading orders
# (hard-finding eligible); the rest are reconstructions (manual review).
TEXT_GENUINE_READINGS = 7
# How many of them feed the hard page seam (a value running from one
# page's last line onto the next page's first line). Not the readings
# across the visible edge: their last line may be off-page text.
TEXT_SEAM_READINGS = 4


def _reconstruct(
    glyphs: list[tuple[float, float, float, float, str]],
    cluster_axis: int,
) -> str:
    """Cluster glyphs into visual lines along one axis, sort along the other.

    glyphs: (x_center, y_center, width, height, char).
    cluster_axis 1 clusters by y (horizontal lines, read left-to-right);
    cluster_axis 0 clusters by x (vertical columns, read top-to-bottom).
    The clustering tolerance scales with the median glyph extent along the
    cluster axis, so 30pt form-box digits with 6pt baseline jitter still
    land in one line while 8pt fine print keeps its lines separate.
    """
    if not glyphs:
        return ""

    extents = sorted(g[3] if cluster_axis == 1 else g[2] for g in glyphs)
    median_extent = extents[len(extents) // 2]
    tolerance = max(MIN_LINE_TOLERANCE_PT, min(MAX_LINE_TOLERANCE_PT, 0.5 * median_extent))
    order_axis = 1 - cluster_axis

    ordered = sorted(glyphs, key=lambda g: g[cluster_axis])
    clusters: list[list[tuple[float, float, float, float, str]]] = []
    current = [ordered[0]]
    center = ordered[0][cluster_axis]
    for glyph in ordered[1:]:
        # cluster_axis (0 or 1) always indexes one of the two float
        # fields, never the trailing str field, but mypy can't tell a
        # variable tuple index from a literal one — see model.WarnList's
        # __iadd__ for the same tradeoff.
        if abs(glyph[cluster_axis] - center) <= tolerance:  # type: ignore[operator]
            current.append(glyph)
            # Running mean keeps the cluster stable against drift.
            center += (glyph[cluster_axis] - center) / len(current)  # type: ignore[operator]
        else:
            clusters.append(current)
            current = [glyph]
            center = glyph[cluster_axis]
    clusters.append(current)

    lines: list[str] = []
    for cluster in clusters:
        cluster.sort(key=lambda g: g[order_axis])
        lines.append(_join_cluster(cluster, order_axis))
    return "\n".join(lines)


def _join_cluster(
    cluster: list[tuple[float, float, float, float, str]], order_axis: int
) -> str:
    """Join one visual line's glyphs, breaking it at column gaps.

    Whitespace glyphs are dropped during extraction, so without this a
    table ROW ('123' | '45' | '6789' in three columns) would concatenate
    into '123456789' and read as one contiguous number. A gap is treated
    as a column break — emitted as a newline, which no single [-\\s.]
    separator slot can cross — when it is BOTH a large outlier against the
    other gaps on this line AND wider than a character. Both conditions
    matter: form boxes space every glyph widely but *uniformly* (no
    outlier, so the run stays intact), while ordinary word spaces are
    narrower than a character (so 'SSN 123 45 6789' stays intact too).
    """
    if len(cluster) < 2:
        return "".join(g[4] for g in cluster)

    extent_index = 2 if order_axis == 0 else 3
    gaps: list[float] = []
    for prev, nxt in zip(cluster, cluster[1:]):
        # order_axis/extent_index (0-3) always index one of the four
        # float fields, never the trailing str field — see the same
        # variable-tuple-index caveat in _reconstruct above.
        prev_end = prev[order_axis] + prev[extent_index] / 2.0  # type: ignore[operator]
        next_start = nxt[order_axis] - nxt[extent_index] / 2.0  # type: ignore[operator]
        gaps.append(max(0.0, next_start - prev_end))

    ordered_gaps = sorted(gaps)
    median_gap = ordered_gaps[len(ordered_gaps) // 2]
    extents = sorted(g[extent_index] for g in cluster)
    median_extent = extents[len(extents) // 2]
    threshold = max(3.0 * median_gap, 1.5 * median_extent)  # type: ignore[operator]

    out = [cluster[0][4]]
    for gap, glyph in zip(gaps, cluster[1:]):
        if gap > threshold:
            out.append("\n")
        out.append(glyph[4])
    return "".join(out)


def extract_visual_text(page: fitz.Page) -> list[str]:
    """Reconstruct page text in visual reading orders.

    Returns nine readings, or [] for a page with no glyphs:

    0. horizontal — visible, horizontally written text, lines top to bottom;
    1-2. rotated, top-to-bottom and bottom-to-top — visible text written
       vertically (a rotated matrix), read along its own direction;
    3. off-page — text placed outside the visible area (crop or media box),
       read horizontally on its own;
    4. across the visible edge, horizontal — every horizontally written
       glyph, visible or not, read as lines;
    5-6. across the visible edge, rotated — every vertically written
       glyph, visible or not, read along its own direction;
    7-8. every visible glyph read in vertical columns, top-to-bottom and
       bottom-to-top.

    Readings 0-6 are genuine: each reads text the way it was written, so
    the caller matches known values and pattern rules on each of their
    lines for hard findings. Off-page text is kept apart from the visible
    readings 0-2: merged into them, a slug or Bates stamp below the page
    became the page's "last line" and hid a value split across the page
    break. Readings 4-6 read across the visible edge, so a value that
    starts on the page and runs off it, or vertically written text off
    the page (which reading 3, horizontal only, scrambles), is read
    whole. They add to 0-3 and replace none of them. Across a page
    break, readings 0-3 (TEXT_SEAM_READINGS) feed the hard seam, so 0-2
    still give the visible text's true last line; 4-6 feed only the
    caller's manual-review net, because their last line may be an
    off-page slug, which does not run on into the next page. When a page
    has no off-page glyph of a direction, the matching across-the-edge
    reading repeats the visible one, so that net always joins the same
    reading on adjacent pages. A visible line and an off-page slug on one
    baseline meet in reading 4 only across a gap; one wider than one and
    a half characters and three times the line's median gap is a column
    break (_join_cluster), which no match crosses.

    Readings 7-8 are reconstructions: a column of an ordinary horizontal
    page stacks one glyph from each of many lines (a ledger's last
    digits, a numbered list's numbers), so they can assemble a value by
    accident — yet they are also the only reading of a value written one
    character per line. Callers treat them as manual-review only.
    """
    Glyph = tuple[float, float, float, float, str]
    flat: list[Glyph] = []
    rotated: list[Glyph] = []
    off_page: list[Glyph] = []      # in drawing order, both directions
    off_flat: list[Glyph] = []
    off_rotated: list[Glyph] = []
    # The visible area in the (unrotated) coordinates glyphs are reported
    # in, computed once per page: a per-glyph Point * Matrix doubled the
    # Text layer's run time. Its origin is (0, 0): glyphs are reported
    # with the top-left corner of the visible area (crop box ∩ media box,
    # scaled by /UserUnit) at the origin, whatever the rotation. Its size
    # is page.rect's, derotated. page.rect * page.derotation_matrix has
    # that size but not that offset on a rotated page (90, 180 or 270)
    # whose crop box reaches past its media box, whose /UserUnit is not 1,
    # or whose crop box ∩ media box is empty (MuPDF then falls back to a
    # 612x792 frame): it filed visible text as off-page and split a value
    # across the two readings. PyMuPDF normalizes a /Rotate that is not a
    # multiple of 90 to 0, so the derotation matrix is the identity; MuPDF
    # still turns such a page to the nearest multiple of 90 and reports
    # glyphs in the turned frame, which page.rect describes as it is.
    unrotated = page.rect * page.derotation_matrix
    vx0, vy0, vx1, vy1 = 0.0, 0.0, unrotated.width, unrotated.height
    # No clipping to the page: text outside the visible area is invisible
    # to a reader but still in the file, and still a leak.
    raw: dict[str, Any] = page.get_text(
        "rawdict", clip=fitz.INFINITE_RECT(),
        flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_MEDIABOX_CLIP)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            # dir is the writing direction: (1, 0) for ordinary text,
            # (0, -1) / (0, 1) for text rotated 90 / 270 degrees.
            written_vertically = abs(line.get("dir", (1.0, 0.0))[1]) > 0.1
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    c: str = char.get("c", "")
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = char["bbox"]
                    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                    glyph = (cx, cy, x1 - x0, y1 - y0, c)
                    visible = vx0 <= cx <= vx1 and vy0 <= cy <= vy1
                    if not visible:
                        off_page.append(glyph)
                    if written_vertically:
                        (rotated if visible else off_rotated).append(glyph)
                    else:
                        (flat if visible else off_flat).append(glyph)

    if not (flat or rotated or off_page):
        return []

    def columns(glyphs: list[Glyph]) -> tuple[str, str]:
        if not glyphs:
            return "", ""
        down = _reconstruct(glyphs, cluster_axis=0)
        return down, "\n".join(line[::-1] for line in down.split("\n"))

    horizontal = _reconstruct(flat, cluster_axis=1) if flat else ""
    rotated_down, rotated_up = columns(rotated)
    outside = _reconstruct(off_page, cluster_axis=1) if off_page else ""
    across = _reconstruct(flat + off_flat, cluster_axis=1) if off_flat else horizontal
    across_down, across_up = (columns(rotated + off_rotated) if off_rotated
                              else (rotated_down, rotated_up))
    all_down, all_up = columns(flat + rotated)
    return [horizontal, rotated_down, rotated_up, outside,
            across, across_down, across_up, all_down, all_up]
