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
import os
import re

import fitz
import pytest

from caselib import (CELLS, GALLERY_FIELDS_PENDING, NEW_CELL_ALLOWLIST, NONFITZ_PENDING,
                     PRIVACY_KINDS, REGISTRY, UNDOCUMENTED_GAPS, load)
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


RUN_PERF = os.environ.get("RUN_PERF") == "1"


def _params():
    return list(_iter_params())     # pytest 10 rejects a bare generator


def _iter_params():
    for case_id, case in sorted(REGISTRY.items()):
        missing = case.requires - HAVE
        marks = [pytest.mark.skip(reason=f"needs {', '.join(sorted(missing))}")] if missing else []
        if case.grid:
            marks.append(pytest.mark.grid)     # CI runs grids on Linux only
        if case.perf:
            marks.append(pytest.mark.perf)     # large files: opt in with RUN_PERF=1
            if not RUN_PERF:
                marks.append(pytest.mark.skip(reason="perf case; set RUN_PERF=1 to run"))
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


# ── Privacy scrub: every committed binary, raw, decompressed and decoded ─
#
# Beyond scanning raw bytes and decompressed streams directly, this also
# extracts every PDF string token (literal ``(...)`` and hex ``<...>``)
# from each blob and decodes it — hex to its packed bytes, and either of
# those (or a literal string) starting with a UTF-16 byte-order mark to
# actual text — because a leak hiding in a hex string or a UTF-16BE
# literal never matches an ASCII-oriented regex against the surrounding
# raw bytes.

_HOME_PATH_RE = re.compile(rb"/Users/[^/\s)>]+|/home/[^/\s)>]+|C:\\Users\\[^\\/\s)>]+")
_EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_ALLOWED_EMAIL_DOMAINS = (b"example.com", b"example.org")
_HOSTNAME_RE = re.compile(
    rb"\b[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.(?:local|lan|corp|internal|home|"
    rb"localdomain)\b", re.IGNORECASE)
_UUID = rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_XMP_ID_RE = re.compile(rb"xmpMM:(?:Document|Instance)ID=\"[^\"]*" + _UUID + rb"[^\"]*\"")
# Patterns .privacy_allowlist rejects outright, kept alongside the probe
# check below (belt and suspenders — cheap to check, catches the common
# cases by name in an assertion message instead of just "matched a probe").
_TRIVIAL_PATTERNS = frozenset({"", ".", ".*", ".+", "(?s).*", "(?s:.*)"})

# A literal denylist is trivially dodged by anything equivalent but
# spelled differently (`[\s\S]*`, `.*?`, `(?s)^.*$`, ...). Instead, a
# pattern is rejected if it fullmatches any of these probe strings: no
# legitimate, narrowly-scoped exception (a specific fixture value) should
# ever match unrelated, unplanned text. The generic probes catch
# "matches anything" patterns regardless of kind; the per-kind ones catch
# a pattern that is specific to *looking* like that kind (e.g.
# `/Users/.*`) without actually being narrow.
_GENERIC_PROBES: tuple[str, ...] = (
    "kQ7#mZ2$vB9!wK4^pL6&nR1*sD8@fG3%tY5~uJ0X",  # ~40 mixed letters/digits/punctuation
    "probe line one\nprobe line two",             # contains a newline
)
# Several varied, fixed values per kind — not just one. A single fixed
# probe per kind can itself be dodged (e.g. a negative lookahead pinned
# to exactly that one value, `(?!probe)`); several realistically-diverse
# values close that hole without needing every dodge to be anticipated
# individually. Lookarounds are additionally rejected outright below,
# which is the primary defense — this is belt and suspenders.
_KIND_PROBES: dict[str, tuple[str, ...]] = {
    "home-path": (
        "/Users/probe/x.txt", "/home/probe/x.txt", r"C:\Users\probe\x.txt",
        "/Users/jsmith/notes.txt", "/Users/anna-lee/Desktop/report.docx",
        "/home/jdoe/data.csv", "/home/test-user1/inbox.eml",
        r"C:\Users\bob.martinez\file.txt",
    ),
    "email": (
        "probe@probe.invalid", "john.doe@example.com", "a.smith+work@example.org",
        "no-reply@corp-mail.io", "user42@sub.example.net",
    ),
    "hostname": (
        "probe-host.local", "db-prod-01.local", "office-printer.lan",
        "ws-42.corp", "backup-server.internal", "mail01.localdomain",
    ),
    "xmp-id": (
        'xmpMM:DocumentID="uuid:00000000-0000-0000-0000-000000000000"',
        'xmpMM:InstanceID="uuid:12345678-90ab-cdef-1234-567890abcdef"',
    ),
}

# Any lookaround defeats probing itself: a pattern can be written so it
# fails to match whatever fixed probe values happen to be listed above
# while still matching everything else (`/Users/(?!probe)[a-zA-Z0-9_-]+/.*`
# matches every real home path except the one probe it was built to
# dodge). No legitimate, narrowly-scoped allowlist entry — one pinned to
# a specific fixture value via re.escape — ever needs a lookaround, so
# these are rejected outright regardless of what they exclude.
_LOOKAROUND_TOKENS = ("(?=", "(?!", "(?<=", "(?<!")


def _email_allowed(domain: bytes) -> bool:
    domain = domain.lower()
    return domain in _ALLOWED_EMAIL_DOMAINS or domain.endswith(b".test")


def _extract_pdf_strings(blob: bytes) -> list[bytes]:
    """Every literal ``(...)`` (escapes resolved) and hex ``<...>`` (nibbles
    packed) PDF string token's raw bytes in *blob*. A best-effort scanner,
    not a full PDF parser — good enough for the privacy scrub, which only
    needs to find text, not build an object model. Dictionaries
    (``<<...>>``) are not hex strings: a ``<`` immediately followed or
    preceded by another ``<``/``>`` is skipped."""
    out: list[bytes] = []
    i, n = 0, len(blob)
    simple_escapes = {0x6e: 0x0a, 0x72: 0x0d, 0x74: 0x09, 0x62: 0x08,
                      0x66: 0x0c, 0x28: 0x28, 0x29: 0x29, 0x5c: 0x5c}
    while i < n:
        ch = blob[i]
        if ch == 0x28:  # "("
            depth, j, buf = 1, i + 1, bytearray()
            while j < n and depth > 0:
                c = blob[j]
                if c == 0x5c and j + 1 < n:  # backslash
                    nxt = blob[j + 1]
                    if nxt in simple_escapes:
                        buf.append(simple_escapes[nxt]); j += 2
                    elif 0x30 <= nxt <= 0x37:  # octal escape
                        k, digits = j + 1, b""
                        while k < n and len(digits) < 3 and 0x30 <= blob[k] <= 0x37:
                            digits += blob[k:k + 1]; k += 1
                        buf.append(int(digits, 8) & 0xFF); j = k
                    elif nxt in (0x0d, 0x0a):  # line continuation: dropped
                        j += 2
                        if nxt == 0x0d and j < n and blob[j] == 0x0a:
                            j += 1
                    else:
                        buf.append(nxt); j += 2
                elif c == 0x28:
                    depth += 1; buf.append(c); j += 1
                elif c == 0x29:
                    depth -= 1; j += 1
                    if depth > 0:
                        buf.append(c)
                else:
                    buf.append(c); j += 1
            out.append(bytes(buf))
            i = j
        elif ch == 0x3c and blob[i + 1:i + 2] != b"<" and (i == 0 or blob[i - 1:i] != b"<"):
            j = i + 1
            while j < n and blob[j] != 0x3e:
                j += 1
            digits = re.sub(rb"\s+", b"", blob[i + 1:j])
            if digits and re.fullmatch(rb"[0-9A-Fa-f]*", digits):
                if len(digits) % 2:
                    digits += b"0"
                try:
                    out.append(bytes.fromhex(digits.decode("ascii")))
                except ValueError:
                    pass
            i = j + 1
        else:
            i += 1
    return out


def _decode_pdf_string(raw: bytes) -> str | None:
    """A PDF string's likely text: UTF-16 if it opens with the byte-order
    mark PDF literal/hex strings use for it, else latin-1 (never fails,
    and preserves byte values 1:1 for the ASCII case)."""
    if raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            return None
    if raw.startswith(b"\xff\xfe"):
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return None
    return raw.decode("latin-1")


def _privacy_findings(pdf_path) -> list[tuple[str, str]]:
    """(kind, matched text) for every home path, non-fixture email,
    hostname-shaped string, and machine-generated-looking XMP id, found
    in *pdf_path*'s raw bytes, its decompressed streams, every PDF string
    token those contain (decoded — including UTF-16 and hex strings),
    the document's metadata and XMP, and any embedded file's name or
    description."""
    blobs = [pdf_path.read_bytes()]
    try:
        doc = fitz.open(str(pdf_path))
        for xref in range(1, doc.xref_length()):
            if doc.xref_is_stream(xref):
                try:
                    blobs.append(doc.xref_stream(xref))
                except Exception:
                    pass
        for value in doc.metadata.values():
            if isinstance(value, str) and value:
                blobs.append(value.encode("utf-8", "surrogatepass"))
        xmp = doc.get_xml_metadata()
        if xmp:
            blobs.append(xmp.encode("utf-8", "surrogatepass"))
        for i in range(doc.embfile_count()):
            info = doc.embfile_info(i)
            for key in ("name", "filename", "ufilename", "description"):
                value = info.get(key)
                if isinstance(value, str) and value:
                    blobs.append(value.encode("utf-8", "surrogatepass"))
        doc.close()
    except Exception:
        pass

    # Every PDF string token nested in the blobs above, decoded, feeds
    # back in as its own blob — this is what catches a hex or UTF-16
    # encoded leak an ASCII regex over the container bytes would miss.
    decoded: list[bytes] = []
    for blob in blobs:
        for raw in _extract_pdf_strings(blob):
            decoded.append(raw)
            text = _decode_pdf_string(raw)
            if text is not None:
                decoded.append(text.encode("utf-8", "surrogatepass"))
    blobs += decoded

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


def _is_trivial_pattern(pattern: str, kind: str | None = None) -> bool:
    """True if *pattern* is on the literal denylist, contains a lookaround
    (rejected outright — see ``_LOOKAROUND_TOKENS``), fails to compile, or
    fullmatches any probe string — generic ones always, plus *kind*'s own
    (a pattern scoped to one kind can still be too broad for that kind
    specifically, e.g. ``/Users/.*`` for "home-path")."""
    if pattern.strip() in _TRIVIAL_PATTERNS:
        return True
    if any(token in pattern for token in _LOOKAROUND_TOKENS):
        return True
    probes = _GENERIC_PROBES + (_KIND_PROBES.get(kind, ()) if kind else ())
    for probe in probes:
        try:
            if re.fullmatch(pattern, probe):
                return True
        except re.error:
            return True  # cannot even compile: certainly not a narrow, specific pattern
    return False


def _allowlisted(case, kind: str, text: str) -> bool:
    """True only for an entry whose ``kind`` matches *kind* exactly and
    whose ``pattern`` fully matches *text* (a regex, not a substring) —
    one kind's exception never excuses another kind's finding."""
    for entry in case.privacy_allowlist:
        if entry.get("kind") != kind:
            continue
        pattern = entry.get("pattern", "")
        try:
            if re.fullmatch(pattern, text):
                return True
        except re.error:
            continue
    return False


class TestPrivacyScrub:
    """No committed binary — today only ``real/*.pdf`` and the red-team
    round's PDFs, but this runs for any future ``writer="file"`` case —
    may hold a home-directory path, a non-fixture email, a hostname, or a
    machine-looking XMP id, unless the case explicitly allowlists it
    (``Case.privacy_allowlist``) with a kind, a reason, and a non-trivial
    pattern."""

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
                kind = entry.get("kind")
                assert kind in PRIVACY_KINDS, (
                    f"{case.id}: privacy_allowlist entry kind {kind!r} "
                    f"must be one of {sorted(PRIVACY_KINDS)}")
                pattern = entry.get("pattern", "")
                assert pattern and not _is_trivial_pattern(pattern, kind), (
                    f"{case.id}: privacy_allowlist pattern {pattern!r} is too broad")
                assert entry.get("reason"), f"{case.id}: privacy_allowlist entry has no reason"

    @pytest.mark.parametrize("pattern", [
        r"[\s\S]*",       # matches everything, including newlines, without saying ".*"
        r".*?",           # non-greedy, but fullmatch still forces it to consume everything
        r"(?s)^.*$",      # DOTALL with anchors: still "matches everything"
        r"(?s:.*)",       # scoped inline-flag form of the same
        r".*",            # already on the literal denylist, sanity-checked here too
    ])
    def test_generic_bypass_patterns_are_still_trivial(self, pattern: str) -> None:
        """These are not on the literal _TRIVIAL_PATTERNS denylist (a
        reviewer found several of them still slip through it), but each
        still fullmatches an arbitrary-text probe and must be rejected."""
        assert _is_trivial_pattern(pattern, "hostname")

    @pytest.mark.parametrize(("kind", "pattern"), [
        ("home-path", r"/Users/.*"),
        ("home-path", r"/Users/[^/]+/.*"),
        ("email", r".*@.*"),
        ("email", r"[^@]+@[^@]+"),
        ("hostname", r".*\.local"),
    ])
    def test_kind_specific_bypass_patterns_are_still_trivial(self, kind: str, pattern: str) -> None:
        """A pattern shaped like the kind it claims to narrow (a path
        prefix, an "anything@anything" email) is still too broad — it
        fullmatches the kind-specific probe even though it wouldn't match
        the fully generic ones."""
        assert _is_trivial_pattern(pattern, kind)

    def test_a_genuinely_narrow_pattern_is_not_trivial(self) -> None:
        """The probe check must not reject everything — a pattern tied to
        one specific, already-known fixture value is exactly what
        privacy_allowlist exists for."""
        assert not _is_trivial_pattern(re.escape("ci@example.com"), "email")
        assert not _is_trivial_pattern(re.escape("/Users/fixture-only/known.txt"), "home-path")

    @pytest.mark.parametrize(("kind", "pattern"), [
        # The reviewer's exact reproductions: a negative lookahead pinned
        # to exactly the one fixed probe value, so the probe check alone
        # (before this fix) never noticed how broad the rest of the
        # pattern really is.
        ("home-path", r"/Users/(?!probe)[a-zA-Z0-9_-]+/.*"),
        ("hostname", r"(?!probe-host\.local$)[a-z0-9-]+\.local"),
        ("home-path", r"/home/(?!probe\b)\S+"),
        # A couple of our own attempts in the same family: a lookahead
        # tied to the generic probe instead of a kind probe, and a
        # lookbehind excluding an arbitrary prefix.
        ("email", r"(?!probe@probe\.invalid$)[^@]+@[^@]+"),
        ("home-path", r"(?<!not-)/Users/[a-zA-Z0-9_-]+/.*"),
    ])
    def test_lookaround_bypass_patterns_are_rejected_outright(self, kind: str, pattern: str) -> None:
        """Any lookaround is rejected regardless of what it excludes —
        the fix does not try to enumerate every way a lookahead/lookbehind
        could be built to dodge a fixed probe set; it refuses the whole
        category."""
        assert _is_trivial_pattern(pattern, kind)

    def test_diverse_probes_catch_a_pattern_that_only_dodges_one_fixed_value(self) -> None:
        """Before this fix, "home-path" probed a single fixed value
        ("/Users/probe/x.txt"). A pattern narrow enough to dodge exactly
        that one value (no lookaround needed at all — just knowledge of
        the one probe) but still far too broad for a real home path must
        still be rejected once probing uses several varied values."""
        pattern = r"/Users/[a-oq-z][a-z]*/[a-z]+\.[a-z]+"
        assert not re.fullmatch(pattern, "/Users/probe/x.txt"), (
            "sanity check: this pattern must dodge the old sole probe")
        assert _is_trivial_pattern(pattern, "home-path")

    def test_lookaround_is_not_rejected_for_unrelated_kinds_only(self) -> None:
        """Control: the lookaround rejection is unconditional — it does
        not need a matching kind to trigger, unlike the probe checks."""
        assert _is_trivial_pattern(r"(?!x)y", kind=None)
        assert _is_trivial_pattern(r"(?!x)y", kind="xmp-id")

    def test_allowlist_kind_does_not_cross_exempt(self) -> None:
        """A fake case allowlisting an 'email' finding must not also
        exempt an identical string reported as a 'hostname' finding."""
        from caselib.model import Case, expect

        fake = Case(
            id="page.allowlist-probe", truth="clean", cells=(), expected=expect(0),
            story="probe.", build=lambda p: None,
            privacy_allowlist=({"kind": "email", "pattern": re.escape("ci@example.com"),
                               "reason": "fixture address, not a real leak"},),
        )
        assert _allowlisted(fake, "email", "ci@example.com")
        assert not _allowlisted(fake, "hostname", "ci@example.com")

    def test_catches_home_path_in_utf16be_literal_string(self, tmp_path) -> None:
        """A home path stored as a UTF-16BE literal string (as a
        producer's /Title or a form field can hold Unicode text) is
        invisible to a plain ASCII byte scan — the reviewer built this
        exact case; it must be caught."""
        pdf = tmp_path / "utf16-leak.pdf"
        doc = fitz.open()
        doc.new_page()
        xref = doc.get_new_xref()
        payload = b"\xfe\xff" + "/Users/exampleuser/notes.txt".encode("utf-16-be")
        doc.update_object(xref, "<< /Type /Custom >>")
        doc.update_stream(xref, b"(" + payload + b")", compress=False)
        doc.save(str(pdf))
        doc.close()
        findings = _privacy_findings(pdf)
        assert any(kind == "home-path" for kind, _ in findings), findings

    def test_catches_home_path_in_hex_string(self, tmp_path) -> None:
        """The same leak, as a PDF hex string (``<2f55...>``) — decoded to
        plain ASCII bytes rather than left as hex digits."""
        pdf = tmp_path / "hex-leak.pdf"
        doc = fitz.open()
        doc.new_page()
        xref = doc.get_new_xref()
        text = b"/Users/exampleuser/secret.txt"
        hex_token = b"<" + text.hex().encode("ascii") + b">"
        doc.update_object(xref, "<< /Type /Custom >>")
        doc.update_stream(xref, hex_token, compress=False)
        doc.save(str(pdf))
        doc.close()
        findings = _privacy_findings(pdf)
        assert any(kind == "home-path" for kind, _ in findings), findings


# ── The blind red-team slot (eval/caselib/redteam/) ──────────────────────

class TestRedTeam:
    def test_at_least_the_example_round_is_loaded(self) -> None:
        redteam_cases = [c for c in REGISTRY.values() if c.origin == "redteam"]
        assert redteam_cases, "no origin='redteam' cases registered"

    @staticmethod
    def _ids_on_disk() -> set[str]:
        """Every case id listed in a round's labels.json, read straight
        off disk — independent of families/redteam.py's own (mutable,
        in-process) REGISTERED_IDS bookkeeping, which a bug in the loader
        itself could get wrong without this test noticing."""
        ids: set[str] = set()
        for round_dir in redteam_loader.round_dirs():
            entries = json.loads((round_dir / "labels.json").read_text())
            ids.update(entry["id"] for entry in entries)
        return ids

    def test_origin_matches_the_loader(self) -> None:
        """origin="redteam" is otherwise a self-declared tag any family
        could set on an ordinary case. The real gate is this: the set of
        ids actually claiming it must equal exactly the ids every round's
        labels.json lists on disk — recomputed independently here, not
        read back from the loader's own REGISTERED_IDS, so a bug that made
        the loader agree with itself (e.g. registering the wrong id, or an
        ordinary family also appending to REGISTERED_IDS) would still be
        caught. A normal family case with origin="redteam" would inflate
        the left side without ever appearing in any round's labels.json."""
        declared = {c.id for c in REGISTRY.values() if c.origin == "redteam"}
        assert declared == self._ids_on_disk()
        # The loader's own bookkeeping should agree too — a mismatch here
        # (with the disk-based check above still passing) would point at
        # a bug in families/redteam.py itself rather than a bypass.
        assert declared == set(redteam_loader.REGISTERED_IDS)
        assert len(redteam_loader.REGISTERED_IDS) == len(set(redteam_loader.REGISTERED_IDS))

    def test_round_dirs_are_found(self) -> None:
        assert redteam_loader.round_dirs(), "no red-team round directories found"

    @pytest.mark.parametrize("round_dir", redteam_loader.round_dirs(), ids=lambda d: d.name)
    def test_label_hash_lock(self, round_dir) -> None:
        """labels.json must match round.json's recorded hash, and any
        drift from the round's frozen initial hash must be recorded in
        adjudications.log (redteam/README.md's "Why labels are frozen")."""
        redteam_loader.check_label_lock(round_dir)  # raises with the specifics on failure

    @pytest.mark.parametrize("round_dir", redteam_loader.round_dirs(), ids=lambda d: d.name)
    def test_round_attestation(self, round_dir) -> None:
        """Every round declares who wrote it, when, which COVERAGE.md
        commit they had, and a no-code-access statement in their own
        words — plus a README.md a human can read."""
        redteam_loader.check_attestation(round_dir)  # raises with the specifics on failure

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
