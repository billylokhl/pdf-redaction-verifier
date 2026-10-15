"""The visible map's drawing and self-check (docs/DESIGN.md "Drawing the
visible map"): what the map file shows, and that nothing of a value
reaches it — page text, streams, document information, XMP, raw bytes.

Every PDF here is fabricated (SSN 123-45-6789, "Jane Q. Example") and
written under the test's own tmp_path.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf as fitz
import pytest

from redaction_verifier.matching import normalize_string
from redaction_verifier.visible_map import markers, plan, render

from .visible_map_support import (
    NAME,
    SSN,
    SSN_RULES,
    drawn,
    map_strings,
    map_text,
    run_main,
    write_rules,
)


def _save(doc: fitz.Document, path: Path) -> Path:
    doc.save(path)
    doc.close()
    return path


def _value_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), f"SSN {SSN} for {NAME}")
    return _save(doc, path)


def _assert_absent(path: Path, *texts: str) -> None:
    """None of *texts* anywhere a reader of the map file could look: page
    text, decompressed streams and their decoded strings, document
    information, XMP, raw bytes -- verbatim or normalized."""
    every = map_strings(path)
    for text in texts:
        assert text not in every, text
        for line in every.splitlines():
            assert normalize_string(text) not in normalize_string(line), (text, line[:80])


# ── Markers ───────────────────────────────────────────────────────────
def test_markers_keep_the_file_unrepaired() -> None:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "map", fontname="helv")
    marked = markers.insert_markers(doc.tobytes(garbage=4, deflate=True))
    assert marked.split(b"\n")[1] == markers.MARKER
    assert marked.endswith(markers.MARKER_LINE)
    assert markers.markers_in(marked[:1024], marked[-64:]) == (True, True)
    reopened = fitz.open("pdf", marked)
    assert not reopened.is_repaired and reopened[0].get_text().strip() == "map"


def test_a_map_without_markers_fails_the_self_check(tmp_path, monkeypatch) -> None:
    pdf = _value_pdf(tmp_path / "plain.pdf")
    monkeypatch.setattr(markers, "insert_markers", lambda data: data)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
    data = pdf.read_bytes()
    with pytest.raises(render.SelfCheckFailed):
        render.build_map(data, hashlib.sha256(data).hexdigest(), run.header)


# ── Self-check (design §8, §9 test 12) ────────────────────────────────
class TestSelfCheck:
    def test_a_rule_named_after_the_secret(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "named.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf, [{"name": SSN, "value": SSN}])
        text = map_text(out)
        assert out.exists() and "value rule #1" in text
        assert "value rule #1 = 1st value entry, rules-file entry 1" in text
        _assert_absent(out, SSN, "6789")

    def test_a_legacy_rule_name_is_never_shown(self, tmp_path, monkeypatch) -> None:
        # The name is the user's own words: here, private text that is not
        # the value and that no rule would catch.
        pdf = _value_pdf(tmp_path / "private.pdf")
        run, _, out = drawn(monkeypatch, tmp_path, pdf,
                        [{"name": f"{NAME}'s file number", "value": SSN}])
        assert out.exists()
        _assert_absent(out, SSN, "Jane", "Example", "file number")

    def test_a_pattern_tail_that_is_part_of_a_value_is_dropped(self, tmp_path,
                                                                monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "overlap.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                               f'exact_values:\n  - "{SSN}"\nentity_types: [ssn]\n')
        assert "built-in type: SSN ****" in {b.label for b in the_plan.boxes}
        _assert_absent(out, SSN, "6789")
        # With no exact value it would reveal, the masked tail stays.
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf, "entity_types: [ssn]\n")
        assert "built-in type: SSN ****6789" in {b.label for b in the_plan.boxes}

    def test_a_one_character_tail_on_a_value_is_hidden(self, tmp_path, monkeypatch) -> None:
        # \d\d matches 12, 34, 67, 89 inside the value; mask() of each shows
        # one digit of it.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"Ref {SSN} end")
        pdf = _save(doc, tmp_path / "pairs.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                               [{"name": "ssn", "value": SSN}, {"name": "pair", "pattern": r"\d\d"}])
        pairs = [b for b in the_plan.boxes if b.label.startswith("pattern rule #1")]
        assert pairs and {b.label for b in pairs} == {"pattern rule #1 ****"}
        assert all("****" == i.label.split(" ")[-1] for i in the_plan.listed
                   if i.label.startswith("pattern rule #1"))

    def test_a_one_character_report_sample_on_a_value_is_hidden(self) -> None:
        rules = plan.Rules({"secrets": [["ssn", "123456789"]], "patterns": [],
                            "labels": [["ssn", "value", "'ssn'"]]}, lambda t, *n: t)
        assert rules.masked("****9") == "****"
        assert rules.masked("****0") == "****0"

    def test_a_pattern_running_past_a_value_hides_its_tail(self, tmp_path, monkeypatch) -> None:
        # 123-45-67890 holds the value and runs on: mask() would show 7890,
        # three of the value's characters -- in the box label and in the
        # live-content entry built from the --json report's sample alike.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Account 123-45-67890 ok")
        pdf = _save(doc, tmp_path / "runon.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                               f'exact_values:\n  - "{SSN}"\npatterns:\n'
                               '  - "\\\\d{3}-\\\\d{2}-\\\\d{5}"\n')
        assert any("****7890" in f["sample"] for f in run.report["findings"])
        _assert_absent(out, SSN, "7890", "789", "6789")
        assert "pattern #1 (patterns[0]) ****" in {b.label for b in the_plan.boxes}

    def test_a_pattern_that_matches_masks_and_counts(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "digits.pdf")
        rules = [{"name": "four", "pattern": r"\d{4}"}, {"name": "ssn", "class": "ssn"}]
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf, rules)
        assert out.exists(), run.stderr
        text = map_text(out)
        # mask() of a match would show "6789", which \d{4} matches: the
        # neutral form is drawn instead.
        assert "****6789" not in text and "****" in text
        _assert_absent(out, SSN, "6789")

    def test_a_target_named_after_the_secret(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / f"{SSN}.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf)
        assert out.exists() and the_plan.target_name == "target"
        _assert_absent(out, SSN)

    def test_non_latin_names(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "文書.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                               [{"name": "社会保障", "value": SSN}])
        assert out.exists()
        text = map_text(out)
        assert "\\u{6587}\\u{66F8}.pdf" in text and "value rule #1" in text

    def test_yaml_labels(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "yaml.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                               f'exact_values:\n  - "{SSN}"\nentity_types: [ssn]\n')
        labels = {b.label for b in the_plan.boxes}
        assert "exact value #1 (exact_values[0])" in labels
        assert "built-in type: SSN ****" in labels      # tail dropped: part of the value
        assert not the_plan.legend
        _assert_absent(out, SSN, "6789")

    def test_step_two_catches_what_step_one_missed(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / f"{SSN}.pdf")
        run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
        monkeypatch.setattr(render.Checker, "clean", lambda self, text, *neutral: text)
        data = pdf.read_bytes()
        with pytest.raises(render.SelfCheckFailed):
            render.build_map(data, hashlib.sha256(data).hexdigest(), run.header)

    def test_variable_text_reads_back_in_its_own_colour(self) -> None:
        w = render.Writer()
        w.new_page(300, 200)
        w.text(10, 50, "fixed", variable=False)
        w.text(60, 50, "variable", variable=True)
        w.commit()
        doc = fitz.open("pdf", w.doc.tobytes())
        colours = {s["text"]: s["color"] for b in doc[0].get_text("dict")["blocks"]
                   for ln in b["lines"] for s in ln["spans"]}
        assert colours["variable"] == render.VAR_HEX
        assert colours["fixed"] != render.VAR_HEX

    def test_fixed_text_hits_are_tolerated(self) -> None:
        checker = render.Checker()
        checker.use([], [plan.PatternRule("four", re.compile(r"\d{4}"))])
        w = render.Writer()
        w.new_page(300, 200)
        w.text(10, 50, "sha256 1234abcd", variable=False)
        w.text(10, 80, "label", variable=True)
        w.commit()
        data = markers.insert_markers(w.doc.tobytes(garbage=4, deflate=True))
        render.check_saved(data, checker, w)
        w2 = render.Writer()
        w2.new_page(300, 200)
        w2.text(10, 50, "x 1234", variable=True)
        w2.commit()
        data = markers.insert_markers(w2.doc.tobytes(garbage=4, deflate=True))
        with pytest.raises(render.SelfCheckFailed):
            render.check_saved(data, checker, w2)


# ── Rendering (design §6) ─────────────────────────────────────────────
class TestRendering:
    def test_a_small_page_gets_a_band(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        doc.new_page(width=100, height=60).insert_text((5, 30), SSN, fontsize=6)
        _run, _plan, out = drawn(monkeypatch, tmp_path, _save(doc, tmp_path / "small.pdf"))
        page = fitz.open(out)[1]
        assert page.rect.height == 60 + render.BAND and page.rect.width >= 144
        assert "[1] value rule #1, selectable text" in page.get_text()

    def test_the_headline_and_lists_are_drawn(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"SSN {SSN}")
        page.add_text_annot((100, 300), f"note {SSN}")
        _run, the_plan, out = drawn(monkeypatch, tmp_path, _save(doc, tmp_path / "two.pdf"))
        text = map_text(out)
        assert render.latin(the_plan.headline) in text
        assert "VISIBLE SECRETS FOUND" in text
        assert "selectable text" in text
        _assert_absent(out, SSN, "6789")

    def test_the_box_cap_still_draws_a_map(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=2000)
        for row in range(70):
            page.insert_text((20, 20 + row * 28), f"{SSN} {SSN} {SSN}", fontsize=8)
        _run, the_plan, out = drawn(monkeypatch, tmp_path, _save(doc, tmp_path / "many.pdf"))
        assert len(the_plan.boxes) == plan.MAX_BOXES_PER_PAGE
        assert "INCOMPLETE: box cap reached on p.1" in map_text(out)

    def test_latin_escapes(self) -> None:
        assert render.latin("café — 文") == "café -- \\u{6587}"
