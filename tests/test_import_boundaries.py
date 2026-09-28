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
"""

from __future__ import annotations

import ast
from pathlib import Path

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
