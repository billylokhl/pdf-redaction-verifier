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
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .keys import effective_exit
from .metrics import CaseMetricRow, Scorecard, compute_metrics
from .runner import CliResult, current_python, error_code, run_cli

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
    # None: unknown, because the probe could not read the pages (see
    # `classify`). Never guessed either way.
    text_bearing: bool | None
    multi_revision: bool
    producer_family: str  # coarse bucket only — never the raw /Producer string
    # Whether the probe below could read the file: one of PROBE_OUTCOMES.
    # Defaults to "ok" so a manifest written before the field existed (which
    # could hold no other kind: `classify` used to raise on them) still loads.
    probe: str = "ok"


# What `classify`'s probe made of a file. "password": MuPDF opened it but
# needs a password (the CLI's PDF_PASSWORD). "unreadable": MuPDF could not
# open it (the CLI's PDF_UNREADABLE). "text-unreadable": MuPDF opened it
# but failed reading a page's text — the CLI has no matching error code,
# so its verdict there is whatever the scan reaches. None keeps the
# exception's message: it can name the path.
PROBE_OUTCOMES = ("ok", "password", "unreadable", "text-unreadable")


def classify(pdf_path: Path) -> tuple[bool | None, bool, str, str]:
    """(text_bearing, multi_revision, producer_family, probe) via a quick
    probe. The raw `/Producer` string is read here and immediately reduced
    to a coarse family — it is never returned or stored (it can hold a
    username, hostname, or email address some producers embed).

    A file the probe cannot read (*probe* other than "ok") is recorded,
    not raised on: `corpus run` counts the CLI's exit 2 on an encrypted or
    corrupt file as a review verdict (owner's decision), which it could
    never do if the build aborted. Its text_bearing is None, unknown, so
    `stratify` keeps it out of every measured stratum. Either guess would
    make a measured stratum look better than it is: the CLI's error report
    on a "password" or "unreadable" file has no findings, so counting it as
    text-bearing adds an unscanned file to the false-hard denominator of
    the stratum the redesign's targets are measured on, and counting it as
    text-free does the same there. A "text-unreadable" file is scanned by
    the CLI, so its verdict is real but has no known text stratum: it is
    its own counted group, `probe:text-unreadable`, reported on every run
    (owner decision, 2026-10-12). Its producer family is "unknown", since its
    metadata was not read. multi_revision is still measured, from the raw
    bytes, which need no parser."""
    import pymupdf as fitz

    multi_revision = pdf_path.read_bytes().count(b"%%EOF") > 1
    try:
        doc = fitz.open(pdf_path)
    except Exception:  # any open failure; the message is never kept
        return None, multi_revision, "unknown", "unreadable"
    try:
        if doc.needs_pass:
            return None, multi_revision, "unknown", "password"
        try:
            text_bearing = any(page.get_text().strip() for page in doc)
        except Exception:
            return None, multi_revision, "unknown", "text-unreadable"
        try:
            raw_producer = (doc.metadata or {}).get("producer") or None
        except Exception:
            raw_producer = None  # family "unknown", as for a file without one
    finally:
        doc.close()
    return text_bearing, multi_revision, producer_family(raw_producer), "ok"


@dataclass(frozen=True)
class CorpusFiles:
    """What a walk of the corpus root found: the PDFs to record, and counts
    — never names — of everything it left out, so nothing is left out
    silently."""

    pdfs: tuple[Path, ...]
    other_files: int = 0  # files without a .pdf extension (any case)
    not_regular: int = 0  # *.pdf entries that are not files: broken links, sockets, ...
    linked_dirs: int = 0  # symbolic links to directories: not followed
    unreadable_dirs: int = 0  # directories the walk could not list
    unreadable_files: int = 0  # *.pdf entries the walk could not even stat

    def skipped(self, unread: int = 0) -> dict[str, int]:
        """Counts by kind; *unread* adds files `build_manifest` could not
        read, which it leaves out of the manifest."""
        return {
            "file(s) without a .pdf extension": self.other_files,
            "entr(y/ies) named .pdf but not a regular file": self.not_regular,
            "symlinked dir(s) not followed": self.linked_dirs,
            "unreadable dir(s)": self.unreadable_dirs,
            "unreadable file(s)": self.unreadable_files + unread,
        }


def find_pdfs(root: Path) -> CorpusFiles:
    """Every regular file under *root* whose name ends in `.pdf` in any
    case (`.PDF`, `.Pdf`, ...), sorted, plus counts of what was skipped.
    A symbolic link to a file counts as that file; a link to a directory
    is not followed (a link cycle could otherwise loop), but is counted."""
    if not root.is_dir():
        # The caller supplied the path, but it is still never echoed.
        raise SystemExit("refusing to build: --root is not an existing directory")
    try:  # os.walk would report an unlistable root as one skipped directory
        os.scandir(root).close()
    except OSError:
        raise SystemExit("refusing to build: --root cannot be listed") from None
    pdfs: list[Path] = []
    other = not_regular = linked = unreadable = unreadable_files = 0

    def on_error(_exc: OSError) -> None:
        nonlocal unreadable
        unreadable += 1

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        linked += sum(os.path.islink(os.path.join(dirpath, d)) for d in dirnames)
        for name in filenames:
            path = Path(dirpath) / name
            if not name.lower().endswith(".pdf"):
                other += 1
            else:
                try:  # EACCES is not swallowed here; its message names the path
                    regular = path.is_file()
                except OSError:
                    unreadable_files += 1
                    continue
                if regular:
                    pdfs.append(path)
                else:
                    not_regular += 1
    return CorpusFiles(tuple(sorted(pdfs)), other, not_regular, linked, unreadable,
                       unreadable_files)


def build_manifest(root: Path, *, manifest_path: Path = DEFAULT_MANIFEST,
                   files: CorpusFiles | None = None) -> list[CorpusEntry]:
    """Hash and classify every PDF `find_pdfs` finds under *root* (or the
    already-walked *files*), and write the manifest. Re-running replaces
    it entirely, so a file removed from *root* is dropped and a changed
    file's hash and strata are refreshed. A file the probe cannot parse is
    recorded with its text layer unknown (see `classify`). A file that
    cannot be read at all (a permission error, say) cannot be hashed, so
    it is left out and counted — `len(files.pdfs) - len(entries)` — never
    raised on: the exception's message names the path.

    Refuses (SystemExit, nothing written) when no file could be recorded:
    a mistyped or unmounted root would otherwise replace a good manifest
    with an empty one. The manifest is written to a temporary file and
    renamed into place, so a failed write never leaves half of one."""
    if files is None:
        files = find_pdfs(root)
    entries: list[CorpusEntry] = []
    for path in files.pdfs:
        try:
            data = path.read_bytes()
            text_bearing, multi_revision, family, probe = classify(path)
        except OSError:
            continue  # counted by the caller; the message is never kept
        sha256 = hashlib.sha256(data).hexdigest()
        entries.append(
            CorpusEntry(
                sha256=sha256,
                path=str(path.relative_to(root)),
                text_bearing=text_bearing,
                multi_revision=multi_revision,
                producer_family=family,
                probe=probe,
            )
        )
    if not entries:
        counts = ", ".join(f"{n} {what}" for what, n in
                           files.skipped(len(files.pdfs)).items() if n) or "nothing found"
        raise SystemExit("refusing to write an empty manifest: no readable PDF under --root "
                         f"({counts}); any existing manifest is left as it was")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"entries": [vars(e) for e in entries]}
    partial = manifest_path.with_name(manifest_path.name + ".partial")
    partial.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(partial, manifest_path)
    return entries


def load_manifest(manifest_path: Path = DEFAULT_MANIFEST) -> list[CorpusEntry]:
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text())
    entries = [CorpusEntry(**e) for e in payload.get("entries", [])]
    for entry in entries:
        # A hand-edited value would invent a stratum; the message names the
        # value only, never the entry's path.
        if entry.probe not in PROBE_OUTCOMES:
            raise SystemExit(f"manifest entry {entry.sha256[:16]} has an unknown probe "
                             f"{entry.probe!r}: rebuild it with `corpus build`")
        if not isinstance(entry.text_bearing, (bool, type(None))):
            raise SystemExit(f"manifest entry {entry.sha256[:16]} has text_bearing "
                             f"{entry.text_bearing!r}, not true, false or null: rebuild it "
                             "with `corpus build`")
        if (entry.probe == "ok") != (entry.text_bearing is not None):
            raise SystemExit(f"manifest entry {entry.sha256[:16]} has probe {entry.probe!r} "
                             f"but text_bearing {entry.text_bearing!r}: rebuild it with "
                             "`corpus build`")
    return entries


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


def metric_row(run: CorpusRun, have: frozenset[str] | None) -> CaseMetricRow:
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
        error_code=error_code(run.result),
    )


# The CLI error code a probe outcome predicts: `classify` and the CLI open
# a file the same way (fitz.open, then needs_pass).
PROBE_ERROR_CODES = {"password": "PDF_PASSWORD", "unreadable": "PDF_UNREADABLE"}


def probe_mismatches(runs: list[CorpusRun]) -> list[tuple[str, str, str | None]]:
    """(SHA-256 prefix, probe, CLI error code) for every scanned file whose
    stratum the build probe cannot vouch for. `stratify` places a file by
    its build-time probe, so a file the probe could not open but the CLI
    scanned (after a PyMuPDF upgrade, say) would have its verdict, hard
    findings included, left out of every measured stratum; and a file the
    probe opened but the CLI could not would pad a measured stratum's
    false-hard denominator. A "text-unreadable" file opened, so only a CLI
    that could not open it contradicts the probe; otherwise it is its own
    counted group (owner decision, 2026-10-12). A crash, a timeout or a
    run-level error code (INTERNAL_ERROR, ...) is left to the run's own
    unmeasured check, which already fails it."""
    found = []
    for run in runs:
        if run.result.crashed or run.result.timed_out:
            continue
        code = error_code(run.result)
        if code is not None and code not in PROBE_ERROR_CODES.values():
            continue  # a run-level error: the run's unmeasured check lists it
        probe = run.entry.probe
        expected = PROBE_ERROR_CODES.get(probe)
        if code != expected:
            found.append((run.entry.sha256[:16], probe, code))
    return found


def stratify(runs: list[CorpusRun], have: frozenset[str] | None = None) -> dict[str, Scorecard]:
    """Clean-side metrics overall and per stratum: text-bearing vs
    text-free, multi-revision vs not, and each producer family seen. A
    file the build probe could not read is counted in "all" (its exit 2
    is a review verdict, the owner's decision) and otherwise only in
    "text-unknown" and its own `probe:password`, `probe:unreadable` or
    `probe:text-unreadable` stratum, never in a measured one — which is
    safe only when the CLI agreed with the probe: check `probe_mismatches`.
    `probe:text-unreadable` is always present, even empty, so its count is
    reported on every run.
    *have* drops environmental warnings (e.g. no OCR on this machine) the
    same way the differential does, so a run on a partial environment
    does not inflate the review rate — pass `caselib.run.available()` for that;
    omit it to use the exit code exactly as reported."""
    # Always present, even empty: the owner asked for this count every run.
    strata: dict[str, list[CorpusRun]] = {"all": list(runs), "probe:text-unreadable": []}
    for run in runs:
        if run.entry.probe != "ok":
            # Unread: nothing measured its text layer or producer, and its
            # error report has no findings, so in any measured stratum it
            # would only pad the false-hard denominator. Revisions are
            # measured from bytes, but are left out for the same reason.
            strata.setdefault("text-unknown", []).append(run)
            strata.setdefault(f"probe:{run.entry.probe}", []).append(run)
            continue
        strata.setdefault(
            "text-bearing" if run.entry.text_bearing else "text-free", []
        ).append(run)
        strata.setdefault(
            "multi-revision" if run.entry.multi_revision else "single-revision", []
        ).append(run)
        strata.setdefault(f"producer:{run.entry.producer_family}", []).append(run)
    return {
        name: compute_metrics([metric_row(r, have) for r in group]) for name, group in strata.items()
    }
