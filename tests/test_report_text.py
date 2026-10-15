"""The plain-language human report (redaction_verifier/report/text.py).

Every finding line must say the secret is STILL in the file, name the
rule without revealing any of an exact value, and the report must
explain each kind of finding that occurs. Fabricated data only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz
import pytest

from redaction_verifier.model import ScanReport
from redaction_verifier.report import print_report
from redaction_verifier.rules import load_rules

from .conftest import SSN, ReportLine, report_banner, report_lines, run_verify

VALUE = "Jane Q. Example"  # fabricated


def _yaml_rules(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(body)
    return path


def _render(capsys, report: ScanReport, rules_path: Path | None = None, **kw) -> str:
    rules = load_rules(rules_path) if rules_path is not None else None
    print_report(report, Path("doc.pdf"), rules=rules, **kw)
    return str(capsys.readouterr().out)


@pytest.fixture
def yaml_rules(tmp_path: Path) -> Path:
    return _yaml_rules(
        tmp_path,
        f'exact_values:\n  - "{VALUE}"\n  - "Another Value"\n'
        'patterns:\n  - "ACCT-[0-9]{4}"  # account numbers\n'
        "entity_types:\n  - ssn\n  - email\n",
    )


def _leaky_report() -> ScanReport:
    report = ScanReport()
    report.record("Objects", "exact_values[0]", "ORPHANED object 143", storage="orphaned")
    report.record("Text", "patterns[0]", "page 3 (visual text layer)", "ACCT-5916", page=3)
    report.record("OCR", "entity_types:ssn", "page 1", SSN, page=1)
    return report


class TestParser:
    def test_positive_control(self, capsys, yaml_rules) -> None:
        # The shared parser must find every line of a known FAIL, so the
        # "layer not in report_layers(...)" assertions elsewhere cannot go
        # vacuous after a format change.
        out = _render(capsys, _leaky_report(), yaml_rules)
        assert report_banner(out) == "FAIL"
        assert report_lines(out) == [
            ReportLine("exact value #1 (exact_values[0])", "Objects", "left over",
                       "ORPHANED object 143"),
            ReportLine("pattern #1 (patterns[0]) ****5916", "Text", "on a page",
                       "page 3 (visual text layer)"),
            ReportLine("built-in type: SSN ****6789", "OCR", "on a page", "page 1"),
        ]

    def test_an_unparseable_finding_line_fails_loudly(self) -> None:
        with pytest.raises(AssertionError, match="unparsed finding line"):
            report_lines("  ✖ STILL PRESENT | broken")


class TestBanners:
    def test_fail_banner_counts_by_source_kind(self, capsys, yaml_rules) -> None:
        out = _render(capsys, _leaky_report(), yaml_rules)
        assert "[FAIL]  SECRETS ARE STILL IN THIS FILE: doc.pdf" in out
        flat = " ".join(out.split())
        assert ("Found: 1 of your 2 exact values, 1 of your 1 patterns, "
                "1 of your 2 built-in types.") in flat
        assert "the redaction did not remove them" in flat
        assert "there may be more" not in flat

    def test_default_entity_types_are_called_out(self, capsys, tmp_path) -> None:
        rules = _yaml_rules(tmp_path, f'exact_values:\n  - "{VALUE}"\n')
        report = ScanReport()
        report.record("Text", "entity_types:ssn", "page 1", SSN)
        flat = " ".join(_render(capsys, report, rules).split())
        assert "built-in types checked by default" in flat
        assert "because your secrets file does not list entity_types" in flat

    def test_fail_fast_note_only_when_checks_were_skipped(self, capsys, yaml_rules) -> None:
        report = _leaky_report()
        assert "there may be more" not in _render(capsys, report, yaml_rules)
        report.warn("SKIPPED_FAIL_FAST", "OCR", "OCR: skipped (--fail-fast after earlier finding)")
        out = _render(capsys, report, yaml_rules)
        assert "Later checks were skipped (--fail-fast); there may be more." in out

    def test_unwritten_json_report_is_not_certified(self, capsys, yaml_rules) -> None:
        out = _render(capsys, ScanReport(), yaml_rules, json_unwritten=True)
        assert report_banner(out) == "NOT CERTIFIED"
        assert "[PASS]" not in out
        assert "JSON report could not be written -- so this is NOT a certified clean result" \
            in " ".join(out.split())

    def test_pass_with_warnings_is_not_certified(self, capsys, yaml_rules) -> None:
        report = ScanReport()
        report.warn("PAGE_FAILED", "OCR", "OCR: page 1 failed")
        out = _render(capsys, report, yaml_rules)
        assert report_banner(out) == "NOT CERTIFIED"
        assert "[PASS]" not in out
        assert "NOT a certified clean result (exit code 2)" in " ".join(out.split())
        assert "OCR: page 1 failed" in out

    def test_clean_pass_unchanged(self, capsys, yaml_rules) -> None:
        out = _render(capsys, ScanReport(), yaml_rules)
        assert report_banner(out) == "PASS"
        assert "No target secrets detected in: doc.pdf" in out
        assert "WHAT THIS MEANS" not in out

    def test_fail_with_warnings_says_they_are_additional(self, capsys, yaml_rules) -> None:
        report = _leaky_report()
        report.warn("PAGE_FAILED", "OCR", "OCR: page 2 failed")
        out = _render(capsys, report, yaml_rules)
        assert report_banner(out) == "FAIL"
        assert "these are additional possible problems" in out


class TestLabels:
    def test_no_character_of_an_exact_value_is_shown(self, capsys, yaml_rules) -> None:
        out = _render(capsys, _leaky_report(), yaml_rules)
        for token in ("Jane", "Example", "Another", "Value"):
            assert token not in out
        # ...nor the YAML comment beside a pattern.
        assert "account" not in out

    def test_pattern_and_builtin_samples_stay_masked(self, capsys, yaml_rules) -> None:
        out = _render(capsys, _leaky_report(), yaml_rules)
        assert "ACCT" not in out and "123-45" not in out
        assert "5916" in out and "6789" in out

    def test_native_names_are_quoted_sanitized_and_capped(self, capsys, tmp_path) -> None:
        name = "evil | Text | \x1b[31mspoof" + "x" * 60
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": name, "value": VALUE},
                                     {"name": "card", "class": "credit-card"}]))
        report = ScanReport()
        report.record("Metadata", name, "Title")
        out = _render(capsys, report, rules)
        assert "\x1b" not in out
        line = next(ln for ln in out.splitlines() if "STILL PRESENT" in ln)
        label = line.split("STILL PRESENT | ", 1)[1]
        # "|" cannot fake a column: the parser still sees five columns.
        assert label.startswith("'evil ¦ Text ¦ ")
        assert label.split("…", 1)[0].__len__() == 39  # capped at 40 incl. "…"
        (parsed,) = report_lines(out)
        assert (parsed.layer, parsed.where, parsed.location) == ("Metadata", "properties", "Title")
        assert VALUE not in out
        assert "Found: 1 of your 1 values, 0 of your 1 built-in types." in " ".join(out.split())

    def test_unlabelled_rule_falls_back_to_its_name(self, capsys) -> None:
        report = ScanReport()
        report.record("Text", "some-rule", "page 1")
        out = _render(capsys, report)
        assert report_lines(out)[0].label == "'some-rule'"


class TestWhatThisMeans:
    @pytest.mark.parametrize(
        ("layer", "location", "storage", "tag", "heading"),
        [
            ("Text", "page 1 (visual text layer)", "live", "on a page", "IN THE PAGE'S TEXT"),
            ("OCR", "page 1", "live", "on a page", "VISIBLE ON THE PAGE"),
            ("Objects", "object 12", "live", "stored", "STORED IN THE FILE'S OBJECTS"),
            ("Objects", "ORPHANED object 5", "orphaned", "left over", "HIDDEN, BUT NOT REMOVED"),
            ("Objects", "ORPHANED object 6 (XMP metadata)", "orphaned", "left over",
             "older copy of the document's metadata"),
            ("Objects", "object 7", "unreferenced", "left over?", "PROBABLY HIDDEN"),
            ("Objects", "earlier revision 1, object 8", "superseded", "earlier version",
             "IN AN OLDER VERSION OF THE FILE"),
            ("Metadata", "Title", "live", "properties", "IN THE DOCUMENT'S PROPERTIES"),
            ("Hidden", "annotation 4 on page 1 (contents)", "live", "comment or note",
             "IN A COMMENT OR NOTE"),
            ("Hidden", "form field 7 on page 1 (value)", "live", "form field", "IN A FORM FIELD"),
            ("Hidden", "form field 7 on page 1 (name)", "live", "form field", "or its name"),
            ("Hidden", "link #0 on page 1 (uri)", "live", "link", "IN A LINK"),
            ("Hidden", "embedded file #1 (name)", "live", "attachment", "ATTACHMENT'S NAME"),
            ("Hidden", "JavaScript in object 9", "live", "script", "JAVASCRIPT"),
            ("Hidden", "optional-content group 3 (name)", "live", "layer name", "IN A LAYER NAME"),
            ("Hidden", "something new", "live", "other", "NOT PAGE TEXT"),
        ],
    )
    def test_each_kind_is_tagged_and_explained(
        self, capsys, yaml_rules, layer, location, storage, tag, heading
    ) -> None:
        report = ScanReport()
        report.record(layer, "exact_values[0]", location, storage=storage)
        out = _render(capsys, report, yaml_rules)
        (line,) = report_lines(out)
        assert (line.layer, line.where, line.location) == (layer, tag, location)
        meaning = " ".join(out.split("WHAT THIS MEANS", 1)[1].split())
        assert heading in meaning

    def test_only_kinds_that_occur_are_explained(self, capsys, yaml_rules) -> None:
        report = ScanReport()
        report.record("Text", "exact_values[0]", "page 1")
        meaning = _render(capsys, report, yaml_rules).split("WHAT THIS MEANS", 1)[1]
        assert "IN THE PAGE'S TEXT" in meaning
        assert "HIDDEN, BUT NOT REMOVED" not in meaning
        assert "pattern #N" not in meaning and "exact value #N" in meaning

    def test_garbage_collection_advice_for_orphans(self, capsys, yaml_rules) -> None:
        flat = " ".join(_render(capsys, _leaky_report(), yaml_rules).split())
        assert 'save the file with "garbage collection"' in flat


class TestCliWiring:
    """The CLI must hand print_report the rules (labels, counts); calling
    print_report directly, as the tests above do, cannot catch that."""

    def _scan(self, tmp_path: Path, *extra: str):
        pdf = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"SSN {SSN}")
        doc.save(pdf)
        doc.close()
        rules = _yaml_rules(tmp_path, f'exact_values:\n  - "{SSN}"\nentity_types: []\n')
        return run_verify(pdf, rules, *extra)

    def test_labels_and_counts_reach_the_cli_report(self, tmp_path) -> None:
        result = self._scan(tmp_path)
        assert result.returncode == 1, result.stdout
        assert {ln.label for ln in report_lines(result.stdout)} == {
            "exact value #1 (exact_values[0])"}
        assert "Found: 1 of your 1 exact values." in " ".join(result.stdout.split())
        assert "123-45" not in result.stdout

    def test_fail_fast_note_reaches_the_cli_report(self, tmp_path) -> None:
        result = self._scan(tmp_path, "--fail-fast")
        assert result.returncode == 1, result.stdout
        assert "Later checks were skipped (--fail-fast); there may be more." in result.stdout


class TestMaskedTailNeverShowsAnExactValue:
    """Issue #66: the human report drops a pattern sample's masked tail when
    it overlaps an exact value. Fabricated data."""

    @staticmethod
    def _line(capsys, tmp_path, sample: str, rule_list: list[dict]) -> str:
        rules_path = tmp_path / "rules.json"
        rules_path.write_text(json.dumps(rule_list))
        report = ScanReport()
        report.record("Text", "any", "page 1 (visual text layer)", sample, page=1)
        out = _render(capsys, report, rules_path)
        return next(ln for ln in out.splitlines() if "STILL PRESENT" in ln)

    def test_builtin_type_overlapping_an_exact_value(self, capsys, tmp_path) -> None:
        # Before the fix: "****6789".
        line = self._line(capsys, tmp_path, SSN, [
            {"name": "any", "class": "ssn"}, {"name": "v", "value": SSN}])
        assert "****" in line and "6789" not in line

    def test_user_pattern_overlapping_an_exact_value(self, capsys, tmp_path) -> None:
        # Before the fix: "****7890".
        line = self._line(capsys, tmp_path, "123-45-67890", [
            {"name": "any", "pattern": r"\d{3}-\d{2}-\d{5}"}, {"name": "v", "value": SSN}])
        assert "****" in line and "7890" not in line

    def test_one_character_tail(self, capsys, tmp_path) -> None:
        # "ab" reveals only "b"; that character is in the value.
        line = self._line(capsys, tmp_path, "ab", [
            {"name": "any", "pattern": "ab"}, {"name": "v", "value": "Xbq"}])
        assert line.split("|")[1].rstrip().endswith("****")

    def test_sample_containing_the_whole_value(self, capsys, tmp_path) -> None:
        line = self._line(capsys, tmp_path, "123-45-60000", [
            {"name": "any", "pattern": r"\d{3}-\d{2}-\d{5}"}, {"name": "v", "value": "123-45"}])
        assert "0000" not in line

    def test_non_overlapping_control_keeps_its_tail(self, capsys, tmp_path) -> None:
        line = self._line(capsys, tmp_path, "123-45-67890", [
            {"name": "any", "pattern": r"\d{3}-\d{2}-\d{5}"}, {"name": "v", "value": VALUE}])
        assert "****7890" in line


    def test_non_ascii_tail_of_an_exact_value_is_dropped(self, capsys, tmp_path) -> None:
        # "-O-slash" is the start of the exact value; ASCII-only folding
        # used to lose it and show "****-O-slash".
        line = self._line(capsys, tmp_path, "ABC-Ø", [
            {"name": "any", "pattern": r"[A-Z]{3}-\w"}, {"name": "v", "value": "Øvre 12"}])
        assert "Ø" not in line
