"""Tests for pattern-class scanning: loader validation, validators,
per-layer detection, tiering, masking, and evasion resistance."""

from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

import verify

from .conftest import requires_full_env, requires_metadata_tools, run_verify

# Valid under SSA rules (area not 000/666/9xx, group not 00, serial not 0000).
VALID_SSN = "123-45-6789"
# Passes Luhn; the +1 variant fails it.
VALID_CARD = "4111 1111 1111 1111"
INVALID_LUHN_CARD = "4111 1111 1111 1112"


def _rules_file(tmp_path: Path, rules: list[dict]) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules))
    return path


def _pdf_with_text(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 100 + i * 30), line)
    doc.save(path)
    doc.close()
    return path


class TestLoader:
    def test_entry_with_value_and_pattern_rejected(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "value": "a1", "pattern": "a"}])
        with pytest.raises(verify.VerifyError, match="exactly one"):
            verify.load_rules(path)

    def test_unknown_class_rejected_with_valid_list(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "class": "passport"}])
        with pytest.raises(verify.VerifyError, match="ssn"):
            verify.load_rules(path)

    def test_invalid_regex_rejected(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "pattern": "(unclosed"}])
        with pytest.raises(verify.VerifyError, match="invalid regex"):
            verify.load_rules(path)

    def test_empty_matching_pattern_rejected(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "pattern": "a*"}])
        with pytest.raises(verify.VerifyError, match="empty string"):
            verify.load_rules(path)

    def test_duplicate_names_rejected(self, tmp_path) -> None:
        # Regression: same-named rules silently overwrote each other's
        # hits and suppressed cross-page value findings.
        path = _rules_file(tmp_path, [
            {"name": "PII", "class": "ssn"},
            {"name": "PII", "class": "credit-card"},
        ])
        with pytest.raises(verify.VerifyError, match="duplicate rule name"):
            verify.load_rules(path)

    def test_mixed_rules_load(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [
            {"name": "known", "value": "123-45-6789"},
            {"name": "any ssn", "class": "ssn"},
            {"name": "case id", "pattern": r"CASE-\d{4}"},
        ])
        secrets, patterns = verify.load_rules(path)
        assert len(secrets) == 1 and len(patterns) == 2


class TestValidators:
    def test_luhn(self) -> None:
        assert verify._valid_card(VALID_CARD)
        assert not verify._valid_card(INVALID_LUHN_CARD)
        assert not verify._valid_card("0000 0000 0000 0000")  # degenerate

    def test_card_rejects_pdf_date_stamps(self) -> None:
        # Regression: D:YYYYMMDDHHmmSS literals pass Luhn ~10% of the
        # time; no real 14-digit card IIN starts with 19/20.
        assert not verify._valid_card("20190115235959")
        assert not verify._valid_card("19991231115959")

    def test_ssn_rules(self) -> None:
        assert verify._valid_ssn(VALID_SSN)
        assert not verify._valid_ssn("000-12-3456")  # area 000
        assert not verify._valid_ssn("666-12-3456")  # area 666
        assert not verify._valid_ssn("987-65-4321")  # area 9xx
        assert not verify._valid_ssn("123-00-6789")  # group 00
        assert not verify._valid_ssn("123-45-0000")  # serial 0000

    def test_nanp_rejects_non_phone_integers(self) -> None:
        assert verify._valid_nanp("(425) 867-5309")
        assert not verify._valid_nanp("1756500000")  # epoch-like, area '175'
        assert not verify._valid_nanp("0123456789")

    def test_email_rejects_asset_filenames(self) -> None:
        # Regression: logo@2x.png fully matched the email class.
        assert not verify._valid_email("logo@2x.png")
        assert verify._valid_email("alice@example.com")

    def test_mask_never_reveals_more_than_half(self) -> None:
        assert verify.mask("123-45-6789") == "****6789"
        assert verify.mask("38217") == "****17"      # 5-char PIN: reveal 2
        assert verify.mask("abc") == "****c"         # reveal 1 of 3
        assert verify.mask("ab") == "****b"          # reveal 1 of 2
        assert verify.mask("a") == "****"            # reveal 0


class TestMatchSemantics:
    SSN_RULES = None  # built per test via load

    def _patterns(self, tmp_path, rules):
        _, patterns = verify.load_rules(_rules_file(tmp_path, rules))
        return patterns

    def test_greedy_rejection_retries_embedded_match(self, tmp_path) -> None:
        # Regression: validator-rejected greedy superspan swallowed the
        # embedded valid card and finditer never retried inside it.
        patterns = self._patterns(tmp_path, [{"name": "card", "class": "credit-card"}])
        hits = verify.match_patterns(f"Gate 7 {VALID_CARD}", patterns)
        assert "card" in hits

    def test_unicode_dashes_folded(self, tmp_path) -> None:
        # Regression: en-dash SSNs evaded the ASCII separator class.
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert "ssn" in verify.match_patterns("SSN: 123–45–6789", patterns)

    def test_fullwidth_digits_folded_and_validated(self, tmp_path) -> None:
        # Regression: fullwidth digits matched \d but bypassed the ASCII
        # comparisons in _valid_ssn (area-9xx passed as a finding).
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert verify.match_patterns("９８７６５４３２１", patterns) == {}
        assert "ssn" in verify.match_patterns("５２９１２４５６７", patterns)

    def test_zip_plus_four_and_decimals_not_ssn(self, tmp_path) -> None:
        # Regression: ZIP+4 regrouped into an SSN; decimal fractions
        # matched because '.' was not excluded by the leading guard.
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert verify.match_patterns("Anytown, NY 12345-6789", patterns) == {}
        assert verify.match_patterns("ratio 1.123456789 observed", patterns) == {}

    def test_zero_width_matches_never_findings(self, tmp_path) -> None:
        # Regression: lookbehind-only patterns produced zero-width hits
        # with empty samples on every document.
        patterns = self._patterns(
            tmp_path, [{"name": "x", "pattern": r"(?<=SSN: )\d*"}]
        )
        assert verify.match_patterns("SSN: [REDACTED]", patterns) == {}

    def test_anchored_patterns_match_per_line(self, tmp_path) -> None:
        # Regression: ^/$ compiled without re.MULTILINE never matched
        # mid-page — a silent certified-clean false PASS.
        patterns = self._patterns(
            tmp_path, [{"name": "case", "pattern": r"^Ref CASE-\d{4}$"}]
        )
        assert "case" in verify.match_patterns("intro\nRef CASE-8912\nend", patterns)


class TestPatternDetection:
    def test_ssn_class_found_and_masked(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, f"Applicant SSN: {VALID_SSN}")
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(pdf, rules)
        assert result.returncode == 1
        assert "LAYER: DOM" in result.stdout
        # The report must never leak the full match — only the masked tail.
        assert "123-45" not in result.stdout
        assert "6789" in result.stdout

    def test_luhn_invalid_card_not_flagged_valid_card_is(self, tmp_path) -> None:
        rules = _rules_file(tmp_path, [{"name": "Any card", "class": "credit-card"}])
        pdf = _pdf_with_text(tmp_path, f"Card: {VALID_CARD}")
        assert run_verify(pdf, rules).returncode == 1

        pdf_bad = _pdf_with_text(tmp_path, f"Card: {INVALID_LUHN_CARD}")
        result = run_verify(pdf_bad, rules)
        # Not exit 1: the invalid card must produce no finding. (Exit 0
        # or 2 depending on environment completeness.)
        assert result.returncode in (0, 2), result.stdout
        assert "LAYER: DOM" not in result.stdout
        assert "Scanning" in result.stdout  # the scan actually ran

    def test_custom_pattern(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, "Ref CASE-8912 attached")
        rules = _rules_file(tmp_path, [{"name": "case id", "pattern": r"CASE-\d{4}"}])
        result = run_verify(pdf, rules)
        assert result.returncode == 1
        assert "LAYER: DOM" in result.stdout

    @requires_metadata_tools
    def test_pattern_in_metadata(self, tmp_path) -> None:
        path = tmp_path / "meta.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean body")
        doc.set_metadata({"subject": f"applicant {VALID_SSN}"})
        doc.save(path)
        doc.close()
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(path, rules)
        assert result.returncode == 1
        assert "LAYER: Metadata" in result.stdout or "LAYER: Binary" in result.stdout


class TestTiering:
    def test_stacked_digit_column_not_a_hard_finding(self, tmp_path) -> None:
        # Regression: vertical-variant reconstruction of an invoice-style
        # digit column fabricated an SSN and hard-failed a clean page.
        path = tmp_path / "column.pdf"
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):
            page.insert_text((300, 100 + i * 20), digit)
        doc.save(path)
        doc.close()
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(path, rules)
        assert result.returncode != 1, result.stdout
        # The possible fusion is surfaced for review, not silenced.
        assert "manual review" in result.stdout

    def test_hyphen_wrapped_ssn_surfaces_for_review(self, tmp_path) -> None:
        # Regression: '123-45-' wrapped at the hyphen presented two
        # separator chars and matched nowhere; the soft tier must at
        # least refuse to certify (exit 2 with a manual-review warning).
        pdf = _pdf_with_text(tmp_path, "SSN on file: 123-45-", "6789 (continued)")
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(pdf, rules)
        assert result.returncode != 0, result.stdout
        assert "manual review" in result.stdout or "LAYER:" in result.stdout

    def test_adjacent_lines_fusion_not_a_hard_finding(self, tmp_path) -> None:
        # Regression: '\n' satisfied the \s separator slot, fusing digits
        # from unrelated lines into hard findings.
        pdf = _pdf_with_text(tmp_path, "Conference room 123", "45 6789 Main Street")
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(pdf, rules)
        assert result.returncode != 1, result.stdout


@requires_full_env
class TestPatternCleanPass:
    def test_clean_pdf_with_ssn_class_certified(self, clean_pdf, tmp_path) -> None:
        # clean_pdf contains "555-00-0000", which is SSN-shaped but has
        # group 00 and serial 0000 — the validator must reject it.
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(clean_pdf, rules)
        assert result.returncode == 0, result.stdout

    def test_clean_pdf_with_card_class_ignores_dates(self, clean_pdf, tmp_path) -> None:
        # Regression: PDF CreationDate/ModDate literals passed Luhn and
        # hard-failed ~1 in 5 clean PDFs under the credit-card class.
        rules = _rules_file(tmp_path, [{"name": "Any card", "class": "credit-card"}])
        result = run_verify(clean_pdf, rules)
        assert result.returncode == 0, result.stdout
