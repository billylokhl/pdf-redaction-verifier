"""Real redaction-tool output, committed as files (the only binaries in
the library): generated documents can only show failures someone thought
to build. All data in these files is fabricated.

Provenance: a PyMuPDF-based redaction tool (version 0.1.0, 2026-09-13), run with
LLM detection disabled and these exact values:
"Jordan Q. Testperson", "123-45-6789", "01/01/1970", "1600 Fictional Ave",
"Springfield", "000123456789". The compacted file is the hex output
re-saved by PyMuPDF with garbage=4.

The real-redactor set (eval/redactor_set, eval/README.md) adds measured
outputs of the same tool: fabricated documents from five producers, run
as-is (m1) or re-saved with garbage collection (m2), each the exact bytes
the oracle classed and the adjudication locked, with that adjudication
in its sidecar's ``oracle`` block. They use the set's persona
(``PERSONA_RULES``) instead of ``RULES``.

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
# The real-redactor set's persona and config patterns (eval/redactor_set/
# persona.py; tests/test_case_library.py checks the two agree).
PERSONA_RULES = (
    {"name": "NAME1", "value": "Jordan Q. Testperson"},
    {"name": "NAME2", "value": "Avery R. Samplename"},
    {"name": "SSN1", "value": "666-51-4073"},
    {"name": "SSN2", "value": "666-38-2914"},
    {"name": "ADDR", "value": "1600 Fictional Ave"},
    {"name": "ACCT", "value": "731904628155"},
    {"name": "ROUT", "value": "997382611"},
    {"name": "CARD", "value": "4111 1111 1111 1111"},
    {"name": "EIN", "value": "00-4857261"},
    {"name": "DOB", "value": "03/14/1971"},
    {"name": "EMAIL", "value": "jordan.testperson@example.com"},
    {"name": "SSN pattern", "pattern": r"\d{3}-\d{2}-\d{4}"},
    {"name": "Card pattern", "pattern": r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"},
)


def _copy(name: str):
    def build(path: Path) -> None:
        shutil.copyfile(REAL / name, path)
    return build


def _sidecar(pdf_name: str, case_id: str, truth: str,
             rules: tuple[dict[str, str], ...] = RULES) -> dict[str, Any]:
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
    if list(data.get("redacted_values", [])) != [dict(r) for r in rules]:
        raise ValueError(f"{pdf_name}: sidecar redacted_values do not match the case's rules")
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


# ── The real-redactor set's committed subset ────────────────────────────

SET_NOTE = ("A fabricated document from the real-redactor set (eval/redactor_set), "
            "redacted by the tool with LLM detection off; the label is its locked "
            "adjudication (sidecar oracle block).")


def _set_case(pdf_name: str, case_id: str, truth: str, *, expected, story: str,
              mistake: str, recovery: str = "", cells: tuple[str, ...] = (),
              features: tuple[str, ...] = (), requires: tuple[str, ...] = ()) -> None:
    data = _sidecar(pdf_name, case_id, truth, PERSONA_RULES)
    case(case_id, truth=truth, cells=cells, features=features, writer="file",
         origin="redactor", rules=PERSONA_RULES, expected=expected, story=story,
         mistake=mistake, recovery=recovery, requires=requires,
         privacy_allowlist=tuple(data.get("privacy_allowlist", ())),
         )(_copy(pdf_name))


_set_case(
    "redactor-form-fields.pdf", "document.redactor-form-fields", "leak",
    cells=("annot-fields.plain",),
    expected=expect(1, findings=tuple((n, "live") for n in ("NAME1", "SSN1", "DOB", "EMAIL"))),
    story="A fillable benefits application run through the tool and re-saved with garbage "
          "collection: the tool found every value in the form fields and counted them as "
          "redacted, but redaction does not remove a widget, so each field still shows "
          "its value.",
    mistake="Redacting a filled form with a page-text redactor: the fields' values and "
            "appearances are annotations, which applying redactions leaves in place.",
    recovery="Open the file: the fields still show the values (or read each /V).")

_set_case(
    "redactor-annotations.pdf", "document.redactor-annotations", "leak",
    cells=("annot-fields.plain",),
    # NAME1 also survives as the word "jordan" in a mailto: link: a name
    # fragment, an owner-accepted listed gap (2026-10-15), not required here.
    expected=expect(1, findings=tuple((n, "live") for n in ("SSN2", "DOB", "EMAIL"))),
    story="An annotated statement run through the tool and re-saved with garbage "
          "collection: the page text is gone, but a sticky note, a highlight's comment and "
          "a mailto: link that overlap no redacted text keep the SSN, the date of birth and "
          "the email address.",
    mistake="Redacting page text only: annotation comments and link targets are not page "
            "text, and nothing removes those that do not overlap a redaction.",
    recovery="Open the comments panel, or read each annotation's /Contents and /URI.")

_set_case(
    "redactor-scan-skipped.pdf", "page.redactor-scan-skipped", "leak",
    cells=("live.pixels",), requires=("ocr",),
    expected=expect(1, findings=tuple((n, "live") for n in ("NAME1", "SSN1", "ADDR")),
                    layers=tuple((n, "OCR") for n in ("NAME1", "SSN1", "ADDR"))),
    story="A scanned letter run through the tool and re-saved with garbage collection: "
          "the page has no text layer, so the tool skips it and the name, SSN and address "
          "stay visible in the image.",
    mistake="Trusting a text-based redactor with a scan: it only processes pages that have "
            "text.",
    recovery="Look at the page.")

_set_case(
    "redactor-info-orphan.pdf", "leftover.redactor-info-orphan", "leak",
    cells=("orphaned.plain", "leftover-xmp.plain", "orphaned.font"),
    expected=expect(1, findings=tuple((n, "orphaned") for n in ("NAME1", "SSN1", "ACCT")),
                    warnings=(("LEFTOVER_UNDECODABLE_TEXT", "orphaned"),)),
    story="A metadata-heavy tax summary run through the tool as-is: it nulls the trailer's "
          "/Info and unlinks the XMP, but saves without garbage collection, so the old "
          "Info dictionary (a custom SSN key included), the XMP packet and the page's "
          "original content stream all stay in the file, orphaned.",
    mistake=NO_GC,
    recovery="qpdf --qdf --object-streams=disable, then grep the uncompressed objects.")

_set_case(
    "redactor-statement-gc.pdf", "page.redactor-statement-gc", "clean",
    features=("live.font",), expected=expect(0),
    story="A three-page checking statement run through the tool and re-saved with garbage "
          "collection: every value was in page text, and none is left.",
    mistake="None -- a correctly redacted statement (U3: it must exit 0).")

_set_case(
    "redactor-w2-gc.pdf", "page.redactor-w2-gc", "clean",
    features=("live.font",), expected=expect(0),
    story="A W-2 run through the tool and re-saved with garbage collection: every value "
          "was in page text, and none is left.",
    mistake="None -- a correctly redacted form (U3: it must exit 0).")
