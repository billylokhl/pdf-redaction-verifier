"""End-to-end CLI regression tests: exit codes, layers, degradation.

Contract under test: exit 0 = certified clean, 1 = secret detected,
2 = operational error / uncertifiable scan. Several tests pin bugs where
the wrong exit code (or a silent clean verdict) was previously produced.
"""

from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest

from .conftest import (
    SSN,
    requires_full_env,
    requires_qpdf,
    run_verify,
)


class TestExitCodeContract:
    def test_missing_secrets_file_exits_2(self, clean_pdf, tmp_path) -> None:
        # Regression: operational errors exited 1 (the leak code).
        result = run_verify(clean_pdf, tmp_path / "nope.json")
        assert result.returncode == 2

    def test_malformed_secrets_exits_2(self, clean_pdf, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text('{"not": "a list"}')
        assert run_verify(clean_pdf, bad).returncode == 2

    def test_empty_normalized_secret_exits_2(self, clean_pdf, tmp_path) -> None:
        bad = tmp_path / "empty.json"
        bad.write_text(json.dumps([{"name": "dashes", "value": "---"}]))
        assert run_verify(clean_pdf, bad).returncode == 2

    def test_missing_target_exits_2(self, secrets_file, tmp_path) -> None:
        assert run_verify(tmp_path / "nope.pdf", secrets_file).returncode == 2

    def test_ascii_stdout_degraded_path_exits_2(self, clean_pdf, secrets_file) -> None:
        # Regression: the ⚠/✖ report glyphs crashed on ASCII stdout and
        # the traceback turned a degraded PASS into exit 1.
        result = run_verify(
            clean_pdf, secrets_file,
            env_overrides={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "ascii"},
        )
        assert result.returncode == 2
        assert "Traceback" not in result.stderr


@requires_full_env
class TestBaselines:
    def test_leaky_pdf_flags_all_four_layers(self, leaky_pdf, secrets_file) -> None:
        result = run_verify(leaky_pdf, secrets_file)
        assert result.returncode == 1
        for layer in ("LAYER: DOM", "LAYER: OCR", "LAYER: Metadata", "LAYER: Binary"):
            assert layer in result.stdout, f"expected a Finding for {layer}"

    def test_clean_pdf_certified(self, clean_pdf, secrets_file) -> None:
        result = run_verify(clean_pdf, secrets_file)
        assert result.returncode == 0
        assert "[PASS]" in result.stdout

    def test_fail_fast_skips_later_layers(self, leaky_pdf, secrets_file) -> None:
        result = run_verify(leaky_pdf, secrets_file, "--fail-fast")
        assert result.returncode == 1
        assert "skipped (--fail-fast" in result.stdout


@requires_full_env
class TestDetectionGaps:
    def test_cross_page_secret_detected(self, secrets_file, tmp_path) -> None:
        # Regression: per-page haystacks missed secrets spanning a page
        # boundary; the tool certified a visibly leaking document clean.
        path = tmp_path / "split.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 700), "Subject SSN: 123-45-")
        doc.new_page().insert_text((72, 72), "6789 (continued)")
        doc.save(path)
        doc.close()
        result = run_verify(path, secrets_file)
        assert result.returncode == 1
        assert "across page boundaries" in result.stdout


@requires_full_env
class TestFalsePositives:
    def test_secret_digits_in_path_do_not_fail(self, secrets_file, tmp_path) -> None:
        # Regression: exiftool's SourceFile/Directory fields let the
        # local filesystem path trigger a Metadata-layer FAIL.
        case_dir = tmp_path / f"case-{SSN}"
        case_dir.mkdir()
        path = case_dir / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "nothing here")
        doc.save(path)
        doc.close()
        assert run_verify(path, secrets_file).returncode == 0

    def test_numeric_operand_collision_is_warning_not_fail(
        self, secrets_file, tmp_path
    ) -> None:
        # Regression: adjacent content-stream operands ("123.45 6789 m")
        # normalized into the SSN and produced a hard Binary FAIL on a
        # clean PDF. They must surface as a manual-review warning
        # (exit 2), never as a confirmed leak (exit 1).
        path = tmp_path / "collision.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean document")
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        xref = doc[0].get_contents()[0]
        doc.update_stream(xref, doc.xref_stream(xref) + b"\n123.45 6789 m 200 200 l S\n")
        path2 = tmp_path / "collision2.pdf"
        doc.save(path2)
        doc.close()

        result = run_verify(path2, secrets_file)
        assert result.returncode == 2
        assert "[PASS]" in result.stdout
        assert "manual review" in result.stdout


@requires_qpdf
class TestDegradation:
    def test_truncated_pdf_never_silently_clean(self, secrets_file, tmp_path) -> None:
        # Regression (twice!): qpdf exits 3 on a truncated input while
        # emitting a partial QDF stream; accepting that output as a
        # complete scan produced a silent false PASS. Any nonzero qpdf
        # exit must degrade the verdict to 2.
        big = tmp_path / "big.pdf"
        doc = fitz.open()
        for i in range(30):
            doc.new_page().insert_text((72, 72), f"benign filler page {i}")
        doc.save(big)
        doc.close()
        truncated = tmp_path / "truncated.pdf"
        data = big.read_bytes()
        truncated.write_bytes(data[: len(data) // 2])

        result = run_verify(truncated, secrets_file)
        assert result.returncode == 2, (
            "truncated input must never be certified clean"
        )
