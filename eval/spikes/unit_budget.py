#!/usr/bin/env python3
"""Phase 3a-12: the unit budget (ADR 0006's amendment) and the orphan
overlap (ADR 0007), on the real corpus.

Four steps, each re-runnable on its own; per-file records go to a
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
* ``levers``: for the unflagged files with a hint stream, an XMP image or
  a synthesized annotation, what clearing that class would take: qpdf's
  linearization check and a stale /L (ADR 0011 R6 (b)); each XMP image
  decoded, its size and what Apple Vision reads on it (R7 (b), 4b); each
  synthesized annotation's shown inputs (R2, 4a).
* ``report``: joins them and prints one JSON object: unit
  distributions (p50/p90/p99/max, per kind, per byte), and for each 7e
  class and their union which files already exit 2 today (and why, by
  warning code; a qpdf exit status is its own reason) and which would
  newly exit 2; then the review rate today and after, on all files and on
  text-bearing ones, with and without today's qpdf exit-3 warnings; and
  what each lever, and each combination, would take off the new files.

Aggregate numbers only are printed; ``--out`` holds per-file records
(hash prefixes, counts, warning codes) and the CLI's reports, which carry
the target's file name: keep it local, like the manifest.

    PYTHONPATH=eval:. python eval/spikes/unit_budget.py units \\
        --root eval/scorecard/real_corpus/links --workers 6
    PYTHONPATH=eval:. python eval/spikes/unit_budget.py cli \\
        --root eval/scorecard/real_corpus/links --workers 6
    PYTHONPATH=eval:. python eval/spikes/unit_budget.py levers \\
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


# ── levers ───────────────────────────────────────────────────────────────
# What each large new-cost class would need to clear (the owner's
# briefing, "Levers"), measured on the files in it: a hint stream's qpdf
# linearization check and stale /L (ADR 0011 R6 (b)); each image an XMP
# packet holds, decoded, its size and what OCR reads in it (R7 (b), 4b);
# each synthesized annotation's shown inputs (R2, 4a).
_RICH_IMAGE = ("<img", "<svg", "url(")
LEVER_CLASSES = ("hint_stream", "xmp_image", "synthesized")


def measure_levers(path: str, classes: dict[str, int]) -> dict[str, Any]:
    import base64
    import binascii
    import re
    import shutil
    import subprocess

    import fitz

    from redaction_verifier.inventory.labels import StructuralKind
    from scorecard import inventory as inv_mod

    data = Path(path).read_bytes()
    out: dict[str, Any] = {}
    inv = build_inventory(data, budget=Budget(file_size=len(data)))
    doc = fitz.open(stream=data, filetype="pdf")
    if classes.get("hint_stream"):
        qpdf = shutil.which("qpdf")
        if qpdf is not None:
            done = subprocess.run([qpdf, "--check-linearization", path], capture_output=True,
                                  timeout=300)
            out["qpdf_linearization"] = done.returncode  # 0 clean, 2 errors, 3 warnings
        out["hint_stale"] = inv_mod.measure(data, inv).get("hint_stream_stale", 0)
    if classes.get("xmp_image"):
        images = []
        current = {ref: n for n, ref in inv.bodies_by_number(0).items()}
        older = 0
        for unit, labels in inv.body_labels.items():
            if StructuralKind.XMP not in labels.structural:
                continue
            if unit not in current:
                older += 1  # not read here: MuPDF reads the current revision
                continue
            packet = inv_mod._XML_BREAKS.sub(b"", doc.xref_stream(current[unit]) or b"")
            runs = []
            for match in inv_mod._BASE64_RUN.finditer(packet):
                run = re.sub(rb"[\t\n\r ]", b"", match.group())
                try:
                    runs.append(base64.b64decode(run + b"=" * (-len(run) % 4)))
                except (binascii.Error, ValueError):
                    continue
            for match in inv_mod._HEX_RUN.finditer(packet):
                runs.append(bytes.fromhex(re.sub(rb"[\t\n\r ]", b"", match.group()).decode()))
            for run in runs:
                if len(run) < 48 or not run.startswith(inv_mod._IMAGE_SIGNATURES):
                    continue
                images.append(_image_reading(run, fitz))
        out["xmp_images"] = images
        out["xmp_older_bodies"] = older
    if classes.get("synthesized"):
        notes = []
        found = inv.annotations[0] if inv.annotations else None
        for annotation in found or ():
            if not annotation.synthesized:
                continue
            notes.append(_shown_inputs(doc, annotation.number))
        out["synthesized_annotations"] = notes
    doc.close()
    return out


def _image_reading(run: bytes, fitz: Any) -> dict[str, Any]:
    """An XMP image: decodes or not, its size, and the characters Apple
    Vision reads on it (the shipped OCR path, enlarged to a page)."""
    from redaction_verifier.views.ocr import extract_ocr_text

    try:
        pixmap = fitz.Pixmap(run)
    except Exception:
        return {"decodes": False}
    reading: dict[str, Any] = {"decodes": True, "width": pixmap.width, "height": pixmap.height}
    try:
        page_doc = fitz.open()
        page = page_doc.new_page(width=pixmap.width, height=pixmap.height)
        page.insert_image(page.rect, stream=run)
        lines = extract_ocr_text(page)
        reading["ocr_chars"] = sum(len(line.strip()) for line in lines)
        reading["ocr_words"] = sum(len(line.split()) for line in lines)
        page_doc.close()
    except Exception as exc:
        reading["ocr_error"] = type(exc).__name__
    return reading


def _shown_inputs(doc: Any, number: int | None) -> dict[str, Any]:
    """What MuPDF builds a synthesized annotation from (ADR 0011 R2): its
    /Subtype, /FT and /V (inherited through /Parent), and whether it has
    rich text (/RC, /RV) -- and a rich text that could draw an image."""
    if number is None:
        return {"direct": True}
    note: dict[str, Any] = {"subtype": doc.xref_get_key(number, "Subtype")[1]}
    node, seen = number, set()
    for key in ("FT", "V"):
        node, seen = number, set()
        while node and node not in seen:
            seen.add(node)
            kind, value = doc.xref_get_key(node, key)
            if kind != "null":
                note[key] = value if key == "FT" else kind
                break
            kind, value = doc.xref_get_key(node, "Parent")
            node = int(value.split()[0]) if kind == "xref" else 0
    for key in ("RC", "RV", "DS", "Contents"):
        kind, value = doc.xref_get_key(number, key)
        if kind != "null":
            note[key] = True
            if kind == "xref":  # rich text held in a stream
                target = int(value.split()[0])
                value = (doc.xref_stream(target) or b"").decode("latin-1") \
                    if doc.xref_is_stream(target) else doc.xref_object(target)
            if key in ("RC", "RV") and any(m in value.lower() for m in _RICH_IMAGE):
                note["rich_image"] = True
    return note


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

    # Levers: what clearing each large class would take off the new files.
    by_hash_levers = _load(out_dir / "levers.jsonl")
    if by_hash_levers:
        levers = {str(i): by_hash_levers[e.sha256[:16]] for i, e in enumerate(manifest)
                  if e.sha256[:16] in by_hash_levers}
        result["levers"] = lever_report(union, members, units, cli, levers, text)
    return result


def _hint_ok(lever: dict[str, Any]) -> bool:
    """R6 (b)'s precondition: qpdf's linearization check clean, /L not stale."""
    return lever.get("qpdf_linearization") == 0 and not lever.get("hint_stale")


def _xmp_in_envelope(lever: dict[str, Any]) -> bool:
    """R7 (b)'s precondition: every image decodes, within ADR 0004's
    envelope (at most 10,000 px a side, at most 35 Mpx)."""
    images = lever.get("xmp_images", [])
    return bool(images) and not lever.get("xmp_older_bodies") and all(
        i.get("decodes") and max(i["width"], i["height"]) <= 10_000
        and i["width"] * i["height"] <= 35_000_000 for i in images)


def _synth_plain(lever: dict[str, Any]) -> bool:
    """R2's DECODED precondition in part: no rich text that could draw an
    image (the trace comparison itself is 4a's)."""
    notes = lever.get("synthesized_annotations", [])
    return bool(notes) and not any(n.get("rich_image") for n in notes)


def lever_report(union: list[str], members: dict[str, list[str]],
                 units: dict[str, dict[str, Any]], cli: dict[str, dict[str, Any]],
                 levers: dict[str, dict[str, Any]], text: set[str]) -> dict[str, Any]:
    new = [h for h in union if cli[h].get("exit") == 0]

    def classes_of(h: str) -> set[str]:
        return {c for c in UNION if units[h]["classes"].get(c, 0) > 0}

    checks = {"hint_stream": _hint_ok, "xmp_image": _xmp_in_envelope,
              "synthesized": _synth_plain}
    out: dict[str, Any] = {"new_files": len(new),
                           "new_text_bearing": sum(1 for h in new if h in text)}
    for name, check in checks.items():
        rows = [h for h in members[name] if h in levers]
        new_rows = [h for h in rows if cli[h].get("exit") == 0]
        sole = [h for h in new_rows if classes_of(h) == {name}]
        section: dict[str, Any] = {
            "files": len(rows), "new": len(new_rows), "new_sole_class": len(sole),
            "new_sole_class_text_bearing": sum(1 for h in sole if h in text),
            "precondition_met": sum(1 for h in rows if check(levers[h])),
            "new_sole_class_precondition_met": sum(1 for h in sole if check(levers[h])),
        }
        if name == "hint_stream":
            section["qpdf_linearization"] = dict(Counter(
                str(levers[h].get("qpdf_linearization")) for h in rows))
            section["stale"] = sum(1 for h in rows if levers[h].get("hint_stale"))
            section["new_precondition_met"] = sum(1 for h in new_rows if check(levers[h]))
            section["new_text_bearing"] = sum(1 for h in new_rows if h in text)
            section["new_text_bearing_precondition_met"] = sum(
                1 for h in new_rows if h in text and check(levers[h]))
            section["new_text_bearing_stale"] = sum(
                1 for h in new_rows if h in text and levers[h].get("hint_stale"))
        if name == "xmp_image":
            images = [i for h in rows for i in levers[h].get("xmp_images", [])]
            readable = [i for i in images if i.get("decodes")]
            section["images"] = len(images)
            section["images_decoded"] = len(readable)
            section["image_long_side"] = percentiles(
                [max(i["width"], i["height"]) for i in readable])
            section["images_ocr_reads_words"] = sum(1 for i in readable
                                                    if i.get("ocr_words", 0) > 0)
            section["images_ocr_reads_3_words"] = sum(1 for i in readable
                                                      if i.get("ocr_words", 0) >= 3)
            section["files_ocr_reads_words"] = sum(
                1 for h in rows if any(i.get("ocr_words", 0) > 0
                                       for i in levers[h].get("xmp_images", [])))
            section["ocr_errors"] = sum(1 for i in images if "ocr_error" in i)
            section["files_with_older_bodies"] = sum(1 for h in rows
                                                     if levers[h].get("xmp_older_bodies"))
        if name == "synthesized":
            notes = [n for h in rows for n in levers[h].get("synthesized_annotations", [])]
            section["annotations"] = len(notes)
            section["by_field_type"] = dict(Counter(str(n.get("FT")) for n in notes))
            section["with_value"] = sum(1 for n in notes if "V" in n)
            section["rich_text"] = sum(1 for n in notes if n.get("RC") or n.get("RV"))
            section["rich_text_image"] = sum(1 for n in notes if n.get("rich_image"))
            section["direct"] = sum(1 for n in notes if n.get("direct"))
        out[name] = section
    # Levers together: the new files whose every class is cleared -- by a
    # lever whose precondition the file meets, or (JavaScript) by a decoder
    # assumed to clear every stream; orphans clear under no lever (R3).
    scenarios = {
        "synthesized (4a)": ("synthesized",),
        "hint (R6 b)": ("hint_stream",),
        "hint + synthesized": ("hint_stream", "synthesized"),
        "hint + synthesized + javascript": ("hint_stream", "synthesized", "javascript"),
        "xmp (R7 b)": ("xmp_image",),
        "xmp + synthesized": ("xmp_image", "synthesized"),
        "all four": ("hint_stream", "synthesized", "javascript", "xmp_image"),
    }
    out["scenarios"] = {}
    for label, taken in scenarios.items():
        cleared = [h for h in new if classes_of(h) <= set(taken) and all(
            name == "javascript" or (h in levers and checks[name](levers[h]))
            for name in classes_of(h))]
        # The upper bound: each lever clears its whole class, preconditions aside.
        outright = [h for h in new if classes_of(h) <= set(taken)]
        out["scenarios"][label] = {"cleared": len(cleared),
                                   "cleared_text_bearing": sum(1 for h in cleared if h in text),
                                   "outright": len(outright),
                                   "outright_text_bearing": sum(1 for h in outright if h in text)}
    out["new_by_classes"] = dict(Counter(
        "+".join(sorted(classes_of(h))) for h in new).most_common())
    out["new_by_classes_text_bearing"] = dict(Counter(
        "+".join(sorted(classes_of(h))) for h in new if h in text).most_common())
    return out


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
    parser.add_argument("step", choices=("units", "cli", "levers", "report"))
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
    elif args.step == "levers":
        target = out / "levers.jsonl"
        units = _load(out / "units.jsonl")
        done = _load(target)
        todo = []
        for e in manifest:
            key = e.sha256[:16]
            row = units.get(key, {})
            if (key not in done and key not in {k for k, _ in todo} and not row.get("flags")
                    and any(row.get("classes", {}).get(c) for c in LEVER_CLASSES)):
                todo.append((key, e))
        with ProcessPoolExecutor(args.workers) as pool:
            rows = pool.map(measure_levers, [str(args.root / e.path) for _, e in todo],
                            [units[k]["classes"] for k, _ in todo], chunksize=2)
            for (key, _), row in zip(todo, rows):
                _append(target, key, row)
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
