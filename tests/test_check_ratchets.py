"""check_ratchets.py's own checks, fed two fake in-memory trees — no git,
no filesystem — so the actual comparison logic is verified in isolation
from CI's git-history plumbing (which tests/test_case_library.py already
covers from a single checkout's point of view)."""

from __future__ import annotations

import pytest

from check_ratchets import (CHECKER_PATH, RATCHET_REEXPORTS, DictTree, check_ratchet_set,
                            check_redteam_anchors, check_registry, check_reexports,
                            determine_base_ref, run_all_checks)

CELLS_TEMPLATE = """
UNDOCUMENTED_GAPS: frozenset[str] = frozenset({{
{gaps}
}})
NONFITZ_PENDING: frozenset[str] = frozenset()
"""


def _cells_source(gaps: tuple[str, ...]) -> str:
    body = "\n".join(f'    "{g}",' for g in gaps)
    return CELLS_TEMPLATE.format(gaps=body)


# ── check_ratchet_set: frozenset may-only-shrink ─────────────────────────

class TestCheckRatchetSet:
    def test_shrinking_is_fine(self) -> None:
        old = DictTree({"cells.py": _cells_source(("a.gap", "b.gap"))})
        new = DictTree({"cells.py": _cells_source(("a.gap",))})
        assert check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS") == []

    def test_unchanged_is_fine(self) -> None:
        old = DictTree({"cells.py": _cells_source(("a.gap", "b.gap"))})
        new = DictTree({"cells.py": _cells_source(("a.gap", "b.gap"))})
        assert check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS") == []

    def test_growing_fails(self) -> None:
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": _cells_source(("a.gap", "b.gap"))})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert len(problems) == 1
        assert "b.gap" in problems[0]
        assert "may only shrink" in problems[0]

    def test_swap_same_size_is_still_growth(self) -> None:
        """Same length, different member: still growth of the new element,
        even though the shrink of the old one happened at the same time."""
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": _cells_source(("c.gap",))})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert len(problems) == 1
        assert "c.gap" in problems[0]

    def test_file_missing_in_old_tree_is_not_a_failure(self) -> None:
        # A brand new file: nothing to have grown from.
        new = DictTree({"cells.py": _cells_source(("a.gap",))})
        old = DictTree({})
        assert check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS") == []

    def test_file_missing_in_new_tree_fails_closed(self) -> None:
        # Still listed in RATCHET_SETS but its file is gone: nothing left
        # to verify, which must never read as "fine".
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0]

    def test_empty_frozenset_literal_parses(self) -> None:
        old = DictTree({"model.py": "GALLERY_FIELDS_PENDING: frozenset[str] = frozenset()\n"})
        new = DictTree({"model.py": 'GALLERY_FIELDS_PENDING: frozenset[str] = frozenset({"x"})\n'})
        problems = check_ratchet_set(old, new, "model.py", "GALLERY_FIELDS_PENDING")
        assert len(problems) == 1
        assert "x" in problems[0]

    def test_non_literal_assignment_is_reported_not_silently_skipped(self) -> None:
        """If a future refactor computes the set instead of writing a
        literal, the check must fail loudly (fail-closed) rather than
        pass by accident."""
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": "UNDOCUMENTED_GAPS = frozenset(some_function())\n"})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0]

    def test_newly_introduced_non_literal_in_old_tree_does_not_fail(self) -> None:
        # The name didn't exist as a literal in the old tree at all (e.g.
        # it's brand new in this diff) — nothing to compare against.
        old = DictTree({"cells.py": "SOMETHING_ELSE = 1\n"})
        new = DictTree({"cells.py": _cells_source(("a.gap",))})
        assert check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS") == []

    # ── reviewer's confirmed bypass: ast.walk (and Python's own name       ──
    # ── binding rules) means a SECOND top-level assignment to the same     ──
    # ── name silently wins at runtime while the old check kept reading the ──
    # ── first, never-changing one. Fail closed instead. ────────────────────

    def test_later_reassignment_growing_the_set_is_caught(self) -> None:
        """The reviewer's exact reproduction: a small, innocent-looking
        frozenset up top, then a second assignment further down that
        actually grows it. Python binds the second one; the check must
        never trust the first."""
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": (
            'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap"})\n'
            '\n'
            '# ... a lot of unrelated code later ...\n'
            'UNDOCUMENTED_GAPS = frozenset({"a.gap", "sneaky.new.gap"})\n'
        )})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems, "a later reassignment that grows the set must be caught, not ignored"
        assert "cannot verify" in problems[0]

    def test_two_top_level_assignments_fail_even_when_the_second_shrinks(self) -> None:
        # Not just "the effective value grew" — ANY second top-level
        # writer is inherently ambiguous (which one does a reader trust?)
        # and must fail closed, even if this particular second assignment
        # happens to look smaller.
        old = DictTree({"cells.py": _cells_source(("a.gap", "b.gap"))})
        new = DictTree({"cells.py": (
            'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap", "b.gap"})\n'
            'UNDOCUMENTED_GAPS = frozenset({"a.gap"})\n'
        )})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0]

    def test_augmented_assignment_is_rejected(self) -> None:
        """``NAME |= {...}`` is a rebinding this check must never treat as
        a harmless no-op literal read."""
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": (
            'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap"})\n'
            'UNDOCUMENTED_GAPS |= frozenset({"sneaky.new.gap"})\n'
        )})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0]

    def test_globals_rebind_is_rejected(self) -> None:
        """An indirect rebind via ``globals()[...]`` — not a plain ``NAME
        = ...`` the naive AST-target check would even recognise as
        touching *name* at all — must still be caught as a second writer."""
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": (
            'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap"})\n'
            'globals()["UNDOCUMENTED_GAPS"] = frozenset({"a.gap", "sneaky.new.gap"})\n'
        )})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0]

    def test_single_assignment_still_works_exactly_as_before(self) -> None:
        # Regression guard: the fix must not make the ordinary, single-
        # assignment case (the only shape every real ratchet set uses
        # today) any stricter than it needs to be.
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({"cells.py": _cells_source(("a.gap", "b.gap"))})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert len(problems) == 1
        assert "b.gap" in problems[0]


# ── the whole module, not just its top level: every way the reviewer   ──
# ── found to rebind a ratchet name besides a second top-level statement ──
# ── (24 of 25 attacks passed the top-level-only scan). Each must fail   ──
# ── closed. The literal assignment itself stays exactly as it was.       ──

_LITERAL = 'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap"})\n'
_GROWN = 'frozenset({"a.gap", "sneaky.gap"})'

_REBIND_ATTACKS: dict[str, str] = {
    "if-block": f"if True:\n    UNDOCUMENTED_GAPS = {_GROWN}\n",
    "else-block": f"if False:\n    pass\nelse:\n    UNDOCUMENTED_GAPS = {_GROWN}\n",
    "try-block": f"try:\n    UNDOCUMENTED_GAPS = {_GROWN}\nexcept Exception:\n    pass\n",
    "with-block": (f"import contextlib\nwith contextlib.nullcontext():\n"
                   f"    UNDOCUMENTED_GAPS = {_GROWN}\n"),
    "while-block": f"while True:\n    UNDOCUMENTED_GAPS = {_GROWN}\n    break\n",
    "tuple-unpacking": f"UNDOCUMENTED_GAPS, _other = {_GROWN}, 1\n",
    "starred-unpacking": f"_first, *UNDOCUMENTED_GAPS = [1, {_GROWN}]\n",
    "chained-assignment": f"_other = UNDOCUMENTED_GAPS = {_GROWN}\n",
    "for-target": f"for UNDOCUMENTED_GAPS in [{_GROWN}]:\n    pass\n",
    "walrus": f"(UNDOCUMENTED_GAPS := {_GROWN})\n",
    "augmented": 'UNDOCUMENTED_GAPS |= frozenset({"sneaky.gap"})\n',
    "annotated-reassignment": f"UNDOCUMENTED_GAPS: frozenset[str] = {_GROWN}\n",
    "del": "del UNDOCUMENTED_GAPS\n",
    "global-in-def-called-at-import": (
        f"def _grow():\n    global UNDOCUMENTED_GAPS\n    UNDOCUMENTED_GAPS = {_GROWN}\n_grow()\n"),
    "globals-subscript": f'globals()["UNDOCUMENTED_GAPS"] = {_GROWN}\n',
    "vars-subscript": f'vars()["UNDOCUMENTED_GAPS"] = {_GROWN}\n',
    "locals-subscript": f'locals()["UNDOCUMENTED_GAPS"] = {_GROWN}\n',
    "globals-update-keyword": f"globals().update(UNDOCUMENTED_GAPS={_GROWN})\n",
    "globals-update-dict": f'globals().update({{"UNDOCUMENTED_GAPS": {_GROWN}}})\n',
    "setattr-sys-modules": (
        f'import sys\nsetattr(sys.modules[__name__], "UNDOCUMENTED_GAPS", {_GROWN})\n'),
    "attribute-store-sys-modules": (
        f"import sys\nsys.modules[__name__].UNDOCUMENTED_GAPS = {_GROWN}\n"),
    "exec": 'exec("UNDOCUMENTED_GAPS = frozenset({\'a.gap\', \'sneaky.gap\'})")\n',
    "exec-bytes": 'exec(b"UNDOCUMENTED_GAPS = frozenset({\'a.gap\', \'sneaky.gap\'})")\n',
    "import-alias": "import json as UNDOCUMENTED_GAPS\n",
    "from-import-alias": "from os import sep as UNDOCUMENTED_GAPS\n",
    "from-import-same-name": "from sneaky_module import UNDOCUMENTED_GAPS\n",
    "star-import": "from sneaky_module import *\n",
    "with-as": (f"import contextlib\nwith contextlib.nullcontext({_GROWN}) as UNDOCUMENTED_GAPS:\n"
                "    pass\n"),
    "except-as": "try:\n    raise ValueError\nexcept ValueError as UNDOCUMENTED_GAPS:\n    pass\n",
    "match-capture": f"match {_GROWN}:\n    case UNDOCUMENTED_GAPS:\n        pass\n",
    "match-as": f"match {_GROWN}:\n    case frozenset() as UNDOCUMENTED_GAPS:\n        pass\n",
    "match-star": "match [1]:\n    case [*UNDOCUMENTED_GAPS]:\n        pass\n",
    "match-mapping-rest": "match {}:\n    case {**UNDOCUMENTED_GAPS}:\n        pass\n",
    "class-body": f"class _Holder:\n    UNDOCUMENTED_GAPS = {_GROWN}\n",
    "class-named-it": "class UNDOCUMENTED_GAPS:\n    pass\n",
    "def-named-it": "def UNDOCUMENTED_GAPS():\n    pass\n",
    "async-def-named-it": "async def UNDOCUMENTED_GAPS():\n    pass\n",
    "parameter-named-it": "def _f(UNDOCUMENTED_GAPS=None):\n    pass\n",
}

# Rebinding frozenset itself makes the literal assignment compute
# something else entirely; these go BEFORE the literal.
_SHADOW_ATTACKS: dict[str, str] = {
    "frozenset-assigned": 'frozenset = lambda items: {*items, "sneaky.gap"}\n',
    "frozenset-def": 'def frozenset(items):\n    return {*items, "sneaky.gap"}\n',
    "frozenset-imported": "from sneaky_module import frozenset\n",
}


class TestRebindAnywhereFailsClosed:
    @pytest.mark.parametrize("attack", sorted(_REBIND_ATTACKS))
    def test_rebind_after_the_literal_is_caught(self, attack: str) -> None:
        old = DictTree({"cells.py": _LITERAL})
        new = DictTree({"cells.py": _LITERAL + _REBIND_ATTACKS[attack]})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0], attack

    @pytest.mark.parametrize("attack", sorted(_REBIND_ATTACKS))
    def test_rebind_alone_is_not_accepted_as_the_literal(self, attack: str) -> None:
        # With the literal gone, the attack is the only writer: either not
        # a top-level NAME = frozenset(...) literal at all (not trusted), or
        # one that is, and then it grew.
        old = DictTree({"cells.py": _LITERAL})
        new = DictTree({"cells.py": _REBIND_ATTACKS[attack]})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems, attack
        assert "cannot verify" in problems[0] or "sneaky.gap" in problems[0], attack

    @pytest.mark.parametrize("attack", sorted(_SHADOW_ATTACKS))
    def test_shadowing_frozenset_is_caught(self, attack: str) -> None:
        old = DictTree({"cells.py": _LITERAL})
        new = DictTree({"cells.py": _SHADOW_ATTACKS[attack] + _LITERAL})
        problems = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert problems and "cannot verify" in problems[0], attack

    def test_reading_the_name_and_mentioning_it_in_docs_is_fine(self) -> None:
        # Control: loads, docstrings and a same-prefix name are not writers.
        source = ('"""Mentions UNDOCUMENTED_GAPS in the docstring."""\n' + _LITERAL
                  + "UNDOCUMENTED_GAPS_COUNT = len(UNDOCUMENTED_GAPS)\n"
                  "def _f():\n    \"\"\"UNDOCUMENTED_GAPS again.\"\"\"\n"
                  "    return sorted(UNDOCUMENTED_GAPS)\n")
        old = DictTree({"cells.py": _LITERAL})
        new = DictTree({"cells.py": source})
        assert check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS") == []

    def test_the_real_ratchet_modules_pass(self) -> None:
        from check_ratchets import REPO_ROOT, RATCHET_SETS, _frozenset_literal
        for path, name in RATCHET_SETS:
            _frozenset_literal((REPO_ROOT / path).read_text(), name)


# ── the package __init__ consumers import the sets through: re-export ──
# ── only, never a rebind (no ratchet looked at this file before).      ──

_INIT = "eval/caselib/__init__.py"
_INIT_OK = ('"""The case library."""\n'
            "from .cells import CELLS, NONFITZ_PENDING, UNDOCUMENTED_GAPS\n"
            "from .model import GALLERY_FIELDS_PENDING, REGISTRY\n"
            '__all__ = ["CELLS", "GALLERY_FIELDS_PENDING", "NONFITZ_PENDING", "REGISTRY",\n'
            '           "UNDOCUMENTED_GAPS"]\n')

_INIT_ATTACKS: dict[str, str] = {
    "assignment": f"UNDOCUMENTED_GAPS = {_GROWN}\n",
    "augmented": 'UNDOCUMENTED_GAPS |= frozenset({"sneaky.gap"})\n',
    "if-block": f"if True:\n    UNDOCUMENTED_GAPS = {_GROWN}\n",
    "globals-subscript": f'globals()["UNDOCUMENTED_GAPS"] = {_GROWN}\n',
    "globals-update-keyword": f"globals().update(UNDOCUMENTED_GAPS={_GROWN})\n",
    "setattr-self": f'import sys\nsetattr(sys.modules[__name__], "UNDOCUMENTED_GAPS", {_GROWN})\n',
    "attribute-store-on-cells": f"from . import cells\ncells.UNDOCUMENTED_GAPS = {_GROWN}\n",
    "reimport-from-elsewhere": "from .families.sneaky import UNDOCUMENTED_GAPS\n",
    "reimport-under-alias": "from .cells import NEW_CELL_ALLOWLIST as UNDOCUMENTED_GAPS\n",
    "star-import": "from .cells import *\n",
    "exec": 'exec("UNDOCUMENTED_GAPS = frozenset({\'sneaky.gap\'})")\n',
    "def-named-it": "def UNDOCUMENTED_GAPS():\n    pass\n",
}


class TestReexports:
    def test_plain_reexport_is_fine(self) -> None:
        assert check_reexports(DictTree({_INIT: _INIT_OK}), _INIT) == []

    def test_missing_init_is_fine(self) -> None:
        assert check_reexports(DictTree({}), _INIT) == []

    def test_the_real_init_passes(self) -> None:
        from check_ratchets import REPO_ROOT
        tree = DictTree({_INIT: (REPO_ROOT / _INIT).read_text()})
        assert check_reexports(tree, _INIT) == []

    def test_init_is_under_the_check(self) -> None:
        assert _INIT in RATCHET_REEXPORTS

    @pytest.mark.parametrize("attack", sorted(_INIT_ATTACKS))
    def test_rebind_in_init_fails_closed(self, attack: str) -> None:
        problems = check_reexports(DictTree({_INIT: _INIT_OK + _INIT_ATTACKS[attack]}), _INIT)
        assert problems and "cannot verify" in problems[0], attack
        assert "UNDOCUMENTED_GAPS" in problems[0]

    def test_run_all_checks_includes_the_init(self) -> None:
        tree = DictTree({_INIT: _INIT_OK + _INIT_ATTACKS["assignment"]})
        assert any(_INIT in p for p in run_all_checks(DictTree({}), tree))


# ── the checker edited in the same change: the base's own RATCHET_SETS  ──
# ── (read from the base's copy of check_ratchets.py, not the running   ──
# ── script) says what must still be checked. Deleting an entry, or     ──
# ── renaming or moving a set along with a matching entry, used to pass ──
# ── ("nothing to shrink from") and let the set grow in the same change. ──

_CELLS = "eval/caselib/cells.py"
_MODEL = "eval/caselib/model.py"


def _checker(sets: tuple[tuple[str, str], ...], reexports: tuple[str, ...] | None = None) -> str:
    text = f"RATCHET_SETS: tuple[tuple[str, str], ...] = {sets!r}\n"
    if reexports is not None:
        text += f"RATCHET_REEXPORTS: tuple[str, ...] = {reexports!r}\n"
    return text


class TestRegistry:
    SETS = ((_CELLS, "UNDOCUMENTED_GAPS"), (_MODEL, "GALLERY_FIELDS_PENDING"))
    CELLS_OLD = 'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap"})\n'
    MODEL_OLD = "GALLERY_FIELDS_PENDING: frozenset[str] = frozenset()\n"

    def _old(self, sets=SETS, **extra: str) -> DictTree:
        return DictTree({CHECKER_PATH: _checker(sets), _CELLS: self.CELLS_OLD,
                         _MODEL: self.MODEL_OLD, **extra})

    def test_unchanged_registry_passes(self) -> None:
        old = self._old()
        new = DictTree({_CELLS: self.CELLS_OLD, _MODEL: self.MODEL_OLD})
        assert run_all_checks(old, new, sets=self.SETS, reexports=()) == []

    def test_deleting_an_entry_fails(self) -> None:
        # The entry goes and the set grows in the same change.
        old = self._old()
        new = DictTree({_CELLS: 'UNDOCUMENTED_GAPS: frozenset[str] = frozenset({"a.gap", "b"})\n',
                        _MODEL: self.MODEL_OLD})
        problems = run_all_checks(old, new, sets=self.SETS[1:], reexports=())
        assert any("UNDOCUMENTED_GAPS" in p and "no longer" in p for p in problems), problems

    def test_renaming_a_set_with_a_matching_entry_fails(self) -> None:
        old = self._old()
        new = DictTree({_CELLS: 'RENAMED_GAPS: frozenset[str] = frozenset({"a.gap", "b"})\n',
                        _MODEL: self.MODEL_OLD})
        sets = ((_CELLS, "RENAMED_GAPS"), self.SETS[1])
        problems = run_all_checks(old, new, sets=sets, reexports=())
        assert any("UNDOCUMENTED_GAPS" in p and "no longer" in p for p in problems), problems

    def test_moving_a_set_with_a_matching_entry_fails(self) -> None:
        old = self._old()
        new = DictTree({_CELLS: "\n", _MODEL: self.MODEL_OLD + 'UNDOCUMENTED_GAPS: frozenset[str] '
                        '= frozenset({"a.gap", "b"})\n'})
        sets = ((_MODEL, "UNDOCUMENTED_GAPS"), self.SETS[1])
        problems = run_all_checks(old, new, sets=sets, reexports=())
        assert any(_CELLS in p and "no longer" in p for p in problems), problems

    def test_listed_in_the_base_but_missing_there_fails(self) -> None:
        # The base's registry names it, so "not in the old tree" is no
        # longer "a brand-new ratchet": it was renamed or moved.
        old = self._old(**{_CELLS: "SOMETHING_ELSE = 1\n"})
        new = DictTree({_CELLS: self.CELLS_OLD, _MODEL: self.MODEL_OLD})
        problems = run_all_checks(old, new, sets=self.SETS, reexports=())
        assert any("UNDOCUMENTED_GAPS" in p and "cannot verify" in p for p in problems), problems

    def test_listed_in_the_base_but_its_file_missing_there_fails(self) -> None:
        old = DictTree({CHECKER_PATH: _checker(self.SETS), _MODEL: self.MODEL_OLD})
        new = DictTree({_CELLS: self.CELLS_OLD, _MODEL: self.MODEL_OLD})
        problems = run_all_checks(old, new, sets=self.SETS, reexports=())
        assert any(_CELLS in p and "cannot verify" in p for p in problems), problems

    def test_a_genuinely_new_ratchet_has_nothing_to_shrink_from(self) -> None:
        # Not in the base's registry, not in the base: no comparison.
        old = self._old()
        new = DictTree({_CELLS: self.CELLS_OLD + 'NEW_LIST: frozenset[str] = frozenset({"x"})\n',
                        _MODEL: self.MODEL_OLD})
        sets = self.SETS + ((_CELLS, "NEW_LIST"),)
        assert run_all_checks(old, new, sets=sets, reexports=()) == []

    def test_dropping_a_reexport_module_fails(self) -> None:
        old = DictTree({CHECKER_PATH: _checker(self.SETS, (_INIT,))})
        problems = check_registry(old, sets=self.SETS, reexports=())
        assert problems and _INIT in problems[0]

    def test_base_without_a_checker_has_no_registry(self) -> None:
        assert check_registry(DictTree({}), sets=(), reexports=()) == []

    def test_base_checker_predating_reexports_is_fine(self) -> None:
        old = DictTree({CHECKER_PATH: _checker(self.SETS)})
        assert check_registry(old, sets=self.SETS, reexports=(_INIT,)) == []

    def test_unreadable_base_registry_fails_closed(self) -> None:
        old = DictTree({CHECKER_PATH: "RATCHET_SETS = tuple(compute())\n"})
        assert check_registry(old, sets=self.SETS, reexports=())

    def test_the_real_base_registry_is_still_checked(self) -> None:
        from check_ratchets import REPO_ROOT, RATCHET_SETS
        this = DictTree({CHECKER_PATH: (REPO_ROOT / CHECKER_PATH).read_text()})
        assert check_registry(this) == []
        assert (_CELLS, "UNDOCUMENTED_GAPS") in RATCHET_SETS


class TestWriterMessages:
    """"N possible writers" names each one and why it counts."""

    @pytest.mark.parametrize(("attack", "reason"), [
        ("globals-subscript", "a string constant mentioning UNDOCUMENTED_GAPS at line 2"),
        ("import-alias", "an import binding UNDOCUMENTED_GAPS at line 2"),
        ("def-named-it", "def UNDOCUMENTED_GAPS at line 2"),
        ("except-as", "except ... as UNDOCUMENTED_GAPS at line 4"),
        ("del", "a del of UNDOCUMENTED_GAPS at line 2"),
    ])
    def test_reason_is_named(self, attack: str, reason: str) -> None:
        old = DictTree({"cells.py": _LITERAL})
        new = DictTree({"cells.py": _LITERAL + _REBIND_ATTACKS[attack]})
        (problem,) = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert "an assignment to UNDOCUMENTED_GAPS at line 1" in problem
        assert reason in problem

    def test_star_import_is_named(self) -> None:
        # A star import could rebind frozenset itself, reported first.
        old = DictTree({"cells.py": _LITERAL})
        new = DictTree({"cells.py": _LITERAL + _REBIND_ATTACKS["star-import"]})
        (problem,) = check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS")
        assert "a star import at line 2" in problem


# ── check_redteam_anchors: initial_labels_sha256 must never move,      ──
# ── and a round's identity may not be renamed or removed out from under ──
# ── it (the reviewer's confirmed bypass: `git mv` the round dir, forge  ──
# ── a fresh anchor at the new path, and the old glob-by-path comparison ──
# ── never notices the old path is gone).                                ──

class TestCheckRedteamAnchors:
    ROUND = "eval/caselib/redteam/round-0-example/round.json"
    RENAMED = "eval/caselib/redteam/round-0-renamed/round.json"

    def _round_json(self, initial: str, labels: str | None = None,
                    round_id: str = "round-0-example") -> str:
        import json
        return json.dumps({
            "round": round_id, "initial_labels_sha256": initial,
            "labels_sha256": labels or initial,
        })

    def test_unchanged_anchor_is_fine(self) -> None:
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.ROUND: self._round_json("abc123")})
        assert check_redteam_anchors(old, new) == []

    def test_moved_anchor_fails(self) -> None:
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.ROUND: self._round_json("def456", labels="def456")})
        problems = check_redteam_anchors(old, new)
        assert len(problems) == 1
        assert "abc123" in problems[0] and "def456" in problems[0]

    def test_labels_sha256_alone_moving_is_fine_here(self) -> None:
        """This check is only about the anchor; labels_sha256 drifting
        from initial (a legitimate, adjudicated relabel) is
        families/redteam.py's check_label_lock's job, not this one's."""
        old = DictTree({self.ROUND: self._round_json("abc123", labels="abc123")})
        new = DictTree({self.ROUND: self._round_json("abc123", labels="def456")})
        assert check_redteam_anchors(old, new) == []

    def test_new_identity_has_nothing_to_compare_against(self) -> None:
        # A genuinely new round: its identity did not exist in the old
        # tree at all (which is empty here), so there is nothing to have
        # moved from.
        old = DictTree({})
        new = DictTree({self.ROUND: self._round_json("abc123")})
        assert check_redteam_anchors(old, new) == []

    def test_removed_round_fails(self) -> None:
        """A round's identity existing in the old tree and not the new
        one is always a failure — a round's history may not simply
        vanish, whether that is an outright deletion or (see the rename
        tests below) a rename that also changes what round.json's own
        "round" field says."""
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({})
        problems = check_redteam_anchors(old, new)
        assert len(problems) == 1
        assert "round-0-example" in problems[0]
        assert "missing from the new tree" in problems[0]

    def test_pure_rename_with_unchanged_anchor_is_fine(self) -> None:
        """Renaming the round's directory changes nothing this check
        cares about, as long as the identity (round.json's "round" field)
        and the anchor both come along unchanged: rounds are matched by
        identity, never by path."""
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.RENAMED: self._round_json("abc123")})
        assert check_redteam_anchors(old, new) == []

    def test_rename_with_forged_anchor_still_fails(self) -> None:
        """The confirmed bypass: rename the round's directory *and*
        rewrite labels.json, setting both labels_sha256 and
        initial_labels_sha256 to match the new content. Because rounds
        are matched by identity (the "round" field, left unchanged by the
        rename) rather than by path, the old anchor for that identity is
        still found and still compared — the path moving does not let the
        forged anchor slip through unnoticed."""
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.RENAMED: self._round_json("forged999", labels="forged999")})
        problems = check_redteam_anchors(old, new)
        assert len(problems) == 1
        assert "round-0-example" in problems[0]
        assert "abc123" in problems[0] and "forged999" in problems[0]

    def test_rename_that_also_changes_identity_fails_as_a_removal(self) -> None:
        """Renaming the directory *and* the "round" field inside it is
        indistinguishable from deleting the old identity and creating an
        unrelated new one — the old identity still disappears, so this
        still fails, just under the "removed" message rather than
        "anchor changed"."""
        old = DictTree({self.ROUND: self._round_json("abc123", round_id="round-0-example")})
        new = DictTree({self.RENAMED: self._round_json("abc123", round_id="round-0-renamed")})
        problems = check_redteam_anchors(old, new)
        assert len(problems) == 1
        assert "round-0-example" in problems[0]
        assert "missing from the new tree" in problems[0]

    def test_multiple_rounds_checked_independently(self) -> None:
        other_old, other_new = "eval/caselib/redteam/round-1/round.json", \
            "eval/caselib/redteam/round-1/round.json"
        old = DictTree({
            self.ROUND: self._round_json("abc123"),
            other_old: self._round_json("zzz", round_id="round-1"),
        })
        new = DictTree({
            self.ROUND: self._round_json("abc123"),
            other_new: self._round_json("yyy", labels="yyy", round_id="round-1"),
        })
        problems = check_redteam_anchors(old, new)
        assert len(problems) == 1
        assert "round-1" in problems[0]

    def test_malformed_json_fails_rather_than_crashes(self) -> None:
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.ROUND: "{not json"})
        problems = check_redteam_anchors(old, new)
        assert problems and "JSON" in problems[0]

    def test_missing_round_field_fails_rather_than_crashes(self) -> None:
        import json
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.ROUND: json.dumps({"initial_labels_sha256": "abc123",
                                                "labels_sha256": "abc123"})})
        problems = check_redteam_anchors(old, new)
        assert problems and "round" in problems[0].lower()

    def test_duplicate_identity_in_one_tree_fails_rather_than_crashes(self) -> None:
        other = "eval/caselib/redteam/round-1/round.json"
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({
            self.ROUND: self._round_json("abc123", round_id="round-0-example"),
            other: self._round_json("zzz", round_id="round-0-example"),  # same identity twice
        })
        problems = check_redteam_anchors(old, new)
        assert problems and "unique" in problems[0]


# ── run_all_checks: everything together ──────────────────────────────────

def test_run_all_checks_combines_every_ratchet() -> None:
    old = DictTree({
        "eval/caselib/cells.py": _cells_source(("a.gap",)),
        "eval/caselib/model.py": "GALLERY_FIELDS_PENDING: frozenset[str] = frozenset()\n",
        "eval/caselib/redteam/round-0-example/round.json":
            TestCheckRedteamAnchors()._round_json("abc123"),
    })
    new = DictTree({
        "eval/caselib/cells.py": _cells_source(("a.gap", "b.gap")),
        "eval/caselib/model.py": 'GALLERY_FIELDS_PENDING: frozenset[str] = frozenset({"x.y"})\n',
        "eval/caselib/redteam/round-0-example/round.json":
            TestCheckRedteamAnchors()._round_json("def456", labels="def456"),
    })
    problems = run_all_checks(old, new)
    # One problem each: UNDOCUMENTED_GAPS grew, GALLERY_FIELDS_PENDING
    # grew, and the round-0-example anchor moved. NONFITZ_PENDING is
    # unset in both fakes and parses as empty in both, so it's silent.
    assert len(problems) == 3


def test_run_all_checks_clean_when_nothing_changed() -> None:
    files = {
        "eval/caselib/cells.py": _cells_source(("a.gap",)),
        "eval/caselib/model.py": "GALLERY_FIELDS_PENDING: frozenset[str] = frozenset()\n",
    }
    assert run_all_checks(DictTree(files), DictTree(dict(files))) == []


# ── is_ci_context: only a genuinely local run may skip cleanly ──────────

class TestIsCiContext:
    def test_github_actions_flag(self) -> None:
        from check_ratchets import is_ci_context
        assert is_ci_context({"GITHUB_ACTIONS": "true"})

    def test_base_ref_alone_counts(self) -> None:
        from check_ratchets import is_ci_context
        assert is_ci_context({"GITHUB_BASE_REF": "main"})

    def test_event_name_alone_counts(self) -> None:
        from check_ratchets import is_ci_context
        assert is_ci_context({"GITHUB_EVENT_NAME": "push"})

    def test_empty_env_is_not_ci(self) -> None:
        from check_ratchets import is_ci_context
        assert not is_ci_context({})


# ── determine_base_ref: which base to diff against, and whether failing ──
# ── to resolve one is a skip or a hard failure ────────────────────────────

class TestDetermineBaseRef:
    def test_pull_request_uses_merge_base(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: ref == "origin/main")
        monkeypatch.setattr(check_ratchets, "merge_base",
                            lambda a, b: "deadbeef" if b == "origin/main" else None)
        ref, message, fatal = determine_base_ref({"GITHUB_BASE_REF": "main"})
        assert ref == "deadbeef"
        assert "pull request" in message
        assert fatal is False

    def test_pull_request_with_unreachable_base_fails_closed_in_ci(self, monkeypatch) -> None:
        """The reviewer's second confirmed bypass: an unresolvable base
        (origin/main not fetched, no merge-base, ...) used to return
        (None, "...skipping") and main() would exit 0 — silently passing
        the whole check. GITHUB_BASE_REF being set at all means this is a
        pull request build, i.e. always CI, so this must now be fatal."""
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message, fatal = determine_base_ref({"GITHUB_BASE_REF": "main"})
        assert ref is None
        assert fatal is True

    def test_push_to_main_uses_head_minus_one(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: ref == "HEAD~1")
        ref, message, fatal = determine_base_ref({
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        })
        assert ref == "HEAD~1"
        assert "single-commit" in message
        assert fatal is False

    def test_push_to_main_first_commit_fails_closed_in_ci(self, monkeypatch) -> None:
        """GITHUB_EVENT_NAME being set at all is also a CI signal, so no
        HEAD~1 to fall back to (e.g. the repository's very first commit)
        must be fatal here too, not a silent skip."""
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message, fatal = determine_base_ref({
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        })
        assert ref is None
        assert fatal is True

    def test_local_run_prefers_origin_main_merge_base(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: ref == "origin/main")
        monkeypatch.setattr(check_ratchets, "merge_base", lambda a, b: "cafef00d")
        monkeypatch.setattr(check_ratchets, "rev_parse", lambda ref: "current-head")
        ref, message, fatal = determine_base_ref({})
        assert ref == "cafef00d"
        assert "local run" in message
        assert fatal is False

    def test_local_run_on_main_itself_falls_back(self, monkeypatch) -> None:
        """merge-base with origin/main equals HEAD itself (we ARE main):
        nothing to diff there, fall back to HEAD~1."""
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists",
                            lambda ref: ref in ("origin/main", "HEAD~1"))
        monkeypatch.setattr(check_ratchets, "merge_base", lambda a, b: "same-as-head")
        monkeypatch.setattr(check_ratchets, "rev_parse", lambda ref: "same-as-head")
        ref, message, fatal = determine_base_ref({})
        assert ref == "HEAD~1"
        assert fatal is False

    def test_nothing_available_in_a_local_run_skips_cleanly(self, monkeypatch) -> None:
        """No CI env vars at all (a bare local invocation) and nothing to
        compare against: this is the one case that may still skip."""
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message, fatal = determine_base_ref({})
        assert ref is None
        assert fatal is False

    def test_nothing_available_in_ci_fails_closed(self, monkeypatch) -> None:
        """The same unresolvable situation, but GITHUB_ACTIONS=true is
        set (e.g. some future job shape hitting this final fallback):
        must fail, not skip."""
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message, fatal = determine_base_ref({"GITHUB_ACTIONS": "true"})
        assert ref is None
        assert fatal is True


class TestMain:
    def test_explicit_base_runs_against_head(self) -> None:
        """A thin smoke test of main() against the real repo: --base HEAD
        (comparing the working tree against itself) must always be clean."""
        import check_ratchets

        assert check_ratchets.main(["--base", "HEAD"]) == 0

    def test_unresolvable_base_in_ci_env_fails_the_process(self, monkeypatch) -> None:
        """End-to-end: simulate the exact CI shape from the confirmed
        bypass (a pull request whose base can't be resolved) and check
        main() itself now exits non-zero instead of silently returning 0."""
        import check_ratchets

        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        assert check_ratchets.main([]) == 1

    def test_unresolvable_base_locally_exits_zero(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        assert check_ratchets.main([]) == 0
