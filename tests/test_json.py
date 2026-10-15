"""The --json report: the machine-readable contract the evaluation harness
diffs against. It must carry the same verdict as the exit code, stable
codes and structured fields instead of message wording, never an
unmasked sample or a rules-file secret, and never outlive a run that did
not write it.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

import verify

from redaction_verifier.matching import SecretMatcher, mask, normalize_string
from redaction_verifier.model import (
    LAYERS,
    STORAGE_CLASSES,
    WARNING_CODES,
    WARNING_FIELDS,
    ScanReport,
    Secret,
    Warn,
)
from redaction_verifier.report import JSON_SCHEMA_VERSION, build_json_report
from redaction_verifier.rules import RuleSet
from redaction_verifier.views import OCR_DPI, _OCR_IMPORTS_OK

from .conftest import REPO_ROOT, SSN, report_banner, requires_full_env, run_verify

# verify.py's pure parts (docs/REDESIGN.md §4, §6 "Move, don't wrap") now
# live in redaction_verifier/ — the static checks below must still see
# every Warn()/warn() call and every yield, wherever it now lives.
SOURCE = "\n".join(
    path.read_text()
    for path in [
        REPO_ROOT / "verify.py",
        *sorted((REPO_ROOT / "redaction_verifier").rglob("*.py")),
    ]
)
VERDICTS = {0: "pass", 1: "fail", 2: "uncertified"}


def _run(target: Path, rules: Path, tmp_path: Path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / "report.json"
    code = verify.main(["--target", str(target), "--secrets", str(rules),
                        "--json", str(out), *extra])
    data = json.loads(out.read_text())
    # The contract every consumer relies on: one verdict, stated twice.
    assert data["exit_code"] == code and data["verdict"] == VERDICTS[code]
    return code, data


def _pdf(path: Path, *lines: str) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 72 + 18 * i), line)
    doc.save(path)
    doc.close()
    return path


def _rules(path: Path, rules: list[dict]) -> Path:
    path.write_text(json.dumps(rules))
    return path


def _exit_from(data: dict) -> int:
    return 1 if data["findings"] else 2 if data["warnings"] else 0


class TestVerdict:
    def test_clean(self, clean_pdf, secrets_file, tmp_path) -> None:
        code, data = _run(clean_pdf, secrets_file, tmp_path)
        assert code in (0, 2)
        assert data["findings"] == []
        assert data["error"] is None
        assert data["schema_version"] == JSON_SCHEMA_VERSION
        assert data["tool"]["version"] == verify.__version__
        assert data["target"] == "clean.pdf"

    def test_leak(self, leaky_pdf, secrets_file, tmp_path) -> None:
        code, data = _run(leaky_pdf, secrets_file, tmp_path)
        assert code == 1
        assert "Objects" in {f["layer"] for f in data["findings"]}
        assert all(f["tier"] == "hard" for f in data["findings"])
        assert "Target SSN" in {f["rule"] for f in data["findings"]}

    def test_exit_code_is_a_function_of_the_report(self, clean_pdf, leaky_pdf,
                                                   secrets_file, tmp_path) -> None:
        # Consumers (e.g. a Linux scorecard excluding OCR) may recompute the
        # verdict from the report; that only works if this holds exactly.
        for target in (clean_pdf, leaky_pdf):
            _, data = _run(target, secrets_file, tmp_path)
            assert data["error"] is None and data["exit_code"] == _exit_from(data)

    def test_matches_the_process_exit_code(self, leaky_pdf, secrets_file, tmp_path) -> None:
        out = tmp_path / "r.json"
        result = run_verify(leaky_pdf, secrets_file, "--json", str(out))
        assert result.returncode == json.loads(out.read_text())["exit_code"] == 1
        assert report_banner(result.stdout) == "FAIL"  # the human report still prints

    def test_deterministic(self, leaky_pdf, secrets_file, tmp_path) -> None:
        _, first = _run(leaky_pdf, secrets_file, tmp_path)
        _, second = _run(leaky_pdf, secrets_file, tmp_path)
        assert first == second
        for items in (first["findings"], first["warnings"]):
            keys = [json.dumps(d, sort_keys=True) for d in items]
            assert keys == sorted(keys)


class TestErrors:
    @pytest.mark.parametrize("name, content, code", [
        ("rules.json", "not json", "RULES_INVALID"),
    ])
    def test_invalid_rules(self, clean_pdf, tmp_path, name, content, code) -> None:
        rules = tmp_path / name
        rules.write_text(content)
        assert _run(clean_pdf, rules, tmp_path)[1]["error"]["code"] == code

    def test_missing_target_names_only_the_file(self, secrets_file, tmp_path) -> None:
        missing = tmp_path / "private-dir" / "client_name.pdf"
        code, data = _run(missing, secrets_file, tmp_path)
        assert code == 2 and data["error"]["code"] == "TARGET_NOT_FOUND"
        assert "client_name.pdf" in data["error"]["message"]
        assert "private-dir" not in json.dumps(data)

    def test_unreadable_pdf(self, secrets_file, tmp_path) -> None:
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"this is not a pdf")
        code, data = _run(bad, secrets_file, tmp_path)
        assert code == 2 and data["error"]["code"] == "PDF_UNREADABLE"

    def test_password_protected(self, secrets_file, tmp_path) -> None:
        path = tmp_path / "locked.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "hello")
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
        code, data = _run(path, secrets_file, tmp_path)
        assert code == 2 and data["error"]["code"] == "PDF_PASSWORD"


class TestReportFile:
    """The report file must never say something this run did not decide."""

    def test_unwritable_report_never_passes(self, clean_pdf, leaky_pdf,
                                            secrets_file, tmp_path, capsys) -> None:
        target = tmp_path / "missing-dir" / "r.json"
        args = ["--secrets", str(secrets_file), "--json", str(target)]
        assert verify.main(["--target", str(clean_pdf), *args]) == 2
        out = capsys.readouterr().out
        assert report_banner(out) == "NOT CERTIFIED", out  # never a [PASS] first
        if "ATTENTION" not in out:  # else the run is already 2 for its warnings
            assert "JSON report could not be written" in " ".join(out.split())
        assert verify.main(["--target", str(leaky_pdf), *args]) == 1

    def _seed_pass(self, path: Path) -> None:
        path.write_text(json.dumps({"exit_code": 0, "verdict": "pass"}))

    def test_stale_report_removed_before_scanning(self, secrets_file, tmp_path,
                                                  monkeypatch) -> None:
        out = tmp_path / "r.json"
        self._seed_pass(out)
        # A run that crashes before reaching its own normal finish() must
        # not leave the previous run's pass behind — and (see #1) the
        # crash itself must exit 2, never 1 (the leak code), and still
        # produce a --json report of its own, marked INTERNAL_ERROR, so a
        # consumer polling the file never reads the stale pass as this
        # run's verdict.
        monkeypatch.setattr(verify, "print_report",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        target = _pdf(tmp_path / "doc.pdf", "hello")
        code = verify.main(["--target", str(target), "--secrets", str(secrets_file),
                            "--json", str(out)])
        assert code == 2
        data = json.loads(out.read_text())
        assert data["exit_code"] == 2 and data["verdict"] == "uncertified"
        assert data["error"]["code"] == "INTERNAL_ERROR"
        assert "RuntimeError" in data["error"]["message"]

    def test_uncaught_exception_never_exits_the_leak_code(
        self, secrets_file, tmp_path, monkeypatch
    ) -> None:
        # Regression (#1): an exception escaping ANY of the late steps —
        # print_report here, but the same guard covers start_hidden_tools,
        # doc.page_count, and the JSON writer itself — used to propagate
        # out of main() uncaught. Under run_cli that exits 1, the "a
        # secret was found" code, on a document nothing was found in.
        monkeypatch.setattr(verify, "print_report",
                            lambda *a: (_ for _ in ()).throw(ValueError("boom")))
        target = _pdf(tmp_path / "doc.pdf", "hello")
        code = verify.main(["--target", str(target), "--secrets", str(secrets_file)])
        assert code == 2

    def test_crash_after_a_confirmed_finding_stays_a_fail(
        self, leaky_pdf, secrets_file, tmp_path, monkeypatch
    ) -> None:
        # This project's severity order is 0 < 2 < 1: a confirmed leak
        # outranks "cannot certify". print_report runs only after every
        # layer has already recorded its findings, so a crash there must
        # not downgrade an already-confirmed FAIL to a mere "uncertified"
        # — the secret was still found, crash or no crash.
        out = tmp_path / "r.json"
        monkeypatch.setattr(verify, "print_report",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        code = verify.main(["--target", str(leaky_pdf), "--secrets", str(secrets_file),
                            "--json", str(out)])
        assert code == 1
        data = json.loads(out.read_text())
        assert data["exit_code"] == 1 and data["verdict"] == "fail"
        # The crash is still visible to a consumer, alongside the finding.
        assert data["error"]["code"] == "INTERNAL_ERROR"
        assert data["findings"]

    def test_crash_before_any_finding_stays_uncertified(
        self, clean_pdf, secrets_file, tmp_path, monkeypatch
    ) -> None:
        out = tmp_path / "r.json"
        monkeypatch.setattr(verify, "print_report",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        code = verify.main(["--target", str(clean_pdf), "--secrets", str(secrets_file),
                            "--json", str(out)])
        assert code == 2
        data = json.loads(out.read_text())
        assert data["exit_code"] == 2 and data["verdict"] == "uncertified"
        assert data["error"]["code"] == "INTERNAL_ERROR"
        assert data["findings"] == []

    def test_read_only_old_report_is_replaced(self, leaky_pdf, secrets_file,
                                              tmp_path) -> None:
        out = tmp_path / "report.json"
        self._seed_pass(out)
        out.chmod(0o444)
        code, data = _run(leaky_pdf, secrets_file, tmp_path)
        assert code == 1 and data["verdict"] == "fail"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_irremovable_old_report_refuses_to_scan(self, leaky_pdf, secrets_file,
                                                    tmp_path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        out = locked / "r.json"
        self._seed_pass(out)
        locked.chmod(0o555)
        try:
            code = verify.main(["--target", str(leaky_pdf), "--secrets",
                                str(secrets_file), "--json", str(out)])
        finally:
            locked.chmod(0o755)
        assert code == 2       # never 0; the stale pass cannot be trusted

    def test_never_overwrites_an_input(self, clean_pdf, secrets_file, tmp_path) -> None:
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(clean_pdf.read_bytes())
        rules = tmp_path / "rules.json"
        rules.write_text(secrets_file.read_text())
        before = (pdf.read_bytes(), rules.read_text())
        for clash in (pdf, rules, tmp_path / "." / "doc.pdf"):
            code = verify.main(["--target", str(pdf), "--secrets", str(rules),
                                "--json", str(clash)])
            assert code == 2
        assert (pdf.read_bytes(), rules.read_text()) == before

    def test_private_and_not_through_a_symlink(self, clean_pdf, secrets_file,
                                               tmp_path) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        link = tmp_path / "report.json"
        link.symlink_to(victim)
        _run(clean_pdf, secrets_file, tmp_path)
        assert victim.read_text() == "keep me"
        assert not link.is_symlink()
        assert stat.S_IMODE(link.stat().st_mode) == 0o600


class TestVersion:
    def test_version_alone(self, capsys) -> None:
        assert verify.main(["--version"]) == 0
        assert verify.__version__ in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["--version", "--v", "--vers"])
    def test_version_never_skips_a_scan(self, leaky_pdf, secrets_file, flag) -> None:
        # As an argparse "version" action it exited 0 — the certified-clean
        # code — alongside --target, without scanning.
        with pytest.raises(SystemExit) as exc:
            verify.main(["--target", str(leaky_pdf), "--secrets", str(secrets_file), flag])
        assert exc.value.code == 2

    def test_package_version_is_the_module_version(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        assert 'version = {attr = "verify.__version__"}' in pyproject


@requires_full_env
class TestOcrFailureFailsClosed:
    def test_a_vision_error_on_a_page_exits_2(self, tmp_path, monkeypatch) -> None:
        # How macOS 27 broke the bridge: Vision raised on every page. A
        # page OCR cannot read must stay uncertified (PAGE_FAILED, exit 2),
        # never a clean pass, whatever the bridge raises.
        from redaction_verifier.views import ocr

        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        pdf = _pdf(tmp_path / "clean.pdf", "nothing here")
        assert _run(pdf, rules, tmp_path)[0] == 0  # certifiable while OCR works

        def _raise(png_bytes: bytes) -> tuple[list[ocr.Observation], list[ocr.Observation]]:
            raise ValueError("NSInvalidArgumentException - key does not exist")

        monkeypatch.setattr(ocr, "_vision_recognize_batch", _raise)
        code, data = _run(pdf, rules, tmp_path)
        assert code == 2
        assert [(w["code"], w["layer"]) for w in data["warnings"]] == [("PAGE_FAILED", "OCR")]

    def test_vision_reporting_success_without_running_exits_2(
        self, tmp_path, monkeypatch
    ) -> None:
        # Vision says the requests succeeded but never ran them: both
        # readings would be empty, so the page would pass as read with
        # nothing on it. The bridge must refuse (PAGE_FAILED, exit 2).
        from typing import Any

        import Vision

        from redaction_verifier.views import ocr

        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        pdf = _pdf(tmp_path / "clean.pdf", "nothing here")
        assert _run(pdf, rules, tmp_path)[0] == 0  # certifiable while OCR works

        class _NeverRuns:
            def performRequests_error_(self, requests: Any, error: Any) -> Any:
                return True, None

        class _Handler:
            @staticmethod
            def alloc() -> Any:
                return _Handler()

            def initWithData_options_(self, data: Any, options: Any) -> _NeverRuns:
                return _NeverRuns()

        class _Vision:
            VNImageRequestHandler = _Handler

            def __getattr__(self, name: str) -> Any:
                return getattr(Vision, name)

        monkeypatch.setattr(ocr, "Vision", _Vision())
        code, data = _run(pdf, rules, tmp_path)
        assert code == 2
        assert [(w["code"], w["layer"]) for w in data["warnings"]] == [("PAGE_FAILED", "OCR")]
        assert "never completed" in data["warnings"][0]["message"]

    def test_vision_never_returning_exits_2_at_the_deadline(
        self, tmp_path, monkeypatch
    ) -> None:
        # A stalled Vision held a test run for about 25 minutes (#115).
        # performRequests has no timeout of its own: the bridge's deadline
        # must turn it into PAGE_FAILED (exit 2), never a hang or a pass.
        import threading
        import time
        from typing import Any

        import Vision

        from redaction_verifier.views import ocr

        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        pdf = _pdf(tmp_path / "clean.pdf", "nothing here")
        assert _run(pdf, rules, tmp_path)[0] == 0  # certifiable while OCR works

        release = threading.Event()
        real_handler = Vision.VNImageRequestHandler

        class _Stalls:
            def __init__(self, real: Any) -> None:
                self._real = real

            def performRequests_error_(self, requests: Any, error: Any) -> Any:
                release.wait()
                return self._real.performRequests_error_(requests, error)

        class _Handler:
            @staticmethod
            def alloc() -> Any:
                return _Handler()

            def initWithData_options_(self, data: Any, options: Any) -> _Stalls:
                return _Stalls(real_handler.alloc().initWithData_options_(data, options))

        class _Vision:
            VNImageRequestHandler = _Handler

            def __getattr__(self, name: str) -> Any:
                return getattr(Vision, name)

        monkeypatch.setattr(ocr, "Vision", _Vision())
        monkeypatch.setattr(ocr, "VISION_CALL_TIMEOUT_S", 1.0)
        try:
            start = time.monotonic()
            code, data = _run(pdf, rules, tmp_path)
            assert time.monotonic() - start < 60
        finally:
            release.set()
            for thread in list(ocr._stalled):
                thread.join(60)
            # A thread still in Vision would fail every later OCR test here.
            assert not any(thread.is_alive() for thread in ocr._stalled)
        assert code == 2
        assert [(w["code"], w["layer"]) for w in data["warnings"]] == [("PAGE_FAILED", "OCR")]
        assert "did not return within 1 s" in data["warnings"][0]["message"]

    def test_a_handler_raising_on_a_vision_thread_exits_2(self, tmp_path) -> None:
        # Real Vision runs the two completion handlers on its own worker
        # threads; an exception escaping one there aborts the process
        # (no verdict, no report). The handler must catch it so the page
        # is PAGE_FAILED. Run in a child process: a regression kills the
        # child, not this test run.
        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        pdf = _pdf(tmp_path / "clean.pdf", "nothing here")
        out = tmp_path / "report.json"
        script = tmp_path / "raising_handler.py"
        script.write_text(
            "import sys\n"
            "import Vision\n"
            "import verify\n"
            "from redaction_verifier.views import ocr\n"
            "\n"
            "class _Broken:\n"
            "    def results(self):\n"
            "        raise ValueError('results() failed')\n"
            "\n"
            "class _Alloc:\n"
            "    def initWithCompletionHandler_(self, handler):\n"
            "        return Vision.VNRecognizeTextRequest.alloc()"
            ".initWithCompletionHandler_(lambda request, error: handler(_Broken(), error))\n"
            "\n"
            "class _Request:\n"
            "    @staticmethod\n"
            "    def alloc():\n"
            "        return _Alloc()\n"
            "\n"
            "class _Vision:\n"
            "    VNRecognizeTextRequest = _Request\n"
            "    def __getattr__(self, name):\n"
            "        return getattr(Vision, name)\n"
            "\n"
            "ocr.Vision = _Vision()\n"
            # run_cli, like every child-process test: it leaves via
            # os._exit, so Vision's native teardown cannot SIGKILL the
            # child after the report is written (seen on CI's macOS VMs).
            "verify.run_cli(sys.argv[1:])\n"
        )
        result = subprocess.run(
            [sys.executable, str(script), "--target", str(pdf), "--secrets", str(rules),
             "--json", str(out)],
            capture_output=True, text=True, timeout=300, cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        assert result.returncode == 2, result.stderr[-2000:]
        data = json.loads(out.read_text())
        assert data["exit_code"] == 2 and data["verdict"] == VERDICTS[2]
        assert [(w["code"], w["layer"]) for w in data["warnings"]] == [("PAGE_FAILED", "OCR")]
        assert "completion handler raised" in data["warnings"][0]["message"]


class TestEnvironmentField:
    def test_ocr_available_matches_the_running_process(self, tmp_path) -> None:
        # main() threads its own `_OCR_IMPORTS_OK` into build_json_report
        # as `ocr_available` (the same value that gates the OCR layer,
        # docs/28) rather than the report re-deriving it — pin that the
        # field is present, boolean, and matches this interpreter's own
        # OCR availability.
        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        pdf = _pdf(tmp_path / "clean.pdf", "nothing here")
        _, data = _run(pdf, rules, tmp_path)
        assert data["environment"]["ocr_available"] is _OCR_IMPORTS_OK
        assert isinstance(data["environment"]["ocr_available"], bool)

    def test_ocr_dpi_is_pinned(self) -> None:
        # OCR_DPI drives the OCR view's rendering resolution and is baked
        # into the scorecard's expectations; pin its value so a change is
        # a deliberate, reviewed edit rather than a silent drift the suite
        # would not otherwise catch.
        assert OCR_DPI == 300


class TestStructuredFields:
    def test_live_page_finding(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "live.pdf", f"SSN {SSN}")
        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        _, data = _run(pdf, rules, tmp_path)
        text = [f for f in data["findings"] if f["layer"] == "Text"]
        assert text and all(f["storage"] == "live" and f["page"] == 1 for f in text)

    def test_orphaned_object(self, tmp_path) -> None:
        path = tmp_path / "orphan.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "nothing here")
        xref = doc.get_new_xref()
        doc.update_object(xref, f"<< /Leftover ({SSN}) >>")
        doc.save(path)                     # no garbage collection: stays orphaned
        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        _, data = _run(path, rules, tmp_path)
        (hit,) = [f for f in data["findings"] if f["layer"] == "Objects"]
        assert (hit["storage"], hit["object"], hit["revision"]) == ("orphaned", xref, None)

    def test_superseded_object(self, tmp_path) -> None:
        path = tmp_path / "incremental.pdf"
        doc = fitz.open()
        doc.new_page()
        xref = doc.get_new_xref()
        doc.update_object(xref, f"<< /Note ({SSN}) >>")
        doc.xref_set_key(doc[0].xref, "Note", f"{xref} 0 R")
        doc.save(path)
        doc.close()
        doc = fitz.open(path)
        doc.update_object(xref, "<< /Note (redacted) >>")
        doc.saveIncr()
        doc.close()
        rules = _rules(tmp_path / "r.json", [{"name": "ssn value", "value": SSN}])
        _, data = _run(path, rules, tmp_path)
        (hit,) = [f for f in data["findings"] if f["layer"] == "Objects"]
        assert (hit["storage"], hit["object"], hit["revision"]) == ("superseded", xref, 1)

    def test_review_warning_names_rule_and_adjacency(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "wrapped.pdf", "Account ref 123-45-", "6789 continues")
        rules = tmp_path / "rules.yaml"
        rules.write_text("entity_types:\n  - person_name\n  - ssn\n")
        code, data = _run(pdf, rules, tmp_path)
        assert code == 2
        review = [w for w in data["warnings"] if w["code"] == "REVIEW_FUSED_PATTERN"]
        assert review and all(
            w["kind"] == "review" and w["rule"] == "entity_types:ssn"
            and w["adjacency"] == "JOINED_LINES" and w["storage"] == "live"
            for w in review)
        (scope,) = [w for w in data["warnings"] if w["code"] == "SCOPE_UNVERIFIABLE"]
        assert scope["kind"] == "scope" and scope["layer"] == "Rules"

    def test_missing_tools_are_named(self, clean_pdf, secrets_file, tmp_path,
                                     monkeypatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))       # neither qpdf nor exiftool
        _, data = _run(clean_pdf, secrets_file, tmp_path)
        missing = {(w["tool"], w["layer"]) for w in data["warnings"]
                   if w["code"] == "TOOL_MISSING"}
        assert missing == {("exiftool", "Metadata"), ("qpdf", "Binary")}

    def test_every_warning_has_the_full_shape(self, tmp_path) -> None:
        # Many codes at once: a binary attachment, a link carrying the
        # value, and a YAML config with an unquoted number and no
        # entity_types (so the default roster applies).
        path = tmp_path / "many.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(0, 0, 50, 50),
                          "uri": f"https://example.test/{SSN}"})
        doc.embfile_add("blob.bin", bytes(range(256)) * 8)
        doc.save(path)
        rules = tmp_path / "rules.yaml"
        rules.write_text("exact_values:\n  - 0777\n  - \"" + SSN + "\"\n")
        _, data = _run(path, rules, tmp_path)
        codes = {w["code"] for w in data["warnings"]}
        assert {"ATTACHMENT_NOT_TEXT", "RULES_UNQUOTED_VALUE", "SCOPE_UNVERIFIABLE",
                "SCOPE_PARTIAL_ENTITY"} <= codes
        shape = {"code", "kind", "layer", "message", *WARNING_FIELDS}
        for w in data["warnings"]:
            assert set(w) == shape
            assert w["kind"] == WARNING_CODES[w["code"]]
            assert w["layer"] in LAYERS
            assert w["storage"] in STORAGE_CLASSES | {None}


class TestToolReturnCodeField:
    """#3: TOOL_EXIT_NONZERO carries the tool's own exit status as a
    structured field, so a consumer can tell a real qpdf failure from its
    benign (version-dependent) exit 3 "succeeded with warnings" without
    parsing the message text.

    Both collectors are exercised directly against a stand-in subprocess
    (this interpreter, not the real tool) so the test needs neither qpdf
    nor exiftool installed.
    """

    def test_qpdf_nonzero_exit_carries_returncode(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 64); sys.exit(3)"],
            stdout=subprocess.PIPE,
        )
        report = ScanReport()
        verify._collect_qpdf(proc, SecretMatcher([]), report)
        (warn,) = [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]
        assert warn.fields["returncode"] == 3
        assert "returncode" in WARNING_FIELDS

    def test_exiftool_nonzero_exit_carries_returncode(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.write('[]'); sys.exit(2)"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        report = ScanReport()
        verify._collect_exiftool(proc, SecretMatcher([]), [], report)
        (warn,) = [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]
        assert warn.fields["returncode"] == 2

    def test_returncode_reaches_the_json_report(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 64); sys.exit(3)"],
            stdout=subprocess.PIPE,
        )
        report = ScanReport()
        verify._collect_qpdf(proc, SecretMatcher([]), report)
        data = build_json_report(
            report, 2, target=Path("t.pdf"), tool_version=verify.__version__,
            ocr_available=_OCR_IMPORTS_OK,
        )
        (warning,) = [w for w in data["warnings"] if w["code"] == "TOOL_EXIT_NONZERO"]
        assert warning["returncode"] == 3

    def test_returncode_is_null_when_not_applicable(self, tmp_path) -> None:
        # Additive field: every OTHER warning must still carry it, null.
        rules = tmp_path / "rules.yaml"
        rules.write_text("entity_types:\n  - person_name\n")
        pdf = _pdf(tmp_path / "clean.pdf", "nothing here")
        _, data = _run(pdf, rules, tmp_path)
        assert data["warnings"]
        assert all(w["returncode"] is None for w in data["warnings"]
                   if w["code"] != "TOOL_EXIT_NONZERO")


class TestWarningsAreAlwaysCoded:
    """A warning without a code would reach the report as UNCODED and the
    harness would lose track of it — so the report refuses one outright."""

    @pytest.mark.parametrize("add", [
        lambda ws: ws.append("plain"),
        lambda ws: ws.extend(["plain"]),
        lambda ws: ws.insert(0, "plain"),
        lambda ws: ws.__iadd__(["plain"]),
        lambda ws: ws.__setitem__(slice(0, 0), ["plain"]),
    ])
    def test_plain_strings_rejected(self, add) -> None:
        for holder in (ScanReport(), RuleSet()):
            with pytest.raises(TypeError):
                add(holder.warnings)

    def test_unregistered_code_layer_or_field_raises(self) -> None:
        for args, fields in [
            (("NOT_A_CODE", "Text", "m"), {}),
            (("PAGE_FAILED", "Nowhere", "m"), {}),
            (("PAGE_FAILED", "Text", "m"), {"colour": "red"}),
            (("PAGE_FAILED", "Text", "m"), {"storage": "elsewhere"}),
        ]:
            with pytest.raises(ValueError):
                Warn(*args, **fields)

    def test_warn_compares_as_its_message(self) -> None:
        w = Warn("PAGE_FAILED", "Text", "Text: page 1 failed (x)", page=1)
        assert w == "Text: page 1 failed (x)" and w.code == "PAGE_FAILED"

    def test_hidden_plain_string_is_coded_not_a_crash(self, monkeypatch) -> None:
        item = verify.HiddenItem(location="link #0 on page 1 (uri)", text=SSN)
        monkeypatch.setattr(verify, "_hidden_objects",
                            lambda doc: iter(["Hidden: something failed", item]))
        report = ScanReport()
        matcher = SecretMatcher([Secret("s", normalize_string(SSN))])
        verify.scan_hidden_objects(fitz.open(), matcher, [], report)
        assert [w.code for w in report.warnings] == ["HIDDEN_ITEM_FAILED"]
        assert report.findings                  # the rest of the layer still ran

    def test_no_bare_string_yields(self) -> None:
        offenders = [
            node.lineno for node in ast.walk(ast.parse(SOURCE))
            if isinstance(node, ast.Yield)
            and isinstance(node.value, (ast.JoinedStr, ast.Constant))
            and isinstance(getattr(node.value, "value", ""), str)
        ]
        assert offenders == []

    def test_every_code_used_is_registered_and_every_registered_code_is_used(self) -> None:
        used = set()
        for node in ast.walk(ast.parse(SOURCE)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            first = node.args[0]
            if name in ("Warn", "warn") and isinstance(first, ast.Constant):
                used.add(first.value)
        assert used <= set(WARNING_CODES)
        assert set(WARNING_CODES) <= used


class TestNothingSecretLeaks:
    DIGITS = re.compile(r"123\D{0,3}45|45\D{0,3}6789|3\D{0,3}4\D{0,3}5\D{0,3}6")

    def test_pattern_sample_is_masked(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "ssn.pdf", f"SSN {SSN}")
        rules = _rules(tmp_path / "r.json", [{"name": "any ssn", "class": "ssn"}])
        code, data = _run(pdf, rules, tmp_path)
        assert code == 1
        # OCR samples are always fully masked; other layers keep the tail.
        assert {f["sample"] for f in data["findings"] if f["layer"] != "OCR"} == {mask(SSN)}
        assert {f["sample"] for f in data["findings"] if f["layer"] == "OCR"} <= {"****"}
        assert not self.DIGITS.search(json.dumps(data))

    def test_review_samples_are_masked(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "wrapped.pdf", "Account ref 123-45-", "6789 continues")
        rules = _rules(tmp_path / "r.json", [{"name": "any ssn", "class": "ssn"}])
        _, data = _run(pdf, rules, tmp_path)
        assert any("****" in w["message"] for w in data["warnings"])
        assert not self.DIGITS.search(json.dumps(data))

    @pytest.mark.parametrize("config", [
        # A one-space mis-indent: PyYAML's error quotes the offending line.
        'exact_values:\n  - "Jane Roe"\n - "' + SSN + '"\n',
        # An unterminated quote.
        'exact_values:\n  - "' + SSN + '\n',
        # A value pasted under the wrong key.
        'entity_types:\n  - "' + SSN + '"\n',
    ])
    def test_rules_errors_never_echo_values(self, clean_pdf, tmp_path, capsys,
                                            config) -> None:
        rules = tmp_path / "rules.yaml"
        rules.write_text(config)
        code, data = _run(clean_pdf, rules, tmp_path)
        assert code == 2 and data["error"]["code"] == "RULES_INVALID"
        assert not self.DIGITS.search(json.dumps(data))
        assert not self.DIGITS.search(capsys.readouterr().err)

    def test_unquoted_yaml_value_warning_has_no_digit_of_the_value(
        self, tmp_path
    ) -> None:
        # Regression (#2): the warning used to show mask() of BOTH the
        # YAML-coerced value and the literal spec; together the two
        # masked tails narrowed a short secret from thousands of
        # candidates to a few dozen. It must now name only the TYPE YAML
        # coerced the value to, with no digit or character of the value.
        pdf = _pdf(tmp_path / "clean.pdf", "nothing sensitive here")
        rules = tmp_path / "rules.yaml"
        rules.write_text("exact_values:\n  - 0123456701\n")
        _, data = _run(pdf, rules, tmp_path)
        (warning,) = [w for w in data["warnings"] if w["code"] == "RULES_UNQUOTED_VALUE"]
        assert "a number" in warning["message"]
        assert "****" not in warning["message"]
        # "exact_values[0]" itself carries the rule's index, not the
        # secret's digits — everything else must be digit-free.
        assert not re.search(r"\d", warning["message"].replace("exact_values[0]", ""))

    def test_control_characters_sanitized(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "doc.pdf", f"SSN {SSN}")
        rules = _rules(tmp_path / "r.json",
                       [{"name": "evil\x1b[2Jname\nline", "value": SSN}])
        _, data = _run(pdf, rules, tmp_path)
        for f in data["findings"]:
            assert f["rule"].isprintable()


class TestMaskedTailNeverShowsAnExactValue:
    """Issue #66: a pattern sample's masked tail must not reveal part of an
    exact value (README: exact values are never shown). Fabricated data."""

    @staticmethod
    def _samples(tmp_path, text: str, rule_list: list[dict]) -> set[str]:
        pdf = _pdf(tmp_path / "doc.pdf", text)
        rules = _rules(tmp_path / "r.json", rule_list)
        code, data = _run(pdf, rules, tmp_path)
        assert code == 1
        return {f["sample"] for f in data["findings"]}

    def test_builtin_type_overlapping_an_exact_value(self, tmp_path) -> None:
        # Before the fix this showed "****6789" (the value's own tail).
        samples = self._samples(tmp_path, f"SSN {SSN}", [
            {"name": "the value", "value": SSN},
            {"name": "any ssn", "class": "ssn"},
        ])
        assert samples == {"", "****"}

    def test_user_pattern_overlapping_an_exact_value(self, tmp_path) -> None:
        # Before the fix: "****7890", whose "789" comes from the value.
        samples = self._samples(tmp_path, "ref 123-45-67890", [
            {"name": "the value", "value": SSN},
            {"name": "long ref", "pattern": r"\d{3}-\d{2}-\d{5}"},
        ])
        assert samples == {"", "****"}

    def test_sample_containing_the_whole_value(self, tmp_path) -> None:
        # The revealed tail ("0000") shares nothing with the value, but the
        # sample contains it, so even the tail is dropped.
        samples = self._samples(tmp_path, "ref 123-45-00000", [
            {"name": "the value", "value": "123-45"},
            {"name": "long ref", "pattern": r"\d{3}-\d{2}-\d{5}"},
        ])
        assert samples == {"", "****"}

    def test_non_overlapping_control_keeps_its_tail(self, tmp_path) -> None:
        samples = self._samples(tmp_path, "ref 123-45-67890 Jane Q. Example", [
            {"name": "other value", "value": "Jane Q. Example"},
            {"name": "long ref", "pattern": r"\d{3}-\d{2}-\d{5}"},
        ])
        assert samples - {"****"} == {"", "****7890"}   # an OCR copy shows only "****"


class TestMaskHelper:
    @pytest.mark.parametrize("sample, exact, shown", [
        ("123-45-6789", ["123456789"], "****"),    # tail comes from the value
        ("123-45-60000", ["12345"], "****"),       # sample contains the value
        ("abc-12345", ["x45y"], "****"),           # 2 consecutive chars overlap
        ("abc-12345", ["x4y5z"], "****"),          # any shared character hides the tail
        ("abc-12345", ["xyz"], "****2345"),        # nothing shared: kept
        ("123-45-67890", ["0ok"], "****"),         # tail ends with the value's first char
        ("abcd", ["xyzabc"], "****"),              # tail begins with the value's last char
        ("ab", ["b"], "****"),                     # 1-character tail
        ("ab", ["q"], "****b"),                    # 1-character control
        ("123-45-6789", [], "****6789"),           # no exact values
        ("123-45-6789", [""], "****6789"),         # an empty value matches nothing
        ("a", ["a"], "****"),                      # too short to reveal anything
    ])
    def test_cases(self, sample, exact, shown) -> None:
        assert mask(sample, exact) == shown


def test_review_warning_text_is_masked_against_exact_values(tmp_path) -> None:
    # Fused across lines: a REVIEW_FUSED_PATTERN warning embeds a sample.
    # "0ok" is an exact value whose first character ends the sample.
    pdf = _pdf(tmp_path / "doc.pdf", "ref 123-45-", "67890 ok")
    rules = _rules(tmp_path / "r.json", [
        {"name": "v", "value": "0ok"},
        {"name": "long ref", "pattern": r"\d{3}-\d{2}-\d{5}"},
    ])
    _, data = _run(pdf, rules, tmp_path)
    messages = [w["message"] for w in data["warnings"] if w["code"] == "REVIEW_FUSED_PATTERN"]
    assert messages and all("7890" not in m for m in messages)


class TestMaskNonAsciiAndCost:
    def test_non_latin_letters_in_the_tail_are_compared(self, tmp_path) -> None:
        # The ASCII-only normalizer drops O-slash; the tail "-O-slash"
        # belongs to the exact value and must not be shown.
        pdf = _pdf(tmp_path / "doc.pdf", "Ref ABC-Øvre 12")
        rules = _rules(tmp_path / "r.json", [
            {"name": "v", "value": "Øvre 12"},
            {"name": "p", "pattern": r"[A-Z]{3}-\w"},
        ])
        _, data = _run(pdf, rules, tmp_path)
        samples = {f["sample"] for f in data["findings"]}
        assert samples == {"", "****"}

    @pytest.mark.parametrize("sample, value, shown", [
        ("ABC-Ø", "Øvre 12", "****"),                       # Latin, non-ASCII
        ("kod-中文", "中文 12", "****"),              # CJK
        ("abc-жук", "жук 5", "****"),   # Cyrillic
        ("SSN \u0661\u0662\u0663-\u0664\u0665-\u0666\u0667\u0668\u0669", "123-45-6789", "****"),
        ("123-99-5\u06606\u0660", "9956", "****"),          # matcher drops the interleaved digits
        ("x1234\u4e2d5", "12345678", "****"),                # sample and tail in the matcher form
        ("555-55-6712", ("1\u00d82", "12"), "****"),   # plus the matcher form "12"
        ("123-45-6781", ("\u738b12", "12"), "****"),   # edge overlap, CJK lead
        ("ABC-Ø", "Zebra 12", "****-Ø"),                    # control: kept
    ])
    def test_helper(self, sample, value, shown) -> None:
        # A tuple stands for the two forms verify.py indexes per value:
        # the searched (normalized) form and the wide fold.
        values = [value] if isinstance(value, str) else list(value)
        assert mask(sample, values) == shown

    def test_cost_does_not_scale_with_the_number_of_values(self) -> None:
        import time

        from redaction_verifier.model import MaskIndex

        values = [f"v{i:06d}-{i * 7919 % 1000003:07d}" for i in range(200_000)]
        index = MaskIndex(values)
        start = time.perf_counter()
        for _ in range(2000):
            mask("ref 000000-0000000 and more text", index)
        assert time.perf_counter() - start < 2.0


def test_value_the_matcher_normalizes_differently_is_still_masked(tmp_path) -> None:
    # The matcher searches "12" for the value "1<O-slash>2"; the sample's
    # tail "6712" is exactly what it found, so it must not be shown.
    pdf = _pdf(tmp_path / "doc.pdf", "SSN 555-55-6712")
    rules = _rules(tmp_path / "r.json", [
        {"name": "v", "value": "1Ø2"},
        {"name": "any ssn", "class": "ssn"},
    ])
    _, data = _run(pdf, rules, tmp_path)
    assert {f["sample"] for f in data["findings"]} == {"", "****"}


class TestEveryReviewWarningMasksAgainstExactValues:
    """Each REVIEW_* warning text that embeds a sample masks it against the
    exact values (here the whole SSN, so the tail must be dropped)."""

    @staticmethod
    def _index_report() -> ScanReport:
        from redaction_verifier.model import MaskIndex

        report = ScanReport()
        report.exact_index = MaskIndex([SSN])
        return report

    @staticmethod
    def _patterns() -> list:
        from redaction_verifier.matching import BUILTIN_PATTERN_CLASSES, PatternRule, _valid_ssn

        return [PatternRule("Any SSN", re.compile(BUILTIN_PATTERN_CLASSES["ssn"][0]), _valid_ssn)]

    @staticmethod
    def _assert_masked(report: ScanReport, code: str) -> None:
        messages = [w for w in report.warnings if getattr(w, "code", "") == code]
        assert messages, code
        assert all("(sample ****)" in str(w) and "6789" not in str(w) for w in messages)

    def test_hidden_text(self) -> None:
        doc = fitz.open()
        doc.new_page()
        doc.embfile_add("notes.txt", f"attachment {SSN}".encode(), filename="notes.txt")
        report = self._index_report()
        verify.scan_hidden_objects(doc, SecretMatcher([]), self._patterns(), report)
        self._assert_masked(report, "REVIEW_HIDDEN_TEXT")

    def test_metadata_raw(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import sys; sys.stdout.write('not json {SSN}')"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        report = self._index_report()
        verify._collect_exiftool(proc, SecretMatcher([]), self._patterns(), report)
        self._assert_masked(report, "REVIEW_METADATA_RAW")

    def test_object_text_payload(self) -> None:
        report = self._index_report()
        verify._report_payload_hit(report, "object 5", "Any SSN", SSN, hard=False, origin={})
        self._assert_masked(report, "REVIEW_OBJECT_TEXT")

    @pytest.mark.parametrize("trailer, code", [
        (b"/ID [(123456789) <44556677>]", "REVIEW_OBJECT_TEXT"),
        (b"/ID [(123-45-) (6789)]", "REVIEW_ADJACENT_LITERALS"),
    ])
    def test_trailer_id_warnings(self, tmp_path, trailer, code) -> None:
        from .test_trailers import NO_VALUES, _with_trailer

        path = tmp_path / "t.pdf"
        path.write_bytes(_with_trailer(trailer))
        report = self._index_report()
        doc = fitz.open(path)
        try:
            verify.scan_trailers(path, doc, NO_VALUES, self._patterns(), report)
        finally:
            doc.close()
        self._assert_masked(report, code)

    def test_fused_pattern_on_the_ocr_layer_is_fully_masked(self) -> None:
        # Fused-line warnings come from the Text and OCR layers; for OCR the
        # sample is never shown even when it shares nothing with a value.
        assert mask("ABC-12345", [], full=True) == "****"


class TestOcrSamplesAreAlwaysFullyMasked:
    """OCR can misread a letter as a look-alike of an exact value's letter
    (a Greek name read as Latin-looking text), which no folding recognises;
    so an OCR sample never shows a tail."""

    def test_json_and_text_reports(self, capsys) -> None:
        from redaction_verifier.report import print_report

        report = ScanReport()
        report.record("OCR", "any", "page 1 (OCR)", "ref-2OõI", page=1)
        report.record("Text", "any", "page 1 (visual text layer)", "ref-2OõI", page=1)
        data = build_json_report(
            report, 1, target=Path("t.pdf"), tool_version="x", ocr_available=True)
        by_layer = {f["layer"]: f["sample"] for f in data["findings"]}
        assert by_layer == {"OCR": "****", "Text": "****2OõI"}
        print_report(report, Path("t.pdf"))
        lines = [ln for ln in capsys.readouterr().out.splitlines() if "STILL PRESENT" in ln]
        ocr_line = next(ln for ln in lines if "OCR" in ln.split("|")[2])
        text_line = next(ln for ln in lines if "Text" in ln.split("|")[2])
        assert "2OõI" not in ocr_line and "2OõI" in text_line
