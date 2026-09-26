"""check_ratchets.py's own checks, fed two fake in-memory trees — no git,
no filesystem — so the actual comparison logic is verified in isolation
from CI's git-history plumbing (which tests/test_case_library.py already
covers from a single checkout's point of view)."""

from __future__ import annotations

from check_ratchets import (DictTree, check_ratchet_set, check_redteam_anchors,
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

    def test_file_missing_in_new_tree_is_not_a_failure(self) -> None:
        old = DictTree({"cells.py": _cells_source(("a.gap",))})
        new = DictTree({})
        assert check_ratchet_set(old, new, "cells.py", "UNDOCUMENTED_GAPS") == []

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


# ── check_redteam_anchors: initial_labels_sha256 must never move ────────

class TestCheckRedteamAnchors:
    ROUND = "eval/caselib/redteam/round-0-example/round.json"

    def _round_json(self, initial: str, labels: str | None = None) -> str:
        import json
        return json.dumps({
            "round": "round-0-example", "initial_labels_sha256": initial,
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

    def test_new_round_has_nothing_to_compare_against(self) -> None:
        old = DictTree({})
        new = DictTree({self.ROUND: self._round_json("abc123")})
        assert check_redteam_anchors(old, new) == []

    def test_removed_round_is_not_this_checks_concern(self) -> None:
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({})
        assert check_redteam_anchors(old, new) == []

    def test_multiple_rounds_checked_independently(self) -> None:
        other = "eval/caselib/redteam/round-1/round.json"
        old = DictTree({self.ROUND: self._round_json("abc123"), other: self._round_json("zzz")})
        new = DictTree({self.ROUND: self._round_json("abc123"), other: self._round_json("yyy")})
        problems = check_redteam_anchors(old, new)
        assert len(problems) == 1
        assert "round-1" in problems[0]

    def test_malformed_json_fails_rather_than_crashes(self) -> None:
        old = DictTree({self.ROUND: self._round_json("abc123")})
        new = DictTree({self.ROUND: "{not json"})
        problems = check_redteam_anchors(old, new)
        assert problems and "JSON" in problems[0]


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


# ── determine_base_ref: which base to diff against ───────────────────────

class TestDetermineBaseRef:
    def test_pull_request_uses_merge_base(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: ref == "origin/main")
        monkeypatch.setattr(check_ratchets, "merge_base",
                            lambda a, b: "deadbeef" if b == "origin/main" else None)
        ref, message = determine_base_ref({"GITHUB_BASE_REF": "main"})
        assert ref == "deadbeef"
        assert "pull request" in message

    def test_pull_request_with_unreachable_base_skips(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message = determine_base_ref({"GITHUB_BASE_REF": "main"})
        assert ref is None
        assert "skipping" in message

    def test_push_to_main_uses_head_minus_one(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: ref == "HEAD~1")
        ref, message = determine_base_ref({
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        })
        assert ref == "HEAD~1"
        assert "single-commit" in message

    def test_push_to_main_first_commit_skips(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message = determine_base_ref({
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
        })
        assert ref is None
        assert "skipping" in message

    def test_local_run_prefers_origin_main_merge_base(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: ref == "origin/main")
        monkeypatch.setattr(check_ratchets, "merge_base", lambda a, b: "cafef00d")
        monkeypatch.setattr(check_ratchets, "rev_parse", lambda ref: "current-head")
        ref, message = determine_base_ref({})
        assert ref == "cafef00d"
        assert "local run" in message

    def test_local_run_on_main_itself_falls_back(self, monkeypatch) -> None:
        """merge-base with origin/main equals HEAD itself (we ARE main):
        nothing to diff there, fall back to HEAD~1."""
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists",
                            lambda ref: ref in ("origin/main", "HEAD~1"))
        monkeypatch.setattr(check_ratchets, "merge_base", lambda a, b: "same-as-head")
        monkeypatch.setattr(check_ratchets, "rev_parse", lambda ref: "same-as-head")
        ref, message = determine_base_ref({})
        assert ref == "HEAD~1"

    def test_nothing_available_skips_cleanly(self, monkeypatch) -> None:
        import check_ratchets

        monkeypatch.setattr(check_ratchets, "ref_exists", lambda ref: False)
        ref, message = determine_base_ref({})
        assert ref is None
        assert "skipping" in message


def test_main_with_explicit_base_runs_against_head(tmp_path, monkeypatch) -> None:
    """A thin smoke test of main() against the real repo: --base HEAD
    (comparing the working tree against itself) must always be clean."""
    import check_ratchets

    assert check_ratchets.main(["--base", "HEAD"]) == 0
