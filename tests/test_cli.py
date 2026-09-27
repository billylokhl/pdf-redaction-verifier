"""End-to-end CLI regression tests: exit codes, layers, degradation.

Contract under test: exit 0 = certified clean, 1 = secret detected,
2 = operational error / uncertifiable scan. Several tests pin bugs where
the wrong exit code (or a silent clean verdict) was previously produced.
"""

from __future__ import annotations

import json
import os
import shutil
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

    @pytest.mark.parametrize(
        "exc_id, stub_code, expect_substr",
        [
            ("ImportError", 'raise ImportError("stub: simulated ImportError")\n',
             "ImportError"),
            ("SyntaxError", 'raise SyntaxError("stub: simulated SyntaxError")\n',
             "SyntaxError"),
            ("AttributeError", 'raise AttributeError("stub: simulated AttributeError")\n',
             "AttributeError"),
            # SystemExit and KeyboardInterrupt are BaseException, not
            # Exception: an `except Exception` guard does not catch them,
            # so a module that calls sys.exit() at import time (deliberate
            # or accidental — argparse, a misplaced CLI shim, a debug
            # `exit()` left in a partial install) would otherwise slip
            # straight through as the whole process's own exit code —
            # reproduced: SystemExit(0) made `verify.py --help` exit 0
            # with no output whatsoever, silently discarding every
            # argument including --target/--secrets.
            ("SystemExit(0)", "raise SystemExit(0)\n", "SystemExit"),
            ("SystemExit(1)", "raise SystemExit(1)\n", "SystemExit"),
        ],
    )
    def test_missing_redaction_verifier_package_exits_2(
        self, tmp_path, exc_id, stub_code, expect_substr
    ) -> None:
        # Regression: verify.py's re-export of redaction_verifier.* was a
        # bare top-level import. If the package can't be found (not
        # installed, or verify.py copied out of the repo on its own), that
        # raised ModuleNotFoundError straight through main() — a plain
        # traceback and exit 1, the "secret found" code, silently
        # breaking the "operational failure exits 2, never 1" contract
        # the PyMuPDF import already guards a few lines above it. The
        # guard must also catch more than plain ImportError: a corrupt or
        # partial install, or a bytecode/ABI mismatch, can fail with a
        # SyntaxError or AttributeError instead — and, per the
        # SystemExit/KeyboardInterrupt case above, the catch must be
        # BaseException, not Exception.
        #
        # redaction_verifier is installed editable (a .pth finder in
        # site-packages pointing back at the repo), so neither an empty
        # cwd nor a stripped PYTHONPATH hides it from this interpreter.
        # What DOES take precedence is the directory a script is run
        # from: Python inserts it at sys.path[0], ahead of site-packages.
        # So copying verify.py alone into tmp_path and placing a stub
        # top-level redaction_verifier.py next to it — a plain module,
        # not a package, that raises the given exception on import —
        # reliably simulates each failure mode without needing -I or any
        # interpreter/environment trickery.
        shutil.copy(REPO_ROOT / "verify.py", tmp_path / "verify.py")
        (tmp_path / "redaction_verifier.py").write_text(stub_code)
        result = subprocess.run(
            [sys.executable, str(tmp_path / "verify.py"),
             "--target", "x.pdf", "--secrets", "x.json"],
            capture_output=True, text=True, timeout=60, cwd=tmp_path,
        )
        assert result.returncode == 2, (exc_id, result.stdout, result.stderr)
        assert "Traceback" not in result.stderr
        assert "[ERROR] cannot import redaction_verifier" in result.stderr
        # The real failure must stay visible — not masked by a generic
        # "not found" message when the package IS present but something
        # inside it failed.
        assert expect_substr in result.stderr, result.stderr

    def test_ocr_bridge_system_exit_at_import_exits_2(self, tmp_path) -> None:
        # Same BaseException gap as above, but for the Vision/OCR import
        # guard specifically: unlike an ordinary missing or broken OCR
        # bridge (which must degrade to _OCR_IMPORTS_OK = False, not
        # crash — see TestBaselines below), an import that itself calls
        # sys.exit() or is interrupted must stop the tool loudly here.
        # Order matters in the guard: (SystemExit, KeyboardInterrupt) is
        # checked before the broad Exception catch that implements the
        # degrade, so this must exit 2, not silently continue.
        #
        # A stub top-level Vision.py placed on PYTHONPATH shadows any
        # real pyobjc Vision the same way a script's own directory
        # shadows site-packages (see the test above) — PYTHONPATH entries
        # are inserted ahead of the standard library and site-packages.
        # Uses the real repo verify.py directly (no need to copy it: only
        # the Vision import is being intercepted, not redaction_verifier).
        stub_dir = tmp_path / "stub_vision"
        stub_dir.mkdir()
        (stub_dir / "Vision.py").write_text("raise SystemExit(0)\n")
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(stub_dir), env.get("PYTHONPATH", "")]
        )
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "verify.py"), "--help"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert result.returncode == 2, (result.stdout, result.stderr)
        assert "Traceback" not in result.stderr
        assert "[ERROR] cannot import the OCR bridge (Vision)" in result.stderr
        assert "SystemExit" in result.stderr


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

    def test_system_exit_inside_pipeline_does_not_escape(
        self, clean_pdf, secrets_file, monkeypatch
    ) -> None:
        # Regression: only Exception and KeyboardInterrupt were caught, so
        # a SystemExit raised inside the pipeline (a buggy dependency
        # calling sys.exit, say) carried its own arbitrary code all the
        # way out of main() — a clean document could exit however that
        # SystemExit happened to be constructed, including 0 or 1.
        def _boom(*args, **kwargs):
            raise SystemExit(1)

        monkeypatch.setattr(verify, "print_report", _boom)
        assert verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        ) == 2

    def test_argparse_error_through_run_cli_still_exits_2(self) -> None:
        # The BaseException backstop added to run_cli (for the SystemExit
        # case above) must not swallow argparse's OWN error path: a
        # missing required flag still exits 2 with argparse's own
        # message, not a spurious "internal error" line.
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "verify.py"), "--secrets", "x.json"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2
        assert "[ERROR] internal error" not in result.stderr
        assert "required" in result.stderr


class TestOrphanedChildProcesses:
    """Regression (#2): a crash escaping main()'s scan pipeline — or a
    Ctrl-C while check_hidden_layers is blocked waiting on qpdf/exiftool
    — must never leave those subprocesses running once main() has decided
    to leave. run_cli then calls os._exit, which would otherwise abandon
    them as orphans with no parent left to reap them.
    """

    def test_keyboard_interrupt_kills_hidden_tool_children(
        self, clean_pdf, secrets_file, tmp_path, monkeypatch
    ) -> None:
        # A stand-in for both qpdf and exiftool that just hangs, so the
        # test can prove they get killed rather than merely finishing on
        # their own before the assertion runs.
        stub = tmp_path / "stall.sh"
        stub.write_text("#!/bin/sh\nsleep 30\n")
        stub.chmod(0o755)
        monkeypatch.setattr(verify.shutil, "which", lambda name: str(stub))

        spawned: list[subprocess.Popen] = []
        real_popen = verify.subprocess.Popen

        def _spy_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(verify.subprocess, "Popen", _spy_popen)

        # Simulate Ctrl-C while Phase 4 is blocked waiting on the (here,
        # stalled) tools — exactly the case check_hidden_layers' own
        # per-collector `except Exception` cannot catch.
        def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(verify, "check_hidden_layers", _interrupt)

        code = verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        )
        assert code == 2
        assert len(spawned) == 2       # both stand-ins actually launched
        for proc in spawned:
            assert proc.poll() is not None, "child process still alive after main() returned"


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

        captured: dict[str, tuple[list[str], dict[str, str], str]] = {}

        class _FakeProc:
            def poll(self):
                return None

        def _fake_popen(argv, stdout=None, stderr=None, env=None, cwd=None):
            captured[argv[0]] = (list(argv), dict(env or {}), cwd)
            return _FakeProc()

        monkeypatch.setattr(verify.subprocess, "Popen", _fake_popen)

        report = verify.ScanReport()
        procs, scratch = verify.start_hidden_tools(target, report)
        try:
            assert report.warnings == []      # both "found": no TOOL_MISSING

            exif_argv, exif_env, exif_cwd = captured["/opt/tools/exiftool"]
            # -config "" first: no config file from EXIFTOOL_HOME/HOME is
            # ever loaded; "--" then ends option parsing so the filename
            # can never be read as a flag (exiftool's own documented
            # convention).
            assert exif_argv[1:5] == ["-config", "", "-json", "--"]
            assert exif_argv[5] == str(target.resolve())
            assert exif_argv[5].startswith("/")

            qpdf_argv, qpdf_env, qpdf_cwd = captured["/opt/tools/qpdf"]
            assert qpdf_argv[-2:] == [str(target.resolve()), "-"]
            assert qpdf_argv[-2].startswith("/")

            for env in (exif_env, qpdf_env):
                assert set(env) <= {"PATH", "LANG", "LC_ALL"}
                assert env.get("LANG") == "C" and env.get("LC_ALL") == "C"
                assert "SOME_OTHER_VAR" not in env

            # Both launched from the same private, empty scratch directory
            # — never this process's own cwd (see #4: exiftool also
            # searches its WORKING DIRECTORY for .ExifTool_config).
            assert exif_cwd == qpdf_cwd == scratch.name
            assert exif_cwd != os.getcwd()
            assert os.listdir(exif_cwd) == []
        finally:
            scratch.cleanup()

    def test_missing_tools_still_warn_tool_missing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(verify.shutil, "which", lambda name: None)
        report = verify.ScanReport()
        procs, scratch = verify.start_hidden_tools(tmp_path / "doc.pdf", report)
        try:
            assert procs == {"exiftool": None, "qpdf": None}
            missing = {(w.code, w.fields.get("tool"), w.layer) for w in report.warnings}
            assert missing == {
                ("TOOL_MISSING", "exiftool", "Metadata"),
                ("TOOL_MISSING", "qpdf", "Binary"),
            }
        finally:
            scratch.cleanup()

    def test_exiftool_argv_starts_with_config_override(self, monkeypatch) -> None:
        # Dedicated, narrow check that the broader hardening test above
        # doesn't spell out on its own: exiftool's argv must start with
        # '-config ""' — before -json, before --, before anything else —
        # so no config file is ever consulted for EXIFTOOL_HOME/HOME/
        # HOMEDRIVE+HOMEPATH (the cwd fallback is closed separately, by
        # running from a private scratch directory — see #4 and the tests
        # below).
        monkeypatch.setattr(verify.shutil, "which", lambda name: f"/opt/tools/{name}")
        captured: dict[str, list[str]] = {}

        class _FakeProc:
            def poll(self):
                return None

        def _fake_popen(argv, stdout=None, stderr=None, env=None, cwd=None):
            captured[argv[0]] = list(argv)
            return _FakeProc()

        monkeypatch.setattr(verify.subprocess, "Popen", _fake_popen)

        report = verify.ScanReport()
        procs, scratch = verify.start_hidden_tools(Path("/nonexistent/target.pdf"), report)
        try:
            assert captured["/opt/tools/exiftool"][1:3] == ["-config", ""]
        finally:
            scratch.cleanup()

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
    def test_metadata_secret_found_through_hardened_exiftool(
        self, secrets_file, tmp_path
    ) -> None:
        # #5: the hardening (minimal env, -config "", --, absolute path,
        # private cwd) must not accidentally break exiftool's actual job.
        path = tmp_path / "meta.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "clean body")
        doc.set_metadata({"subject": f"applicant {SSN}"})
        doc.save(path)
        doc.close()
        result = run_verify(path, secrets_file)
        assert result.returncode == 1, result.stdout
        assert "LAYER: Metadata" in result.stdout


# A config file with an OBSERVABLE effect when exiftool actually loads it:
# it registers a user-defined Composite tag (exiftool's own documented
# extension mechanism — see its example.config) that ALWAYS evaluates to
# a fixed string. If the config is read, that tag — and its value —
# appears as a new key in every '-json' output; if it is not, the key is
# simply absent. This was verified empirically (a config that instead
# called die() was NOT what it first appeared to be: exiftool wraps
# config loading in its own eval and merely warns on a die or a syntax
# error, still exiting 0 with unchanged JSON either way — no more
# observable than the original "not valid Perl" fixture this replaces).
# A defined-tag config's effect is unambiguous because it does not depend
# on exiftool's own error handling at all.
_EXIFTOOL_TAG_CONFIG = """\
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::Composite' => {
        PWNEDConfigLoaded => {
            Require => 'FileType',
            ValueConv => '"PWNED-CONFIG-LOADED"',
        },
    },
);
1;  #end
"""


def _run_real_exiftool(
    pdf: Path, cwd: Path, *, config_override: bool
) -> subprocess.CompletedProcess[str]:
    """Invoke the real exiftool binary the same way verify.py's own argv
    does — or, with config_override=False, without the '-config ""' guard
    — from a given working directory, with the same minimal environment
    verify.py uses (no HOME, so a config search can only ever find
    something via EXIFTOOL_HOME or the cwd fallback, never the real
    tester's home directory).
    """
    exe = shutil.which("exiftool")
    argv = [exe]
    if config_override:
        argv += ["-config", ""]
    argv += ["-json", "--", str(pdf.resolve())]
    return subprocess.run(
        argv, capture_output=True, text=True, cwd=str(cwd), timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
    )


@requires_exiftool
class TestExiftoolConfigOverrideIsTheRealProtection:
    """Confirmation-review follow-up on #4: the original cwd-hardening
    test planted a config that was merely invalid Perl, which exiftool
    only warns about (to stderr, which verify.py discards) while still
    exiting 0 with identical JSON whether or not it was read — so that
    test could never fail even if the fix were reverted. This plants a
    config whose effect (a new JSON key) is unambiguous, and proves each
    protection independently: that the fixture itself would be caught if
    nothing protected against it, and that '-config ""' — not the private
    cwd, which is defence in depth — is what actually stops it. Every
    path used here lives under tmp_path; nothing touches the real HOME or
    this process's own cwd.
    """

    def _hostile_cwd(self, tmp_path: Path) -> Path:
        cwd = tmp_path / "evil-cwd"
        cwd.mkdir()
        (cwd / ".ExifTool_config").write_text(_EXIFTOOL_TAG_CONFIG)
        return cwd

    def _tiny_pdf(self, tmp_path: Path) -> Path:
        path = tmp_path / "doc.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "nothing sensitive here")
        doc.save(path)
        doc.close()
        return path

    def test_planted_config_is_detected_when_actually_loaded(self, tmp_path) -> None:
        # Proves the fixture is meaningful: WITHOUT '-config ""', exiftool
        # run from a cwd holding this file really does pick it up — the
        # user-defined tag's value shows up as a new JSON key.
        cwd = self._hostile_cwd(tmp_path)
        pdf = self._tiny_pdf(tmp_path)
        result = _run_real_exiftool(pdf, cwd, config_override=False)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload[0].get("PWNEDConfigLoaded") == "PWNED-CONFIG-LOADED"

    def test_config_override_stops_it_even_from_the_hostile_cwd(self, tmp_path) -> None:
        # This is the actual protection (#4): '-config ""' alone means
        # exiftool never attempts to load the file, regardless of cwd —
        # the tag never gets registered, so the key is simply absent.
        cwd = self._hostile_cwd(tmp_path)
        pdf = self._tiny_pdf(tmp_path)
        result = _run_real_exiftool(pdf, cwd, config_override=True)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload
        assert "PWNEDConfigLoaded" not in payload[0]

    def test_end_to_end_through_verify_is_unaffected(
        self, secrets_file, tmp_path
    ) -> None:
        # The real CLI, run from that same hostile cwd, must scan
        # normally and never surface the planted tag — proving the actual
        # hardened invocation (not just the isolated exiftool call above)
        # is protected.
        cwd = self._hostile_cwd(tmp_path)
        pdf = self._tiny_pdf(tmp_path)
        result = run_verify(pdf, secrets_file, cwd=cwd)
        assert result.returncode in (0, 2)
        assert "TOOL_EXIT_NONZERO" not in result.stdout
        assert "TOOL_NO_OUTPUT" not in result.stdout
        assert "TOOL_OUTPUT_MALFORMED" not in result.stdout
        assert "PWNEDConfigLoaded" not in result.stdout
        assert "PWNED-CONFIG-LOADED" not in result.stdout


class TestCleanupIsBestEffort:
    """Cheap fix from the confirmation review: kill_hidden_tools() and the
    scratch directory's cleanup() run in main()'s outer `finally` — AFTER
    print_report has already printed the verdict. If either raised, it
    used to propagate into the `except BaseException` guard (#1/#2),
    replacing an already-decided, already-printed 0/1 with a fresh
    INTERNAL_ERROR at exit 2. Cleanup problems are still worth reporting,
    so they print a stderr note — they just must never change what was
    already decided.
    """

    def test_kill_hidden_tools_failure_never_overrides_the_verdict(
        self, clean_pdf, secrets_file, monkeypatch, capsys
    ) -> None:
        baseline = verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        )
        capsys.readouterr()  # discard the baseline run's output

        def _boom_kill(procs):
            raise OSError("simulated cleanup failure")

        monkeypatch.setattr(verify, "kill_hidden_tools", _boom_kill)
        code = verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        )
        out, err = capsys.readouterr()
        assert code == baseline
        assert "[PASS]" in out or "[FAIL]" in out
        assert "simulated cleanup failure" in err

    def test_scratch_cleanup_failure_never_overrides_the_verdict(
        self, clean_pdf, secrets_file, monkeypatch, capsys
    ) -> None:
        baseline = verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        )
        capsys.readouterr()

        def _boom_cleanup(self):
            raise OSError("simulated scratch cleanup failure")

        monkeypatch.setattr(verify.tempfile.TemporaryDirectory, "cleanup", _boom_cleanup)
        code = verify.main(
            ["--target", str(clean_pdf), "--secrets", str(secrets_file)]
        )
        out, err = capsys.readouterr()
        assert code == baseline
        assert "[PASS]" in out or "[FAIL]" in out
        assert "simulated scratch cleanup failure" in err
