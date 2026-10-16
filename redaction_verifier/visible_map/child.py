"""redaction_verifier.visible_map.child — the map's child process.

Started by runner.py as `python -I -c BOOTSTRAP` with the target PDF on
an inherited, read-only descriptor and the header (rules, the scan's
truth, the --json report's content) on stdin. It holds everything in its
own memory and writes nothing to disk: its only output is one record
line on stdout,

    {"ok": true, "length": N, "map_sha256": ..., "target_sha256": ...}

followed by exactly N bytes of map — or {"ok": false, "code": ...}. The
parent trusts nothing else: not the exit status, not the bytes without
the record, not the record without the bytes' own hash. `target_sha256`
is the hash of what this process actually read, so a target changed
since the scan cannot be mapped as if it were the scanned one.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from typing import Any

_READ_CHUNK = 1 << 20


def _read_target(fd: int) -> tuple[memoryview, str]:
    """The whole target, read once into one buffer, and the hash of
    exactly what was read: a file that grew or shrank since it was
    sized hashes differently, so the parent refuses the map."""
    digest = hashlib.sha256()
    size = os.fstat(fd).st_size
    buffer = memoryview(bytearray(size))
    got = 0
    while got < size:
        n = os.preadv(fd, [buffer[got:got + _READ_CHUNK]], got) if hasattr(os, "preadv") else (
            _pread_into(fd, buffer[got:got + _READ_CHUNK], got))
        if n == 0:
            break
        digest.update(buffer[got:got + n])
        got += n
    digest.update(os.pread(fd, 1, got))       # anything past the size taken
    os.close(fd)
    return buffer[:got], digest.hexdigest()


def _pread_into(fd: int, view: memoryview, offset: int) -> int:
    chunk = os.pread(fd, len(view), offset)
    view[:len(chunk)] = chunk
    return len(chunk)


def _watchdog(timeout: float) -> None:
    """Exit once the parent is gone or the deadline it set has passed: a
    parent killed mid-stream must not leave this process running."""
    parent = os.getppid()
    deadline = time.monotonic() + timeout

    def watch() -> None:
        while True:
            if os.getppid() != parent or time.monotonic() > deadline:
                os._exit(1)
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def build_map(data: bytes | memoryview, target_sha256: str, header: dict[str, Any]) -> tuple[bytes, str]:
    """The checked map for the document *data*, and its headline (fixed
    text and warning codes only): render.build_map. Raises on any
    failure, render.SelfCheckFailed included."""
    import pymupdf as fitz

    from redaction_verifier.visible_map import render

    fitz.TOOLS.mupdf_display_errors(False)
    pdf, shown, _plan = render.build_map(data, target_sha256, header)
    return pdf, shown


def run(header: dict[str, Any]) -> None:
    """The child's entry point (BOOTSTRAP calls it). Never returns."""
    _watchdog(float(header.get("timeout", 600)))
    out = os.dup(1)
    # Anything else that writes to stdout goes nowhere instead of into the
    # record stream.
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    sys.stdout = sys.stderr
    try:
        data, target_sha256 = _read_target(header["fd"])
        try:
            pdf, shown = build_map(data, target_sha256, header)
        except Exception as exc:
            from redaction_verifier.visible_map import render
            code = ("MAP_SELF_CHECK_FAILED" if isinstance(exc, render.SelfCheckFailed)
                    else "MAP_CHILD_FAILED")
            record: dict[str, Any] = {"ok": False, "code": code, "target_sha256": target_sha256}
            _write_all(out, json.dumps(record).encode() + b"\n")
        else:
            record = {"ok": True, "length": len(pdf),
                      "map_sha256": hashlib.sha256(pdf).hexdigest(),
                      "target_sha256": target_sha256, "headline": shown}
            _write_all(out, json.dumps(record).encode() + b"\n")
            _write_all(out, pdf)
    finally:
        os._exit(0)
