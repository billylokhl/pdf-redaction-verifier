"""The case library (eval/caselib): every case judged against the tool,
and the library itself checked for integrity.

A case with a ``known_gap`` is judged against the wrong answer today's
tool gives (``KnownGap.today``): a fix, a partial fix or a broken
generator all fail the test until the label is updated
(docs/REDESIGN.md §5).
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from caselib import CELLS, REGISTRY, UNDOCUMENTED_GAPS, load
from caselib.cells import COLUMNS, ROW_STORAGE, parts
from caselib.lock import LOCK, lockable
from caselib.run import available, build, judge, scan

from .conftest import REPO_ROOT

load()
HAVE = available()
LOCKED = json.loads(LOCK.read_text())


def _params():
    for case_id, case in sorted(REGISTRY.items()):
        missing = case.requires - HAVE
        marks = [pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}")] if missing else []
        yield pytest.param(case, id=case_id, marks=marks)


@pytest.mark.parametrize("case", _params())
def test_case(case, tmp_path) -> None:
    pdf = build(case, tmp_path / f"{case.id}.pdf")
    digest = LOCKED.get(case.id)
    if digest is not None:
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == digest, (
            f"{case.id}: generator output changed — review it, then run "
            "`PYTHONPATH=eval:. python -m caselib.lock`")
    result = scan(case, pdf, tmp_path)
    if case.known_gap:
        today = judge(case, result, want=case.known_gap.today)
        assert today.ok, (f"{case.id}: known gap {case.known_gap.cell} moved — "
                          f"{'; '.join(today.problems)}. Relabel the case.")
        assert not judge(case, result).ok, f"{case.id}: gap closed — remove known_gap"
    else:
        verdict = judge(case, result)
        assert verdict.ok, f"{case.id}: {'; '.join(verdict.problems)}"


# ── The library ────────────────────────────────────────────────────────

def _evidence() -> dict[str, list]:
    """Cell -> leak cases the tool gets right that plant a secret there."""
    by_cell: dict[str, list] = {}
    for case in REGISTRY.values():
        if case.truth == "leak" and case.known_gap is None:
            for cell in case.cells:
                by_cell.setdefault(cell, []).append(case)
    return by_cell


def _storages(case) -> set[str]:
    want = case.expected
    return {s for _, s in want.findings} | {s for _, s in want.warnings if s}


class TestEvidence:
    def test_every_claimed_cell_has_a_leak_case(self) -> None:
        """Every ✓ and ⚑ cell needs a case that plants a secret there and
        is caught — a clean case that merely gets flagged is not evidence."""
        evidence = _evidence()
        claimed = sorted(cid for cid, cell in CELLS.items()
                         if cell.status in ("read", "flagged") and cid not in evidence)
        assert claimed == []

    def test_evidence_is_reported_where_the_cell_says(self) -> None:
        """A leak case credited to a cell must expect the finding or flag
        in that cell's storage class (live / orphaned / superseded)."""
        wrong = []
        for cell, cases in _evidence().items():
            row, _, _ = parts(cell)
            storage = ROW_STORAGE.get(row)
            for case in cases:
                if storage and case.expected.exit != 0 and storage not in _storages(case):
                    wrong.append((case.id, cell, sorted(_storages(case))))
        assert wrong == []

    def test_flagged_cells_expect_a_flag(self) -> None:
        for cell, cases in _evidence().items():
            if CELLS[cell].status == "flagged":
                for case in cases:
                    assert case.expected.warnings, f"{case.id} evidences ⚑ {cell} without a flag"

    def test_review_tier_cells_expect_review(self) -> None:
        for cell, cases in _evidence().items():
            if CELLS[cell].status == "read" and CELLS[cell].tier == "review":
                assert any(c.expected.exit == 2 for c in cases), cell

    def test_every_gap_has_a_pinned_case(self) -> None:
        pinned = {c.known_gap.cell for c in REGISTRY.values() if c.known_gap}
        gaps = {cid for cid, cell in CELLS.items() if cell.status in ("gap", "false-alarm")}
        assert sorted(gaps - pinned - UNDOCUMENTED_GAPS) == []
        assert sorted(UNDOCUMENTED_GAPS & pinned) == [], "pinned now: remove from UNDOCUMENTED_GAPS"
        assert UNDOCUMENTED_GAPS <= gaps

    def test_known_gaps_name_gap_cells(self) -> None:
        for case in REGISTRY.values():
            if case.known_gap:
                assert CELLS[case.known_gap.cell].status in ("gap", "false-alarm"), case.id
                assert case.known_gap.today != case.expected, case.id


class TestCoverageDoc:
    """cells.py must say what COVERAGE.md says, cell by cell."""

    GLYPHS = {"✓": "read", "⚑": "flagged", "✗": "gap"}

    def _table(self) -> dict[tuple[str, str], str]:
        text = (REPO_ROOT / "COVERAGE.md").read_text()
        table = {}
        for line in text.splitlines():
            m = re.match(r"^\| `([a-z-]+)` \|", line)
            if m:
                cols = [c.strip() for c in line.strip().strip("|").split("|")]
                for column, cell in zip(COLUMNS, cols[2:6]):
                    table[(m.group(1), column)] = cell
        return table

    def test_statuses_match(self) -> None:
        table = self._table()
        problems = []
        for (row, column), text in table.items():
            base = CELLS.get(f"{row}.{column}")
            glyph = next((g for g in text if g in self.GLYPHS), None)
            if text.startswith("—"):
                if base:
                    problems.append(f"{row}.{column}: COVERAGE says —, cells.py has {base.status}")
                continue
            if base is None:
                problems.append(f"{row}.{column}: COVERAGE says {glyph}, no cell id")
            elif self.GLYPHS[glyph] != base.status:
                problems.append(f"{row}.{column}: COVERAGE {glyph}, cells.py {base.status}")
            qualified_gaps = [cid for cid, c in CELLS.items()
                              if cid.startswith(f"{row}.{column}.") and c.status == "gap"]
            if "✗" in text and base and base.status != "gap" and not qualified_gaps:
                problems.append(f"{row}.{column}: COVERAGE has a ✗ part with no id")
            if qualified_gaps and "✗" not in text:
                problems.append(f"{row}.{column}: {qualified_gaps} not marked ✗ in COVERAGE")
        for cid in CELLS:
            row, column, _ = parts(cid)
            if column and (row, column) not in table:
                problems.append(f"{cid}: no such cell in COVERAGE.md")
        assert problems == []


class TestLibrary:
    def test_lock_lists_every_lockable_case(self) -> None:
        expected_ids = {c.id for c in REGISTRY.values() if lockable(c)}
        assert set(LOCKED) == expected_ids, "case list changed: rerun python -m caselib.lock"

    def test_real_files_are_referenced_and_small(self) -> None:
        real = REPO_ROOT / "eval" / "caselib" / "real"
        on_disk = {p.name for p in real.glob("*.pdf")}
        source = (REPO_ROOT / "eval" / "caselib" / "families" / "redactors.py").read_text()
        assert on_disk and all(name in source for name in on_disk)
        assert all((real / name).stat().st_size < 100_000 for name in on_disk)

    def test_real_files_hold_nothing_personal(self) -> None:
        for pdf in (REPO_ROOT / "eval" / "caselib" / "real").glob("*.pdf"):
            data = pdf.read_bytes()
            assert not re.search(rb"/Users/|/home/|C:\\\\Users", data), pdf.name

    def test_stories(self) -> None:
        for case in REGISTRY.values():
            assert case.story.strip().endswith((".", ")")), case.id
