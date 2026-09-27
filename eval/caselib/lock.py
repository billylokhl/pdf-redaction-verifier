"""The build lock: the SHA-256 of every case's PDF, so a change in what a
generator writes — an edit, or a PyMuPDF upgrade — is caught and
reviewed rather than silently shifting the corpus.

    python -m caselib.lock          # rewrite eval/caselib/cases.lock.json

Each case is built twice, a moment apart; a case whose two builds differ
(encryption salts, dates inside compressed streams) is recorded as null
and only its verdict is checked. Cases built by external tools (qpdf)
are left out: their bytes depend on the tool's version.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path

from . import REGISTRY, load
from .model import Case
from .run import build

LOCK = Path(__file__).resolve().parent / "cases.lock.json"


def lockable(case: Case) -> bool:
    # qpdf's output depends on the installed version; a perf case is built
    # from scratch every run specifically to be large and slow — locking it
    # would mean building it (at least) three times per lock refresh for a
    # hash nothing reviews byte-for-byte.
    return case.writer != "qpdf" and not case.perf


def digest(case: Case, workdir: Path) -> str:
    pdf = build(case, workdir / f"{case.id}.pdf")
    data = pdf.read_bytes()
    pdf.unlink()
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    load()
    cases = [c for _, c in sorted(REGISTRY.items()) if lockable(c)]
    with tempfile.TemporaryDirectory() as tmp:
        first = {c.id: digest(c, Path(tmp)) for c in cases}
        time.sleep(1.1)                      # a second apart: dates would differ
        second = {c.id: digest(c, Path(tmp)) for c in cases}
    lock = {cid: (h if second[cid] == h else None) for cid, h in first.items()}
    LOCK.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    unstable = sorted(cid for cid, h in lock.items() if h is None)
    print(f"{len(lock)} cases locked; {len(unstable)} not reproducible: {', '.join(unstable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
