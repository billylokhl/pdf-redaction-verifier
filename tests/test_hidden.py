"""Tests for the Hidden layer: content no page renders.

These are the categories Acrobat removes in its separate "Remove Hidden
Information" pass. Each test asserts a HARD finding reached without any
external binary, since the qpdf sweep that used to cover some of them
disappears whenever qpdf is not installed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import fitz
import pytest

import verify

from redaction_verifier.matching import SecretMatcher
from redaction_verifier.model import ScanReport, Secret

from .conftest import run_verify

SSN = "123-45-6789"


@pytest.fixture
def ssn_rules(tmp_path: Path) -> Path:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"name": "Target SSN", "value": SSN}]))
    return path


def _doc_with_clean_page() -> fitz.Document:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Visible body text, nothing sensitive")
    return doc


def _scan(path: Path, rules: Path) -> subprocess.CompletedProcess[str]:
    """Run with an EMPTY directory as PATH, so exiftool and qpdf are
    genuinely absent and only this layer can produce a finding.

    An earlier version used "/usr/bin:/bin", which is a no-op on the
    Linux CI job: it apt-installs both tools into /usr/bin, so the qpdf
    sweep supplied the exit 1 and these tests would have passed with the
    Hidden layer deleted. tests/test_cli.py documents the same trap.
    """
    empty = path.parent / "empty-path"
    empty.mkdir(exist_ok=True)
    return run_verify(path, rules, env_overrides={"PATH": str(empty)})


def _hidden_findings(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [ln for ln in result.stdout.splitlines() if "LAYER: Hidden" in ln]


class TestHiddenLayer:
    def test_embedded_attachment_content(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        doc.embfile_add("notes.txt", f"attachment {SSN}".encode(), filename="notes.txt")
        path = tmp_path / "att.pdf"
        doc.save(path)
        doc.close()
        # Attachment BODIES are a run of arbitrary text, so they are the
        # manual-review tier, not a hard finding (exit 2, never silent).
        result = _scan(path, ssn_rules)
        assert result.returncode == 2, result.stdout
        assert "manual review" in result.stdout
        assert "embedded file #0 (content)" in result.stdout

    def test_compressed_attachment_beats_the_byte_sweep(self, tmp_path, ssn_rules) -> None:
        # The decisive case: the literal is not in the file's bytes at
        # all, so no raw-stream sweep could ever find it.
        doc = _doc_with_clean_page()
        payload = ("filler " * 4000 + f"SSN {SSN} " + "filler " * 4000).encode()
        doc.embfile_add("big.txt", payload, filename="big.txt")
        path = tmp_path / "att_zip.pdf"
        doc.save(path, deflate=True)
        doc.close()
        assert SSN.encode() not in path.read_bytes(), "fixture must be compressed"
        result = _scan(path, ssn_rules)
        assert result.returncode == 2, result.stdout
        assert "embedded file #0 (content)" in result.stdout

    def test_attachment_filename_and_description(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        # Filename and description are discrete values -> hard findings.
        # Kept separate so neither can pass on the other's behalf: the
        # description path was silently dead (wrong dict key) while a
        # combined fixture still went green.
        doc.embfile_add("x.txt", b"harmless", filename=f"{SSN}.txt", desc="plain")
        path = tmp_path / "att_name.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert "(filename)" in result.stdout
        # The secret is in the filename, but the location must not print
        # it: locations are never masked, so they carry identity only.
        assert SSN not in result.stdout

    def test_attachment_description_is_scanned(self, tmp_path, ssn_rules) -> None:
        # Regression: embfile_info() returns 'description', not 'desc',
        # so this path was dead and no test noticed.
        doc = _doc_with_clean_page()
        doc.embfile_add("x.txt", b"harmless", filename="harmless.txt",
                        desc=f"Case file for {SSN}")
        path = tmp_path / "att_desc.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert "(description)" in result.stdout

    def test_hidden_annotation(self, tmp_path, ssn_rules) -> None:
        # Flagged hidden, so nothing renders it and OCR cannot see it.
        doc = _doc_with_clean_page()
        annot = doc[0].add_text_annot((300, 300), f"comment {SSN}")
        annot.set_flags(fitz.PDF_ANNOT_IS_HIDDEN)
        annot.update()
        path = tmp_path / "annot.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert any("on page 1" in f for f in _hidden_findings(result))

    def test_form_field_value(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        widget = fitz.Widget()
        widget.field_name = "ssn"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.rect = fitz.Rect(72, 400, 300, 420)
        widget.field_value = f"field {SSN}"
        doc[0].add_widget(widget)
        path = tmp_path / "widget.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert any("form field" in f and "page 1" in f for f in _hidden_findings(result))

    def test_link_target(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 500, 200, 520),
                            "uri": f"https://x.test/?ssn={SSN}"})
        path = tmp_path / "link.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert any("link #0 on page 1" in f for f in _hidden_findings(result))

    def test_clean_document_raises_nothing(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        doc.embfile_add("ok.txt", b"nothing sensitive", filename="ok.txt")
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 500, 200, 520),
                            "uri": "https://example.test/help"})
        path = tmp_path / "clean.pdf"
        doc.save(path)
        doc.close()
        assert _hidden_findings(_scan(path, ssn_rules)) == []

    def test_pattern_rules_apply_too(self, tmp_path) -> None:
        rules = tmp_path / "cls.json"
        rules.write_text(json.dumps([{"name": "Any SSN", "class": "ssn"}]))
        doc = _doc_with_clean_page()
        doc.embfile_add("n.txt", f"attachment {SSN}".encode(), filename="n.txt")
        path = tmp_path / "att_pat.pdf"
        doc.save(path)
        doc.close()
        # Attachment content is the soft tier, so a pattern match there
        # is a masked manual-review warning rather than a hard finding.
        result = _scan(path, rules)
        assert result.returncode == 2, result.stdout
        assert "manual review" in result.stdout and "sample" in result.stdout

    def test_fields_are_matched_individually(self, tmp_path) -> None:
        # One field's tail must not fuse with the next field's head.
        rules = tmp_path / "f.json"
        rules.write_text(json.dumps([{"name": "Fused", "value": "123456789"}]))
        doc = _doc_with_clean_page()
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 500, 200, 520),
                            "uri": "https://x.test/123"})
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 530, 200, 550),
                            "uri": "https://x.test/456789"})
        # Each link is matched on its own, so their text cannot fuse.
        path = tmp_path / "fuse.pdf"
        doc.save(path)
        doc.close()
        assert _hidden_findings(_scan(path, rules)) == []

    def test_layer_survives_a_broken_document(self, tmp_path, ssn_rules) -> None:
        # A sweep failure must degrade to a warning, never crash the run.
        report = ScanReport()

        class Exploding:
            page_count = 1
            def embfile_names(self): raise RuntimeError("boom")

        verify.scan_hidden_objects(
            Exploding(), SecretMatcher([Secret("x", "y")]), (), report
        )
        assert any("Hidden" in w and "NOT scanned" in w for w in report.warnings)
        assert report.findings == []


class TestHiddenLayerRegressions:
    """Each test pins a bug a max-effort review reproduced end to end."""

    def test_js_object_dict_is_not_scanned_as_text(self, tmp_path) -> None:
        # The /JS sweep used to yield the whole object dictionary, so a
        # widget's /Rect fused into a Luhn-valid digit run and hard-FAILed
        # every Acrobat form with a calculation script.
        rules = tmp_path / "card.json"
        rules.write_text(json.dumps([{"name": "any-card", "class": "credit-card"}]))
        doc = _doc_with_clean_page()
        widget = fitz.Widget()
        widget.field_name = "total"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.rect = fitz.Rect(251.8, 455.4, 407.2, 484.4)
        widget.field_value = ""
        doc[0].add_widget(widget)
        for w in doc[0].widgets():
            doc.xref_set_key(
                w.xref, "AA",
                "<</C<</S/JavaScript/JS(this.getField('total').value=0;)>>>>")
        path = tmp_path / "js_fp.pdf"
        doc.save(path)
        doc.close()
        assert _scan(path, rules).returncode != 1

    def test_javascript_stored_as_a_stream_is_scanned(self, tmp_path, ssn_rules) -> None:
        # xref_object returns only the dictionary, so a stream-stored
        # script was invisible — and Flate defeats the qpdf sweep too.
        doc = _doc_with_clean_page()
        js = doc.get_new_xref()
        doc.update_object(js, "<<>>")
        doc.update_stream(js, f"var acct='{SSN}';".encode(), compress=True)
        action = doc.get_new_xref()
        doc.update_object(action, f"<</S/JavaScript/JS {js} 0 R>>")
        doc.xref_set_key(doc.pdf_catalog(), "Names",
                         "<</JavaScript<</Names[(s) %d 0 R]>>>>" % action)
        path = tmp_path / "js_stream.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert any("JavaScript" in f for f in _hidden_findings(result))

    def test_page_level_file_attachment(self, tmp_path, ssn_rules) -> None:
        # embfile_names() covers only the document-level tree, so
        # Acrobat's paperclip attachment was missed entirely.
        doc = _doc_with_clean_page()
        doc[0].add_file_annot(fitz.Point(100, 100),
                              f"payload {SSN}".encode(), "evidence.txt")
        path = tmp_path / "fileannot.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 2, result.stdout
        assert "file attached to annotation" in result.stdout

    def test_undecodable_attachment_degrades(self, tmp_path, ssn_rules) -> None:
        # embfile_get returns b"" instead of raising, so an unread
        # attachment was silently skipped and the run exited 0.
        doc = _doc_with_clean_page()
        doc.embfile_add("b.txt", b"secret " + SSN.encode(), filename="b.txt")
        staged = tmp_path / "staged.pdf"
        doc.save(staged)
        doc.close()
        doc = fitz.open(staged)
        for xref in range(1, doc.xref_length()):
            if "EmbeddedFile" in doc.xref_object(xref):
                doc.update_stream(xref, b"not-deflate-at-all", compress=False)
                doc.xref_set_key(xref, "Filter", "/FlateDecode")
        path = tmp_path / "broken.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 2, result.stdout
        assert "NOT scanned" in result.stdout

    def test_oversized_attachment_is_bounded_and_warned(self, tmp_path, ssn_rules) -> None:
        # A small PDF can declare gigabytes of compressible payload; the
        # uncompressed size is checked before reading allocates.
        doc = _doc_with_clean_page()
        doc.embfile_add("big.bin", b"A" * (verify.MAX_ATTACHMENT_BYTES + 1024),
                        filename="big.bin")
        path = tmp_path / "big.pdf"
        doc.save(path, deflate=True)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 2, result.stdout
        assert "scan limit" in result.stdout

    def test_binary_attachment_never_hard_fails(self, tmp_path) -> None:
        # Mojibake from errors="replace" can synthesize email-shaped
        # runs; that must be manual review, not a confirmed leak.
        import os
        import zlib
        rules = tmp_path / "mail.json"
        rules.write_text(json.dumps([{"name": "any-email", "class": "email"}]))
        doc = _doc_with_clean_page()
        doc.embfile_add("chart.png", zlib.compress(os.urandom(3 * 1024 * 1024), 6),
                        filename="chart.png")
        path = tmp_path / "bin.pdf"
        doc.save(path)
        doc.close()
        assert _scan(path, rules).returncode != 1

    def test_distinct_carriers_do_not_collapse(self, tmp_path) -> None:
        # Locations carried no identity, so Finding dedup silently
        # dropped every leak after the first on a page.
        rules = tmp_path / "anyssn.json"
        rules.write_text(json.dumps([{"name": "any-ssn", "class": "ssn"}]))
        doc = _doc_with_clean_page()
        for i, value in enumerate(("123-45-6789", "078-05-1120", "219-09-9999")):
            widget = fitz.Widget()
            widget.field_name = f"f{i}"
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.rect = fitz.Rect(72, 100 + i * 40, 400, 130 + i * 40)
            widget.field_value = value
            doc[0].add_widget(widget)
        path = tmp_path / "multi.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, rules)
        assert len(_hidden_findings(result)) == 3, result.stdout

    def test_a_bad_page_keeps_earlier_findings(self, tmp_path, ssn_rules) -> None:
        # list(generator) discarded everything already yielded when a
        # later page raised, downgrading a FAIL to PASS-with-warning.
        doc = fitz.open()
        for _ in range(3):
            doc.new_page().insert_text((72, 72), "clean")
        doc[0].add_text_annot((100, 100), f"reviewer note {SSN}")
        xrefs = [doc.page_xref(i) for i in range(3)]
        doc.update_object(
            xrefs[2],
            f"<</Type/Page/Parent {xrefs[2]} 0 R/MediaBox[0 0 612 792]>>")
        path = tmp_path / "cyclic.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert "NOT scanned" in result.stdout      # the bad page still warns

    def test_a_silent_sweep_failure_is_impossible(self, tmp_path) -> None:
        # Every swallow must warn, or the layer can scan nothing and the
        # run still exits 0.
        report = ScanReport()

        class Hostile:
            page_count = 1
            def embfile_names(self): raise RuntimeError("names")
            def load_page(self, i): raise RuntimeError("page")
            def pdf_catalog(self): raise RuntimeError("catalog")
            def get_ocgs(self): raise RuntimeError("ocgs")

        verify.scan_hidden_objects(
            Hostile(), SecretMatcher([Secret("x", "y")]), (), report)
        assert report.findings == []
        assert report.degraded, "a layer that scanned nothing must not be silent"
