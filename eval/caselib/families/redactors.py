"""Real redaction-tool output, committed as files (the only binaries in
the library): generated documents can only show failures someone thought
to build. All data in these files is fabricated.

Provenance: a PyMuPDF-based redaction tool (version 0.1.0, 2026-09-13), run with
LLM detection disabled and these exact values:
"Jordan Q. Testperson", "123-45-6789", "01/01/1970", "1600 Fictional Ave",
"Springfield", "000123456789". The compacted file is the hex output
re-saved by PyMuPDF with garbage=4.

Each committed PDF has a sidecar (``<name>.json`` beside it) recording the
same provenance machine-readably — sha256, tool, settings, the fabricated-
data statement, scrubbed metadata fields, and a summary of the label below
— so a reviewer (or a script) can check the two agree without re-deriving
either from the other. ``_sidecar`` below cross-checks every case against
its sidecar at import time: a case whose rules, truth or story summary
drifts from what the sidecar says fails loudly here, not silently in the
gallery.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ..model import SSN, case, expect

REAL = Path(__file__).resolve().parent.parent / "real"
SIZE_CAP = 100_000
RULES = (
    {"name": "Name", "value": "Jordan Q. Testperson"},
    {"name": "SSN", "value": SSN},
    {"name": "DOB", "value": "01/01/1970"},
    {"name": "Address", "value": "1600 Fictional Ave"},
    {"name": "Account", "value": "000123456789"},
)
NO_GC = ("redactor applies the redactions correctly, then saves with PyMuPDF's "
         "default garbage=0, so the page's original content streams stay in the file.")
ALL_ORPHANED = expect(1, findings=tuple((r["name"], "orphaned") for r in RULES))


def _copy(name: str):
    def build(path: Path) -> None:
        shutil.copyfile(REAL / name, path)
    return build


def _sidecar(pdf_name: str, case_id: str, truth: str) -> dict[str, Any]:
    """Load ``real/<pdf_name>.json`` and cross-check it against the case
    it documents: the sha256 and size cap are re-derived from the file on
    disk (never trusted from the sidecar alone), and the sidecar's own
    case id, tool description and rules must match what the case below
    declares. Raised errors surface at ``caselib.load()`` time — the same
    place a duplicate or malformed case id would."""
    pdf_path = REAL / pdf_name
    sidecar_path = REAL / f"{pdf_name.removesuffix('.pdf')}.json"
    if not sidecar_path.exists():
        raise ValueError(f"{pdf_name}: no provenance sidecar at {sidecar_path.name}")
    data = json.loads(sidecar_path.read_text())
    body = pdf_path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if data.get("sha256") != digest:
        raise ValueError(f"{pdf_name}: sidecar sha256 does not match the file on disk")
    cap = data.get("size_cap_bytes")
    if not isinstance(cap, int) or cap <= 0:
        raise ValueError(f"{pdf_name}: sidecar has no positive size_cap_bytes")
    if len(body) > cap:
        raise ValueError(f"{pdf_name}: {len(body)} bytes exceeds its declared cap {cap}")
    if len(body) > SIZE_CAP:
        raise ValueError(f"{pdf_name}: {len(body)} bytes exceeds the family cap {SIZE_CAP}")
    if data.get("case_id") != case_id:
        raise ValueError(f"{pdf_name}: sidecar case_id {data.get('case_id')!r} != {case_id!r}")
    if data.get("origin") != "redactor":
        raise ValueError(f"{pdf_name}: sidecar origin must be 'redactor'")
    if not data.get("fabricated_data_statement"):
        raise ValueError(f"{pdf_name}: sidecar has no fabricated-data statement")
    if "metadata_fields_scrubbed" not in data:
        raise ValueError(f"{pdf_name}: sidecar has no metadata_fields_scrubbed list (may be empty)")
    summary = data.get("labels_summary", {})
    if summary.get("truth") != truth:
        raise ValueError(f"{pdf_name}: sidecar labels_summary.truth {summary.get('truth')!r} "
                          f"!= case truth {truth!r}")
    values = {v["name"]: v["value"] for v in data.get("redacted_values", [])}
    expected_values = {r["name"]: r["value"] for r in RULES}
    if values != expected_values:
        raise ValueError(f"{pdf_name}: sidecar redacted_values do not match RULES")
    return data


_LITERAL = _sidecar("redactor-literal.pdf", "leftover.redactor-literal", "leak")
case("leftover.redactor-literal", truth="leak", cells="orphaned.plain", writer="file",
     origin="redactor", rules=RULES, expected=ALL_ORPHANED,
     story="A benefits application redacted by redactor: the page shows blank fields, "
           "but every original line is still in the file as literal strings.",
     mistake=NO_GC,
     recovery="mutool clean -d file.pdf out.pdf && grep -a 123-45-6789 out.pdf",
     privacy_allowlist=tuple(_LITERAL.get("privacy_allowlist", ())),
     )(_copy("redactor-literal.pdf"))

_HEX = _sidecar("redactor-hex.pdf", "leftover.redactor-hex", "leak")
case("leftover.redactor-hex", truth="leak", cells="orphaned.plain", writer="file",
     origin="redactor", rules=RULES, expected=ALL_ORPHANED,
     story="The same failure with the original text stored as hex strings: still plain "
           "character codes, so every value is recovered from the leftover streams.",
     mistake=NO_GC,
     recovery="mutool clean -d, then decode the hex strings (each pair of digits is a character).",
     privacy_allowlist=tuple(_HEX.get("privacy_allowlist", ())),
     )(_copy("redactor-hex.pdf"))

_COMPACTED = _sidecar("redactor-hex-compacted.pdf", "leftover.redactor-compacted", "clean")
case("leftover.redactor-compacted", truth="clean", features="orphaned.plain",
     writer="file", origin="redactor", rules=RULES, expected=expect(0),
     story="The hex-coded output re-saved with garbage collection: the leftovers are gone.",
     mistake="None — the fix for the failure above.",
     privacy_allowlist=tuple(_COMPACTED.get("privacy_allowlist", ())),
     )(_copy("redactor-hex-compacted.pdf"))
