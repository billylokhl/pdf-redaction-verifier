"""The case library (eval/caselib): every case judged against the tool,
and the library itself checked for integrity.

A case with a ``known_gap`` is a strict xfail: today's tool gets it
wrong, and when that changes the test fails until the label is updated
(docs/REDESIGN.md §5).
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from caselib import CELLS, PENDING, REGISTRY, load
from caselib.lock import LOCK, lockable
from caselib.run import available, build, judge, scan

from .conftest import REPO_ROOT

load()
HAVE = available()


def _params():
    for case_id, case in sorted(REGISTRY.items()):
        marks = []
        missing = case.requires - HAVE
        if missing:
            marks.append(pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}"))
        if case.known_gap:
            marks.append(pytest.mark.xfail(
                strict=True, reason=f"known gap: {case.known_gap} ({CELLS[case.known_gap].description})"))
        yield pytest.param(case, id=case_id, marks=marks)


@pytest.mark.parametrize("case", _params())
def test_case(case, tmp_path) -> None:
    pdf = build(case, tmp_path / f"{case.id}.pdf")
    verdict = judge(case, scan(case, pdf, tmp_path))
    assert verdict.ok, f"{case.id}: {'; '.join(verdict.problems)}"


class TestLibrary:
    def test_every_claimed_cell_has_evidence(self) -> None:
        """Every cell COVERAGE.md claims as read (✓) or flagged (⚑) needs a
        case that plants a secret there and expects the tool to catch it —
        or an entry in PENDING, which may only shrink."""
        evidence = {cell for case in REGISTRY.values()
                    if case.known_gap is None and case.expected.exit != 0
                    for cell in case.cells}
        claimed = {cid for cid, cell in CELLS.items() if cell.status in ("read", "flagged")}
        assert sorted(claimed - evidence - PENDING) == [], "claimed cells without a case"
        assert sorted(PENDING & evidence) == [], "covered now: remove from PENDING"
        assert PENDING <= claimed

    def test_every_gap_case_names_a_gap_cell(self) -> None:
        for case in REGISTRY.values():
            if case.known_gap:
                assert CELLS[case.known_gap].status == "gap", case.id

    def test_coverage_md_names_every_row(self) -> None:
        coverage = (REPO_ROOT / "COVERAGE.md").read_text()
        rows = set(re.findall(r"^\| `([a-z-]+)` \|", coverage, re.M))
        used = {cid.split(".")[0] for cid in CELLS} - {"match", "fp"}
        assert used <= rows, f"rows missing from COVERAGE.md: {sorted(used - rows)}"

    def test_builds_match_the_lock(self, tmp_path) -> None:
        """A generator whose output changed (an edit, a PyMuPDF upgrade)
        must be reviewed: rerun `python -m caselib.lock` and commit it."""
        lock = json.loads(LOCK.read_text())
        expected_ids = {c.id for c in REGISTRY.values() if lockable(c)}
        assert set(lock) == expected_ids, "case list changed: rerun python -m caselib.lock"
        drift = []
        for case_id, digest in sorted(lock.items()):
            if digest is None:
                continue
            case = REGISTRY[case_id]
            pdf = build(case, tmp_path / f"{case_id}.pdf")
            if hashlib.sha256(pdf.read_bytes()).hexdigest() != digest:
                drift.append(case_id)
        assert drift == [], f"generator output changed: {drift}"

    def test_real_files_are_referenced(self) -> None:
        real = REPO_ROOT / "eval" / "caselib" / "real"
        on_disk = {p.name for p in real.glob("*.pdf")}
        source = (REPO_ROOT / "eval" / "caselib" / "families" / "redactors.py").read_text()
        assert all(name in source for name in on_disk)
        assert all((real / name).stat().st_size < 100_000 for name in on_disk)

    def test_stories(self) -> None:
        for case in REGISTRY.values():
            assert case.story.strip(), case.id
            assert case.story.rstrip().endswith((".", ")")), case.id

    def test_ids_are_grouped(self) -> None:
        prefixes = {cid.split(".")[0] for cid in REGISTRY}
        assert prefixes <= {"clean", "leak", "gap", "redactor"}, prefixes

