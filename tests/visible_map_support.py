"""Shared helpers for the visible map's engine tests: run the scan
in-process with a MapSink handed to its two hooks (scan_page_layer and
scan_hidden_objects), build the header the map is planned from, and plan
it in-process, so tests can look at boxes and lists directly.

The CLI flag that does the same in a child process comes later; until
then, this harness is the only caller of the hooks. Labels go through
an identity checker here: the map's own checker (the self-check) is
tested with the rendering.

Every PDF here is fabricated (SSN 123-45-6789, "Jane Q. Example") and
written under the test's own tmp_path.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf as fitz

import verify
from redaction_verifier.visible_map import plan
from redaction_verifier.visible_map.plan import MapPlan
from redaction_verifier.visible_map.sink import MapSink, offer

SSN = "123-45-6789"
NAME = "Jane Q. Example"
SSN_RULES = [{"name": "ssn", "value": SSN}]


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str
    report: dict[str, Any] | None
    header: dict[str, Any] | None


def write_rules(tmp_path: Path, rules: list[dict[str, str]] | str, name: str = "rules") -> Path:
    if isinstance(rules, str):
        path = tmp_path / f"{name}.yaml"
        path.write_text(rules)
    else:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(rules))
    return path


def identity_clean(text: str, *neutral: str) -> str:
    return text


def _with_sink(scan: Any, sink: MapSink) -> Any:
    """*scan* with *sink* as its sink, whatever sink its caller passes."""
    @functools.wraps(scan)
    def with_sink(*args: Any, **kwargs: Any) -> Any:
        return scan(*args, **{**kwargs, "sink": sink})
    return with_sink


def run_main(monkeypatch: Any, target: Path, rules: Path, *extra: str, ocr: bool = False,
             sink: bool = True, json_report: bool = True) -> Run:
    """verify.main, OCR off unless asked, with (*sink*) or without a
    MapSink handed to the scan's hooks: exit code, stdout, the --json
    report and the header the map is planned from (None without a sink,
    or when the report was not written)."""
    if not ocr:
        monkeypatch.setattr(verify, "_OCR_IMPORTS_OK", False)
    the_sink = MapSink() if sink else None
    report_path = target.with_name(target.stem + (".on" if sink else ".off") + ".json")
    report_path.unlink(missing_ok=True)
    argv = ["--target", str(target), "--secrets", str(rules), *extra]
    if json_report:
        argv += ["--json", str(report_path)]
    stdout, stderr = io.StringIO(), io.StringIO()
    hooks = ("scan_page_layer", "scan_hidden_objects")
    originals = {name: getattr(verify, name) for name in hooks}
    try:
        if the_sink is not None:
            for name in hooks:
                setattr(verify, name, _with_sink(originals[name], the_sink))
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = verify.main(argv)
    finally:
        for name in hooks:
            setattr(verify, name, originals[name])
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    header = None
    if the_sink is not None and report is not None:
        with fitz.open(target) as doc:
            if not doc.needs_pass:
                offer(the_sink, "document", doc.page_count)
        header = {
            "rules": plan.rules_header(verify.load_rules(rules)),
            "sink": the_sink.to_header(),
            "report": report,
            "exit_code": code,
            "scan_code": code,
            "target_name": target.name,
            "version": verify.__version__,
        }
    return Run(code, stdout.getvalue(), stderr.getvalue(), report, header)


def plan_of(target: Path, header: dict[str, Any]) -> MapPlan:
    """The map's plan, built in-process from *header*."""
    data = target.read_bytes()
    rules = plan.Rules(header["rules"], identity_clean)
    return plan.make_plan(data, hashlib.sha256(data).hexdigest(), header, rules)


def mapped(monkeypatch: Any, tmp_path: Path, target: Path,
           rules: list[dict[str, str]] | str = SSN_RULES, *extra: str,
           ocr: bool = False) -> tuple[Run, MapPlan]:
    run = run_main(monkeypatch, target, write_rules(tmp_path, rules), *extra, ocr=ocr)
    assert run.header is not None, run.stderr
    return run, plan_of(target, run.header)


def without_ocr_warning(header: dict[str, Any]) -> dict[str, Any]:
    """*header* as if OCR had run: its OCR_UNAVAILABLE warning (which these
    OCR-off runs always carry, and which alone marks a map incomplete)
    dropped and the exit code recomputed, so a test sees what else makes
    the map incomplete."""
    report = dict(header["report"])
    report["warnings"] = [w for w in report["warnings"] if w["code"] != "OCR_UNAVAILABLE"]
    code = 1 if report["findings"] else 2 if report["warnings"] else 0
    return dict(header, report=report, exit_code=code, scan_code=code)


def sections(plan: MapPlan, section: str) -> list[tuple[int | None, str, str]]:
    return [(i.page, i.label, i.note) for i in plan.listed if i.section == section]


def boxes(plan: MapPlan, style: str | None = None) -> list[Any]:
    return [b for b in plan.boxes if style is None or b.style == style]
