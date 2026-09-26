"""Per-case differential: the pinned reference vs the candidate, on the
normalised key (docs/REDESIGN.md §5, Phase 0c).

Builds every selected case's PDF once, then scans it with both the
reference CLI (checked out via `scorecard.refs.worktree_for_ref`) and the
candidate CLI (the working tree), and diffs the normalised keys. Every
difference must be listed in `eval/accepted_diffs.yaml`, or the case's
diff is "unlisted" and the gate fails.

The reference almost never changes (the tag is pinned, and the case
library's generators change rarely), so its results can be cached across
CI runs, keyed by the reference commit plus the case library's build
lock hash (docs/REDESIGN.md §5, Phase 0c: "results cached by (PDF hash,
rules hash, tree hash)" — this is the cheap version of that: skip the
reference entirely on a cache hit, at the case-id granularity).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .accepted import AcceptedDiff, find_accepted, load_accepted_diffs
from .keys import NormalizedKey, effective_exit, normalize
from .metrics import CaseMetricRow
from .refs import REFERENCE_REF, candidate_verify_path, repo_root, resolve_commit, worktree_for_ref
from .runner import CliResult, current_python, run_cli

DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class CaseRun:
    """One side (reference or candidate) of a case's differential.

    *result* is the raw subprocess outcome, kept for diagnostics — it is
    None only for a reference run loaded from the cache, where nothing
    was actually launched this time.
    """

    case_id: str
    key: NormalizedKey | None  # None only when crashed or timed out
    crashed: bool
    timed_out: bool
    elapsed: float
    result: CliResult | None = None

    @staticmethod
    def from_result(
        case_id: str, result: CliResult, have: frozenset[str] | None = None
    ) -> "CaseRun":
        key = None if result.crashed else normalize(result.report, have=have)  # type: ignore[arg-type]
        return CaseRun(case_id, key, result.crashed, result.timed_out, result.elapsed, result)


@dataclass(frozen=True)
class DifferentialRun:
    """The result of `run_differential`: one `CaseDiff` per case, plus any
    `accepted_diffs.yaml` entry that no case in this run actually
    produced — "stale", and itself a gate failure, so the file cannot
    silently accumulate entries for changes that no longer happen (only
    computed on a full run, i.e. *case_ids* was None; a filtered run of
    a handful of cases would flag nearly everything as unused)."""

    diffs: list[CaseDiff]
    stale: tuple[AcceptedDiff, ...] = ()


@dataclass(frozen=True)
class CaseDiff:
    case_id: str
    reference: CaseRun
    candidate: CaseRun
    accepted: AcceptedDiff | None

    @property
    def crashed(self) -> bool:
        return self.reference.crashed or self.candidate.crashed

    @property
    def changed(self) -> bool:
        return not self.crashed and self.reference.key != self.candidate.key

    @property
    def ok(self) -> bool:
        """False when this case fails the gate: a crash, or a difference
        not covered by an exactly-matching accepted_diffs.yaml entry."""
        if self.crashed:
            return False
        return self.reference.key == self.candidate.key or self.accepted is not None

    @property
    def unlisted(self) -> bool:
        return self.changed and self.accepted is None

    def describe(self) -> str:
        if self.crashed:
            parts = []
            if self.reference.crashed:
                parts.append(f"reference crashed ({_crash_reason(self.reference)})")
            if self.candidate.crashed:
                parts.append(f"candidate crashed ({_crash_reason(self.candidate)})")
            return "; ".join(parts)
        if not self.changed:
            return "no change"
        assert self.reference.key is not None and self.candidate.key is not None
        diff = "; ".join(self.reference.key.diff(self.candidate.key))
        if self.accepted is not None:
            return f"accepted ({self.accepted.reason}): {diff}"
        return f"UNLISTED: {diff}"


def _crash_reason(run: CaseRun) -> str:
    if run.timed_out:
        return "timed out"
    if run.result is None:
        return "cached crash"
    if run.result.report is None:
        return f"no/invalid report (exit {run.result.returncode})"
    return f"unexpected exit {run.result.returncode}"


def select_cases(case_ids: Iterable[str] | None = None, *, have: frozenset[str] | None = None):
    """The registered cases to run: all of them (or the given ids) whose
    `requires` this environment satisfies. Mirrors
    `tests/test_case_library.py`'s skip logic."""
    from caselib import REGISTRY, load
    from caselib.run import available

    load()
    have = available() if have is None else have
    ids = sorted(case_ids) if case_ids is not None else sorted(REGISTRY)
    return [REGISTRY[i] for i in ids if not (REGISTRY[i].requires - have)]


def normalization_have(available_tools: frozenset[str]) -> frozenset[str] | None:
    """What `keys.normalize`/`effective_exit` should treat as *have*: the
    real availability, or None (drop nothing, use the exit exactly as
    reported) on a full-environment job (`REQUIRE_FULL_ENV=1`), where a
    missing tool must show up as a real difference rather than being
    silently absorbed as "this machine's own limitation".

    This is where REQUIRE_FULL_ENV is read — deliberately not inside
    `keys.is_environmental`/`effective_exit` themselves, which are pure
    functions of their arguments so their behaviour never depends on
    which job happens to be running them (mirrors
    `caselib.run._environmental`'s REQUIRE_FULL_ENV check, but applied at
    the orchestration layer instead of inside the pure comparison)."""
    return None if os.environ.get("REQUIRE_FULL_ENV") == "1" else available_tools


# ── reference result cache ──────────────────────────────────────────────

# Bump whenever the cache *file's own JSON shape* changes in a way the
# code hash below would not catch on its own (belt and braces — the code
# hash already invalidates on any scorecard source change).
CACHE_SCHEMA_VERSION = 2


def _lock_hash(root: Path) -> str:
    lock_path = root / "eval" / "caselib" / "cases.lock.json"
    data = lock_path.read_bytes() if lock_path.exists() else b""
    return hashlib.sha256(data).hexdigest()[:16]


def _scorecard_code_hash() -> str:
    """Hash of every eval/scorecard/*.py file — invalidates the cache the
    moment the normalisation logic itself changes, even without anyone
    remembering to bump CACHE_SCHEMA_VERSION."""
    pkg_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(pkg_dir.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _tool_version(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        return "absent"
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    return text[0] if text else "unknown"


def _environment_fingerprint(have: frozenset[str]) -> str:
    """qpdf/exiftool versions, OCR availability, the PyMuPDF version, and
    whether REQUIRE_FULL_ENV changes what gets cached (it changes the
    `have` passed to `normalize` — see `normalization_have` — so it
    changes the stored keys, not just this machine's raw capability) —
    the reference's own results depend on all of these, so a cache entry
    from a different environment or mode must never be reused."""
    import fitz

    parts = [
        f"ocr={'1' if 'ocr' in have else '0'}",
        f"qpdf={_tool_version('qpdf')}",
        f"exiftool={_tool_version('exiftool')}",
        f"pymupdf={fitz.VersionBind}",
        f"require_full_env={'1' if normalization_have(have) is None else '0'}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def reference_cache_key(reference_ref: str, root: Path, have: frozenset[str]) -> str:
    """A key that changes whenever the reference commit moves, the case
    library's generators change, the scorecard's own comparison logic
    changes, or this machine's tool versions/OCR availability differ —
    never a mutable ref name, and never anything about the candidate."""
    commit = resolve_commit(reference_ref, root)
    return "-".join((
        str(CACHE_SCHEMA_VERSION),
        commit,
        _lock_hash(root),
        _scorecard_code_hash(),
        _environment_fingerprint(have),
    ))


def load_reference_cache(cache_dir: Path, key: str) -> dict[str, CaseRun] | None:
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if payload.get("key") != key:
        return None
    runs: dict[str, CaseRun] = {}
    for case_id, entry in payload.get("cases", {}).items():
        # A crashed/timed-out run is never trusted from cache, even if an
        # older version of this code once wrote one: treat it as a miss
        # so it is retried, rather than freezing a flake into every
        # future run.
        if entry.get("crashed"):
            continue
        key_data = entry.get("key")
        normalized = NormalizedKey.from_jsonable(key_data) if key_data is not None else None
        runs[case_id] = CaseRun(
            case_id, normalized, entry["crashed"], entry["timed_out"], entry["elapsed"], None
        )
    return runs


def save_reference_cache(cache_dir: Path, key: str, runs: dict[str, CaseRun]) -> None:
    """Persist *runs* — silently dropping any crashed or timed-out entry,
    so one flaky reference run never poisons the cache for everyone
    after it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": key,
        "cases": {
            case_id: {
                "key": run.key.to_jsonable() if run.key is not None else None,
                "crashed": run.crashed,
                "timed_out": run.timed_out,
                "elapsed": run.elapsed,
            }
            for case_id, run in runs.items()
            if not run.crashed
        },
    }
    (cache_dir / f"{key}.json").write_text(json.dumps(payload, sort_keys=True) + "\n")


# ── running ──────────────────────────────────────────────────────────────


def run_differential(
    case_ids: Iterable[str] | None = None,
    *,
    out_dir: Path,
    reference_ref: str = REFERENCE_REF,
    accepted_path: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_workers: int | None = None,
    progress: Callable[[CaseDiff], None] | None = None,
    cache_dir: Path | None = None,
) -> DifferentialRun:
    """Build every selected case once, then scan it with the reference and
    the candidate CLI. Returns one `CaseDiff` per case (sorted by case
    id) plus any stale `accepted_diffs.yaml` entry (only computed when
    *case_ids* is None, i.e. every case ran).

    When *cache_dir* is given and already holds every selected case's
    reference result under today's `reference_cache_key`, the reference
    worktree and subprocess runs are skipped entirely."""
    from caselib.run import available, build as build_case

    from .accepted import DEFAULT_PATH

    root = repo_root()
    accepted = load_accepted_diffs(accepted_path or DEFAULT_PATH)
    have = available()               # true capability: which cases can run at all
    filter_have = normalization_have(have)   # what normalize()/effective_exit() should use
    cases = select_cases(case_ids, have=have)
    out_dir.mkdir(parents=True, exist_ok=True)
    python_exe = current_python()
    workers = max_workers or min(32, (os.cpu_count() or 4) * 4)

    cached: dict[str, CaseRun] = {}
    cache_key: str | None = None
    if cache_dir is not None:
        cache_key = reference_cache_key(reference_ref, root, have)
        cached = load_reference_cache(cache_dir, cache_key) or {}

    pdfs = {case.id: build_case(case, out_dir / f"{case.id}.pdf") for case in cases}

    reference_runs: dict[str, CaseRun] = dict(cached)
    missing = [case for case in cases if case.id not in reference_runs]
    if missing:
        with worktree_for_ref(reference_ref, root=root) as reference_verify:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        run_cli, python_exe, reference_verify, pdfs[case.id], case.rules,
                        out_dir / "reference", case.id, timeout=timeout,
                    ): case
                    for case in missing
                }
                for future in concurrent.futures.as_completed(futures):
                    case = futures[future]
                    reference_runs[case.id] = CaseRun.from_result(
                        case.id, future.result(), have=filter_have
                    )
        if cache_dir is not None and cache_key is not None:
            save_reference_cache(cache_dir, cache_key, reference_runs)

    candidate_verify = candidate_verify_path(root)
    candidate_runs: dict[str, CaseRun] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                run_cli, python_exe, candidate_verify, pdfs[case.id], case.rules,
                out_dir / "candidate", case.id, timeout=timeout,
            ): case
            for case in cases
        }
        for future in concurrent.futures.as_completed(futures):
            case = futures[future]
            candidate_runs[case.id] = CaseRun.from_result(
                case.id, future.result(), have=filter_have
            )

    results: list[CaseDiff] = []
    for case in cases:
        reference = reference_runs[case.id]
        candidate = candidate_runs[case.id]
        found = None
        if reference.key is not None and candidate.key is not None and reference.key != candidate.key:
            found = find_accepted(accepted, case.id, reference.key, candidate.key)
        diff = CaseDiff(case.id, reference, candidate, found)
        results.append(diff)
        if progress is not None:
            progress(diff)
    results.sort(key=lambda d: d.case_id)

    # Only meaningful on a full run: a filtered subset would flag nearly
    # every entry as unused just because its case never ran.
    stale = stale_accepted_diffs(accepted, results) if case_ids is None else ()
    return DifferentialRun(results, stale)


def stale_accepted_diffs(
    accepted: dict[str, list[AcceptedDiff]], diffs: Sequence[CaseDiff]
) -> tuple[AcceptedDiff, ...]:
    """`accepted_diffs.yaml` entries that none of *diffs* actually matched
    — the change they describe no longer happens, so the entry is stale
    and must be removed (or the run that should have produced it is
    missing). Compare against a full run's diffs; a filtered subset makes
    every entry not touched by it look unused."""
    used = {d.accepted for d in diffs if d.accepted is not None}
    all_entries = [entry for entries in accepted.values() for entry in entries]
    return tuple(entry for entry in all_entries if entry not in used)


def unlisted_diffs(diffs: Sequence[CaseDiff]) -> list[CaseDiff]:
    return [d for d in diffs if not d.ok]


def to_metric_row(
    case: Any, result: CliResult, have: frozenset[str] | None = None
) -> CaseMetricRow:
    """A case + its candidate CLI result, reduced to a metrics row
    (`scorecard.metrics`) — the correct verdict comes from the case's own
    `expected` (docs/REDESIGN.md §5), not from a known gap's `today`, so a
    documented gap still counts as a silent miss or downgrade here.
    *have* drops environmental warnings the same way the differential
    does, so the exit code used here is comparable across environments
    (e.g. a case's exit is not counted as 2 on Linux merely because OCR
    is absent)."""
    report = result.report
    has_hard = bool(
        report and any(f.get("tier") == "hard" for f in report.get("findings", []))
    )
    actual_exit = None if result.crashed else effective_exit(report, have)  # type: ignore[arg-type]
    return CaseMetricRow(
        case_id=case.id,
        truth=case.truth,
        expected_exit=case.expected.exit,
        actual_exit=actual_exit,
        has_hard_finding=has_hard,
        crashed=result.crashed,
        timed_out=result.timed_out,
        elapsed=result.elapsed,
    )


def run_candidate(
    case_ids: Iterable[str] | None = None,
    *,
    out_dir: Path,
    timeout: float = DEFAULT_TIMEOUT,
    max_workers: int | None = None,
) -> list[CaseMetricRow]:
    """Run the candidate CLI alone over the selected cases, for the
    scorecard's label-based metrics — no reference or worktree needed."""
    from caselib.run import available, build as build_case

    root = repo_root()
    have = available()
    filter_have = normalization_have(have)
    cases = select_cases(case_ids, have=have)
    out_dir.mkdir(parents=True, exist_ok=True)
    python_exe = current_python()
    candidate_verify = candidate_verify_path(root)

    rows: list[CaseMetricRow] = []
    workers = max_workers or min(32, (os.cpu_count() or 4) * 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for case in cases:
            pdf = build_case(case, out_dir / f"{case.id}.pdf")
            futures[
                pool.submit(
                    run_cli,
                    python_exe,
                    candidate_verify,
                    pdf,
                    case.rules,
                    out_dir / "candidate",
                    case.id,
                    timeout=timeout,
                )
            ] = case
        for future in concurrent.futures.as_completed(futures):
            case = futures[future]
            rows.append(to_metric_row(case, future.result(), have=filter_have))
    return rows
