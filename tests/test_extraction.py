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


def _scan_pages(pages: list[list[str]]) -> verify.ScanReport:
    """Text layer only, one line of text per list entry, one list per page."""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + 30 * i), line)
    report = verify.ScanReport()
    matcher = verify.SecretMatcher([verify.Secret("SSN", verify.normalize_string(SSN))])
    try:
        verify.scan_page_layer(doc, matcher, report, layer="Text",
                               extractor=verify.extract_visual_text,
                               note="visual text layer", patterns=[])
    finally:
        doc.close()
    return report


class TestPageBoundaryTiers:
    """A value split across lines is manual-review within a page and hard
    only when it genuinely continues from one page's last line onto the
    next page's first line."""

    def test_same_page_split_is_not_escalated(self) -> None:
        # Regression: each page's fully joined text fed a cross-page rolling
        # scanner, so a same-page cross-line join became a hard finding
        # mislabelled "across page boundaries" — on a one-page document.
        report = _scan_pages([["Invoice total 123-45-", "6789 units shipped"]])
        assert report.findings == []
        assert any("cross-line" in w for w in report.warnings)

    def test_split_between_last_and_first_line_is_hard(self) -> None:
        report = _scan_pages([["intro", "Applicant SSN 123-45-"],
                              ["6789 continues here", "more"]])
        assert [f.location for f in report.findings] == [
            "across page boundaries, pages 1–2 (visual text layer)"]

    def test_split_needing_other_lines_is_manual_review(self) -> None:
        # Spans the break only by also joining two lines of page 1.
        report = _scan_pages([["ref 123", "45-"], ["6789 end"]])
        assert report.findings == []
        assert any("spans the page break" in w for w in report.warnings)

    def test_split_continues_over_a_blank_page(self) -> None:
        report = _scan_pages([["SSN 123-45-"], [], ["6789 tail"]])
        assert [f.location for f in report.findings] == [
            "across page boundaries, pages 1–3 (visual text layer)"]

    def test_confirmed_leak_draws_no_coincidence_warning(self) -> None:
        # Regression: the cross-line check compared only against hits from
        # its own reading, so vertical readings called a secret found hard
        # on one horizontal line "possibly coincidental" — twice.
        report = _scan_pages([["SSN 123-45-6789"], ["next page"]])
        assert [f.location for f in report.findings] == ["page 1 (visual text layer)"]
        assert report.warnings == []
