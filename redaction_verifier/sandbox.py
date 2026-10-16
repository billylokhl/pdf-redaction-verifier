"""Running untrusted-input work in a contained child process (REDESIGN §3
rule 4 and §4 "Process boundary"; Phases 3b and 3d).

A prototype, not wired into the CLI yet: Phase 3b's child entry and
Phase 3d's integration use it. It gives one primitive, ``run``:

- the child is **spawned, not forked** (``subprocess`` with no
  ``preexec_fn``), as an isolated interpreter (``python -I``) with an
  **empty environment** apart from a private ``TMPDIR``;
- its working directory is a **private ``0700`` temporary directory**,
  removed when it ends (``Result.private_dir_removed`` says whether that
  worked, even against a child that locked a subdirectory);
- the input file is handed over as an **open, read-only descriptor**,
  never as a path: the child finds its number in ``INPUT_FD`` (set by the
  preamble from its first argument) and holds nothing else of the
  parent's (``close_fds``; stdin is ``/dev/null``). The child can still
  learn the file's path from the descriptor (``F_GETPATH`` on macOS,
  ``/proc/self/fd`` on Linux) and, without the OS sandbox, read files
  generally: the descriptor keeps the path out of its arguments, not out
  of its reach;
- the child sets its own **rlimits** before running anything: CPU time
  (``SIGXCPU`` at ``cpu_s``, ``SIGKILL`` a second later, which Linux
  enforces), file size, core dumps off;
- the parent enforces a **wall-clock limit** and a **memory watchdog**
  polling the physical footprint of the child's process group (macOS
  ignores memory rlimits); on either it kills the **whole process group**.
  The watchdog samples every ``POLL_S``, so a child that allocates fast
  can overshoot the cap before it is killed; a live member whose
  footprint cannot be read counts as over the cap (fail closed). Any
  exception in the parent, Ctrl-C included, kills the group too;
- a child that **leaves its process group** (``setsid``) is out of the
  group kill's reach. Without the OS sandbox (which denies ``fork``)
  nothing here stops that -- it needs a cgroup or subreaper on Linux. It
  is detected only while the escaped process still holds the child's
  output pipes once the child has ended (``killed="stray"``, not ok); one
  that also closes them is neither stopped nor detected;
- optionally, on macOS, an **OS sandbox** (``sandbox-exec``): no network,
  no direct file writes outside the private directory, no ``fork`` and no
  ``exec`` but the interpreter's own (``interpreters``), no Mach lookups (so no writes by proxy
  through system services). ``os_sandbox=True`` where that is unavailable
  raises: a requested limit is never silently dropped.

Output is bounded: stdout is read up to a cap (past it, dropped and
reported as truncated); stderr keeps its last ``STDERR_BYTES``. Nothing
here logs input bytes or the child's output.
"""

from __future__ import annotations

import os
import queue
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

# Defaults: named, each with its reason (REDESIGN §3 rule 9).
WALL_CLOCK_S = 300.0         # backstop only: limits are work-based (rule 6)
CPU_S = 600                  # RLIMIT_CPU soft limit: a spinning child dies even if the parent stalls
FILE_BYTES = 256 << 20       # RLIMIT_FSIZE: the child writes only small temp files
MEMORY_BYTES = 4 << 30       # watchdog cap on the child group's physical footprint
OUTPUT_BYTES = 64 << 20      # cap on what is read back from the child's stdout
STDERR_BYTES = 64 << 10      # stderr is diagnostics only: keep its last part
POLL_S = 0.05                # watchdog interval (see the overshoot note above)
NICE = 10                    # the child runs below the parent (3b plan, blocker 4)
PIPE_GRACE_S = 2.0           # how long the pipes may stay open after the child ends


class SandboxUnavailable(RuntimeError):
    """The OS sandbox was requested where it cannot be applied."""


@dataclass(frozen=True)
class Limits:
    wall_clock_s: float = WALL_CLOCK_S
    cpu_s: int = CPU_S
    file_bytes: int = FILE_BYTES
    memory_bytes: int = MEMORY_BYTES
    output_bytes: int = OUTPUT_BYTES
    os_sandbox: bool = False     # macOS sandbox-exec (see the module docstring)


@dataclass(frozen=True)
class Result:
    returncode: int | None       # None: killed by the parent (see ``killed``)
    killed: str | None           # "wall-clock", "memory", "cancelled", "stray" (left
                                 # its group), "reader-failed" (output may be
                                 # incomplete), else None
    stdout: bytes
    stdout_truncated: bool
    stderr_tail: str
    stderr_truncated: bool
    peak_memory_bytes: int
    elapsed_s: float
    os_sandboxed: bool
    private_dir_removed: bool

    @property
    def ok(self) -> bool:
        return (self.killed is None and self.returncode == 0 and not self.stdout_truncated
                and self.private_dir_removed)


def os_sandbox_available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def limits_preamble(limits: Limits) -> str:
    """Python the child runs first, before any untrusted work (3b-5a):

    - ``INPUT_FD``, the input's descriptor, and the lifeline, the parent's
      pid, the private directory and the package root, taken off its
      arguments (the package root, a code path, goes first on sys.path);
    - its own rlimits (CPU time, file size, no core dumps) and a lower
      priority (nice +10);
    - parent death: when the lifeline pipe (whose write end only the parent
      holds) reaches EOF, or -- on macOS -- a kqueue watch sees the parent
      exit, the child removes the private directory and kills its own
      process group. On Linux, PR_SET_PDEATHSIG(SIGKILL) as well (the
      parent spawns from one long-lived thread: it fires when the spawning
      thread exits). Each re-checks ``getppid()`` once armed: a parent gone
      before then is caught there. A child stuck in a long C call holds the
      GIL, so its watch threads run only once the call returns; the CPU
      rlimit bounds that, and on Linux PDEATHSIG does not wait for it (it
      kills the child alone: the 3b child starts no processes of its own,
      and 3d's OS sandbox denies fork)."""
    return (
        "import os, resource, shutil, signal, sys, threading\n"
        "INPUT_FD = int(sys.argv.pop(1))\n"
        "_LIFELINE, _PARENT = int(sys.argv.pop(1)), int(sys.argv.pop(1))\n"
        "_PRIVATE, _ROOT = sys.argv.pop(1), sys.argv.pop(1)\n"
        "if _ROOT:\n"
        "    sys.path.insert(0, _ROOT)\n"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({limits.cpu_s}, {limits.cpu_s + 1}))\n"
        f"resource.setrlimit(resource.RLIMIT_FSIZE, ({limits.file_bytes}, {limits.file_bytes}))\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        f"os.nice({NICE})\n"
        "def _orphaned():\n"
        "    shutil.rmtree(_PRIVATE, ignore_errors=True)\n"
        "    try:\n"
        "        os.killpg(os.getpgrp(), signal.SIGKILL)\n"
        "    finally:\n"
        "        os._exit(70)\n"
        "def _lifeline():\n"
        "    try:\n"
        "        while os.read(_LIFELINE, 64):\n"
        "            pass\n"
        "    except OSError:\n"
        "        pass\n"
        "    _orphaned()\n"
        "threading.Thread(target=_lifeline, daemon=True).start()\n"
        "if sys.platform.startswith('linux'):\n"
        "    import ctypes\n"
        "    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)\n"
        "elif sys.platform == 'darwin':\n"
        "    import select\n"
        "    _KQ = select.kqueue()\n"
        "    try:\n"
        "        _KQ.control([select.kevent(_PARENT, select.KQ_FILTER_PROC, select.KQ_EV_ADD,\n"
        "                                   select.KQ_NOTE_EXIT)], 0)\n"
        "    except OSError:\n"
        "        _orphaned()\n"
        "    def _parent_exit():\n"
        "        _KQ.control(None, 1)\n"
        "        _orphaned()\n"
        "    threading.Thread(target=_parent_exit, daemon=True).start()\n"
        "if os.getppid() != _PARENT:\n"
        "    _orphaned()\n"
    )


def _sandbox_profile(private_dir: Path, interpreters: Sequence[Path]) -> str:
    """A sandbox-exec profile for the child. Paths with a quote or a
    backslash are refused rather than escaped: they never occur in a
    temporary directory or an interpreter path we create."""
    for path in (private_dir, *interpreters):
        if '"' in str(path) or "\\" in str(path):
            raise SandboxUnavailable(f"a path the sandbox profile cannot quote: {path!r}")
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{private_dir}"))\n'
        '(allow file-write-data (literal "/dev/null"))\n'
        "(deny process-fork)\n"
        "(deny process-exec)\n"
        + "".join(f'(allow process-exec (literal "{p}"))\n' for p in interpreters)
        + "(deny mach-lookup)\n"
    )


def interpreters() -> list[Path]:
    """Every binary the child's interpreter may exec while starting: the
    environment's python and its resolved target, the base executable, and
    -- in a macOS framework build (python.org's, and CI's setup-python) --
    the Python.app binary its ``python`` re-execs into. The OS sandbox
    allows exactly these and nothing else."""
    candidates = [Path(sys.executable), Path(getattr(sys, "_base_executable", sys.executable)),
                  Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS"
                  / "Python"]
    found: list[Path] = []
    for path in candidates:
        for form in (path, path.resolve()):
            if form.exists() and form not in found:
                found.append(form)
    return found


def footprint_bytes(pid: int) -> int | None:
    """The process's physical footprint; None if it cannot be read
    (gone, or not ours to inspect). macOS: proc_pid_rusage's
    ri_phys_footprint; elsewhere, /proc's resident set."""
    if sys.platform == "darwin":
        return _darwin_footprint(pid)
    try:
        with open(f"/proc/{pid}/statm", "rb") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _libproc() -> Any:
    import ctypes

    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def _darwin_footprint(pid: int) -> int | None:
    import ctypes

    class RusageInfoV2(ctypes.Structure):
        _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
            (name, ctypes.c_uint64) for name in (
                "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
                "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
                "ri_proc_start_abstime", "ri_proc_exit_abstime", "ri_child_user_time",
                "ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups",
                "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread",
                "ri_diskio_byteswritten")]

    info = RusageInfoV2()
    if _libproc().proc_pid_rusage(pid, 2, ctypes.byref(info)) != 0:     # RUSAGE_INFO_V2
        return None
    return int(info.ri_phys_footprint)


def group_members(pgid: int) -> list[int]:
    """The live processes in process group *pgid*, without spawning a
    process: libproc's proc_listpgrppids on macOS, a /proc scan elsewhere."""
    if sys.platform == "darwin":
        import ctypes

        buf = (ctypes.c_int * 4096)()
        # Returns the number of pids it wrote (not bytes).
        n = _libproc().proc_listpgrppids(pgid, buf, ctypes.sizeof(buf))
        return [buf[i] for i in range(min(max(0, n), len(buf))) if buf[i]]
    members = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if os.getpgid(int(entry)) == pgid:
                    members.append(int(entry))
            except OSError:
                pass
    return members


def _group_footprint(pgid: int) -> int | None:
    """The footprint of every process in the group; None if a live member
    cannot be read (the caller fails closed)."""
    members = group_members(pgid)
    if not members and _alive(pgid):
        return None                     # a live group we cannot list: fail closed
    total = 0
    for pid in members:
        size = footprint_bytes(pid)
        if size is None:
            if _alive(pid):
                return None
            continue
        total += size
    return total


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _chunks(fd: int, stop: threading.Event) -> Iterator[bytes]:
    """*fd*'s data as it arrives, until its end or *stop*. It waits with
    poll (no descriptor-number limit, unlike select), never blocked in
    read, so a stop is seen even while an escaped process holds the pipe,
    and the fd is never read after the caller closes it."""
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while not stop.is_set():
        if not poller.poll(100):
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        yield chunk


def _reader(target: Callable[..., None], failed: list[str]) -> Callable[..., None]:
    """*target*, recording any exception in *failed*: a reader that dies
    must never leave lost output looking complete."""
    def run(*args: Any) -> None:
        try:
            target(*args)
        except Exception as exc:              # noqa: BLE001 -- recorded, then reported
            failed.append(type(exc).__name__)
    return run


def _read_head(fd: int, cap: int, sink: list[bytes], over: list[bool],
               stop: threading.Event) -> None:
    """Read *fd* to its end (as data arrives, never waiting for a full
    block), keeping its first *cap* bytes."""
    total = 0
    for chunk in _chunks(fd, stop):
        if total < cap:
            sink.append(chunk[:cap - total])
        if total + len(chunk) > cap:
            over.append(True)
        total += len(chunk)


def _read_tail(fd: int, cap: int, sink: list[bytes], over: list[bool],
               stop: threading.Event) -> None:
    """Read *fd* to its end, keeping its last *cap* bytes."""
    tail = bytearray()
    for chunk in _chunks(fd, stop):
        tail += chunk
        if len(tail) > cap:
            del tail[:len(tail) - cap]
            over[:] = [True]
    sink.append(bytes(tail))


def _chmod_dir(path: str) -> None:
    """Make *path* searchable and writable for us, but only if it is a real
    directory: never through a link the child planted (it could point
    anywhere the user owns)."""
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass


def _remove(path: Path) -> bool:
    """Remove the private directory, unlocking anything the child made
    unreadable first (links are removed, never followed); True when it is
    gone."""
    def unlock(func: Callable[..., Any], target: str, _exc: object) -> None:
        _chmod_dir(os.path.dirname(target))
        _chmod_dir(target)
        try:
            func(target)
        except OSError:
            pass

    for root, dirs, _files in os.walk(path):      # does not descend into links
        for name in dirs:
            _chmod_dir(os.path.join(root, name))
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=unlock)
    else:
        shutil.rmtree(path, onerror=unlock)
    return not path.exists()


# ── The spawner: one long-lived thread starts every child ──────────────
# Linux's PR_SET_PDEATHSIG fires when the *thread* that spawned the child
# exits, not the process: every child is spawned from this one thread,
# which lives as long as the parent, whatever thread calls start().
_SPAWN_QUEUE: queue.Queue[tuple[Callable[[], Any], list[Any], threading.Event]] = queue.Queue()
_SPAWNER: threading.Thread | None = None
_SPAWNER_LOCK = threading.Lock()


def _spawner_loop() -> None:
    while True:
        job, box, done = _SPAWN_QUEUE.get()
        try:
            box.append((True, job()))
        except BaseException as exc:            # noqa: BLE001 -- handed back to the caller
            box.append((False, exc))
        done.set()


def _on_spawner(job: Callable[[], Any]) -> Any:
    """*job()* run on the spawner thread; its result, or its exception
    raised here."""
    global _SPAWNER
    with _SPAWNER_LOCK:
        if _SPAWNER is None or not _SPAWNER.is_alive():
            _SPAWNER = threading.Thread(target=_spawner_loop, name="sandbox-spawner",
                                        daemon=True)
            _SPAWNER.start()
    if threading.current_thread() is _SPAWNER:
        return job()
    box: list[Any] = []
    done = threading.Event()
    _SPAWN_QUEUE.put((job, box, done))
    done.wait()
    ok, value = box[0]
    if not ok:
        raise value
    return value


# ── The sweeper: private directories a dead parent left ────────────────
_PRIVATE_PREFIX = "prv-child-"
_PRIVATE_NAME = re.compile(r"prv-child-(\d+)-[A-Za-z0-9_]+\Z")


def sweep(root: Path | None = None) -> int:
    """Remove the private directories left by parents that died (killed
    before they could, their child killed with them: PDEATHSIG cannot be
    caught). Only a real directory -- never a link -- named exactly as
    start() names one, owned by us, whose recorded parent pid is not
    alive. Returns how many were removed. Spooled bytes never reach it
    (opened and unlinked at once): what it removes is the child's own
    temporary files."""
    root = Path(tempfile.gettempdir()) if root is None else root
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        match = _PRIVATE_NAME.match(entry.name)
        if match is None:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            continue
        parent = int(match.group(1))
        if parent == os.getpid() or _alive(parent):
            continue
        if _remove(Path(entry.path)):
            removed += 1
    return removed


# ── A running child ────────────────────────────────────────────────────
class Child:
    """A contained child started by ``start()``: ``cancel()`` it, or
    ``wait()`` for its ``Result``. Its watchdog (wall clock, memory
    footprint) and its output readers run in threads of their own, never
    the caller's. As a context manager, any exception leaving the block --
    ``KeyboardInterrupt`` included -- cancels the child, waits for it and
    re-raises (a Ctrl-C reaches the parent's own interrupt path unchanged)."""

    def __init__(self, proc: subprocess.Popen[bytes], private: Path, limits: Limits,
                 lifeline: int, started: float) -> None:
        self.proc, self.private, self.limits = proc, private, limits
        self.started = started
        self._lifeline: int | None = lifeline
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._result: Result | None = None
        assert proc.stdout is not None and proc.stderr is not None
        self._out: list[bytes] = []
        self._out_over: list[bool] = []
        self._err: list[bytes] = []
        self._err_over: list[bool] = []
        self._stop = threading.Event()
        self._failed: list[str] = []
        self._readers = [
            threading.Thread(target=_reader(_read_head, self._failed), daemon=True,
                             args=(proc.stdout.fileno(), limits.output_bytes, self._out,
                                   self._out_over, self._stop)),
            threading.Thread(target=_reader(_read_tail, self._failed), daemon=True,
                             args=(proc.stderr.fileno(), STDERR_BYTES, self._err,
                                   self._err_over, self._stop))]
        for t in self._readers:
            t.start()
        self._watchdog = threading.Thread(target=self._watch, name="sandbox-watchdog",
                                          daemon=True)
        self._watchdog.start()

    def cancel(self) -> None:
        """Kill the child's whole process group (``killed="cancelled"``
        unless a limit killed it first). Idempotent."""
        self._cancelled.set()
        _kill_group(self.proc)

    def wait(self, timeout: float | None = None) -> Result | None:
        """The child's Result once it has ended and been cleaned up; None
        if *timeout* seconds pass first."""
        if not self._done.wait(timeout):
            return None
        assert self._result is not None
        return self._result

    def __enter__(self) -> Child:
        return self

    def __exit__(self, kind: object, exc: object, tb: object) -> None:
        if kind is not None:
            self.cancel()
        self._done.wait()

    def _watch(self) -> None:
        proc, limits = self.proc, self.limits
        killed: str | None = None
        peak = 0
        try:
            while proc.poll() is None:
                if self._cancelled.is_set():
                    killed = "cancelled"
                else:
                    size = _group_footprint(proc.pid)
                    if size is None:
                        killed = "memory"                # unreadable live member: fail closed
                    else:
                        peak = max(peak, size)
                        if peak > limits.memory_bytes:
                            killed = "memory"
                        elif time.monotonic() - self.started > limits.wall_clock_s:
                            killed = "wall-clock"
                if killed:
                    break
                time.sleep(POLL_S)
            if killed is None and self._cancelled.is_set() and proc.returncode is None:
                killed = "cancelled"
        finally:
            self._finish(killed, peak)

    def _finish(self, killed: str | None, peak: int) -> None:
        proc = self.proc
        try:
            _kill_group(proc)                     # whatever it started in its group dies too
            proc.wait()
            for t in self._readers:
                t.join(timeout=PIPE_GRACE_S)
            if any(t.is_alive() for t in self._readers):
                # Something outside the group still holds the pipes: the child
                # left its group. Report it; never pass its output as complete.
                killed = killed or "stray"
                self._stop.set()
                for t in self._readers:
                    t.join()
            assert proc.stdout is not None and proc.stderr is not None
            proc.stdout.close()
            proc.stderr.close()
            if self._failed:
                killed = killed or "reader-failed"     # output may be incomplete: never ok
            if killed is None and self._cancelled.is_set() and proc.returncode not in (0,):
                killed = "cancelled"
            elapsed = time.monotonic() - self.started
            returncode = None if killed in ("wall-clock", "memory", "cancelled") \
                else proc.returncode
            stdout, stderr = b"".join(self._out), b"".join(self._err)
        finally:
            if self._lifeline is not None:
                os.close(self._lifeline)              # the child's lifeline: EOF from here
                self._lifeline = None
            removed = _remove(self.private)
        self._result = Result(returncode, killed, stdout, bool(self._out_over),
                              stderr.decode("utf-8", "replace"), bool(self._err_over), peak,
                              elapsed, self.limits.os_sandbox, removed)
        self._done.set()


def start(child_code: str, input_path: Path | None = None, args: Sequence[str] = (),
          limits: Limits = Limits(), *, input_fd: int | None = None,
          package_root: Path | None = None) -> Child:
    """Start *child_code* (Python source) in a contained child process and
    return its handle at once. The input is *input_path*, opened read-only
    here, or *input_fd*, the parent's own open descriptor (both sides must
    read it with ``os.pread``, never ``read``/``seek``: they share one open
    file description and its offset). Either way the child finds it on
    ``INPUT_FD``. *package_root*, a code path (never a rules path), goes
    first on the child's sys.path. The child sees *args* as
    ``sys.argv[1:]``. Raises only SandboxUnavailable."""
    if limits.os_sandbox and not os_sandbox_available():
        raise SandboxUnavailable("os_sandbox requested, but sandbox-exec is not available")
    if (input_path is None) == (input_fd is None):
        raise ValueError("start() takes exactly one of input_path and input_fd")
    sweep()
    private = Path(tempfile.mkdtemp(prefix=f"{_PRIVATE_PREFIX}{os.getpid()}-")).resolve()
    lifeline_r, lifeline_w = os.pipe()
    handle = None
    try:
        source = private / "child.py"
        source.write_text(limits_preamble(limits) + child_code)
        if input_fd is None:
            assert input_path is not None
            handle = open(input_path, "rb")
            fd = handle.fileno()
        else:
            fd = input_fd
        # The interpreter as invoked, not resolved: a virtual environment's
        # python is a link whose packages come from its pyvenv.cfg, so the
        # resolved binary would not see them. The profile allows it, its
        # target and whatever a framework build re-execs into.
        argv = [str(Path(sys.executable)), "-I", str(source), str(fd), str(lifeline_r),
                str(os.getpid()), str(private), str(package_root or ""), *args]
        if limits.os_sandbox:
            argv = ["sandbox-exec", "-p", _sandbox_profile(private, interpreters()), *argv]

        def spawn() -> tuple[subprocess.Popen[bytes], float]:
            started = time.monotonic()
            return subprocess.Popen(
                argv, cwd=private, env={"TMPDIR": str(private)},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(fd, lifeline_r), close_fds=True,
                start_new_session=True), started
        proc, started = _on_spawner(spawn)
    except BaseException:
        os.close(lifeline_w)
        _remove(private)
        raise
    finally:
        os.close(lifeline_r)
        if handle is not None:
            handle.close()
    try:
        return Child(proc, private, limits, lifeline_w, started)
    except BaseException:
        _kill_group(proc)
        proc.wait()
        os.close(lifeline_w)
        _remove(private)
        raise


def run(child_code: str, input_path: Path, args: Sequence[str] = (),
        limits: Limits = Limits()) -> Result:
    """``start()`` and ``wait()`` in one: the child's Result. Any exception
    here, Ctrl-C included, cancels the child first."""
    with start(child_code, input_path, args, limits) as child:
        result = child.wait()
    assert result is not None
    return result


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass            # gone, or (macOS) only unreaped members are left
