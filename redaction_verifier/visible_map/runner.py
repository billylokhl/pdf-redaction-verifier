"""redaction_verifier.visible_map.runner — the map, from the parent's side.

verify.main() drives it in three steps, none of which can change the
verdict:

1. before the scan, `prepare` checks the output path (the only step
   that can end a run: MAP_PATH_CONFLICT / MAP_PATH_UNWRITABLE, exit 2)
   and `MapJob.open_target` opens the target once — read-only, without
   blocking, regular files only — and hashes it. That descriptor is the
   one the child reads, so a target changed after the scan is caught by
   hash, and one swapped by rename by its identity;
2. the scan hands its truth to a sink.MapSink;
3. after the human report is out, `MapJob.make` runs the child and
   publishes what it returns, or says on stderr why there is no map.

The parent never holds the PDF in memory: the child reads it from the
descriptor, and the map comes back in 64 KB chunks straight into a 0600
temp file beside OUT, hashed as it streams. The child's own exit status
is ignored; only its record and the bytes that match it count.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from redaction_verifier.visible_map import paths

# Stdlib only: it runs before sys.path is set. -I already keeps the
# child's cwd, user site-packages and PYTHON* variables out; the path
# the parent hands over excludes '' and the parent's cwd too, and puts
# this package's own parent directory first, so a decoy package in
# either directory cannot load in its place.
BOOTSTRAP = (
    "import json, sys\n"
    "n = int(sys.stdin.buffer.readline(32))\n"
    "header = json.loads(sys.stdin.buffer.read(n))\n"
    "sys.path[:] = header['sys_path']\n"
    "from redaction_verifier.visible_map.child import run\n"
    "run(header)\n"
)
CHUNK = 64 << 10
HASH_CHUNK = 1 << 20
RECORD_CAP = 4096
TIMEOUT_BASE_S = 30.0
TIMEOUT_PER_PAGE_S = 0.25
TIMEOUT_CAP_S = 600.0
# The most map a run may send back: generous per page and per item the
# scan handed over, so a child that writes forever is stopped long
# before the disk fills.
LENGTH_BASE = 4 << 20
LENGTH_PER_PAGE = 256 << 10
LENGTH_PER_ITEM = 16 << 10
LENGTH_CAP = 2 << 30

_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class MapFailed(Exception):
    """No map: *code* is the stderr code, the message says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def timeout_for(pages: int) -> float:
    return min(TIMEOUT_CAP_S, TIMEOUT_BASE_S + TIMEOUT_PER_PAGE_S * max(pages, 0))


def length_cap(pages: int, items: int) -> int:
    return min(LENGTH_CAP, LENGTH_BASE + LENGTH_PER_PAGE * max(pages, 0)
               + LENGTH_PER_ITEM * max(items, 0))


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def prepare(out: Path, inputs: Sequence[Path]) -> MapJob:
    """The pre-scan path checks; raises paths.PathRefused."""
    paths.check_out(out, inputs)
    return MapJob(out)


class MapJob:
    """One run's map: its output path, the target's descriptor and hash,
    and while the map is being made, the child and the temp file."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.fd: int | None = None
        self.target: Path | None = None
        self.target_sha256: str | None = None
        self.identity: tuple[int, int, int, int] | None = None
        self.skip: MapFailed | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.tmp: Path | None = None
        self.tmp_id: tuple[int, int] | None = None
        # Every identity the temp file has had (FAT renumbers it once it
        # has data): abort() removes it only while it is still one of them.
        self.tmp_ids: set[tuple[int, int]] = set()
        # True once OUT holds this run's map: what happens after that
        # (removing the temp file, an interrupt) cannot unwrite it.
        self.published = False
        self.stopped = False

    # ── before the scan ───────────────────────────────────────────────
    def open_target(self, target: Path) -> None:
        """Open and hash the target. Never raises: a failure only means
        there will be no map (MAP_TARGET_UNREADABLE)."""
        try:
            self._open_target(target)
        except MapFailed as exc:
            self.skip = exc
        except Exception as exc:
            self.skip = MapFailed("MAP_TARGET_UNREADABLE",
                                  f"cannot read the target for the map ({type(exc).__name__})")

    def _open_target(self, target: Path) -> None:
        # O_NONBLOCK: a FIFO swapped in at the path opens at once instead
        # of waiting for a writer; it is then refused as not regular.
        opened = os.open(target, os.O_RDONLY | _NONBLOCK)
        try:
            # Never fd 0-2: with stdin closed the descriptor would land on 0,
            # where the child's own stdin (and every tool's) would replace it.
            fd = fcntl.fcntl(opened, fcntl.F_DUPFD_CLOEXEC, 3)
        finally:
            os.close(opened)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise MapFailed("MAP_TARGET_UNREADABLE", "the target is not a regular file")
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~_NONBLOCK)
            if _identity(os.stat(target)) != _identity(st):
                raise MapFailed("MAP_TARGET_CHANGED", "the target changed while it was opened")
            digest = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.pread(fd, HASH_CHUNK, offset)
                if not chunk:
                    break
                digest.update(chunk)
                offset += len(chunk)
            if _identity(os.fstat(fd)) != _identity(st) or offset != st.st_size:
                raise MapFailed("MAP_TARGET_CHANGED", "the target changed while it was hashed")
        except BaseException:
            os.close(fd)
            raise
        self.fd, self.target = fd, target
        self.target_sha256, self.identity = digest.hexdigest(), _identity(st)

    # ── after the report ──────────────────────────────────────────────
    def make(self, header: dict[str, Any], *, env: dict[str, str], cwd: str, pages: int,
             items: int) -> str:
        """Run the child and publish the map. Returns the headline the
        map carries; raises MapFailed (or anything unexpected) if no map
        was written, after cleaning up."""
        if self.skip is not None:
            raise self.skip
        if self.fd is None or self.target is None:
            raise MapFailed("MAP_TARGET_UNREADABLE", "the target was not opened")
        try:
            now = _identity(os.stat(self.target))
        except OSError:
            now = None
        if now != self.identity:
            raise MapFailed("MAP_TARGET_CHANGED", "the target changed after the scan")
        timeout = timeout_for(pages)
        header = dict(header, fd=self.fd, sys_path=child_sys_path(), timeout=timeout)
        payload = json.dumps(header).encode()
        try:
            record = self._exchange(f"{len(payload)}\n".encode() + payload, env=env, cwd=cwd,
                                    deadline=time.monotonic() + timeout,
                                    cap=length_cap(pages, items))
            assert self.tmp is not None and self.tmp_id is not None
            self._check_published_bytes(record)
            if self.stopped:
                raise MapFailed("MAP_INTERRUPTED", "interrupted")
            paths.publish(self.tmp, self.out, self.tmp_id)
            self.published = True
            self.tmp = None
        except paths.PathRefused as exc:
            self.abort()
            raise MapFailed(exc.code, exc.message)
        except BaseException:
            self.abort()
            raise
        return str(record.get("headline", ""))

    def _exchange(self, data: bytes, *, env: dict[str, str], cwd: str, deadline: float,
                  cap: int) -> dict[str, Any]:
        """Send the header, stream the map into the temp file, and wait
        for the child — all against one deadline."""
        assert self.fd is not None
        proc = self.proc = subprocess.Popen(
            [sys.executable, "-I", "-c", BOOTSTRAP],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=cwd, env=env, pass_fds=(self.fd,),
        )
        assert proc.stdin is not None and proc.stdout is not None
        stdin, stdout = proc.stdin.fileno(), proc.stdout.fileno()
        os.set_blocking(stdin, False)
        os.set_blocking(stdout, False)
        sel = selectors.DefaultSelector()
        sel.register(stdin, selectors.EVENT_WRITE)
        sel.register(stdout, selectors.EVENT_READ)
        pending = memoryview(data)
        head = bytearray()
        record: dict[str, Any] | None = None
        out_fd: int | None = None
        received = 0
        digest = hashlib.sha256()
        eof = False
        try:
            while not eof:
                if self.stopped:
                    raise MapFailed("MAP_INTERRUPTED", "interrupted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MapFailed("MAP_TIMEOUT", "the map took too long")
                for key, _ in sel.select(remaining):
                    if key.fd == stdin:
                        try:
                            pending = pending[os.write(stdin, pending[:CHUNK]):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            sel.unregister(stdin)
                            proc.stdin.close()
                        continue
                    chunk = os.read(stdout, CHUNK)
                    if not chunk:
                        eof = True
                        break
                    if record is None:
                        head += chunk
                        line, sep, rest = bytes(head).partition(b"\n")
                        if len(line) > RECORD_CAP:
                            raise MapFailed("MAP_OUTPUT_INVALID", "no record line")
                        if not sep:
                            continue
                        record = _parse_record(line, cap)
                        if not record.get("ok"):
                            code = str(record.get("code"))
                            raise MapFailed(
                                code if re.fullmatch(r"MAP_[A-Z_]{1,40}", code)
                                else "MAP_CHILD_FAILED",
                                "the map process could not build a map")
                        self.tmp, out_fd = paths.create_temp(self.out)
                        st = os.fstat(out_fd)
                        self.tmp_ids.add((st.st_dev, st.st_ino))
                        chunk = rest
                    received += len(chunk)
                    if received > record["length"]:
                        raise MapFailed("MAP_OUTPUT_INVALID", "more map than declared")
                    digest.update(chunk)
                    assert out_fd is not None
                    paths.write_all(out_fd, chunk)
        finally:
            sel.close()
            if out_fd is not None:
                try:
                    # Taken after the last write: on FAT (macOS msdosfs) an
                    # empty file's inode number changes once it has data.
                    st = os.fstat(out_fd)
                    self.tmp_id = (st.st_dev, st.st_ino)
                    self.tmp_ids.add(self.tmp_id)
                finally:
                    os.close(out_fd)
        # EOF: give the child what is left of the deadline to exit.
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if record is None:
            raise MapFailed("MAP_CHILD_FAILED", "the map process ended without a record")
        if received != record["length"] or digest.hexdigest() != record["map_sha256"]:
            raise MapFailed("MAP_OUTPUT_INVALID", "the map bytes do not match the record")
        if record["target_sha256"] != self.target_sha256:
            raise MapFailed("MAP_TARGET_CHANGED", "the target changed after the scan")
        self.proc = None
        proc.stdout.close()
        return record

    def _check_published_bytes(self, record: dict[str, Any]) -> None:
        import pymupdf as fitz

        assert self.tmp is not None
        # Read back through a descriptor that must be the file written.
        fd = os.open(self.tmp, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != self.tmp_id:
                raise MapFailed("MAP_OUTPUT_INVALID", "the map's temp file was replaced")
            # Hashed again as it streams, never held whole.
            digest = hashlib.sha256()
            got = 0
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                got += len(chunk)
            head = os.pread(fd, 1024, 0)
            tail = os.pread(fd, len(paths.MARKER_LINE), max(0, got - len(paths.MARKER_LINE)))
        finally:
            os.close(fd)
        if got != record["length"] or digest.hexdigest() != record["map_sha256"]:
            raise MapFailed("MAP_OUTPUT_INVALID", "the map's temp file changed")
        if paths.markers_in(head, tail) != (True, True):
            raise MapFailed("MAP_OUTPUT_INVALID", "the map lacks its markers")
        doc = fitz.open(self.tmp)
        try:
            if doc.is_repaired or doc.page_count < 1:
                raise MapFailed("MAP_OUTPUT_INVALID", "the map does not reopen cleanly")
        finally:
            doc.close()
        if self.target is None or _identity(os.stat(self.target)) != self.identity:
            raise MapFailed("MAP_TARGET_CHANGED", "the target changed after the scan")

    def interrupt(self) -> None:
        """From a signal handler: stop the map. Kills the child, if any, and
        refuses to publish; never raises."""
        try:
            self.stopped = True
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
        except BaseException:
            pass

    def wrote_map(self) -> bool:
        """OUT holds this run's map (checked by inode, so an interrupt
        between publishing and noting it still counts)."""
        if self.published:
            return True
        if self.tmp_id is None:
            return False
        try:
            st = os.lstat(self.out)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == self.tmp_id

    def abort(self) -> None:
        """Kill and reap the child, remove the temp file. Idempotent; never
        touches OUT."""
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        tmp, self.tmp = self.tmp, None
        if tmp is not None:
            paths.unlink_if(tmp, self.tmp_ids)

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)


def _parse_record(line: bytes, cap: int) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except ValueError:
        raise MapFailed("MAP_OUTPUT_INVALID", "the record is not JSON")
    if not isinstance(record, dict):
        raise MapFailed("MAP_OUTPUT_INVALID", "the record is not an object")
    if record.get("ok"):
        length = record.get("length")
        if (not isinstance(length, int) or isinstance(length, bool) or not 0 < length <= cap
                or not isinstance(record.get("map_sha256"), str)
                or not isinstance(record.get("target_sha256"), str)):
            raise MapFailed("MAP_OUTPUT_INVALID", "the record is malformed or over the size cap")
    return record


def child_sys_path() -> list[str]:
    """The parent's absolute sys.path minus '' and its cwd, with this
    package's parent directory first (so an uninstalled checkout works)."""
    home = str(Path(__file__).resolve().parents[2])
    cwd = os.getcwd()
    rest = [p for p in sys.path if p and os.path.isabs(p) and p not in (cwd, home)]
    return [home, *rest]


def rules_header(rules: Any) -> dict[str, Any]:
    """plan.rules_header, imported only when the map is made (after the
    report), so the planner's own imports can cost only the map."""
    from redaction_verifier.visible_map import plan

    return plan.rules_header(rules)
