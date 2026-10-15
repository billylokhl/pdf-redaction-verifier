#!/usr/bin/env python3
"""Phase 3a-12: the unit budget (an ADR 0006 amendment draft) and the
orphan overlap (ADR 0007), on the real corpus.

Three steps, each re-runnable on its own; per-file records go to a
local-only directory (``--out``, by default under the gitignored
``eval/scorecard/real_corpus/``), keyed by SHA-256 prefix, never by name:

* ``units``: builds each manifest file's inventory (``build_inventory``,
  a fresh ``Budget``) and records its units -- every unit of the byte
  tiling by ``UnitKind`` (object-stream members and slack included), the
  SYNTHESIZED units of every revision (3a-7d: charged to ``max_units``
  today, the only units the budget is charged for yet), what the budget
  charged, and the quantities 3b and 4a add on top that the inventory
  already determines: the page views (one per page of the current
  revision; REDESIGN §4's views) and UNINDEXED ranges (each a unit,
  REDESIGN's decoder registry), plus a proxy for 4a's (stream, context)
  units -- each revision's DRAWABLE streams, summed over revisions. The
  same pass records each file's 3a-7e exit-2 classes (scorecard
  ``inventory.measure``: orphans per body and per current revision, hint
  streams, XMP images, JavaScript, synthesized annotations, output
  intents) and whether the inventory flags it.
* ``cli``: runs today's shipped CLI (``verify.py``, through the
  scorecard's ``run_cli``) once on every manifest file with a fabricated,
  non-sequential value rule, keeping each report.
* ``report``: joins the two and prints one JSON object: unit
  distributions (p50/p90/p99/max, per kind, per byte), and for each 7e
  class and their union which files already exit 2 today (and why, by
  warning code; a qpdf exit status is its own reason) and which would
  newly exit 2; then the review rate today and after, on all files and on
  text-bearing ones, with and without today's qpdf exit-3 warnings.

Aggregate numbers only are printed; ``--out`` holds per-file records
(hash prefixes, counts, warning codes) and the CLI's reports, which carry
the target's file name: keep it local, like the manifest.

    PYTHONPATH=eval:. python eval/spikes/unit_budget.py units \\
        --root eval/scorecard/real_corpus/links --workers 6
    PYTHONPATH=eval:. python eval/spikes/unit_budget.py cli \\
        --root eval/scorecard/real_corpus/links --workers 6
    PYTHONPATH=eval:. python eval/spikes/unit_budget.py report
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT))

from redaction_verifier.budget import Budget  # noqa: E402
from redaction_verifier.inventory import build_inventory  # noqa: E402
from redaction_verifier.ledger import UnitKind  # noqa: E402
from scorecard import corpus as corpus_mod  # noqa: E402

DEFAULT_OUT = corpus_mod.REAL_CORPUS_DIR / "unit-budget"
# A fabricated value no corpus file holds and with no digits in sequence
# (eval/README.md, "Use a non-sequential value in --secrets").
RULES = [{"name": "Fabricated SSN", "value": "666-51-4073"}]

# The 3a-7e exit-2 classes (eval/scorecard/inventory_gate.py's
# _EXIT_2_CLASSES), plus orphans in the current revision's sense.
CLASSES = (
    ("orphan", "bodies_orphaned"),
    ("orphan_current", "orphaned_current"),
    ("hint_stream", "structural_hint_stream"),
    ("xmp_image", "xmp_image_bodies"),
    ("javascript", "structural_javascript"),
    ("synthesized", "synthesized_current"),
    ("output_intent", "structural_output_intent"),
)
# The union is the 7e briefing's: orphan_current is reported, not joined.
UNION = ("orphan", "hint_stream", "xmp_image", "javascript", "synthesized", "output_intent")


# ── units ────────────────────────────────────────────────────────────────
def measure_units(path: str) -> dict[str, Any]:
    from scorecard import inventory as inv_mod

    data = Path(path).read_bytes()
    out: dict[str, Any] = {"size": len(data)}
    started = time.monotonic()
    try:
        budget = Budget(file_size=len(data))
        inv = build_inventory(data, budget=budget)
    except Exception as exc:  # recorded, never fatal
        out["error"] = type(exc).__name__
        return out
    out["build_seconds"] = round(time.monotonic() - started, 3)
    kinds = Counter(unit.ref.kind.value for unit in inv.units)
    out["units_by_kind"] = dict(kinds)
    out["tiling_units"] = len(inv.units)
    out["unindexed_regions"] = sum(1 for r in inv.tiling.regions
                                   if r.kind is UnitKind.UNINDEXED)
    out["synthesized_units"] = sum(len(units) for units in inv.synthesized)
    out["budget_units"] = budget.units
    out["revisions"] = inv.revisions
    out["pages_current"] = len(inv.pages[0] or ()) if inv.pages else 0
    out["drawable_pairs"] = sum(len(found.drawable) for found in inv.labels
                                if found is not None)
    out["flags"] = sorted({flag.reason.value for flag in inv.flags})
    try:
        measures = inv_mod.measure(data, inv)
    except Exception as exc:
        out["measure_error"] = type(exc).__name__
        measures = {}
    out["classes"] = {name: measures.get(key, 0) for name, key in CLASSES}
    return out


# ── cli ──────────────────────────────────────────────────────────────────
def run_one(path: Path, workdir: Path, case_id: str, timeout: float) -> dict[str, Any]:
    from scorecard.runner import current_python, run_cli

    result = run_cli(current_python(), ROOT / "verify.py", path, RULES, workdir, case_id,
                     timeout=timeout)
    report = result.report
    record: dict[str, Any] = {"crashed": result.crashed}
    if report is None:
        return record
    record["exit"] = report.get("exit_code")
    error = report.get("error")
    record["error"] = error.get("code") if isinstance(error, dict) else None
    reasons = set()
    for warning in report.get("warnings", []):
        code = warning.get("code")
        if code == "TOOL_EXIT_NONZERO":
            code = f"{code}:{warning.get('tool')}:{warning.get('returncode')}"
        reasons.add(code)
    record["reasons"] = sorted(reasons)
    record["hard"] = sum(1 for f in report.get("findings", []) if f.get("tier") == "hard")
    return record


# ── report ───────────────────────────────────────────────────────────────
def percentiles(values: list[float], digits: int = 0) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def at(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)], digits)
    return {"n": len(ordered), "p50": at(0.5), "p90": at(0.9), "p99": at(0.99),
            "max": round(ordered[-1], digits)}


QPDF_3 = "TOOL_EXIT_NONZERO:qpdf:3"


def exits_2(cli: dict[str, Any], *, without_qpdf_3: bool = False) -> bool:
    """Today's exit 2 (a file with no verdict counts: it is not certified),
    and the same with today's qpdf exit-3 warnings taken out (a file whose
    only reasons they are would exit 0, had it no hard finding)."""
    if cli.get("exit") is None:
        return True
    if cli["exit"] != 2:
        return False
    if not without_qpdf_3 or cli.get("error"):
        return True
    return any(reason != QPDF_3 for reason in cli.get("reasons", []))


def overlap(files: list[str], cli: dict[str, dict[str, Any]]) -> dict[str, Any]:
    already = [h for h in files if exits_2(cli[h])]
    reasons: Counter[str] = Counter()
    only: Counter[str] = Counter()
    for h in already:
        found = cli[h].get("reasons", [])
        if cli[h].get("error"):
            found = [f"error:{cli[h]['error']}"]
        elif cli[h].get("exit") is None:
            found = ["no_verdict"]
        reasons.update(found)
        if len(found) == 1:
            only[found[0]] += 1
    return {
        "files": len(files),
        "already_exit_2": len(already),
        "already_exit_2_but_qpdf_3": sum(1 for h in already if not exits_2(cli[h],
                                                                         without_qpdf_3=True)),
        "exit_1_today": sum(1 for h in files if cli[h].get("exit") == 1),
        "newly_exit_2": sum(1 for h in files if cli[h].get("exit") == 0),
        "already_exit_2_by_reason": dict(reasons.most_common()),
        "already_exit_2_single_reason": dict(only.most_common()),
    }


def review_rates(population: list[str], added: set[str],
                 cli: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"files": len(population)}
    for name, without in (("with_qpdf_3", False), ("without_qpdf_3", True)):
        today = {h for h in population if exits_2(cli[h], without_qpdf_3=without)}
        after = today | {h for h in population if h in added and cli[h].get("exit") != 1}
        n = max(len(population), 1)
        out[name] = {"today": len(today), "today_rate": round(100 * len(today) / n, 1),
                     "after": len(after), "after_rate": round(100 * len(after) / n, 1),
                     "newly": len(after - today),
                     "points_added": round(100 * (len(after) - len(today)) / n, 1)}
    return out


def report(out_dir: Path, manifest: list[Any]) -> dict[str, Any]:
    # Records are kept once per content (SHA-256 prefix); everything below
    # counts manifest entries, as the corpus gate does -- a file present
    # under two paths counts twice, here as in the 3a-7e briefing.
    by_hash_units = _load(out_dir / "units.jsonl")
    by_hash_cli = _load(out_dir / "cli.jsonl")
    hashes = [str(i) for i in range(len(manifest))]
    units = {str(i): by_hash_units[e.sha256[:16]] for i, e in enumerate(manifest)
             if e.sha256[:16] in by_hash_units}
    cli = {str(i): by_hash_cli[e.sha256[:16]] for i, e in enumerate(manifest)
           if e.sha256[:16] in by_hash_cli}
    text = {str(i) for i, e in enumerate(manifest) if e.text_bearing}
    built = [h for h in hashes if h in units and "error" not in units[h]]
    result: dict[str, Any] = {"manifest": len(hashes), "built": len(built),
                              "distinct_contents": len({e.sha256 for e in manifest}),
                              "text_bearing": len(text),
                              "build_errors": sum(1 for h in hashes if "error" in units.get(h, {}))}

    # Units.
    def dist(rows: list[str], key: Any, digits: int = 0) -> dict[str, float]:
        return percentiles([key(units[h]) for h in rows], digits)

    def total(r: dict[str, Any]) -> int:
        return r["tiling_units"] + r["synthesized_units"]

    def with_3b(r: dict[str, Any]) -> int:
        return total(r) + r["pages_current"]

    def with_4a(r: dict[str, Any]) -> int:
        return with_3b(r) + r["drawable_pairs"]

    kinds = sorted({k for h in built for k in units[h]["units_by_kind"]})
    for label, rows in (("all", built),
                        ("unflagged", [h for h in built if not units[h]["flags"]])):
        section: dict[str, Any] = {
            "tiling_units": dist(rows, lambda r: r["tiling_units"]),
            "by_kind": {k: dist(rows, lambda r, k=k: r["units_by_kind"].get(k, 0)) for k in kinds},
            "synthesized_units": dist(rows, lambda r: r["synthesized_units"]),
            "budget_units_charged": dist(rows, lambda r: r["budget_units"]),
            "units_today": dist(rows, total),
            "pages_current": dist(rows, lambda r: r["pages_current"]),
            "unindexed_regions": dist(rows, lambda r: r["unindexed_regions"]),
            "units_with_views": dist(rows, with_3b),
            "drawable_pairs": dist(rows, lambda r: r["drawable_pairs"]),
            "units_with_views_and_drawable_pairs": dist(rows, with_4a),
            "units_per_kib": dist(rows, lambda r: 1024 * total(r) / max(r["size"], 1), 2),
            "units_with_views_and_drawable_pairs_per_kib":
                dist(rows, lambda r: 1024 * with_4a(r) / max(r["size"], 1), 2),
        }
        largest = max(rows, key=lambda h: with_4a(units[h])) if rows else None
        if largest is not None:
            r = units[largest]
            section["largest"] = {"size": r["size"], "tiling_units": r["tiling_units"],
                                  "synthesized_units": r["synthesized_units"],
                                  "pages_current": r["pages_current"],
                                  "drawable_pairs": r["drawable_pairs"],
                                  "revisions": r["revisions"], "flagged": bool(r["flags"])}
        result[f"units_{label}"] = section

    # Overlap with today's CLI.
    scanned = [h for h in built if h in cli]
    result["cli"] = {
        "scanned": len(scanned),
        "exit": dict(Counter(str(cli[h].get("exit")) for h in scanned)),
        "crashed": sum(1 for h in scanned if cli[h].get("crashed")),
        "hard_findings_files": sum(1 for h in scanned if cli[h].get("hard")),
    }
    flagged = [h for h in scanned if units[h]["flags"]]
    unflagged = [h for h in scanned if not units[h]["flags"]]
    members = {name: [h for h in unflagged if units[h]["classes"].get(name, 0) > 0]
               for name, _ in CLASSES}
    union = sorted({h for name in UNION for h in members[name]})
    table = {name: overlap(rows, cli) for name, rows in members.items()}
    table["union_unflagged"] = overlap(union, cli)
    table["inventory_flagged"] = overlap(flagged, cli)
    table["union_with_inventory_flagged"] = overlap(sorted(set(union) | set(flagged)), cli)
    for name in members:
        rows = [h for h in members[name] if h in text]
        table[f"{name}_text_bearing"] = overlap(rows, cli)
    table["union_unflagged_text_bearing"] = overlap([h for h in union if h in text], cli)
    result["overlap"] = table

    # Today's reasons, over every scanned file that exits 2.
    today_reasons: Counter[str] = Counter()
    for h in scanned:
        if exits_2(cli[h]):
            today_reasons.update(cli[h].get("reasons", []) or
                                 [f"error:{cli[h].get('error') or 'no_verdict'}"])
    result["today_exit_2_by_reason"] = dict(today_reasons.most_common())

    # Net review rate: today's exit-2 set against it joined with the 7e set.
    added_unflagged = set(union)
    added_all = added_unflagged | set(flagged)
    text_rows = [h for h in scanned if h in text]
    result["review_rate"] = {
        "all_files_7e_unflagged": review_rates(scanned, added_unflagged, cli),
        "all_files_7e_and_inventory_flags": review_rates(scanned, added_all, cli),
        "text_bearing_7e_unflagged": review_rates(text_rows, added_unflagged, cli),
        "text_bearing_7e_and_inventory_flags": review_rates(text_rows, added_all, cli),
    }
    return result


# ── plumbing ─────────────────────────────────────────────────────────────
def _load(path: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            found[row.pop("id")] = row
    return found


def _append(path: Path, key: str, row: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps({"id": key, **row}, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=("units", "cli", "report"))
    parser.add_argument("--root", type=Path, help="the corpus root (units, cli)")
    parser.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--allow-outside", action="store_true",
                        help="--out outside the gitignored real_corpus/ (keep it local)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=300.0, help="per CLI run (s)")
    parser.add_argument("--limit", type=int, default=None, help="first N files only")
    args = parser.parse_args(argv)
    out = corpus_mod.ensure_local_only(args.out, allow_outside=args.allow_outside)
    out.mkdir(parents=True, exist_ok=True)
    manifest = corpus_mod.load_manifest(args.manifest)[:args.limit]

    if args.step == "report":
        print(json.dumps(report(out, manifest), indent=2, sort_keys=True))
        return 0
    if args.root is None:
        parser.error("--root is required for units and cli")
    if args.step == "units":
        target = out / "units.jsonl"
        done = _load(target)
        todo = [e for e in manifest if e.sha256[:16] not in done]
        with ProcessPoolExecutor(args.workers) as pool:
            rows = pool.map(measure_units, [str(args.root / e.path) for e in todo],
                            chunksize=4)
            for entry, row in zip(todo, rows):
                _append(target, entry.sha256[:16], row)
    else:
        target = out / "cli.jsonl"
        done = _load(target)
        todo = [e for e in manifest if e.sha256[:16] not in done]
        workdir = out / "cli-workdir"
        with ThreadPoolExecutor(args.workers) as pool:
            futures = [(e, pool.submit(run_one, args.root / e.path, workdir, e.sha256[:16],
                                       args.timeout)) for e in todo]
            for entry, future in futures:
                _append(target, entry.sha256[:16], future.result())
    print(f"{args.step}: {len(todo)} file(s) recorded in {target.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
