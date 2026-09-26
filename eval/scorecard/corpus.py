"""The real-world corpus: local-only, clean-side metrics (docs/REDESIGN.md
§5). Nothing here is committed: the manifest and every scan result live
under `eval/scorecard/real_corpus/` (gitignored — see `ensure_local_only`,
which refuses a manifest or output path outside it by default), keyed by
SHA-256 with paths relative to a *configured root* that is never itself
recorded — only the caller who already has that root can turn a manifest
entry back into a file. The manifest's own `path` field IS a relative
file name from that root (needed to re-open the file for a re-scan), so
it must never be committed or shared outside this machine, same as the
root itself.

The corpus is assumed clean (no planted secret), so only two metrics
apply: false hard (a hard finding) and review rate (exit 2), reported
per stratum — text-bearing, multi-revision, and producer family — as
well as overall. Only the coarse producer *family* (e.g. "acrobat",
"office") is ever stored; the raw `/Producer` string is read, classified,
and discarded on the spot — it can hold a username, hostname or email
address a real producer app embeds, none of which belongs even in a
local-only file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .keys import effective_exit
from .metrics import CaseMetricRow, Scorecard, compute_metrics
from .runner import CliResult, current_python, run_cli

REAL_CORPUS_DIR = Path(__file__).resolve().parent / "real_corpus"
DEFAULT_MANIFEST = REAL_CORPUS_DIR / "manifest.json"


def ensure_local_only(path: Path, *, allow_outside: bool = False) -> Path:
    """Refuse a manifest or output *path* outside the gitignored
    `real_corpus/` directory, unless the caller explicitly opts out. The
    whole point of that directory is that its contents — SHA-256s,
    relative file names, and scan artifacts that can carry a target's own
    basename — never get committed or shared by accident; a path outside
    it loses that guarantee silently."""
    if allow_outside:
        return path
    resolved = path.resolve()
    boundary = REAL_CORPUS_DIR.resolve()
    if resolved != boundary and boundary not in resolved.parents:
        raise SystemExit(
            f"refusing to use {path} — it is outside the local-only, gitignored "
            f"{REAL_CORPUS_DIR} (docs: eval/README.md, \"The real-world corpus\").\n"
            "Pass --allow-outside if you really mean this (e.g. an external drive) — "
            "you are then responsible for never committing or sharing that path."
        )
    return path

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
    producer_family: str  # coarse bucket only — never the raw /Producer string


def classify(pdf_path: Path) -> tuple[bool, bool, str]:
    """(text_bearing, multi_revision, producer_family) via a quick probe.
    The raw `/Producer` string is read here and immediately reduced to a
    coarse family — it is never returned or stored (it can hold a
    username, hostname, or email address some producers embed)."""
    import fitz

    text_bearing = False
    raw_producer = None
    doc = fitz.open(pdf_path)
    try:
        text_bearing = any(page.get_text().strip() for page in doc)
        if doc.metadata:
            raw_producer = doc.metadata.get("producer") or None
    finally:
        doc.close()
    multi_revision = pdf_path.read_bytes().count(b"%%EOF") > 1
    return text_bearing, multi_revision, producer_family(raw_producer)


def build_manifest(root: Path, *, manifest_path: Path = DEFAULT_MANIFEST) -> list[CorpusEntry]:
    """Scan *root* for `*.pdf` files, hash and classify each, and write the
    manifest. Re-running replaces it entirely, so a file removed from
    *root* is dropped and a changed file's hash and strata are refreshed."""
    entries: list[CorpusEntry] = []
    for path in sorted(root.rglob("*.pdf")):
        data = path.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        text_bearing, multi_revision, family = classify(path)
        entries.append(
            CorpusEntry(
                sha256=sha256,
                path=str(path.relative_to(root)),
                text_bearing=text_bearing,
                multi_revision=multi_revision,
                producer_family=family,
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


def _row(run: CorpusRun, have: frozenset[str] | None) -> CaseMetricRow:
    report = run.result.report
    has_hard = bool(report and any(f.get("tier") == "hard" for f in report.get("findings", [])))
    actual_exit = None if run.result.crashed else effective_exit(report, have)  # type: ignore[arg-type]
    return CaseMetricRow(
        case_id=run.entry.sha256[:16],
        truth="clean",  # the real corpus is assumed clean: no planted secret
        expected_exit=None,
        actual_exit=actual_exit,
        has_hard_finding=has_hard,
        crashed=run.result.crashed,
        timed_out=run.result.timed_out,
        elapsed=run.result.elapsed,
    )


def stratify(runs: list[CorpusRun], have: frozenset[str] | None = None) -> dict[str, Scorecard]:
    """Clean-side metrics overall and per stratum: text-bearing vs not,
    multi-revision vs not, and each producer family seen. *have* drops
    environmental warnings (e.g. no OCR on this machine) the same way
    the differential does, so a run on a partial environment does not
    inflate the review rate — pass `caselib.run.available()` for that;
    omit it to use the exit code exactly as reported."""
    strata: dict[str, list[CorpusRun]] = {"all": list(runs)}
    for run in runs:
        strata.setdefault(
            "text-bearing" if run.entry.text_bearing else "text-free", []
        ).append(run)
        strata.setdefault(
            "multi-revision" if run.entry.multi_revision else "single-revision", []
        ).append(run)
        strata.setdefault(f"producer:{run.entry.producer_family}", []).append(run)
    return {
        name: compute_metrics([_row(r, have) for r in group]) for name, group in strata.items()
    }
