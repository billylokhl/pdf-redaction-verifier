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

Two things this deliberately does not trust:

- A round is matched between trees by round.json's own ``"round"`` field,
  never by path — a directory rename alone changes nothing, but a rename
  that also forges a fresh ``initial_labels_sha256`` is still caught,
  because the old anchor is looked up by identity. A round whose identity
  vanishes entirely between the two trees (deleted, or its identity field
  itself changed — indistinguishable from delete-and-recreate) is always
  a failure (``check_redteam_anchors``).
- When no base ref can be resolved, that is only a clean skip in what
  looks like a genuinely local run (``is_ci_context``); inside anything
  that looks like CI, it is a failure — an unresolvable base must never
  quietly turn into "nothing to check".
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

class _RedteamDataError(Exception):
    """A round's data could not be read at all — parsed as JSON, or
    identified — in one of the two trees. Always a hard failure: a
    ratchet check that can't make sense of a round must never treat that
    as "nothing to compare", since that is exactly the gap a rename or a
    malformed commit could hide behind."""


def _round_identities(tree: Tree) -> dict[str, tuple[str, dict]]:
    """{round identity: (path, parsed round.json)} for every round this
    tree has, keyed by round.json's own ``"round"`` field — never by
    path. A directory rename alone does not change a round's identity, so
    it is not by itself a difference this returns; renaming *and* forging
    a fresh ``initial_labels_sha256`` still leaves the identity mapped to
    the new (wrong) anchor, which check_redteam_anchors below compares
    against the old tree's anchor for that same identity."""
    identities: dict[str, tuple[str, dict]] = {}
    for path in tree.glob(REDTEAM_ROUND_GLOB):
        text = tree.read(path)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _RedteamDataError(f"{path}: could not parse as JSON ({exc})") from exc
        identity = data.get("round")
        if not identity:
            raise _RedteamDataError(f'{path}: round.json has no non-empty "round" field')
        if identity in identities:
            raise _RedteamDataError(
                f"{path}: round identity {identity!r} is also used by "
                f"{identities[identity][0]} — round identities must be unique")
        identities[identity] = (path, data)
    return identities


def check_redteam_anchors(old: Tree, new: Tree) -> list[str]:
    """A round's ``initial_labels_sha256`` must never move once it exists,
    and a round may not simply disappear — by path *or* by identity — to
    dodge that comparison. Rounds are matched by round.json's own
    ``"round"`` field, not by path: renaming a round's directory without
    touching its identity or its anchor is not a failure (nothing here
    changed), but renaming it *and* rewriting the anchor is still caught,
    because the old anchor is looked up by identity, not by the path that
    moved. A round whose identity existed in the old tree but is gone
    from the new one — removed outright, or its identity itself changed,
    which is indistinguishable from deleting the old one and creating an
    unrelated new one — is always a failure: an existing round's history
    is never allowed to simply vanish. A round whose identity did not
    exist in the old tree at all is a genuinely new round and has nothing
    to compare against."""
    try:
        old_ids = _round_identities(old)
    except _RedteamDataError as exc:
        return [f"redteam (old tree): {exc}"]
    try:
        new_ids = _round_identities(new)
    except _RedteamDataError as exc:
        return [f"redteam (new tree): {exc}"]

    problems = []
    for identity, (old_path, old_data) in old_ids.items():
        if identity not in new_ids:
            problems.append(
                f"redteam round {identity!r} ({old_path}) is missing from the new tree — "
                "an existing round may not be removed or renamed away; its history must "
                "stay reachable under its own identity")
            continue
        new_path, new_data = new_ids[identity]
        old_anchor = old_data.get("initial_labels_sha256")
        new_anchor = new_data.get("initial_labels_sha256")
        if old_anchor != new_anchor:
            where = old_path if old_path == new_path else f"{old_path} -> {new_path}"
            problems.append(
                f"redteam round {identity!r} ({where}): initial_labels_sha256 changed "
                f"({old_anchor!r} -> {new_anchor!r}) — this anchor is frozen forever once a "
                "round exists; if labels genuinely needed correcting, that belongs in "
                "labels_sha256 plus a redteam/adjudications.log entry, never here")
    return problems


class _NotAFrozensetLiteral(Exception):
    pass


def _module_level_writer(node: ast.stmt, name: str) -> bool:
    """True if top-level statement *node* writes to *name* in any form
    this check can detect: a plain ``NAME = ...``/``NAME: T = ...``, an
    augmented ``NAME |= ...``, or an indirect rebind via
    ``globals()["NAME"] = ...``. Used only to COUNT competing writers to
    *name* — a second writer anywhere below the first is exactly how a
    ratchet set can be silently grown: ``ast.walk``'s "first match wins"
    reads the never-changing initial value while Python itself binds the
    *last* assignment, so a later ``UNDOCUMENTED_GAPS = frozenset({...,
    "sneaky.new.gap"})`` was invisible to this check even though it is
    exactly what ends up imported. Rather than trust whichever single
    assignment happens to look like a literal, this now fails closed the
    moment there is more than one writer at all."""
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    if isinstance(node, ast.AugAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return True
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Call)
                    and getattr(target.value.func, "id", None) == "globals"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == name):
                return True
        return False
    return False


def _frozenset_literal(source: str, name: str) -> set[str]:
    """The elements of ``NAME: frozenset[str] = frozenset({...})`` (or
    ``frozenset()``) as written at the top level of *source*. Raises if
    *name* is missing, is written to by more than one top-level statement
    (including an augmented ``|=`` or an indirect ``globals()[...]``
    rebind — see ``_module_level_writer``), or is not itself written as a
    literal frozenset — this check only ever trusts what it can read
    statically, never an import (a ratchet's own module may not even be
    importable standalone, and executing untrusted historical revisions
    of it is not something to do lightly)."""
    tree = ast.parse(source)
    writers = [node for node in tree.body if _module_level_writer(node, name)]
    if len(writers) > 1:
        raise _NotAFrozensetLiteral(
            f"{name} is written to by {len(writers)} top-level statements — this check "
            "requires exactly one, unambiguous literal assignment, since any later "
            "reassignment would silently grow the ratchet unseen")
    if not writers:
        raise _NotAFrozensetLiteral(f"{name} not found")
    node = writers[0]
    if isinstance(node, ast.AugAssign):
        raise _NotAFrozensetLiteral(
            f"{name} is assigned via augmented assignment (e.g. |=), not a literal "
            "NAME = frozenset(...) assignment")
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        raise _NotAFrozensetLiteral(  # pragma: no cover — _module_level_writer's own contract
            f"{name}: unrecognized top-level assignment form")
    if isinstance(node, ast.Assign) and not any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets):
        raise _NotAFrozensetLiteral(
            f"{name} is rebound indirectly (e.g. via globals()[...]), not a literal "
            "NAME = frozenset(...) assignment")
    value = node.value
    if value is None:
        raise _NotAFrozensetLiteral(f"{name} is not assigned a literal frozenset(...)")
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


def is_ci_context(env: Mapping[str, str]) -> bool:
    """True if this run looks like it's happening in CI: GitHub Actions
    sets ``GITHUB_ACTIONS=true`` on every job, and ``GITHUB_BASE_REF`` /
    ``GITHUB_EVENT_NAME`` being set at all are further, independent
    signals (in case some other CI system sets those two but not the
    first). Only a run that looks purely local may skip cleanly when no
    base is resolvable — in anything that looks like CI, "no base to
    compare against" must fail the job, not silently pass it, since that
    silence is exactly what would let a ratchet move undetected."""
    return (env.get("GITHUB_ACTIONS") == "true" or bool(env.get("GITHUB_BASE_REF"))
            or bool(env.get("GITHUB_EVENT_NAME")))


def determine_base_ref(env: Mapping[str, str] | None = None) -> tuple[str | None, str, bool]:
    """(ref, message, fatal_if_none): *ref* is what to diff HEAD against,
    or None if nothing could be resolved. When *ref* is None,
    *fatal_if_none* says whether the caller must treat that as a failure
    (anything that looks like CI — see ``is_ci_context``) or may skip
    cleanly (a genuinely local run). Three cases:

    - A pull request (or merge queue) build: ``GITHUB_BASE_REF`` names the
      target branch. Compare against the merge-base with
      ``origin/<that branch>`` — the whole PR is checked as one unit
      against where it will land, so a rewrite spread across several of
      the PR's own commits is still caught. ``GITHUB_BASE_REF`` being set
      at all means this is always a CI context, so failing to resolve it
      is always fatal here.
    - A direct push to ``main`` (no pull request): there is no PR base to
      compare against. This compares HEAD against HEAD~1 instead — it
      catches a rewrite landing in one commit, not one spread across
      several individually-pushed commits (see the module docstring's
      "Known limitation"). ``GITHUB_EVENT_NAME`` being set makes this a
      CI context too, so no ``HEAD~1`` to compare against is fatal.
    - Anything else (a local run, another branch's push build): prefer
      the merge-base with ``origin/main`` when available, else HEAD~1,
      else give up — fatal only if this still looks like CI.
    """
    env = os.environ if env is None else env
    ci = is_ci_context(env)

    base_branch = env.get("GITHUB_BASE_REF")
    if base_branch:
        remote = f"origin/{base_branch}"
        if not ref_exists(remote):
            return None, f"pull request base {remote!r} not found locally", True
        mb = merge_base("HEAD", remote)
        if not mb:
            return None, f"no merge-base between HEAD and {remote}", True
        return mb, f"pull request: comparing against the merge-base with {remote} ({mb})", False

    if env.get("GITHUB_EVENT_NAME") == "push" and env.get("GITHUB_REF") == "refs/heads/main":
        if ref_exists("HEAD~1"):
            return "HEAD~1", ("push to main with no pull request: comparing HEAD against "
                              "HEAD~1 (single-commit rewrites only — see the module "
                              "docstring's known limitation)"), False
        return None, "push to main with no HEAD~1 (repository's first commit)", True

    if ref_exists("origin/main"):
        mb = merge_base("HEAD", "origin/main")
        if mb and mb != rev_parse("HEAD"):
            return mb, f"local run: comparing against the merge-base with origin/main ({mb})", False
    if ref_exists("HEAD~1"):
        return "HEAD~1", "local run: no usable origin/main, falling back to HEAD~1", False
    return None, "no base ref available to compare against", ci


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare against this ref instead of the resolved one")
    args = parser.parse_args(argv)

    if args.base:
        base_ref: str | None = args.base
        message, fatal = f"comparing against explicit --base {args.base!r}", False
    else:
        base_ref, message, fatal = determine_base_ref()

    if base_ref is None:
        if fatal:
            print(f"check_ratchets: ERROR: {message} — this looks like CI, so refusing to "
                  "silently pass rather than skip the ratchet check.")
            return 1
        print(f"check_ratchets: {message} — skipping (not running in CI).")
        return 0

    print(f"check_ratchets: {message}")
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
