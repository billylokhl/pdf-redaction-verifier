"""Run the verifier's CLI as a subprocess, judged on the process's exit
code (docs/REDESIGN.md §5, Phase 0c: "One runner for pytest and the
scorecard: the CLI as a subprocess per case with a timeout").

`caselib.run.scan` calls `verify.main()` in-process for pytest's speed;
this module launches the real CLI (`python verify.py ...`) as a
subprocess so the scorecard can run it from an arbitrary git ref (see
`scorecard.refs`) and enforce a wall-clock timeout per file, matching how
the tool is actually invoked in production.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class CliResult:
    """The outcome of one subprocess run of the verifier's CLI.

    *report* is the parsed `--json` output, or None if the process timed
    out, was killed, or did not write a (valid) report. *crashed* is true
    for any process outcome the scorecard cannot judge normally: a
    timeout; an exit code outside 0/1/2; a missing or malformed report;
    or the process's own exit code disagreeing with what the report
    itself claims (`report["exit_code"]`) — mirroring
    `caselib.run.judge`'s "report says exit X, process Y" check. That
    last case matters on its own: a fail-open bug where the process
    actually returns 0 while its report still (correctly) lists a
    warning would otherwise slip through as a plain report to normalise,
    not a crash.
    """

    returncode: int | None
    report: dict[str, Any] | None
    elapsed: float
    timed_out: bool
    stderr_tail: str

    @property
    def crashed(self) -> bool:
        if self.timed_out or self.report is None or self.returncode not in (0, 1, 2):
            return True
        return self.report.get("exit_code") != self.returncode


def run_cli(
    python_exe: str,
    verify_path: Path,
    target: Path,
    rules: Sequence[Mapping[str, str]],
    workdir: Path,
    case_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> CliResult:
    """Run `python verify_path --target target --secrets ... --json ...`
    as a subprocess and return its outcome. *workdir* holds the scratch
    rules/report files, named after *case_id* so parallel runs (and runs
    against different refs) never collide."""
    workdir.mkdir(parents=True, exist_ok=True)
    rules_path = workdir / f"{case_id}.rules.json"
    rules_path.write_text(json.dumps(list(rules)))
    report_path = workdir / f"{case_id}.report.json"
    report_path.unlink(missing_ok=True)  # never judge a stale report

    argv = [
        python_exe,
        str(verify_path),
        "--target",
        str(target),
        "--secrets",
        str(rules_path),
        "--json",
        str(report_path),
    ]

    start = time.monotonic()
    timed_out = False
    stderr_tail = ""
    returncode: int | None = None
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        returncode = proc.returncode
        stderr_tail = proc.stderr[-2000:] if proc.stderr else ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        raw_stderr = exc.stderr
        decoded = raw_stderr.decode("utf-8", "replace") if isinstance(raw_stderr, bytes) else raw_stderr
        stderr_tail = (decoded or "")[-2000:]
    elapsed = time.monotonic() - start

    report: dict[str, Any] | None = None
    if not timed_out and report_path.exists():
        try:
            report = json.loads(report_path.read_text())
        except (json.JSONDecodeError, OSError):
            report = None

    return CliResult(
        returncode=returncode,
        report=report,
        elapsed=elapsed,
        timed_out=timed_out,
        stderr_tail=stderr_tail,
    )


def current_python() -> str:
    """The interpreter running this process — used to launch the CLI so
    the same environment (installed deps) is used for every ref."""
    return sys.executable
