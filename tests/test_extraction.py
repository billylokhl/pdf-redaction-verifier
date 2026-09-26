"""Unit-level regression tests for the normalizer and text-layer extraction.

Each test pins a previously confirmed detection-gap bug.
"""

from __future__ import annotations

import fitz

import verify

from .conftest import SSN


def _dom_finds(page: fitz.Page, normalized_secret: str) -> bool:
    return any(
        normalized_secret in verify.normalize_string(variant)
        for variant in verify.extract_visual_text(page)
    )


class TestNormalizer:
    def test_formatting_variants_collapse(self) -> None:
        for form in ("123-45-6789", "123 45 6789", "1 2 3\n4 5-6.7/8 9"):
            assert verify.normalize_string(form) == "123456789"

    def test_fullwidth_digits_fold_to_ascii(self) -> None:
        # Regression: ASCII-only normalizer deleted fullwidth forms,
        # so a visually identical secret false-PASSed.
        assert verify.normalize_string("１２３－４５－６７８９") == "123456789"

    def test_accents_and_casefold(self) -> None:
        assert verify.normalize_string("José-Straße") == "josestrasse"

    def test_overlapping_secrets_both_found(self) -> None:
        # SecretMatcher must not let one match consume an overlapping one.
        matcher = verify.SecretMatcher([
            verify.Secret("a", "1234"), verify.Secret("b", "2345"),
        ])
        assert {s.name for s in matcher.search("12345")} == {"a", "b"}


class TestVisualOrder:
    def test_shuffled_form_box_digits(self, leaky_pdf) -> None:
        # Digits drawn out of order at one baseline must reconstruct
        # in visual left-to-right order (horizontal variant specifically).
        doc = fitz.open(leaky_pdf)
        try:
            horizontal = verify.extract_visual_text(doc[0])[0]
            assert SSN.replace("-", "") in verify.normalize_string(horizontal)
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
            horizontal = verify.extract_visual_text(page)[0]
            assert "123456789" in verify.normalize_string(horizontal)
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
            horizontal = verify.extract_visual_text(page)[0]
            assert "111222333" in verify.normalize_string(horizontal.split("\n")[0])
        finally:
            doc.close()


def _build(pages: list[list[str]], *, rotate: int = 0) -> fitz.Document:
    """One list of lines per page; rotate=90 lays each line out vertically."""
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
          hard_variants: int = 1, secrets=(("SSN", SSN),)) -> verify.ScanReport:
    report = verify.ScanReport()
    matcher = verify.SecretMatcher(
        [verify.Secret(name, verify.normalize_string(v)) for name, v in secrets])
    try:
        verify.scan_page_layer(doc, matcher, report, layer=layer,
                               extractor=extractor or verify.extract_visual_text,
                               note="visual text layer", patterns=[],
                               hard_variants=hard_variants)
    finally:
        doc.close()
    return report


def _scan_pages(pages: list[list[str]], **kw) -> verify.ScanReport:
    return _scan(_build(pages), **kw)


SEAM = "across page boundaries, continuing onto page {} (visual text layer)"
JOINED = ("Text: page {}: a sequence matching secret 'SSN' appears only when "
          "text is joined across a page break")


def _joined_warnings(report: verify.ScanReport) -> list[str]:
    return [w.split(" — ")[0] for w in report.warnings if "page break" in w]


class TestPageBoundaryTiers:
    """Where a split value lands: hard only at a genuine page seam (last line
    of one page onto the first line of the next adjacent page, continuing
    through single-line pages); manual-review for every other join; never
    silent where a plain rolling scan would have caught it."""

    # ── same page ──────────────────────────────────────────────────────
    def test_same_page_split_is_not_escalated(self) -> None:
        # Regression: each page's fully joined text fed a cross-page rolling
        # scanner, so a same-page cross-line join became a hard finding
        # mislabelled "across page boundaries" — on a one-page document.
        report = _scan_pages([["Invoice total 123-45-", "6789 units shipped"]])
        assert report.findings == []
        assert [w for w in report.warnings if "cross-line" in w] == report.warnings
        assert len(report.warnings) == 1

    def test_cross_line_warning_is_raised_once_per_page(self) -> None:
        # Diagonal digits fuse in all three readings; one warning, not three.
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):
            page.insert_text((72 + 20 * i, 100 + 20 * i), digit)
        report = _scan(doc)
        assert report.findings == []
        assert len(report.warnings) == 1 and "cross-line" in report.warnings[0]

    def test_confirmed_leak_draws_no_coincidence_warning(self) -> None:
        # Regression: vertical readings called a secret found hard on one
        # horizontal line "possibly coincidental" — twice.
        report = _scan_pages([["SSN 123-45-6789"], ["next page"]])
        assert [f.location for f in report.findings] == ["page 1 (visual text layer)"]
        assert report.warnings == []

    def test_single_line_hit_in_a_vertical_reading_counts(self) -> None:
        # Rotated text: only a vertical reading holds the value on one line.
        # That hit is hard and must suppress the horizontal cross-line join.
        report = _scan(_build([["SSN 123-45-6789"]], rotate=90))
        assert [f.location for f in report.findings] == ["page 1 (visual text layer)"]
        assert report.warnings == []

    # ── the seam: hard ─────────────────────────────────────────────────
    def test_split_between_last_and_first_line_is_hard(self) -> None:
        report = _scan_pages([["intro", "Applicant SSN 123-45-"],
                              ["6789 continues here", "more"]])
        assert [f.location for f in report.findings] == [SEAM.format(2)]
        assert report.warnings == []

    def test_value_chained_over_three_pages_is_hard(self) -> None:
        # Regression (silent miss): the seam check compared only adjacent
        # pages, so a value whose middle part is the whole of page 2 was
        # neither a finding nor a warning. Single-line pages now carry the
        # continuation.
        report = _scan_pages([["intro", "SSN 123-"], ["45-"], ["6789 end"]])
        assert [f.location for f in report.findings] == [SEAM.format(3)]
        assert report.warnings == []

    def test_seam_keeps_enough_of_the_previous_line(self) -> None:
        # Eight of nine digits on the last line: the continuation must keep
        # max_len-1 characters or the value drops to manual review.
        report = _scan_pages([["SSN 123-45-678"], ["9 end"]])
        assert [f.location for f in report.findings] == [SEAM.format(2)]

    def test_value_wholly_on_the_first_line_is_not_a_seam(self) -> None:
        # Found on page 2's first line alone: a page finding, not also a
        # spurious "across page boundaries" one.
        report = _scan_pages([["intro"], ["SSN 123-45-6789 end"]])
        assert [f.location for f in report.findings] == ["page 2 (visual text layer)"]
        assert report.warnings == []

    def test_every_seam_is_reported(self) -> None:
        report = _scan_pages([["SSN 123-45-"], ["6789", "id 123-"], ["45-6789"]])
        assert [f.location for f in report.findings] == [SEAM.format(2), SEAM.format(3)]

    def test_rotated_split_across_pages_is_hard(self) -> None:
        report = _scan(_build([["SSN 123-45-"], ["6789 end"]], rotate=90))
        assert [f.location for f in report.findings] == [SEAM.format(2)]
        assert report.warnings == []

    # ── everything else: manual review, never silent ───────────────────
    def test_split_needing_other_lines_on_the_first_page(self) -> None:
        report = _scan_pages([["ref 123", "45-"], ["6789 end"]])
        assert report.findings == []
        assert _joined_warnings(report) == [JOINED.format(2)]

    def test_split_needing_other_lines_on_the_next_page(self) -> None:
        report = _scan_pages([["SSN 123-45-"], ["67", "89 end"]])
        assert report.findings == []
        assert _joined_warnings(report) == [JOINED.format(2)]

    def test_split_over_multi_line_middle_page(self) -> None:
        report = _scan_pages([["SSN 123-"], ["45", "-67"], ["89 end"]])
        assert report.findings == []
        assert _joined_warnings(report) == [JOINED.format(3)]

    def test_one_character_either_side_of_the_break(self) -> None:
        # The backstop window must hold max_len-1 characters on each side.
        for pages in ([["x 1234", "5678"], ["9 end"]],
                      [["abc 1"], ["2345", "6789"]]):
            report = _scan_pages(pages)
            assert report.findings == []
            assert _joined_warnings(report) == [JOINED.format(2)], pages

    def test_blank_or_unreadable_middle_page_breaks_the_seam(self) -> None:
        # Content between the halves is unseen (blank, punctuation-only, or
        # failed to extract), so the join is not a proven continuation:
        # manual review, not a hard finding — but never silent.
        for pages in ([["SSN 123-45-"], [], ["6789 tail"]],
                      [["SSN 123-45-"], ["* * *"], ["6789 tail"]]):
            report = _scan_pages(pages)
            assert report.findings == [], pages
            assert _joined_warnings(report) == [JOINED.format(3)], pages

        def flaky(page: fitz.Page) -> list[str]:
            if page.number == 1:
                raise RuntimeError("unreadable")
            return verify.extract_visual_text(page)

        report = _scan(_build([["SSN 123-45-"], ["x"], ["6789 tail"]]),
                       extractor=flaky)
        assert report.findings == []
        assert _joined_warnings(report) == [JOINED.format(3)]

    def test_a_confirmed_leak_suppresses_the_joined_warning(self) -> None:
        report = _scan_pages([["SSN 123-45-6789", "id 123", "45-"], ["6789 x"]])
        assert [f.location for f in report.findings] == ["page 1 (visual text layer)"]
        assert report.warnings == []


class TestPageBoundaryTiersOcr:
    """The same rules on the OCR path: two genuine readings, both hard-eligible."""

    @staticmethod
    def _ocr(pages_text: list[list[str]]) -> verify.ScanReport:
        doc = fitz.open()
        for _ in pages_text:
            doc.new_page()
        readings = {i: t for i, t in enumerate(pages_text)}
        return _scan(doc, layer="OCR", hard_variants=2,
                     extractor=lambda page: readings[page.number])

    def test_seam_is_hard(self) -> None:
        report = self._ocr([["x\nSSN 123-45-"] * 2, ["6789 end"] * 2])
        assert [f.location for f in report.findings] == [SEAM.format(2)]
        assert report.warnings == []

    def test_blank_middle_page_is_manual_review(self) -> None:
        report = self._ocr([["x\nSSN 123-45-"] * 2, [""] * 2, ["6789 end"] * 2])
        assert report.findings == []
        assert [w.split(" — ")[0] for w in report.warnings] == [
            JOINED.format(3).replace("Text:", "OCR:")]
