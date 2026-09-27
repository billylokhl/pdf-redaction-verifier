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

Three things this deliberately does not trust:

- Which ratchets to check is read from the *base's* copy of this script
  (``check_registry``), not only from the running one, so a change
  cannot drop, rename or move a ratchet. Ratchets are never retired or
  renamed; adding a new one is always fine.
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

Threat model: this guards against accidental or unreviewed growth. It
does not defend against deliberately malicious code in the change under
review (edits to this script's logic, runtime code mutating what the
tests read); those are visible in the diff and left to the mandatory
review. See eval/README.md.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

# Package modules that re-export those names to their consumers (tests
# import them through ``caselib``). Each may only re-export a ratchet name
# unchanged from its defining module, never bind it any other way
# (``check_reexports``).
RATCHET_REEXPORTS: tuple[str, ...] = ("eval/caselib/__init__.py",)

REDTEAM_ROUND_GLOB = "eval/caselib/redteam/*/round.json"

# This script, as a path in a tree: the base's copy says which ratchets
# the base checked (``check_registry``), so dropping one here is caught.
CHECKER_PATH = "eval/check_ratchets.py"


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


class _RatchetNameMissing(_NotAFrozensetLiteral):
    pass


def _inert_constants(tree: ast.Module) -> set[int]:
    """ids of the string constants in *tree* that cannot bind anything: a
    bare expression statement (a docstring, anywhere) and the elements of
    a top-level ``__all__ = [...]`` literal, which only names exports."""
    inert: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            inert.add(id(node.value))
    for stmt in tree.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "__all__"
                and isinstance(stmt.value, (ast.List, ast.Tuple))):
            inert.update(id(element) for element in stmt.value.elts)
    return inert


def _binding_sites(tree: ast.Module, name: str, strings: bool = True) -> list[ast.AST]:
    """Every node anywhere in *tree* that could bind *name*, which is
    anything that mentions it other than reading it: a ``Name`` or
    attribute stored or deleted (plain, augmented, annotated, unpacked,
    ``for``/``with``/walrus targets, in any block, at any depth), ``global
    NAME``, ``def``/``class NAME``, an import binding it, ``except ... as
    NAME``, a ``match`` capture, a parameter or keyword argument named
    NAME (``globals().update(NAME=...)``), any star import, and — when
    *strings* is true — any string or bytes constant containing NAME
    (``globals()["NAME"]``, ``setattr(module, "NAME", ...)``, ``exec("NAME
    = ...")``) other than a docstring or an ``__all__`` entry.

    Deliberately over-inclusive: a mention this cannot tell is harmless
    (a local of the same name, say) still counts, since the ratchet only
    ever needs exactly one. What no static reading can see (a name built
    at runtime, ``exec`` of a computed string) is left to the runtime
    check in tests/test_case_library.py, which compares the value
    consumers actually import against the literal read here."""
    inert = _inert_constants(tree) if strings else set()
    encoded = name.encode()
    hits: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            continue  # reading NAME (or x.NAME) never rebinds it
        if isinstance(node, ast.alias) and node.name == "*":
            hits.append(node)  # a star import can bind any name at all
            continue
        if isinstance(node, ast.Constant):
            value = node.value
            if strings and id(node) not in inert and (
                    (isinstance(value, str) and name in value)
                    or (isinstance(value, bytes) and encoded in value)):
                hits.append(node)
            continue
        for _field, value in ast.iter_fields(node):
            values = value if isinstance(value, list) else [value]
            if any(isinstance(v, str) and name in v.split(".") for v in values):
                hits.append(node)
                break
    return hits


def _describe(node: ast.AST, name: str) -> str:
    """What *node* (one of ``_binding_sites``) is, for a failure message
    — so "2 possible writers" says which, and why each counts."""
    if isinstance(node, ast.Constant):
        what = f"a string constant mentioning {name}"
    elif isinstance(node, ast.alias):
        what = "a star import" if node.name == "*" else f"an import binding {name}"
    elif isinstance(node, (ast.Name, ast.Attribute)):
        what = f"a del of {name}" if isinstance(node.ctx, ast.Del) else f"an assignment to {name}"
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        what = f"def {name}"
    elif isinstance(node, ast.ClassDef):
        what = f"class {name}"
    elif isinstance(node, ast.Global):
        what = f"global {name}"
    elif isinstance(node, ast.Nonlocal):
        what = f"nonlocal {name}"
    elif isinstance(node, ast.ExceptHandler):
        what = f"except ... as {name}"
    elif isinstance(node, ast.arg):
        what = f"a parameter named {name}"
    elif isinstance(node, ast.keyword):
        what = f"a keyword argument {name}=..."
    elif isinstance(node, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
        what = f"a match pattern capturing {name}"
    else:
        what = f"a {type(node).__name__} naming {name}"
    return f"{what} at line {getattr(node, 'lineno', '?')}"


def _where(nodes: list[ast.AST], name: str) -> str:
    return "; ".join(_describe(n, name) for n in nodes)


def _frozenset_literal(source: str, name: str) -> set[str]:
    """The elements of ``NAME: frozenset[str] = frozenset({...})`` (or
    ``frozenset()``) in *source*. Fails closed (raises) unless that one
    top-level assignment is the *only* thing in the whole module that
    could bind NAME (``_binding_sites``) and nothing rebinds
    ``frozenset`` itself — any second writer, in any form or block,
    could grow the set Python actually binds while this reads the
    literal. It only ever reads source statically, never imports it (a
    ratchet's module may not be importable standalone, and executing
    historical revisions of it is not something to do lightly)."""
    tree = ast.parse(source)
    shadows = _binding_sites(tree, "frozenset", strings=False)
    if shadows:
        raise _NotAFrozensetLiteral(
            f"frozenset itself is rebound ({_where(shadows, 'frozenset')}), so {name}'s "
            "literal cannot be "
            "trusted")
    hits = _binding_sites(tree, name)
    if not hits:
        raise _RatchetNameMissing(f"{name} not found")
    if len(hits) > 1:
        raise _NotAFrozensetLiteral(
            f"{name} has {len(hits)} possible writers ({_where(hits, name)}) — this check requires "
            "exactly one, a top-level literal assignment, since any other could grow the "
            "ratchet unseen")
    (hit,) = hits
    stmt = next((s for s in tree.body if isinstance(hit, ast.Name) and (
                 (isinstance(s, ast.AnnAssign) and s.target is hit)
                 or (isinstance(s, ast.Assign) and s.targets == [hit]))), None)
    if stmt is None:
        raise _NotAFrozensetLiteral(
            f"{name}'s only writer ({_where(hits, name)}) is not a top-level "
            "NAME = frozenset(...) assignment")
    value = stmt.value
    if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "frozenset" \
            and not value.keywords and len(value.args) <= 1:
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


def check_ratchet_set(old: Tree, new: Tree, path: str, name: str,
                      in_old_registry: bool = False) -> list[str]:
    """*name* (a frozenset in *path*) may only shrink from *old* to *new*.
    A ratchet the old tree's own RATCHET_SETS already listed
    (*in_old_registry*) must be readable in the old tree too: if it is
    not there, it was renamed or moved, and "nothing to shrink from"
    would let the renamed set start over at any size."""
    new_text = new.read(path)
    if new_text is None:
        return [f"{path}:{name}: {path} is missing in the new tree — ratchet check cannot "
                "verify it"]
    old_text = old.read(path)
    if old_text is None:
        if in_old_registry:
            return [f"{path}:{name}: listed in the base's RATCHET_SETS, but {path} is missing "
                    "in the base — ratchet check cannot verify it"]
        return []  # file is new — nothing to have grown from
    try:
        new_set = _frozenset_literal(new_text, name)
    except _NotAFrozensetLiteral as exc:
        return [f"{path}:{name}: {exc} in the new tree — ratchet check cannot verify it"]
    except SyntaxError as exc:
        return [f"{path}:{name}: does not parse in the new tree ({exc}) — ratchet check "
                "cannot verify it"]
    try:
        old_set = _frozenset_literal(old_text, name)
    except (_NotAFrozensetLiteral, SyntaxError) as exc:
        if in_old_registry:
            return [f"{path}:{name}: listed in the base's RATCHET_SETS, but unreadable in the "
                    f"base ({exc}) — renamed or moved? Ratchet check cannot verify it"]
        return []  # a new ratchet (not in the base's registry): nothing to shrink from
    grown = new_set - old_set
    if grown:
        return [f"{path}:{name}: grew by {sorted(grown)} — this list may only shrink"]
    return []


def check_reexports(new: Tree, path: str,
                    sets: tuple[tuple[str, str], ...] = RATCHET_SETS) -> list[str]:
    """*path* (a package ``__init__.py`` that re-exports the ratchet
    sets for other importers — ``caselib`` does; the tests themselves
    read them via ``_verified(NAME)``, the literal this script checks)
    may bind a ratchet name only by re-exporting it unchanged from its
    defining module in the same package (``from .cells import
    UNDOCUMENTED_GAPS``). Anything else that could bind it there —
    assigning it, importing it from elsewhere or under another name's
    alias, a star import, a string naming it — fails closed, since
    ``check_ratchet_set`` never looks at this file and a rebind here
    would change the set every such importer sees."""
    text = new.read(path)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{path}: does not parse ({exc}) — ratchet re-exports cannot be verified"]
    here = PurePosixPath(path).parent
    problems = []
    for ratchet_path, name in sets:
        defining = PurePosixPath(ratchet_path)
        allowed: set[int] = set()
        if defining.parent == here:
            for stmt in tree.body:
                if (isinstance(stmt, ast.ImportFrom) and stmt.level == 1
                        and stmt.module == defining.stem):
                    allowed.update(id(a) for a in stmt.names
                                   if a.name == name and a.asname in (None, name))
        bad = [n for n in _binding_sites(tree, name) if id(n) not in allowed]
        if bad:
            problems.append(
                f"{path}:{name}: may only be re-exported unchanged (from .{defining.stem} "
                f"import {name}), but something else here could bind it ({_where(bad, name)}) — "
                "ratchet check cannot verify it")
    return problems


class _RegistryError(Exception):
    pass


def _declared(tree: Tree, var: str) -> tuple | None:
    """The literal value of top-level *var* in *tree*'s own copy of this
    script (``CHECKER_PATH``): None if that tree has no copy at all, ()
    if its copy predates *var*. Raises _RegistryError if the copy exists
    but *var* can't be read as a literal."""
    text = tree.read(CHECKER_PATH)
    if text is None:
        return None
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        raise _RegistryError(f"{CHECKER_PATH} does not parse ({exc})") from exc
    for stmt in module.body:
        target: ast.expr
        value: ast.expr | None
        if isinstance(stmt, ast.AnnAssign):
            target, value = stmt.target, stmt.value
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == var and value is not None:
            try:
                return tuple(ast.literal_eval(value))
            except (ValueError, TypeError, SyntaxError) as exc:
                raise _RegistryError(f"{CHECKER_PATH}: {var} is not a literal ({exc})") from exc
    return ()


def check_registry(old: Tree, sets: tuple[tuple[str, str], ...] = RATCHET_SETS,
                   reexports: tuple[str, ...] = RATCHET_REEXPORTS) -> list[str]:
    """Every ratchet the *old* tree's own copy of this script checked
    (its RATCHET_SETS and RATCHET_REEXPORTS, read from the base, never
    from the running script) must still be checked now. Without this, a
    pull request could stop a ratchet being checked — delete its
    RATCHET_SETS entry, or rename or move the set along with a matching
    entry — and grow it unseen. Ratchets are never retired or renamed,
    by design: an empty one stays (with its test) to guard the "no
    exceptions" state. Adding a new ratchet is always fine.

    A base with no copy of this script at all only passes if it predates
    the ratchets entirely — none of the files in *sets* exist there
    either. A base that has ratchet files but no checker fails closed:
    there is no registry to compare against."""
    try:
        old_sets = _declared(old, "RATCHET_SETS")
        old_reexports = _declared(old, "RATCHET_REEXPORTS")
    except _RegistryError as exc:
        return [f"base: {exc} — cannot tell which ratchets must still be checked"]
    if old_sets is None:
        present = sorted({path for path, _name in sets if old.read(path) is not None})
        if present:
            return [f"base: has ratchet file(s) {present} but no {CHECKER_PATH} — cannot "
                    "tell which ratchets must still be checked"]
        return []
    problems = []
    for entry in old_sets:
        if tuple(entry) not in sets:
            path, name = entry
            problems.append(
                f"ratchet ({path}, {name}) from the base registry is no longer checked; "
                "ratchets are never retired or renamed (see eval/README.md)")
    for path in old_reexports or ():
        if path not in reexports:
            problems.append(f"re-export module {path} from the base registry is no longer "
                            "checked; ratchets are never retired (see eval/README.md)")
    return problems


def run_all_checks(old: Tree, new: Tree, sets: tuple[tuple[str, str], ...] = RATCHET_SETS,
                   reexports: tuple[str, ...] = RATCHET_REEXPORTS) -> list[str]:
    problems = list(check_redteam_anchors(old, new))
    problems += check_registry(old, sets, reexports)
    try:
        old_sets = {tuple(entry) for entry in _declared(old, "RATCHET_SETS") or ()}
    except _RegistryError:
        old_sets = set()  # already reported by check_registry
    for path, name in sets:
        problems += check_ratchet_set(old, new, path, name,
                                      in_old_registry=(path, name) in old_sets)
    for path in reexports:
        problems += check_reexports(new, path, sets)
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
