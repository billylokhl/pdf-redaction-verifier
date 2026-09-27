"""Tests for pattern-class scanning: loader validation, validators,
per-layer detection, tiering, masking, and evasion resistance."""

from __future__ import annotations

import json
import re
from pathlib import Path

import fitz
import pytest

import verify

from redaction_verifier.matching import (
    BUILTIN_PATTERN_CLASSES,
    SecretMatcher,
    _valid_card,
    _valid_email,
    _valid_nanp,
    _valid_ssn,
    mask,
    match_patterns,
)
from redaction_verifier.model import ScanReport, Secret, VerifyError
from redaction_verifier.rules import ENTITY_TYPE_TO_CLASS, load_rules

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
        with pytest.raises(VerifyError, match="exactly one"):
            load_rules(path)

    def test_unknown_class_rejected_with_valid_list(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "class": "passport"}])
        with pytest.raises(VerifyError, match="ssn"):
            load_rules(path)

    def test_invalid_regex_rejected(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "pattern": "(unclosed"}])
        with pytest.raises(VerifyError, match="invalid regex"):
            load_rules(path)

    def test_empty_matching_pattern_rejected(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [{"name": "x", "pattern": "a*"}])
        with pytest.raises(VerifyError, match="empty string"):
            load_rules(path)

    def test_duplicate_names_rejected(self, tmp_path) -> None:
        # Regression: same-named rules silently overwrote each other's
        # hits and suppressed cross-page value findings.
        path = _rules_file(tmp_path, [
            {"name": "PII", "class": "ssn"},
            {"name": "PII", "class": "credit-card"},
        ])
        with pytest.raises(VerifyError, match="duplicate rule name"):
            load_rules(path)

    def test_mixed_rules_load(self, tmp_path) -> None:
        path = _rules_file(tmp_path, [
            {"name": "known", "value": "123-45-6789"},
            {"name": "any ssn", "class": "ssn"},
            {"name": "case id", "pattern": r"CASE-\d{4}"},
        ])
        rules = load_rules(path)
        assert len(rules.secrets) == 1 and len(rules.patterns) == 2


class TestValidators:
    def test_luhn(self) -> None:
        assert _valid_card(VALID_CARD)
        assert not _valid_card(INVALID_LUHN_CARD)
        assert not _valid_card("0000 0000 0000 0000")  # degenerate

    def test_card_rejects_pdf_date_stamps(self) -> None:
        # Regression: D:YYYYMMDDHHmmSS literals pass Luhn ~10% of the
        # time; no real 14-digit card IIN starts with 19/20.
        assert not _valid_card("20190115235959")
        assert not _valid_card("19991231115959")

    def test_ssn_rules(self) -> None:
        assert _valid_ssn(VALID_SSN)
        assert not _valid_ssn("000-12-3456")  # area 000
        assert not _valid_ssn("666-12-3456")  # area 666
        assert not _valid_ssn("987-65-4321")  # area 9xx
        assert not _valid_ssn("123-00-6789")  # group 00
        assert not _valid_ssn("123-45-0000")  # serial 0000

    def test_nanp_rejects_non_phone_integers(self) -> None:
        assert _valid_nanp("(425) 867-5309")
        assert not _valid_nanp("1756500000")  # epoch-like, area '175'
        assert not _valid_nanp("0123456789")

    def test_email_rejects_asset_filenames(self) -> None:
        # Regression: logo@2x.png fully matched the email class.
        assert not _valid_email("logo@2x.png")
        assert _valid_email("alice@example.com")

    def test_mask_never_reveals_more_than_half(self) -> None:
        assert mask("123-45-6789") == "****6789"
        assert mask("38217") == "****17"      # 5-char PIN: reveal 2
        assert mask("abc") == "****c"         # reveal 1 of 3
        assert mask("ab") == "****b"          # reveal 1 of 2
        assert mask("a") == "****"            # reveal 0


class TestMatchSemantics:
    SSN_RULES = None  # built per test via load

    def _patterns(self, tmp_path, rules):
        return load_rules(_rules_file(tmp_path, rules)).patterns

    def test_soft_hyphen_is_folded_to_a_dash(self, tmp_path) -> None:
        # Regression: fonts embedded by some producers (PyMuPDF with Arial)
        # extract '-' as U+00AD, which NFKC keeps, so the ssn class missed
        # 'SSN 123-45-6789' drawn in such a font — exit 0 on a leak.
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert match_patterns("SSN 123­45­6789", patterns) == {
            "ssn": "123-45-6789"}

    def test_greedy_rejection_retries_embedded_match(self, tmp_path) -> None:
        # Regression: validator-rejected greedy superspan swallowed the
        # embedded valid card and finditer never retried inside it.
        patterns = self._patterns(tmp_path, [{"name": "card", "class": "credit-card"}])
        hits = match_patterns(f"Gate 7 {VALID_CARD}", patterns)
        assert "card" in hits

    def test_unicode_dashes_folded(self, tmp_path) -> None:
        # Regression: en-dash SSNs evaded the ASCII separator class.
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert "ssn" in match_patterns("SSN: 123–45–6789", patterns)

    def test_fullwidth_digits_folded_and_validated(self, tmp_path) -> None:
        # Regression: fullwidth digits matched \d but bypassed the ASCII
        # comparisons in _valid_ssn (area-9xx passed as a finding).
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert match_patterns("９８７６５４３２１", patterns) == {}
        assert "ssn" in match_patterns("５２９１２４５６７", patterns)

    def test_zip_plus_four_and_decimals_not_ssn(self, tmp_path) -> None:
        # Regression: ZIP+4 regrouped into an SSN; decimal fractions
        # matched because '.' was not excluded by the leading guard.
        patterns = self._patterns(tmp_path, [{"name": "ssn", "class": "ssn"}])
        assert match_patterns("Anytown, NY 12345-6789", patterns) == {}
        assert match_patterns("ratio 1.123456789 observed", patterns) == {}

    def test_zero_width_matches_never_findings(self, tmp_path) -> None:
        # Regression: lookbehind-only patterns produced zero-width hits
        # with empty samples on every document.
        patterns = self._patterns(
            tmp_path, [{"name": "x", "pattern": r"(?<=SSN: )\d*"}]
        )
        assert match_patterns("SSN: [REDACTED]", patterns) == {}

    def test_anchored_patterns_match_per_line(self, tmp_path) -> None:
        # Regression: ^/$ compiled without re.MULTILINE never matched
        # mid-page — a silent certified-clean false PASS.
        patterns = self._patterns(
            tmp_path, [{"name": "case", "pattern": r"^Ref CASE-\d{4}$"}]
        )
        assert "case" in match_patterns("intro\nRef CASE-8912\nend", patterns)


class TestPatternDetection:
    def test_ssn_class_found_and_masked(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, f"Applicant SSN: {VALID_SSN}")
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(pdf, rules)
        assert result.returncode == 1
        assert "LAYER: Text" in result.stdout
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
        assert "LAYER: Text" not in result.stdout
        assert "Scanning" in result.stdout  # the scan actually ran

    def test_custom_pattern(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, "Ref CASE-8912 attached")
        rules = _rules_file(tmp_path, [{"name": "case id", "pattern": r"CASE-\d{4}"}])
        result = run_verify(pdf, rules)
        assert result.returncode == 1
        assert "LAYER: Text" in result.stdout

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

    def test_same_line_table_cells_not_a_hard_finding(self, tmp_path) -> None:
        # Regression: extract_visual_text drops whitespace glyphs, so
        # three numeric cells in one table ROW concatenated into
        # '123456789' and hard-failed a clean invoice.
        path = tmp_path / "table.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 130), "123")
        page.insert_text((200, 130), "45")
        page.insert_text((330, 130), "6789")
        doc.save(path)
        doc.close()
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(path, rules)
        assert result.returncode != 1, result.stdout
        assert "manual review" in result.stdout  # surfaced, not silenced

    def test_wide_uniform_form_boxes_still_hard(self, tmp_path) -> None:
        # Guard against over-fencing: per-character form boxes are spaced
        # widely but UNIFORMLY, so the run must stay intact and hard.
        path = tmp_path / "boxes.pdf"
        doc = fitz.open()
        page = doc.new_page()
        for i, digit in enumerate("123456789"):
            page.insert_text((72 + i * 30, 140), digit, fontsize=14)
        doc.save(path)
        doc.close()
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(path, rules)
        assert result.returncode == 1, result.stdout
        assert "LAYER: Text" in result.stdout

    def test_space_separated_ssn_on_one_line_still_hard(self, tmp_path) -> None:
        # Guard against over-fencing: ordinary word spaces are narrower
        # than a character and must not break the run.
        pdf = _pdf_with_text(tmp_path, "Applicant SSN 123 45 6789 on file")
        rules = _rules_file(tmp_path, [{"name": "Any SSN", "class": "ssn"}])
        result = run_verify(pdf, rules)
        assert result.returncode == 1, result.stdout
        assert "LAYER: Text" in result.stdout

    def test_adjacent_metadata_values_do_not_fuse(self, tmp_path) -> None:
        # Regression: metadata string values were joined with a single
        # '\n', which fits the class regexes' separator slot, so two
        # unrelated fields fused into a hard credit-card finding.
        path = tmp_path / "meta_fuse.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean body")
        doc.set_metadata({"author": "4111 1111 1111", "subject": "1111 batch note"})
        doc.save(path)
        doc.close()
        rules = _rules_file(tmp_path, [{"name": "Any card", "class": "credit-card"}])
        result = run_verify(path, rules)
        assert result.returncode != 1, result.stdout

    def test_second_ocr_variant_yields_hard_finding(self, tmp_path) -> None:
        # Regression: tier was chosen by variant index, so a leak read
        # cleanly only by the correction-OFF Vision pass (variants[1] —
        # the more digit-accurate read) could never be a hard finding.
        rules = _rules_file(tmp_path, [{"name": "ssn", "class": "ssn"}])
        patterns = load_rules(rules).patterns
        doc = fitz.open()
        doc.new_page()
        try:
            report = ScanReport()
            verify.scan_page_layer(
                doc, SecretMatcher([Secret("unused", "zzzz")]), report,
                layer="OCR",
                extractor=lambda page: ["SSN: I23-45-6789", f"SSN: {VALID_SSN}"],
                note="test", patterns=patterns, hard_variants=2,
            )
            assert [f.secret_name for f in report.findings] == ["ssn"]
        finally:
            doc.close()

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


class TestSharedYamlConfig:
    """A redactor's redact_config.yaml doubles as a rules file, so the
    redactor and the verifier cannot drift out of sync."""

    def _yaml(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "redact_config.yaml"
        path.write_text(body)
        return path

    def test_maps_all_three_sections(self, tmp_path) -> None:
        path = self._yaml(tmp_path, """
entity_types:
  - ssn
  - email
  - person_name
  - address
exact_values:
  - "Jane Doe"
  - "123-45-6789"
patterns:
  - '\\d{3}-\\d{2}-\\d{4}'
backend: lmstudio
model: gemma-4-26b-a4b-qat
scrub_metadata: true
""")
        rules = load_rules(path)
        assert len(rules.secrets) == 2                      # exact_values
        assert len(rules.patterns) == 3                     # 1 regex + ssn + email
        # Unmappable entity types are recorded, never silently dropped.
        assert sorted(rules.unverifiable) == ["address", "person_name"]

    def test_entity_types_use_our_own_classes(self, tmp_path) -> None:
        # Independence: an entity_type maps to THIS tool's regex and
        # validator, not to the redactor's pattern for the same concept.
        path = self._yaml(tmp_path, "entity_types: [ssn]\n")
        patterns = load_rules(path).patterns
        assert len(patterns) == 1
        assert patterns[0].validator is _valid_ssn
        assert patterns[0].regex.pattern == BUILTIN_PATTERN_CLASSES["ssn"][0]

    def test_unverifiable_types_force_exit_2(self, tmp_path) -> None:
        # A clean document must NOT certify as 0 when the config asked
        # for categories this tool cannot search for.
        pdf = _pdf_with_text(tmp_path, "nothing sensitive here")
        path = self._yaml(tmp_path, "entity_types: [person_name]\nexact_values: []\n")
        result = run_verify(pdf, path)
        assert result.returncode == 2, result.stdout
        assert "CANNOT verify" in result.stdout
        assert "person_name" in result.stdout

    def test_exact_values_still_normalize(self, tmp_path) -> None:
        # YAML-sourced value rules get the same normalization as JSON
        # ones, so separator variants still match.
        pdf = _pdf_with_text(tmp_path, "SSN on file: 123 45 6789")
        path = self._yaml(tmp_path, 'exact_values:\n  - "123-45-6789"\n')
        result = run_verify(pdf, path)
        assert result.returncode == 1
        assert "LAYER: Text" in result.stdout

    def test_yaml_patterns_are_case_insensitive(self, tmp_path) -> None:
        # The redactor compiles its patterns IGNORECASE; a shared file
        # must not match differently in the two tools.
        path = self._yaml(tmp_path, "patterns:\n  - 'case-[0-9]{4}'\n")
        patterns = load_rules(path).patterns
        assert "patterns[0]" in match_patterns("Ref CASE-8912", patterns)

    def test_malformed_yaml_exits_2_not_1(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, "clean")
        path = self._yaml(tmp_path, "exact_values: 'not-a-list'\n")
        assert run_verify(pdf, path).returncode == 2

    def test_invalid_regex_in_yaml_rejected(self, tmp_path) -> None:
        path = self._yaml(tmp_path, "patterns:\n  - '(unclosed'\n")
        with pytest.raises(VerifyError, match="invalid regex"):
            load_rules(path)


class TestYamlScalarCoercion:
    """YAML implicit typing must never change what gets searched for."""

    def _yaml(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "cfg.yaml"
        path.write_text(body)
        return path

    def test_unquoted_leading_zero_is_not_octal(self, tmp_path) -> None:
        # Regression: `00123456` parsed as octal int 42798, so the tool
        # searched for "42798" and CERTIFIED CLEAN a document containing
        # the real account number.
        path = self._yaml(tmp_path, "exact_values:\n  - 00123456\n")
        assert [s.normalized for s in load_rules(path).secrets] == ["00123456"]

    def test_scalars_keep_their_literal_text(self, tmp_path) -> None:
        path = self._yaml(
            tmp_path, "exact_values:\n  - 1.50\n  - yes\n  - 0123\n"
        )
        # 1.50 must not become "1.5", yes must not become "True".
        assert [s.normalized for s in load_rules(path).secrets] == [
            "150", "yes", "0123",
        ]

    def test_coercion_divergence_is_warned(self, tmp_path) -> None:
        # A redactor sharing the file reads it with a plain safe_load and
        # would remove a different string — say so, and never exit 0.
        path = self._yaml(tmp_path, "exact_values:\n  - 0123\n")
        rules = load_rules(path)
        assert any("unquoted" in w for w in rules.warnings)

    def test_quoted_values_raise_no_warning(self, tmp_path) -> None:
        # Other warnings are expected here (an absent entity_types key
        # means the full upstream roster); only the coercion one must go.
        path = self._yaml(tmp_path, 'exact_values:\n  - "0123"\n')
        assert not any("unquoted" in w for w in load_rules(path).warnings)

    def test_unquoted_secret_is_still_detected(self, tmp_path) -> None:
        pdf = _pdf_with_text(tmp_path, "Account: 00123456 on file")
        path = self._yaml(tmp_path, "exact_values:\n  - 00123456\n")
        result = run_verify(pdf, path)
        assert result.returncode == 1, result.stdout
        assert "LAYER: Text" in result.stdout


class TestYamlAdapterFidelity:
    """The shared config must never make the verifier scan for less than
    the redactor was told to remove."""

    def _yaml(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "redact_config.yaml"
        path.write_text(body)
        return path

    def test_merge_keys_are_honored(self, tmp_path) -> None:
        # Regression: stripping YAML's implicit resolvers to stop scalar
        # coercion also removed the merge resolver, so `<<: *anchor`
        # silently dropped whole sections the redactor did see.
        path = self._yaml(tmp_path, 'base: &b\n  exact_values: ["SECRET-A"]\n'
                                    "<<: *b\nentity_types: [ssn]\n")
        assert [s.normalized for s in load_rules(path).secrets] == ["secreta"]

    def test_absent_entity_types_means_the_full_roster(self, tmp_path) -> None:
        # Upstream defaults to every type when the key is absent; reading
        # it as "none" certified clean what was never scanned.
        rules = load_rules(self._yaml(tmp_path, 'exact_values: ["x1"]\n'))
        assert len(rules.unverifiable) == 6
        assert len(rules.patterns) == len(ENTITY_TYPE_TO_CLASS)

    def test_unknown_top_level_key_is_warned(self, tmp_path) -> None:
        path = self._yaml(tmp_path, 'exact_value: ["x"]\nentity_types: [email]\n')
        assert any("unrecognized" in w for w in load_rules(path).warnings)

    def test_duplicate_key_rejected(self, tmp_path) -> None:
        path = self._yaml(tmp_path, 'exact_values: ["a1"]\nexact_values: ["b2"]\n')
        with pytest.raises(VerifyError, match="duplicate key"):
            load_rules(path)

    def test_falsy_non_list_section_rejected(self, tmp_path) -> None:
        path = self._yaml(tmp_path, "entity_types: [ssn]\nexact_values: {}\n")
        with pytest.raises(VerifyError, match="must be a list"):
            load_rules(path)

    def test_non_string_entries_rejected(self, tmp_path) -> None:
        # A JSON-shaped entry, or an explicit !!int tag, would otherwise
        # stringify into a search key the user never wrote.
        path = self._yaml(tmp_path, 'exact_values:\n  - name: x\n    value: "1"\n')
        with pytest.raises(VerifyError, match="quoted string"):
            load_rules(path)
        path = self._yaml(tmp_path, "exact_values:\n  - !!int 00123456\n")
        with pytest.raises(VerifyError, match="quoted string"):
            load_rules(path)

    def test_unknown_entity_type_is_an_error(self, tmp_path) -> None:
        # A typo or case variant must be fixed, not reported as an
        # inherent LLM-only coverage gap.
        for bad in ("SSN", "credit-card", "emial"):
            path = self._yaml(tmp_path, f"entity_types: [{bad}]\n")
            with pytest.raises(VerifyError, match="not a known entity type"):
                load_rules(path)

    def test_partial_coverage_is_declared(self, tmp_path) -> None:
        path = self._yaml(tmp_path, "entity_types: [phone]\n")
        assert any("partly verifiable" in w for w in load_rules(path).warnings)

    def test_capture_group_pattern_warns(self, tmp_path) -> None:
        # The redactor removes only group text, so the rule verifies more
        # than it removed.
        path = self._yaml(tmp_path, "patterns:\n  - '(AB|CD)[0-9]{6}'\n")
        assert any("capturing group" in w for w in load_rules(path).warnings)

    def test_yaml_flags_match_the_redactor(self, tmp_path) -> None:
        # IGNORECASE only: adding MULTILINE would make a shared rule mean
        # different things in the two tools.
        path = self._yaml(tmp_path, "patterns:\n  - 'case-[0-9]{4}'\n")
        flags = load_rules(path).patterns[0].regex.flags
        assert flags & re.IGNORECASE and not flags & re.MULTILINE

    def test_entity_types_are_deduped(self, tmp_path) -> None:
        path = self._yaml(tmp_path, "entity_types: [ssn, ssn]\n")
        assert len(load_rules(path).patterns) == 1

    def test_divergence_warning_masks_the_secret(self, tmp_path) -> None:
        # The warning must not re-leak the value it warns about.
        path = self._yaml(tmp_path, "exact_values:\n  - 00123456\n")
        joined = " ".join(load_rules(path).warnings)
        assert "unquoted" in joined
        assert "00123456" not in joined and "42798" not in joined

    def test_divergence_warning_names_only_the_type(self, tmp_path) -> None:
        # Regression (#2): showing mask() of both the coerced value and
        # the literal spec together narrowed a short secret from
        # thousands of candidates to a few dozen, since the coerced value
        # is a deterministic function of the whole spec. The warning must
        # name only what YAML read the value as, with no digit of it.
        path = self._yaml(tmp_path, "exact_values:\n  - 0123456701\n")
        rules = load_rules(path)
        (warning,) = [w for w in rules.warnings if w.code == "RULES_UNQUOTED_VALUE"]
        assert "a number" in warning
        assert "****" not in warning
        assert not re.search(r"\d", warning.replace("exact_values[0]", ""))

    @pytest.mark.parametrize("body, kind", [
        ("exact_values:\n  - yes\n", "a boolean"),
        ("exact_values:\n  - 2024-01-01T10:00:00Z\n", "a date"),
        ("exact_values:\n  - 1.50\n", "a number"),
        ("exact_values:\n  - 0123456701\n", "a number"),
    ])
    def test_divergence_warning_names_the_right_type(
        self, tmp_path, body, kind
    ) -> None:
        path = self._yaml(tmp_path, body)
        (warning,) = [
            w for w in load_rules(path).warnings
            if w.code == "RULES_UNQUOTED_VALUE"
        ]
        assert kind in warning

    def test_non_utf8_config_exits_2(self, tmp_path) -> None:
        # Operational error, never the leak code, and never a traceback.
        path = tmp_path / "latin.yaml"
        path.write_bytes('exact_values:\n  - "Jos\xe9"\n'.encode("latin-1"))
        pdf = _pdf_with_text(tmp_path, "clean")
        result = run_verify(pdf, path)
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
