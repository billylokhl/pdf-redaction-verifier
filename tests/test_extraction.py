"""Unit-level regression tests for the normalizer and text-layer extraction.

Each test pins a previously confirmed detection-gap bug.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

import caselib
from caselib.families.raw_cells import (BASELINE_GAPS, BREAK_LAYOUTS, OFF_PAGE_CARRY,
                                        SCATTERED, TABLE_ROW_OFF_PAGE, page_break_at_edge,
                                        table_row_beside_off_page, value_carried_off_page,
                                        value_scattered,
                                        visible_beside_off_page)
import pymupdf as fitz
import pytest

import verify

from redaction_verifier.matching import SecretMatcher, normalize_string
from redaction_verifier.model import ScanReport, Secret
from redaction_verifier.rules import load_rules
from redaction_verifier.views import (TEXT_GENUINE_READINGS, TEXT_SEAM_READINGS,
                                      TEXT_SCATTER_READINGS, TEXT_TAILS_INTO_NEXT_PAGE,
                                      extract_visual_text,
                                      ocr)

from .conftest import HAS_OCR, SSN, report_lines, run_verify


def _dom_finds(page: fitz.Page, normalized_secret: str) -> bool:
    return any(
        normalized_secret in normalize_string(variant)
        for variant in extract_visual_text(page)
    )


class TestNormalizer:
    def test_formatting_variants_collapse(self) -> None:
        for form in ("123-45-6789", "123 45 6789", "1 2 3\n4 5-6.7/8 9"):
            assert normalize_string(form) == "123456789"

    def test_fullwidth_digits_fold_to_ascii(self) -> None:
        # Regression: ASCII-only normalizer deleted fullwidth forms,
        # so a visually identical secret false-PASSed.
        assert normalize_string("１２３－４５－６７８９") == "123456789"

    def test_accents_and_casefold(self) -> None:
        assert normalize_string("José-Straße") == "josestrasse"

    def test_overlapping_secrets_both_found(self) -> None:
        # SecretMatcher must not let one match consume an overlapping one.
        matcher = SecretMatcher([
            Secret("a", "1234"), Secret("b", "2345"),
        ])
        assert {s.name for s in matcher.search("12345")} == {"a", "b"}


class TestVisualOrder:
    def test_shuffled_form_box_digits(self, leaky_pdf) -> None:
        # Digits drawn out of order at one baseline must reconstruct
        # in visual left-to-right order (horizontal variant specifically).
        doc = fitz.open(leaky_pdf)
        try:
            horizontal = extract_visual_text(doc[0])[0]
            assert SSN.replace("-", "") in normalize_string(horizontal)
        finally:
            doc.close()

    def test_baseline_jitter_scales_with_glyph_size(self) -> None:
        # Regression: fixed 4pt tolerance split 30pt digits with 6pt
        # jitter into pseudo-lines, scrambling the order.
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):
            page.insert_text((72 + i * 40, 200 + (6 if i % 2 else 0)), digit, fontsize=30)
        try:
            horizontal = extract_visual_text(page)[0]
            assert "123456789" in normalize_string(horizontal)
        finally:
            doc.close()

    def test_rotated_matrix_text(self) -> None:
        # Regression: vertical (rotate=90) text reconstructed reversed
        # or interleaved under the horizontal-only line sort.
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((300, 400), SSN, fontsize=14, rotate=90)
        try:
            assert _dom_finds(page, SSN.replace("-", ""))
        finally:
            doc.close()

    def test_dense_fine_print_lines_stay_separate(self) -> None:
        # The tolerance ceiling must keep 8pt lines from merging.
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "111 222 333", fontsize=8)
        page.insert_text((72, 109), "444 555 666", fontsize=8)
        try:
            horizontal = extract_visual_text(page)[0]
            assert "111222333" in normalize_string(horizontal.split("\n")[0])
        finally:
            doc.close()



def _overhanging_crop_page(crop: tuple[float, float, float, float] | None, rotate: int,
                           on_page: tuple[int, int], off_page: tuple[int, int],
                           user_unit: float = 1) -> fitz.Document:
    """A 595x842 page with an optional crop box (one reaching past the
    media box, too), rotated, with the SSN drawn once inside the visible
    area and once outside it."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # Set directly: PyMuPDF's set_cropbox refuses a box past the media box.
    if crop is not None:
        doc.xref_set_key(page.xref, "CropBox", "[%g %g %g %g]" % crop)
    doc.xref_set_key(page.xref, "Rotate", str(rotate))
    if user_unit != 1:
        doc.xref_set_key(page.xref, "UserUnit", str(user_unit))
    font = doc.get_new_xref()
    doc.update_object(font, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    doc.xref_set_key(page.xref, "Resources", f"<</Font<</F1 {font} 0 R>>>>")
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, (
        f"BT /F1 9 Tf {on_page[0]} {on_page[1]} Td (VISIBLE {SSN}) Tj ET\n"
        f"BT /F1 9 Tf {off_page[0]} {off_page[1]} Td (HIDDEN 987654321) Tj ET\n"
    ).encode())
    doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
    return fitz.open("pdf", doc.tobytes())


class TestVisibleAreaOnRotatedCrops:
    """Glyphs are reported with the visible area (crop box ∩ media box,
    scaled by /UserUnit) at the origin whatever the rotation. Derotating
    page.rect has the right size but, on a rotated page whose crop box
    reaches past its media box or whose /UserUnit is not 1, the wrong
    offset — which filed visible text as off-page (reading 3) and could
    split a value between the two readings."""

    # (crop box, a point inside crop ∩ media, a point inside the media box
    # but outside the crop box), all in PDF user space.
    CASES = [
        ((0, 200, 612, 992), (300, 500), (300, 100)),
        ((-100, -100, 400, 600), (150, 300), (450, 300)),
        ((200, 300, 900, 1000), (300, 600), (60, 600)),
        ((50, 60, 500, 700), (200, 400), (520, 400)),  # control: inside the media box
    ]

    @staticmethod
    def _assert_readings(doc: fitz.Document) -> None:
        try:
            page = doc[0]
            # Positive control: MuPDF itself shows the one and not the other.
            shown = page.get_text("text")
            assert "VISIBLE" in shown and "HIDDEN" not in shown
            readings = extract_visual_text(page)
            visible, off = readings[0], readings[3]
            assert SSN.replace("-", "") in normalize_string(visible), readings
            assert "HIDDEN" not in visible
            assert "HIDDEN" in off and "VISIBLE" not in off
        finally:
            doc.close()

    @pytest.mark.parametrize("rotate", [0, 90, 180, 270])
    @pytest.mark.parametrize(("crop", "on_page", "off_page"), CASES)
    def test_visible_and_off_page_text_land_in_the_right_reading(
        self, crop, on_page, off_page, rotate
    ) -> None:
        self._assert_readings(_overhanging_crop_page(crop, rotate, on_page, off_page))

    @pytest.mark.parametrize("rotate", [0, 90, 180, 270])
    @pytest.mark.parametrize(("crop", "on_page", "off_page"), [
        (None, (100, 100), (700, 100)),               # past the media box
        ((0, 200, 612, 992), (300, 500), (300, 100)),  # crop past the media box
    ])
    def test_user_unit_other_than_one(self, crop, on_page, off_page, rotate) -> None:
        # /UserUnit 2 doubles the page and every glyph position alike.
        self._assert_readings(
            _overhanging_crop_page(crop, rotate, on_page, off_page, user_unit=2))

    @pytest.mark.parametrize("rotate", [0, 90, 180, 270])
    @pytest.mark.parametrize("crop", [
        (700, 100, 900, 400),   # wholly outside the media box
        (60, 60, 60, 60),       # a point inside it
    ])
    def test_empty_crop_box_and_media_box_intersection(self, crop, rotate) -> None:
        # Nothing is drawn, and MuPDF falls back to a 612x792 frame at the
        # origin. The readings follow that frame at every rotation, as at
        # /Rotate 0: inside it, reading 0; past it, reading 3.
        doc = _overhanging_crop_page(crop, rotate, (150, 150), (700, 150))
        try:
            page = doc[0]
            assert page.get_text("text").strip() == ""          # control
            assert sorted(page.rect[2:]) == [612, 792]           # control
            readings = extract_visual_text(page)
            assert "VISIBLE" in readings[0] and "HIDDEN" not in readings[0], readings
            assert "HIDDEN" in readings[3] and "VISIBLE" not in readings[3], readings
        finally:
            doc.close()


def _build(pages: list[list[str]], *, rotate: int = 0) -> fitz.Document:
    """One list of lines per page (an empty list is a blank page);
    rotate=90 writes each line vertically."""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        for i, line in enumerate(lines):
            if rotate:
                page.insert_text((100 + 30 * i, 300), line, rotate=rotate)
            else:
                page.insert_text((72, 100 + 30 * i), line)
    return doc


def _scan(doc: fitz.Document, *, layer: str = "Text", extractor=None,
          hard_variants: int = TEXT_GENUINE_READINGS,
          seam_variants: int | None = TEXT_SEAM_READINGS,
          tails_into_next: tuple[int, ...] = TEXT_TAILS_INTO_NEXT_PAGE,
          scatter_readings: tuple[int, ...] = TEXT_SCATTER_READINGS,
          secrets=(("SSN", SSN),), patterns=(), fail_fast: bool = False) -> ScanReport:
    """scan_page_layer as the CLI runs the Text layer (pass hard_variants,
    seam_variants=None, tails_into_next=() and scatter_readings=() for
    OCR)."""
    report = ScanReport()
    matcher = SecretMatcher(
        [Secret(name, normalize_string(v)) for name, v in secrets])
    try:
        verify.scan_page_layer(doc, matcher, report, layer=layer,
                               extractor=extractor or extract_visual_text,
                               note="visual text layer", patterns=patterns,
                               hard_variants=hard_variants, seam_variants=seam_variants,
                               tails_into_next=tails_into_next,
                               scatter_readings=scatter_readings, fail_fast=fail_fast)
    finally:
        doc.close()
    return report


def _scan_pages(pages: list[list[str]], **kw) -> ScanReport:
    return _scan(_build(pages), **kw)


def seam(prev: int, page: int) -> str:
    return verify.CROSS_PAGE_LOCATION.format(prev=prev, page=page,
                                             note="visual text layer")


def line(page: int, name: str = "SSN", layer: str = "Text") -> str:
    return verify.CROSS_LINE_WARNING.format(layer=layer, page=page, name=name)


def crossing(page: int, name: str = "SSN", layer: str = "Text") -> str:
    return verify.CROSS_PAGE_WARNING.format(layer=layer, page=page, name=name)


def _where(report: ScanReport) -> list[str]:
    return [f.location for f in report.findings]


PAGE = "page {} (visual text layer)"


class TestSamePageTiers:
    """Within one page: hard on one line of a genuine reading; manual review
    for joined lines and for reconstructed (all-glyph vertical) readings."""

    def test_same_page_split_is_not_escalated(self) -> None:
        # Regression: each page's fully joined text fed a cross-page rolling
        # scanner, so a same-page cross-line join became a hard finding
        # mislabelled "across page boundaries" — on a one-page document.
        report = _scan_pages([["Invoice total 123-45-", "6789 units shipped"]])
        assert report.findings == []
        assert report.warnings == [line(1)]

    def test_cross_line_warning_is_raised_once_per_page(self) -> None:
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):        # a diagonal
            page.insert_text((72 + 20 * i, 100 + 20 * i), digit)
        report = _scan(doc)
        assert report.findings == []
        assert report.warnings == [line(1)]

    def test_each_page_gets_its_own_cross_line_warning(self) -> None:
        report = _scan_pages([["a 123-45-", "6789 x"], ["b 123-45-", "6789 y"]])
        # Page 2 also draws a page-break review: page 1's '6789 x' and
        # page 2's 'b 123-45-' form the value in reverse order, a few lines
        # apart (SecretMatcher.scattered).
        assert report.warnings == [line(1), line(2), crossing(2)]

    def test_confirmed_leak_draws_no_coincidence_warning(self) -> None:
        # Regression: vertical readings called a secret found hard on one
        # horizontal line "possibly coincidental" — twice.
        report = _scan_pages([["SSN 123-45-6789"], ["next page"]])
        assert _where(report) == [PAGE.format(1)]
        assert report.warnings == []

    def test_rotated_text_is_read_in_its_own_direction(self) -> None:
        report = _scan(_build([["SSN 123-45-6789"]], rotate=90))
        assert _where(report) == [PAGE.format(1)]
        assert report.warnings == []

    def test_numbered_list_is_not_a_hard_finding(self) -> None:
        # Regression: an all-glyph vertical reading turns a numbered list's
        # first column into '123456789' — the README's own example value —
        # and it counted as a hard finding on a clean page.
        report = _scan_pages([[f"{i}. item" for i in range(1, 10)]])
        assert report.findings == []
        assert report.warnings == [line(1)]

    def test_value_written_one_digit_per_line_is_manual_review(self) -> None:
        # Only a reconstructed reading holds it: surfaced, not certified.
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):
            page.insert_text((300, 100 + 20 * i), digit)
        report = _scan(doc)
        assert report.findings == []
        assert report.warnings == [line(1)]


class TestPageSeam:
    """Hard: a value running from one page's last line onto the next
    page's first line, in a genuine reading."""

    def test_split_between_last_and_first_line_is_hard(self) -> None:
        report = _scan_pages([["intro", "Applicant SSN 123-45-"],
                              ["6789 continues here", "more"]])
        assert _where(report) == [seam(1, 2)]
        assert report.warnings == []

    def test_seam_keeps_enough_of_the_previous_line(self) -> None:
        report = _scan_pages([["SSN 123-45-678"], ["9 end"]])
        assert _where(report) == [seam(1, 2)]

    def test_value_wholly_on_the_first_line_is_not_a_seam(self) -> None:
        report = _scan_pages([["intro"], ["SSN 123-45-6789 end"]])
        assert _where(report) == [PAGE.format(2)]
        assert report.warnings == []

    def test_every_seam_is_reported(self) -> None:
        report = _scan_pages([["SSN 123-45-"], ["6789", "id 123-"], ["45-6789"]])
        assert _where(report) == [seam(1, 2), seam(2, 3)]

    def test_a_whole_copy_does_not_mask_a_crossing_one(self) -> None:
        # Regression: matching by secret name dropped a seam whenever the
        # same value also sat wholly on one side of the join.
        report = _scan_pages([["x 123-45-"], ["6789 SSN 123-45-6789"]])
        assert _where(report) == [PAGE.format(2), seam(1, 2)]

    def test_rotated_split_is_hard(self) -> None:
        report = _scan(_build([["SSN 123-45-"], ["6789 end"]], rotate=90))
        assert _where(report) == [seam(1, 2)]
        assert report.warnings == []

    def test_several_secrets_of_different_lengths(self) -> None:
        secrets = (("SSN", SSN), ("NAME", "smith"), ("ACCT", "GB82WEST12345698765432"))
        report = _scan_pages([["Mr Smith"], ["and co"]], secrets=secrets)
        assert _where(report) == [PAGE.format(1)]          # no spurious seam
        report = _scan_pages([["intro", "x 12345"], ["6789 zz"]], secrets=secrets)
        assert _where(report) == [seam(1, 2)]

    def test_vertical_reconstruction_never_builds_a_hard_seam(self) -> None:
        # Regression: a ledger's right-hand digits (page 1) and a numbered
        # list (page 2) joined down the all-glyph vertical reading into a
        # hard '123456789' across the break on an all-horizontal document.
        doc = fitz.open()
        p1 = doc.new_page()
        for i, amount in enumerate(["$1,201", "$3,402", "$5,603", "$7,804", "$9,005"]):
            p1.insert_text((480, 100 + 20 * i), amount)
        p2 = doc.new_page()
        for i, n in enumerate("6789"):
            p2.insert_text((40, 100 + 20 * i), f"{n}. fee")
        report = _scan(doc)
        assert report.findings == []

    def test_fail_fast_stops_at_the_seam(self) -> None:
        # Regression: seam findings were recorded only after the page loop,
        # so --fail-fast kept extracting (and OCRing) every later page.
        seen: list[int] = []

        def counting(page: fitz.Page) -> list[str]:
            seen.append(page.number)
            return extract_visual_text(page)

        doc = _build([["SSN 123-45-"], ["6789 end"]] + [["filler"]] * 8)
        report = _scan(doc, extractor=counting, fail_fast=True)
        assert _where(report) == [seam(1, 2)]
        assert seen == [0, 1]


class TestPageBreakBackstop:
    """Manual review: every other match that crosses a page break, located
    at the page it crosses into — never silent, never hidden by name."""

    def test_split_needing_other_lines_before_the_break(self) -> None:
        report = _scan_pages([["ref 123", "45-"], ["6789 end"]])
        assert report.findings == []
        assert report.warnings == [crossing(2)]

    def test_split_needing_other_lines_after_the_break(self) -> None:
        report = _scan_pages([["SSN 123-45-"], ["67", "89 end"]])
        assert report.findings == []
        assert report.warnings == [crossing(2)]

    def test_located_at_its_own_page_not_the_last(self) -> None:
        report = _scan_pages([["ref 123", "45-"], ["6789 end"], ["tail"]])
        assert report.warnings == [crossing(2)]

    def test_value_spread_over_three_pages(self) -> None:
        # Regression (silent miss): the first seam-only design lost a value
        # whose middle part is the whole of page 2.
        report = _scan_pages([["intro", "SSN 123-"], ["45-"], ["6789 end"]])
        assert report.findings == []
        assert report.warnings == [crossing(3)]

    def test_page_number_pages_do_not_chain_into_a_hard_finding(self) -> None:
        # Pages whose only text is a page or Bates number must not assemble
        # a hard finding: the chain is manual review.
        report = _scan_pages([[str(n)] for n in range(1, 13)],
                             secrets=(("CASE", "456789101"),))
        assert report.findings == []
        assert report.warnings == [crossing(11, "CASE")]   # ...9, 10, 1(1)

    def test_one_character_either_side_of_the_break(self) -> None:
        for pages in ([["x 1234", "5678"], ["9 end"]],
                      [["abc 1"], ["2345", "6789"]]):
            report = _scan_pages(pages)
            assert report.findings == [], pages
            assert report.warnings == [crossing(2)], pages

    def test_blank_or_unreadable_middle_page_breaks_the_seam(self) -> None:
        for pages in ([["SSN 123-45-"], [], ["6789 tail"]],
                      [["SSN 123-45-"], ["* * *"], ["6789 tail"]]):
            report = _scan_pages(pages)
            assert report.findings == [], pages
            assert report.warnings == [crossing(3)], pages

        def flaky(page: fitz.Page) -> list[str]:
            if page.number == 1:
                raise RuntimeError("unreadable")
            return extract_visual_text(page)

        report = _scan(_build([["SSN 123-45-"], ["x"], ["6789 tail"]]),
                       extractor=flaky)
        assert report.findings == []
        assert report.warnings == ["Text: page 2 failed (unreadable)", crossing(3)]

    def test_every_crossing_location_is_reported(self) -> None:
        # Regression: only the first backstop location per secret was kept.
        report = _scan_pages([["t 123-45"], [], ["6789 u"], ["x"],
                              ["123-45"], [], ["6789 v"]])
        assert report.warnings == [crossing(3), crossing(7)]

    def test_unrelated_warning_does_not_hide_a_real_split(self) -> None:
        # Regression (lost location): a same-page coincidence on page 1
        # muted the page-break warning for the real split on pages 2-4.
        report = _scan_pages([["Qty 123-45", "6789 units"],
                              ["Employee SSN 123-45"], [], ["6789 end of record"]])
        # crossing(2): page 2's '...123-45' and page 1's '6789 units' form
        # the value in reverse order across the first break, also reviewed.
        assert report.warnings == [line(1), crossing(2), crossing(4)]

    def test_a_confirmed_leak_does_not_hide_another_location(self) -> None:
        report = _scan_pages([["SSN 123-45-6789", "id 123", "45-"], ["6789 x"]])
        assert _where(report) == [PAGE.format(1)]
        assert report.warnings == [crossing(2)]

    def test_rotated_text_joined_over_other_lines(self) -> None:
        report = _scan(_build([["ref 123", "45-"], ["6789 end"]], rotate=90))
        assert report.findings == []
        assert report.warnings == [crossing(2)]



# The case library's font-coded layouts: horizontal text running past the
# crop box or past the media box under a crop box that reaches beyond it,
# and text written vertically off the page, each at /Rotate 0 and 270.
ACROSS_THE_EDGE = sorted(
    case_id for case_id in caselib.load()
    if case_id.startswith(("page.raw-font-coded-runs-", "page.raw-font-coded-vertical-off-page-")))
NO_ACROSS_READINGS = 4    # readings 0-3, as before the readings across the edge


class TestReadingsAcrossTheVisibleEdge:
    """Readings 4-6 read every glyph of one writing direction, visible or
    not, so a value running across the edge of the visible area, or
    written vertically outside it, is read whole and found hard. Text
    wholly off the page never makes a hard page seam, a column gap keeps
    an off-page slug apart from a visible line on its baseline, and the
    line tolerance across the edge is the visible text's."""

    def test_the_layouts_are_in_the_case_library(self) -> None:
        assert len(ACROSS_THE_EDGE) == 8, ACROSS_THE_EDGE

    @pytest.mark.parametrize("case_id", [*ACROSS_THE_EDGE, "page.runs-off-right-edge-embedded-font"])
    def test_font_coded_value_is_found_hard(self, case_id: str, tmp_path) -> None:
        pdf = tmp_path / "case.pdf"
        caselib.REGISTRY[case_id].build(pdf)
        assert _where(_scan(fitz.open(pdf))) == [PAGE.format(1)]

        # Without readings 4-6 the value is split or scrambled: no reading
        # holds it whole, so nothing is found hard.
        def without_across(page: fitz.Page) -> list[str]:
            readings = extract_visual_text(page)
            return readings[:NO_ACROSS_READINGS] + readings[TEXT_GENUINE_READINGS:]

        assert _scan(fitz.open(pdf), extractor=without_across,
                     hard_variants=NO_ACROSS_READINGS).findings == []

    def test_visible_and_off_page_readings_keep_their_content(self) -> None:
        doc = _overhanging_crop_page((0, 0, 500, 842), 0, (450, 300), (540, 500))
        try:
            readings = extract_visual_text(doc[0])
        finally:
            doc.close()
        assert len(readings) == 10
        visible, outside, across = readings[0], readings[3], readings[4]
        # 'VISIBLE 123-45-6789' starting at x=450 is cut by the crop box's
        # edge at x=500: its start on the page, its end off it.
        assert "VISIBLE" in visible and SSN.replace("-", "") not in normalize_string(visible)
        assert "HIDDEN" not in visible and "HIDDEN" in outside
        assert SSN.replace("-", "") not in normalize_string(outside)
        assert any("VISIBLE" + SSN in line for line in across.split("\n")), across

    @staticmethod
    def _slug_then_unrelated_first_line() -> fitz.Document:
        doc = fitz.open()
        p1 = doc.new_page()
        p1.insert_text((72, 700), "Quarterly report, page one.")
        p1.insert_text((72, 900), "JOB 123-45")             # below the page
        doc.new_page().insert_text((72, 60), "6789 units shipped in March.")
        return doc

    def test_off_page_slug_at_a_page_break_is_not_a_hard_seam(self) -> None:
        # Clean control: a slug below page 1 ends with '123-45' and the
        # unrelated first line of page 2 starts with '6789'. The slug is
        # wholly off the page, so it is the last line of reading 3 only,
        # and page 2 has no off-page text to seam it with. The join is
        # still a page-break crossing (reading 3's tail into page 2's
        # reading 0), so the manual-review net reports it.
        report = _scan(self._slug_then_unrelated_first_line())
        assert report.findings == []
        assert report.warnings == [crossing(2)]

    def test_off_page_slug_at_a_page_break_end_to_end(self, tmp_path) -> None:
        # The same control through the CLI, which must pass the Text layer
        # its seam readings: manual review (2), not a finding (1).
        pdf = tmp_path / "slug.pdf"
        doc = self._slug_then_unrelated_first_line()
        doc.save(pdf)
        doc.close()
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "SSN", "value": SSN}]))
        result = run_verify(pdf, rules)
        assert result.returncode == 2, result.stdout
        assert report_lines(result.stdout) == []

    def test_a_split_across_a_page_break_stays_hard_beside_a_slug(self) -> None:
        # The visible readings keep the visible last line: the slug below
        # page 1 does not hide the split, and is not part of it.
        doc = fitz.open()
        p1 = doc.new_page()
        p1.insert_text((72, 780), "Employee SSN 123-45-")
        p1.insert_text((72, 900), "JOB-4471 slug")          # below the page
        doc.new_page().insert_text((72, 60), "6789 was on file.")
        assert _where(_scan(doc)) == [seam(1, 2)]

    def test_visible_line_and_off_page_slug_on_one_baseline(self, tmp_path) -> None:
        # A visible line ending '123-45' and an off-page slug starting
        # '-6789' on the same baseline, far apart. The gap is a column
        # break, and the slug's segment holds no visible glyph, so reading
        # 4 leaves it to reading 3: no hard finding for a value rule or the
        # SSN pattern rule. Reading 9 keeps the line whole, so the value
        # they form is reviewed (owner decision, 2026-10-11).
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 300), "Invoice 123-45")
        page.insert_text((640, 300), "-6789 slug")             # past x=595
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "Any SSN", "class": "ssn"}]))
        readings = extract_visual_text(page)
        assert readings[4] == readings[0] == "Invoice123-45", readings
        assert readings[3] == "-6789slug", readings
        assert readings[9] == "Invoice123-45\n-6789slug", readings
        report = _scan(doc, patterns=load_rules(rules).patterns)
        assert report.findings == []
        assert [w.code for w in report.warnings] == ["REVIEW_CROSS_LINE", "REVIEW_FUSED_PATTERN"]

    @pytest.mark.parametrize("banner_at", [(640, 200), (50, 900)])   # right of, below the page
    def test_large_off_page_text_does_not_merge_visible_fine_print(
            self, banner_at, tmp_path) -> None:
        # Clean control: two visible 6 pt lines 7 pt apart, their glyphs
        # interleaved ('1 3 5 7 9' / '2 4 6 8'), beside four 30 pt lines
        # off the page. Taken from every glyph of reading 4, the line
        # tolerance would merge the two fine-print lines into '123-45-6789';
        # it is the visible text's own.
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Clean schedule of parts", fontsize=10)
        for i, char in enumerate(SSN):
            page.insert_text((200 + i * 3.6, 400 if i % 2 == 0 else 407), char, fontsize=6)
        for k in range(4):
            page.insert_text((banner_at[0], banner_at[1] + 40 * k), "ARCHIVE COPY", fontsize=30)
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "Any SSN", "class": "ssn"}]))
        readings = extract_visual_text(page)
        assert readings[4] == readings[0], readings       # no visible line gains a glyph
        report = _scan(doc, patterns=load_rules(rules).patterns)
        assert report.findings == []


class TestPageBreakAtTheVisibleEdge:
    """A value split at a page break whose first half lies at the visible
    edge: page 1's last line runs past the right edge of the media box
    (hard: it is the visible text's last line, read across the edge) or
    lies wholly past it (manual review: a slug beside or below a page is
    not the start of the next page's text). Never silent, with or without
    a slug opening page 2, at /Rotate 0 and 270."""

    @pytest.mark.parametrize("rotate", [0, 270])
    @pytest.mark.parametrize("layout", BREAK_LAYOUTS)
    def test_never_silent(self, layout: str, rotate: int, tmp_path) -> None:
        pdf = tmp_path / "break.pdf"
        page_break_at_edge(pdf, layout, rotate)
        report = _scan(fitz.open(pdf))
        if layout.startswith("straddling"):
            assert _where(report) == [seam(1, 2)]
            assert report.warnings == []
        else:
            assert report.findings == []
            assert report.warnings == [crossing(2)]



def _slug_beside_visible_text(visible: str, visible_x: float, slug_x: float,
                              rotate: int = 0) -> fitz.Document:
    """Page 1: a visible line, and a page number or footer *visible* at
    *visible_x* on a baseline it shares with an off-page slug 'Job 123-45'
    at *slug_x* (past the 595 pt right edge); page 2 opens with '6789'."""
    doc = fitz.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 300), "Quarterly report, page one.")
    p1.insert_text((visible_x, 830), visible)
    p1.insert_text((slug_x, 830), "Job 123-45")
    doc.new_page(width=595, height=842).insert_text((72, 60), "6789 units shipped in March.")
    if rotate:
        doc[0].set_rotation(rotate)
    return fitz.open("pdf", doc.tobytes())


# (visible text, its x, the slug's x, /Rotate): the slug is a column-break
# segment of its own, holding no visible glyph.
SLUG_BESIDE_VISIBLE = {
    "page-number": ("7", 560, 640, 0),
    "page-number-rotated-90": ("7", 560, 640, 90),
    "footer": ("Confidential - internal use", 300, 700, 0),
    "page-number-far-away": ("7", 300, 2000, 0),
}


class TestSlugBesideVisibleText:
    """A slug wholly off the page on a baseline it shares with visible
    text (a page number, a footer). Reading 4 keeps only the column-break
    segments that hold a visible glyph, so the slug is reading 3's alone:
    it never meets the next page's first line at a hard seam, only in the
    manual-review net, and a value it forms with that visible text on
    their line is reviewed too, never a finding."""

    @pytest.mark.parametrize("layout", sorted(SLUG_BESIDE_VISIBLE))
    def test_no_hard_seam(self, layout: str) -> None:
        visible, visible_x, slug_x, rotate = SLUG_BESIDE_VISIBLE[layout]
        doc = _slug_beside_visible_text(visible, visible_x, slug_x, rotate)
        readings = extract_visual_text(doc[0])
        assert "Job" not in readings[4] and "Job123-45" in readings[3], readings
        report = _scan(doc)
        assert report.findings == []
        assert report.warnings == [crossing(2)]

    def test_no_hard_seam_end_to_end(self, tmp_path) -> None:
        pdf = tmp_path / "slug.pdf"
        doc = _slug_beside_visible_text(*SLUG_BESIDE_VISIBLE["page-number"])
        doc.save(pdf)
        doc.close()
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "SSN", "value": SSN}]))
        result = run_verify(pdf, rules)
        assert result.returncode == 2, result.stdout
        assert report_lines(result.stdout) == []

    def test_slug_at_a_normal_gap_is_read_as_one_line(self) -> None:
        # The limit: with no column break between them ('Page 7' and the
        # slug a word space apart), the line reads 'Page7Job123-45', which
        # nothing tells apart from a value running past the edge, so its
        # seam with the next page is hard (the case library pins it as a
        # false alarm, false-alarm.slug-beside-visible-line).
        doc = _slug_beside_visible_text("Page 7", 560, 603)
        readings = extract_visual_text(doc[0])
        assert readings[4].endswith("Page7Job123-45"), readings
        assert _where(_scan(doc)) == [seam(1, 2)]


    @pytest.mark.parametrize("rotate", [0, 270])
    @pytest.mark.parametrize("gap", sorted(BASELINE_GAPS))
    def test_visible_and_far_off_page_parts_of_a_line_are_reviewed(
            self, gap: str, rotate: int, tmp_path) -> None:
        # The case library's 'Acct 123-45' at the margin and '6789 Job' a
        # column break past the edge, font-coded: no hard finding (nothing
        # ties the halves), but reviewed, never silent (owner decision,
        # 2026-10-11). Reading 9 alone joins them.
        pdf = tmp_path / "baseline.pdf"
        visible_beside_off_page(pdf, gap, rotate)
        doc = fitz.open(pdf)
        readings = extract_visual_text(doc[0])
        assert "6789" not in readings[4] and "Acct123-45\n6789Job" in readings[9], readings
        report = _scan(doc)
        assert report.findings == []
        assert report.warnings == [line(1)]



class TestValueCarriedOffThePage:
    """A value whose second half is wholly off the page: above the next
    page's top edge, or on a line below the page's bottom edge. Reading 9
    reads every horizontal glyph, visible or not, so it is reviewed, never
    silent and never a finding (owner decision, 2026-10-11)."""

    @pytest.mark.parametrize("rotate", [0, 270])
    @pytest.mark.parametrize("layout", OFF_PAGE_CARRY)
    def test_reviewed(self, layout: str, rotate: int, tmp_path) -> None:
        pdf = tmp_path / "carry.pdf"
        value_carried_off_page(pdf, layout, rotate)
        report = _scan(fitz.open(pdf))
        assert report.findings == []
        assert report.warnings == [crossing(2) if layout == "second-half-above" else line(1)]


class TestTableRowBesideOffPageText:
    """Clean controls: a font-coded table row 'Item | 123 | 45 | 6789'
    inside the crop box, beside wide single-digit cells or a letter-spaced
    slug past it on the same baseline. The column breaks of a line read
    across the edge are set by its visible glyphs, so the off-page text's
    wide gaps cannot fuse the cells into a hard value."""

    @pytest.mark.parametrize("rotate", [0, 270])
    @pytest.mark.parametrize("layout", TABLE_ROW_OFF_PAGE)
    def test_no_hard_finding(self, layout: str, rotate: int, tmp_path) -> None:
        pdf = tmp_path / "row.pdf"
        table_row_beside_off_page(pdf, layout, rotate)
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "Any SSN", "class": "ssn"}]))
        doc = fitz.open(pdf)
        readings = extract_visual_text(doc[0])
        assert "123\n45\n6789" in readings[4], readings
        report = _scan(doc, patterns=load_rules(rules).patterns)
        assert report.findings == []



class TestScatteredValues:
    """The review net assembles a value from two or three pieces of nearby
    lines, in any order (SecretMatcher.scattered): around a header or
    footer, within a page or across a page break, or with its halves in
    reverse order. Review only, never a finding (owner decision,
    2026-10-11); Phase 5's geometry matching is the hard-tier fix."""

    @pytest.mark.parametrize("rotate", [0, 270])
    @pytest.mark.parametrize("layout", SCATTERED)
    def test_reviewed(self, layout: str, rotate: int, tmp_path) -> None:
        pdf = tmp_path / "scattered.pdf"
        value_scattered(pdf, layout, rotate)
        report = _scan(fitz.open(pdf))
        assert report.findings == []
        assert report.warnings == [crossing(2) if layout == "furniture-at-break" else line(1)]

    def test_matcher_cost_grows_linearly_with_lines(self) -> None:
        # A dense numeric page with many value rules: eight times the lines
        # must cost about eight times as much, not sixty-four (the window
        # bounds the pairs each line is compared with). Deterministic: the
        # cost is the number of source lines executed in the matcher's
        # module (every loop iteration there, whatever it touches), not
        # wall-clock time, so machine load cannot flake it. Bound: under
        # twelve times (linear is eight) at each of two steps, 62 -> 500 ->
        # 4000 lines; each run stops once past the bound, so a quadratic
        # fails in seconds instead of running for minutes.
        import random
        import sys
        from types import FrameType

        from redaction_verifier.matching import values

        rng = random.Random(1)
        m = SecretMatcher([Secret(f"s{i}", "%09d" % rng.randrange(10**9)) for i in range(150)])
        source = values.__file__
        executed = [0]
        budget = [math.inf]

        class OverBudget(Exception):
            pass

        def local(frame: FrameType, event: str, arg: Any) -> Any:
            if event == "line":
                executed[0] += 1
                if executed[0] > budget[0]:
                    raise OverBudget
            return local

        def outer(frame: FrameType, event: str, arg: Any) -> Any:
            return local if frame.f_code.co_filename == source else None

        def cost(count: int) -> int:
            ls = ["%d" % rng.randrange(10 ** rng.randint(1, 8)) for _ in range(count)]
            executed[0] = 0
            previous = sys.gettrace()
            sys.settrace(outer)
            try:
                m.scattered(ls, 5)
            except OverBudget:
                pass
            finally:
                sys.settrace(previous)
            return executed[0]

        tiny = cost(62)
        assert tiny > 0
        budget[0] = 12 * tiny
        small = cost(500)
        assert small < 12 * tiny
        budget[0] = 12 * small
        assert cost(4000) < 12 * small

    def test_matcher_bounds(self) -> None:
        m = SecretMatcher([Secret("SSN", normalize_string(SSN))])
        assert m.scattered(["ssn12345", "header", "6789end"], 5) == ["SSN"]
        assert m.scattered(["6789end", "ssn12345"], 5) == ["SSN"]           # reversed
        assert m.scattered(["x123", "45", "6789y"], 5) == ["SSN"]           # three pieces
        # Too far apart for the window, or needing four pieces: not found.
        assert m.scattered(["x12345", "a", "b", "c", "d", "6789y"], 5) == []
        assert m.scattered(["x12", "34", "56", "789y"], 5) == []
        # Across a page break, a piece must come from each side.
        assert m.scattered(["x12345", "6789y", "next page"], 5, across=2) == []
        assert m.scattered(["x12345", "next page", "6789y"], 5, across=1) == ["SSN"]


class TestPageBreaksOcr:
    """The same rules on the OCR path: two genuine readings that can differ."""

    @staticmethod
    def _ocr(readings: list[list[str]]) -> ScanReport:
        doc = fitz.open()
        for _ in readings:
            doc.new_page()
        by_page = dict(enumerate(readings))
        return _scan(doc, layer="OCR", hard_variants=ocr.OCR_GENUINE_READINGS,
                     seam_variants=None, tails_into_next=(), scatter_readings=(),
                     extractor=lambda page: by_page[page.number])

    def test_seam_in_the_second_reading_only(self) -> None:
        report = self._ocr([["", "x\nSSN 123-45-"], ["", "6789 end"]])
        assert _where(report) == [seam(1, 2)]
        assert report.warnings == []

    def test_blank_middle_page_is_manual_review(self) -> None:
        report = self._ocr([["x\nSSN 123-45-"] * 2, [""] * 2, ["6789 end"] * 2])
        assert report.findings == []
        assert report.warnings == [crossing(3, layer="OCR")]

    def test_joined_crossing_in_one_reading(self) -> None:
        report = self._ocr([["nothing", "ref 123\n45-"], ["page two", "6789 end"]])
        assert report.findings == []
        assert report.warnings == [crossing(2, layer="OCR")]


# Vision's observations as macOS 27 returned them for case-library pages
# (A4 at 300 dpi: 2479 x 3508 px), in normalized image coordinates.
A4_PX = (2479, 3508)
# One digit per line, 1 to 9, read as one line rotated 270 degrees.
STACK = ocr.Observation("199140190", (0.179, 0.891), (0.179, 0.747), (0.167, 0.891))
# Genuinely rotated text: 90 and 270 degrees.
ROTATED_90 = ocr.Observation(f"SSN {SSN}", (0.490, 0.406), (0.490, 0.510), (0.506, 0.406))
ROTATED_270 = ocr.Observation("123-45-", (0.854, 0.642), (0.854, 0.594), (0.835, 0.642))
HORIZONTAL = ocr.Observation("body text here", (0.121, 0.891), (0.239, 0.891), (0.121, 0.878))


def _as(observation: ocr.Observation, text: str) -> ocr.Observation:
    return ocr.Observation(text, observation.top_left, observation.top_right,
                           observation.bottom_left)


class TestOcrVerticalLines:
    """On macOS 27, Vision reads a one-character-per-line stack as one
    sideways line (often a misread: '199140190' for 1 to 9), the
    orientation it gives rotated text. A vertical line is a line of the
    page only if an upright re-read of its box confirms it; otherwise it
    is manual review, never hard and never silent. Vision is stubbed, so
    these run without macOS."""

    def test_vertical_is_measured_in_pixels(self) -> None:
        for observation in (STACK, ROTATED_90, ROTATED_270):
            assert ocr._is_vertical(observation, *A4_PX)
        assert not ocr._is_vertical(HORIZONTAL, *A4_PX)
        # A slanted line: steep on A4, shallow on an image 40 times as wide.
        slanted = ocr.Observation("x", (0.10, 0.5), (0.11, 0.6), (0.09, 0.5))
        assert ocr._is_vertical(slanted, *A4_PX)
        assert not ocr._is_vertical(slanted, A4_PX[0] * 40, A4_PX[1])

    @staticmethod
    def _confirm(monkeypatch: pytest.MonkeyPatch, passes, reads) -> list[list[bool]]:
        calls: list[fitz.IRect] = []

        def upright(image: fitz.Page, box: fitz.IRect) -> tuple[list[str], list[str]]:
            calls.append(box)
            return reads(len(calls))

        monkeypatch.setattr(ocr, "_upright_reads", upright)
        doc = fitz.open()
        try:
            png = doc.new_page().get_pixmap().tobytes("png")
        finally:
            doc.close()
        return ocr._confirm_vertical(png, *A4_PX, passes)

    def test_a_re_read_in_the_same_pass_confirms_a_vertical_line(self, monkeypatch) -> None:
        confirmed = self._confirm(
            monkeypatch, ([HORIZONTAL, ROTATED_90], [ROTATED_90]),
            lambda call: ([f"x {ROTATED_90.text} y"], ["something else"]))
        assert confirmed == [[True, True], [False]]

    def test_an_unconfirmed_line_gets_one_wider_re_read(self, monkeypatch) -> None:
        confirmed = self._confirm(monkeypatch, ([ROTATED_270], []),
                                  lambda call: (["123-45-"] if call == 2 else ["123-4ął"], []))
        assert confirmed == [[True], []]

    def test_past_the_cap_a_vertical_line_stays_unconfirmed(self, monkeypatch) -> None:
        # One re-read allowed: the first line takes it, failing in both
        # rounds, so the second line is never re-read.
        monkeypatch.setattr(ocr, "MAX_UPRIGHT_REREADS", 1)
        confirmed = self._confirm(
            monkeypatch, ([ROTATED_90, ROTATED_270, HORIZONTAL], []),
            lambda call: ([], []) if call == 1 else pytest.fail("re-read past the cap"))
        assert confirmed == [[False, False, True], []]

    def test_a_failed_re_read_leaves_the_line_unconfirmed(self, monkeypatch) -> None:
        def fail(call: int) -> tuple[list[str], list[str]]:
            raise RuntimeError("Apple Vision error")

        confirmed = self._confirm(monkeypatch, ([ROTATED_90, HORIZONTAL], []), fail)
        assert confirmed == [[False, True], []]

    def test_a_mupdf_error_in_a_re_read_fails_the_page(self, monkeypatch) -> None:
        # As on main: dropping it would move a failed page (exit 2) to
        # whatever the rest of the page gives, possibly clean (#99).
        def refuse(call: int) -> tuple[list[str], list[str]]:
            raise fitz.mupdf.FzErrorBase(5, "Overly large image")

        with pytest.raises(fitz.mupdf.FzErrorBase):   # the page fails: PAGE_FAILED
            self._confirm(monkeypatch, ([ROTATED_90, HORIZONTAL], []), refuse)

    def test_a_timed_out_re_read_fails_the_page(self, monkeypatch) -> None:
        # Not dropped like a Vision error: Vision is stuck, and each later
        # re-read would wait out a deadline of its own (#115).
        def stuck(call: int) -> tuple[list[str], list[str]]:
            raise ocr.VisionTimeout("Apple Vision did not return")

        with pytest.raises(ocr.VisionTimeout):   # the page fails: PAGE_FAILED
            self._confirm(monkeypatch, ([ROTATED_90, HORIZONTAL], []), stuck)

    def test_a_bug_in_a_re_read_is_not_swallowed(self, monkeypatch) -> None:
        def bug(call: int) -> tuple[list[str], list[str]]:
            raise AttributeError("no such method")

        with pytest.raises(AttributeError):   # the page fails: PAGE_FAILED
            self._confirm(monkeypatch, ([ROTATED_90], []), bug)

    def test_the_re_read_crop_is_padded_by_the_line_thickness(self) -> None:
        # A slanted line, about 43 px thick and 720 px long: the first crop
        # adds its thickness plus 2 pt around the line's own box, not a
        # margin as large as the box's short side (about 520 px here).
        slanted = ocr.Observation("x", (0.3, 0.3), (0.5, 0.45), (0.29, 0.31))
        corners = [(x * A4_PX[0], (1 - y) * A4_PX[1]) for x, y in (
            (0.3, 0.3), (0.5, 0.45), (0.29, 0.31), (0.49, 0.46))]
        thickness = math.hypot(corners[0][0] - corners[2][0], corners[0][1] - corners[2][1])
        x0, x1 = min(x for x, _ in corners), max(x for x, _ in corners)
        first = ocr._box(slanted, *A4_PX, ocr.REREAD_PADS[0])
        margin = (first.width - (x1 - x0)) / 2
        assert abs(margin - (thickness + 2 * ocr.OCR_DPI / 72)) <= 1.5
        assert ocr._box(slanted, *A4_PX, ocr.REREAD_PADS[1]).contains(first)

    def test_a_re_read_counts_only_lines_read_horizontally(self, monkeypatch) -> None:
        # Turned upright, a stack is a row of sideways glyphs; Vision may
        # read that sideways again, which confirms nothing.
        # A line near 45 degrees stays near 45 turned upright: it counts.
        # (On the 100 x 200 turned crop: 60 px across, 70 up, about 49 degrees.)
        slanted = ocr.Observation("789", (0.2, 0.2), (0.8, 0.55), (0.25, 0.15))
        monkeypatch.setattr(ocr, "_vision_recognize_batch", lambda png: (
            [_as(STACK, "123"), _as(HORIZONTAL, "456"), slanted], []))
        doc = fitz.open()
        try:
            image = doc.new_page(width=200, height=100)
            reads = ocr._upright_reads(image, fitz.IRect(0, 0, 200, 100))
            assert reads == (["456", "789", "456", "789"], [])
        finally:
            doc.close()

    def test_genuine_readings_split_an_unconfirmed_line_one_character_per_line(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(ocr, "_vision_recognize_batch",
                            lambda png: ([HORIZONTAL, STACK, ROTATED_90],) * 2)
        monkeypatch.setattr(ocr, "_upright_reads",
                            lambda image, box: ([ROTATED_90.text, "1 G G"],) * 2)
        monkeypatch.setattr(ocr, "_extra_readings", lambda *args: ["turned on", "turned off"])
        doc = fitz.open()
        try:
            readings = ocr.extract_ocr_text(doc.new_page())  # A4 by default
        finally:
            doc.close()
        split = "\n".join([HORIZONTAL.text, *STACK.text, ROTATED_90.text])
        fused = "\n".join([HORIZONTAL.text, STACK.text, ROTATED_90.text])
        assert len(readings) == 6 and ocr.OCR_GENUINE_READINGS == 2
        assert readings == [split, split, fused, fused, "turned on", "turned off"]

    def _scan(self, tmp_path, monkeypatch, pages: list[ocr.Observation], rule: dict,
              upright: str, extra: str = "") -> tuple[int, dict]:
        # The CLI end to end, with Vision stubbed: one observation per page,
        # *upright* what every upright re-read returns and *extra* each
        # pass's turned and unstacked reading.
        monkeypatch.setattr(verify, "_OCR_IMPORTS_OK", True)
        per_page = iter(pages)
        monkeypatch.setattr(ocr, "_vision_recognize_batch",
                            lambda png: ([next(per_page)],) * 2)
        monkeypatch.setattr(ocr, "_upright_reads", lambda image, box: ([upright],) * 2)
        monkeypatch.setattr(ocr, "_extra_readings", lambda *args: [extra, extra])
        doc = fitz.open()
        for _ in pages:
            doc.new_page().insert_text((72, 72), "nothing here")
        pdf = tmp_path / "page.pdf"
        doc.save(pdf)
        doc.close()
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([rule]))
        out = tmp_path / "report.json"
        code = verify.main(["--target", str(pdf), "--secrets", str(rules),
                            "--json", str(out)])
        return code, json.loads(out.read_text())

    @pytest.mark.parametrize("rule, code", [
        ({"name": "Any SSN", "class": "ssn"}, "REVIEW_FUSED_PATTERN"),
        ({"name": "SSN", "value": SSN}, "REVIEW_CROSS_LINE"),
    ])
    def test_an_unconfirmed_vertical_value_is_manual_review(
        self, tmp_path, monkeypatch, rule, code
    ) -> None:
        exit_code, data = self._scan(tmp_path, monkeypatch, [_as(STACK, "123456789")],
                                     rule, upright="")
        assert exit_code == 2
        assert data["findings"] == []
        assert (code, "OCR", rule["name"]) in {
            (w["code"], w["layer"], w["rule"]) for w in data["warnings"]}

    @pytest.mark.parametrize("rule", [
        {"name": "Any SSN", "class": "ssn"}, {"name": "SSN", "value": SSN}])
    def test_confirmed_rotated_text_is_a_hard_finding(self, tmp_path, monkeypatch, rule) -> None:
        exit_code, data = self._scan(tmp_path, monkeypatch, [ROTATED_90], rule,
                                     upright=ROTATED_90.text)
        assert exit_code == 1
        assert [(f["layer"], f["rule"]) for f in data["findings"]] == [("OCR", rule["name"])]

    def test_an_unconfirmed_line_still_matches_a_rule_needing_a_run_of_separators(
        self, tmp_path, monkeypatch
    ) -> None:
        # Collapsing separators turns 'REF--482913' into 'REF-482913';
        # the raw line itself must still be matched (it was hard before
        # vertical lines needed confirming, so it must not become silent).
        exit_code, data = self._scan(
            tmp_path, monkeypatch, [_as(STACK, "REF--482913")],
            {"name": "Ref", "pattern": r"REF--\d{6}"}, upright="")
        assert exit_code == 2
        assert data["findings"] == []
        assert ("REVIEW_FUSED_PATTERN", "OCR", "Ref") in {
            (w["code"], w["layer"], w["rule"]) for w in data["warnings"]}

    def test_an_unconfirmed_pattern_across_a_page_break_is_manual_review(
        self, tmp_path, monkeypatch
    ) -> None:
        # Split one character per line, the genuine readings cannot join
        # it; the cross-page scan reads Vision's raw line instead.
        exit_code, data = self._scan(
            tmp_path, monkeypatch, [_as(STACK, "123-45-"), _as(STACK, "6789")],
            {"name": "Any SSN", "class": "ssn"}, upright="")
        assert exit_code == 2
        assert data["findings"] == []
        assert ("REVIEW_FUSED_PATTERN", "OCR") in {
            (w["code"], w["layer"]) for w in data["warnings"]}


def _stack_image(digits: str = "123456789") -> tuple[fitz.Document, fitz.Page]:
    """A page image (one point per pixel, as _extra_readings builds one)
    of *digits* one per line, rendered at 300 dpi like a scan."""
    src = fitz.open()
    page = src.new_page()
    for i, digit in enumerate(digits):
        page.insert_text((100, 100 + 14 * i), digit, fontsize=11)
    pix = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY)
    src.close()
    doc = fitz.open()
    image = doc.new_page(width=pix.width, height=pix.height)
    image.insert_image(image.rect, stream=pix.tobytes("png"))
    return doc, image


class TestOcrExtraReadings:
    """A page Vision reads any line of vertically gets two more
    reconstructed readings: the page turned 90 and 270 degrees (Vision
    reads sideways text only in part, #67), and each unconfirmed vertical
    line unstacked into one line (#61). Never hard. Vision is stubbed."""

    @staticmethod
    def _png() -> bytes:
        doc = fitz.open()
        try:
            return doc.new_page().get_pixmap().tobytes("png")
        finally:
            doc.close()

    def test_a_page_with_no_vertical_line_costs_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr(ocr, "_vision_recognize_batch",
                            lambda png: pytest.fail("no extra Vision call"))
        passes = ([HORIZONTAL], [HORIZONTAL])
        assert ocr._extra_readings(self._png(), *A4_PX, passes, [[True], [True]]) == ["", ""]

    def test_turned_and_unstacked_reads_per_pass(self, monkeypatch) -> None:
        # Calls in order: the page turned 90, turned 270, then one
        # unstacked line. Turned, a horizontal line of the page is
        # vertical, and is left out.
        replies = iter([
            ([_as(HORIZONTAL, "turned 90 on"), _as(STACK, "page line")],
             [_as(HORIZONTAL, "turned 90 off")]),
            ([_as(HORIZONTAL, "turned 270 on")], []),
            ([_as(HORIZONTAL, "unstacked on")], [_as(HORIZONTAL, "unstacked off")]),
        ])
        monkeypatch.setattr(ocr, "_vision_recognize_batch", lambda png: next(replies))
        monkeypatch.setattr(ocr, "_unstack", lambda image, box: fitz.Pixmap(
            fitz.csGRAY, fitz.IRect(0, 0, 4, 4), False))
        passes = ([HORIZONTAL, STACK], [HORIZONTAL])
        on, off = ocr._extra_readings(self._png(), *A4_PX, passes, [[True, False], [True]])
        assert on.split("\n") == ["turned 90 on", "turned 270 on", "unstacked on"]
        assert off.split("\n") == ["turned 90 off"]   # its pass confirmed every line

    def test_a_failed_extra_read_loses_only_itself(self, monkeypatch) -> None:
        def fail(png: bytes) -> Any:
            raise RuntimeError("Apple Vision error")

        monkeypatch.setattr(ocr, "_vision_recognize_batch", fail)
        passes = ([STACK], [])
        assert ocr._extra_readings(self._png(), *A4_PX, passes, [[False], []]) == ["", ""]

    def test_past_the_cap_a_line_is_not_unstacked(self, monkeypatch) -> None:
        monkeypatch.setattr(ocr, "MAX_UNSTACKS", 0)
        monkeypatch.setattr(ocr, "_vision_recognize_batch", lambda png: ([], []))
        monkeypatch.setattr(ocr, "_unstack", lambda image, box: pytest.fail("unstacked"))
        assert ocr._extra_readings(self._png(), *A4_PX, ([STACK], []), [[False], []]) == ["", ""]

    def test_unstack_lays_a_column_out_as_one_line(self) -> None:
        doc, image = _stack_image()
        try:
            strip = ocr._unstack(image, image.rect.irect)
        finally:
            doc.close()
        assert strip is not None and strip.width > 3 * strip.height
        samples, width = strip.samples, strip.width
        inked = [min(samples[x::width]) < 128 for x in range(width)]
        glyphs = sum(1 for x in range(width) if inked[x] and (x == 0 or not inked[x - 1]))
        assert glyphs == 9   # nine digits, side by side

    def test_a_mupdf_error_in_an_unstack_loses_only_that_read(self, monkeypatch) -> None:
        # MuPDF's errors are not RuntimeErrors: one escaping would fail the
        # page and drop its genuine hard findings.
        def refuse(image: fitz.Page, box: fitz.IRect) -> fitz.Pixmap:
            raise fitz.mupdf.FzErrorBase(5, "Overly large image")

        monkeypatch.setattr(ocr, "_unstack", refuse)
        monkeypatch.setattr(ocr, "_vision_recognize_batch", lambda png: ([], []))
        assert ocr._extra_readings(self._png(), *A4_PX, ([STACK], []), [[False], []]) == ["", ""]

    def test_a_tall_run_among_thin_ones_is_no_stack(self) -> None:
        # A photo-like block and thin rules: laid out, a strip MuPDF may refuse.
        doc = fitz.open()
        image = doc.new_page(width=200, height=900)
        image.draw_rect(fitz.Rect(20, 20, 180, 420), color=(0, 0, 0), fill=(0, 0, 0))
        for y in range(450, 890, 40):
            image.draw_line(fitz.Point(20, y), fitz.Point(180, y), width=2)
        try:
            assert ocr._unstack(image, image.rect.irect) is None
        finally:
            doc.close()

    def test_an_oversized_strip_is_not_built(self, monkeypatch) -> None:
        monkeypatch.setattr(ocr, "MAX_UNSTACK_PIXELS", 100)
        doc, image = _stack_image()
        try:
            assert ocr._unstack(image, image.rect.irect) is None
        finally:
            doc.close()

    def test_a_hyphen_among_digits_is_still_a_stack(self) -> None:
        # A hyphen is a short run of ink; the median run decides, so a
        # value with separators is not taken for a photo among rules.
        doc, image = _stack_image("123-45-6789")
        try:
            assert ocr._unstack(image, image.rect.irect) is not None
        finally:
            doc.close()

    def test_a_mupdf_error_in_a_turned_reading_loses_only_it(self, monkeypatch) -> None:
        def refuse(png: bytes) -> Any:
            raise fitz.mupdf.FzErrorBase(5, "Overly large image")

        monkeypatch.setattr(ocr, "_vision_recognize_batch", refuse)
        monkeypatch.setattr(ocr, "_unstack", lambda image, box: None)
        assert ocr._extra_readings(self._png(), *A4_PX, ([STACK], []), [[False], []]) == ["", ""]

    def test_a_page_image_mupdf_refuses_gives_no_extra_reading(self, monkeypatch) -> None:
        monkeypatch.setattr(ocr, "_vision_recognize_batch",
                            lambda png: pytest.fail("no page image, no read"))
        assert ocr._extra_readings(b"not a png", *A4_PX, ([STACK], []), [[False], []]) \
            == ["", ""]

    def test_a_bug_in_a_turned_reading_fails_the_page(self, monkeypatch) -> None:
        def bug(png: bytes) -> Any:
            raise AttributeError("no such method")

        monkeypatch.setattr(ocr, "_vision_recognize_batch", bug)
        with pytest.raises(AttributeError):   # the page fails: PAGE_FAILED
            ocr._extra_readings(self._png(), *A4_PX, ([STACK], []), [[False], []])

    def test_a_timed_out_extra_read_fails_the_page(self, monkeypatch) -> None:
        def stuck(png: bytes) -> Any:
            raise ocr.VisionTimeout("Apple Vision did not return")

        monkeypatch.setattr(ocr, "_vision_recognize_batch", stuck)
        with pytest.raises(ocr.VisionTimeout):   # the page fails: PAGE_FAILED
            ocr._extra_readings(self._png(), *A4_PX, ([STACK], []), [[False], []])

    def test_a_bug_in_an_extra_reading_fails_the_page(self, monkeypatch) -> None:
        def bug(image: fitz.Page, box: fitz.IRect) -> fitz.Pixmap:
            raise AttributeError("no such method")

        monkeypatch.setattr(ocr, "_vision_recognize_batch", lambda png: ([], []))
        monkeypatch.setattr(ocr, "_unstack", bug)
        with pytest.raises(AttributeError):   # the page fails: PAGE_FAILED
            ocr._extra_readings(self._png(), *A4_PX, ([STACK], []), [[False], []])

    def test_fewer_than_three_rows_of_ink_is_no_stack(self) -> None:
        doc, image = _stack_image("12")
        try:
            assert ocr._unstack(image, image.rect.irect) is None
        finally:
            doc.close()

    def test_an_unstacked_value_is_manual_review(self, tmp_path, monkeypatch) -> None:
        # Vision misread the stack; unstacked, it reads as written: the
        # value rule matches there, a reconstruction (#61).
        exit_code, data = TestOcrVerticalLines()._scan(
            tmp_path, monkeypatch, [STACK], {"name": "SSN", "value": SSN},
            upright="", extra="123456789")
        assert exit_code == 2
        assert data["findings"] == []
        assert ("REVIEW_CROSS_LINE", "OCR", "SSN") in {
            (w["code"], w["layer"], w["rule"]) for w in data["warnings"]}


@pytest.mark.skipif(not HAS_OCR, reason="needs the PyObjC Vision bridge (macOS)")
class TestOcrVerticalLinesWithVision:
    """The re-read with Apple Vision itself, on the text a geometric rule
    could not tell from a stack."""

    @staticmethod
    def _genuine(page: fitz.Page) -> list[str]:
        readings = ocr.extract_ocr_text(page)
        return [normalize_string(ln)
                for r in readings[:ocr.OCR_GENUINE_READINGS] for ln in r.split("\n")]

    @pytest.mark.parametrize("rotate, at", [(90, (300, 650)), (270, (300, 150))])
    def test_rotated_digits_stay_one_line(self, rotate, at) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(at, SSN, fontname="cour", fontsize=8, rotate=rotate)
        try:
            assert normalize_string(SSN) in self._genuine(page)
        finally:
            doc.close()

    @pytest.mark.parametrize("rotate, at", [(90, (300, 650)), (270, (300, 150))])
    def test_a_spaced_rotated_line_vision_reads_whole_stays_whole(self, rotate, at) -> None:
        # Spaced digits are as sparse along the line as a stack. Whether
        # Vision reads 8 pt spaced digits whole depends on the OS (macOS 27
        # does; on CI's macOS 26 it did not, at 90 or 270 degrees), so the
        # check is the invariant: a line Vision's own reading holds whole,
        # the genuine readings hold whole too. Vision's own reading is the
        # raw readings only: the turned and unstacked reads after them are
        # of another image (on macOS 26 the page turned holds the line
        # whole where Vision's own reading does not).
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(at, "1 2 3 - 4 5 - 6 7 8 9", fontname="cour", fontsize=8,
                         rotate=rotate)
        try:
            readings = ocr.extract_ocr_text(page)
        finally:
            doc.close()
        genuine, raw = (
            [normalize_string(ln) for r in part for ln in r.split("\n")]
            for part in (readings[:ocr.OCR_GENUINE_READINGS],
                         readings[ocr.OCR_GENUINE_READINGS:2 * ocr.OCR_GENUINE_READINGS]))
        whole = [ln for ln in raw if normalize_string(SSN) in ln]
        assert all(ln in genuine for ln in whole), (raw, genuine)

    def test_a_line_slanted_near_45_degrees_stays_one_line(self) -> None:
        # 135 degrees, 10 pt, near the right edge: turned upright it is
        # still near 45 degrees, which a 45-degree cut on re-reads dropped.
        text, size, angle = f"Employee SSN {SSN} on file", 10, 135
        doc = fitz.open()
        page = doc.new_page()
        length = fitz.get_text_length(text, fontsize=size)
        cos, sin = math.cos(math.radians(angle)), -math.sin(math.radians(angle))
        centre = (page.rect.width - abs(cos) * length / 2 - size - 4, page.rect.height / 2)
        start = fitz.Point(centre[0] - cos * length / 2, centre[1] - sin * length / 2)
        page.insert_text(start, text, fontsize=size, morph=(start, fitz.Matrix(angle)))
        try:
            assert any(normalize_string(SSN) in line for line in self._genuine(page))
        finally:
            doc.close()

    def test_a_digit_stack_is_never_one_line(self) -> None:
        # Digits 1 to 9; a stack of glyphs that look alike turned sideways
        # (0, 1, 8, O, I, H) can still be confirmed (#65).
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):
            page.insert_text((100, 100 + 14 * i), digit, fontsize=11)
        try:
            assert all(len(ln) <= 1 for ln in self._genuine(page) if ln)
        finally:
            doc.close()


@pytest.mark.skipif(not HAS_OCR, reason="needs the PyObjC Vision bridge (macOS)")
class TestVisionBridge:
    """The Apple Vision bridge's image-handler options."""

    def test_options_survive_vision_removing_a_consumed_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # From macOS 27, VNImageRequestHandler's init mutable-copies its
        # options and removes the keys it consumes from the copy. A Python
        # {} bridges as a proxy that raises on removing an absent key, so
        # every OCR page failed (PAGE_FAILED, never exit 0). The isinstance
        # check is the guard on any macOS version (a Python dict is never
        # an NSDictionary); the replay documents what macOS 27 does.
        import Vision
        from Foundation import NSDictionary

        from redaction_verifier.views import ocr

        seen: list[Any] = []
        real_handler = Vision.VNImageRequestHandler

        class _Handler:
            @staticmethod
            def alloc() -> _Handler:
                return _Handler()

            def initWithData_options_(self, data: Any, options: Any) -> Any:
                seen.append(options)
                return real_handler.alloc().initWithData_options_(data, options)

        class _Vision:
            VNImageRequestHandler = _Handler

            def __getattr__(self, name: str) -> Any:
                return getattr(Vision, name)

        monkeypatch.setattr(ocr, "Vision", _Vision())
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 144), f"SSN {SSN}", fontsize=28)

        readings = ocr.extract_ocr_text(page)

        (options,) = seen
        assert isinstance(options, NSDictionary)  # native, not a Python dict
        copied = options.mutableCopy()
        copied.removeObjectForKey_(Vision.VNImageOptionProperties)
        copied.removeObjectForKey_(Vision.VNImageOptionCameraIntrinsics)
        # Genuine and raw readings hold it; no line is vertical, so the
        # turned and unstacked readings are not made.
        assert all(normalize_string(SSN) in normalize_string(text)
                   for text in readings[:2 * ocr.OCR_GENUINE_READINGS])
        assert readings[2 * ocr.OCR_GENUINE_READINGS:] == ["", ""]


class _FakeCandidate:
    def __init__(self, text: str) -> None:
        self._text = text

    def string(self) -> str:
        return self._text


class _FakePoint:
    def __init__(self, x: float, y: float) -> None:
        self.x, self.y = x, y


class _FakeObservation:
    """A horizontal line of text, as Vision reports one."""

    def __init__(self, text: str) -> None:
        self._text = text

    def topCandidates_(self, count: int) -> list[_FakeCandidate]:
        return [_FakeCandidate(self._text)]

    def topLeft(self) -> _FakePoint:
        return _FakePoint(0.1, 0.9)

    def topRight(self) -> _FakePoint:
        return _FakePoint(0.5, 0.9)

    def bottomLeft(self) -> _FakePoint:
        return _FakePoint(0.1, 0.88)


class _RaisingObservation:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def topCandidates_(self, count: int) -> list[_FakeCandidate]:
        raise self._exc


class _UnprintableError(Exception):
    def __repr__(self) -> str:
        raise RuntimeError("repr failed")


class _FakeRequest:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.correction: bool | None = None
        self.observations: list[Any] | None = None
        self.cancelled = False

    def setRecognitionLevel_(self, level: Any) -> None:
        pass

    def setUsesLanguageCorrection_(self, on: bool) -> None:
        self.correction = on

    def results(self) -> list[Any] | None:
        return self.observations

    def cancel(self) -> None:
        self.cancelled = True


def _fake_vision(plans: dict[bool, tuple[str, Any]],
                 made: list[_FakeRequest] | None = None) -> Any:
    """A stand-in for the Vision module. *plans* says, per language
    correction setting, what performRequests does with that request:
    ("text", [lines]) runs its handler with those observations, ("none",
    None) runs it with results() None, ("error", message) runs it with an
    error, ("raises", exc) runs it on an observation whose topCandidates_
    raises *exc*, and ("skip", None) never runs it — all while
    still reporting success. ("hang", event) blocks performRequests
    until *event* is set, as a stalled Vision would (#115), then runs
    nothing. Handlers run on the calling thread here; on
    real Vision they run on worker threads, where an exception escaping
    one aborts the process, so the handler itself must not let one out.
    """

    class _Alloc:
        def __init__(self, make: Any) -> None:
            self._make = make

        def initWithCompletionHandler_(self, handler: Any) -> _FakeRequest:
            request = _FakeRequest(handler)
            if made is not None:
                made.append(request)
            return request

        def initWithData_options_(self, data: Any, options: Any) -> Any:
            return self._make()

    class _ImageHandler:
        def performRequests_error_(
            self, requests: list[_FakeRequest], error: Any
        ) -> tuple[bool, None]:
            for request in requests:
                assert request.correction is not None
                kind, value = plans[request.correction]
                if kind == "text":
                    request.observations = [_FakeObservation(v) for v in value]
                    request.handler(request, None)
                elif kind == "none":
                    request.handler(request, None)
                elif kind == "error":
                    request.handler(request, value)
                elif kind == "raises":
                    request.observations = [_RaisingObservation(value)]
                    request.handler(request, None)
                elif kind == "hang":
                    value.wait()
            return True, None

    class _Request:
        @staticmethod
        def alloc() -> _Alloc:
            return _Alloc(None)

    class _Handler:
        @staticmethod
        def alloc() -> _Alloc:
            return _Alloc(_ImageHandler)

    class _Vision:
        VNRecognizeTextRequest = _Request
        VNImageRequestHandler = _Handler
        VNRequestTextRecognitionLevelAccurate = 0

    return _Vision


class TestVisionBridgeCompletion:
    """A reading counts only once Vision has delivered it: each request's
    completion handler must have run to the end. Fakes stand in for Vision
    and Foundation, so these run on every platform."""

    @staticmethod
    def _recognize(monkeypatch: pytest.MonkeyPatch,
                   plans: dict[bool, tuple[str, Any]]) -> tuple[str, str]:
        from types import SimpleNamespace

        from redaction_verifier.views import ocr

        monkeypatch.setattr(ocr, "Vision", _fake_vision(plans), raising=False)
        monkeypatch.setattr(ocr, "NSData", SimpleNamespace(
            dataWithBytes_length_=lambda data, length: data), raising=False)
        monkeypatch.setattr(ocr, "NSDictionary", SimpleNamespace(
            dictionary=lambda: {}), raising=False)
        on, off = ocr._vision_recognize_batch(b"png")
        return "\n".join(o.text for o in on), "\n".join(o.text for o in off)

    def test_both_handlers_completing_returns_both_readings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        readings = self._recognize(monkeypatch, {
            True: ("text", ["SSN", SSN]), False: ("text", [SSN]),
        })
        assert readings == (f"SSN\n{SSN}", SSN)

    def test_a_page_with_no_text_is_an_empty_reading_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._recognize(monkeypatch, {
            True: ("text", []), False: ("text", []),
        }) == ("", "")

    @pytest.mark.parametrize("plans, message", [
        ({True: ("skip", None), False: ("skip", None)}, "correction on never completed"),
        ({True: ("text", [SSN]), False: ("skip", None)}, "correction off never completed"),
        ({True: ("skip", None), False: ("text", [SSN])}, "correction on never completed"),
        ({True: ("none", None), False: ("text", [SSN])}, "no results"),
        ({True: ("text", [SSN]), False: ("error", "boom")}, "boom"),
        ({True: ("raises", AttributeError("x")), False: ("text", [SSN])},
         "completion handler raised AttributeError"),
        # On a Vision worker thread even these would abort the process.
        ({True: ("raises", SystemExit(0)), False: ("text", [SSN])},
         "completion handler raised SystemExit"),
        ({True: ("raises", _UnprintableError()), False: ("text", [SSN])},
         "correction on never completed"),
    ], ids=["neither-ran", "off-never-ran", "on-never-ran", "results-none", "error",
            "handler-raised", "handler-raised-systemexit", "handler-raised-unprintable"])
    def test_a_request_that_did_not_complete_raises(
        self, monkeypatch: pytest.MonkeyPatch,
        plans: dict[bool, tuple[str, Any]], message: str,
    ) -> None:
        # Reported success, but a reading was never delivered: an empty
        # reading here would let the page pass as read with nothing on it.
        with pytest.raises(RuntimeError, match=message):
            self._recognize(monkeypatch, plans)


class TestVisionDeadline:
    """performRequests has no timeout of its own: a stalled Vision once
    held a test run for about 25 minutes with no CPU in use (#115). Each
    Vision call is waited on for at most VISION_CALL_TIMEOUT_S, and a page
    it times out on fails (PAGE_FAILED, exit 2), never hangs and never
    passes. Fakes stand in for Vision and Foundation, so these run on
    every platform."""

    DEADLINE = 0.5

    @pytest.fixture
    def stalled(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """A Vision whose performRequests never returns until the test
        ends (released, so no stalled thread outlives the test)."""
        from types import SimpleNamespace

        release = threading.Event()
        made: list[_FakeRequest] = []
        monkeypatch.setattr(ocr, "VISION_CALL_TIMEOUT_S", self.DEADLINE)
        monkeypatch.setattr(ocr, "Vision", _fake_vision(
            {True: ("hang", release), False: ("hang", release)}, made), raising=False)
        monkeypatch.setattr(ocr, "NSData", SimpleNamespace(
            dataWithBytes_length_=lambda data, length: data), raising=False)
        monkeypatch.setattr(ocr, "NSDictionary", SimpleNamespace(
            dictionary=lambda: {}), raising=False)
        try:
            yield SimpleNamespace(release=release, made=made)
        finally:
            release.set()
            for thread in list(ocr._stalled):
                thread.join(10)
            assert not any(thread.is_alive() for thread in ocr._stalled)

    def test_a_call_vision_never_finishes_raises_at_the_deadline(self, stalled) -> None:
        start = time.monotonic()
        with pytest.raises(ocr.VisionTimeout, match="did not return within 0.5 s"):
            ocr._vision_recognize_batch(b"png")
        elapsed = time.monotonic() - start
        assert self.DEADLINE <= elapsed < self.DEADLINE + 5
        assert len(stalled.made) == 2 and all(r.cancelled for r in stalled.made)

    def test_a_timeout_is_no_read_failure_a_re_read_may_drop(self) -> None:
        # _READ_FAILURES are dropped per read; a timeout must fail the page.
        assert not issubclass(ocr.VisionTimeout, ocr._READ_FAILURES)

    def test_while_a_stalled_call_runs_the_next_fails_at_once(self, stalled) -> None:
        with pytest.raises(ocr.VisionTimeout):
            ocr._vision_recognize_batch(b"png")
        start = time.monotonic()
        with pytest.raises(ocr.VisionTimeout, match="an earlier call has not returned"):
            ocr._vision_recognize_batch(b"png")
        assert time.monotonic() - start < self.DEADLINE
        assert len(stalled.made) == 2   # Vision was not called again

    def test_once_the_stalled_call_returns_vision_is_used_again(
        self, stalled, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ocr.VisionTimeout):
            ocr._vision_recognize_batch(b"png")
        stalled.release.set()
        for thread in list(ocr._stalled):
            thread.join(10)
        monkeypatch.setattr(ocr, "Vision", _fake_vision(
            {True: ("text", [SSN]), False: ("text", [SSN])}), raising=False)
        on, off = ocr._vision_recognize_batch(b"png")
        assert [o.text for o in on] == [o.text for o in off] == [SSN]

    def test_an_error_inside_performrequests_still_reaches_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        class _Handler:
            @staticmethod
            def alloc() -> Any:
                return _Handler()

            def initWithData_options_(self, data: Any, options: Any) -> Any:
                return self

            def performRequests_error_(self, requests: Any, error: Any) -> Any:
                raise ValueError("NSInvalidArgumentException")

        vision = _fake_vision({True: ("text", []), False: ("text", [])})
        vision.VNImageRequestHandler = _Handler
        monkeypatch.setattr(ocr, "Vision", vision, raising=False)
        monkeypatch.setattr(ocr, "NSData", SimpleNamespace(
            dataWithBytes_length_=lambda data, length: data), raising=False)
        monkeypatch.setattr(ocr, "NSDictionary", SimpleNamespace(
            dictionary=lambda: {}), raising=False)
        with pytest.raises(ValueError, match="NSInvalidArgumentException"):
            ocr._vision_recognize_batch(b"png")

    def test_pages_vision_never_finishes_fail_and_the_scan_ends(self, stalled) -> None:
        # The CLI's OCR layer over three pages: each is PAGE_FAILED, none
        # is read as clean, and the run costs one deadline, not three.
        doc = _build([[f"SSN {SSN}"], ["page two"], ["page three"]])
        start = time.monotonic()
        report = _scan(doc, layer="OCR", extractor=ocr.extract_ocr_text,
                       hard_variants=ocr.OCR_GENUINE_READINGS, seam_variants=None,
                       tails_into_next=(), scatter_readings=())
        assert time.monotonic() - start < self.DEADLINE + 5
        assert not report.findings
        assert [(w.code, w.layer, w.fields.get("page")) for w in report.warnings] == [
            ("PAGE_FAILED", "OCR", 1), ("PAGE_FAILED", "OCR", 2), ("PAGE_FAILED", "OCR", 3)]
        assert "did not return within" in report.warnings[0]
        assert all("earlier call has not returned" in w for w in report.warnings[1:])


class TestOcrPageSize:
    """Vision reads less and less of a render's smaller print as the
    render grows, then nothing, and reports success (#128). A page whose
    300 dpi render is longer than MAX_OCR_RENDER_SIDE on a side is read
    all the same, and also flagged as not fully read (PAGE_FAILED), from
    its size alone: a blank page reads as no lines too."""

    @staticmethod
    def _page(doc: fitz.Document, width: float, height: float) -> fitz.Page:
        return doc.new_page(width=width, height=height)

    @pytest.mark.parametrize("rotate", [0, 90])
    @pytest.mark.parametrize("size", [(612, 792), (842.5, 1190.5), (1009.3, 611.7), (73.1, 71.9)])
    def test_the_size_is_the_renders_without_rendering(self, size, rotate) -> None:
        doc = fitz.open()
        page = self._page(doc, *size)
        page.set_rotation(rotate)
        page.set_cropbox(fitz.Rect(3.3, 4.4, size[0] - 5.5, size[1] - 6.6))
        pix = page.get_pixmap(dpi=ocr.OCR_DPI, colorspace=fitz.csGRAY)
        assert ocr.ocr_render_size(page) == (pix.width, pix.height)

    @pytest.mark.parametrize("size, too_large", [
        ((612, 792), False),          # letter, 2550 x 3300 px
        ((792, 612), False),          # letter, landscape
        ((595, 842), False),          # A4, 3509 px long
        ((842.4, 595), False),        # 3510 px: the cap itself
        ((842.6, 595), True),         # 3511 px
        ((612, 1008), True),          # legal, 4200 px long
        ((842, 1191), True),          # A3
        ((792, 1224), True),          # tabloid
        ((216, 2160), True),          # a long receipt: the longest side counts
        ((2400, 3200), True),         # the #128 page, 10000 x 13334 px
    ])
    def test_the_longest_side_decides(self, size, too_large) -> None:
        doc = fitz.open()
        why = ocr.ocr_page_too_large(self._page(doc, *size))
        assert (why is not None) == too_large
        if too_large:
            assert "page too large for OCR" in why

    def _scan(self, size: tuple[float, float], readings: list[str]) -> ScanReport:
        doc = fitz.open()
        self._page(doc, *size)
        report = ScanReport()
        try:
            verify.scan_page_layer(
                doc, SecretMatcher([Secret("SSN", normalize_string(SSN))]), report,
                layer="OCR", extractor=lambda page: readings, note="test",
                hard_variants=ocr.OCR_GENUINE_READINGS,
                incomplete=lambda page: ocr.ocr_page_too_large(page))
        finally:
            doc.close()
        return report

    def test_a_blank_page_of_ordinary_size_is_clean(self) -> None:
        report = self._scan((595, 842), ["", "", "", "", "", ""])
        assert not report.findings and not report.warnings

    def test_a_blank_oversized_page_is_flagged(self) -> None:
        report = self._scan((2400, 3200), ["", "", "", "", "", ""])
        assert not report.findings
        assert [(w.code, w.layer, w.fields.get("page"), w.fields.get("storage"))
                for w in report.warnings] == [("PAGE_FAILED", "OCR", 1, "live")]
        assert "not fully read (page too large for OCR" in report.warnings[0]

    def test_a_failing_size_check_flags_the_page_and_keeps_its_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(page: fitz.Page) -> str | None:
            raise ValueError("no size")

        monkeypatch.setattr(ocr, "ocr_page_too_large", broken)
        report = self._scan((612, 792), [f"SSN {SSN}", "", "", "", "", ""])
        assert [f.secret_name for f in report.findings] == ["SSN"]
        assert [w.code for w in report.warnings] == ["PAGE_FAILED"]
        assert "could not check the page (no size)" in report.warnings[0]

    def test_what_ocr_reads_on_an_oversized_page_still_counts(self) -> None:
        # Flagging must never lose a finding: exit 1 stays 1, not 2.
        report = self._scan((2400, 3200), [f"SSN {SSN}", "", "", "", "", ""])
        assert [f.secret_name for f in report.findings] == ["SSN"]
        assert [w.code for w in report.warnings] == ["PAGE_FAILED"]

    def test_a_failed_oversized_page_is_one_warning(self) -> None:
        def broken(page: fitz.Page) -> list[str]:
            raise RuntimeError("Apple Vision error")

        doc = fitz.open()
        self._page(doc, 2400, 3200)
        report = ScanReport()
        verify.scan_page_layer(
            doc, SecretMatcher([Secret("SSN", normalize_string(SSN))]), report,
            layer="OCR", extractor=broken, note="test", incomplete=ocr.ocr_page_too_large)
        assert [w.code for w in report.warnings] == ["PAGE_FAILED"]
        assert "failed (Apple Vision error)" in report.warnings[0]
