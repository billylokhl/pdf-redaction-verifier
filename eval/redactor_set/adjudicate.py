"""Adjudication: the oracle's classes, checked per file and frozen before
the verifier ever runs on the set.

``draft`` turns an oracle result into an adjudication record: per
instance its class, the COVERAGE.md cell it sits in and the verdict a
correct verifier gives there (``EXPECTED``, taken from the case library's
own case for that cell; tests/test_redactor_set_oracle.py checks every
case id and exit against the library), plus the channel disagreements
and OCR near misses the adjudicator must look at. An adjudicator (an
agent viewing the renders and crops, the owner spot-checking) fills in
``adjudicator``, resolves each uncertain instance and may override a
class with a reason. ``lock`` refuses an unfinished record and writes a
sha256 per record to ``adjudication.lock.json``; ``verify`` re-checks
every record against it. A record changed after the lock fails until it
is re-locked with a reason (``relocks``).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .oracle import SEVERITY, FileResult

# Severity of a verifier exit, worst last: clean, cannot certify, leak.
EXIT_ORDER = {0: 0, 2: 1, 1: 2}


@dataclass(frozen=True)
class Expectation:
    cell: str           # COVERAGE.md cell
    case: str           # the case library's case for that cell
    exit: int           # that case's `expected` exit (what a correct verifier gives)
    note: str = ""


# (location code, where the oracle found it) -> what a correct verifier
# gives. "where" is "live" (on the page or in a live object), "orphaned"
# (an object nothing references any more, such as a redacted page's old
# content stream), or "pixels". Owner decision 7 (2026-10-12): approved,
# to be revisited with this PR; the rows marked provisional have no case
# of their own in the library and are listed for that review.
EXPECTED: dict[tuple[str, str], Expectation] = {
    # Drawn text. Chrome's is font-coded (Identity-H); reportlab's and
    # Quartz's are plain codes.
    ("T1", "live"): Expectation("live.font", "page.visible-embedded-font", 1),
    ("T2", "live"): Expectation("live.font", "page.visible-embedded-font", 1),
    ("T3a", "live"): Expectation("match.line-wrap", "page.raw-font-coded-scattered-reversed", 2),
    ("T3b", "live"): Expectation("match.columns", "layout.two-column-wrap", 2),
    ("T3c", "live"): Expectation("match.page-break", "layout.raw-page-break", 1),
    ("Q1", "live"): Expectation("live.plain", "page.visible", 1),
    ("S2", "live"): Expectation("live.plain", "page.visible", 1,
                                "an OCR engine's invisible text layer, drawn in render mode 3"),
    # The same text left behind in an orphaned content stream (method 1).
    ("T1", "orphaned"): Expectation("orphaned.font", "leftover.deleted-page-embedded-font", 2),
    ("T2", "orphaned"): Expectation("orphaned.font", "leftover.deleted-page-embedded-font", 2),
    ("T3a", "orphaned"): Expectation("orphaned.font", "leftover.deleted-page-embedded-font", 2),
    ("T3b", "orphaned"): Expectation("orphaned.font", "leftover.deleted-page-embedded-font", 2),
    ("T3c", "orphaned"): Expectation("orphaned.font", "leftover.deleted-page-embedded-font", 2),
    ("Q1", "orphaned"): Expectation("orphaned.plain", "leftover.redacted-no-gc", 1),
    ("S2", "orphaned"): Expectation("orphaned.font", "leftover.deleted-page-embedded-font", 2,
                                    "provisional: tesseract's glyphless font"),
    # Strings in live objects.
    ("F1", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("F2", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("A1", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("A1x", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("A2", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("A3", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("A4", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1),
    ("O1", "live"): Expectation("annot-fields.plain", "document.form-ssn", 1,
                                "provisional: no case of its own for an outline title"),
    ("M1", "live"): Expectation("metadata.plain", "document.title", 1),
    ("M2", "live"): Expectation("metadata.plain", "document.title", 1),
    ("M3", "live"): Expectation("metadata.plain", "document.xmp-title", 1),
    ("E1", "live"): Expectation("attachment.plain", "attachment.raw-notes", 2),
    # Strings left in orphaned objects (method 1).
    ("M1", "orphaned"): Expectation("orphaned.plain", "leftover.deleted-page", 1,
                                    "provisional: an orphaned Info dictionary"),
    ("M2", "orphaned"): Expectation("orphaned.plain", "leftover.deleted-page", 1,
                                    "provisional: an orphaned Info dictionary"),
    ("M3", "orphaned"): Expectation("leftover-xmp.plain", "leftover.xmp", 1),
    ("F1", "orphaned"): Expectation("orphaned.plain", "leftover.deleted-page", 1),
    ("F2", "orphaned"): Expectation("orphaned.plain", "leftover.deleted-page", 1),
    ("A1", "orphaned"): Expectation("orphaned.plain", "leftover.deleted-page", 1),
    ("A1x", "orphaned"): Expectation("orphaned.plain", "leftover.deleted-page", 1),
    # Pixels.
    ("S1", "pixels"): Expectation("live.pixels", "page.scan-image", 1),
    ("S2", "pixels"): Expectation("live.pixels", "page.scan-image", 1),
    ("I1", "pixels"): Expectation("live.pixels", "page.raw-pixels", 1),
    ("P1", "pixels"): Expectation("thumbnail.pixels", "page.thumb-image", 1,
                                  "a known gap: today's tool exits 0"),
}


def _where(inst: dict[str, Any]) -> str:
    if inst["encoding"] == "pixels":
        return "pixels"
    storages = {e["storage"] for e in inst["evidence"] if e["counts"] and e["storage"]}
    return "orphaned" if storages == {"orphaned"} else "live"


def worst_exit(exits: list[int]) -> int:
    return max(exits, key=lambda e: EXIT_ORDER[e]) if exits else 0


# Producers whose drawn text uses plain (simple-font) codes: their page
# text maps as Q1 does, not as Chrome's font-coded text.
PLAIN_TEXT_PRODUCERS = {"quartz", "quartz+pikepdf", "reportlab"}


def expectation(loc: str, where: str, producer: str) -> Expectation | None:
    if loc in ("T1", "T2") and producer in PLAIN_TEXT_PRODUCERS:
        loc = "Q1"
    return EXPECTED.get((loc, where))


def draft(result: FileResult, truth_instances: list[dict[str, Any]],
          oracle_sha: str, producer: str) -> dict[str, Any]:
    """An adjudication record to be completed and locked."""
    instances = []
    for r in result.to_dict()["instances"]:
        base = truth_instances[r["index"]]
        r = {**r, "encoding": base["encoding"]}
        row: dict[str, Any] = {"index": r["index"], "secret": r["secret"], "loc": r["loc"],
                               "class": r["cls"], "uncertain": r["uncertain"],
                               "evidence": r["evidence"], "override": None}
        if r["cls"] != "removed":
            exp = expectation(r["loc"], _where(r), producer)
            row.update({"where": _where(r), "cell": exp.cell if exp else None,
                        "case": exp.case if exp else None,
                        "expected_exit": exp.exit if exp else None,
                        "expectation_note": exp.note if exp else "no mapping: adjudicate"})
        channels = {e["channel"] for e in r["evidence"] if e["how"] == "whole" and e["counts"]}
        if base["encoding"] == "page-text" and ("R3" in channels) != ("V" in channels):
            row["disagreement"] = ("live text read by pdfminer but not seen" if "R3" in channels
                                   else "seen but not in pdfminer's live text")
        instances.append(row)
    return {"output": result.output, "sha256": result.sha256, "doc": result.doc,
            "method": result.method, "oracle_sha256": oracle_sha, "adjudicator": None,
            "date": None, "instances": instances, "notes": "",
            "secrets": result.secrets, "truth": result.truth}


def finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Recompute the per-secret classes, the file's truth and its expected
    exit from the instances (overrides applied). Refuses an unfinished
    record: no adjudicator, an uncertain or disagreeing instance without
    an override or an explicit ``confirmed`` note, a recoverable instance
    without an expected exit."""
    problems = []
    if not record.get("adjudicator"):
        problems.append("no adjudicator")
    per_secret: dict[str, str] = {}
    exits: list[int] = []
    for row in record["instances"]:
        cls = row["override"]["class"] if row.get("override") else row["class"]
        if (row.get("uncertain") or row.get("disagreement")) and not (
                row.get("override") or row.get("confirmed")):
            problems.append(f"instance {row['index']} ({row['secret']} {row['loc']}) unresolved")
        if row.get("override") and not row["override"].get("reason"):
            problems.append(f"instance {row['index']}: an override needs a reason")
        prev = per_secret.get(row["secret"], "removed")
        per_secret[row["secret"]] = max(prev, cls, key=lambda c: SEVERITY[c])
        if cls != "removed":
            exp = row.get("override", {}) or {}
            exit_ = exp.get("expected_exit", row.get("expected_exit"))
            if exit_ is None:
                problems.append(f"instance {row['index']}: no expected exit")
            else:
                exits.append(exit_)
    if problems:
        raise ValueError("; ".join(problems))
    record = dict(record)
    record["secrets"] = dict(sorted(per_secret.items()))
    record["truth"] = "leak" if any(c != "removed" for c in per_secret.values()) else "clean"
    record["expected_exit"] = worst_exit(exits) if record["truth"] == "leak" else 0
    return record


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock(adj_dir: Path, *, reason: str = "") -> Path:
    """Finalise every record under *adj_dir* and write the lock. Records
    already locked must be unchanged unless *reason* is given (logged in
    the lock's ``relocks``)."""
    lock_path = adj_dir / "adjudication.lock.json"
    old = json.loads(lock_path.read_text()) if lock_path.exists() else {"records": {},
                                                                        "relocks": []}
    records: dict[str, str] = {}
    changed = []
    for path in sorted(adj_dir.rglob("*.json")):
        if path == lock_path:
            continue
        rec = json.loads(path.read_text())
        final = finalize_record(rec)
        if final != rec:
            path.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
        rel = str(path.relative_to(adj_dir))
        records[rel] = _sha(path)
        if rel in old["records"] and old["records"][rel] != records[rel]:
            changed.append(rel)
    if changed and not reason:
        raise ValueError(f"locked records changed ({', '.join(changed)}): give a reason")
    relocks = old.get("relocks", [])
    if changed:
        relocks.append({"date": datetime.date.today().isoformat(), "records": changed,
                        "reason": reason})
    lock_path.write_text(json.dumps({"records": records, "relocks": relocks}, indent=2,
                                    sort_keys=True) + "\n")
    return lock_path


def verify(adj_dir: Path) -> list[str]:
    """Problems with the lock: a record missing, added or changed since."""
    lock_path = adj_dir / "adjudication.lock.json"
    if not lock_path.exists():
        return ["no adjudication.lock.json"]
    locked = json.loads(lock_path.read_text())["records"]
    present = {str(p.relative_to(adj_dir)): _sha(p) for p in adj_dir.rglob("*.json")
               if p != lock_path}
    problems = [f"missing: {r}" for r in sorted(set(locked) - set(present))]
    problems += [f"not locked: {r}" for r in sorted(set(present) - set(locked))]
    problems += [f"changed since the lock: {r}" for r in sorted(set(locked) & set(present))
                 if locked[r] != present[r]]
    return problems
