"""`scorecard redactor-set run`: the verifier measured on the real-redactor
set (eval/README.md, "The real-redactor set"; operating plan milestone U,
items U2 and U3).

The set's outputs (a redaction tool run as-is, method m1, and re-saved
with garbage collection, method m2) each carry an adjudication record,
frozen under `adjudication.lock.json` before the verifier ever ran on
them (`redactor_set.adjudicate`). This module runs the verifier's CLI on
every locked output and compares, per method:

- per file (`metrics.compute_metrics`): a silent miss is a leaking file
  that exits 0; a downgrade, expected 1 and got 2;
- per instance (U2): every planted instance the adjudication left
  recoverable (partial residue included, owner decision 2026-10-14) must
  be named by a finding, or for an expected exit 2 at least a warning,
  that carries one of its rules and a storage class it can sit in. A
  file that exits non-zero for another secret does not excuse it;
- U3: of the m2 outputs whose truth is clean (the clean control, which
  had nothing to redact, apart), the share that exits 0, and the warning
  codes behind every exit 2.

Only counts reach stdout and `--json`. Per-file rows go to
`<root>/results/<date>.json`, inside the gitignored local root.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .metrics import CaseMetricRow, compute_metrics
from .runner import CliResult, current_python, error_code, run_cli

REDACTOR_SET_DIR = Path(__file__).resolve().parent / "redactor_set_local"
METHODS = ("m1", "m2")

# Where the adjudicated evidence sits -> the verifier storage classes
# (redaction_verifier.model.STORAGE_CLASSES) a report may give it.
# "orphaned" in the oracle means an object nothing references; the
# verifier calls that "orphaned" when it trusts its reachability walk and
# "unreferenced" when not. After the final %%EOF ("unindexed") the
# verifier has no reachability to trust.
STORAGES: dict[str, frozenset[str]] = {
    "live": frozenset({"live"}),
    "orphaned": frozenset({"orphaned", "unreferenced"}),
    "superseded": frozenset({"superseded"}),
    "unindexed": frozenset({"unreferenced", "orphaned"}),
    "pixels-live": frozenset({"live"}),
    "pixels-orphaned": frozenset({"orphaned", "unreferenced"}),
}

# Instance outcomes, best first.
HIT = "hit"                      # a finding (or, where review is expected, a warning) names it
DOWNGRADED = "downgraded"        # expected a finding; only a warning names it
AMBIGUOUS = "ambiguous"          # named only by occurrences other instances took
FLAGGED = "flagged"              # caught by a ⚑ cell's place warning (owner, 2026-10-15)
LISTED_GAP = "listed-gap"        # a miss of a kind the owner accepted as a listed gap
MISS = "miss"                    # nothing names it with its rule and storage
OUTCOMES = (HIT, FLAGGED, DOWNGRADED, AMBIGUOUS, LISTED_GAP, MISS)
UNMEASURED = "unmeasured"        # its output got no verdict (the run is invalid)

# Misses the owner accepted explicitly as listed gaps (2026-10-15), with
# README guidance to configure every written form of a name: they are
# counted and listed, and do not block U2. A miss of any other kind does.
LISTED_GAPS: dict[str, str] = {
    "name-variant": "a name written in a form other than the configured one (word order, "
                    "case): the verifier matches the configured value",
    "name-fragment": "a piece of a name (a whole word of it, in an email address, say): the "
                     "verifier matches whole values",
}


def listed_gap(loc: str, cls: str, kind: str) -> str | None:
    """Which owner-accepted listed gap a missed instance is, if any."""
    if kind != "person name":
        return None
    if cls == "partial":
        return "name-fragment"
    if loc == "T2":
        return "name-variant"
    return None


def storage_of(row: Mapping[str, Any]) -> str:
    """The storage of a recoverable row's evidence: the drafted one, an
    adjudicator's override, or (for a row the oracle could not place) the
    one its cell names."""
    override = row.get("override") or {}
    where = override.get("where") or row.get("where") or "unknown"
    if where != "unknown":
        return str(where)
    cell = str(override.get("cell") or row.get("cell") or "")
    head = cell.split(".")[0]
    if head.startswith(("orphaned", "leftover")):
        return "orphaned"
    if head in ("superseded", "unindexed"):
        return head
    if head in ("live", "metadata", "annot-fields", "attachment", "match", "thumbnail"):
        return "live"
    return "unknown"


def load_rules(path: Path) -> tuple[list[str], list[str]]:
    """The exact values and patterns of a redactor-style YAML rules file,
    as strings (never YAML-coerced), in file order."""
    import yaml

    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or {}
    return list(data.get("exact_values") or []), list(data.get("patterns") or [])


def rules_for(secret_value: str, written: str | None, exact: list[str],
              patterns: list[str]) -> frozenset[str]:
    """The verifier rule names that can report an instance: the exact
    value configured for its secret, and every pattern that matches the
    string as written (or the configured value, when the oracle found it
    somewhere no planted instance was written)."""
    names = {f"exact_values[{i}]" for i, v in enumerate(exact) if v == secret_value}
    text = written if written is not None else secret_value
    names |= {f"patterns[{i}]" for i, p in enumerate(patterns)
              if re.search(p, text, re.IGNORECASE)}
    return frozenset(names)


@dataclass
class InstanceScore:
    secret: str
    loc: str
    cls: str
    cell: str | None
    storage: str
    expected_exit: int
    known_gap: bool
    provisional: bool
    outcome: str = MISS
    named_by: list[str] = field(default_factory=list)
    gap: str | None = None


# ── Occurrences ─────────────────────────────────────────────────────────
# The verifier's report has one entry per (layer, rule, location): one
# occurrence of a value can give several (the Text and OCR layers on the
# same page; an exact rule and a pattern that both match it). So entries
# are grouped into occurrence units -- a place (storage plus page, else
# object, else location) and, at that place, a secret named by its exact
# rule, else "a pattern match" -- and each unit vouches for at most one
# planted instance. A pattern entry at a place where some secret's exact
# rule also fired belongs to that secret's occurrence, never to another
# secret's instance the pattern happens to fit.

def _place(e: Mapping[str, Any]) -> str:
    storage = e.get("storage")
    if e.get("page") is not None:
        return f"{storage}|p{e['page']}"
    if e.get("object") is not None:
        return f"{storage}|o{e['object']}"
    return f"{storage}|{e.get('layer')}:{e.get('location') or e.get('code')}"


@dataclass
class Unit:
    place: str
    storage: str
    page: int | None             # 1-based, as the report counts
    key: str                     # a secret id, or "pattern"
    rules: frozenset[str]        # the rules that fired for it here
    finding: bool                # a hard finding (else warnings only)


def occurrence_units(report: Mapping[str, Any], exact: list[str],
                     values: Mapping[str, str]) -> list[Unit]:
    secret_of = {f"exact_values[{i}]": sid for i, v in enumerate(exact)
                 for sid, val in values.items() if val == v}
    entries = [(e, True) for e in report.get("findings", []) if e.get("tier") == "hard"]
    entries += [(e, False) for e in report.get("warnings", [])]
    by_place: dict[str, list[tuple[Mapping[str, Any], bool]]] = {}
    for e, hard in entries:
        if e.get("rule") is not None:
            by_place.setdefault(_place(e), []).append((e, hard))
    units = []
    for place, group in sorted(by_place.items()):
        e0 = group[0][0]
        secrets = sorted({secret_of[str(e["rule"])] for e, _ in group
                          if str(e["rule"]) in secret_of})
        pattern_rules = frozenset(str(e["rule"]) for e, _ in group
                                  if str(e["rule"]).startswith("patterns["))
        for key in secrets or ["pattern"]:
            own = [(e, h) for e, h in group
                   if (key != "pattern" and secret_of.get(str(e["rule"])) == key)
                   or str(e["rule"]) in pattern_rules]
            units.append(Unit(place, str(e0.get("storage")), e0.get("page"), key,
                              frozenset(str(e["rule"]) for e, _ in own),
                              any(h for _, h in own)))
    return units


def _candidate(u: Unit, secret: str, rules: frozenset[str],
               storages: frozenset[str] | None, page: int | None) -> bool:
    if storages is not None and u.storage not in storages:
        return False
    if page is not None and u.page != page + 1:
        return False
    if u.key == "pattern":
        return bool(u.rules & rules)
    return u.key == secret and bool(u.rules & rules)


# ── ⚑ cells ─────────────────────────────────────────────────────────────
# In a ⚑ cell (found but unreadable) the verifier reports a place by
# design -- "N orphaned objects it cannot decode: objects 6, 9" -- with no
# rule. Such a warning is recorded as FLAGGED only when its code is the
# one for the cell's column, the instance expects exit 2 there, and the
# instance's object (from the oracle's evidence) is in the warning's
# object list. Owner decision 2026-10-15: under exactly these conditions a
# flagged instance counts as caught for U2 (not a miss), reported apart
# from hits.
PLACE_WARNINGS = {"font": "LEFTOVER_UNDECODABLE_TEXT", "pixels": "LEFTOVER_IMAGE",
                  "container": "LEFTOVER_CONTAINER"}
_OBJ_IN_EVIDENCE = re.compile(r"\bobj(?:ect)? (\d+)")
_OBJS_IN_MESSAGE = re.compile(r"\bobjects? ((?:\d+(?:, )?)+)")


def _flagged_column(cell: str | None) -> str | None:
    from caselib.cells import CELLS, parts

    if cell is None or cell not in CELLS or CELLS[cell].status != "flagged":
        return None
    return parts(cell)[1]


def instance_objects(row: Mapping[str, Any]) -> set[int]:
    return {int(m.group(1)) for e in row.get("evidence", []) if e.get("counts", True)
            for m in _OBJ_IN_EVIDENCE.finditer(str(e.get("where", "")))}


def _place_warning(w: Mapping[str, Any], code: str, storages: frozenset[str] | None,
                   objects: set[int]) -> bool:
    if w.get("code") != code or w.get("rule") is not None:
        return False
    if storages is None or w.get("storage") not in storages:
        return False
    if not objects:
        return False                 # the owner's condition: the instance's object is listed
    listed = {int(n) for m in _OBJS_IN_MESSAGE.finditer(str(w.get("message", "")))
              for n in m.group(1).split(", ") if n}
    return bool(listed & objects)


def score_instances(record: Mapping[str, Any], truth: Mapping[str, Any],
                    report: Mapping[str, Any], exact: list[str],
                    patterns: list[str], values: Mapping[str, str],
                    kinds: Mapping[str, str] | None = None) -> list[InstanceScore]:
    """Score every recoverable row of a locked, finalised adjudication
    record against the verifier's JSON report. *values* maps a secret id
    to its configured value, *kinds* to what it is (for LISTED_GAPS)."""
    units = occurrence_units(report, exact, values)
    warnings = list(report.get("warnings", []))
    scores: list[InstanceScore] = []
    cands: list[list[int]] = []
    places: list[list[str]] = []
    for row in record["instances"]:
        override = row.get("override") or {}
        cls = override.get("class", row["class"])
        if cls == "removed":
            continue
        index = row["index"]
        base = truth["instances"][index] if index >= 0 else None
        written = base["written"] if base else None
        rules = rules_for(values[row["secret"]], written, exact, patterns)
        storage = storage_of(row)
        storages = STORAGES.get(storage)  # None: unknown, any storage names it
        # Drawn content must be found on its own page.
        page = (base.get("page") if base and base.get("encoding") in ("page-text", "pixels")
                and storage in ("live", "pixels-live") else None)
        score = InstanceScore(
            secret=row["secret"], loc=row["loc"], cls=cls,
            cell=override.get("cell", row.get("cell")), storage=storage,
            expected_exit=int(override.get("expected_exit", row.get("expected_exit"))),
            known_gap=bool(row.get("known_gap")), provisional=bool(row.get("provisional")))
        mine = [k for k, u in enumerate(units)
                if _candidate(u, row["secret"], rules, storages, page)]
        # Prefer an occurrence a hard finding reports.
        mine.sort(key=lambda k: (not units[k].finding, k))
        column = _flagged_column(score.cell)
        place = []
        if column in PLACE_WARNINGS and score.expected_exit == 2:
            objects = instance_objects(row)
            place = sorted({str(w.get("message", ""))[:80] for w in warnings
                            if _place_warning(w, PLACE_WARNINGS[str(column)], storages,
                                              objects)})
        scores.append(score)
        cands.append(mine)
        places.append(place)

    # One occurrence vouches for at most one instance: the largest
    # one-to-one matching (augmenting paths) decides; an instance with
    # candidates it cannot cover is ambiguous, not a hit.
    owner: dict[int, int] = {}

    def augment(i: int, seen: set[int]) -> bool:
        for k in cands[i]:
            if k in seen:
                continue
            seen.add(k)
            if k not in owner or augment(owner[k], seen):
                owner[k] = i
                return True
        return False

    for i in range(len(scores)):
        if cands[i]:
            augment(i, set())
    matched = {i: k for k, i in owner.items()}
    for i, sc in enumerate(scores):
        if i in matched:
            u = units[matched[i]]
            sc.named_by = [f"{u.place} {u.key} {sorted(u.rules)}"]
            sc.outcome = HIT if (u.finding or sc.expected_exit == 2) else DOWNGRADED
        elif places[i]:
            sc.outcome, sc.named_by = FLAGGED, places[i]
        elif cands[i]:
            sc.outcome = AMBIGUOUS
        else:
            sc.gap = listed_gap(sc.loc, sc.cls, (kinds or {}).get(sc.secret, ""))
            if sc.gap:
                sc.outcome = LISTED_GAP
    return scores


@dataclass
class FileRun:
    record: dict[str, Any]
    truth: dict[str, Any]
    result: CliResult

    @property
    def method(self) -> str:
        return str(self.record["method"])

    @property
    def clean_control(self) -> bool:
        return bool(self.truth.get("clean_control"))


def metric_row(run: FileRun) -> CaseMetricRow:
    report = run.result.report
    return CaseMetricRow(
        case_id=f"{run.method}/{run.record['stem']}",
        truth=run.record["truth"],
        expected_exit=int(run.record["expected_exit"]),
        # The exit as reported: the set runs where every view is
        # available (run() refuses a report without OCR).
        actual_exit=None if run.result.crashed else int(report["exit_code"]),  # type: ignore[index]
        has_hard_finding=bool(report and any(f.get("tier") == "hard"
                                             for f in report.get("findings", []))),
        crashed=run.result.crashed,
        timed_out=run.result.timed_out,
        elapsed=run.result.elapsed,
        error_code=error_code(run.result),
    )


def load_locked(root: Path, set_name: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every locked adjudication record of *set_name* with its truth
    manifest; refuses a lock that does not verify."""
    from redactor_set import adjudicate

    problems, _ = adjudicate.verify(root, set_name)
    if problems:
        raise SystemExit("adjudication lock does not verify:\n  " + "\n  ".join(problems))
    adj_dir = root / "adjudication" / set_name
    out = []
    for path in sorted(adj_dir.rglob("*.json")):
        if path.name == "adjudication.lock.json":
            continue
        record = json.loads(path.read_text())
        truth = json.loads((root / set_name / f"{record['stem']}.truth.json").read_text())
        out.append((record, truth))
    return out


def lock_sha(root: Path, set_name: str) -> str:
    from redactor_set import adjudicate

    _, sha = adjudicate.verify(root, set_name)
    return sha or ""


def summarise(runs: list[FileRun], exact: list[str], patterns: list[str],
              values: Mapping[str, str], kinds: Mapping[str, str] | None = None,
              ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(counts for stdout and --json, per-file rows for the local root)."""
    counts: dict[str, Any] = {}
    detail: list[dict[str, Any]] = []
    for method in METHODS:
        mruns = [r for r in runs if r.method == method]
        if not mruns:
            continue
        rows = [metric_row(r) for r in mruns]
        file_metrics = compute_metrics(rows).to_jsonable()
        outcomes: Counter[str] = Counter()
        by_loc: dict[str, Counter[str]] = {}
        misses_by_cell: Counter[str] = Counter()
        known_gap_by_cell: Counter[str] = Counter()
        partial: Counter[str] = Counter()
        listed: Counter[str] = Counter()
        flagged_by_cell: Counter[str] = Counter()
        provisional = 0
        for run, row in zip(mruns, rows):
            scores = ([] if run.result.report is None else
                      score_instances(run.record, run.truth, run.result.report, exact,
                                      patterns, values, kinds))
            if run.result.report is None:
                # Never dropped: a run without a report leaves its
                # recoverable instances unmeasured, and counted.
                outcomes[UNMEASURED] += sum(
                    1 for r in run.record["instances"]
                    if (r.get("override") or {}).get("class", r["class"]) != "removed")
            for s in scores:
                outcomes[s.outcome] += 1
                by_loc.setdefault(s.loc, Counter())[s.outcome] += 1
                if s.outcome == LISTED_GAP:
                    listed[str(s.gap)] += 1
                elif s.outcome == FLAGGED:
                    flagged_by_cell[str(s.cell)] += 1
                elif s.outcome != HIT:
                    if s.known_gap:
                        known_gap_by_cell[str(s.cell)] += 1
                    else:
                        misses_by_cell[f"{s.cell} ({s.outcome})"] += 1
                if s.cls == "partial":
                    partial[s.outcome] += 1
                provisional += s.provisional
            detail.append({
                "method": method, "stem": run.record["stem"], "doc": run.record["doc"],
                "truth": row.truth, "expected_exit": row.expected_exit,
                "actual_exit": row.actual_exit, "crashed": row.crashed,
                "error_code": row.error_code,
                "warning_codes": sorted({w.get("code") for w in
                                         (run.result.report or {}).get("warnings", [])}),
                "instances": [s.__dict__ for s in scores],
            })
        counts[method] = {
            "files": file_metrics,
            "instances": {
                "recoverable": sum(outcomes.values()),
                **{o: outcomes[o] for o in OUTCOMES},
                UNMEASURED: outcomes[UNMEASURED],
                "by_location": {loc: dict(c) for loc, c in sorted(by_loc.items())},
                "not_hit_by_cell": dict(sorted(misses_by_cell.items())),
                "known_gap_not_hit_by_cell": dict(sorted(known_gap_by_cell.items())),
                "listed_gap_by_kind": dict(sorted(listed.items())),
                "flagged_by_cell": dict(sorted(flagged_by_cell.items())),
                "partial_residue": dict(partial),
                "provisional_mapping": provisional,
            },
        }
    counts["u3"] = u3(runs)
    return counts, detail


def u3(runs: list[FileRun]) -> dict[str, Any]:
    """Of the m2 outputs whose truth is clean, the share that exits 0;
    the clean control (nothing to redact) on its own line."""
    good = [r for r in runs if r.method == "m2" and r.record["truth"] == "clean"]
    measured = [r for r in good if not r.clean_control]
    exits = Counter(metric_row(r).actual_exit for r in measured)
    reasons: Counter[str] = Counter()
    for r in measured:
        if r.result.report and r.result.report["exit_code"] == 2:
            reasons.update({w.get("code") for w in r.result.report.get("warnings", [])})
    control = [metric_row(r).actual_exit for r in good if r.clean_control]
    n = len(measured)
    return {
        "n": n,
        "exit_0": exits[0],
        "exit_2": exits[2],
        "exit_1_false_hard": exits[1],
        "share_exit_0": round(exits[0] / n, 3) if n else None,
        "exit_2_warning_codes": dict(sorted(reasons.items())),
        "clean_control_exits": dict(Counter(control)),
    }


def run(root: Path, set_name: str, rules_path: Path, *, verify_path: Path, workdir: Path,
        timeout: float, workers: int, seeds: set[int] | None = None) -> list[FileRun]:
    """Run the verifier on every locked output (of *seeds*, when given)."""
    locked = [(rec, truth) for rec, truth in load_locked(root, set_name)
              if rec["method"] in METHODS and (seeds is None or truth.get("seed") in seeds)]
    python_exe = current_python()

    def one(item: tuple[dict[str, Any], dict[str, Any]]) -> FileRun:
        rec, truth = item
        case_id = f"{rec['method']}-{rec['sha256'][:16]}"
        result = run_cli(python_exe, verify_path, root / rec["output"], [], workdir, case_id,
                         timeout=timeout, secrets_file=rules_path)
        return FileRun(rec, truth, result)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, locked))


def unmeasured(runs: list[FileRun]) -> list[str]:
    """Why a run cannot be counted: a crash, a timeout, an error report,
    or a report made without OCR (pixels instances would be unmeasured).
    By method and position in the run only (no per-file identifier)."""
    problems = []
    for n, r in enumerate(runs):
        tag = f"{r.method} output #{n}"
        report = r.result.report
        if r.result.timed_out:
            problems.append(f"{tag}: timed out")
        elif r.result.crashed or report is None:
            problems.append(f"{tag}: crashed")
        elif report.get("error"):
            problems.append(f"{tag}: error {error_code(r.result)}")
        elif not report.get("environment", {}).get("ocr_available"):
            problems.append(f"{tag}: OCR unavailable")
    return problems


def render(counts: Mapping[str, Any], sha: str) -> str:
    lines = [f"adjudication lock sha256 {sha}; seeds {counts.get('seeds', 'all locked')}"
             + ("" if counts.get("valid", True) else
                f"; INVALID: {counts.get('unmeasured_outputs')} output(s) without a verdict")]
    for method in METHODS:
        if method not in counts:
            continue
        f, i = counts[method]["files"], counts[method]["instances"]
        lines.append(f"{method}: files {f['total']} (leak {f['leak_cases']}, clean "
                     f"{f['clean_cases']}); silent miss {f['silent_miss']}, downgrade "
                     f"{f['downgrade']}, false hard {f['false_hard']}")
        lines.append(f"  instances recoverable {i['recoverable']}: hit {i[HIT]}, flagged "
                     f"(caught by a ⚑ place warning) {i[FLAGGED]}, downgraded "
                     f"{i[DOWNGRADED]}, ambiguous {i[AMBIGUOUS]}, listed gap (owner-accepted) "
                     f"{i[LISTED_GAP]}, miss {i[MISS]}")
        for cell, n in i["not_hit_by_cell"].items():
            lines.append(f"    not hit: {cell}: {n}")
        for cell, n in i["known_gap_not_hit_by_cell"].items():
            lines.append(f"    not hit, known gap: {cell}: {n}")
        for kind, n in i["listed_gap_by_kind"].items():
            lines.append(f"    listed gap (owner-accepted 2026-10-15): {kind}: {n}")
        if i["partial_residue"]:
            lines.append(f"  partial residue: {i['partial_residue']}")
    u = counts["u3"]
    lines.append(f"U3: {u['exit_0']} of {u['n']} correctly redacted m2 outputs exit 0 "
                 f"(exit 2: {u['exit_2']}, exit 1: {u['exit_1_false_hard']}); exit-2 codes "
                 f"{u['exit_2_warning_codes']}; clean control exits {u['clean_control_exits']}")
    return "\n".join(lines)


def check_lock_history(root: Path, sha: str, reason: str, *, record: bool = True
                       ) -> str | None:
    """Every measurement appends the lock's sha256 to
    `results/lock-history.json`. A lock that differs from the last one
    measured -- relabelled after the verifier has run -- needs a reason,
    which the history keeps; returns the refusal, or None. (The local
    root can still be rewritten by hand: the published counts carry the
    sha256, which is what ties a number to its labels.)"""
    path = root / "results" / "lock-history.json"
    history = json.loads(path.read_text()) if path.exists() else []
    if history and history[-1]["lock_sha256"] != sha and not reason:
        return ("the adjudication lock changed since the last measurement "
                f"({history[-1]['lock_sha256'][:12]} -> {sha[:12]}): pass --lock-change-reason")
    if not record:
        return None
    history.append({"date": datetime.date.today().isoformat(), "lock_sha256": sha,
                    "reason": reason if history and history[-1]["lock_sha256"] != sha else ""})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2) + "\n")
    return None


def write_detail(root: Path, detail: list[dict[str, Any]], counts: Mapping[str, Any],
                 sha: str) -> Path:
    out = root / "results" / f"{datetime.date.today().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"lock_sha256": sha, "counts": counts, "files": detail},
                              indent=2, sort_keys=True, default=str) + "\n")
    return out


def persona_values() -> dict[str, str]:
    from redactor_set.persona import SECRETS

    return {s.id: s.value for s in SECRETS}


def persona_kinds() -> dict[str, str]:
    from redactor_set.persona import SECRETS

    return {s.id: s.kind for s in SECRETS}


def main(args: Any) -> int:
    from .corpus import ensure_local_only
    from .differential import default_workers
    from .refs import candidate_verify_path, repo_root

    root = ensure_local_only(args.root, allow_outside=args.allow_outside,
                             boundary=REDACTOR_SET_DIR, docs="The real-redactor set")
    rules_path = args.rules or root / "config" / "redact.yaml"
    locked = load_locked(root, args.set)        # refuses a lock that does not verify
    sha = lock_sha(root, args.set)
    refusal = check_lock_history(root, sha, args.lock_change_reason, record=False)
    if refusal:
        print(f"ERROR: {refusal}", file=sys.stderr)
        return 1
    del locked
    runs = run(root, args.set, rules_path, verify_path=candidate_verify_path(repo_root()),
               workdir=root / "results" / "verifier", timeout=args.timeout,
               workers=args.workers or default_workers(),
               seeds=set(args.seed) if args.seed else None)
    if not runs:
        print("ERROR: no locked outputs to measure.", file=sys.stderr)
        return 1
    exact, patterns = load_rules(rules_path)
    counts, detail = summarise(runs, exact, patterns, persona_values(), persona_kinds())
    problems = unmeasured(runs)
    counts["valid"] = not problems
    counts["unmeasured_outputs"] = len(problems)
    counts["seeds"] = sorted(args.seed) if args.seed else "all locked"
    print(render(counts, sha))
    path = write_detail(root, detail, counts, sha)
    print(f"per-file rows: {path.relative_to(root)} (local only)", file=sys.stderr)
    if problems:
        # No shareable counts from a run that did not measure every output.
        print("ERROR: not every output got a verdict (no --json written, not recorded "
              "in the lock history):\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    check_lock_history(root, sha, args.lock_change_reason, record=True)
    if args.json:
        args.json.write_text(json.dumps({"lock_sha256": sha, **counts}, indent=2,
                                        sort_keys=True) + "\n")
    return 0
