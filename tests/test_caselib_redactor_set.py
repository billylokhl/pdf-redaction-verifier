"""The real-redactor set's committed subset (eval/caselib/real/, the files
whose sidecar carries an ``oracle`` block): their labels against the
locked adjudication, the oracle's raw channel re-run on the bytes, and
what metadata and page text they may carry. Fabricated values only."""

from __future__ import annotations

import json
import re
import sys

import pytest

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "eval"))

from caselib import REGISTRY, load  # noqa: E402
from caselib.cells import CELLS  # noqa: E402
from caselib.families.redactors import PERSONA_RULES  # noqa: E402
from redactor_set import oracle_raw, persona  # noqa: E402
from redactor_set.adjudicate import EXIT_ORDER, worst_exit  # noqa: E402
from redactor_set.oracle import CLASSES  # noqa: E402

load()
REAL = REPO_ROOT / "eval" / "caselib" / "real"
SIDECARS = {p.stem: json.loads(p.read_text()) for p in sorted(REAL.glob("*.json"))
            if "oracle" in json.loads(p.read_text())}
STORAGE = {"live": "live", "pixels-live": "live", "orphaned": "orphaned",
           "pixels-orphaned": "orphaned", "superseded": "superseded"}
OWNER_LISTED_GAPS = {"name-variant", "name-fragment"}     # owner decision, 2026-10-15


def test_the_subset_is_there():
    assert len(SIDECARS) >= 6
    assert {d["labels_summary"]["truth"] for d in SIDECARS.values()} == {"leak", "clean"}


@pytest.mark.parametrize("stem", sorted(SIDECARS))
def test_oracle_block_is_well_formed(stem):
    """Test 1: every instance has a class; a recoverable one a cell of
    COVERAGE.md and an expected exit; the file's truth and expected exit
    follow from its rows (worst first, 0 < 2 < 1)."""
    oracle = SIDECARS[stem]["oracle"]
    assert oracle["adjudicator"] and oracle["adjudication_date"]
    assert re.fullmatch(r"[0-9a-f]{64}", oracle["lock_sha256"])
    exits = []
    for inst in oracle["instances"]:
        assert inst["class"] in CLASSES, (stem, inst)
        if inst["class"] == "removed":
            continue
        assert inst["cell"] in CELLS, (stem, inst)
        assert inst["expected_exit"] in EXIT_ORDER, (stem, inst)
        assert inst["expected_exit"] != 0, (stem, inst)   # never on a leaking row
        exits.append(inst["expected_exit"])
    leak = any(i["class"] != "removed" for i in oracle["instances"])
    assert oracle["truth"] == ("leak" if leak else "clean")
    assert oracle["expected_exit"] == (worst_exit(exits) if leak else 0)
    assert SIDECARS[stem]["labels_summary"]["truth"] == oracle["truth"]
    assert SIDECARS[stem]["labels_summary"]["expected_exit"] == oracle["expected_exit"]


@pytest.mark.parametrize("stem", sorted(SIDECARS))
def test_case_label_follows_the_adjudication(stem):
    """Test 2: the case's expected exit is the adjudicated one; each
    recoverable instance expected to give a finding is required as one
    (its secret's rule, its storage), except an owner-accepted listed
    gap; and every instance's mapped library case exists with the same
    exit, unless the adjudicator overrode it with a reason."""
    data = SIDECARS[stem]
    case = REGISTRY[data["case_id"]]
    oracle = data["oracle"]
    assert case.expected.exit == oracle["expected_exit"]
    required = set()
    for inst in oracle["instances"]:
        if inst["class"] == "removed":
            continue
        if inst["listed_gap"]:
            assert inst["listed_gap"] in OWNER_LISTED_GAPS, inst
            continue
        if inst["override_reason"]:
            assert inst["case"] is None or inst["case"] in REGISTRY
        else:
            assert inst["case"] in REGISTRY, inst
            assert REGISTRY[inst["case"]].expected.exit == inst["expected_exit"], inst
        if inst["expected_exit"] == 1 and CELLS[inst["cell"]].status == "read":
            required.add((inst["secret"], STORAGE[inst["storage"]]))
    assert set(case.expected.findings) == required, (stem, case.expected.findings, required)


@pytest.mark.parametrize("stem", sorted(SIDECARS))
def test_raw_channel_rederives_on_the_committed_bytes(stem):
    """Test 3: the standard-library raw scanner (R1) and the object walk
    (R2), re-run on the committed file, find every instance the
    adjudication saw through them again (R1 outside font programs and
    images); and R1 finds no secret whole for an instance classed removed
    (unless another instance of the same written form is recoverable).
    OCR and pdfminer are recorded, not re-run: CI lacks them."""
    data = SIDECARS[stem]
    raw = (REAL / data["file"]).read_bytes()
    instances = [i for i in data["oracle"]["instances"] if i["written"]]
    forms: dict[str, list[str]] = {}
    for inst in instances:
        forms.setdefault(inst["written"], [])
        forms[inst["written"]] += inst["fingerprints"]
    report = oracle_raw.scan(raw, forms, partial=False)
    found = {h.form for h in report.hits if h.kind not in oracle_raw.NOT_RECOVERABLE_ALONE}
    recoverable = {i["written"] for i in instances if i["class"] != "removed"}
    # R2 (pikepdf over every object, live or orphaned) runs in CI too.
    from redactor_set import oracle

    hits, _ = oracle.r2_scan(REAL / data["file"], forms)
    found2 = {h.form for h in hits if h.how == "whole" and h.counts}
    for inst in instances:
        if inst["r1_witness"]:
            assert inst["written"] in found, (stem, inst["secret"], inst["loc"])
        # A pixels instance's R2 witness is OCR of an extracted image: not re-run.
        if "R2" in inst["channels"] and inst["encoding"] != "pixels":
            assert inst["written"] in found2, (stem, inst["secret"], inst["loc"])
        if inst["class"] == "removed" and inst["written"] not in recoverable:
            assert inst["written"] not in found, (stem, inst["secret"], inst["loc"])


PRODUCER_STRINGS = (
    re.compile(r"Skia/PDF m\d+"),
    re.compile(r"Mozilla/5\.0 \(Macintosh; Intel Mac OS X 10_15_7\) AppleWebKit/[\d.]+ "
               r"\(KHTML, like Gecko\) HeadlessChrome/[\d.]+ Safari/[\d.]+"),
    re.compile(r"ReportLab PDF Library - www\.reportlab\.com"),
    re.compile(r"Tesseract [\d.]+"),
    re.compile(r"macOS Version [\d.]+ \(Build \w+\) Quartz PDFContext"),
    re.compile(r"pikepdf(?: [\d.]+)?"),                     # XMP toolkit
    re.compile(r"D:\d{14}(?:[+-]\d\d'\d\d'?|Z)?"),         # a PDF date
    re.compile(r"x-default"),                               # XMP language tag
)


def _info_like(raw: bytes) -> list[tuple[str, str]]:
    """Every Info key and value in any dictionary of the file, reachable
    or not, by a raw scan of uncompressed objects (pikepdf reads object
    streams)."""
    import pikepdf

    keys = {"/Producer", "/Creator", "/Title", "/Author", "/Subject", "/Keywords",
            "/CreationDate", "/ModDate", "/Trapped"}
    out = []
    with pikepdf.open(__import__("io").BytesIO(raw)) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Dictionary) and "/Type" not in obj and \
                    set(obj.keys()) & keys:
                out += [(str(k), str(v)) for k, v in obj.items()]
    return out


@pytest.mark.parametrize("stem", sorted(SIDECARS))
def test_metadata_is_allowlisted(stem):
    """Test 5: every Info key and value (live or orphaned) and every XMP
    value is in the sidecar's info_fields, and each value is a persona
    value (or holds one), or a producer string on the allowlist. A login
    or real name would fail here; TestPrivacyScrub cannot see it."""
    data = SIDECARS[stem]
    raw = (REAL / data["file"]).read_bytes()
    listed = {(f["key"], f["value"]) for f in data["info_fields"]}
    for key, value in _info_like(raw):
        assert (key, value) in listed, (stem, key)
    values = {s.value for s in persona.SECRETS}
    for field in data["info_fields"]:
        v = field["value"]
        persona_held = any(p in v for p in values)
        producer = any(r.fullmatch(v) for r in PRODUCER_STRINGS)
        assert persona_held or producer, (stem, field["key"], v)


@pytest.mark.parametrize("stem", sorted(SIDECARS))
def test_rendered_text_holds_no_local_paths(stem):
    """Test 6: no page's extracted text holds a file URL or a home path
    (a browser's header or footer is drawn in glyph codes the raw scrub
    cannot read)."""
    fitz = pytest.importorskip("fitz")
    with fitz.open(REAL / SIDECARS[stem]["file"]) as doc:
        text = "".join(page.get_text() for page in doc)
    for bad in ("file://", "/Users/", "/home/", "C:\\Users"):
        assert bad not in text, (stem, bad)


def test_persona_rules_are_the_persona():
    """Test 7: the case library's PERSONA_RULES are the set's persona and
    config patterns (a format check: tests/test_redactor_set_generator.py
    checks the values cannot be real and share no digit run)."""
    values = [r for r in PERSONA_RULES if "value" in r]
    assert [(r["name"], r["value"]) for r in values] == [(s.id, s.value)
                                                         for s in persona.SECRETS]
    assert [r["pattern"] for r in PERSONA_RULES if "pattern" in r] == list(persona.PATTERNS)
    for data in SIDECARS.values():
        assert data["redacted_values"] == [dict(r) for r in PERSONA_RULES]


# ── The subset tool (eval/redactor_set/subset.py) ───────────────────────

def test_info_fields_include_an_orphaned_info_dictionary(tmp_path):
    """A redactor that nulls the trailer's /Info leaves the old dictionary
    in the file: info_fields must list it, or the allowlist test above
    could never see it."""
    from redactor_set import subset

    # Object 4 is referenced by nothing (written by hand: savers drop it).
    (tmp_path / "a.pdf").write_bytes(
        b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
        b"4 0 obj << /Author (Jordan Q. Testperson) /Producer (Skia/PDF m154) >> endobj\n"
        b"trailer << /Root 1 0 R /Size 5 >>\n%%EOF\n")
    fields = {(f["key"], f["value"]) for f in subset.info_fields(tmp_path / "a.pdf")}
    assert ("/Author", "Jordan Q. Testperson") in fields
    assert ("/Producer", "Skia/PDF m154") in fields


def test_oracle_block_carries_the_adjudication():
    from redactor_set import subset

    truth = {"instances": [{"written": "666-51-4073", "encoding": "page-text",
                            "fingerprints": ["ab"]}]}
    record = {"adjudicator": "agent", "date": "2026-10-14", "truth": "leak",
              "expected_exit": 1, "secrets": {"SSN1": "recoverable-hidden",
                                               "NAME1": "partial"},
              "instances": [
                  {"index": 0, "secret": "SSN1", "loc": "T1", "class": "recoverable-hidden",
                   "cell": "orphaned.plain", "case": "leftover.redactor-no-gc",
                   "where": "orphaned", "expected_exit": 1, "override": None,
                   "evidence": [{"channel": "R1", "how": "whole", "counts": True}]},
                  {"index": -1, "secret": "NAME1", "loc": "?", "class": "partial",
                   "where": "unknown", "evidence": [],
                   "override": {"class": "partial", "cell": "annot-fields.plain",
                                "expected_exit": 1, "reason": "a fragment"}}]}
    block = subset.oracle_block(record, truth, "f" * 64)
    first, second = block["instances"]
    assert first["r1_witness"] and first["channels"] == ["R1"] and first["listed_gap"] is None
    assert first["written"] == "666-51-4073" and first["encoding"] == "page-text"
    assert second["listed_gap"] == "name-fragment" and second["override_reason"] == "a fragment"
    assert block["lock_sha256"] == "f" * 64


def test_the_subset_refuses_bytes_the_lock_does_not_cover(tmp_path, monkeypatch):
    from redactor_set import adjudicate, subset

    root = tmp_path / "root"
    (root / "work" / "inputs").mkdir(parents=True)
    (root / "adjudication" / "inputs" / "m2").mkdir(parents=True)
    (root / "inputs").mkdir()
    (root / "config").mkdir()
    (root / "config" / "redact.yaml").write_text("entity_types: []\n")
    (root / "work" / "out.pdf").write_bytes(b"%PDF-1.7 changed\n")
    (root / "work" / "inputs" / "run.json").write_text(json.dumps(
        {"documents": {"D01-s0": {"m2": "work/out.pdf"}}}))
    (root / "inputs" / "D01-s0.truth.json").write_text(json.dumps({"instances": []}))
    (root / "adjudication" / "inputs" / "m2" / "D01-s0.json").write_text(json.dumps(
        {"sha256": "0" * 64}))
    monkeypatch.setattr(adjudicate, "verify", lambda r, s: ([], "f" * 64))
    with pytest.raises(SystemExit, match="not the bytes the lock covers"):
        subset.write(root, "inputs", {"x": ("D01-s0", "m2")}, tmp_path, {"x": "page.x"})
    monkeypatch.setattr(adjudicate, "verify", lambda r, s: (["changed since the lock"], None))
    with pytest.raises(SystemExit, match="does not verify"):
        subset.write(root, "inputs", {"x": ("D01-s0", "m2")}, tmp_path, {"x": "page.x"})
