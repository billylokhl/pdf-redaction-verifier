"""redaction_verifier.visible_map.paths — where the map may be written.

A map is only ever written to a new file, and an existing file is only
ever removed when it is one of this tool's own maps: a regular file the
user owns with the marker on line 2 AND as the file's last line. A marker
beats a `--force` flag, which users add by habit: `--visible-map
original_unredacted.pdf --force` would destroy the original.

Every check here opens files without following a final symbolic link and
without blocking (a FIFO swapped in at OUT must not hang the run).
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Callable, Collection, Sequence

from redaction_verifier.visible_map.markers import MARKER, MARKER_LINE, markers_in
from redaction_verifier.visible_map.markers import insert_markers as insert_markers


# os.link fails with these where the filesystem has no hard links (FAT,
# some SMB mounts); the map is then written in place, tail marker last.
_NO_HARD_LINKS = frozenset(
    getattr(errno, name) for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EMLINK", "EXDEV")
    if hasattr(errno, name)
)
_HEAD_BYTES = 1024
# A temp file older than this cannot belong to a live run: twice the
# child's longest deadline (runner.TIMEOUT_CAP_S; a test keeps them tied).
STALE_AFTER_S = 1200.0
# Bytes of OUT's name kept in a sibling's name (NAME_MAX is 255 bytes).
_NAME_PART = 200
_COPY_CHUNK = 64 << 10
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class PathRefused(Exception):
    """The map's output path is unsafe: *code* is MAP_PATH_CONFLICT or
    MAP_PATH_UNWRITABLE, *message* names only the file's base name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _read_markers(path: Path, expect: os.stat_result) -> tuple[bool, bool]:
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino):
            return False, False
        head = os.pread(fd, _HEAD_BYTES, 0)
        tail = os.pread(fd, len(MARKER_LINE), max(0, st.st_size - len(MARKER_LINE)))
    finally:
        os.close(fd)
    return markers_in(head, tail)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


def sibling(out: Path, suffix: str, pid: int | None = None) -> Path:
    """A hidden name beside OUT, ".OUT.<pid>.<suffix>". A long OUT name is
    hashed so the result stays under the filesystem's name limit."""
    part = out.name
    if len(part.encode("utf-8", "surrogateescape")) > _NAME_PART:
        part = hashlib.sha256(part.encode("utf-8", "surrogateescape")).hexdigest()[:24]
    return out.with_name(f".{part}.{os.getpid() if pid is None else pid}.{suffix}")


def _owned(st: os.stat_result) -> bool:
    return stat.S_ISREG(st.st_mode) and (not hasattr(os, "getuid") or st.st_uid == os.getuid())


def _ours(path: Path, *, whole: bool) -> os.stat_result | None:
    """lstat of *path* when it is a regular file the user owns carrying
    the line-2 marker (and the tail marker too, when *whole*)."""
    st = os.lstat(path)
    if not _owned(st):
        return None
    head, tail = _read_markers(path, st)
    return st if head and (tail or not whole) else None


def _empty(path: Path) -> os.stat_result | None:
    st = os.lstat(path)
    return st if _owned(st) and st.st_size == 0 else None


def _unlink_checked(path: Path, st: os.stat_result,
                    still: Callable[[Path], os.stat_result | None], *,
                    same_inode: bool = True) -> None:
    """Remove *path*, which was checked as *st*, without removing a file
    swapped in since the check: move it aside (to a name nothing else
    uses) first, check what was moved with *still* (and, unless the file
    is empty -- FAT renumbers an empty file when it is renamed -- that it
    is the same inode), and put back anything that is not what was
    checked."""
    aside = sibling(path, f"{secrets.token_hex(4)}.old")
    if os.path.lexists(aside):
        raise PathRefused("MAP_PATH_CONFLICT", f"{aside.name} is in the way")
    os.rename(path, aside)
    try:
        moved = still(aside)
    except OSError:
        moved = None
    if moved is not None and (not same_inode or _identity(moved) == _identity(st)):
        os.unlink(aside)
        return
    try:
        try:
            os.link(aside, path)        # never over a file that appeared at path since
            os.unlink(aside)
        except OSError as exc:
            if exc.errno not in _NO_HARD_LINKS:
                raise
            # No hard links here: claim the name with a new empty file of
            # our own first, so the rename can only replace that.
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600))
            os.rename(aside, path)
    except OSError:
        raise PathRefused("MAP_PATH_CONFLICT", f"{path.name} changed while it was being "
                          f"replaced; it is now the hidden file {aside.name} -- rename it back")
    raise PathRefused("MAP_PATH_CONFLICT", f"{path.name} changed while it was being checked; "
                      "left untouched")


def remove_stale(out: Path) -> None:
    """Remove OUT if it is one of this tool's maps; raise PathRefused if
    anything else is there. A missing OUT is fine."""
    name = out.name
    try:
        st = os.lstat(out)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PathRefused("MAP_PATH_UNWRITABLE", f"cannot inspect {name}: {exc.strerror or exc}")
    if stat.S_ISLNK(st.st_mode):
        raise PathRefused("MAP_PATH_CONFLICT", f"{name} is a symbolic link; refusing to write "
                          "the visible map through it")
    if not stat.S_ISREG(st.st_mode):
        raise PathRefused("MAP_PATH_CONFLICT", f"{name} exists and is not a regular file")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PathRefused("MAP_PATH_CONFLICT", f"{name} exists and belongs to another user")
    try:
        head, tail = _read_markers(out, st)
    except FileNotFoundError:
        return                  # gone since the lstat (a concurrent run moved it)
    except OSError as exc:
        raise PathRefused("MAP_PATH_UNWRITABLE", f"cannot read {name}: {exc.strerror or exc}")
    if head and tail:
        try:
            _unlink_checked(out, st, lambda p: _ours(p, whole=True))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PathRefused("MAP_PATH_UNWRITABLE",
                              f"cannot replace the old visible map {name}: {exc.strerror or exc}")
        return
    if head:
        raise PathRefused(
            "MAP_PATH_CONFLICT",
            f"{name}: line-2 marker {MARKER.decode()} present, tail marker missing — a partial "
            "map from an interrupted run; delete it")
    raise PathRefused("MAP_PATH_CONFLICT", f"{name} exists and is not a visible map written by "
                      "this tool; move it or choose another name")


def sweep_temps(out: Path) -> None:
    """Remove what an earlier run killed mid-map left beside OUT: its
    ".OUT.<pid>.<token>.tmp", ".old" and ".probe" files, when the user
    owns them, they carry the line-2 marker or are empty, no process with
    that id runs here, and they are older than STALE_AFTER_S (a run on
    another host sharing the directory, or in another pid namespace, can
    look dead by its id; no run writes for longer than twice its child's
    deadline). Best effort: never raises."""
    prefix = sibling(out, "x", 0).name[:-len("0.x")]
    pattern = re.compile(re.escape(prefix) + r"(\d+)\.(?:[0-9a-f]{8}\.)?(tmp|old|probe)")
    try:
        names = os.listdir(out.parent)
    except OSError:
        return
    now = time.time()
    for name in names:
        m = pattern.fullmatch(name)
        if m is None:
            continue
        try:
            alive = _alive(int(m.group(1)))
        except (OverflowError, ValueError, OSError):
            alive = True            # an id no process can have: leave the file
        if alive:
            continue                # this run's, or a concurrent run's
        path = out.with_name(name)
        try:
            if now - os.lstat(path).st_mtime < STALE_AFTER_S:
                continue
            st = _empty(path)
            if st is not None:
                _unlink_checked(path, st, _empty, same_inode=False)
            elif (st := _ours(path, whole=False)) is not None:
                _unlink_checked(path, st, lambda q: _ours(q, whole=False))
        except (OSError, PathRefused):
            continue


def _alive(pid: int) -> bool:
    """A process with this id exists (it may be another run of this tool
    still writing its temp file)."""
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:             # EPERM: it exists, as another user's
        return True
    return True


def check_inputs(out: Path, inputs: Sequence[Path]) -> None:
    """OUT names none of *inputs* (the target, the rules, the --json
    report): nothing here may ever remove one of them."""
    if any(_same_file(out, path) for path in inputs):
        raise PathRefused("MAP_PATH_CONFLICT", "--visible-map must not name the target PDF, the "
                          "rules file or the --json report")


def check_out(out: Path, inputs: Sequence[Path]) -> None:
    """The pre-scan checks: OUT names none of *inputs*, any old map
    there is removed, and OUT's directory takes a new file."""
    check_inputs(out, inputs)
    remove_stale(out)
    sweep_temps(out)
    probe = sibling(out, f"{secrets.token_hex(4)}.probe")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise PathRefused("MAP_PATH_UNWRITABLE",
                          f"cannot write the visible map {out.name}: {exc.strerror or exc}")


def create_temp(out: Path) -> tuple[Path, int]:
    """A new 0600 temp file beside OUT, named for this run alone."""
    tmp = sibling(out, f"{secrets.token_hex(4)}.tmp")
    return tmp, os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)


def unlink_if(path: Path, ids: Collection[tuple[int, int]]) -> None:
    """Remove *path* only if it is still one of the files *ids* names."""
    try:
        if _identity(os.lstat(path)) in ids:
            os.unlink(path)
    except FileNotFoundError:
        pass


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def publish(tmp: Path, out: Path, expect: tuple[int, int]) -> None:
    """Give the finished temp file OUT's name without ever replacing a
    file that is not one of this tool's maps (os.link refuses to). *tmp*
    must still be the file written (*expect*: its device and inode). On
    a filesystem without hard links, write OUT in place instead, front to
    back, so the tail marker lands last: a run killed mid-copy leaves a
    file with only the line-2 marker, which the next run refuses to
    delete rather than mistaking it for a finished map. Once OUT exists,
    nothing here raises: removing the temp file is best effort."""
    remove_stale(out)
    if _identity(os.lstat(tmp)) != expect:
        raise PathRefused("MAP_PATH_CONFLICT", "the map's temp file was replaced")
    try:
        os.link(tmp, out)
    except FileExistsError:
        raise PathRefused("MAP_PATH_CONFLICT", f"{out.name} appeared while the map was being "
                          "built; it was left untouched")
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        _copy_in_place(tmp, out, expect)
    try:
        os.unlink(tmp)
    except OSError:
        pass


def _copy_in_place(tmp: Path, out: Path, expect: tuple[int, int]) -> None:
    src = os.open(tmp, os.O_RDONLY | _NOFOLLOW)
    try:
        if _identity(os.fstat(src)) != expect:
            raise PathRefused("MAP_PATH_CONFLICT", "the map's temp file was replaced")
        try:
            dst = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        except FileExistsError:
            raise PathRefused("MAP_PATH_CONFLICT", f"{out.name} appeared while the map was "
                              "being built; it was left untouched")
        try:
            while True:
                chunk = os.read(src, _COPY_CHUNK)
                if not chunk:
                    break
                write_all(dst, chunk)
            os.fsync(dst)
        except BaseException:
            # Our own half-written file: remove it rather than leave a
            # partial map behind (only a killed run can leave one) -- if
            # it still is ours.
            mine = _identity(os.fstat(dst))
            os.close(dst)
            try:
                unlink_if(out, {mine})
            except OSError:
                pass
            raise
        os.close(dst)
    finally:
        os.close(src)


def write_all(fd: int, data: bytes | memoryview) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
