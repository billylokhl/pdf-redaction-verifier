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

import fitz
import pytest

from caselib import (CELLS, GALLERY_FIELDS_PENDING, NEW_CELL_ALLOWLIST, NONFITZ_PENDING,
                     REGISTRY, UNDOCUMENTED_GAPS, load)
from caselib.cells import COLUMNS, ROW_STORAGE, parts
from caselib.families import redteam as redteam_loader
from caselib.lock import LOCK, lockable
from caselib.model import Case, expect
from caselib.run import Scan, available, build, judge, scan

from .conftest import REPO_ROOT

load()
HAVE = available()
LOCKED = json.loads(LOCK.read_text())
REAL_DIR = REPO_ROOT / "eval" / "caselib" / "real"
SIZE_CAP = 100_000


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

    def test_stories(self) -> None:
        for case in REGISTRY.values():
            assert case.story.strip().endswith((".", ")")), case.id


# ── Provenance sidecars (eval/caselib/real/<name>.json) ─────────────────

class TestProvenance:
    """Every committed real-tool PDF (docs/REDESIGN.md §5's "real-redactor
    tier") carries a sidecar recording where it came from, so a reviewer
    never has to trust the PDF's bytes on faith."""

    REQUIRED_FIELDS = ("file", "sha256", "size_bytes", "size_cap_bytes", "origin", "case_id",
                       "tool", "fabricated_data_statement", "metadata_fields_scrubbed",
                       "labels_summary")

    def _sidecars(self) -> dict[str, dict]:
        return {p.stem: json.loads(p.read_text()) for p in REAL_DIR.glob("*.json")}

    def test_every_real_pdf_has_a_sidecar(self) -> None:
        pdfs = {p.stem for p in REAL_DIR.glob("*.pdf")}
        sidecars = set(self._sidecars())
        assert pdfs, "no real/*.pdf files found"
        assert pdfs == sidecars, f"missing or orphaned sidecars: {pdfs ^ sidecars}"

    def test_sidecar_fields_present(self) -> None:
        for stem, data in self._sidecars().items():
            missing = [f for f in self.REQUIRED_FIELDS if f not in data]
            assert missing == [], f"{stem}.json missing {missing}"
            tool = data["tool"]
            assert tool.get("description") and tool.get("version") and tool.get("date"), stem
            assert "settings" in tool and tool["settings"], f"{stem}.json: tool.settings is empty"

    def test_sidecar_sha256_matches_the_file(self) -> None:
        for stem, data in self._sidecars().items():
            pdf = REAL_DIR / data["file"]
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            assert digest == data["sha256"], f"{stem}.json: sha256 does not match {pdf.name}"
            assert pdf.stat().st_size == data["size_bytes"], f"{stem}.json: size_bytes is stale"

    def test_sidecar_size_cap_is_declared_and_enforced(self) -> None:
        for stem, data in self._sidecars().items():
            cap = data["size_cap_bytes"]
            assert isinstance(cap, int) and 0 < cap <= SIZE_CAP, f"{stem}.json: bad size_cap_bytes"
            assert data["size_bytes"] <= cap, f"{stem}.json: file exceeds its own declared cap"

    def test_sidecar_case_matches_the_registry(self) -> None:
        """The sidecar's case_id, origin and truth must be the actual case
        that uses this file — cross-checked independently of
        families/redactors.py's own ``_sidecar`` (which only checks the
        cases it itself defines)."""
        for stem, data in self._sidecars().items():
            case_id = data["case_id"]
            assert case_id in REGISTRY, f"{stem}.json: case {case_id!r} is not registered"
            case = REGISTRY[case_id]
            assert case.origin == "redactor", f"{case_id}: origin must be 'redactor'"
            assert case.writer == "file", f"{case_id}: writer must be 'file'"
            assert case.truth == data["labels_summary"]["truth"], (
                f"{case_id}: sidecar truth disagrees with the registered case")


# ── Privacy scrub: every committed binary, raw and decompressed ─────────

_HOME_PATH_RE = re.compile(rb"/Users/[^/\s)>]+|/home/[^/\s)>]+|C:\\Users\\[^\\/\s)>]+")
_EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_ALLOWED_EMAIL_DOMAINS = (b"example.com", b"example.org")
_HOSTNAME_RE = re.compile(
    rb"\b[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.(?:local|lan|corp|internal|home|"
    rb"localdomain)\b", re.IGNORECASE)
_UUID = rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_XMP_ID_RE = re.compile(rb"xmpMM:(?:Document|Instance)ID=\"[^\"]*" + _UUID + rb"[^\"]*\"")


def _email_allowed(domain: bytes) -> bool:
    domain = domain.lower()
    return domain in _ALLOWED_EMAIL_DOMAINS or domain.endswith(b".test")


def _privacy_findings(pdf_path) -> list[tuple[str, str]]:
    """(kind, matched text) for every home path, non-fixture email,
    hostname-shaped string, and machine-generated-looking XMP id in
    *pdf_path*'s raw bytes and its decompressed streams."""
    blobs = [pdf_path.read_bytes()]
    try:
        doc = fitz.open(str(pdf_path))
        for xref in range(1, doc.xref_length()):
            if doc.xref_is_stream(xref):
                try:
                    blobs.append(doc.xref_stream(xref))
                except Exception:
                    pass
        doc.close()
    except Exception:
        pass
    findings: list[tuple[str, str]] = []
    for blob in blobs:
        for m in _HOME_PATH_RE.finditer(blob):
            findings.append(("home-path", m.group().decode("latin-1")))
        for m in _EMAIL_RE.finditer(blob):
            if not _email_allowed(m.group(1)):
                findings.append(("email", m.group().decode("latin-1")))
        for m in _HOSTNAME_RE.finditer(blob):
            findings.append(("hostname", m.group().decode("latin-1")))
        for m in _XMP_ID_RE.finditer(blob):
            findings.append(("xmp-id", m.group().decode("latin-1", "replace")))
    seen: set[tuple[str, str]] = set()
    unique = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def _allowlisted(case, kind: str, text: str) -> bool:
    return any(entry.get("reason") and entry.get("pattern", "") in text
              for entry in case.privacy_allowlist)


class TestPrivacyScrub:
    """No committed binary — today only ``real/*.pdf`` and the red-team
    round's PDFs, but this runs for any future ``writer="file"`` case —
    may hold a home-directory path, a non-fixture email, a hostname, or a
    machine-looking XMP id, unless the case explicitly allowlists it
    (``Case.privacy_allowlist``) with a reason."""

    def _file_cases(self):
        return [c for c in REGISTRY.values() if c.writer == "file"]

    def test_there_are_file_backed_cases_to_check(self) -> None:
        # A regression guard on the test itself: if this ever hits zero,
        # the scrub below is silently checking nothing.
        assert self._file_cases()

    def test_no_unallowlisted_privacy_findings(self, tmp_path) -> None:
        problems = []
        for case in self._file_cases():
            pdf = build(case, tmp_path / f"{case.id}.privacy.pdf")
            for kind, text in _privacy_findings(pdf):
                if not _allowlisted(case, kind, text):
                    problems.append(f"{case.id}: {kind} {text!r}")
        assert problems == []

    def test_allowlist_entries_have_a_reason(self) -> None:
        for case in REGISTRY.values():
            for entry in case.privacy_allowlist:
                assert entry.get("pattern"), f"{case.id}: privacy_allowlist entry has no pattern"
                assert entry.get("reason"), f"{case.id}: privacy_allowlist entry has no reason"


# ── The blind red-team slot (eval/caselib/redteam/) ──────────────────────

class TestRedTeam:
    def test_at_least_the_example_round_is_loaded(self) -> None:
        redteam_cases = [c for c in REGISTRY.values() if c.origin == "redteam"]
        assert redteam_cases, "no origin='redteam' cases registered"

    def test_round_dirs_are_found(self) -> None:
        assert redteam_loader.round_dirs(), "no red-team round directories found"

    @pytest.mark.parametrize("round_dir", redteam_loader.round_dirs(), ids=lambda d: d.name)
    def test_label_hash_lock(self, round_dir) -> None:
        """labels.json must match round.json's recorded hash, and any
        drift from the round's frozen initial hash must be recorded in
        adjudications.log (redteam/README.md's "Why labels are frozen")."""
        redteam_loader.check_label_lock(round_dir)  # raises with the specifics on failure

    def test_adjudication_entries_are_well_formed(self) -> None:
        required = {"round", "case_id", "date", "adjudicator", "reason", "old_sha256", "new_sha256"}
        for entry in redteam_loader.read_adjudications():
            missing = required - entry.keys()
            assert not missing, f"adjudications.log entry missing {missing}: {entry}"

    def test_new_cell_allowlist_maps_to_issue_numbers(self) -> None:
        for cell_id, issue in NEW_CELL_ALLOWLIST.items():
            assert cell_id.startswith("new."), cell_id
            assert isinstance(issue, int) and issue > 0, (
                f"{cell_id}: NEW_CELL_ALLOWLIST value must be a GitHub issue number")

    def test_new_cell_allowlist_has_no_graduated_ids(self) -> None:
        """Once a placeholder gets a real COVERAGE.md row (cells.CELLS),
        it must be removed here — never left listed alongside the real id."""
        graduated = set(NEW_CELL_ALLOWLIST) & set(CELLS)
        assert sorted(graduated) == []

    def test_new_placeholder_rejected_outside_the_allowlist(self) -> None:
        """model.Case's own gate: a redteam case may use an allowlisted
        new.* id; a non-redteam case, or an unlisted new.* id, is rejected
        at construction time — the mechanism redteam/README.md documents,
        exercised directly here rather than requiring a real round to use
        it."""
        from caselib.model import Case, expect

        def make(cell: str, origin: str):
            return Case(id="page.placeholder-probe", truth="leak", cells=(cell,),
                       expected=expect(1, findings=(("SSN", "live"),)),
                       story="probe.", build=lambda p: None, origin=origin)

        with pytest.raises(ValueError):
            make("new.unlisted-place", "redteam")
        with pytest.raises(ValueError):
            make("new.unlisted-place", "generated")
        if NEW_CELL_ALLOWLIST:
            listed = next(iter(NEW_CELL_ALLOWLIST))
            make(listed, "redteam")                      # must not raise
            with pytest.raises(ValueError):
                make(listed, "generated")                 # placeholder is redteam-only


# ── Gallery fields ratchet (mistake / recovery on leak cases) ────────────

class TestGalleryFields:
    """Leak cases are shown in a gallery of how redaction fails; each
    should carry ``mistake`` (what caused it) and ``recovery`` (how it's
    found by hand). ``GALLERY_FIELDS_PENDING`` is a may-only-shrink
    allowlist for the ones that don't yet, the same shape as
    ``UNDOCUMENTED_GAPS``."""

    def _missing(self) -> set[str]:
        return {c.id for c in REGISTRY.values()
               if c.truth == "leak" and not (c.mistake and c.recovery)}

    def test_missing_fields_are_all_pending(self) -> None:
        missing = self._missing()
        assert sorted(missing - GALLERY_FIELDS_PENDING) == []

    def test_pending_list_has_no_stale_entries(self) -> None:
        missing = self._missing()
        assert sorted(GALLERY_FIELDS_PENDING - missing) == [], (
            "filled in now: remove from GALLERY_FIELDS_PENDING")

    def test_pending_ids_are_real_leak_cases(self) -> None:
        leak_ids = {c.id for c in REGISTRY.values() if c.truth == "leak"}
        assert GALLERY_FIELDS_PENDING <= leak_ids
