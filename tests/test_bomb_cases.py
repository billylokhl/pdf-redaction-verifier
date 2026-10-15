"""Bomb and hang cases (``Case.bomb``; eval/caselib/families/bombs.py),
judged out of process under each case's caps (eval/caselib/bombrun.py),
never in the test process. A case that pins how today's tool fails to
answer (``Bomb.today``: hang, memory, error) fails here once it stops
doing so, as a closed known gap does; one that answers is judged like any
case, against its known gap's label or its expected verdict."""

from __future__ import annotations

import subprocess
import time

import pytest

from caselib import REGISTRY, load
from caselib.bombrun import run_bomb
from caselib.model import Bomb, Case, expect
from caselib.run import available, build, judge, today_label

load()
HAVE = available()
BOMBS = sorted(cid for cid, c in REGISTRY.items() if c.bomb is not None)


def _params():
    for cid in BOMBS:
        missing = REGISTRY[cid].requires - HAVE
        marks = [pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}")] if missing else []
        yield pytest.param(cid, id=cid, marks=marks)


@pytest.mark.parametrize("cid", list(_params()))
def test_bomb_case(cid, tmp_path):
    case = REGISTRY[cid]
    pdf = build(case, tmp_path / f"{cid}.pdf")
    run = run_bomb(case, pdf, tmp_path)
    assert case.bomb is not None
    if case.bomb.today is not None:
        assert run.outcome == case.bomb.today, (
            f"{cid}: today pinned {case.bomb.today!r}, got {run.outcome!r} -- the tool's "
            "behaviour moved: relabel the case")
        return
    assert run.outcome is None, f"{cid}: no answer within its caps ({run.outcome})"
    assert run.scan is not None
    label = today_label(case)
    if label is not None:
        assert judge(case, run.scan, want=label).ok, f"{cid}: known gap moved"
        assert not judge(case, run.scan).ok, f"{cid}: gap closed -- remove its label"
    else:
        verdict = judge(case, run.scan)
        assert verdict.ok, f"{cid}: {'; '.join(verdict.problems)}"


def test_every_bomb_case_is_kept_out_of_the_in_process_runs():
    from scorecard.differential import select_cases as differential_cases

    from . import test_case_library as lib

    assert BOMBS
    assert not set(BOMBS) & {p.id for p in lib._params()}
    assert not set(BOMBS) & {c.id for c in differential_cases(None, have=HAVE)}
    assert not set(BOMBS) & {c.id for c in differential_cases(BOMBS, have=HAVE)}


def test_a_sleeper_is_killed_at_its_cap_with_nothing_left_behind(tmp_path):
    # The judge's own guarantee, with a deliberate sleeper standing in for
    # the CLI: killed at the wall-clock cap, its whole group gone.
    sleeper = Case(id="file.bomb-sleeper", truth="clean", cells=(), expected=expect(2),
                   story="A test-only sleeper.", build=lambda p: None,
                   bomb=Bomb(wall_clock_s=2, memory_bytes=1 << 30))
    pid_file = tmp_path / "pid"
    child = ("import os, subprocess, sys, time\n"
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
             "time.sleep(60)\n")
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    started = time.monotonic()
    run = run_bomb(sleeper, pdf, tmp_path, child=child)
    assert run.outcome == "hang" and time.monotonic() - started < 20
    time.sleep(0.3)
    grandchild = int(pid_file.read_text())
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)], capture_output=True,
                         text=True).stdout.strip()
    assert not out or out.startswith("Z"), "a process the CLI started outlived the kill"
