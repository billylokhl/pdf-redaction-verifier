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
                               f'exact_values:\n  - "{SSN}"\nentity_types: [ssn]\n', "--strict-classes")
        assert "built-in type: SSN ****" in {b.label for b in the_plan.boxes}
        _assert_absent(out, SSN, "6789")
        # With no exact value it would reveal, the masked tail stays.
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf, "entity_types: [ssn]\n", "--strict-classes")
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
        # Other \d\d matches (in the target's random /ID, say) may show a
        # digit, but never one of the value's.
        assert not any(set(i.label.split(" ")[-1][4:]) & set(SSN) for i in the_plan.listed
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
        # The --json report hides it too (main's mask, #66).
        assert not any("7890" in f["sample"] for f in run.report["findings"])
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

    def test_a_target_name_holding_part_of_a_value(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "Example_Jane_records.pdf")
        run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                                   [{"name": "ssn", "value": SSN}, {"name": "who", "value": NAME}])
        assert out.exists() and the_plan.target_name == "target"
        _assert_absent(out, "Example", "Jane")

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
                               f'exact_values:\n  - "{SSN}"\nentity_types: [ssn]\n', "--strict-classes")
        labels = {b.label for b in the_plan.boxes}
        assert "exact value #1 (exact_values[0])" in labels
        assert "built-in type: SSN ****" in labels      # tail dropped: part of the value
        assert not the_plan.legend
        _assert_absent(out, SSN, "6789")

    def test_step_two_catches_what_step_one_missed(self, tmp_path, monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / f"{SSN}.pdf")
        run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
        monkeypatch.setattr(render.Checker, "clean", lambda self, text, *neutral: text)
        monkeypatch.setattr(plan.Rules, "free_text", lambda self, text, cap, neutral: text)
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
def test_an_escaped_name_showing_part_of_a_value_is_not_drawn(tmp_path, monkeypatch) -> None:
    pdf = _value_pdf(tmp_path / "文書.pdf")
    _run, the_plan, out = drawn(monkeypatch, tmp_path, pdf,
                                [{"name": "ref", "value": "65871234"}])
    assert the_plan.target_name == "target"
    _assert_absent(out, "6587")


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

    def test_a_notice_is_listed_masked_and_not_drawn_as_a_finding(self, tmp_path,
                                                                    monkeypatch) -> None:
        """ADR 0012: under --no-strict-classes a built-in class match no
        listed value made is listed under "Not counted in the verdict",
        masked, and gets no box; the map passes its self-check."""
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "SSN 234-56-7891 here")
        rules = [{"name": "Any SSN", "class": "ssn"}, {"name": "Target", "value": NAME}]
        run, the_plan, out = drawn(monkeypatch, tmp_path, _save(doc, tmp_path / "notice.pdf"),
                                   rules, "--no-strict-classes")
        assert run.code == 2 and run.report is not None and run.report["notices"]
        text = map_text(out)
        assert not the_plan.boxes and "VISIBLE SECRETS FOUND" not in text
        assert "Not counted in the verdict: shaped like a built-in class:" in text
        assert "p.1 built-in type rule #1 ****7891 -- Text" in text
        _assert_absent(out, "234-56-7891", "234-56", NAME)

    def test_the_box_cap_still_draws_a_map(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=2000)
        for row in range(70):
            page.insert_text((20, 20 + row * 28), f"{SSN} {SSN} {SSN}", fontsize=8)
        _run, the_plan, out = drawn(monkeypatch, tmp_path, _save(doc, tmp_path / "many.pdf"))
        assert len(the_plan.boxes) == plan.MAX_BOXES_PER_PAGE
        assert "INCOMPLETE: box cap reached on p.1" in map_text(out)

    def test_the_map_is_the_same_bytes_every_time(self, tmp_path, monkeypatch) -> None:
        """No random /ID: the map of one plan is one byte string, so no
        random hex can match a short rule in a reader's search."""
        pdf = _value_pdf(tmp_path / "same.pdf")
        run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
        data = pdf.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        first = render.build_map(data, digest, run.header)[0]
        assert first == render.build_map(data, digest, run.header)[0]
        assert b"/ID" not in first

    def test_latin_escapes(self) -> None:
        assert render.latin("café — 文") == "café -- \\u{6587}"


# ── Each surface of the self-check, on its own (review of part 2) ────
def _checker() -> render.Checker:
    checker = render.Checker()
    checker.use([plan.Secret("ssn", normalize_string(SSN))],
                [plan.PatternRule("ssn shape", re.compile(r"\d{3}-\d{2}-\d{4}"))])
    return checker


def _clean_writer(text: str = "label") -> render.Writer:
    w = render.Writer()
    w.new_page(300, 200)
    w.text(10, 50, "fixed heading", variable=False)
    w.text(10, 80, text, variable=True)
    w.commit()
    return w


def _saved(w: render.Writer) -> bytes:
    return markers.insert_markers(w.doc.tobytes(garbage=4, deflate=True))


class TestSelfCheckSurfaces:
    def test_a_clean_map_passes(self) -> None:
        w = _clean_writer()
        render.check_saved(_saved(w), _checker(), w)

    def test_a_hit_only_in_a_stream(self) -> None:
        """A stream no page draws (here an unused form XObject): only
        the stream scan can see it."""
        w = _clean_writer()
        xref = w.doc.get_new_xref()
        w.doc.update_object(xref, "<</Type/XObject/Subtype/Form/BBox[0 0 1 1]>>")
        w.doc.update_stream(xref, b"BT (" + SSN.encode() + b") Tj ET")
        page = w.doc[0]
        kind, value = w.doc.xref_get_key(page.xref, "Resources")
        holder = int(value.split()[0]) if kind == "xref" else page.xref
        key = "XObject" if kind == "xref" else "Resources/XObject"
        w.doc.xref_set_key(holder, key, f"<</Fx {xref} 0 R>>")
        assert SSN not in page.get_text()
        with pytest.raises(render.SelfCheckFailed, match="stream"):
            render.check_saved(_saved(w), _checker(), w)

    def test_a_hit_only_in_page_text(self, monkeypatch) -> None:
        """With the stream scan blind (no string token decoded), the page
        text scan alone catches a variable hit."""
        w = _clean_writer(f"x {SSN}")
        monkeypatch.setattr(render, "_STRING_TOKEN", re.compile(rb"(?!)"))
        with pytest.raises(render.SelfCheckFailed, match="page 1 text"):
            render.check_saved(_saved(w), _checker(), w)

    def test_a_hit_in_the_document_information(self) -> None:
        w = _clean_writer()
        w.doc.set_metadata({"title": f"file {SSN}"})
        with pytest.raises(render.SelfCheckFailed, match="document information"):
            render.check_saved(_saved(w), _checker(), w)

    def test_a_hit_in_xmp(self) -> None:
        w = _clean_writer()
        w.doc.set_xml_metadata(f"<x:xmpmeta xmlns:x='adobe:ns:meta/'>{SSN}</x:xmpmeta>")
        with pytest.raises(render.SelfCheckFailed, match="XMP"):
            render.check_saved(_saved(w), _checker(), w)

    @staticmethod
    def _wide_checker() -> render.Checker:
        from redaction_verifier.model import fold_wide
        checker = render.Checker()
        checker.use([plan.Secret("ru", "1985", fold_wide("Иванов Иван 1985")),
                     plan.Secret("ref", "65871234", "65871234")], [])
        return checker

    def test_step_one_sees_a_value_of_any_script(self) -> None:
        checker = self._wide_checker()
        assert checker.hits("file Иванов Иван 1985.pdf")
        assert checker.hits("ИВАНОВ-ИВАН-1985")
        assert not checker.hits("Иванов 1984")

    @pytest.mark.parametrize("text", ["Иванов Иван 1985", "x ИВАНОВ ИВАН 1985 y"])
    def test_step_two_decodes_escapes_and_folds(self, text) -> None:
        """A value of another script is drawn as \\u{XXXX} escapes: the
        saved text is decoded and folded before it is searched, on the
        page and in a stream's string tokens alike."""
        w = _clean_writer(text)
        with pytest.raises(render.SelfCheckFailed, match="page 1 text"):
            render.check_saved(_saved(w), self._wide_checker(), w)
        token = render.latin(text)
        stream = f"BT ({token}) Tj ET"
        tainted = [stream.index(token) <= k < stream.index(token) + len(token)
                   for k in range(len(stream))]
        assert render._tainted_hit(self._wide_checker(), stream, tainted)
        assert not render._tainted_hit(self._wide_checker(), stream, [False] * len(stream))

    @pytest.mark.parametrize("text, leaks", [
        ("rule #1 ****文", True),          # drawn \u{6587}: 6, 5, 8, 7
        ("rule #1 ****0z", False),
        ("rule #1 ****9z", True),          # 9 of 1985
        ("rule #1 ****x1", True),
        ("rule #1 ****", False),
        ("rule #1 ****ab", False),
    ])
    def test_step_two_checks_every_masked_tail(self, text, leaks) -> None:
        w = _clean_writer(text)
        if leaks:
            with pytest.raises(render.SelfCheckFailed, match="page 1 text"):
                render.check_saved(_saved(w), self._wide_checker(), w)
        else:
            render.check_saved(_saved(w), self._wide_checker(), w)

    def test_variable_text_must_read_back_as_variable(self, monkeypatch) -> None:
        """If variable text does not read back in its colour, every hit
        would pass as fixed text: the map is refused."""
        monkeypatch.setattr(render, "VAR", render.INK)
        w = _clean_writer()
        with pytest.raises(render.SelfCheckFailed, match="variable text not found"):
            render.check_saved(_saved(w), _checker(), w)

    def test_step_one_catches_a_hit_only_with_separators_collapsed(self) -> None:
        checker = render.Checker()
        checker.use([], [plan.PatternRule("ssn shape", re.compile(r"\d{3}-\d{2}-\d{4}"))])
        assert not render.match_patterns("ref 123 -- 45 -- 6789", checker.patterns)
        assert checker.clean("ref 123 -- 45 -- 6789", "neutral") == "neutral"

    def test_step_one_catches_a_hit_only_after_escaping(self) -> None:
        checker = render.Checker()
        checker.use([], [plan.PatternRule("escaped", re.compile(r"\\u\{6587\}"))])
        assert checker.clean("文", "neutral") == "neutral"

    def test_step_one_checks_the_form_drawn(self) -> None:
        """A string clean whole but hit once cut to VAR_CAP (the cut drops
        the digit that kept the pattern from matching) is replaced."""
        text = "x" * (render.VAR_CAP - 12) + "123-45-67890zz"
        checker = render.Checker()
        checker.use([], [plan.PatternRule("ssn", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"))])
        assert not checker.hits(text) and checker.hits(render.cap(text))
        assert checker.clean(text, "neutral") == "neutral"

    def test_literal_strings_are_decoded(self) -> None:
        assert render._decode_token(rb"(a\(b\)\\c\101\n)") == "a(b)\\cA\n"
        assert render._decode_token(b"(12\\\n3)") == "123"
        assert render._decode_token(b"<3132 33>") == "123"
        assert render._decode_token(rb"(x\8y)") == "x8y"

    def test_a_map_that_does_not_reopen_cleanly(self) -> None:
        w = _clean_writer()
        data = bytearray(_saved(w))
        start = data.rindex(b"startxref\n") + len(b"startxref\n")
        data[start:start + 1] = b"9" if data[start:start + 1] != b"9" else b"8"
        with pytest.raises(render.SelfCheckFailed, match="reopen"):
            render.check_saved(bytes(data), _checker(), w)


def test_a_decomposed_name_is_drawn_composed() -> None:
    assert render.latin("Jose\u0301") == "Jos\u00e9"
