"""The child-process sandbox prototype (redaction_verifier/sandbox.py;
REDESIGN §3 rule 4, Phases 3b/3d). Each test runs a tiny child with tight
limits, so the suite stays fast; nothing here reads a real file."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
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
    # SIGXCPU at the soft limit (macOS); Linux enforces the hard limit, a
    # second later, with SIGKILL.
    r = sb.run("while True:\n    pass\n", pdf, limits=sb.Limits(cpu_s=1, wall_clock_s=20))
    assert r.killed is None and r.returncode in (-signal.SIGXCPU, -signal.SIGKILL)


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


def test_stderr_keeps_its_tail(pdf):
    code = ("import sys\nsys.stderr.write('HEAD' + 'x' * (200 << 10) + 'TAIL')\n")
    r = sb.run(code, pdf, limits=FAST)
    assert r.stderr_tail.endswith("TAIL") and not r.stderr_tail.startswith("HEAD")
    assert r.stderr_truncated and len(r.stderr_tail) <= sb.STDERR_BYTES


def test_an_exception_in_the_parent_kills_the_child(pdf, tmp_path, monkeypatch):
    # Ctrl-C (or anything) while the parent waits must not leave the child.
    pid_file = tmp_path / "pid"
    code = (f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n")
    real_sleep = time.sleep

    def interrupted(seconds: float) -> None:
        if pid_file.exists():
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(sb.time, "sleep", interrupted)
    with pytest.raises(KeyboardInterrupt):
        sb.run(code, pdf, limits=FAST)
    monkeypatch.undo()
    time.sleep(0.3)
    assert not _alive(int(pid_file.read_text()))


def test_a_child_that_leaves_its_group_is_reported_not_passed(pdf, tmp_path):
    # Without the OS sandbox a setsid'd grandchild is out of the group
    # kill's reach: it is detected (it holds the pipes) and the result is
    # not ok, never a silent success with lost output.
    pid_file = tmp_path / "stray"
    code = ("import os, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "print('parent done', flush=True)\n")
    try:
        r = sb.run(code, pdf, limits=FAST)
        assert r.killed == "stray" and not r.ok
        assert r.stdout == b"parent done\n"
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("sandboxed", [False, pytest.param(True, marks=pytest.mark.skipif(
    not sb.os_sandbox_available(), reason="the OS sandbox is macOS sandbox-exec"))])
def test_the_child_sees_the_environments_packages(pdf, sandboxed):
    # The child must be able to run the work it exists for: pikepdf (and
    # MuPDF) live only in the project's environment, not the base Python.
    r = sb.run("import pikepdf, pymupdf\nprint('ok')", pdf,
               limits=sb.Limits(os_sandbox=sandboxed, wall_clock_s=60))
    assert r.ok and r.stdout == b"ok\n", r.stderr_tail[-400:]


def test_cleanup_never_follows_a_link_the_child_planted(pdf, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    os.chmod(outside, 0o755)
    code = f"import os\nos.symlink({str(outside)!r}, 'link')\nos.mkdir('d')\nos.chmod('d', 0)\n"
    r = sb.run(code, pdf, limits=FAST)
    assert r.private_dir_removed and outside.exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_a_locked_subdirectory_is_still_removed(pdf):
    r = sb.run("import os\nos.mkdir('d')\nopen('d/x', 'w').write('x')\nos.chmod('d', 0)\n",
               pdf, limits=FAST)
    assert r.private_dir_removed and r.ok


def test_a_requested_os_sandbox_is_never_silently_dropped(pdf, monkeypatch):
    monkeypatch.setattr(sb, "os_sandbox_available", lambda: False)
    with pytest.raises(sb.SandboxUnavailable):
        sb.run("print(1)", pdf, limits=sb.Limits(os_sandbox=True))


MACOS_SANDBOX = pytest.mark.skipif(not sb.os_sandbox_available(),
                                   reason="the OS sandbox is macOS sandbox-exec")


@MACOS_SANDBOX
def test_the_os_sandbox_denies_fork_exec_and_writes_by_proxy(pdf):
    code = ("import os, subprocess\n"
            "try:\n"
            "    os.fork(); print('forked')\n"
            "except OSError:\n"
            "    print('fork denied')\n"
            "try:\n"
            "    subprocess.run(['/usr/bin/defaults', 'write', 'com.example.prv-sandbox-test',"
            " 'k', 'v']); print('exec ran')\n"
            "except OSError:\n"
            "    print('exec denied')\n")
    r = sb.run(code, pdf, limits=sb.Limits(os_sandbox=True, wall_clock_s=20))
    assert r.os_sandboxed
    assert r.stdout.decode().splitlines() == ["fork denied", "exec denied"]
    assert not Path("~/Library/Preferences/com.example.prv-sandbox-test.plist").expanduser().exists()


@MACOS_SANDBOX
def test_the_os_sandbox_denies_writes_by_proxy_in_process(pdf):
    # In process, with no exec at all: CFPreferences asks cfprefsd (a Mach
    # service) to write the user's preferences. Without Mach lookups it
    # cannot, so nothing is written.
    domain = "com.example.prv-sandbox-proxy"
    code = (
        "import ctypes, ctypes.util\n"
        "cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))\n"
        "cf.CFStringCreateWithCString.restype = ctypes.c_void_p\n"
        "cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]\n"
        "cf.CFPreferencesSetAppValue.argtypes = [ctypes.c_void_p] * 3\n"
        "cf.CFPreferencesAppSynchronize.argtypes = [ctypes.c_void_p]\n"
        "cf.CFPreferencesAppSynchronize.restype = ctypes.c_bool\n"
        "s = lambda t: cf.CFStringCreateWithCString(None, t.encode(), 0x08000100)\n"
        f"app = s({domain!r})\n"
        "cf.CFPreferencesSetAppValue(s('k'), s('v'), app)\n"
        "print(bool(cf.CFPreferencesAppSynchronize(app)))\n")
    plist = Path(f"~/Library/Preferences/{domain}.plist").expanduser()
    try:
        r = sb.run(code, pdf, limits=sb.Limits(os_sandbox=True, wall_clock_s=20))
        assert r.stdout.decode().strip() == "False", r.stderr_tail[-300:]
        assert not plist.exists()
    finally:
        plist.unlink(missing_ok=True)


@MACOS_SANDBOX
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


def test_many_open_descriptors_in_the_parent_do_not_lose_output(pdf, tmp_path):
    # select() cannot watch a descriptor numbered 1024 or more; the readers
    # poll instead, so output still arrives with the parent holding many.
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
    try:
        r = sb.run("print('ok')", pdf, limits=FAST)
    finally:
        for fd in held:
            os.close(fd)
    assert r.ok and r.stdout == b"ok\n"


def test_a_reader_that_fails_is_never_ok(pdf, monkeypatch):
    def broken(*_args):
        raise RuntimeError("reader broke")

    monkeypatch.setattr(sb, "_read_head", broken)
    r = sb.run("print('ok')", pdf, limits=FAST)
    assert r.killed == "reader-failed" and not r.ok
