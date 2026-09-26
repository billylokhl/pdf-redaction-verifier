"""Ratchet checks: things this repository allows to only ever shrink (or,
for a red-team round's frozen label anchor, never move at all), verified
against the base of the current change rather than trusted from a single
commit's own bookkeeping.

Why this exists: a "may only shrink" allowlist (``cells.UNDOCUMENTED_GAPS``,
``cells.NONFITZ_PENDING``, ``model.GALLERY_FIELDS_PENDING``) and a
red-team round's frozen ``initial_labels_sha256`` (``eval/caselib/redteam/
<round>/round.json``) are each checked by ``tests/test_case_library.py``
for *internal* consistency — but every one of those checks reads only the
files in the commit being tested. Nothing stops a single commit from
editing the list (or the "frozen" anchor) and its own internal-consistency
check in lockstep: the test passes because it is comparing the new file
against itself. Catching that requires comparing against a *different*
commit — the merge-base with ``main``, or, on a direct push to ``main``
without a pull request, the immediately preceding commit. That is what
this script does; ``tests/test_case_library.py`` cannot do it because
pytest only ever sees one checkout.

    python eval/check_ratchets.py            # run against the resolved base
    python eval/check_ratchets.py --base sha # compare against an explicit ref

Known limitation (docs/REDESIGN.md's Phase 0b deferred items): comparing
against HEAD~1 on a direct push to main only catches a rewrite that lands
in a single commit. A multi-commit rewrite spread across several commits
each moving the anchor a little is not caught by this check alone — each
commit's HEAD~1 diff looks small. The merge-base comparison used for pull
requests does not have this gap (it compares the whole PR against main
in one shot), so the practical exposure is limited to direct pushes to
main, which branch protection should be discouraging anyway.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent

# (path from the repo root, the frozenset name in it) for every "may only
# shrink" list this check knows about. Add a new one here the same PR that
# adds it (tests/test_case_library.py's own internal-consistency check is
# necessary but not sufficient — see the module docstring).
RATCHET_SETS: tuple[tuple[str, str], ...] = (
    ("eval/caselib/cells.py", "UNDOCUMENTED_GAPS"),
    ("eval/caselib/cells.py", "NONFITZ_PENDING"),
    ("eval/caselib/model.py", "GALLERY_FIELDS_PENDING"),
)

REDTEAM_ROUND_GLOB = "eval/caselib/redteam/*/round.json"


# ── Trees: an old and a new view of the repo, git-backed or fake ────────

class Tree(Protocol):
    def read(self, path: str) -> str | None:
        """The text content of *path* (repo-root-relative, forward
        slashes) in this tree, or None if it does not exist there."""

    def glob(self, pattern: str) -> list[str]:
        """Every repo-root-relative path in this tree matching *pattern*
        (a single-level ``*`` glob, e.g. ``eval/caselib/redteam/*/round.json``),
        sorted."""


@dataclass(frozen=True)
class GitTree:
    """A tree as of *ref* in the git repository at *repo_root*."""

    ref: str
    repo_root: Path = REPO_ROOT

    def read(self, path: str) -> str | None:
        result = subprocess.run(
            ["git", "show", f"{self.ref}:{path}"],
            cwd=self.repo_root, capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else None

    def glob(self, pattern: str) -> list[str]:
        before, star, after = pattern.partition("*")
        if not star:
            return [pattern] if self.read(pattern) is not None else []
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", self.ref, "--", before.rsplit("/", 1)[0]],
            cwd=self.repo_root, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        return sorted(p for p in result.stdout.splitlines() if fnmatch.fnmatch(p, pattern))


@dataclass(frozen=True)
class DictTree:
    """An in-memory fake tree ({path: content}), for unit tests — no git,
    no filesystem."""

    files: Mapping[str, str]

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def glob(self, pattern: str) -> list[str]:
        return sorted(p for p in self.files if fnmatch.fnmatch(p, pattern))


# ── The checks themselves (pure functions of two Trees) ─────────────────

def check_redteam_anchors(old: Tree, new: Tree) -> list[str]:
    """A round's ``initial_labels_sha256`` must never move once it exists.
    A round that is new in *new* (absent from *old*) has nothing to
    compare against and is skipped — its anchor is only pinned from here
    on."""
    problems = []
    for path in new.glob(REDTEAM_ROUND_GLOB):
        new_text = new.read(path)
        old_text = old.read(path)
        if old_text is None or new_text is None:
            continue
        try:
            new_data, old_data = json.loads(new_text), json.loads(old_text)
        except json.JSONDecodeError as exc:
            problems.append(f"{path}: could not parse as JSON ({exc}) — treating as a failure")
            continue
        new_anchor = new_data.get("initial_labels_sha256")
        old_anchor = old_data.get("initial_labels_sha256")
        if new_anchor != old_anchor:
            problems.append(
                f"{path}: initial_labels_sha256 changed ({old_anchor!r} -> {new_anchor!r}) — "
                "this anchor is frozen forever once a round exists; if labels genuinely needed "
                "correcting, that belongs in labels_sha256 plus a redteam/adjudications.log "
                "entry, never here")
    return problems


class _NotAFrozensetLiteral(Exception):
    pass


def _frozenset_literal(source: str, name: str) -> set[str]:
    """The elements of ``NAME: frozenset[str] = frozenset({...})`` (or
    ``frozenset()``) as written at the top level of *source*. Raises if
    *name* is missing or is not written as a literal frozenset — this
    check only ever trusts what it can read statically, never an import
    (a ratchet's own module may not even be importable standalone, and
    executing untrusted historical revisions of it is not something to
    do lightly)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            target = names[0] if len(names) == 1 else None
        if target != name or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "frozenset":
            if not value.args:
                return set()
            try:
                literal = ast.literal_eval(value.args[0])
            except (ValueError, SyntaxError) as exc:
                raise _NotAFrozensetLiteral(
                    f"{name}'s frozenset(...) argument is not a literal ({exc})") from exc
            if not isinstance(literal, (set, frozenset, tuple, list)):
                raise _NotAFrozensetLiteral(
                    f"{name}'s frozenset(...) argument is not a set/tuple/list literal")
            return set(literal)
        raise _NotAFrozensetLiteral(f"{name} is not assigned a literal frozenset(...)")
    raise _NotAFrozensetLiteral(f"{name} not found")


def check_ratchet_set(old: Tree, new: Tree, path: str, name: str) -> list[str]:
    """*name* (a frozenset in *path*) may only shrink from *old* to *new*."""
    new_text = new.read(path)
    if new_text is None:
        return []  # file removed entirely — not this check's concern
    old_text = old.read(path)
    if old_text is None:
        return []  # file is new — nothing to have grown from
    try:
        new_set = _frozenset_literal(new_text, name)
    except _NotAFrozensetLiteral as exc:
        return [f"{path}:{name}: {exc} in the new tree — ratchet check cannot verify it"]
    try:
        old_set = _frozenset_literal(old_text, name)
    except _NotAFrozensetLiteral:
        return []  # didn't exist as a literal before (e.g. just introduced): nothing to shrink from
    grown = new_set - old_set
    if grown:
        return [f"{path}:{name}: grew by {sorted(grown)} — this list may only shrink"]
    return []


def run_all_checks(old: Tree, new: Tree) -> list[str]:
    problems = list(check_redteam_anchors(old, new))
    for path, name in RATCHET_SETS:
        problems += check_ratchet_set(old, new, path, name)
    return problems


# ── Resolving which base to compare HEAD against ─────────────────────────

def _run_git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def ref_exists(ref: str) -> bool:
    return _run_git("rev-parse", "--verify", "--quiet", ref) is not None


def merge_base(a: str, b: str) -> str | None:
    return _run_git("merge-base", a, b)


def rev_parse(ref: str) -> str | None:
    return _run_git("rev-parse", ref)


def determine_base_ref(env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """(ref, message): *ref* is what to diff HEAD against, or None to
    skip (with *message* explaining why either way). Three cases:

    - A pull request (or merge queue) build: ``GITHUB_BASE_REF`` names the
      target branch. Compare against the merge-base with
      ``origin/<that branch>`` — the whole PR is checked as one unit
      against where it will land, so a rewrite spread across several of
      the PR's own commits is still caught.
    - A direct push to ``main`` (no pull request): there is no PR base to
      compare against. This compares HEAD against HEAD~1 instead — it
      catches a rewrite landing in one commit, not one spread across
      several individually-pushed commits (see the module docstring's
      "Known limitation").
    - Anything else (a local run, another branch's push build): prefer
      the merge-base with ``origin/main`` when available, else HEAD~1,
      else skip outright (e.g. a single-commit repository).
    """
    env = os.environ if env is None else env
    base_branch = env.get("GITHUB_BASE_REF")
    if base_branch:
        remote = f"origin/{base_branch}"
        if not ref_exists(remote):
            return None, f"pull request base {remote!r} not found locally — skipping"
        mb = merge_base("HEAD", remote)
        if not mb:
            return None, f"no merge-base between HEAD and {remote} — skipping"
        return mb, f"pull request: comparing against the merge-base with {remote} ({mb})"

    if env.get("GITHUB_EVENT_NAME") == "push" and env.get("GITHUB_REF") == "refs/heads/main":
        if ref_exists("HEAD~1"):
            return "HEAD~1", ("push to main with no pull request: comparing HEAD against "
                              "HEAD~1 (single-commit rewrites only — see the module "
                              "docstring's known limitation)")
        return None, "push to main with no HEAD~1 (repository's first commit) — skipping"

    if ref_exists("origin/main"):
        mb = merge_base("HEAD", "origin/main")
        if mb and mb != rev_parse("HEAD"):
            return mb, f"local run: comparing against the merge-base with origin/main ({mb})"
    if ref_exists("HEAD~1"):
        return "HEAD~1", "local run: no usable origin/main, falling back to HEAD~1"
    return None, "no base ref available to compare against — skipping"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare against this ref instead of the resolved one")
    args = parser.parse_args(argv)

    base_ref, message = (args.base, f"comparing against explicit --base {args.base!r}") \
        if args.base else determine_base_ref()
    print(f"check_ratchets: {message}")
    if base_ref is None:
        return 0

    problems = run_all_checks(GitTree(base_ref), GitTree("HEAD"))
    if problems:
        print("check_ratchets: FAILED")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("check_ratchets: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
