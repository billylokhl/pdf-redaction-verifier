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

import verify

import subprocess
import sys

from .conftest import (
    REPO_ROOT,
    SSN,
    requires_exiftool,
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
    def test_leaky_pdf_flags_every_finding_layer(self, leaky_pdf, secrets_file) -> None:
        # Binary is no longer a finding layer: literal decoding moved to
        # the structural Objects pass, and qpdf is now the orphan
        # backstop, which can only raise manual-review warnings.
        result = run_verify(leaky_pdf, secrets_file)
        assert result.returncode == 1
        for layer in ("LAYER: Text", "LAYER: OCR", "LAYER: Metadata", "LAYER: Objects"):
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


class TestLayerCrashesDegrade:
    def test_an_in_process_layer_crash_never_escapes(
        self, clean_pdf, secrets_file, monkeypatch, capsys
    ) -> None:
        # Regression: the Metadata/Hidden/Objects calls were unguarded
        # while Text and OCR were not, so a crash there printed a
        # traceback and exited 1 — the leak code — on a clean document.
        monkeypatch.setattr(verify, "scan_pdf_objects", _boom)
        code = verify.main(["--target", str(clean_pdf), "--secrets", str(secrets_file)])
        assert code == 2
        assert "Objects: layer crashed (boom)" in capsys.readouterr().out


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


class TestRawSweepIsNotAnEquivalentBackstop:
    """Why literal decoding must exist as its own pass.

    A previous change tried to treat the raw byte sweep as a substitute
    for decoding literals. It is not, and these assert the two reasons
    directly so the argument cannot be re-derived from the code.
    """

    def test_raw_sweep_does_not_cover_escaped_literals(self) -> None:
        # QDF writes every non-ASCII byte as an octal escape, and
        # normalize_string keeps the escape's digits.
        source = r"(123\05545\0556789)"          # \055 is '-'
        decoded = verify._unescape_pdf_literal(source[1:-1])
        key = verify.normalize_string("123-45-6789")
        assert verify.normalize_string(decoded) == key       # decoded: found
        assert key not in verify.normalize_string(source)    # raw: not found

    def test_raw_sweep_is_never_given_pattern_rules(self, tmp_path) -> None:
        # Patterns over raw latin-1 byte soup would false-positive on
        # compressed data, so the raw sweep never receives them — which
        # is why it cannot stand in for the structural pass.
        import re

        path = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Applicant SSN: 123-45-6789")
        doc.save(path)
        doc.close()
        report = verify.ScanReport()
        doc = fitz.open(path)
        try:
            verify.scan_pdf_objects(
                doc, verify.SecretMatcher([verify.Secret("z", "zzzz")]),
                [verify.PatternRule("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"))],
                report)
        finally:
            doc.close()
        # The structural pass is the only thing that can produce this.
        assert [f.secret_name for f in report.findings] == ["ssn"]


class TestUncaughtExceptionsNeverExitTheLeakCode:
    """Regression (#1): an exception escaping late in main() — print_report
    here, but the same class covers start_hidden_tools, doc.page_count, or
    the JSON writer — used to propagate all the way out. Under run_cli
    that is a bare Python traceback and exit code 1: the "a secret was
    found" code, on a document where nothing was actually found.
    """

    def test_run_cli_exits_2_not_1(self, clean_pdf, secrets_file) -> None:
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import verify; "
             "verify.print_report = lambda *a, **k: (_ for _ in ()).throw("
             "RuntimeError('boom')); "
             "verify.run_cli(sys.argv[2:])",
             str(REPO_ROOT), "--target", str(clean_pdf), "--secrets", str(secrets_file)],
            capture_output=True, text=True, timeout=300,
        )
        assert result.returncode == 2
        assert "Traceback" not in result.stderr
        assert "[ERROR]" in result.stderr

    def test_main_never_raises(self, clean_pdf, secrets_file, monkeypatch) -> None:
        monkeypatch.setattr(verify, "print_report", _boom)
        assert verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        ) == 2

    def test_keyboard_interrupt_exits_2(self, clean_pdf, secrets_file, monkeypatch) -> None:
        def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(verify, "print_report", _interrupt)
        assert verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        ) == 2


class TestSubprocessHardening:
    """qpdf and exiftool are launched defensively (#4): resolved once to
    an absolute path, given the target as an absolute path, and run with
    a minimal environment — so a hostile filename or an inherited
    environment variable cannot change what either tool does.
    """

    def test_tools_launched_with_absolute_paths_and_minimal_env(
        self, tmp_path, monkeypatch
    ) -> None:
        target = tmp_path / "-evil.pdf"       # a name that looks like a flag
        target.write_bytes(b"%PDF-1.4\n%%EOF")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("SOME_OTHER_VAR", "must-not-reach-the-child")

        resolved = {"exiftool": "/opt/tools/exiftool", "qpdf": "/opt/tools/qpdf"}
        monkeypatch.setattr(verify.shutil, "which", lambda name: resolved.get(name))

        captured: dict[str, tuple[list[str], dict[str, str]]] = {}

        class _FakeProc:
            def poll(self):
                return None

        def _fake_popen(argv, stdout=None, stderr=None, env=None):
            captured[argv[0]] = (list(argv), dict(env or {}))
            return _FakeProc()

        monkeypatch.setattr(verify.subprocess, "Popen", _fake_popen)

        report = verify.ScanReport()
        verify.start_hidden_tools(target, report)

        assert report.warnings == []      # both "found": no TOOL_MISSING

        exif_argv, exif_env = captured["/opt/tools/exiftool"]
        # -config "" first: no config file from the environment is ever
        # loaded; "--" then ends option parsing so the filename can never
        # be read as a flag (exiftool's own documented convention).
        assert exif_argv[1:5] == ["-config", "", "-json", "--"]
        assert exif_argv[5] == str(target.resolve())
        assert exif_argv[5].startswith("/")

        qpdf_argv, qpdf_env = captured["/opt/tools/qpdf"]
        assert qpdf_argv[-2:] == [str(target.resolve()), "-"]
        assert qpdf_argv[-2].startswith("/")

        for env in (exif_env, qpdf_env):
            assert set(env) <= {"PATH", "LANG", "LC_ALL"}
            assert env.get("LANG") == "C" and env.get("LC_ALL") == "C"
            assert "SOME_OTHER_VAR" not in env

    def test_missing_tools_still_warn_tool_missing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(verify.shutil, "which", lambda name: None)
        report = verify.ScanReport()
        procs = verify.start_hidden_tools(tmp_path / "doc.pdf", report)
        assert procs == {"exiftool": None, "qpdf": None}
        missing = {(w.code, w.fields.get("tool"), w.layer) for w in report.warnings}
        assert missing == {
            ("TOOL_MISSING", "exiftool", "Metadata"),
            ("TOOL_MISSING", "qpdf", "Binary"),
        }

    @requires_qpdf
    def test_dash_prefixed_target_is_scanned_not_parsed_as_an_option(
        self, secrets_file, tmp_path
    ) -> None:
        pdf = tmp_path / "-evil.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "nothing sensitive here")
        doc.save(pdf)
        doc.close()
        result = run_verify(pdf, secrets_file)
        assert result.returncode in (0, 2)
        # Any of these would mean qpdf choked on (or hung on) a filename
        # it read as an option instead of the resolved absolute path.
        for marker in ("qpdf exited", "qpdf timed out", "qpdf stdout unavailable"):
            assert marker not in result.stdout

    @requires_exiftool
    def test_exiftool_config_in_home_is_not_honored(
        self, clean_pdf, secrets_file, tmp_path
    ) -> None:
        # exiftool searches $HOME/.ExifTool_config by default; a
        # booby-trapped one must never be loaded just because it happens
        # to sit in the invoking environment's home directory.
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        (fake_home / ".ExifTool_config").write_text("this is not valid Perl {{{\n")
        result = run_verify(
            clean_pdf, secrets_file, env_overrides={"HOME": str(fake_home)}
        )
        assert result.returncode in (0, 2)
        assert "TOOL_EXIT_NONZERO" not in result.stdout
        assert "exiftool" not in result.stdout or "not installed" not in result.stdout
