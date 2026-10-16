"""Bomb and hang cases (``Case.bomb``; eval/caselib/families/bombs.py),
judged out of process under each case's caps (eval/caselib/bombrun.py),
never in the test process. A case that pins how today's tool fails to
answer (``Bomb.today``: hang, memory, error) fails here once it stops
doing so, as a closed known gap does; one that answers is judged like any
case, against its known gap's label or its expected verdict -- or, where
it answers with the right verdict under the wrong storage, against the
answer ``Bomb.today`` pins (and must still fail its expected label)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from caselib import REGISTRY, load
from caselib.bombrun import run_bomb
from caselib.lock import LOCK
from caselib.model import Bomb, Case, Expect, expect
from caselib.run import available, build, judge, today_label

load()
HAVE = available()
LOCKED = json.loads(LOCK.read_text())
BOMBS = sorted(cid for cid, c in REGISTRY.items() if c.bomb is not None)


# Judging a bomb costs real time and memory (about a minute for the set),
# so it runs once per platform per CI run, not on every Python leg: on
# macOS, on Linux's Python 3.13 leg, or anywhere with RUN_BOMBS=1. Nothing
# in it depends on the Python version; the structural tests below run on
# every leg.
RUN_BOMBS = (os.environ.get("RUN_BOMBS") == "1" or sys.platform == "darwin"
             or sys.version_info[:2] == (3, 13))


def _params():
    for cid in BOMBS:
        missing = REGISTRY[cid].requires - HAVE
        marks = [pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}")] if missing else []
        if not RUN_BOMBS:
            marks.append(pytest.mark.skip(reason="bombs run on macOS, Linux 3.13, or RUN_BOMBS=1"))
        yield pytest.param(cid, id=cid, marks=marks)


@pytest.mark.parametrize("cid", list(_params()))
def test_bomb_case(cid, tmp_path):
    case = REGISTRY[cid]
    pdf = build(case, tmp_path / f"{cid}.pdf")
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == LOCKED[cid], (
        f"{cid}: generator output changed -- review it, then run "
        "`PYTHONPATH=eval:. python -m caselib.lock`")
    run = run_bomb(case, pdf, tmp_path)
    assert run.result.private_dir_removed, f"{cid}: the sandbox left its private directory"
    assert case.bomb is not None
    today = case.bomb.today
    if isinstance(today, str):
        assert run.outcome == today, (
            f"{cid}: today pinned {today!r}, got {run.outcome!r} (exit "
            f"{run.result.returncode}) -- the tool's behaviour moved: relabel the case")
        if today.startswith("error:"):
            assert run.result.returncode == 2, f"{cid}: an error must exit 2, failing closed"
        return
    assert run.outcome is None, (f"{cid}: no answer within its caps ({run.outcome}, exit "
                                 f"{run.result.returncode})")
    assert run.scan is not None
    label = today if isinstance(today, Expect) else today_label(case)
    if label is not None:
        assert judge(case, run.scan, want=label).ok, f"{cid}: known gap moved"
        assert not judge(case, run.scan).ok, f"{cid}: gap closed -- remove its label"
        if isinstance(today, Expect):
            assert judge(case, run.scan).exit == case.expected.exit, cid
    else:
        verdict = judge(case, run.scan)
        assert verdict.ok, f"{cid}: {'; '.join(verdict.problems)}"
    # The visible map re-reads the target: asked for, it must move no
    # verdict (whether or not a map comes out: a map that cannot be made
    # costs only the map). A bomb whose `today` is a string returns above and
    # is not covered here (nothing answers to compare). (The map's identity tests in test_visible_map*.py skip
    # bombs, which they would otherwise scan in this process.)
    mapped_dir = tmp_path / "mapped"
    mapped_dir.mkdir()
    mapped = run_bomb(case, pdf, mapped_dir, visible_map=mapped_dir / "map.pdf")
    assert mapped.outcome is None, f"{cid}: with the visible map: {mapped.outcome}"
    assert mapped.scan is not None
    for field in ("exit_code", "error", "findings", "warnings"):
        assert mapped.scan.report[field] == run.scan.report[field], (cid, field)


def test_every_bomb_case_is_kept_out_of_the_in_process_runs():
    from scorecard.differential import select_cases as differential_cases

    from . import test_case_library as lib
    from . import test_views_text_readings as readings
    from . import test_visible_map as map_sink
    from . import test_visible_map_cli as map_cli

    assert BOMBS
    assert not set(BOMBS) & {p.id for p in lib._params()}
    assert not set(BOMBS) & set(readings._library_cases())
    assert not set(BOMBS) & set(map_sink._identity_cases())
    assert not set(BOMBS) & set(map_cli._identity_cases())
    assert not set(BOMBS) & {c.id for c in differential_cases(None, have=HAVE)}
    assert not set(BOMBS) & {c.id for c in differential_cases(BOMBS, have=HAVE)}


def test_a_sleeper_is_killed_at_its_cap_with_nothing_left_behind(tmp_path):
    # The judge's own guarantee, with a deliberate sleeper standing in for
    # the CLI: killed at the wall-clock cap, its whole group gone.
    sleeper = Case(id="file.bomb-sleeper", truth="clean", cells=(), expected=expect(2),
                   story="A test-only sleeper.", build=lambda p: None,
                   bomb=Bomb(wall_clock_s=5, memory_bytes=1 << 30))
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
    assert pid_file.exists(), "the sleeper was killed before it started its grandchild"
    grandchild = int(pid_file.read_text())
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)], capture_output=True,
                         text=True).stdout.strip()
    assert not out or out.startswith("Z"), "a process the CLI started outlived the kill"


def test_an_allocator_is_killed_at_its_memory_cap_with_nothing_left_behind(tmp_path):
    # The judge's memory cap, with a deliberate allocator standing in for
    # the CLI: 1 GiB touched under a 256 MiB cap.
    hog = Case(id="file.bomb-allocator", truth="clean", cells=(), expected=expect(2),
               story="A test-only allocator.", build=lambda p: None,
               bomb=Bomb(wall_clock_s=60, memory_bytes=256 << 20))
    child = ("import time\n"
             "held = [b'x' * (64 << 20) for _ in range(16)]\n"
             "time.sleep(60)\n")
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    started = time.monotonic()
    run = run_bomb(hog, pdf, tmp_path, child=child)
    assert run.outcome == "memory" and time.monotonic() - started < 30
    assert run.result.private_dir_removed
