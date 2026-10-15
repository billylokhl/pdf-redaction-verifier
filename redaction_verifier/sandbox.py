"""Running untrusted-input work in a contained child process (REDESIGN §3
rule 4 and §4 "Process boundary"; Phases 3b and 3d).

A prototype, not wired into the CLI yet: Phase 3b's child entry and
Phase 3d's integration use it. It gives one primitive, ``run``:

- the child is **spawned, not forked** (``subprocess`` with no
  ``preexec_fn``), as an isolated interpreter (``python -I``) with an
  **empty environment** apart from a private ``TMPDIR``;
- its working directory is a **private ``0700`` temporary directory**,
  removed when it ends, whatever happened;
- the input file is handed over as an **open, read-only descriptor**,
  never as a path: the child finds its number in ``INPUT_FD`` (set by the
  preamble from its first argument) and holds nothing else of the
  parent's (``close_fds``; stdin is ``/dev/null``);
- the child sets its own **rlimits** before running anything: CPU time,
  file size, core dumps off (``limits_preamble``);
- the parent enforces a **wall-clock limit** and a **memory watchdog**
  polling the child's physical footprint (macOS ignores memory rlimits),
  and on either kills the child's **whole process group**, so nothing it
  started outlives it;
- optionally, on macOS, an **OS sandbox** (``sandbox-exec``): no network,
  and no file writes outside the private directory.

Output is bounded: the child's stdout and stderr are read up to a cap,
and anything past it is dropped and reported as truncated. Nothing here
logs input bytes or the child's output.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Sequence

# Defaults: named, each with its reason (REDESIGN §3 rule 9).
WALL_CLOCK_S = 300.0         # backstop only: limits are work-based (rule 6)
CPU_S = 600                  # RLIMIT_CPU: a spinning child dies even if the parent stalls
FILE_BYTES = 256 << 20       # RLIMIT_FSIZE: the child writes only small temp files
MEMORY_BYTES = 4 << 30       # watchdog cap on the child's physical footprint
OUTPUT_BYTES = 64 << 20      # cap on what is read back from the child's stdout
STDERR_BYTES = 64 << 10      # stderr is diagnostics only: keep its tail
POLL_S = 0.05                # watchdog interval



@dataclass(frozen=True)
class Limits:
    wall_clock_s: float = WALL_CLOCK_S
    cpu_s: int = CPU_S
    file_bytes: int = FILE_BYTES
    memory_bytes: int = MEMORY_BYTES
    output_bytes: int = OUTPUT_BYTES
    os_sandbox: bool = False     # macOS sandbox-exec: no network, writes only in the temp dir


@dataclass(frozen=True)
class Result:
    returncode: int | None       # None: killed by the parent (see ``killed``)
    killed: str | None           # "wall-clock" or "memory", else None
    stdout: bytes
    stdout_truncated: bool
    stderr_tail: str
    peak_memory_bytes: int
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.killed is None and self.returncode == 0 and not self.stdout_truncated


def limits_preamble(limits: Limits) -> str:
    """Python the child runs first, before any untrusted work: its own
    rlimits (CPU time, file size, no core dumps), and ``INPUT_FD``, the
    input's descriptor, taken off its arguments."""
    return (
        "import resource, sys\n"
        "INPUT_FD = int(sys.argv.pop(1))\n"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({limits.cpu_s}, {limits.cpu_s}))\n"
        f"resource.setrlimit(resource.RLIMIT_FSIZE, ({limits.file_bytes}, {limits.file_bytes}))\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
    )


def _sandbox_profile(private_dir: Path) -> str:
    """A sandbox-exec profile: everything a Python interpreter needs to
    run, no network, and file writes only inside *private_dir*."""
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{private_dir}"))\n'
        '(allow file-write-data (literal "/dev/null"))\n'
    )


def footprint_bytes(pid: int) -> int:
    """The process's physical footprint, 0 if unknown (the process has
    gone). macOS: proc_pid_rusage's ri_phys_footprint; elsewhere,
    /proc's resident set."""
    if sys.platform == "darwin":
        return _darwin_footprint(pid)
    try:
        with open(f"/proc/{pid}/statm", "rb") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _darwin_footprint(pid: int) -> int:
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

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    info = RusageInfoV2()
    if libproc.proc_pid_rusage(pid, 2, ctypes.byref(info)) != 0:     # RUSAGE_INFO_V2
        return 0
    return int(info.ri_phys_footprint)


def _group_footprint(pgid: int, leader: int) -> int:
    """The footprint of every process in the child's group: a child that
    starts helpers is measured whole. The leader alone when the group
    cannot be listed."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True,
                             text=True, timeout=5).stdout
        pids = [int(p) for p, g in (line.split() for line in out.splitlines() if line.strip())
                if int(g) == pgid]
    except (OSError, ValueError, subprocess.SubprocessError):
        pids = [leader]
    return sum(footprint_bytes(p) for p in pids or [leader])


def _read_capped(stream: IO[bytes], cap: int, sink: list[bytes], over: list[bool]) -> None:
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        if total < cap:
            sink.append(chunk[:cap - total])
        if total + len(chunk) > cap:
            over.append(True)
        total += len(chunk)


def run(child_code: str, input_path: Path, args: Sequence[str] = (),
        limits: Limits = Limits()) -> Result:
    """Run *child_code* (Python source) in a contained child process with
    *input_path* open read-only on descriptor ``INPUT_FD``. The child sees
    *args* as ``sys.argv[1:]``. Never raises for anything the child does."""
    private = Path(tempfile.mkdtemp(prefix="prv-child-"))
    os.chmod(private, 0o700)
    try:
        source = private / "child.py"
        source.write_text(limits_preamble(limits) + child_code)
        with open(input_path, "rb") as handle:
            argv = [sys.executable, "-I", str(source), str(handle.fileno()), *args]
            if limits.os_sandbox and sys.platform == "darwin" and shutil.which("sandbox-exec"):
                argv = ["sandbox-exec", "-p", _sandbox_profile(private.resolve()), *argv]
            started = time.monotonic()
            proc = subprocess.Popen(
                argv, cwd=private, env={"TMPDIR": str(private)},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(handle.fileno(),), close_fds=True, start_new_session=True)
        out: list[bytes] = []
        over: list[bool] = []
        err: list[bytes] = []
        readers = [threading.Thread(target=_read_capped, args=(proc.stdout, limits.output_bytes,
                                                               out, over), daemon=True),
                   threading.Thread(target=_read_capped, args=(proc.stderr, STDERR_BYTES,
                                                               err, []), daemon=True)]
        for t in readers:
            t.start()
        killed, peak = None, 0
        while proc.poll() is None:
            peak = max(peak, _group_footprint(proc.pid, proc.pid))
            if peak > limits.memory_bytes:
                killed = "memory"
            elif time.monotonic() - started > limits.wall_clock_s:
                killed = "wall-clock"
            if killed:
                _kill_group(proc)
                break
            time.sleep(POLL_S)
        proc.wait()
        _kill_group(proc)           # whatever the child started dies with it
        for t in readers:
            t.join(timeout=5)
        return Result(None if killed else proc.returncode, killed, b"".join(out), bool(over),
                      b"".join(err)[-STDERR_BYTES:].decode("utf-8", "replace"), peak,
                      time.monotonic() - started)
    finally:
        shutil.rmtree(private, ignore_errors=True)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass            # gone, or (macOS) only unreaped members are left
