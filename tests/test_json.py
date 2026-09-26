"""The --json report: the machine-readable contract the evaluation harness
diffs against. It must carry the same verdict as the exit code, stable
warning codes instead of message wording, and never an unmasked sample.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import fitz
import pytest

import verify

from .conftest import REPO_ROOT, SSN, run_verify

SOURCE = (REPO_ROOT / "verify.py").read_text()


def _run(target: Path, rules: Path, tmp_path: Path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / "report.json"
    code = verify.main(["--target", str(target), "--secrets", str(rules),
                        "--json", str(out), *extra])
    return code, json.loads(out.read_text())


class TestVerdict:
    def test_clean(self, clean_pdf, secrets_file, tmp_path) -> None:
        code, data = _run(clean_pdf, secrets_file, tmp_path)
        assert data["exit_code"] == code
        assert data["verdict"] == {0: "pass", 2: "uncertified"}[code]
        assert data["findings"] == []
        assert data["error"] is None
        assert data["schema_version"] == verify.JSON_SCHEMA_VERSION
        assert data["tool"]["version"] == verify.__version__
        assert data["target"] == "clean.pdf"

    def test_leak(self, leaky_pdf, secrets_file, tmp_path) -> None:
        code, data = _run(leaky_pdf, secrets_file, tmp_path)
        assert code == data["exit_code"] == 1
        assert data["verdict"] == "fail"
        layers = {f["layer"] for f in data["findings"]}
        assert "Objects" in layers          # Metadata needs exiftool (not on every runner)
        assert all(f["tier"] == "hard" for f in data["findings"])
        assert {f["rule"] for f in data["findings"]} >= {"Target SSN"}

    def test_matches_the_process_exit_code(self, leaky_pdf, secrets_file, tmp_path) -> None:
        out = tmp_path / "r.json"
        result = run_verify(leaky_pdf, secrets_file, "--json", str(out))
        assert result.returncode == json.loads(out.read_text())["exit_code"] == 1
        assert "[FAIL]" in result.stdout          # the human report is unchanged


class TestErrors:
    def test_missing_target(self, secrets_file, tmp_path) -> None:
        code, data = _run(tmp_path / "nope.pdf", secrets_file, tmp_path)
        assert code == data["exit_code"] == 2
        assert data["error"]["code"] == "TARGET_NOT_FOUND"

    def test_invalid_rules(self, clean_pdf, tmp_path) -> None:
        rules = tmp_path / "rules.json"
        rules.write_text("not json")
        code, data = _run(clean_pdf, rules, tmp_path)
        assert code == data["exit_code"] == 2
        assert data["error"]["code"] == "RULES_INVALID"

    def test_unreadable_pdf(self, secrets_file, tmp_path) -> None:
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"this is not a pdf")
        code, data = _run(bad, secrets_file, tmp_path)
        assert code == data["exit_code"] == 2
        assert data["error"]["code"] == "PDF_UNREADABLE"

    def test_password_protected(self, secrets_file, tmp_path) -> None:
        path = tmp_path / "locked.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "hello")
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
        code, data = _run(path, secrets_file, tmp_path)
        assert code == data["exit_code"] == 2
        assert data["error"]["code"] == "PDF_PASSWORD"

    def test_unwritable_report_never_passes(self, clean_pdf, leaky_pdf,
                                            secrets_file, tmp_path) -> None:
        # A caller that asked for the JSON report gates on it: a missing
        # report must not read as a certified pass, and must not hide a leak.
        target = tmp_path / "missing-dir" / "r.json"
        args = ["--secrets", str(secrets_file), "--json", str(target)]
        assert verify.main(["--target", str(clean_pdf), *args]) == 2
        assert verify.main(["--target", str(leaky_pdf), *args]) == 1


class TestWarnings:
    def test_codes_and_kinds(self, tmp_path) -> None:
        # One review warning (a pattern match only across a line break)
        # and one scope warning (unverifiable entity type).
        path = tmp_path / "wrapped.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Account ref 123-45-")
        page.insert_text((72, 90), "6789 continues")
        doc.save(path)
        rules = tmp_path / "rules.yaml"
        rules.write_text("entity_types:\n  - person_name\n  - ssn\n")
        code, data = _run(path, rules, tmp_path)
        assert code == 2
        by_code = {w["code"]: w for w in data["warnings"]}
        assert by_code["SCOPE_UNVERIFIABLE"]["kind"] == "scope"
        assert by_code["REVIEW_FUSED_PATTERN"]["kind"] == "review"
        assert all(w["code"] in verify.WARNING_CODES for w in data["warnings"])

    def test_no_uncoded_warnings_from_a_degraded_scan(self, clean_pdf, secrets_file,
                                                      tmp_path) -> None:
        # Tools missing from PATH exercise the coverage-warning sites.
        _, data = _run(clean_pdf, secrets_file, tmp_path)
        assert all(w["code"] != "UNCODED" for w in data["warnings"])
        assert all(w["kind"] == verify.WARNING_CODES[w["code"]] for w in data["warnings"])

    def test_unregistered_code_raises(self) -> None:
        with pytest.raises(ValueError):
            verify.Warn("NOT_A_CODE", "message")

    def test_warn_compares_as_its_message(self) -> None:
        w = verify.Warn("PAGE_FAILED", "Text: page 1 failed (x)")
        assert w == "Text: page 1 failed (x)" and w.code == "PAGE_FAILED"


class TestMasking:
    def test_pattern_sample_is_masked(self, tmp_path) -> None:
        path = tmp_path / "ssn.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"SSN {SSN}")
        doc.save(path)
        rules = tmp_path / "rules.json"
        rules.write_text(json.dumps([{"name": "any ssn", "class": "ssn"}]))
        code, data = _run(path, rules, tmp_path)
        assert code == 1
        raw = json.dumps(data)
        assert SSN not in raw and SSN.replace("-", "") not in raw
        assert any(f["sample"] for f in data["findings"])


class TestDeterminism:
    def test_sorted(self, leaky_pdf, secrets_file, tmp_path) -> None:
        _, data = _run(leaky_pdf, secrets_file, tmp_path)
        keys = [(f["layer"], f["rule"], f["location"], f["sample"]) for f in data["findings"]]
        assert keys == sorted(keys)
        wkeys = [(w["code"], w["message"]) for w in data["warnings"]]
        assert wkeys == sorted(wkeys)


class TestVersion:
    def test_version_flag(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            verify.main(["--version"])
        assert exc.value.code == 0
        assert verify.__version__ in capsys.readouterr().out

    def test_package_version_is_the_module_version(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        assert 'version = {attr = "verify.__version__"}' in pyproject


class TestEveryWarningIsCoded:
    """Static guard: a warning added without a code would reach the JSON
    report as UNCODED, and the harness would lose track of it."""

    tree = ast.parse(SOURCE)

    def _calls(self, attr: str):
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == attr):
                yield node

    def test_no_direct_appends(self) -> None:
        # Only the two warn() helpers may append to a warnings list.
        offenders = [
            n.lineno for n in self._calls("append")
            if isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "warnings"
            and not (isinstance(n.args[0], ast.Call)
                     and getattr(n.args[0].func, "id", None) == "Warn")
        ]
        assert offenders == []

    def test_no_bare_string_yields(self) -> None:
        offenders = [
            node.lineno for node in ast.walk(self.tree)
            if isinstance(node, ast.Yield)
            and isinstance(node.value, (ast.JoinedStr, ast.Constant))
            and isinstance(getattr(node.value, "value", ""), str)
        ]
        assert offenders == []

    def test_every_code_used_is_registered_and_every_registered_code_is_used(self) -> None:
        used = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            first = node.args[0]
            if name in ("Warn", "warn") and isinstance(first, ast.Constant):
                used.add(first.value)
        assert used <= set(verify.WARNING_CODES)
        assert set(verify.WARNING_CODES) <= used
