"""The child-process sandbox prototype (redaction_verifier/sandbox.py;
REDESIGN §3 rule 4, Phases 3b/3d). Each test runs a tiny child with tight
limits, so the suite stays fast; nothing here reads a real file."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from redaction_verifier import sandbox as sb

FAST = sb.Limits(wall_clock_s=20)


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "in.pdf"
    path.write_bytes(b"%PDF-1.7\n% fabricated test input\n")
    return path


def test_the_input_arrives_as_a_read_only_descriptor_and_nothing_else(pdf):
    code = (
        "import os, sys\n"
        "data = os.read(INPUT_FD, 1 << 20)\n"
        "try:\n"
        "    os.write(INPUT_FD, b'x'); print('writable')\n"
        "except OSError:\n"
        "    print('read-only')\n"
        "print(len(data), sys.argv[1:])\n"
        "print(sorted(set(os.listdir('/dev/fd')) - {'0', '1', '2', str(INPUT_FD)}))\n")
    r = sb.run(code, pdf, args=["x"], limits=FAST)
    lines = r.stdout.decode().splitlines()
    assert r.ok, r.stderr_tail
    assert lines[0] == "read-only"
    assert lines[1] == f"{pdf.stat().st_size} ['x']"
    # Only the descriptor listdir itself opened may remain.
    assert len(eval(lines[2])) <= 1


def test_the_environment_is_empty_and_the_working_directory_private(pdf):
    code = ("import os\nprint(sorted(os.environ))\nprint(oct(os.stat('.').st_mode & 0o777))\n"
            "print(os.getcwd() == os.environ['TMPDIR'] or os.path.realpath(os.getcwd()) "
            "== os.path.realpath(os.environ['TMPDIR']))\nprint(os.getcwd())")
    r = sb.run(code, pdf, limits=FAST)
    names, mode, same, cwd = r.stdout.decode().splitlines()
    # Only the private TMPDIR, plus what the interpreter and the OS set
    # themselves (macOS adds LC_CTYPE and __CF_USER_TEXT_ENCODING).
    assert set(eval(names)) <= {"TMPDIR", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert mode == "0o700" and same == "True"
    assert not Path(cwd).exists()                  # removed when the child ended


def test_the_child_is_an_isolated_interpreter(pdf):
    r = sb.run("import sys\nprint(sys.flags.isolated, sys.flags.ignore_environment)", pdf,
               limits=FAST)
    assert r.stdout.decode().split() == ["1", "1"]


def test_the_wall_clock_kills_the_whole_process_group(pdf, tmp_path):
    # The child starts a grandchild in its own group, reports its pid, and
    # sleeps: on the wall-clock kill neither may survive.
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(60)\n")
    started = time.monotonic()
    r = sb.run(code, pdf, limits=sb.Limits(wall_clock_s=1.5))
    assert r.killed == "wall-clock" and r.returncode is None
    assert time.monotonic() - started < 15
    grandchild = int(r.stdout.decode().split()[0])
    time.sleep(0.3)
    assert not _alive(grandchild), "a process the child started outlived it"


def test_the_memory_watchdog_kills_a_child_over_its_cap(pdf):
    code = ("x = bytearray(1200 << 20)\n"
            "x[::4096] = b'1' * len(x[::4096])\n"     # touch every page: real footprint
            "import time; time.sleep(30)\n")
    r = sb.run(code, pdf, limits=sb.Limits(memory_bytes=300 << 20, wall_clock_s=25))
    assert r.killed == "memory"
    assert r.peak_memory_bytes > 300 << 20


def test_the_cpu_rlimit_stops_a_spinning_child(pdf):
    r = sb.run("while True:\n    pass\n", pdf, limits=sb.Limits(cpu_s=1, wall_clock_s=20))
    assert r.killed is None and r.returncode == -signal.SIGXCPU


def test_the_file_size_rlimit_stops_a_large_write(pdf):
    r = sb.run("open('big', 'wb').write(b'x' * (2 << 20))", pdf,
               limits=sb.Limits(file_bytes=1 << 20, wall_clock_s=20))
    assert r.returncode == 1 and "File too large" in r.stderr_tail


def test_no_core_dumps(pdf):
    r = sb.run("import resource\nprint(resource.getrlimit(resource.RLIMIT_CORE))", pdf,
               limits=FAST)
    assert r.stdout.decode().strip() == "(0, 0)"


def test_output_past_its_cap_is_dropped_and_reported(pdf):
    r = sb.run("import sys\nsys.stdout.buffer.write(b'x' * 300000)", pdf,
               limits=sb.Limits(output_bytes=100000, wall_clock_s=20))
    assert r.stdout_truncated and len(r.stdout) == 100000 and not r.ok


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                    reason="the OS sandbox is macOS sandbox-exec")
def test_the_os_sandbox_denies_network_and_writes_outside_its_directory(pdf, tmp_path):
    outside = tmp_path / "escape.txt"
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=1); print('net open')\n"
            "except PermissionError:\n"
            "    print('net denied')\n"
            "except OSError:\n"
            "    print('net error')\n"
            "try:\n"
            f"    open({str(outside)!r}, 'w').write('x'); print('wrote outside')\n"
            "except OSError:\n"
            "    print('outside denied')\n"
            "open('inside', 'w').write('ok'); print('wrote inside')\n")
    r = sb.run(code, pdf, limits=sb.Limits(os_sandbox=True, wall_clock_s=20))
    assert r.stdout.decode().splitlines() == ["net denied", "outside denied", "wrote inside"]
    assert not outside.exists()


def _alive(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                         text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")
