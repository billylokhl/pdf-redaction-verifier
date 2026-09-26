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

from caselib import CELLS, NONFITZ_PENDING, REGISTRY, UNDOCUMENTED_GAPS, load
from caselib.cells import COLUMNS, ROW_STORAGE, parts
from caselib.lock import LOCK, lockable
from caselib.model import Case, expect
from caselib.run import Scan, available, build, judge, scan

from .conftest import REPO_ROOT

load()
HAVE = available()
LOCKED = json.loads(LOCK.read_text())


def _params():
    for case_id, case in sorted(REGISTRY.items()):
        missing = case.requires - HAVE
        marks = [pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}")] if missing else []
        if case.grid:
            marks.append(pytest.mark.grid)     # CI runs grids on Linux only
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


# ── judge()'s warning-storage check ──────────────────────────────────────


def test_judge_sorts_mixed_none_and_string_storage_without_crashing():
    """A code the case does not expect at all (want.warnings names a
    storage that never appears) can still be reported at more than one
    storage, one of them None (e.g. HIDDEN_ITEM_FAILED, raised both with
    and without a storage class) — sorting that residual set used to
    crash with TypeError (None is not orderable against str) before
    comparing by str."""
    synthetic = Case(
        id="file.warning-storage-mix",
        truth="clean",
        cells=(),
        expected=expect(2, warnings=(("HIDDEN_ITEM_FAILED", "orphaned"),)),
        story="unit test for judge()'s warning-storage check",
        build=lambda path: None,
    )
    report = {
        "error": None,
        "exit_code": 2,
        "findings": [],
        "warnings": [
            {"code": "HIDDEN_ITEM_FAILED", "storage": "live", "layer": "Hidden", "message": "m1"},
            {"code": "HIDDEN_ITEM_FAILED", "storage": None, "layer": "Hidden", "message": "m2"},
        ],
    }
    verdict = judge(synthetic, Scan(report=report, exit_code=2), have=frozenset({"qpdf", "exiftool", "ocr"}))
    assert not verdict.ok
    assert any("missing warning HIDDEN_ITEM_FAILED (orphaned)" in p for p in verdict.problems)
    assert any("HIDDEN_ITEM_FAILED" in p and "expected a different storage" in p for p in verdict.problems)


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

    def test_claimed_cells_have_non_fitz_evidence(self) -> None:
        """Every claimed cell needs a caught leak case whose bytes were not
        serialised by fitz — also what the tool reads with — or must be
        listed in NONFITZ_PENDING (docs/REDESIGN.md §5's same-author-bias
        guard). NONFITZ_PENDING may only shrink."""
        covered = {cid for cid, cases in _evidence().items()
                  if any(c.writer != "fitz" for c in cases)}
        claimed = {cid for cid, cell in CELLS.items() if cell.status in ("read", "flagged")}
        assert sorted(claimed - covered - NONFITZ_PENDING) == []
        assert sorted(NONFITZ_PENDING & covered) == [], "covered now: shrink NONFITZ_PENDING"

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
    def test_object_stream_case_really_packs_the_secret(self, tmp_path) -> None:
        # The object-stream layout exists to put the secret inside an
        # /ObjStm body (an annotation is packed; /Info is not): the surface
        # behind the bug that called every packed object ORPHANED.
        import fitz

        from caselib import SSN
        import verify
        path = build(REGISTRY["document.annotation-objstm"], tmp_path / "objstm.pdf")
        doc = fitz.open(path)
        assert any(
            doc.xref_get_key(x, "Type")[1] == "/ObjStm"
            and verify.normalize_string(SSN)
            in verify.normalize_string(doc.xref_stream(x).decode("latin-1"))
            for x in range(1, doc.xref_length()) if doc.xref_is_stream(x))


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
