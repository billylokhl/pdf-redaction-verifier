"""Tests for pattern-class scanning: loader validation, validators,
per-layer detection, and sample masking."""

from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

import verify

from .conftest import requires_full_env, run_verify

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

    def test_ssn_rules(self) -> None:
        assert verify._valid_ssn(VALID_SSN)
        assert not verify._valid_ssn("000-12-3456")  # area 000
        assert not verify._valid_ssn("666-12-3456")  # area 666
        assert not verify._valid_ssn("987-65-4321")  # area 9xx
        assert not verify._valid_ssn("123-00-6789")  # group 00
        assert not verify._valid_ssn("123-45-0000")  # serial 0000

    def test_mask_keeps_last_four(self) -> None:
        assert verify.mask("123-45-6789") == "*******6789"
        assert verify.mask("abc") == "***"


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
        pdf = _pdf_with_text(tmp_path, f"Card: {VALID_CARD}")
        rules = _rules_file(tmp_path, [{"name": "Any card", "class": "credit-card"}])
        assert run_verify(pdf, rules).returncode == 1

        pdf_bad = _pdf_with_text(tmp_path, f"Card: {INVALID_LUHN_CARD}")
        result = run_verify(pdf_bad, rules)
        assert "LAYER: DOM" not in result.stdout

    def test_custom_pattern(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, "Ref CASE-8912 attached")
        rules = _rules_file(tmp_path, [{"name": "case id", "pattern": r"CASE-\d{4}"}])
        result = run_verify(pdf, rules)
        assert result.returncode == 1
        assert "LAYER: DOM" in result.stdout

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


@requires_full_env
class TestPatternCleanPass:
    def test_clean_pdf_with_ssn_class_certified(self, clean_pdf, tmp_path) -> None:
        # clean_pdf contains "555-00-0000", which is SSN-shaped but has
        # group 00 and serial 0000 — the validator must reject it.
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(clean_pdf, rules)
        assert result.returncode == 0, result.stdout
