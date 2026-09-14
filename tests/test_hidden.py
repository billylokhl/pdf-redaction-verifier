"""Tests for the Hidden layer: content no page renders.

These are the categories Acrobat removes in its separate "Remove Hidden
Information" pass. Each test asserts a HARD finding reached without any
external binary, since the qpdf sweep that used to cover some of them
disappears whenever qpdf is not installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import fitz
import pytest

import verify

from .conftest import REPO_ROOT, run_verify

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
    """Run with a PATH that has no exiftool/qpdf, proving this layer
    stands on its own."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "verify.py"),
         "--target", str(path), "--secrets", str(rules)],
        capture_output=True, text=True, timeout=300,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )


def _hidden_findings(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [ln for ln in result.stdout.splitlines() if "LAYER: Hidden" in ln]


class TestHiddenLayer:
    def test_embedded_attachment_content(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        doc.embfile_add("notes.txt", f"attachment {SSN}".encode(), filename="notes.txt")
        path = tmp_path / "att.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert any("embedded file" in f for f in _hidden_findings(result))

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
        assert result.returncode == 1, result.stdout
        assert any("embedded file" in f for f in _hidden_findings(result))

    def test_attachment_filename_and_description(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        doc.embfile_add("x.txt", b"harmless", filename=f"{SSN}.txt",
                        desc=f"about {SSN}")
        path = tmp_path / "att_meta.pdf"
        doc.save(path)
        doc.close()
        assert _scan(path, ssn_rules).returncode == 1

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
        assert any("annotation on page 1" in f for f in _hidden_findings(result))

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
        assert any("form field on page 1" in f for f in _hidden_findings(result))

    def test_link_target(self, tmp_path, ssn_rules) -> None:
        doc = _doc_with_clean_page()
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 500, 200, 520),
                            "uri": f"https://x.test/?ssn={SSN}"})
        path = tmp_path / "link.pdf"
        doc.save(path)
        doc.close()
        result = _scan(path, ssn_rules)
        assert result.returncode == 1, result.stdout
        assert any("link target on page 1" in f for f in _hidden_findings(result))

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
        result = _scan(path, rules)
        assert result.returncode == 1, result.stdout
        assert any("sample" in f for f in _hidden_findings(result))

    def test_fields_are_matched_individually(self, tmp_path) -> None:
        # One field's tail must not fuse with the next field's head.
        rules = tmp_path / "f.json"
        rules.write_text(json.dumps([{"name": "Fused", "value": "123456789"}]))
        doc = _doc_with_clean_page()
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 500, 200, 520),
                            "uri": "https://x.test/123"})
        doc[0].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 530, 200, 550),
                            "uri": "https://x.test/456789"})
        path = tmp_path / "fuse.pdf"
        doc.save(path)
        doc.close()
        assert _hidden_findings(_scan(path, rules)) == []

    def test_layer_survives_a_broken_document(self, tmp_path, ssn_rules) -> None:
        # A sweep failure must degrade to a warning, never crash the run.
        report = verify.ScanReport()

        class Exploding:
            page_count = 1
            def embfile_names(self): raise RuntimeError("boom")

        verify.scan_hidden_objects(
            Exploding(), verify.SecretMatcher([verify.Secret("x", "y")]), (), report
        )
        assert any("Hidden" in w and "NOT scanned" in w for w in report.warnings)
        assert report.findings == []
