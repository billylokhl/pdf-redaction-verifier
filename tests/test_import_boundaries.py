"""Regression guard for docs/REDESIGN.md Phase 2 ("Move"): once a name's
implementation moves out of ``verify.py`` into ``redaction_verifier``,
tests must import it from its new home directly instead of reaching it
through ``verify.py``'s compatibility re-export (``verify.<name>``). That
re-export exists for the CLI and for external users, not so the test
suite can stay coupled to it — the whole point of decoupling is that a
future removal of the re-export block (docs/REDESIGN.md §6, Phase 6)
does not have to touch any test.

The set of "moved names" is derived from ``verify.py`` itself (every name
it imports from ``redaction_verifier`` — the single guarded block
documented at its own top), not hard-coded here, so this check can never
drift out of date with what has actually moved. A name ``verify.py``
still defines on its own (``main``, ``run_cli``, ``scan_pdf_objects``,
``__version__``, ...) is never in that set, so referencing it as
``verify.<name>`` is unaffected.

Phase 3a adds guards for the process boundary (docs/REDESIGN.md §4): the
shipped CLI never loads the shadow-mode inventory (or pikepdf), and the
child-side code (the inventory, budget, ledger) never imports the parts
that hold secrets (rules, matching, report, views) or verify -- checked
both statically and by importing it in a fresh interpreter -- and never
imports dynamically at all: child-side code may import only an
allowlist (a few standard-library modules and the child-side package
itself), and any mention of __import__, exec, eval, compile,
__builtins__, __loader__ or the sys import machinery -- by name, as an
attribute or as a string -- is banned, not just a direct call, so
aliasing one (``f = exec``) or reaching it through getattr is caught too. This guards against mistakes
and unreviewed drift; deliberately obfuscated code in a pull request is
left to the mandatory review, as in eval/README.md's threat model.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

TESTS_DIR = Path(__file__).resolve().parent


def _moved_names(verify_source: str) -> frozenset[str]:
    """Names verify.py imports from redaction_verifier (its re-export
    block), minus any name verify.py also defines on its own — belt and
    braces against a false positive, though the two sets do not overlap
    today."""
    tree = ast.parse(verify_source, filename="verify.py")
    imported: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom) and node.module
                and (node.module == "redaction_verifier"
                     or node.module.startswith("redaction_verifier."))):
            for alias in node.names:
                imported.add(alias.asname or alias.name)

    own_defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            own_defined.add(node.name)
        elif isinstance(node, ast.Assign):
            own_defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            own_defined.add(node.target.id)

    return frozenset(imported - own_defined)


def _verify_dot_moved_refs(path: Path, moved: frozenset[str]) -> list[tuple[int, str]]:
    """Every ``verify.<name>`` ATTRIBUTE ACCESS in *path* where <name> is a
    moved name — never a string inside a string literal (a
    ``monkeypatch.setattr(verify, "name", ...)`` call, or prose in a
    docstring/comment), since those are not `ast.Attribute` nodes and are
    legitimate (a patch of verify.py's own module-level re-export, looked
    up by a caller that still lives in verify.py)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "verify"
                and node.attr in moved):
            hits.append((node.lineno, node.attr))
    return hits


class TestTestsDoNotReachMovedNamesThroughVerify:
    def test_no_verify_dot_moved_name_references(self) -> None:
        moved = _moved_names((REPO_ROOT / "verify.py").read_text())
        assert moved, "sanity: verify.py's re-export block must still exist"

        problems: list[str] = []
        for path in sorted(TESTS_DIR.rglob("*.py")):
            for lineno, attr in _verify_dot_moved_refs(path, moved):
                problems.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: verify.{attr} — "
                    f"{attr!r} moved into redaction_verifier; import it from "
                    "there directly instead of through verify.py's re-export"
                )
        assert not problems, "\n" + "\n".join(problems)

    def test_check_itself_does_not_false_positive_on_verifys_own_names(self) -> None:
        # verify.py defines plenty of its own names identically shaped to a
        # moved one (a function, a module-level constant): none of them may
        # ever appear in the moved set, or every test that legitimately
        # uses verify.<own-name> would start failing.
        source = (
            "import re\n"
            "SUBPROCESS_TIMEOUT_S = 120\n"
            "def scan_pdf_objects(): pass\n"
            "def main(): pass\n"
            "from redaction_verifier.model import ScanReport as ScanReport\n"
        )
        moved = _moved_names(source)
        assert moved == {"ScanReport"}


# ── Phase 3a: the process boundary ────────────────────────────────────────
CHILD_SIDE = sorted((REPO_ROOT / "redaction_verifier" / "inventory").rglob("*.py")) + [
    REPO_ROOT / "redaction_verifier" / name for name in ("budget.py", "ledger.py")]
SECRET_HOLDERS = tuple(f"redaction_verifier.{name}" for name in (
    "rules", "matching", "report", "views")) + ("verify",)
# What child-side code may import: an allowlist, not a ban list. The
# standard library has too many ways to run a string of code (timeit,
# code, cProfile, doctest, pickle, ctypes, ...) to ban them one by one.
# Add a module here only when child-side code needs it, after review.
ALLOWED_STDLIB = frozenset({"__future__", "bisect", "collections", "collections.abc",
                            "dataclasses", "enum", "typing", "re"})
ALLOWED_INTERNAL = ("redaction_verifier.inventory", "redaction_verifier.budget",
                    "redaction_verifier.ledger")
# Kept beside the allowlist for strings, which can name a module to a
# loader: modules that import or run code by name (imp exists on the 3.10
# floor; the underscored ones are the import system's internals).
BANNED_MODULES = SECRET_HOLDERS + (
    "importlib", "runpy", "pkgutil", "builtins", "imp", "zipimport", "_imp",
    "_frozen_importlib", "_frozen_importlib_external")
BANNED_NAMES = frozenset({"__import__", "exec", "eval", "compile", "__builtins__",
                          "__loader__", "import_module"})
# ...as attributes: all but compile, which re.compile shares, plus the
# import machinery reached through sys (sys.modules, sys.meta_path, ...).
BANNED_ATTRS = (BANNED_NAMES - {"compile"}) | {
    "modules", "meta_path", "path_hooks", "path_importer_cache",
    "load_module", "exec_module", "find_spec"}


def _names_banned_module(name: str) -> bool:
    return any(name == h or name.startswith((h + ".", h + ":")) for h in BANNED_MODULES)


def _allowed_import(module: str) -> bool:
    return module in ALLOWED_STDLIB or any(
        module == a or module.startswith(a + ".") for a in ALLOWED_INTERNAL)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _forbidden_imports(source: str, module: str, is_package: bool) -> list[tuple[int, str]]:
    """Every import in *source* (resolving relative ones against *module*)
    of a module not on the allowlist, plus any mention of a builtin that
    imports or runs code by name: a name, an attribute (other than
    ``.compile``) or a string constant equal to one; and any str or bytes
    constant naming a banned module (``sys.modules['runpy']``,
    ``'verify:main'``)."""
    package = module if is_package else module.rpartition(".")[0]
    hits = []
    for node in ast.walk(ast.parse(source)):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package.split(".")
            base = base[:len(base) - node.level + 1] if node.level else []
            root = ".".join(base + ([node.module] if node.module else []))
            # `from X import y` imports X; `from .. import y` imports each
            # submodule it names, so check those instead of the package.
            targets = [root] if node.module else [f"{root}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            hits.append((node.lineno, f"dynamic code via {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            hits.append((node.lineno, f"dynamic code via .{node.attr}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = (node.value.decode("latin-1") if isinstance(node.value, bytes)
                    else node.value)
            if text in BANNED_NAMES or _names_banned_module(text):
                hits.append((node.lineno, f"dynamic code via {text!r}"))
        for target in targets:
            if not _allowed_import(target):
                hits.append((getattr(node, "lineno", 0), target))
    return hits


class TestProcessBoundary:
    @staticmethod
    def _loaded_after(imports: str, *watched: str) -> list[str]:
        """The modules named by *watched* (or under them) that a fresh
        interpreter has loaded after ``import <imports>``."""
        probe = (f"import json, sys, {imports}; w = {list(watched)!r}; "
                 "print(json.dumps(sorted(m for m in sys.modules "
                 "if any(m == p or m.startswith(p + '.') for p in w))))")
        result = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        loaded: list[str] = json.loads(result.stdout.strip().splitlines()[-1])
        return loaded

    def test_cli_never_loads_the_inventory_or_pikepdf(self) -> None:
        watched = ("redaction_verifier.inventory", "pikepdf")
        assert self._loaded_after("verify", *watched) == []
        # ...and the probe does see the package when something loads it.
        assert "redaction_verifier.inventory" in self._loaded_after(
            "verify, redaction_verifier.inventory", *watched)

    def test_child_side_code_loads_no_secret_holder_or_pymupdf(self) -> None:
        watched = (*SECRET_HOLDERS, "fitz", "pymupdf")
        assert self._loaded_after(
            "redaction_verifier.inventory, redaction_verifier.budget", *watched) == []
        assert "pymupdf" in self._loaded_after("verify", *watched)  # the probe works

    def test_child_side_code_never_imports_a_secret_holder(self) -> None:
        assert len(CHILD_SIDE) >= 4, "sanity: the inventory package must exist"
        problems = [
            f"{path.relative_to(REPO_ROOT)}:{lineno}: {target}"
            for path in CHILD_SIDE
            for lineno, target in _forbidden_imports(
                path.read_text(), _module_name(path), path.name == "__init__.py")
        ]
        assert not problems, "\n" + "\n".join(problems)

    @pytest.mark.parametrize("source", [
        "import verify", "import redaction_verifier.rules", "from verify import main",
        "from redaction_verifier.matching.values import SecretMatcher",
        "from .. import report", "from ..views import text", "from ...redaction_verifier import rules",
        "import importlib; importlib.import_module('x')", "__import__('verify')",
        "import importlib.util", "from importlib import import_module", "import runpy",
        "exec('import verify')", "eval('1')", "compile('x', 'f', 'exec')",
        "import builtins; builtins.__import__('verify')",
        "from builtins import __import__ as imp", "builtins.exec('x')",
        "getattr(__builtins__, '__import__')('verify')", "__builtins__['exec']('x')",
        "f = exec", "import pkgutil; pkgutil.resolve_name('verify:main')",
        "getattr(object, '__import__')", "__loader__.load_module('verify')",
        "sys.modules['importlib'].import_module('verify')",
        "import imp; imp.load_source('verify', 'verify.py')", "import zipimport",
        "import _imp", "import _frozen_importlib; _frozen_importlib._gcd_import('verify')",
        "import _frozen_importlib_external", "sys.modules['runpy'].run_module('verify')",
        "sys.meta_path[-1].find_spec('verify').loader.load_module('verify')",
        "m = sys.modules", "x = 'verify:main'", "x = 'redaction_verifier.rules'",
        # Not on the allowlist: stdlib ways to run a string of code, and more.
        "import timeit; timeit.timeit('import verify', number=1)",
        "import code", "import cProfile", "import profile", "import trace", "import bdb",
        "import pdb", "import doctest", "import pickle", "import ctypes", "import os",
        "import subprocess", "import sys", "from sys import modules", "from os import system",
        "x = b'verify:main'", "from .. import rules", "from ..matching import values",
    ])
    def test_check_catches_each_form_of_forbidden_import(self, source: str) -> None:
        assert _forbidden_imports(source, "redaction_verifier.inventory.tiling", False)

    @pytest.mark.parametrize("source", [
        "from ..ledger import Span", "from . import types", "from .types import Region",
        "import re", "from redaction_verifier.budget import Budget", "re.compile('x')",
        "_WS = re.compile(rb'x')", "x = 'executable'", "from .. import ledger, budget",
        "from collections.abc import Iterator", "from __future__ import annotations",
        "from redaction_verifier.inventory.types import Region",
    ])
    def test_check_allows_child_side_imports(self, source: str) -> None:
        assert not _forbidden_imports(source, "redaction_verifier.inventory.tiling", False)
