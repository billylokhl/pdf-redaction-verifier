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
  ``exec`` but the interpreter's, no Mach lookups (so no writes by proxy
  through system services). ``os_sandbox=True`` where that is unavailable
  raises: a requested limit is never silently dropped.

Output is bounded: stdout is read up to a cap (past it, dropped and
reported as truncated); stderr keeps its last ``STDERR_BYTES``. Nothing
here logs input bytes or the child's output.
"""

from __future__ import annotations

import os
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
    killed: str | None           # "wall-clock", "memory", "stray" (left its group), else None
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
    """Python the child runs first, before any untrusted work: its own
    rlimits (CPU time, file size, no core dumps), and ``INPUT_FD``, the
    input's descriptor, taken off its arguments."""
    return (
        "import resource, sys\n"
        "INPUT_FD = int(sys.argv.pop(1))\n"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({limits.cpu_s}, {limits.cpu_s + 1}))\n"
        f"resource.setrlimit(resource.RLIMIT_FSIZE, ({limits.file_bytes}, {limits.file_bytes}))\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
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
    select, never blocked in read, so a stop is seen even while an escaped
    process holds the pipe, and the fd is never read after the caller
    closes it."""
    while not stop.is_set():
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        yield chunk


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


def run(child_code: str, input_path: Path, args: Sequence[str] = (),
        limits: Limits = Limits()) -> Result:
    """Run *child_code* (Python source) in a contained child process with
    *input_path* open read-only on descriptor ``INPUT_FD``. The child sees
    *args* as ``sys.argv[1:]``. Raises only SandboxUnavailable (the OS
    sandbox was requested where it cannot apply); never for anything the
    child does."""
    if limits.os_sandbox and not os_sandbox_available():
        raise SandboxUnavailable("os_sandbox requested, but sandbox-exec is not available")
    private = Path(tempfile.mkdtemp(prefix="prv-child-")).resolve()   # created 0700
    proc: subprocess.Popen[bytes] | None = None
    removed = False
    try:
        source = private / "child.py"
        source.write_text(limits_preamble(limits) + child_code)
        # The interpreter as invoked, not resolved: a virtual environment's
        # python is a link whose packages come from its pyvenv.cfg, so the
        # resolved binary would not see them. The profile allows both.
        interpreter = Path(sys.executable)
        with open(input_path, "rb") as handle:
            argv = [str(interpreter), "-I", str(source), str(handle.fileno()), *args]
            if limits.os_sandbox:
                profile = _sandbox_profile(private, (interpreter, interpreter.resolve()))
                argv = ["sandbox-exec", "-p", profile, *argv]
            started = time.monotonic()
            proc = subprocess.Popen(
                argv, cwd=private, env={"TMPDIR": str(private)},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(handle.fileno(),), close_fds=True, start_new_session=True)
        assert proc.stdout is not None and proc.stderr is not None
        out: list[bytes] = []
        out_over: list[bool] = []
        err: list[bytes] = []
        err_over: list[bool] = []
        stop = threading.Event()
        readers = [
            threading.Thread(target=_read_head, daemon=True,
                             args=(proc.stdout.fileno(), limits.output_bytes, out, out_over,
                                   stop)),
            threading.Thread(target=_read_tail, daemon=True,
                             args=(proc.stderr.fileno(), STDERR_BYTES, err, err_over, stop))]
        for t in readers:
            t.start()
        killed, peak = None, 0
        while proc.poll() is None:
            size = _group_footprint(proc.pid)
            if size is None:
                killed = "memory"                    # unreadable live member: fail closed
            else:
                peak = max(peak, size)
                if peak > limits.memory_bytes:
                    killed = "memory"
                elif time.monotonic() - started > limits.wall_clock_s:
                    killed = "wall-clock"
            if killed:
                _kill_group(proc)
                break
            time.sleep(POLL_S)
        _kill_group(proc)                            # whatever it started in its group dies too
        proc.wait()
        for t in readers:
            t.join(timeout=PIPE_GRACE_S)
        if any(t.is_alive() for t in readers):
            # Something outside the group still holds the pipes: the child
            # left its group. Report it; never pass its output as complete.
            # The readers stop first, so neither reads a closed, reused fd.
            killed = killed or "stray"
            stop.set()
            for t in readers:
                t.join()
        proc.stdout.close()
        proc.stderr.close()
        elapsed = time.monotonic() - started
        returncode = None if killed in ("wall-clock", "memory") else proc.returncode
        stdout, stderr = b"".join(out), b"".join(err)
    finally:
        if proc is not None and proc.returncode is None:
            _kill_group(proc)                        # any exception, Ctrl-C included
            proc.wait()
        removed = _remove(private)
    return Result(returncode, killed, stdout, bool(out_over),
                  stderr.decode("utf-8", "replace"), bool(err_over), peak, elapsed,
                  limits.os_sandbox, removed)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass            # gone, or (macOS) only unreaped members are left
