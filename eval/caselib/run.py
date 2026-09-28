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

from redaction_verifier.model import WARNING_CODES

from .model import Case, Expect
from .pdfkit import finalize

# Warnings that only say the environment lacks a tool, and which tool.
_ENVIRONMENT = {"OCR_UNAVAILABLE": "ocr", "TOOL_MISSING": None}


def available() -> frozenset[str]:
    """This machine's own capability, independent of which `verify.py` is
    imported: OCR is probed directly (mirroring redaction_verifier/views/
    ocr.py's own Vision import) rather than read off
    `verify._OCR_IMPORTS_OK`, so this stays the same true answer no
    matter which git ref's verify.py happens to be the current process's
    `import verify` (the scorecard's reference and candidate are two
    different files on disk, but only one of them is ever the in-process
    `verify` module — the OCR bridge itself is a property of this
    machine, not of either file)."""
    have = {name for name in ("qpdf", "exiftool") if shutil.which(name)}
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
        from Foundation import NSData  # noqa: F401

        ocr_ok = True
    except ImportError:
        ocr_ok = False
    have.add("ocr" if ocr_ok else "no-ocr")
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
    if case.writer == "fitz":
        finalize(path)          # raw and committed files are used exactly as written
    return path


@dataclass(frozen=True)
class Scan:
    report: dict[str, Any]
    exit_code: int              # what the process returned


def scan(case: Case, pdf: Path, workdir: Path) -> Scan:
    """Run the verifier's CLI entry point in-process with --json."""
    import verify
    rules = workdir / f"{case.id}.rules.json"
    rules.write_text(json.dumps(list(case.rules)))
    report = workdir / f"{case.id}.report.json"
    report.unlink(missing_ok=True)          # never judge an earlier run's report
    code = verify.main(["--target", str(pdf), "--secrets", str(rules), "--json", str(report)])
    return Scan(json.loads(report.read_text()), code)


@dataclass(frozen=True)
class Judgement:
    exit: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def _environmental(warning: dict[str, Any], have: frozenset[str]) -> bool:
    """A "not available" warning for a tool this machine lacks. On a full
    environment nothing is environmental: a missing tool there is a bug."""
    if warning["code"] not in _ENVIRONMENT or os.environ.get("REQUIRE_FULL_ENV") == "1":
        return False
    tool = _ENVIRONMENT[warning["code"]] or warning.get("tool")
    return tool not in have


def judge(case: Case, result: Scan, have: frozenset[str] | None = None,
          want: Expect | None = None) -> Judgement:
    """Judge a scan against *want* (default: the case's correct verdict)."""
    have = available() if have is None else have
    want = case.expected if want is None else want
    report = result.report
    problems: list[str] = []
    if report["error"] is not None:
        return Judgement(result.exit_code, (f"error {report['error']['code']}",))
    if report["exit_code"] != result.exit_code:
        problems.append(f"report says exit {report['exit_code']}, process {result.exit_code}")

    warnings = [w for w in report["warnings"] if not _environmental(w, have)]
    exit_code = 1 if report["findings"] else 2 if warnings else 0
    if len(warnings) == len(report["warnings"]) and exit_code != result.exit_code:
        problems.append(f"exit {result.exit_code} disagrees with the report ({exit_code})")
    if exit_code != want.exit:
        problems.append(f"exit {exit_code}, expected {want.exit}")

    found = {(f["rule"], f["storage"]) for f in report["findings"]}
    for rule, storage in sorted(want.findings - found):
        problems.append(f"missing finding {rule!r} ({storage})")
    # For a rule the case expects, where it was found is exact: an extra
    # storage (a live object also called ORPHANED) is a mislabel.
    expected_rules = {rule for rule, _ in want.findings}
    for rule, storage in sorted(found - want.findings):
        if rule in expected_rules:
            problems.append(f"unexpected finding {rule!r} ({storage})")
    by_layer = {(f["rule"], f["layer"]) for f in report["findings"]}
    for rule, layer in sorted(want.layers - by_layer):
        problems.append(f"no {layer} finding for {rule!r}")

    listed = {(w["code"], w["storage"]) for w in warnings}
    for code, storage in sorted(want.warnings, key=str):  # type: ignore[assignment]
        if (code, storage) not in listed:
            problems.append(f"missing warning {code} ({storage})")
    expected_codes = {code for code, _ in want.warnings}
    # For a code the case expects, where it is filed is exact too (mirrors
    # the findings check above): the same code also appearing under a
    # storage class the case does not list — e.g. a review warning filed
    # under both "live" and "orphaned" — is a mislabel, not just noise.
    for code, storage in sorted(listed - want.warnings, key=str):  # storage may be None
        if code in expected_codes:
            problems.append(f"unexpected {code} ({storage}) — expected a different storage")
    for w in warnings:
        if WARNING_CODES[w["code"]] == "coverage" and w["code"] not in expected_codes:
            problems.append(f"unexpected {w['code']} ({w['storage']}): {w['message'][:80]}")
    return Judgement(exit_code, tuple(problems))


def main(argv: list[str] | None = None) -> int:
    """python -m caselib.run OUTDIR [CASE_ID ...] — build and judge cases."""
    from . import REGISTRY, load
    load()
    args = sys.argv[1:] if argv is None else argv
    out = Path(args[0]); out.mkdir(parents=True, exist_ok=True)
    # Perf cases are large on purpose (eval/README.md); a bare invocation
    # with no case ids must not build all of them by surprise.
    ids = args[1:] or sorted(cid for cid, c in REGISTRY.items() if not c.perf)
    have = available()
    failed = 0
    for case_id in ids:
        case = REGISTRY[case_id]
        missing = case.requires - have
        if missing:
            print(f"{'skip':9} {case.id:48} needs {', '.join(sorted(missing))}")
            continue
        pdf = build(case, out / f"{case.id}.pdf")
        result = scan(case, pdf, out)
        if case.known_gap:
            verdict = judge(case, result, want=case.known_gap.today)
            status = "gap" if verdict.ok else "GAP MOVED"
        else:
            verdict = judge(case, result)
            status = "ok" if verdict.ok else "FAIL"
        failed += not verdict.ok
        print(f"{status:9} {case.id:48} exit {verdict.exit}  {'; '.join(verdict.problems)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
