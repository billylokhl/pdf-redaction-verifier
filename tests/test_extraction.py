"""Unit-level regression tests for the normalizer and text-layer extraction.

Each test pins a previously confirmed detection-gap bug.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pymupdf as fitz
import pytest

import verify

from redaction_verifier.matching import SecretMatcher, normalize_string
from redaction_verifier.model import ScanReport, Secret
from redaction_verifier.views import TEXT_GENUINE_READINGS, extract_visual_text, ocr

from .conftest import HAS_OCR, SSN


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
          secrets=(("SSN", SSN),), fail_fast: bool = False) -> ScanReport:
    report = ScanReport()
    matcher = SecretMatcher(
        [Secret(name, normalize_string(v)) for name, v in secrets])
    try:
        verify.scan_page_layer(doc, matcher, report, layer=layer,
                               extractor=extractor or extract_visual_text,
                               note="visual text layer", patterns=[],
                               hard_variants=hard_variants, fail_fast=fail_fast)
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


class TestPageBreaksOcr:
    """The same rules on the OCR path: two genuine readings that can differ."""

    @staticmethod
    def _ocr(readings: list[list[str]]) -> ScanReport:
        doc = fitz.open()
        for _ in readings:
            doc.new_page()
        by_page = dict(enumerate(readings))
        return _scan(doc, layer="OCR", hard_variants=ocr.OCR_GENUINE_READINGS,
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
        doc = fitz.open()
        try:
            readings = ocr.extract_ocr_text(doc.new_page())  # A4 by default
        finally:
            doc.close()
        split = "\n".join([HORIZONTAL.text, *STACK.text, ROTATED_90.text])
        fused = "\n".join([HORIZONTAL.text, STACK.text, ROTATED_90.text])
        assert len(readings) == 4 and ocr.OCR_GENUINE_READINGS == 2
        assert readings == [split, split, fused, fused]

    def _scan(self, tmp_path, monkeypatch, pages: list[ocr.Observation], rule: dict,
              upright: str) -> tuple[int, dict]:
        # The CLI end to end, with Vision stubbed: one observation per page,
        # and *upright* what every upright re-read returns.
        monkeypatch.setattr(verify, "_OCR_IMPORTS_OK", True)
        per_page = iter(pages)
        monkeypatch.setattr(ocr, "_vision_recognize_batch",
                            lambda png: ([next(per_page)],) * 2)
        monkeypatch.setattr(ocr, "_upright_reads", lambda image, box: ([upright],) * 2)
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
        # the genuine readings hold whole too.
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
                         readings[ocr.OCR_GENUINE_READINGS:]))
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
        assert all(normalize_string(SSN) in normalize_string(text) for text in readings)


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
