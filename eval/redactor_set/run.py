"""Run the redactor on a generated set: method 1 (the tool as-is) and
method 2 (method 1's output re-saved with garbage collection).

The tool is never named or located in code. Two environment variables
say how to run it:

- ``REDACTOR_CMD``: the command, as the owner runs it, before the
  input path (split with shlex). The tool is called exactly as
  ``$REDACTOR_CMD <input.pdf> --config <yaml>`` and writes
  ``<input stem>_redacted.pdf`` beside its input.
- ``REDACTOR_PYTHON``: the Python of the tool's own environment, used
  for method 2's re-save, so the re-save uses the same PyMuPDF the tool
  saves with.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

GC_SCRIPT = ("import sys, pymupdf; d = pymupdf.open(sys.argv[1]); "
             "d.save(sys.argv[2], garbage=4, deflate=True); print(pymupdf.VersionBind)")


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"set {name} (see eval/redactor_set/run.py)")
    return value


def _run_group(cmd: list[str], cwd: Path, timeout: float) -> tuple[int | str, str, str]:
    """Run *cmd* in its own process group; on timeout end the whole group
    (a wrapper such as ``uv run`` would otherwise leave the tool running
    and still writing) and return ``"timeout"`` with what it had printed."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                out, err = proc.communicate(timeout=10)
                break
            except ProcessLookupError:
                out, err = proc.communicate()
                break
            except subprocess.TimeoutExpired:
                out, err = "", ""
                continue
        return "timeout", out or "", err or ""


def run_set(root: Path, set_name: str, config: Path, *, timeout: float = 300.0) -> dict[str, object]:
    """Run both methods on every ``<root>/<set_name>/*.pdf``; outputs go
    under ``<root>/work/<set_name>/m1`` and ``m2``. Returns (and writes)
    the run record."""
    cmd = shlex.split(_env("REDACTOR_CMD"))
    gc_python = _env("REDACTOR_PYTHON")
    src_dir = root / set_name
    m1_dir, m2_dir = root / "work" / set_name / "m1", root / "work" / set_name / "m2"
    m2_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {"set": set_name, "config": str(config.relative_to(root)),
                                 "redactor_version": os.environ.get("REDACTOR_VERSION",
                                                                    "unrecorded"),
                                 "documents": {}}
    docs: dict[str, object] = {}
    gc_pymupdf = None
    for pdf in sorted(src_dir.glob("*.pdf")):
        stem = pdf.stem
        doc_dir = m1_dir / stem
        if doc_dir.exists():
            shutil.rmtree(doc_dir)
        doc_dir.mkdir(parents=True)
        inp = doc_dir / pdf.name
        shutil.copyfile(pdf, inp)
        m1, m2 = m1_dir / f"{stem}.pdf", m2_dir / f"{stem}.pdf"
        for stale in (m1, m2):            # never let an older run's output stand in
            stale.unlink(missing_ok=True)
        started = time.monotonic()
        entry: dict[str, object] = {"exit": None, "seconds": None, "m1": None, "m2": None}
        code, stdout, stderr = _run_group([*cmd, str(inp), "--config", str(config)],
                                          doc_dir, timeout)
        entry["exit"] = code
        (doc_dir / "stdout.txt").write_text(stdout)
        (doc_dir / "stderr.txt").write_text(stderr)
        entry["seconds"] = round(time.monotonic() - started, 2)
        out = doc_dir / f"{stem}_redacted.pdf"
        if entry["exit"] == 0 and out.exists():
            shutil.copyfile(out, m1)
            entry["m1"] = str(m1.relative_to(root))
            try:
                gc = subprocess.run([gc_python, "-I", "-c", GC_SCRIPT, str(m1), str(m2)],
                                    capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                entry["m2_error"] = "timeout"
            else:
                if gc.returncode == 0:
                    entry["m2"] = str(m2.relative_to(root))
                    gc_pymupdf = gc.stdout.strip()
                else:
                    entry["m2_error"] = gc.stderr.strip()[-500:]
        docs[stem] = entry
        print(f"{stem}: exit {entry['exit']}, {entry['seconds']}s, "
              f"m1 {'ok' if entry['m1'] else 'MISSING'}, m2 {'ok' if entry['m2'] else 'MISSING'}")
    record["documents"] = docs
    record["gc_pymupdf"] = gc_pymupdf
    (root / "work" / set_name / "run.json").write_text(json.dumps(record, indent=2) + "\n")
    return record
