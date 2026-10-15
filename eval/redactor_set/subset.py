"""The committed subset: a few measured outputs of the real-redactor set,
copied into ``eval/caselib/real/`` with a provenance sidecar each.

``python -m redactor_set subset --set inputs --pick NAME=STEM:METHOD:CASE_ID ...``
copies the exact bytes that were measured (never regenerated, never
shrunk) and writes ``<NAME>.json`` beside each: the usual sidecar fields
(eval/caselib/families/redactors.py checks them), the tool's settings and
method, every Info-like dictionary and XMP value in the file -- reachable
or not -- and an ``oracle`` block, the locked adjudication of that file:
per planted instance its written form, fingerprints, class, cell, storage,
expected exit and the channels that found it, with the lock's sha256.
Records must be locked first (``adjudicate lock``); a file the lock does
not cover is refused.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from . import adjudicate
from .persona import BY_ID, PATTERNS, SECRETS

SIZE_CAP = 100_000
TOOL = {"description": "a PyMuPDF-based redaction tool"}
INFO_KEYS = ("/Producer", "/Creator", "/Title", "/Author", "/Subject", "/Keywords",
             "/CreationDate", "/ModDate", "/Trapped")
FABRICATED = ("Every value in this document -- the household's names, SSNs, address, "
              "account, routing and card numbers, EIN, date of birth and email -- is "
              "fabricated (eval/redactor_set/persona.py) and cannot belong to a real person.")

# Misses the owner accepted as listed gaps (2026-10-15); the same rule as
# scorecard/redactor_set.py's LISTED_GAPS, which this package may not import.
def listed_gap(loc: str, cls: str, secret: str) -> str | None:
    if BY_ID[secret].kind != "person name":
        return None
    if cls == "partial":
        return "name-fragment"
    return "name-variant" if loc == "T2" else None


def rules() -> list[dict[str, str]]:
    """The persona as case-library rules: one value rule per secret, named
    by its id, and the config's two patterns."""
    return [{"name": s.id, "value": s.value} for s in SECRETS] + [
        {"name": "SSN pattern", "pattern": PATTERNS[0]},
        {"name": "Card pattern", "pattern": PATTERNS[1]}]


def info_like(obj: object) -> bool:
    """An Info-like dictionary: no /Type, at least one entry, and every
    value a string, name or date -- whatever its keys, so a dictionary of
    custom keys only is caught too."""
    import pikepdf

    if not isinstance(obj, pikepdf.Dictionary) or isinstance(obj, pikepdf.Stream) or \
            "/Type" in obj or not len(obj.keys()):
        return False
    return all(isinstance(v, (pikepdf.String, pikepdf.Name)) for v in obj.values())


def xmp_values(data: bytes) -> list[tuple[str, str]]:
    """Every element's text and every attribute (namespace declarations
    apart) of an XMP packet, keyed by their local tag."""
    import xml.etree.ElementTree as ET

    text = data.decode("utf-8", "replace")
    start, end = text.find("<x:xmpmeta"), text.rfind("</x:xmpmeta>")
    if start < 0 or end < 0:
        start, end = text.find("<rdf:RDF"), text.rfind("</rdf:RDF>")
        end += len("</rdf:RDF>")
    else:
        end += len("</x:xmpmeta>")
    out = []
    for el in ET.fromstring(text[start:end]).iter():
        tag = el.tag.split("}")[-1]
        if el.text and el.text.strip():
            out.append((f"xmp {tag}", el.text.strip()))
        for key, value in el.attrib.items():
            if value:
                out.append((f"xmp @{key.split('}')[-1]}", value))
    return out


def info_fields(path: Path) -> list[dict[str, str]]:
    """Every Info-like dictionary's entries, reachable or not (a redactor
    that nulls the trailer's /Info leaves the old one orphaned), and every
    XMP value."""
    import pikepdf

    out: list[dict[str, str]] = []
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if info_like(obj):
                for key, value in obj.items():
                    out.append({"object": str(obj.objgen[0]), "key": key, "value": str(value)})
            elif isinstance(obj, pikepdf.Stream) and obj.get("/Type") == "/Metadata":
                for key, value in xmp_values(obj.read_bytes()):
                    out.append({"object": str(obj.objgen[0]), "key": key, "value": value})
    return out


def oracle_block(record: dict[str, Any], truth: dict[str, Any], lock_sha: str) -> dict[str, Any]:
    instances = []
    for row in record["instances"]:
        ov = row.get("override") or {}
        cls = ov.get("class", row["class"])
        base = truth["instances"][row["index"]] if row["index"] >= 0 else None
        whole = sorted({e["channel"] for e in row["evidence"]
                        if e["how"] == "whole" and e["counts"]})
        entry = {
            "secret": row["secret"], "loc": row["loc"], "class": cls,
            "written": base["written"] if base else None,
            "encoding": base["encoding"] if base else None,
            "fingerprints": base.get("fingerprints", []) if base else [],
            "channels": whole,
            "r1_witness": "R1" in whole,
        }
        if cls != "removed":
            entry.update({
                "cell": ov.get("cell", row.get("cell")), "case": row.get("case"),
                "storage": row.get("where"),
                "expected_exit": ov.get("expected_exit", row.get("expected_exit")),
                "override_reason": ov.get("reason"),
                "listed_gap": listed_gap(row["loc"], cls, row["secret"]),
            })
        instances.append(entry)
    return {"adjudicator": record["adjudicator"], "adjudication_date": record["date"],
            "lock_sha256": lock_sha, "truth": record["truth"],
            "expected_exit": record["expected_exit"], "secrets": record["secrets"],
            "instances": instances}


def write(root: Path, set_name: str, picks: dict[str, tuple[str, str]], real_dir: Path,
          case_ids: dict[str, str], run_date: str) -> list[Path]:
    """Copy each pick's measured output to *real_dir* and write its
    sidecar; *run_date* is the day the tool was run (owner's local date)."""
    problems, lock_sha = adjudicate.verify(root, set_name)
    if problems or not lock_sha:
        raise SystemExit("the adjudication lock does not verify: " + "; ".join(problems))
    run = json.loads((root / "work" / set_name / "run.json").read_text())
    config = (root / "config" / "redact.yaml").read_text()
    # What the run recorded, never a guess: "unrecorded" when REDACTOR_VERSION
    # was not set for it.
    version = str(run.get("redactor_version") or "unrecorded")
    written = []
    for name, (doc, method) in picks.items():
        record = json.loads((root / "adjudication" / set_name / method /
                             f"{doc}.json").read_text())
        truth = json.loads((root / set_name / f"{doc}.truth.json").read_text())
        src = root / run["documents"][doc][method]
        body = src.read_bytes()
        if hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise SystemExit(f"{name}: the output is not the bytes the lock covers")
        if len(body) > SIZE_CAP:
            raise SystemExit(f"{name}: {len(body)} bytes is over the {SIZE_CAP}-byte cap")
        dst = real_dir / f"{name}.pdf"
        shutil.copyfile(src, dst)
        oracle = oracle_block(record, truth, lock_sha)
        cells = sorted({i["cell"] for i in oracle["instances"] if i["class"] != "removed"})
        sidecar = {
            "file": dst.name, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "size_cap_bytes": SIZE_CAP, "origin": "redactor",
            "case_id": case_ids[name], "source_case": None,
            "source_case_note": (f"The real-redactor set (eval/redactor_set): template "
                                 f"{record['doc']}, seed {truth['seed']}, producer "
                                 f"{truth['producer']}; the exact bytes measured."),
            "tool": {**TOOL, "version": version, "date": run_date, "settings": {
                "llm_detection": False, "invocation": "<pdf> --config <yaml>",
                "config": config, "method": method,
                "garbage": 0 if method == "m1" else 4, "deflate": method == "m2",
                "gc_pymupdf": run.get("gc_pymupdf") if method == "m2" else None,
                "note": ("m1: the tool as it is run day to day. m2: m1's output re-saved "
                         "with garbage collection in the tool's own environment, which can "
                         "differ in detail from a collecting save inside the tool."),
            }},
            "fabricated_data_statement": FABRICATED,
            "redacted_values": rules(),
            "metadata_fields_scrubbed": [],
            "metadata_fields_scrubbed_note": ("Nothing was scrubbed: these are the exact bytes "
                                              "measured. info_fields lists every value left."),
            "info_fields": info_fields(dst),
            "oracle": oracle,
            "labels_summary": {"truth": record["truth"], "cells": cells,
                               "expected_exit": record["expected_exit"]},
        }
        side = real_dir / f"{name}.json"
        if side.exists():
            # A reviewed privacy allowlist (eval/caselib/model.py) survives a
            # rewrite; the scrub test drops any entry no finding matches.
            kept = json.loads(side.read_text()).get("privacy_allowlist")
            if kept:
                sidecar["privacy_allowlist"] = kept
        side.write_text(json.dumps(sidecar, indent=2) + "\n")
        written += [dst, side]
    return written


REAL_DIR = Path(__file__).resolve().parents[1] / "caselib" / "real"


def parse_pick(text: str) -> tuple[str, str, str, str]:
    name, _, rest = text.partition("=")
    stem, method, case_id = rest.split(":")
    return name, stem, method, case_id
