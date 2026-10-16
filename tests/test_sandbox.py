"""The child-process sandbox prototype (redaction_verifier/sandbox.py;
REDESIGN §3 rule 4, Phases 3b/3d). Each test runs a tiny child with tight
limits, so the suite stays fast; nothing here reads a real file."""

from __future__ import annotations

import os
import signal
import stat
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
        "own = {str(_LIFELINE)} | ({str(_KQ.fileno())} if '_KQ' in globals() else set())\n"
        "print(sorted(set(os.listdir('/dev/fd')) - {'0', '1', '2', str(INPUT_FD)} - own))\n")
    r = sb.run(code, pdf, args=["x"], limits=FAST)
    lines = r.stdout.decode().splitlines()
    assert r.ok, r.stderr_tail
    assert lines[0] == "read-only"
    assert lines[1] == f"{pdf.stat().st_size} ['x']"
    # Beside the preamble's own (the lifeline, macOS's kqueue), only the
    # descriptor listdir itself opened may remain.
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


def test_ctrl_c_in_the_parent_cancels_the_child_and_re_raises(pdf, tmp_path):
    # Ctrl-C while the parent waits: the child is cancelled and cleaned up,
    # and the KeyboardInterrupt reaches the parent's own path unchanged.
    import threading

    pid_file = tmp_path / "pid"
    code = (f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n")

    def ctrl_c_once_started() -> None:
        for _ in range(200):
            if pid_file.exists():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=ctrl_c_once_started, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        sb.run(code, pdf, limits=FAST)
    assert time.monotonic() - started < 10
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


def test_a_framework_builds_relaunch_binary_is_an_allowed_interpreter(tmp_path, monkeypatch):
    # python.org's (and CI's) macOS builds re-exec python into
    # Resources/Python.app; the sandbox must allow it, or the child cannot start.
    app = tmp_path / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    app.parent.mkdir(parents=True)
    app.write_bytes(b"")
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
    allowed = sb.interpreters()
    assert app in allowed and Path(sys.executable) in allowed


# ── 3b-5a: the child's lifetime ────────────────────────────────────────
REPO = Path(__file__).resolve().parents[1]
SLEEPER = ("import os, sys, time\n"
           "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
           "time.sleep(120)\n")


def _wait_for(path: Path, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def _gone(pid: int, seconds: float = 10) -> float | None:
    """Seconds until *pid* is no longer alive, or None if it outlives *seconds*."""
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if not _alive(pid):
            return time.monotonic() - started
        time.sleep(0.05)
    return None


def _parent(tmp_path: Path, pdf: Path, child_code: str) -> tuple[subprocess.Popen[bytes], Path]:
    """A separate parent process that starts *child_code* from a
    short-lived thread (which then exits: PDEATHSIG fires on the spawning
    thread's exit, so the spawner thread must be sandbox's own) and then
    just lives."""
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import sys, threading, time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from pathlib import Path\n"
        "from redaction_verifier import sandbox as sb\n"
        "held = []\n"
        "def go():\n"
        f"    held.append(sb.start({child_code!r}, Path({str(pdf)!r}),\n"
        f"                         args=[{str(pid_file)!r}], limits=sb.Limits(wall_clock_s=120)))\n"
        "t = threading.Thread(target=go)\n"
        "t.start(); t.join()\n"
        "print('started', flush=True)\n"
        "time.sleep(120)\n")
    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                              start_new_session=True)
    return parent, pid_file


def test_the_child_outlives_the_spawning_thread_and_dies_with_the_parent(pdf, tmp_path):
    parent, pid_file = _parent(tmp_path, pdf, SLEEPER)
    try:
        _wait_for(pid_file)
        child = int(pid_file.read_text())
        time.sleep(0.5)                       # the thread that called start() has exited
        assert _alive(child), "the child died with the thread that started it"
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        delay = _gone(child)
        assert delay is not None, "the child outlived its parent's SIGKILL"
        # Its private directory: removed by the child (lifeline, kqueue) or,
        # where PDEATHSIG killed it first, by the next sweep.
        import tempfile
        sb.sweep()
        left = [p for p in Path(tempfile.gettempdir()).glob(f"prv-child-{parent.pid}-*")]
        assert left == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_a_child_busy_in_c_dies_once_the_call_returns(pdf, tmp_path):
    # A long C call holds the GIL: on macOS the lifeline and kqueue threads
    # run only once it returns (accepted; the CPU rlimit bounds it); on
    # Linux PDEATHSIG does not wait. The delay is measured, not bounded
    # tighter than the call itself.
    busy = ("import os, re, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "re.match(r'(a+)+$', 'a' * 27 + 'b')\n"      # seconds in one C call
            "time.sleep(120)\n")
    parent, pid_file = _parent(tmp_path, pdf, busy)
    try:
        _wait_for(pid_file)
        child = int(pid_file.read_text())
        time.sleep(0.3)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        delay = _gone(child, seconds=120)
        assert delay is not None
        print(f"\nchild death after a parent SIGKILL, mid C call: {delay:.2f} s")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_a_private_directory_a_dead_parent_left_is_swept(pdf, tmp_path):
    root = tmp_path / "tmp"
    root.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = root / f"prv-child-{dead.pid}-abc123"
    stale.mkdir()
    (stale / "left.txt").write_text("child temp")
    live = root / f"prv-child-{os.getppid()}-def456"
    live.mkdir()
    other = root / "prv-child-notapid-xyz"
    other.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours to remove")
    link = root / f"prv-child-{dead.pid}-lnk999"
    link.symlink_to(outside, target_is_directory=True)
    assert sb.sweep(root) == 1
    assert not stale.exists()
    assert live.exists() and other.exists() and link.is_symlink()
    assert (outside / "keep.txt").exists()            # a link is never followed


def test_start_and_wait_and_cancel(pdf, tmp_path):
    pid_file = tmp_path / "pid"
    child = sb.start(SLEEPER, pdf, args=[str(pid_file)], limits=FAST)
    _wait_for(pid_file)
    assert child.wait(timeout=0.2) is None            # still running
    child.cancel()
    result = child.wait(timeout=20)
    assert result is not None and result.killed == "cancelled" and not result.ok
    assert result.returncode is None and result.private_dir_removed
    assert not _alive(int(pid_file.read_text()))
    child.cancel()                                    # idempotent


def test_cancel_kills_the_whole_group(pdf, tmp_path):
    pid_file = tmp_path / "grandchild"
    code = ("import os, subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open(sys.argv[1], 'w').write(str(p.pid))\n"
            "time.sleep(120)\n")
    child = sb.start(code, pdf, args=[str(pid_file)], limits=FAST)
    _wait_for(pid_file)
    child.cancel()
    assert child.wait(timeout=20) is not None
    assert _gone(int(pid_file.read_text())) is not None


def test_an_inherited_descriptor_is_read_with_pread_and_its_offset_never_moves(pdf, tmp_path):
    code = ("import os\n"
            "print(os.pread(INPUT_FD, 9, 0))\n")
    with open(pdf, "rb") as f:
        f.seek(5)
        before = os.lseek(f.fileno(), 0, os.SEEK_CUR)
        result = sb.start(code, input_fd=f.fileno(), limits=FAST).wait()
        assert os.lseek(f.fileno(), 0, os.SEEK_CUR) == before == 5
        assert f.read() == pdf.read_bytes()[5:]       # still open, still ours
    assert result is not None and result.ok, result and result.stderr_tail
    assert result.stdout.strip() == repr(pdf.read_bytes()[:9]).encode()


def test_exactly_one_input(pdf):
    with pytest.raises(ValueError):
        sb.start("pass", pdf, input_fd=0)
    with pytest.raises(ValueError):
        sb.start("pass")


def test_the_child_runs_below_the_parent_and_finds_its_package(pdf, tmp_path):
    root = tmp_path / "pkgroot"
    (root / "probe_pkg").mkdir(parents=True)
    (root / "probe_pkg" / "__init__.py").write_text("NAME = 'found'\n")
    code = ("import os, probe_pkg\n"
            "print(os.nice(0), probe_pkg.NAME)\n")
    result = sb.start(code, pdf, limits=FAST, package_root=root).wait()
    assert result is not None and result.ok, result and result.stderr_tail
    niceness, name = result.stdout.split()
    assert int(niceness) >= os.nice(0) + sb.NICE and name == b"found"
