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

import subprocess
import sys

from .conftest import (
    REPO_ROOT,
    SSN,
    requires_full_env,
    requires_qpdf,
    run_verify,
)


class TestProcessExit:
    """The exit code is the contract; teardown must not be able to eat it.

    run_cli calls os._exit so Apple Vision's native teardown cannot
    SIGKILL the process after the verdict is decided (seen in CI as a
    full report on stdout followed by -9). That skips buffer flushing,
    so these tests pin the two things it could break.
    """

    def test_whole_report_reaches_a_pipe(self, clean_pdf, secrets_file) -> None:
        # stdout is block-buffered when piped — exactly how CI runs this
        # — so an unflushed os._exit would truncate the tail.
        result = run_verify(clean_pdf, secrets_file)
        assert result.returncode in (0, 2)
        assert "[*] Scanning" in result.stdout          # head intact
        # Assert the marker before indexing, so a regressed verdict fails
        # readably instead of raising ValueError from str.index.
        assert "[PASS]" in result.stdout, result.stdout
        # The closing rule is printed after the verdict, so finding it
        # later in the stream proves the tail was flushed, not truncated.
        assert result.stdout.rindex("=" * 70) > result.stdout.index("[PASS]")

    def test_exit_code_survives_for_each_verdict(
        self, clean_pdf, leaky_pdf, secrets_file, tmp_path
    ) -> None:
        assert run_verify(leaky_pdf, secrets_file).returncode == 1
        assert run_verify(tmp_path / "nope.pdf", secrets_file).returncode == 2
        # A negative return code means killed by a signal — the exact
        # failure this hardening exists to prevent.
        assert run_verify(clean_pdf, secrets_file).returncode >= 0

    def test_console_script_path_is_hardened(self, leaky_pdf, secrets_file) -> None:
        # The pdf-verify console script calls verify:run_cli, not main(),
        # so it must carry the same exit code and flush behaviour.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); "
             "import verify; verify.run_cli(sys.argv[2:])",
             str(REPO_ROOT), "--target", str(leaky_pdf),
             "--secrets", str(secrets_file)],
            capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 1
        assert "[FAIL]" in result.stdout, result.stdout
        assert result.stdout.rindex("=" * 70) > result.stdout.index("[FAIL]")


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

    def test_ascii_stdout_degraded_path_exits_2(
        self, clean_pdf, secrets_file, tmp_path
    ) -> None:
        # Regression: the ⚠/✖ report glyphs crashed on ASCII stdout and
        # the traceback turned a degraded PASS into exit 1.
        # PATH points at an empty directory so exiftool/qpdf are missing
        # on every platform (apt installs them into /usr/bin, so a
        # /usr/bin:/bin override would not strip them on Linux).
        empty_path_dir = tmp_path / "empty-path"
        empty_path_dir.mkdir()
        result = run_verify(
            clean_pdf, secrets_file,
            env_overrides={"PATH": str(empty_path_dir), "PYTHONIOENCODING": "ascii"},
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


class TestLiteralCarryWarning:
    """The carry-cap warning must stay unconditional.

    It is noisy — an unclosed '(' inside image data stalls the literal
    scanner exactly as a real one would, so it fires on most
    image-bearing PDFs. An attempt to suppress it argued the raw byte
    sweep covers anything dropped. These tests exist because that
    argument is false in two independent ways, each of which turned a
    correct exit 2 into a silent exit 0.
    """

    class _FakeProc:
        """Stands in for the qpdf Popen, faithfully enough to exercise
        the nonzero-exit branch as well."""

        def __init__(self, data: bytes, tmp_path) -> None:
            self.returncode = None
            path = tmp_path / "qdf.bin"
            path.write_bytes(data)
            self.stdout = path.open("rb", buffering=0)

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self): pass
        def communicate(self, timeout=None): return (b"", b"")

    def _scan(self, data, tmp_path, secrets=(), patterns=()):
        import verify
        proc = self._FakeProc(data, tmp_path)
        report = verify.ScanReport()
        try:
            verify._collect_qpdf(
                proc,
                verify.SecretMatcher(list(secrets) or [verify.Secret("z", "zzzz")]),
                patterns, report)
        finally:
            proc.stdout.close()
        return report

    def test_raw_sweep_does_not_cover_escaped_literals(self) -> None:
        """The invariant a suppression would have to rely on, asserted
        directly so nobody re-derives it from the code."""
        import verify
        source = r"(123\05545\0556789)"          # \055 is '-'
        decoded = verify._unescape_pdf_literal(source[1:-1])
        key = verify.normalize_string("123-45-6789")
        assert verify.normalize_string(decoded) == key       # literal pass finds it
        assert key not in verify.normalize_string(source)    # raw sweep does NOT

    def test_escaped_secret_in_oversize_literal_is_not_silent(self, tmp_path) -> None:
        # QDF writes every non-ASCII byte as an octal escape, so this is
        # the ordinary case for an accented name, not an exotic one.
        import verify
        secret = verify.Secret("target", verify.normalize_string("123-45-6789"))
        data = (b"(unclosed 123\05545\0556789 "
                + b"A" * (verify.MAX_LITERAL_CARRY + 4096))
        report = self._scan(data, tmp_path, secrets=[secret])
        assert report.findings == []          # the literal pass never saw it
        assert report.degraded, "a dropped literal must never be silent"

    def test_pattern_rules_have_no_raw_fallback(self, tmp_path) -> None:
        # Patterns are fed only from decoded literals — the raw sweep is
        # deliberately never given them — so truncation costs 100% of
        # pattern coverage with no backstop at all.
        import re

        import verify
        rule = verify.PatternRule("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"))
        small = self._scan(b"(SSN 123-45-6789)", tmp_path, patterns=[rule])
        assert [f.secret_name for f in small.findings] == ["ssn"]

        big = self._scan(
            b"(unclosed SSN 123-45-6789 " + b"A" * (verify.MAX_LITERAL_CARRY + 4096),
            tmp_path, patterns=[rule])
        assert big.findings == []
        assert big.degraded, "lost pattern coverage must never be silent"

    def test_no_truncation_means_no_warning(self, tmp_path) -> None:
        import verify
        report = self._scan(b"(closed) " + b"A" * 4096, tmp_path)
        assert not any("carry" in w for w in report.warnings)
