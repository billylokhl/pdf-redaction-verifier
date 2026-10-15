"""Judging a bomb case (``Case.bomb``) out of process: the verifier's CLI
runs in a child through redaction_verifier.sandbox, under the case's
wall-clock and memory caps, its whole process group (qpdf and exiftool
included) killed at either. Never in the test process: a bomb is made to
cost a reader time or memory.

The child is the sandbox's isolated interpreter; it gets ``PATH`` (so the
external tools resolve as they do for a user) and the repository on its
import path, then calls ``verify.main`` exactly as ``caselib.run.scan``
does. The input descriptor the sandbox hands over is not used: the CLI
takes a path, which is what a user gives it.

This is a test harness that bounds the whole CLI's time and memory, so
its child runs everything -- the rules (fabricated) included. It is not
REDESIGN §4's child process, which never receives the rules; that one is
Phase 3b's, built on the same sandbox.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from redaction_verifier import sandbox

from .model import Case
from .run import Scan

REPO = Path(__file__).resolve().parents[2]

CHILD = """
import os, sys
repo, path, pdf, rules, report = sys.argv[1:6]
os.environ["PATH"] = path
sys.path.insert(0, repo)
import verify
sys.exit(verify.main(["--target", pdf, "--secrets", rules, "--json", report]))
"""


@dataclass(frozen=True)
class BombRun:
    outcome: str | None          # "hang", "memory", "error:CODE", or None (it answered)
    scan: Scan | None            # the answer, when there was one
    result: sandbox.Result


def run_bomb(case: Case, pdf: Path, workdir: Path, child: str = CHILD) -> BombRun:
    """Run the CLI on *pdf* for *case* under its caps; *child* is the
    child's code (tests replace it to prove the caps hold)."""
    assert case.bomb is not None, case.id
    rules = workdir / f"{case.id}.rules.json"
    rules.write_text(json.dumps(list(case.rules)))
    report = workdir / f"{case.id}.report.json"
    report.unlink(missing_ok=True)                 # never judge an earlier run's report
    limits = sandbox.Limits(wall_clock_s=case.bomb.wall_clock_s,
                            memory_bytes=case.bomb.memory_bytes,
                            cpu_s=int(case.bomb.wall_clock_s) + 60)
    result = sandbox.run(child, pdf, args=[str(REPO), os.environ.get("PATH", ""), str(pdf),
                                           str(rules), str(report)], limits=limits)
    if result.killed == "wall-clock":
        return BombRun("hang", None, result)
    if result.killed == "memory":
        return BombRun("memory", None, result)
    if not report.exists():
        return BombRun(f"error:no-report (exit {result.returncode})", None, result)
    data = json.loads(report.read_text())
    if data.get("error"):
        return BombRun(f"error:{data['error']['code']}", None, result)
    return BombRun(None, Scan(data, result.returncode if result.returncode is not None else -1),
                   result)
