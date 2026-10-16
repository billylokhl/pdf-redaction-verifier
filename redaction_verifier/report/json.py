"""redaction_verifier.report.json — the machine-readable `--json` report.

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §4/§6 ("report/ human, JSON [with schema_version,
experimental]"; "Move, don't wrap"). Behaviour is byte-identical to the
code this replaced; verify.py re-exports every name here so existing
imports, `verify.X` references and the CLI keep working unchanged.

`fitz` is imported plainly here (like redaction_verifier.views), as
`import pymupdf as fitz`: by the time this module loads, verify.py's own
top-level `import pymupdf as fitz` has already succeeded and cached the
module, so this is never a *new* failure mode when this module is
reached the way verify.py reaches it — via its own guarded import block,
only after that top-level import has already succeeded. An importer
that reaches this module some other way (bypassing verify.py's guard)
would not get that protection; it exists so the environment block below
(`fitz.VersionBind`) does not have to be threaded in as a parameter.

`tool_version` is NOT read from a constant owned by this module: the
tool's version is `verify.__version__`, the single source of truth
pyproject.toml's dynamic version reads (`attr = "verify.__version__"`),
and this package must never import verify (one-way dependency). The
caller (verify.py's own `main()`) passes it in explicitly instead.

`ocr_available` is likewise passed in by the caller rather than read
from `redaction_verifier.views._OCR_IMPORTS_OK` here: verify.py's `main()`
already imports that value once (as its own re-exported `_OCR_IMPORTS_OK`)
to gate the OCR layer, and threads the same value through to this
function — like `tool_version` — so exactly one value drives both the
gate and the report, instead of two separate reads of the same module
attribute that could in principle observe different things.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import pymupdf as fitz

from redaction_verifier.matching import mask
from redaction_verifier.model import WARNING_FIELDS, Finding, ScanReport, Warn
from redaction_verifier.report.text import _sanitize_report_text

# Bumped when a field of the --json report is renamed, removed or changes
# meaning; adding a field does not bump it. 2: `notices` (ADR 0012) -- with
# built-in classes informational, a class match no longer appears in
# `findings` or `warnings`, which changes what those mean. `info` (owner
# decision 2026-10-16) is an added field, so it does not bump it.
JSON_SCHEMA_VERSION: int = 2


def build_json_report(
    report: ScanReport,
    exit_code: int,
    error: tuple[str, str] | None = None,
    *,
    target: Path,
    tool_version: str,
    ocr_available: bool,
    private_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """The machine-readable report: the same content as print_report,
    masked and sanitized the same way, plus stable codes and structured
    fields (layer, storage class, object, revision, page, rule, adjacency,
    tool) so consumers never parse message wording.

    Every field is always present (null when it does not apply), and
    findings and warnings are sorted, so the output is identical across
    runs. Paths given on the command line appear only as base names.
    """

    def text(value: str) -> str:
        for path in private_paths:
            for form in {str(path), str(path.resolve())}:
                if form not in (".", ""):
                    value = value.replace(form, path.name)
        return _sanitize_report_text(value)

    def finding_entry(f: Finding) -> dict[str, Any]:
        return {
            "layer": f.layer,
            "rule": text(f.secret_name),
            "tier": "hard",
            "storage": f.storage,
            "object": f.object,
            "revision": f.revision,
            "page": f.page,
            "location": text(f.location),
            "sample": text(mask(f.sample, report.exact_index, full=f.layer == "OCR")) if f.sample else "",
        }

    def warning_entry(w: str) -> dict[str, Any]:
        # mypy narrows on a direct `isinstance(w, Warn)` check, not
        # through a bool variable holding its result — real, but the same
        # limitation this code always had under verify.py's own (until
        # now unchecked) mypy override.
        coded = isinstance(w, Warn)
        fields = w.fields if coded else {}  # type: ignore[attr-defined]
        entry: dict[str, Any] = {
            "code": w.code if coded else "UNCODED",  # type: ignore[attr-defined]
            "kind": w.kind if coded else "coverage",  # type: ignore[attr-defined]
            "layer": w.layer if coded else None,  # type: ignore[attr-defined]
        }
        for name in WARNING_FIELDS:
            value = fields.get(name)
            entry[name] = text(value) if isinstance(value, str) else value
        entry["message"] = text(w)
        return entry

    # Every field a finding or a warning has, so a notice has one shape.
    notice_fields = ("layer", "rule", "storage", "object", "revision", "page",
                     "location", "sample", "code", *WARNING_FIELDS, "message")

    def notice_entry(n: Finding | Warn) -> dict[str, Any]:
        """A built-in class match that does not count toward the verdict
        (ADR 0012): the fields of the finding or review warning it would
        have been, every field present (null when it does not apply); the
        warning's own kind is kept as `warning_kind`."""
        if isinstance(n, Finding):
            base, tier, warning_kind = finding_entry(n), "hard", None
        else:
            base, tier = warning_entry(n), "review"
            warning_kind = base.pop("kind")
        return {"kind": "class-match", "tier": tier, "warning_kind": warning_kind,
                **{k: base.get(k) for k in notice_fields}}

    def ordered(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(entries, key=lambda d: json.dumps(d, sort_keys=True))

    findings = ordered([finding_entry(f) for f in report.findings])
    warnings = ordered([warning_entry(w) for w in report.warnings])
    notices = ordered([notice_entry(n) for n in report.notices])
    # Information that is not a finding, a warning or a notice and never
    # changes the verdict (model.Info): today only ENTITY_TYPES_NOT_CHECKED.
    info = ordered([{"code": i.code, "entity_types": list(i.entity_types),
                     "by_default": i.by_default, "message": text(i.message)}
                    for i in report.info])
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool": {"name": "pdf-redaction-verifier", "version": tool_version},
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "pymupdf": fitz.VersionBind,
            "ocr_available": ocr_available,
        },
        "target": text(target.name),
        "exit_code": exit_code,
        "verdict": {0: "pass", 1: "fail", 2: "uncertified"}[exit_code],
        "error": ({"code": error[0], "message": text(error[1])} if error else None),
        "findings": findings,
        "warnings": warnings,
        "notices": notices,
        "info": info,
    }


def write_json_report(path: Path, data: dict[str, Any]) -> bool:
    """Write the JSON report atomically — to a temporary file beside it,
    then renamed over it — so a reader never sees a partial report.
    False (and a stderr note) if it cannot be written."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        # Private (0600), never through a symlink, never over an existing
        # file; the rename then replaces a link at *path*, not its target.
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(
            f"[ERROR] Cannot write JSON report {path.name}: {exc.strerror or exc}", file=sys.stderr
        )
        return False
    return True
