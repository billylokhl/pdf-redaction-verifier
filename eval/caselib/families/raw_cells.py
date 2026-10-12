"""Non-fitz evidence: every claimed cell (docs/REDESIGN.md §5) needs at
least one leak case whose bytes were never serialised by PyMuPDF — the
library the tool itself reads with, so a fitz-only corpus would hide any
place the two disagree. These cases rebuild, by hand, the structures a
few families elsewhere only ever produce through ``fitz.Document.save``:
a font-coded live page (a custom ``/Differences`` encoding needs no
embedded program), listed attachments (a real ``/EmbeddedFiles`` name
tree), and superseded content (``rawpdf.incremental_update``, this
review's second hand-assembled writer capability, alongside object
streams and cross-reference streams for the couple of cases that use
them)."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..model import SSN, KnownGap, case, expect
from ..pdfkit import FILLER, compressed, png_of, zipbytes
from ..rawpdf import (CATALOG, HELVETICA, build, build_objstm, incremental_update, one_page,
                      page, stream, text)

SECRET = f"SSN {SSN}"
# Pins the Text layer specifically: page.raw-font-coded's OCR finding alone
# would let its font decoding break unnoticed, matching families/live.py's
# fitz siblings (e.g. page.visible-embedded-font).
LIVE_SSN = expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),))
ORPHAN_SSN = expect(1, findings=(("SSN", "orphaned"),))
SUPERSEDED_SSN = expect(1, findings=(("SSN", "superseded"),))
UNDECODABLE_ORPHAN = expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),))
UNDECODABLE_SUPERSEDED = expect(2, warnings=(("LEFTOVER_UNDECODABLE_TEXT", "superseded"),))
NOT_TEXT = expect(2, warnings=(("ATTACHMENT_NOT_TEXT", "live"),))
PAGE = stream(b"", text("Quarterly report"))
# "SSN 123-45-6789" as glyph ids (NUL-interleaved: unreadable as latin-1
# text), the same fixture families/raw.py's leftover cases use — the
# Objects layer judges leftover streams by their raw codes, not through a
# font, so no real font object is needed for it to call these undecodable.
GLYPHS = b"<0034003400310003001400150016000E0017001800190019001A001B001C>"


def leak(id: str, cells, story: str, expected=LIVE_SSN, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story,
                writer="raw", **kw)


def _write(path: Path, objects: dict[int, bytes], **kw) -> None:
    path.write_bytes(build(objects, **kw))


# ── Font-coded text with no embedded program ────────────────────────────
# A live page is read through the font's Unicode map (docs/COVERAGE.md
# footnote 1 names a custom /Differences encoding as one kind, alongside
# Identity-H). A standard font needs no embedded program for that map:
# /Differences alone, resolved through the Adobe Glyph List, is enough —
# no fitz.Font buffer required, so the whole file is hand-assembled.
_GLYPH_NAMES = {" ": "space", "-": "hyphen", "0": "zero", "1": "one", "2": "two",
                "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
                "8": "eight", "9": "nine"}


def _glyph_name(c: str) -> str:
    if c in _GLYPH_NAMES:
        return _GLYPH_NAMES[c]
    if c.isalpha():
        return c                # the AGL names 'A'-'Z' and 'a'-'z' after themselves
    raise ValueError(f"no standard glyph name for {c!r}")


def differences_font(chars: str) -> bytes:
    """A non-embedded Helvetica whose codes 1..N are remapped, in order, to
    *chars* via /Differences — font-coded text with no font program."""
    names = b" ".join(b"/" + _glyph_name(c).encode() for c in chars)
    return (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding "
            b"<< /Type /Encoding /Differences [1 " + names + b"] >> >>")


def coded(chars: str) -> bytes:
    """*chars* shown as the sequential codes differences_font(chars) maps."""
    return b"<" + "".join("%02X" % (i + 1) for i in range(len(chars))).encode() + b">"


@leak("page.raw-visible", "live.plain", "The SSN drawn directly on the page.",
      mistake="Nobody redacted it.", recovery="Read the page.")
def visible(path: Path) -> None:
    _write(path, one_page(stream(b"", text(SECRET))))


@leak("page.raw-font-coded", "live.font",
      "The SSN on the page in a font with a custom /Differences encoding: the codes "
      "are not the characters shown, but the font's own map recovers them.",
      mistake="Nobody redacted it.", recovery="Read the page.")
def font_coded(path: Path) -> None:
    content = stream(b"", b"BT /F1 12 Tf 72 700 Td " + coded(SECRET) + b" Tj ET")
    _write(path, one_page(content, o6=differences_font(SECRET)))


@leak("page.raw-pixels", "live.pixels",
      "A scanned-looking strip of pixels showing the SSN, drawn directly on the page "
      "as an inline image.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "OCR"),)),
      requires=("ocr",), mistake="Redacting the text layer and forgetting the scan.",
      recovery="Look at the page.")
def pixels(path: Path) -> None:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(SECRET)))
    w, h = gray.width, gray.height
    data = (b"q %d 0 0 %d 72 400 cm BI /W %d/H %d/BPC 8/CS/G ID " % (w, h, w, h)
            + gray.samples + b"\nEI Q")
    _write(path, one_page(stream(b"", data)))


@leak("page.raw-above-page", "off-page.plain",
      "The SSN drawn above the top edge of the page, where no viewer shows it.",
      mistake="Nobody redacted it; the value simply sits off the visible page.",
      recovery="Copy all text, or enlarge the page box.")
def above_page(path: Path) -> None:
    _write(path, one_page(stream(b"", text(SECRET, -40))))


@leak("page.raw-off-page-font-coded", "off-page.font",
      "The SSN off the right edge of the page, in a font with a custom /Differences "
      "encoding — the map still resolves it, wherever it is drawn.",
      mistake="Nobody redacted it; the value simply sits off the visible page.",
      recovery="Enlarge the page box and read the font's Encoding /Differences.")
def off_page_font_coded(path: Path) -> None:
    content = stream(b"", b"BT /F1 12 Tf 700 100 Td " + coded(SECRET) + b" Tj ET")
    _write(path, one_page(content, o6=differences_font(SECRET)))


# ── Font-coded text across the visible edge, or written vertically off it ─
# The visible area is the crop box ∩ the media box. The Text layer reads
# every glyph of one writing direction together, visible or not, so a
# value that starts on the page and runs off it, or that is written
# vertically outside it, is read whole. Font codes keep the Objects
# layer's literal scan from reading the value, and OCR sees at most the
# visible half, so the Text layer is the one that must find it. Each
# layout at /Rotate 0 and 270: the visible area is computed in the
# unrotated frame glyphs are reported in.
EDGE_MISTAKE = "Nobody redacted it; the value simply runs past the visible margin."
OFF_PAGE_MISTAKE = "Nobody redacted it; the value simply sits off the visible page."


def _edge_page(path: Path, matrix: bytes, crop: tuple[int, int, int, int] | None,
               rotate: int) -> None:
    """A page with a line of plain text and SECRET in font codes drawn
    with text matrix *matrix*, under an optional crop box and /Rotate."""
    content = stream(b"", text("Quarterly report", 400) + b"\nBT /F2 12 Tf " + matrix
                     + b" Tm " + coded(SECRET) + b" Tj ET")
    extra = b"/Rotate %d" % rotate
    if crop is not None:
        extra += b" /CropBox [%d %d %d %d]" % crop
    objects = one_page(content, o7=differences_font(SECRET))
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>", extra=extra)
    _write(path, objects)


def _edge_cases() -> None:
    inside = (0, 0, 500, 792)        # a crop box inside the 612x792 media box
    overhanging = (0, 0, 812, 792)   # one reaching 200 pt past its right edge
    for rotate, suffix in ((0, ""), (270, "-rotated")):
        turned = " The page is turned with /Rotate 270." if rotate else ""

        @leak(f"page.raw-font-coded-runs-past-crop-box{suffix}", "off-page.font.straddling",
              "The SSN in font codes, starting inside the crop box and running past its "
              "right edge: the end of the value is cropped away, the rest is on the page."
              + turned,
              mistake="Cropping the page so that it cuts through a value.",
              recovery="Remove the crop box and read the font's Encoding /Differences.")
        def past_crop(path: Path, rotate: int = rotate) -> None:
            _edge_page(path, b"1 0 0 1 450 300", inside, rotate)

        @leak(f"page.raw-font-coded-runs-off-overhanging-crop{suffix}",
              "off-page.font.straddling",
              "The SSN in font codes running off the right edge of the media box, under a "
              "crop box that reaches past that edge: the visible area ends at the media "
              "box." + turned,
              mistake=EDGE_MISTAKE,
              recovery="Enlarge the media box and read the font's Encoding /Differences.")
        def off_overhanging_crop(path: Path, rotate: int = rotate) -> None:
            _edge_page(path, b"1 0 0 1 560 300", overhanging, rotate)

        for direction, matrix in (("up", b"0 1 -1 0 700 300"), ("down", b"0 -1 1 0 700 500")):
            @leak(f"page.raw-font-coded-vertical-off-page-{direction}{suffix}",
                  "off-page.font.vertical",
                  f"The SSN in font codes written vertically, reading {direction}ward, right "
                  "of the page edge." + turned,
                  mistake=OFF_PAGE_MISTAKE,
                  recovery="Enlarge the page box, turn the view, and read the font's "
                           "Encoding /Differences.")
            def vertical_off_page(path: Path, matrix: bytes = matrix, rotate: int = rotate) -> None:
                _edge_page(path, matrix, None, rotate)


_edge_cases()


# ── A value split at a page break, its first half at the visible edge ───
# Page 1's last line holds 'SSN 123-45' in font codes, running past the
# media box's right edge (or wholly past it); page 2 opens with '6789
# continues'. Read across the edge, a line holding a visible glyph is the
# visible text's last line, so the split is a hard page seam; a first half
# wholly off the page is read only off the page, and its join with the
# next page's first line is manual review (a slug below a page is not the
# start of the next page's text). With /Rotate 270, page 1 also has a crop
# box reaching 200 pt past its media box's right edge.
BREAK_LAYOUTS = ("straddling", "straddling-slug", "off-page", "off-page-slug",
                 "off-page-both-turned")
_BREAK_CHARS = "".join(dict.fromkeys("SSN 123-45" + "6789 continues"))


def _break_coded(text: str) -> bytes:
    return b"<" + b"".join(b"%02X" % (_BREAK_CHARS.index(c) + 1) for c in text) + b">"


def page_break_at_edge(path: Path, layout: str, rotate: int) -> None:
    """Two pages: *layout* is one of BREAK_LAYOUTS ('-slug': page 2 also
    has a slug above its top edge; 'off-page-both-turned': page 2 is
    turned like page 1, with a slug past its right edge)."""
    x = 560 if layout.startswith("straddling") else 650
    first = (text("Quarterly report", 700)
             + b"\nBT /F2 12 Tf %d 20 Td " % x + _break_coded("SSN 123-45") + b" Tj ET")
    second_at = b"300 770" if layout == "off-page-both-turned" else b"72 770"
    second = b"BT /F2 12 Tf " + second_at + b" Td " + _break_coded("6789 continues") + b" Tj ET"
    if layout.endswith("-slug"):
        second = text("Job 0002 printed", 810) + b"\n" + second
    elif layout == "off-page-both-turned":
        second = b"BT /F1 12 Tf 650 780 Td (Job 0002 printed) Tj ET\n" + second
    turned = b"/Rotate %d /CropBox [0 0 812 792]" % rotate if rotate else b"/Rotate 0"
    fonts = b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>"
    objects = {1: CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
               3: page(resources=fonts, extra=turned), 4: stream(b"", first),
               6: HELVETICA, 7: differences_font(_BREAK_CHARS),
               8: page(b"9 0 R", fonts, turned if layout == "off-page-both-turned" else b""),
               9: stream(b"", second)}
    _write(path, objects)


def _break_cases() -> None:
    for rotate, suffix in ((0, ""), (270, "-rotated")):
        turned = " Page 1 is turned with /Rotate 270." if rotate else ""
        for layout, id_part, slug in (
                ("straddling", "", ""),
                ("straddling-slug", "-with-slug", " Page 2 has a slug above its top edge.")):
            @leak(f"page.raw-font-coded-split-at-break-past-edge{id_part}{suffix}",
                  ("off-page.font.straddling", "match.page-break"),
                  "The SSN in font codes split across a page break: its first half, page 1's "
                  "last line, runs past the media box's right edge; its second half opens "
                  "page 2." + slug + turned,
                  mistake="Nobody redacted it; the value runs past the margin and over the "
                          "page break.",
                  recovery="Enlarge the page box, read both pages' text through the font's "
                           "Encoding /Differences, and join the halves.")
            def split_past_edge(path: Path, layout: str = layout, rotate: int = rotate) -> None:
                page_break_at_edge(path, layout, rotate)


_break_cases()


def _split_off_page_cases() -> None:
    for rotate, suffix in ((270, ""), (180, "-180")):
        @leak(f"page.raw-font-coded-split-at-break-off-page-rotated{suffix}",
              "match.page-break-off-page-half",
              "The SSN in font codes split across a page break: its first half lies wholly "
              f"past the media box's right edge on page 1, turned with /Rotate {rotate} "
              "under a crop box reaching past that edge; its second half opens page 2. A "
              "half wholly off the page reads like a slug, which does not run on into the "
              "next page, so the join is manual review rather than a finding.",
              expected=expect(2, warnings=(("REVIEW_CROSS_PAGE", "live"),)),
              mistake="Nobody redacted it; half the value sits past the margin, the rest "
                      "over the page break.",
              recovery="Enlarge the page box, read both pages' text through the font's "
                       "Encoding /Differences, and join the halves.")
        def split_off_page_rotated(path: Path, rotate: int = rotate) -> None:
            page_break_at_edge(path, "off-page", rotate)


_split_off_page_cases()


def _more_off_page_half_cases() -> None:
    # The other off-page-half layouts, at /Rotate 0: a slug above page 2,
    # and both pages turned with a slug past page 2's right edge.
    for layout in ("off-page-slug", "off-page-both-turned"):
        turned = layout == "off-page-both-turned"
        @leak(f"page.raw-font-coded-split-at-break-{layout}",
              "match.page-break-off-page-half",
              "The SSN in font codes split across a page break: its first half lies wholly "
              "past the media box's right edge on page 1; its second half opens page 2"
              + (", which is turned like page 1 with a slug past its right edge."
                 if turned else ", below a slug above its top edge.")
              + " A half wholly off the page reads like a slug, so the join is manual "
                "review rather than a finding.",
              expected=expect(2, warnings=(("REVIEW_CROSS_PAGE", "live"),)),
              mistake="Nobody redacted it; half the value sits past the margin, the rest "
                      "over the page break.",
              recovery="Enlarge the page box, read both pages' text through the font's "
                       "Encoding /Differences, and join the halves.")
        def split(path: Path, layout: str = layout, rotate: int = 270 if turned else 0) -> None:
            page_break_at_edge(path, layout, rotate)


_more_off_page_half_cases()


# ── A value carried onto text wholly off the page ───────────────────────
# Page 1's last visible line ends 'SSN 123-45-'; the rest, '6789
# continues', is wholly off the page: above page 2's top edge
# ('second-half-above'), or on a line just below page 1's bottom edge
# ('wrap-below'). Reading 9 reads every horizontal glyph, visible or not,
# so the join is reviewed (owner decision, 2026-10-11): never a finding,
# since off-page text does not run on from the visible text, and never
# silent.
OFF_PAGE_CARRY = ("second-half-above", "wrap-below")


def value_carried_off_page(path: Path, layout: str, rotate: int) -> None:
    """*layout* is one of OFF_PAGE_CARRY; with /Rotate 270 every page
    also has a crop box reaching 200 pt past its media box's right edge."""
    first_y, second_y = (40, 810) if layout == "second-half-above" else (20, -10)
    first = (text("Quarterly report", 700) + b"\nBT /F2 12 Tf 72 %d Td " % first_y
             + _break_coded("SSN 123-45-") + b" Tj ET")
    second = b"BT /F2 12 Tf 72 %d Td " % second_y + _break_coded("6789 continues") + b" Tj ET"
    turned = b"/Rotate %d /CropBox [0 0 812 792]" % rotate if rotate else b""
    fonts = b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>"
    if layout == "wrap-below":
        objects = one_page(stream(b"", first + b"\n" + second), o7=differences_font(_BREAK_CHARS))
        objects[3] = page(resources=fonts, extra=turned)
    else:
        objects = {1: CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
                   3: page(resources=fonts, extra=turned), 4: stream(b"", first),
                   6: HELVETICA, 7: differences_font(_BREAK_CHARS),
                   8: page(b"9 0 R", fonts, turned),
                   9: stream(b"", second + b"\n" + text("Quarterly report", 700))}
    _write(path, objects)


def _carry_cases() -> None:
    for layout, cell, code, where in (
            ("second-half-above", "match.page-break-off-page-half", "REVIEW_CROSS_PAGE",
             "its second half, '6789 continues', lies wholly above page 2's top edge"),
            ("wrap-below", "match.line-wrap", "REVIEW_CROSS_LINE",
             "its second half, '6789 continues', is on a line wholly below the page's "
             "bottom edge")):
        for rotate, suffix in ((0, ""), (270, "-rotated")):
            @leak(f"page.raw-font-coded-carried-off-page-{layout}{suffix}", (cell, "off-page.font"),
                  "The SSN in font codes: page 1's last visible line ends 'SSN 123-45-', and "
                  + where + ". Off-page text does not run on from the visible text, so the "
                  "join is manual review rather than a finding."
                  + (" The pages are turned with /Rotate 270." if rotate else ""),
                  expected=expect(2, warnings=((code, "live"),)),
                  mistake="Nobody redacted it; half the value sits past the page edge.",
                  recovery="Enlarge the page box, read the text through the font's Encoding "
                           "/Differences, and join the halves.")
            def carried(path: Path, layout: str = layout, rotate: int = rotate) -> None:
                value_carried_off_page(path, layout, rotate)


_carry_cases()


# ── A value scattered around other text (owner decision, 2026-10-11) ────
# The parts of a value lie a few lines apart, with other text between
# them, or in reverse order: across a page break with page 2's header
# between ('furniture-at-break'), within a page with a footer line
# between ('furniture-in-page'), or with the second half on the line
# above the first ('reversed'). No reading holds the value whole; the
# review net assembles it from two or three pieces of nearby lines
# (SecretMatcher.scattered), so it is reviewed, never a finding.
SCATTERED = ("furniture-at-break", "furniture-in-page", "reversed")


def value_scattered(path: Path, layout: str, rotate: int) -> None:
    first_half, second_half = _break_coded("SSN 123-45-"), _break_coded("6789 continues")
    turned = b"/Rotate %d /CropBox [0 0 812 792]" % rotate if rotate else b""
    fonts = b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>"
    if layout == "furniture-at-break":
        first = text("Quarterly report", 700) + b"\nBT /F2 12 Tf 72 40 Td " + first_half + b" Tj ET"
        second = (text("Quarterly report, page 2", 770) + b"\nBT /F2 12 Tf 72 740 Td "
                  + second_half + b" Tj ET")
        objects = {1: CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
                   3: page(resources=fonts, extra=turned), 4: stream(b"", first),
                   6: HELVETICA, 7: differences_font(_BREAK_CHARS),
                   8: page(b"9 0 R", fonts, turned), 9: stream(b"", second)}
    else:
        top, bottom = ((first_half, second_half) if layout == "furniture-in-page"
                       else (second_half, first_half))
        between = text("Internal use only", 385) + b"\n" if layout == "furniture-in-page" else b""
        content = (text("Quarterly report", 700) + b"\nBT /F2 12 Tf 72 400 Td " + top
                   + b" Tj ET\n" + between + b"BT /F2 12 Tf 72 370 Td " + bottom + b" Tj ET")
        objects = one_page(stream(b"", content), o7=differences_font(_BREAK_CHARS))
        objects[3] = page(resources=fonts, extra=turned)
    _write(path, objects)


def _scattered_cases() -> None:
    for layout in SCATTERED:
        at_break = layout == "furniture-at-break"
        story = {
            "furniture-at-break": "page 1's last line ends 'SSN 123-45-' and page 2 opens with "
                                  "its header, then '6789 continues'",
            "furniture-in-page": "a line ends 'SSN 123-45-', a footer line 'Internal use "
                                 "only' follows, then '6789 continues'",
            "reversed": "'6789 continues' sits on the line above 'SSN 123-45-'",
        }[layout]
        for rotate, suffix in ((0, ""), (270, "-rotated")):
            @leak(f"page.raw-font-coded-scattered-{layout}{suffix}",
                  "match.page-break-furniture" if at_break else "match.line-wrap",
                  "The SSN in font codes: " + story + ". No reading holds it whole; the "
                  "review net assembles it from nearby lines, so it is manual review."
                  + (" The pages are turned with /Rotate 270." if rotate else ""),
                  expected=expect(2, warnings=(
                      ("REVIEW_CROSS_PAGE" if at_break else "REVIEW_CROSS_LINE", "live"),)),
                  mistake="Nobody redacted it; the value is split around other text.",
                  recovery="Read the text through the font's Encoding /Differences and join "
                           "the parts.")
            def scattered(path: Path, layout: str = layout, rotate: int = rotate) -> None:
                value_scattered(path, layout, rotate)


_scattered_cases()


@leak("layout.raw-font-coded-two-column-wrap", "match.columns",
      "The SSN in font codes wrapped across two lines of a two-column page's left column, "
      "with a line of the right column between them at the same height: joining the "
      "page's lines top to bottom puts that line between the halves, so only the review "
      "net, which assembles a value from nearby lines, finds it.",
      expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
      mistake="Nobody redacted the value; it wraps inside one column of a two-column layout.",
      recovery="Read the page column by column, through the font's Encoding /Differences.")
def two_column_wrap(path: Path) -> None:
    content = (text("Left column heading", 692) + b"\nBT /F2 12 Tf 72 662 Td "
               + _break_coded("SSN 123-45-") + b" Tj ET\n"
               + b"BT /F1 12 Tf 320 644 Td (Right column: unrelated filler text.) Tj ET\n"
               + b"BT /F2 12 Tf 72 626 Td " + _break_coded("6789 continues") + b" Tj ET")
    objects = one_page(stream(b"", content), o7=differences_font(_BREAK_CHARS))
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>")
    _write(path, objects)


# ── Vertical text split at a page break, one half off the page ──────────
# Owner decisions of 2026-10-10 and 2026-10-11: like the horizontal
# off-page-half split, vertically written text whose second half lies
# wholly off the next page is manual review, not a finding: the vertical
# readings across the edge feed no hard seam (their last column may be an
# off-page slug). /Rotate 270 under a crop box reaching 200 pt past the
# media box's right edge, where the old visible area put that half on
# the page and made it a finding by accident.

@leak("page.raw-font-coded-vertical-split-at-break-off-page-rotated",
      "match.page-break-off-page-half",
      "The SSN in font codes written vertically, top to bottom: 'SSN 123-45-' on page 1 "
      "and '6789 continues' on page 2, wholly past the media box's right edge, on pages "
      "turned with /Rotate 270 under a crop box reaching past that edge. A half wholly "
      "off the page is manual review rather than a finding.",
      expected=expect(2, warnings=(("REVIEW_CROSS_PAGE", "live"),)),
      mistake="Nobody redacted it; half the value sits past the margin, the rest over the "
              "page break.",
      recovery="Enlarge the page box, turn the view, read both pages' text through the "
               "font's Encoding /Differences, and join the halves.")
def vertical_split_off_page(path: Path) -> None:
    turned = b"/Rotate 270 /CropBox [0 0 812 792]"
    fonts = b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>"
    first = (text("Quarterly report", 760) + b"\nBT /F2 12 Tf 0 -1 1 0 300 600 Tm "
             + _break_coded("SSN 123-45-") + b" Tj ET")
    second = (text("Quarterly report", 760) + b"\nBT /F2 12 Tf 0 -1 1 0 700 700 Tm "
              + _break_coded("6789 continues") + b" Tj ET")
    objects = {1: CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
               3: page(resources=fonts, extra=turned), 4: stream(b"", first),
               6: HELVETICA, 7: differences_font(_BREAK_CHARS),
               8: page(b"9 0 R", fonts, turned), 9: stream(b"", second)}
    _write(path, objects)


# ── Clean controls: a table row beside off-page text on its baseline ────
# 'Item' | '123' | '45' | '6789' in cells inside a crop box ending at
# x=400, and on the same baseline past it either wide single-digit cells
# or a letter-spaced slug (25 pt character spacing). Read across the edge,
# the row's column breaks are set by its visible glyphs alone, so the
# off-page text's wide gaps cannot fuse the visible cells into a value.
TABLE_ROW_OFF_PAGE = ("wide-cells", "letterspaced-slug")


_ROW_CHARS = "".join(dict.fromkeys("Item0123456789JOBREF-"))


def _row_coded(text_: str) -> bytes:
    return b"<" + b"".join(b"%02X" % (_ROW_CHARS.index(c) + 1) for c in text_) + b">"


def table_row_beside_off_page(path: Path, layout: str, rotate: int) -> None:
    """Font-coded, so the Objects layer's literal scan, which joins the
    strings of one content stream, cannot read the cells as one value."""
    cells = b"".join(b"BT /F2 12 Tf %d 500 Td %s Tj ET\n" % (x, _row_coded(t))
                     for x, t in ((30, "Item"), (100, "123"), (150, "45"), (200, "6789")))
    if layout == "wide-cells":
        off = b"".join(b"BT /F2 12 Tf %d 500 Td %s Tj ET\n" % (430 + 40 * i, _row_coded(d))
                       for i, d in enumerate("31415926"))
    else:
        off = b"BT /F2 12 Tf 25 Tc 440 500 Td " + _row_coded("JOB-REF-2291") + b" Tj ET\n"
    content = text("Quarterly report", 760) + b"\n" + cells + off
    extra = b"/CropBox [0 0 400 792]" + (b" /Rotate %d" % rotate if rotate else b"")
    objects = one_page(stream(b"", content), o7=differences_font(_ROW_CHARS))
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>", extra=extra)
    _write(path, objects)


def _table_row_cases() -> None:
    for layout in TABLE_ROW_OFF_PAGE:
        for rotate, suffix in ((0, ""), (270, "-rotated")):
            @case(f"page.raw-table-row-beside-off-page-{layout}{suffix}", truth="clean",
                  features=("live.font", "off-page.font"), writer="raw",
                  expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
                  story="A font-coded table row 'Item | 123 | 45 | 6789' inside the crop box, and on "
                        "its baseline past the crop box's right edge "
                        + ("wide single-digit cells" if layout == "wide-cells"
                           else "a letter-spaced slug")
                        + ". The cells are read apart, never as one number; joining the "
                          "page's lines gives the usual manual-review warning."
                        + (" The page is turned with /Rotate 270." if rotate else ""))
            def row(path: Path, layout: str = layout, rotate: int = rotate) -> None:
                table_row_beside_off_page(path, layout, rotate)


_table_row_cases()


# ── A visible part and a far off-page part on the same line ─────────────
# 'Acct 123-45' at the right margin and, on its baseline, '6789 Job' past
# the media box's right edge, a column break away (60 pt, or 12 pt: both
# wider than one and a half characters). No reading but reading 9 (each
# visible line whole) joins them, and only its joined text forms the SSN:
# manual review by the owner's decision of 2026-10-11, never a finding
# (nothing ties the halves) and never silent. A clean page may show such
# a coincidence, so it is review-tier noise by design.
BASELINE_GAPS = {"far": 60, "near": 12}


def visible_beside_off_page(path: Path, gap: str, rotate: int) -> None:
    """One page; *gap* is a key of BASELINE_GAPS; with /Rotate 270 the
    crop box reaches 200 pt past the media box's right edge."""
    content = (text("Quarterly report", 700)
               + b"\nBT /F2 12 Tf 532 400 Td " + _baseline_coded("Acct 123-45") + b" Tj ET"
               + b"\nBT /F2 12 Tf %d 400 Td " % (612 + BASELINE_GAPS[gap])
               + _baseline_coded("6789 Job") + b" Tj ET")
    turned = b"/Rotate %d /CropBox [0 0 812 792]" % rotate if rotate else b""
    objects = one_page(stream(b"", content), o7=differences_font(_BASELINE_CHARS))
    objects[3] = page(resources=b"<< /Font << /F1 6 0 R /F2 7 0 R >> >>", extra=turned)
    _write(path, objects)


_BASELINE_CHARS = "".join(dict.fromkeys("Acct 123-45" + "6789 Job"))


def _baseline_coded(text_: str) -> bytes:
    return b"<" + b"".join(b"%02X" % (_BASELINE_CHARS.index(c) + 1) for c in text_) + b">"


def _baseline_cases() -> None:
    for gap in BASELINE_GAPS:
        for rotate, suffix in ((0, ""), (270, "-rotated")):
            @case(f"page.raw-font-coded-baseline-off-page-{gap}{suffix}", truth="clean",
                  features=("live.font", "off-page.font"), writer="raw",
                  expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
                  story=f"'Acct 123-45' in font codes at the right margin and, "
                        f"{BASELINE_GAPS[gap]} pt past the media box's right edge on the "
                        "same baseline, '6789 Job': together they read as the SSN. A visible "
                        "part and a far off-page part on one line are flagged for review, "
                        "never a finding and never silent (owner decision, 2026-10-11)."
                        + (" The page is turned with /Rotate 270." if rotate else ""))
            def baseline(path: Path, gap: str = gap, rotate: int = rotate) -> None:
                visible_beside_off_page(path, gap, rotate)


_baseline_cases()


# ── false-alarm.slug-beside-visible-line ────────────────────────────────
# Reading 4 keeps a line's column-break segments that hold a visible
# glyph. A slug wholly off the page, a word space from visible text on
# its baseline, is no separate segment: 'Page 7' and 'Job 123-45' read
# 'Page7Job123-45', just as a value running past the edge would, and the
# line meets the next page's first line at the hard seam.

@case(
    "page.raw-slug-beside-page-number", truth="clean",
    features=("live.plain", "off-page.plain"), writer="raw",
    expected=expect(2, warnings=(("REVIEW_CROSS_PAGE", "live"),)),
    story="Page 1's page number, 'Page 7', sits at the right margin, and a print slug "
          "'Job 123-45' just past the media box's right edge on the same baseline, a "
          "word space away; page 2 opens with '6789 units shipped'. Nothing tells the "
          "line apart from a value running past the edge, so the Text layer joins it "
          "with page 2's first line at the hard page seam: a false finding, where the "
          "coincidence deserves a manual-review warning at most.",
    known_gap=KnownGap("false-alarm.slug-beside-visible-line",
                       expect(1, findings=(("SSN", "live"),))),
)
def slug_beside_page_number(path: Path) -> None:
    first = (text("Quarterly report", 700)
             + b"\nBT /F1 12 Tf 570 20 Td (Page 7) Tj ET\nBT /F1 12 Tf 614 20 Td (Job 123-45) Tj ET")
    objects = {1: CATALOG, 2: b"<< /Type /Pages /Kids [3 0 R 8 0 R] /Count 2 >>",
               3: page(), 4: stream(b"", first), 6: HELVETICA,
               8: page(b"9 0 R"), 9: stream(b"", text("6789 units shipped in March.", 770))}
    _write(path, objects)


# ── Document-level live content ─────────────────────────────────────────

@leak("document.raw-title", "metadata.plain",
      "The SSN in the Info dictionary's title.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Metadata"),)),
      requires=("exiftool",),
      mistake="Redacting the page but not the document properties.",
      recovery="File > Properties in any viewer.")
def info_title(path: Path) -> None:
    objects = one_page(PAGE, o7=b"<< /Title (Case " + SSN.encode() + b") >>")
    _write(path, objects, info=7)


@leak("leftover.raw-xmp", "leftover-xmp.plain",
      "An old XMP metadata packet with the SSN in its title, no longer referenced.",
      expected=ORPHAN_SSN,
      mistake="Rewriting metadata without garbage-collecting the old XMP packet.",
      recovery="Decompress the file and read the earlier /Metadata stream's XML.")
def orphan_xmp(path: Path) -> None:
    xmp = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
           '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>Case {SSN}</dc:title></rdf:Description></rdf:RDF></x:xmpmeta>'
           '<?xpacket end="w"?>')
    _write(path, one_page(PAGE, o5=stream(b"/Type /Metadata /Subtype /XML", xmp.encode())))


@leak("document.raw-javascript", "javascript.plain",
      "The SSN inside the document's open-action JavaScript.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Hidden"),)),
      mistake="Leaving a debug or prefill script with the value hard-coded.",
      recovery="Open the catalog's /OpenAction and read the /JS string.")
def javascript(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R /OpenAction 7 0 R >>"
    objects[7] = b"<< /S /JavaScript /JS (var ssn = '" + SSN.encode() + b"';) >>"
    _write(path, objects)


@leak("document.raw-form-field", "annot-fields.plain", "The SSN as a form field's value.",
      expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Hidden"),)),
      mistake="Redacting the page but not the form field's stored value.",
      recovery="Open the form in any PDF editor and inspect the field's value.")
def form_field(path: Path) -> None:
    objects = one_page(PAGE)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [7 0 R] >> >>"
    objects[3] = page(extra=b"/Annots [7 0 R]")
    objects[7] = (b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (ssn) "
                  b"/Rect [72 300 300 320] /V (" + SSN.encode() + b") >>")
    _write(path, objects)


# ── Attachments: a real /EmbeddedFiles name tree ────────────────────────

def attachment_objects(name: str, data: bytes, filespec: int = 7, efile: int = 8) -> dict[int, bytes]:
    """A document-level attachment: catalog /Names /EmbeddedFiles name tree,
    file specification, embedded-file stream — the structure PyMuPDF's own
    embfile_* calls read, hand-assembled instead of written through them."""
    catalog = (b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names "
               b"[(%s) %d 0 R] >> >> >>" % (name.encode(), filespec))
    spec = b"<< /Type /Filespec /F (%s) /EF << /F %d 0 R >> >>" % (name.encode(), efile)
    ef = stream(b"/Type /EmbeddedFile /Params << /Size %d >>" % len(data), data)
    return {1: catalog, filespec: spec, efile: ef}


FORGOT = "Redacting the pages and forgetting the attachments."


@leak("attachment.raw-zip", "attachment.container",
      "A zip of records attached to a clean-looking memo.", expected=NOT_TEXT,
      mistake=FORGOT, recovery="Open the attachments panel and save the zip.")
def zip_attachment(path: Path) -> None:
    objects = one_page(PAGE)
    objects.update(attachment_objects("records.zip", compressed("zip", SECRET)))
    _write(path, objects)


@leak("attachment.raw-scan-png", "attachment.pixels",
      "A scan of the SSN attached as a PNG.", expected=NOT_TEXT, mistake=FORGOT,
      recovery="Open the attachments panel and view the image.")
def png_attachment(path: Path) -> None:
    objects = one_page(PAGE)
    objects.update(attachment_objects("scan.png", png_of(SECRET)))
    _write(path, objects)


@leak("attachment.raw-notes", "attachment.plain",
      "Notes with the SSN attached as plain text (a run of arbitrary text: manual "
      "review).",
      expected=expect(2, warnings=(("REVIEW_HIDDEN_TEXT", "live"),)), mistake=FORGOT,
      recovery="Open the attachments panel and read the text file.")
def notes_attachment(path: Path) -> None:
    objects = one_page(PAGE)
    objects.update(attachment_objects("notes.txt", f"memo {SECRET}\n".encode()))
    _write(path, objects)


# ── Orphaned content ─────────────────────────────────────────────────────

@leak("leftover.raw-orphaned-container", "orphaned.container",
      "An orphaned zip payload, stored but referenced by nothing.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)),
      mistake="Saving without garbage collection.",
      recovery="Decompress the file, find the leftover stream, and extract the zip.")
def orphaned_container(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"", compressed("zip", SECRET))))


@leak("leftover.raw-attachment-zip", "orphaned-attachment.container",
      "A removed zip attachment holding the SSN, still stored.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)),
      mistake="Removing an attachment by deleting its listing, not with garbage collection.",
      recovery="Decompress the file, find the leftover stream, and extract the zip.")
def orphan_attachment_zip(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Type /EmbeddedFile", compressed("zip", SECRET))))


@leak("leftover.raw-attachment-text", "orphaned-attachment.plain",
      "A removed attachment's text is still stored, unreferenced (manual review: a run "
      "of arbitrary text).",
      expected=expect(2, warnings=(("REVIEW_OBJECT_TEXT", "orphaned"),)),
      mistake="Removing an attachment by deleting its listing, not with garbage collection.",
      recovery="Decompress the file and read the leftover /EmbeddedFile stream.")
def orphan_attachment_text(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Type /EmbeddedFile", f"Employee {SECRET}\n".encode())))


@leak("leftover.raw-attachment-png", "orphaned-attachment.pixels",
      "A removed PNG attachment showing the SSN, still stored.",
      expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "orphaned"),)),
      mistake="Removing an attachment by deleting its listing, not with garbage collection.",
      recovery="Decompress the file, find the leftover stream, and view the PNG.")
def orphan_attachment_png(path: Path) -> None:
    _write(path, one_page(PAGE, o5=stream(b"/Type /EmbeddedFile", png_of(SECRET))))


@leak("leftover.raw-compact-glyphs", "orphaned.font.compact-syntax",
      "An orphaned stream sets its font and shows the SSN's glyph codes right after "
      "BT with no separating space (\"BT/EM 11 Tf\"), as a minimizer or redactor "
      "writes it.", expected=UNDECODABLE_ORPHAN,
      mistake="Saving without garbage collection.",
      recovery="Decompress the file and map the glyph codes through the font's Unicode table.")
def compact_glyphs(path: Path) -> None:
    content = b"BT/EM 11 Tf 72 700 Td" + GLYPHS + b"Tj ET"
    _write(path, one_page(PAGE, o5=stream(b"", content)))


# ── Superseded by an incremental update ─────────────────────────────────
# rawpdf.incremental_update appends the new objects and cross-reference
# section itself; the object each replaces stays at its old offset, which
# is exactly what lets the verifier's earlier-revision walk find it again.

INCREMENTAL = "Saving the redaction as an incremental update, which appends the change and keeps the original."
RECOVER_REVISION = "Cut the file at the earlier revision's %%EOF and open that revision."


def _incremental(base: bytes, new: dict[int, bytes | None]):
    """A case builder: *base* with *new* appended as one incremental update."""
    def build_case(path: Path) -> None:
        path.write_bytes(incremental_update(base, new))  # type: ignore[arg-type]
    return build_case


case(
    "revision.raw-rewritten-stream", truth="leak", cells="superseded.plain",
    expected=SUPERSEDED_SSN, writer="raw",
    story="A content stream holding the SSN, overwritten by an incremental update with "
          "clean text — the original stays at its old offset in the file.",
    mistake=INCREMENTAL, recovery=RECOVER_REVISION,
)(_incremental(build(one_page(stream(b"", text(SECRET)))),
               {4: stream(b"", text("REDACTED"))}))

case(
    "revision.raw-rewritten-font", truth="leak", cells="superseded.font",
    expected=UNDECODABLE_SUPERSEDED, writer="raw",
    story="A content stream showing the SSN's glyph codes, rewritten by an incremental "
          "update to plain filler text: the earlier version is undecodable, but still there.",
    mistake=INCREMENTAL, recovery=RECOVER_REVISION,
)(_incremental(build(one_page(stream(b"", b"BT /EM 11 Tf 72 700 Td " + GLYPHS + b" Tj ET"))),
               {4: stream(b"", text("clean now"))}))

case(
    "revision.raw-rewritten-font-compact", truth="leak", cells="superseded.font.compact-syntax",
    expected=UNDECODABLE_SUPERSEDED, writer="raw",
    story="The same, written compactly (\"BT/EM 11 Tf\", no space after BT) before the "
          "incremental update replaces it.",
    mistake=INCREMENTAL, recovery=RECOVER_REVISION,
)(_incremental(build(one_page(stream(b"", b"BT/EM 11 Tf 72 700 Td" + GLYPHS + b"Tj ET"))),
               {4: stream(b"", text("clean now"))}))


def _pixel_strip(word: str) -> tuple[bytes, int, int]:
    gray = fitz.Pixmap(fitz.csGRAY, fitz.Pixmap(png_of(word)))
    return gray.samples, gray.width, gray.height


def _inline_image(word: str) -> bytes:
    samples, w, h = _pixel_strip(word)
    return (b"q %d 0 0 %d 72 400 cm BI /W %d/H %d/BPC 8/CS/G ID " % (w, h, w, h)
            + samples + b"\nEI Q")


case(
    "revision.raw-replaced-image", truth="leak", cells="superseded.pixels",
    expected=expect(2, warnings=(("LEFTOVER_IMAGE", "superseded"),)), writer="raw",
    story="A scan showing the SSN, replaced by a clean image in an incremental update.",
    mistake=INCREMENTAL, recovery="Extract the images from the earlier revision.",
)(_incremental(build(one_page(stream(b"", _inline_image(SECRET)))),
               {4: stream(b"", _inline_image("REDACTED"))}))

_ATTACHMENT_BASE = one_page(stream(b"", text("memo")))
_ATTACHMENT_BASE.update(attachment_objects("records.zip", compressed("zip", SECRET)))
_CLEAN_ZIP = zipbytes("nothing here")

case(
    "revision.raw-replaced-attachment", truth="leak", cells="superseded.container",
    expected=expect(2, warnings=(("LEFTOVER_CONTAINER", "superseded"),
                                 ("ATTACHMENT_NOT_TEXT", "live"))),
    writer="raw",
    story="A zip attachment holding the SSN, replaced by a clean one in an incremental "
          "update.",
    mistake="Replacing an attachment incrementally keeps the original.",
    recovery="Decompress the file, read the earlier revision's embedded-file stream, "
             "and extract the zip.",
)(_incremental(build(_ATTACHMENT_BASE),
               {8: stream(b"/Type /EmbeddedFile /Params << /Size %d >>" % len(_CLEAN_ZIP),
                          _CLEAN_ZIP)}))


def _many_revisions(path: Path) -> None:
    """The SSN only in the second of 57 earlier revisions, the rest no-ops:
    past the revision cap, so the tool says it cannot certify rather than
    passing the file as clean."""
    pdf = build(one_page(stream(b"", text("no secret here"))))
    pdf = incremental_update(pdf, {7: b"<< /Marker (" + SSN.encode() + b") >>"})
    pdf = incremental_update(pdf, {7: b"<< /Marker (gone) >>"})
    for i in range(55):
        pdf = incremental_update(pdf, {8: b"<< /N %d >>" % i})
    path.write_bytes(pdf)


case(
    "revision.raw-many-revisions", truth="leak", cells="superseded.plain.revision-cap",
    expected=expect(2, warnings=(("REVISION_CAP", "superseded"),)), writer="raw",
    story="The SSN sits only in the second of 57 earlier revisions; once more than 50 "
          "pile up, that one falls outside what gets scanned, and the tool says so "
          "rather than pass the file.",
    mistake=INCREMENTAL,
    recovery="Cut the file at the second revision's %%EOF and open it.",
)(_many_revisions)


# ── Matching limits: a page break, a line wrap ──────────────────────────
# Both need the value's two halves in separate content-stream objects, not
# one combined stream: the Objects layer's literal-adjacency join
# (RollingScanner) works within a single object, joining any two literals
# there regardless of what lies between them — which is right for a value
# split to dodge string-boundary matching, but would turn these into a
# hard Objects finding instead of the reading-order join the Text/OCR
# layers do. fitz's own pages happen to keep every insert_text call in its
# own content-stream object, which is why the fitz siblings never hit this;
# a hand-assembled page has to split them on purpose.

def two_page(first: bytes, second: bytes) -> dict[int, bytes]:
    """A two-page document: page 1's contents is object 4, page 2's is
    object 6, sharing one font (8)."""
    resources = b"<< /Font << /F1 8 0 R >> >>"
    return {
        1: CATALOG,
        2: b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        3: page(contents=b"4 0 R", resources=resources),
        4: first,
        5: page(contents=b"6 0 R", resources=resources),
        6: second,
        8: HELVETICA,
    }


def _lines(entries: list[tuple[str, int]]) -> bytes:
    return b" ".join(text(s, y) for s, y in entries)


@leak("layout.raw-page-break", "match.page-break",
      "A code name split across a page break, in a hand-assembled two-page document: "
      "\"Project code BLUE\" at the bottom of page 1, \"HERON continues here\" at the "
      "top of page 2.", expected=expect(1, findings=(("Code", "live"),)),
      mistake="Nobody redacted it; the value simply straddles a page break.",
      recovery="Read the last line of one page and the first line of the next.")
def raw_page_break(path: Path) -> None:
    page1 = [(f, 748 - 16 * i) for i, f in enumerate(FILLER)]
    page1.append(("Project code BLUE", 60))
    page2 = [("HERON continues here", 740)]
    page2 += [(f, 700 - 16 * i) for i, f in enumerate(FILLER)]
    path.write_bytes(build(two_page(stream(b"", _lines(page1)), stream(b"", _lines(page2)))))


@leak("layout.raw-line-wrap", "match.line-wrap",
      "A value wrapped across two consecutive lines, kept as separate content-stream "
      "objects: \"total 123-45-\" then \"6789 units\" — the joined reading matches, but "
      "only as a coincidental fusion (manual review).",
      expected=expect(2, warnings=(("REVIEW_CROSS_LINE", "live"),)),
      mistake="Nobody redacted it; the value simply wraps across two lines.",
      recovery="Read the two consecutive lines together.")
def raw_line_wrap(path: Path) -> None:
    filler = [(f, 700 - 16 * i) for i, f in enumerate(FILLER)]
    objects = one_page(stream(b"", _lines(filler) + b" " + text("total 123-45-", 400)),
                        o5=stream(b"", text("6789 units", 384)))
    objects[3] = page(contents=b"[4 0 R 5 0 R]")
    _write(path, objects)


# ── Object streams and cross-reference streams (PDF 1.5) ────────────────
# The classic writer above never exercises this format; a couple of cases
# rebuilt on it check that the verifier (which reads through PyMuPDF)
# agrees with a hand-assembled ObjStm/XRef stream file too.

@case("page.raw-minimal-objstm", truth="clean", features="live.plain", writer="raw",
      expected=expect(0),
      story="A minimal one-page document written as an object stream with a "
            "cross-reference stream (control for the objstm/xref-stream writer).")
def clean_objstm(path: Path) -> None:
    path.write_bytes(build_objstm(one_page(stream(b"", text("Quarterly report")))))


@leak("leftover.raw-orphaned-objstm", "orphaned.plain",
      "An orphaned stream holding the SSN, in a file whose objects are packed into an "
      "object stream and whose cross-reference is itself a stream.",
      expected=ORPHAN_SSN,
      mistake="Saving without garbage collection.",
      recovery="Decompress the file's object streams and read the leftover stream.")
def orphaned_objstm(path: Path) -> None:
    objects = one_page(PAGE, o5=stream(b"", text(SECRET)))
    path.write_bytes(build_objstm(objects))


def _superseded_objstm(path: Path) -> None:
    base = build_objstm(one_page(stream(b"", text(SECRET))))
    path.write_bytes(incremental_update(base, {4: stream(b"", text("REDACTED"))}))


case(
    "revision.raw-rewritten-stream-objstm", truth="leak", cells="superseded.plain",
    expected=SUPERSEDED_SSN, writer="raw",
    story="The same overwritten content stream, on a base file written as an object "
          "stream with a cross-reference stream, then updated with a classic "
          "incremental section — the two formats chain together correctly.",
    mistake=INCREMENTAL, recovery=RECOVER_REVISION,
)(_superseded_objstm)
