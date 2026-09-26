"""The real-world corpus: local-only, clean-side metrics (docs/REDESIGN.md
§5). Nothing here is committed: the manifest and every scan result live
under `eval/scorecard/real_corpus/` (gitignored), keyed by SHA-256 with
paths relative to a *configured root* that is never itself recorded —
only the caller who already has that root can turn a manifest entry back
into a file.

The corpus is assumed clean (no planted secret), so only two metrics
apply: false hard (a hard finding) and review rate (exit 2), reported
per stratum — text-bearing, multi-revision, and producer family (from
`/Producer`) — as well as overall.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .metrics import CaseMetricRow, Scorecard, compute_metrics
from .runner import CliResult, current_python, run_cli

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "real_corpus" / "manifest.json"

# Coarse producer-family buckets from a lowercased /Producer string.
# Order matters: the first match wins.
_PRODUCER_FAMILIES: tuple[tuple[str, str], ...] = (
    ("acrobat", "acrobat"),
    ("distiller", "acrobat"),
    ("microsoft", "office"),
    ("word", "office"),
    ("powerpoint", "office"),
    ("excel", "office"),
    ("libreoffice", "libreoffice"),
    ("openoffice", "libreoffice"),
    ("ghostscript", "ghostscript"),
    ("skia", "chromium"),
    ("chromium", "chromium"),
    ("quartz", "quartz"),
    ("preview", "quartz"),
)


def producer_family(producer: str | None) -> str:
    if not producer:
        return "unknown"
    lowered = producer.lower()
    for needle, family in _PRODUCER_FAMILIES:
        if needle in lowered:
            return family
    return "other"


@dataclass(frozen=True)
class CorpusEntry:
    sha256: str
    path: str  # relative to the caller's configured root
    text_bearing: bool
    multi_revision: bool
    producer: str | None

    @property
    def producer_family(self) -> str:
        return producer_family(self.producer)


def classify(pdf_path: Path) -> tuple[bool, bool, str | None]:
    """(text_bearing, multi_revision, producer) via a quick probe."""
    import fitz

    text_bearing = False
    producer = None
    doc = fitz.open(pdf_path)
    try:
        text_bearing = any(page.get_text().strip() for page in doc)
        if doc.metadata:
            producer = doc.metadata.get("producer") or None
    finally:
        doc.close()
    multi_revision = pdf_path.read_bytes().count(b"%%EOF") > 1
    return text_bearing, multi_revision, producer


def build_manifest(root: Path, *, manifest_path: Path = DEFAULT_MANIFEST) -> list[CorpusEntry]:
    """Scan *root* for `*.pdf` files, hash and classify each, and write the
    manifest. Re-running replaces it entirely, so a file removed from
    *root* is dropped and a changed file's hash and strata are refreshed."""
    entries: list[CorpusEntry] = []
    for path in sorted(root.rglob("*.pdf")):
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        text_bearing, multi_revision, producer = classify(path)
        entries.append(
            CorpusEntry(
                sha256=sha256,
                path=str(path.relative_to(root)),
                text_bearing=text_bearing,
                multi_revision=multi_revision,
                producer=producer,
            )
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": [vars(e) for e in entries]}
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return entries


def load_manifest(manifest_path: Path = DEFAULT_MANIFEST) -> list[CorpusEntry]:
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text())
    return [CorpusEntry(**e) for e in payload.get("entries", [])]


@dataclass(frozen=True)
class CorpusCheck:
    missing: tuple[str, ...]  # manifest entries no longer found under root
    changed: tuple[str, ...]  # manifest entries whose sha256 no longer matches

    @property
    def ok(self) -> bool:
        return not self.missing and not self.changed


def check_manifest(entries: list[CorpusEntry], root: Path) -> CorpusCheck:
    missing = []
    changed = []
    for entry in entries:
        path = root / entry.path
        if not path.exists():
            missing.append(entry.path)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.sha256:
            changed.append(entry.path)
    return CorpusCheck(tuple(missing), tuple(changed))


@dataclass(frozen=True)
class CorpusRun:
    entry: CorpusEntry
    result: CliResult


def scan_corpus(
    entries: list[CorpusEntry],
    root: Path,
    rules: list[Mapping[str, str]],
    *,
    verify_path: Path,
    workdir: Path,
    timeout: float = 60.0,
) -> list[CorpusRun]:
    """Scan every present, unchanged manifest entry with the candidate CLI.
    Missing or changed files are silently skipped here — call
    `check_manifest` first and surface those separately."""
    python_exe = current_python()
    runs = []
    for entry in entries:
        path = root / entry.path
        if not path.exists():
            continue
        case_id = entry.sha256[:16]
        result = run_cli(python_exe, verify_path, path, rules, workdir, case_id, timeout=timeout)
        runs.append(CorpusRun(entry, result))
    return runs


def _row(run: CorpusRun) -> CaseMetricRow:
    report = run.result.report
    has_hard = bool(report and any(f.get("tier") == "hard" for f in report.get("findings", [])))
    return CaseMetricRow(
        case_id=run.entry.sha256[:16],
        truth="clean",  # the real corpus is assumed clean: no planted secret
        expected_exit=None,
        actual_exit=run.result.returncode if not run.result.crashed else None,
        has_hard_finding=has_hard,
        crashed=run.result.crashed,
        timed_out=run.result.timed_out,
        elapsed=run.result.elapsed,
    )


def stratify(runs: list[CorpusRun]) -> dict[str, Scorecard]:
    """Clean-side metrics overall and per stratum: text-bearing vs not,
    multi-revision vs not, and each producer family seen."""
    strata: dict[str, list[CorpusRun]] = {"all": list(runs)}
    for run in runs:
        strata.setdefault(
            "text-bearing" if run.entry.text_bearing else "text-free", []
        ).append(run)
        strata.setdefault(
            "multi-revision" if run.entry.multi_revision else "single-revision", []
        ).append(run)
        strata.setdefault(f"producer:{run.entry.producer_family}", []).append(run)
    return {name: compute_metrics([_row(r) for r in group]) for name, group in strata.items()}
