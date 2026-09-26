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
from pathlib import Path

import fitz
import pytest

import verify

from .conftest import REPO_ROOT, SSN, run_verify

SOURCE = (REPO_ROOT / "verify.py").read_text()
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
        assert data["schema_version"] == verify.JSON_SCHEMA_VERSION
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
        assert "[FAIL]" in result.stdout          # the human report is unchanged

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
        assert "NOT a certified clean result" in capsys.readouterr().out
        assert verify.main(["--target", str(leaky_pdf), *args]) == 1

    def _seed_pass(self, path: Path) -> None:
        path.write_text(json.dumps({"exit_code": 0, "verdict": "pass"}))

    def test_stale_report_removed_before_scanning(self, secrets_file, tmp_path,
                                                  monkeypatch) -> None:
        out = tmp_path / "r.json"
        self._seed_pass(out)
        # A run that dies before writing its own report must not leave the
        # previous run's pass behind.
        monkeypatch.setattr(verify, "print_report",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        target = _pdf(tmp_path / "doc.pdf", "hello")
        with pytest.raises(RuntimeError):
            verify.main(["--target", str(target), "--secrets", str(secrets_file),
                         "--json", str(out)])
        assert not out.exists()

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
        shape = {"code", "kind", "layer", "message", *verify.WARNING_FIELDS}
        for w in data["warnings"]:
            assert set(w) == shape
            assert w["kind"] == verify.WARNING_CODES[w["code"]]
            assert w["layer"] in verify.LAYERS
            assert w["storage"] in verify.STORAGE_CLASSES | {None}


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
        for holder in (verify.ScanReport(), verify.RuleSet()):
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
                verify.Warn(*args, **fields)

    def test_warn_compares_as_its_message(self) -> None:
        w = verify.Warn("PAGE_FAILED", "Text", "Text: page 1 failed (x)", page=1)
        assert w == "Text: page 1 failed (x)" and w.code == "PAGE_FAILED"

    def test_hidden_plain_string_is_coded_not_a_crash(self, monkeypatch) -> None:
        item = verify.HiddenItem(location="link #0 on page 1 (uri)", text=SSN)
        monkeypatch.setattr(verify, "_hidden_objects",
                            lambda doc: iter(["Hidden: something failed", item]))
        report = verify.ScanReport()
        matcher = verify.SecretMatcher([verify.Secret("s", verify.normalize_string(SSN))])
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
        assert used <= set(verify.WARNING_CODES)
        assert set(verify.WARNING_CODES) <= used


class TestNothingSecretLeaks:
    DIGITS = re.compile(r"123\D{0,3}45|45\D{0,3}6789|3\D{0,3}4\D{0,3}5\D{0,3}6")

    def test_pattern_sample_is_masked(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "ssn.pdf", f"SSN {SSN}")
        rules = _rules(tmp_path / "r.json", [{"name": "any ssn", "class": "ssn"}])
        code, data = _run(pdf, rules, tmp_path)
        assert code == 1
        assert {f["sample"] for f in data["findings"]} == {verify.mask(SSN)}
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

    def test_control_characters_sanitized(self, tmp_path) -> None:
        pdf = _pdf(tmp_path / "doc.pdf", f"SSN {SSN}")
        rules = _rules(tmp_path / "r.json",
                       [{"name": "evil\x1b[2Jname\nline", "value": SSN}])
        _, data = _run(pdf, rules, tmp_path)
        for f in data["findings"]:
            assert f["rule"].isprintable()
