"""Build a case and judge the tool's report against it."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .model import Case
from .pdfkit import finalize

# Warnings that only say the environment lacks a tool. A run without OCR
# or without qpdf/exiftool can still judge a case that does not need
# them, by leaving these out of the verdict (the exit code is exactly
# 1 if findings, else 2 if warnings, else 0 — tests/test_json.py).
_ENVIRONMENT_CODES = frozenset({"OCR_UNAVAILABLE", "TOOL_MISSING"})


def available() -> frozenset[str]:
    import verify
    have = {name for name in ("qpdf", "exiftool") if shutil.which(name)}
    if verify._OCR_IMPORTS_OK:
        have.add("ocr")
    return frozenset(have)


@contextlib.contextmanager
def _utc() -> Iterator[None]:
    """PyMuPDF stamps attachments and annotations with local time, and the
    zone's length ("Z" vs "-07'00'") cannot be pinned in place afterwards:
    build under UTC so every machine writes the same bytes."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def build(case: Case, path: Path) -> Path:
    with _utc():
        case.build(path)
    finalize(path)
    return path


def scan(case: Case, pdf: Path, workdir: Path) -> dict[str, Any]:
    """Run the verifier's CLI entry point in-process with --json."""
    import verify
    rules = workdir / f"{case.id}.rules.json"
    rules.write_text(json.dumps(list(case.rules)))
    report = workdir / f"{case.id}.report.json"
    verify.main(["--target", str(pdf), "--secrets", str(rules), "--json", str(report)])
    return json.loads(report.read_text())


@dataclass(frozen=True)
class Judgement:
    exit: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def effective_exit(report: dict[str, Any]) -> int:
    if report["error"] is not None:
        return report["exit_code"]
    warnings = [w for w in report["warnings"] if w["code"] not in _ENVIRONMENT_CODES]
    return 1 if report["findings"] else 2 if warnings else 0


def judge(case: Case, report: dict[str, Any]) -> Judgement:
    exit_code = effective_exit(report)
    problems = []
    want = case.expected
    if exit_code != want.exit:
        problems.append(f"exit {exit_code}, expected {want.exit}")
    found = {(f["rule"], f["storage"]) for f in report["findings"]}
    for rule, storage in sorted(want.findings - found):
        problems.append(f"missing finding {rule!r} ({storage})")
    codes = {w["code"] for w in report["warnings"]}
    for code in sorted(want.warnings - codes):
        problems.append(f"missing warning {code}")
    return Judgement(exit_code, tuple(problems))


def main(argv: list[str] | None = None) -> int:
    """python -m caselib.run OUTDIR [CASE_ID ...] — build and judge cases."""
    from . import REGISTRY, load
    load()
    args = sys.argv[1:] if argv is None else argv
    out = Path(args[0]); out.mkdir(parents=True, exist_ok=True)
    ids = args[1:] or sorted(REGISTRY)
    failed = 0
    for case_id in ids:
        case = REGISTRY[case_id]
        pdf = build(case, out / f"{case.id}.pdf")
        verdict = judge(case, scan(case, pdf, out))
        status = "ok" if verdict.ok else ("known gap" if case.known_gap else "FAIL")
        failed += status == "FAIL"
        print(f"{status:9} {case.id:48} exit {verdict.exit}  {'; '.join(verdict.problems)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
