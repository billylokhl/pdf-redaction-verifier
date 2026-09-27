"""Run the verifier from an arbitrary git ref, via a disposable worktree.

The scorecard's reference is a pinned git tag (`eval-ref-0`), checked out
into its own worktree — never the live code (docs/REDESIGN.md §5). The
candidate is simply the working tree's own `verify.py`, including any
uncommitted changes, so a PR sees its own diff reflected immediately.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

REFERENCE_REF = "eval-ref-0"


def repo_root(start: Path | None = None) -> Path:
    here = start or Path(__file__).resolve().parent
    out = subprocess.run(
        ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def resolve_commit(ref: str, cwd: Path) -> str:
    """The full commit hash *ref* names — worktrees and result caches key
    on this, not on a name that can move (a branch) or be re-tagged."""
    out = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@contextlib.contextmanager
def worktree_for_ref(ref: str, root: Path | None = None) -> Iterator[Path]:
    """Check *ref* out into a temporary worktree and yield its
    `verify.py`. The worktree (and its containing scratch directory) is
    removed on exit, whether or not the body raises."""
    root = root or repo_root()
    scratch = Path(tempfile.mkdtemp(prefix="scorecard-ref-"))
    wt_dir = scratch / "worktree"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(wt_dir), ref],
        capture_output=True,
        text=True,
        check=True,
    )
    try:
        yield wt_dir / "verify.py"
    finally:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(wt_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(scratch, ignore_errors=True)


def candidate_verify_path(root: Path | None = None) -> Path:
    """The working tree's own `verify.py` — the candidate under test."""
    return (root or repo_root()) / "verify.py"
