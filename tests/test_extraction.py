"""Unit-level regression tests for the normalizer and text-layer extraction.

Each test pins a previously confirmed detection-gap bug.
"""

from __future__ import annotations

import json
from typing import Any

import caselib
import pymupdf as fitz
import pytest

import verify

from redaction_verifier.matching import SecretMatcher, normalize_string
from redaction_verifier.model import ScanReport, Secret
from redaction_verifier.rules import load_rules
from redaction_verifier.views import (TEXT_GENUINE_READINGS, TEXT_SEAM_READINGS,
                                      extract_visual_text)

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
          secrets=(("SSN", SSN),), patterns=(), fail_fast: bool = False) -> ScanReport:
    """scan_page_layer as the CLI runs the Text layer (pass hard_variants
    and seam_variants=None for OCR)."""
    report = ScanReport()
    matcher = SecretMatcher(
        [Secret(name, normalize_string(v)) for name, v in secrets])
    try:
        verify.scan_page_layer(doc, matcher, report, layer=layer,
                               extractor=extractor or extract_visual_text,
                               note="visual text layer", patterns=patterns,
                               hard_variants=hard_variants, seam_variants=seam_variants,
                               fail_fast=fail_fast)
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
        assert report.warnings == [line(1), line(2)]

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
        assert report.warnings == [line(1), crossing(4)]

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
    if case_id.startswith(("page.raw-font-coded-runs-", "page.raw-font-coded-vertical-")))
NO_ACROSS_READINGS = 4    # readings 0-3, as before the readings across the edge


class TestReadingsAcrossTheVisibleEdge:
    """Readings 4-6 read every glyph of one writing direction, visible or
    not, so a value running across the edge of the visible area, or
    written vertically outside it, is read whole and found hard. They feed
    no hard page seam, and a column gap keeps an off-page slug apart from
    a visible line on its baseline."""

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
        assert len(readings) == 9
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
        # unrelated first line of page 2 starts with '6789'. Reading 4's
        # last line on page 1 is the slug, but readings 4-6 feed no hard
        # seam: the slug does not run on into the next page. The join is
        # still a page-break crossing, so the manual-review net reports it.
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
        # Clean control: a visible line ending '123-45' and an off-page slug
        # starting '-6789' on the same baseline, far apart. Reading 4 holds
        # both, but the gap is a column break, so neither a value rule nor
        # the SSN pattern rule finds a hard match.
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 300), "Invoice 123-45")
        page.insert_text((640, 300), "-6789 slug")             # past x=595
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "Any SSN", "class": "ssn"}]))
        readings = extract_visual_text(page)
        assert "Invoice123-45\n-6789slug" in readings[4], readings
        report = _scan(doc, patterns=load_rules(rules).patterns)
        assert report.findings == []


class TestPageBreaksOcr:
    """The same rules on the OCR path: two genuine readings that can differ."""

    @staticmethod
    def _ocr(readings: list[list[str]]) -> ScanReport:
        doc = fitz.open()
        for _ in readings:
            doc.new_page()
        by_page = dict(enumerate(readings))
        return _scan(doc, layer="OCR", hard_variants=2, seam_variants=None,
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
        assert all(normalize_string(SSN) in normalize_string(text) for text in readings)


class _FakeCandidate:
    def __init__(self, text: str) -> None:
        self._text = text

    def string(self) -> str:
        return self._text


class _FakeObservation:
    def __init__(self, text: str) -> None:
        self._text = text

    def topCandidates_(self, count: int) -> list[_FakeCandidate]:
        return [_FakeCandidate(self._text)]


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

    def setRecognitionLevel_(self, level: Any) -> None:
        pass

    def setUsesLanguageCorrection_(self, on: bool) -> None:
        self.correction = on

    def results(self) -> list[Any] | None:
        return self.observations


def _fake_vision(plans: dict[bool, tuple[str, Any]]) -> Any:
    """A stand-in for the Vision module. *plans* says, per language
    correction setting, what performRequests does with that request:
    ("text", [lines]) runs its handler with those observations, ("none",
    None) runs it with results() None, ("error", message) runs it with an
    error, ("raises", exc) runs it on an observation whose topCandidates_
    raises *exc*, and ("skip", None) never runs it — all while
    still reporting success. Handlers run on the calling thread here; on
    real Vision they run on worker threads, where an exception escaping
    one aborts the process, so the handler itself must not let one out.
    """

    class _Alloc:
        def __init__(self, make: Any) -> None:
            self._make = make

        def initWithCompletionHandler_(self, handler: Any) -> _FakeRequest:
            return _FakeRequest(handler)

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
        return ocr._vision_recognize_batch(b"png")

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
