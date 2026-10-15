"""Adjudication: the oracle's classes, checked per file and frozen before
the verifier ever runs on the set.

``draft`` turns an oracle result into an adjudication record: per
instance its class, the COVERAGE.md cell it sits in and the verdict a
correct verifier gives there (``EXPECTED``, taken from the case library's
own case for that cell; tests/test_redactor_set_oracle.py checks every
case id, cell and exit against the library), plus the flags the
adjudicator must clear: an uncertain OCR reading or unexplained ink, a
channel disagreement, evidence no planted instance can claim
(``unattributed``), a storage the oracle could not tell, a row with no
mapping. The adjudicator (an agent viewing the kept renders and crops,
the owner spot-checking) names themself and resolves each flag:
``confirmed`` (with a note) or an ``override`` with a reason -- a class,
and for a recoverable row a cell and expected exit.

``lock`` refuses an unfinished record, and a record whose oracle result
or output file changed, or whose rows differ from a fresh draft of its
oracle result other than by overrides; it writes a sha256 per record to
``adjudication.lock.json``. A locked record that changes, appears or
disappears needs a reason, which the lock keeps. ``verify`` re-checks
every record and prints the lock's own sha256, which the measurement
reports alongside its counts (the lock lives in the gitignored local
root, so that hash is what ties a published number to its labels).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .oracle import CLASSES, SEVERITY, FileResult

# Severity of a verifier exit, worst last: clean, cannot certify, leak.
EXIT_ORDER = {0: 0, 2: 1, 1: 2}


@dataclass(frozen=True)
class Expectation:
    cell: str           # COVERAGE.md cell
    case: str           # the case library's case for that cell
    exit: int           # that case's `expected` exit (what a correct verifier gives)
    note: str = ""
    provisional: bool = False   # no case of its own: listed for the owner's review
    known_gap: bool = False     # today's tool is known to get it wrong (the case's known_gap)


def _e(cell: str, case: str, exit_: int, note: str = "", *, provisional: bool = False,
       known_gap: bool = False) -> Expectation:
    return Expectation(cell, case, exit_, note, provisional, known_gap)


_FONT_LIVE = _e("live.font", "page.visible-embedded-font", 1)
_FONT_ORPHANED = _e("orphaned.font", "leftover.deleted-page-embedded-font", 2)
_FONT_SUPERSEDED = _e("superseded.font", "revision.raw-rewritten-font", 2)
_PLAIN_SUPERSEDED = _e("superseded.plain", "revision.rewritten-stream", 1)
_UNINDEXED = _e("unindexed.plain", "file.after-final-eof", 1, known_gap=True)
_ANNOT = _e("annot-fields.plain", "document.annotation-classic", 1)
_ANNOT_ORPHANED = _e("orphaned.plain", "revision.deleted-annotation", 1)

# (location code, where the oracle found it) -> what a correct verifier
# gives. "where" is the storage of the counting evidence: "live",
# "orphaned" (an object nothing references any more, such as a redacted
# page's old content stream), "superseded" (replaced by an incremental
# update), "unindexed" (after the final %%EOF), or for pixels
# "pixels-live" / "pixels-orphaned". Owner decision 7 (2026-10-12):
# approved, revisited with this PR; provisional rows have no case of
# their own and are listed for that review.
EXPECTED: dict[tuple[str, str], Expectation] = {
    # Drawn text. Chrome's is font-coded (Identity-H); reportlab's and
    # Quartz's use plain codes (see PLAIN_TEXT_PRODUCERS).
    ("T1", "live"): _FONT_LIVE,
    ("T2", "live"): _FONT_LIVE,
    ("T3a", "live"): _e("match.line-wrap", "layout.raw-line-wrap", 2),
    ("T3b", "live"): _e("match.columns", "layout.two-column-wrap", 2),
    ("T3c", "live"): _e("match.page-break", "layout.raw-page-break", 1,
                        "nothing between the halves; with a header or footer between them "
                        "the cell is match.page-break-furniture (exit 2)"),
    ("Q1", "live"): _e("live.plain", "page.visible", 1),
    ("S2", "live"): _e("live.plain", "page.visible", 1,
                       "an OCR engine's invisible text layer (render mode 3, glyphless "
                       "font)", provisional=True),
    ("T1", "orphaned"): _FONT_ORPHANED,
    ("T2", "orphaned"): _FONT_ORPHANED,
    ("T3a", "orphaned"): _FONT_ORPHANED,
    ("T3b", "orphaned"): _FONT_ORPHANED,
    ("T3c", "orphaned"): _FONT_ORPHANED,
    ("Q1", "orphaned"): _e("orphaned.plain", "leftover.redacted-no-gc", 1),
    ("S2", "orphaned"): _e("orphaned.font", "leftover.deleted-page-embedded-font", 2,
                           "tesseract's glyphless font", provisional=True),
    ("T1", "superseded"): _FONT_SUPERSEDED,
    ("T2", "superseded"): _FONT_SUPERSEDED,
    ("Q1", "superseded"): _PLAIN_SUPERSEDED,
    ("T1", "unindexed"): _UNINDEXED,
    ("Q1", "unindexed"): _UNINDEXED,
    # Strings.
    ("F1", "live"): _e("annot-fields.plain", "document.form-ssn", 1),
    ("F2", "live"): _e("annot-fields.plain", "document.form-ssn", 1,
                       "a comb field", provisional=True),
    ("A1", "live"): _ANNOT, ("A1x", "live"): _ANNOT, ("A2", "live"): _ANNOT,
    ("A3", "live"): _ANNOT,
    ("A4", "live"): _e("annot-fields.plain", "document.annotation-classic", 1,
                       "a link's URI", provisional=True),
    ("O1", "live"): _e("annot-fields.plain", "document.annotation-classic", 1,
                       "an outline title", provisional=True),
    ("M1", "live"): _e("metadata.plain", "document.title", 1),
    ("M2", "live"): _e("metadata.plain", "document.title", 1, "a custom Info key",
                       provisional=True),
    ("M3", "live"): _e("metadata.plain", "document.xmp-title", 1),
    ("E1", "live"): _e("attachment.plain", "attachment.raw-notes", 2),
    ("M1", "orphaned"): _e("orphaned.plain", "leftover.deleted-page", 1,
                           "an orphaned Info dictionary", provisional=True),
    ("M2", "orphaned"): _e("orphaned.plain", "leftover.deleted-page", 1,
                           "an orphaned Info dictionary", provisional=True),
    ("M3", "orphaned"): _e("leftover-xmp.plain", "leftover.xmp", 1),
    ("F1", "orphaned"): _ANNOT_ORPHANED, ("F2", "orphaned"): _ANNOT_ORPHANED,
    ("A1", "orphaned"): _ANNOT_ORPHANED, ("A1x", "orphaned"): _ANNOT_ORPHANED,
    ("A2", "orphaned"): _ANNOT_ORPHANED, ("A3", "orphaned"): _ANNOT_ORPHANED,
    ("A4", "orphaned"): _ANNOT_ORPHANED,
    ("O1", "orphaned"): _e("orphaned.plain", "leftover.deleted-page", 1, provisional=True),
    ("E1", "orphaned"): _e("orphaned-attachment.plain", "leftover.attachment-text", 2),
    # Pixels: OCR reads them, a hard finding where the page draws them.
    ("S1", "pixels-live"): _e("live.pixels", "page.scan-image", 1),
    ("S2", "pixels-live"): _e("live.pixels", "page.scan-image", 1),
    ("I1", "pixels-live"): _e("live.pixels", "page.raw-pixels", 1),
    ("S1", "pixels-orphaned"): _e("orphaned.pixels", "leftover.inline-image", 2),
    ("S2", "pixels-orphaned"): _e("orphaned.pixels", "leftover.inline-image", 2),
    ("I1", "pixels-orphaned"): _e("orphaned.pixels", "leftover.inline-image", 2),
    ("P1", "pixels-live"): _e("thumbnail.pixels", "page.thumb-image", 1, known_gap=True),
    ("P1", "pixels-orphaned"): _e("thumbnail.pixels", "page.thumb-image", 1, known_gap=True),
}

# Producers whose drawn text uses plain (simple-font) codes: their page
# text maps as Q1 does, not as Chrome's font-coded text.
PLAIN_TEXT_PRODUCERS = {"quartz", "quartz+pikepdf", "reportlab"}
_STORAGE_ORDER = ("live", "orphaned", "superseded", "unindexed")


def where(row: dict[str, Any], encoding: str) -> str:
    """The storage of a row's counting evidence: the most current one
    found; "unknown" when no counting evidence says (the adjudicator
    decides)."""
    storages = {e["storage"] for e in row["evidence"] if e["counts"] and e["storage"]}
    if encoding == "pixels":
        if any(e["channel"] == "V" and e["how"] == "whole" for e in row["evidence"]):
            return "pixels-live"
        return "pixels-orphaned" if storages == {"orphaned"} else (
            "pixels-live" if "live" in storages else "unknown")
    for s in _STORAGE_ORDER:
        if s in storages:
            return s
    return "unknown"


def expectation(loc: str, where_: str, producer: str) -> Expectation | None:
    if loc in ("T1", "T2") and producer in PLAIN_TEXT_PRODUCERS:
        loc = "Q1"
    return EXPECTED.get((loc, where_))


def worst_exit(exits: list[int]) -> int:
    return max(exits, key=lambda e: EXIT_ORDER[e]) if exits else 0


_DRAFT_FIELDS = ("class", "uncertain", "evidence", "where", "cell", "case", "expected_exit",
                 "provisional", "known_gap", "disagreement", "secret", "loc")


def draft(result: FileResult, truth_instances: list[dict[str, Any]], oracle_sha: str,
          producer: str, stem: str) -> dict[str, Any]:
    """An adjudication record to be completed and locked."""
    rows = []
    for r in result.to_dict()["instances"]:
        base = truth_instances[r["index"]] if r["index"] >= 0 else None
        encoding = base["encoding"] if base else "?"
        row: dict[str, Any] = {"index": r["index"], "secret": r["secret"], "loc": r["loc"],
                               "class": r["cls"], "uncertain": r["uncertain"],
                               "evidence": r["evidence"], "override": None,
                               "confirmed": None}
        if base is None:
            row["unattributed"] = True
        if r["cls"] != "removed":
            w = where(r, encoding) if base else "unknown"
            exp = expectation(r["loc"], w, producer) if base else None
            row.update({"where": w, "cell": exp.cell if exp else None,
                        "case": exp.case if exp else None,
                        "expected_exit": exp.exit if exp else None,
                        "provisional": exp.provisional if exp else None,
                        "known_gap": exp.known_gap if exp else None,
                        "expectation_note": exp.note if exp else
                        "no mapping: the adjudicator assigns a cell and expected exit"})
        if base is not None and encoding == "page-text":
            channels = {e["channel"] for e in r["evidence"]
                        if e["how"] == "whole" and e["counts"] and e["storage"] == "live"}
            if ("R3" in channels) != ("V" in channels):
                row["disagreement"] = ("live text read by pdfminer but not seen"
                                       if "R3" in channels else
                                       "seen but not in pdfminer's live text")
        rows.append(row)
    return {"output": result.output, "sha256": result.sha256, "doc": result.doc,
            "stem": stem, "method": result.method, "oracle_sha256": oracle_sha, "adjudicator": None,
            "date": None, "instances": rows, "notes": "", "secrets": result.secrets,
            "truth": result.truth}


def finalize_record(record: dict[str, Any], name: str = "") -> dict[str, Any]:
    """Recompute the per-secret classes, the file's truth and its expected
    exit from the rows (overrides applied). Refuses an unfinished
    record."""
    label = f"{name}: " if name else ""
    problems = []
    if not record.get("adjudicator"):
        problems.append("no adjudicator")
    per_secret: dict[str, str] = {}
    exits: list[int] = []
    for row in record["instances"]:
        ov = row.get("override") or {}
        tag = f"row {row['index']} ({row['secret']} {row['loc']})"
        if ov:
            if not ov.get("reason"):
                problems.append(f"{tag}: an override needs a reason")
            if ov.get("class", row["class"]) not in CLASSES:
                problems.append(f"{tag}: unknown class {ov.get('class')!r}")
                continue
        cls = ov.get("class", row["class"])
        flagged = row.get("uncertain") or row.get("disagreement") or row.get("unattributed") \
            or (cls != "removed" and row.get("where") == "unknown")
        if flagged and not ov and not row.get("confirmed"):
            problems.append(f"{tag} unresolved (confirm with a note, or override)")
        prev = per_secret.get(row["secret"], "removed")
        per_secret[row["secret"]] = max(prev, cls, key=lambda c: SEVERITY[c])
        if cls != "removed":
            exit_ = ov.get("expected_exit", row.get("expected_exit"))
            if exit_ not in EXIT_ORDER:
                problems.append(f"{tag}: no expected exit (an override must give cell and "
                                "expected_exit)")
            else:
                exits.append(exit_)
    if problems:
        raise ValueError(label + "; ".join(problems))
    record = dict(record)
    record["secrets"] = dict(sorted(per_secret.items()))
    record["truth"] = "leak" if any(c != "removed" for c in per_secret.values()) else "clean"
    record["expected_exit"] = worst_exit(exits) if record["truth"] == "leak" else 0
    return record


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _against_oracle(record: dict[str, Any], root: Path, set_name: str, name: str) -> list[str]:
    """Problems: the output or oracle file changed, or a row differs from a
    fresh draft of the oracle result other than through an override."""
    problems = []
    out = root / record["output"]
    if not out.exists() or _sha(out) != record["sha256"]:
        problems.append(f"{name}: the output file changed or is missing")
    oracle_path = root / "oracle" / set_name / record["method"] / f"{record['stem']}.json"
    if not oracle_path.exists() or _sha(oracle_path) != record["oracle_sha256"]:
        problems.append(f"{name}: its oracle result changed or is missing: re-draft")
        return problems
    truth = json.loads((root / set_name / f"{record['stem']}.truth.json").read_text())
    fresh = draft(FileResult.from_dict(json.loads(oracle_path.read_text())),
                  truth["instances"], record["oracle_sha256"], truth["producer"],
                  record["stem"])
    fresh_rows = {r["index"]: r for r in fresh["instances"]}
    rows = {r["index"]: r for r in record["instances"]}
    if set(rows) != set(fresh_rows):
        problems.append(f"{name}: rows added or removed by hand")
    for idx, row in rows.items():
        f = fresh_rows.get(idx, {})
        changed = [k for k in _DRAFT_FIELDS if row.get(k) != f.get(k)]
        if changed:
            problems.append(f"{name}: row {idx} edited by hand ({', '.join(changed)}): use an "
                            "override with a reason")
    return problems


def lock(root: Path, set_name: str, *, reason: str = "") -> tuple[Path, str]:
    """Finalise every record of *set_name* and write the lock; returns
    the lock's path and its own sha256."""
    adj_dir = root / "adjudication" / set_name
    lock_path = adj_dir / "adjudication.lock.json"
    old = json.loads(lock_path.read_text()) if lock_path.exists() else {"records": {},
                                                                        "relocks": []}
    records: dict[str, str] = {}
    problems: list[str] = []
    finals: dict[Path, dict[str, Any]] = {}
    for path in sorted(adj_dir.rglob("*.json")):
        if path == lock_path:
            continue
        rel = str(path.relative_to(adj_dir))
        rec = json.loads(path.read_text())
        problems += _against_oracle(rec, root, set_name, rel)
        try:
            finals[path] = finalize_record(rec, rel)
        except ValueError as exc:
            problems.append(str(exc))
    if problems:
        raise ValueError("\n".join(problems))
    for path, final in finals.items():
        if final != json.loads(path.read_text()):
            path.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
        records[str(path.relative_to(adj_dir))] = _sha(path)
    changed = sorted(r for r in set(old["records"]) | set(records)
                     if old["records"].get(r) != records.get(r) and r in old["records"])
    if changed and not reason:
        raise ValueError(f"locked records changed or removed ({', '.join(changed)}): "
                         "give a reason")
    relocks = old.get("relocks", [])
    if changed:
        relocks.append({"date": datetime.date.today().isoformat(), "records": changed,
                        "reason": reason})
    lock_path.write_text(json.dumps({"records": records, "relocks": relocks}, indent=2,
                                    sort_keys=True) + "\n")
    return lock_path, _sha(lock_path)


def verify(root: Path, set_name: str) -> tuple[list[str], str | None]:
    """Problems with the lock (a record missing, added or changed since,
    or no longer matching its oracle result and output), and the lock's
    own sha256."""
    adj_dir = root / "adjudication" / set_name
    lock_path = adj_dir / "adjudication.lock.json"
    if not lock_path.exists():
        return ["no adjudication.lock.json"], None
    locked = json.loads(lock_path.read_text())["records"]
    present = {str(p.relative_to(adj_dir)): p for p in adj_dir.rglob("*.json")
               if p != lock_path}
    problems = [f"missing: {r}" for r in sorted(set(locked) - set(present))]
    problems += [f"not locked: {r}" for r in sorted(set(present) - set(locked))]
    for r in sorted(set(locked) & set(present)):
        if locked[r] != _sha(present[r]):
            problems.append(f"changed since the lock: {r}")
        else:
            problems += _against_oracle(json.loads(present[r].read_text()), root, set_name, r)
    return problems, _sha(lock_path)
