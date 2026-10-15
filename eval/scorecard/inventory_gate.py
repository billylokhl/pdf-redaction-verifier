"""The 3a-6 inventory agreement gate (docs/phase3a-plan.md, ADR 0010):
every file's inventory built in a child process with a timeout, then
judged against MuPDF and qpdf by the shared oracle (`scorecard.inventory`)
in a second child with its own, generous timeout.

THE GATE: zero unflagged disagreements (a file that does not tile counts
as one too), zero crashes, zero timeouts (> 60 s), and every unflagged
file actually verified -- an oracle that crashed or timed out on an
unflagged file fails the gate, it never counts as an agreement. Flagged
files are reported by reason, never gated (ADR 0010: measure before
enforcing), as are UNINDEXED/CONTESTED regions (owner decision 3).

Privacy: `aggregate()` holds counts, rates and timings only -- no file
name, path, SHA-256, label, document byte or reader message. Per-file
detail (`write_detail`) is keyed by SHA-256 and belongs only in the
gitignored `eval/scorecard/real_corpus/`; it holds statuses, flag
reasons, integers and scrubbed reader-message templates, never document
bytes. Children's stderr is reduced to an exception's type name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from .inventory import Agreement, Check

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_TIMEOUT = 60.0      # owner decision 3: more than 60 s per file is a failure
ORACLE_TIMEOUT = 900.0    # the readers' side: generous, and reported separately
BUILD_COMMAND: tuple[str, ...] = (sys.executable, "-m", "redaction_verifier.inventory")
ORACLE_COMMAND: tuple[str, ...] = (sys.executable, "-m", "scorecard.inventory")

# A file's verdict in the gate.
AGREE = "agree"              # unflagged, and every reader comparison agrees
FLAGGED = "flagged"          # the inventory flagged it: reported by reason
DISAGREE = "disagree"        # unflagged and a reader disagrees, or no tiling: GATE
CRASH = "crash"              # build_inventory's child failed: GATE
TIMEOUT = "timeout"          # build_inventory's child ran past its timeout: GATE
UNVERIFIED = "unverified"    # unflagged, but the oracle crashed or timed out: GATE
INCONSISTENT = "inconsistent"  # the two children's flags differ: GATE
GATING = (DISAGREE, CRASH, TIMEOUT, UNVERIFIED, INCONSISTENT)
# The flag reason a file gets when a reader needs a password to open it
# (owner decision, 2026-10-12, #48 item 3): it cannot be certified, so it is
# FLAGGED, neither a disagreement nor exempt. Not one of the inventory's own
# FlagReasons, so it never collides with them in the aggregate.
NEEDS_PASSWORD = "needs_password"


@dataclass(frozen=True)
class Job:
    """One file: *key* its SHA-256, *label* a public name (a case id) or
    None for a corpus file, *group* a public stratum or None."""

    key: str
    path: Path
    label: str | None = None
    group: str | None = None


@dataclass(frozen=True)
class ChildRun:
    outcome: str                       # "ok", "crash" or "timeout"
    elapsed: float
    payload: dict[str, Any] | None
    error: str = ""                    # an exception type or "exit N": never a message


@dataclass(frozen=True)
class FileResult:
    job: Job
    size: int
    verdict: str
    build: ChildRun
    oracle: ChildRun | None
    agreement: Agreement | None
    reasons: tuple[str, ...] = ()
    measures: dict[str, int] = field(default_factory=dict)
    regions: dict[str, dict[str, int]] = field(default_factory=dict)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(REPO_ROOT / "eval"), str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _error_kind(stderr: bytes, returncode: int) -> str:
    """The exception class a child died of, else its exit status. Only the
    class name, taken from the first unindented line after the last
    Python traceback's frames -- never a line of the message, which can
    run over several lines and quote the document."""
    lines = stderr.decode("latin-1").splitlines()
    starts = [i for i, line in enumerate(lines) if line == "Traceback (most recent call last):"]
    if starts:
        for line in lines[starts[-1] + 1:]:
            if line[:1].isspace():
                continue  # a frame, its source line, or a caret line
            name = line.split(":", 1)[0]
            if name and all(part.isidentifier() for part in name.split(".")):
                return name.rsplit(".", 1)[-1][:80]
            break
    return f"exit {returncode}"


def run_child(argv: Sequence[str], timeout: float) -> ChildRun:
    """Run *argv*, which must print one JSON object, with a wall-clock
    *timeout*. The child gets its own process group, so a timeout kills
    what it started too (qpdf)."""
    start = time.monotonic()
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=REPO_ROOT, env=_child_env(), start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return ChildRun("timeout", time.monotonic() - start, None, "timeout")
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        return ChildRun("crash", elapsed, None, _error_kind(err, proc.returncode))
    try:
        # The last non-empty line: anything a library printed first is ignored.
        payload = json.loads(next(line for line in reversed(out.splitlines()) if line.strip()))
    except (ValueError, StopIteration):
        return ChildRun("crash", elapsed, None, "unreadable output")
    if not isinstance(payload, dict):
        return ChildRun("crash", elapsed, None, "unreadable output")
    return ChildRun("ok", elapsed, payload)


def run_file(job: Job, *, workdir: Path, build_timeout: float = BUILD_TIMEOUT,
             oracle_timeout: float = ORACLE_TIMEOUT,
             build_command: Sequence[str] = BUILD_COMMAND,
             oracle_command: Sequence[str] = ORACLE_COMMAND) -> FileResult:
    try:
        return _run_file(job, workdir, build_timeout, oracle_timeout, build_command,
                         oracle_command)
    except Exception as error:  # the harness itself failed: a gate failure, never a pass
        failed = ChildRun("crash", 0.0, None, f"harness {type(error).__name__}")
        return FileResult(job, 0, CRASH, failed, None, None)


def _run_file(job: Job, workdir: Path, build_timeout: float, oracle_timeout: float,
              build_command: Sequence[str],
              oracle_command: Sequence[str]) -> FileResult:
    size = job.path.stat().st_size
    build = run_child([*build_command, str(job.path)], build_timeout)
    if build.outcome != "ok":
        return FileResult(job, size, CRASH if build.outcome == "crash" else TIMEOUT, build,
                          None, None)
    scratch = Path(tempfile.mkdtemp(prefix="oracle-", dir=workdir))
    try:
        oracle = run_child([*oracle_command, str(job.path), str(scratch),
                            "--qpdf-timeout", str(oracle_timeout)], oracle_timeout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return judge(job, size, build, oracle)


def judge(job: Job, size: int, build: ChildRun, oracle: ChildRun) -> FileResult:
    """A file's verdict from its two children. Fail closed: anything that
    is not a verified agreement or a consistent flag is a gate failure."""
    summary = build.payload or {}
    try:
        flags = summary["flags"]
        reasons = tuple(sorted(str(reason) for reason in flags))
        tiles = summary["tiles"] is True
    except (KeyError, TypeError):
        return FileResult(job, size, CRASH, replace_error(build, "unreadable summary"), oracle,
                          None)
    agreement: Agreement | None = None
    oracle_reasons: tuple[str, ...] = ()
    payload = oracle.payload or {}
    measures: dict[str, int] = {}
    regions: dict[str, dict[str, int]] = {}
    if oracle.outcome == "ok":
        try:
            agreement = Agreement.from_json(payload["agreement"])
            oracle_reasons = tuple(sorted(set(payload["flags"])))
            measures = {str(k): int(v) for k, v in payload["measures"].items()}
            regions = {str(k): {str(a): int(b) for a, b in v.items()}
                       for k, v in payload["regions"].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            oracle = replace_error(oracle, "unreadable output")
            agreement = None
    if agreement is not None and oracle_reasons != reasons:
        verdict = INCONSISTENT  # the same inventory, built twice, must flag alike
    elif not tiles or (agreement is not None and agreement.check is Check.TILING):
        verdict = DISAGREE
    elif (not reasons and agreement is not None and agreement.disagrees
          and agreement.check is Check.NEEDS_PASSWORD):
        verdict = FLAGGED       # cannot certify: flagged, never an agreement
        reasons = (NEEDS_PASSWORD,)
    elif reasons:
        verdict = FLAGGED       # whatever the oracle did: the file is flagged
    elif agreement is None:
        verdict = UNVERIFIED    # unflagged, but nothing was compared
    elif agreement.agrees:
        verdict = AGREE
    elif agreement.disagrees:
        verdict = DISAGREE
    else:
        verdict = INCONSISTENT  # unflagged in one child, flagged in the other
    return FileResult(job, size, verdict, build, oracle, agreement, reasons, measures, regions)


def replace_error(run: ChildRun, error: str) -> ChildRun:
    return ChildRun("crash", run.elapsed, None, error)


def run_gate(jobs: Sequence[Job], *, workdir: Path, workers: int = 1,
             progress: Callable[[int, int], None] | None = None,
             **options: Any) -> list[FileResult]:
    """Run every job (*options* as `run_file`'s), in *workers* parallel
    pairs of children. Results in the jobs' order."""
    results: list[FileResult | None] = [None] * len(jobs)
    done = 0

    def one(index: int) -> None:
        nonlocal done
        results[index] = run_file(jobs[index], workdir=workdir, **options)
        done += 1
        if progress is not None:
            progress(done, len(jobs))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, range(len(jobs))))
    finished = [r for r in results if r is not None]
    if len(finished) != len(jobs):  # never report a partial run as the whole
        raise RuntimeError("the gate lost results")
    return finished


# ── The aggregate: counts only ────────────────────────────────────────────
def _percentiles(values: Iterable[float], digits: int = 3) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {}

    def at(q: float) -> float:  # nearest rank
        return round(ordered[min(len(ordered) - 1, max(0, int(q * len(ordered) + 0.999999) - 1))],
                     digits)
    return {"p50": at(0.50), "p90": at(0.90), "p99": at(0.99), "max": round(ordered[-1], digits)}


def _flag_counts(results: Sequence[FileResult]) -> dict[str, Any]:
    flagged = [r for r in results if r.verdict == FLAGGED]
    by_reason = Counter(reason for r in flagged for reason in r.reasons)
    sole = Counter(r.reasons[0] for r in flagged if len(r.reasons) == 1)
    return {
        "files_by_reason": dict(sorted(by_reason.items())),
        "top_reasons": [[reason, n] for reason, n in
                        sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))[:10]],
        "files_with_one_reason": dict(sorted(sole.items())),
    }


def _pending(results: Sequence[FileResult]) -> dict[str, Any]:
    """The owner's pending decisions (issues #44 and #46), measured."""
    measured = [r for r in results if r.measures]

    def files(key: str, among: Sequence[FileResult] = measured) -> int:
        return sum(1 for r in among if r.measures.get(key, 0) > 0)

    def total(key: str) -> int:
        return sum(r.measures.get(key, 0) for r in measured)
    updated = [r for r in measured if r.measures.get("linearized_updated")]
    length_off = [r for r in measured if r.measures.get("linearized_length_mismatch")]
    length = [r for r in measured if r.measures.get("length_in_objstm")]
    ambiguous = [r for r in measured if "revision_ambiguous" in r.reasons]
    dead_objstm = [r for r in measured if r.measures.get("dead_objstms")]
    comments = [r for r in measured
                if r.measures.get("header_comment_lines") or r.measures.get("eof_comment_lines")]
    return {
        "files_measured": len(measured),
        "linearized_updates": {  # #44 item 1: flagged today
            "linearized_files": files("linearized"),
            # More than one revision: the signal. /L off the file's length
            # (reported apart: trailing bytes alone do that too).
            "updated_files": len(updated),
            "length_mismatch_files": len(length_off),
            "length_mismatch_single_revision": sum(
                1 for r in length_off if not r.measures.get("linearized_updated")),
            "updated_flagged": sum(1 for r in updated if r.verdict == FLAGGED),
            "updated_reasons": dict(sorted(Counter(
                reason for r in updated for reason in r.reasons).items())),
        },
        "comment_lines": {  # #46 item 1: claimed without a flag
            "files": len(comments),
            "files_after_header": files("header_comment_lines"),
            "files_after_eof": files("eof_comment_lines"),
            "lines": total("header_comment_lines") + total("eof_comment_lines"),
            "longest_line_bytes": max((r.measures.get("comment_longest", 0) for r in measured),
                                      default=0),
            "lines_non_printable": total("comment_nonprintable"),
            "lines_obj_like": total("comment_obj_like"),
        },
        "length_in_object_stream": {  # #46 item 4: not resolved, LENGTH_MISMATCH
            "files": len(length),
            "streams": total("length_in_objstm"),
            "files_flagged_only_length_mismatch": sum(
                1 for r in length if r.reasons == ("length_mismatch",)),
        },
        "revision_ambiguous": {  # #46 item 4: flagged even when the value is equal
            "files": len(ambiguous),
            "flags_equal": total("ambiguous_equal"),
            "flags_differ": total("ambiguous_differ"),
            "flags_unresolved": total("ambiguous_unresolved"),
            "files_only_reason_all_equal": sum(
                1 for r in ambiguous if r.reasons == ("revision_ambiguous",)
                and r.measures.get("ambiguous_equal", 0) > 0
                and not r.measures.get("ambiguous_differ")
                and not r.measures.get("ambiguous_unresolved")),
        },
        "dead_object_streams": {  # #46 item 4: not decoded
            "files": len(dead_objstm),
            "streams": total("dead_objstms"),
            "files_unflagged": sum(1 for r in dead_objstm if r.verdict == AGREE),
            "files_with_dead_bodies": files("dead_bodies"),
            "dead_bodies": total("dead_bodies"),
        },
        "labels": _labels([r for r in measured if r.verdict == AGREE]),
        "labels_cost": _labels_cost([r for r in measured if r.verdict == AGREE]),
    }


def _labels(agree: Sequence[FileResult]) -> dict[str, Any]:
    """3a-7e-1 (ADR 0011 item 5), over the unflagged files' current
    revisions: files and streams with each STRUCTURAL kind, hint streams
    under a stale /L (coordinator decision Q8), and the reference graph's
    work (item 7)."""
    kinds = sorted({key for r in agree for key in r.measures if key.startswith("structural_")})
    work = [r.measures.get("graph_work", 0) for r in agree]
    return {
        "files": len(agree),
        "structural_files": {key[len("structural_"):]: sum(
            1 for r in agree if r.measures.get(key, 0) > 0) for key in kinds},
        "structural_streams": {key[len("structural_"):]: sum(
            r.measures.get(key, 0) for r in agree) for key in kinds},
        "hint_stream_stale_files": sum(1 for r in agree if r.measures.get("hint_stream_stale")),
        "files_missing_labels": sum(1 for r in agree if r.measures.get("labels_missing")),
        "graph_work": _percentiles(work, 0),
        "drawing": _drawing(agree),
    }


# 3a-7e-3: what would exit 2 once labels gate (3b's verdict, 4a's decoders),
# by class, over the unflagged files (ADR 0011's "What it costs" table):
# each class names the measure that puts a file in it.
_EXIT_2_CLASSES: Final = (
    ("orphan", "bodies_orphaned"),                  # R3, per body (item 5's definition)
    ("hint_stream", "structural_hint_stream"),      # R6, ADR 0003
    ("xmp_image", "xmp_image_bodies"),              # R7, decision D
    ("javascript", "structural_javascript"),        # no decoder yet
    ("synthesized", "synthesized_current"),         # until 4a decodes it
    ("output_intent", "structural_output_intent"),  # no decoder yet
)


def _exit_2_classes(measures: dict[str, int]) -> list[str]:
    return [name for name, key in _EXIT_2_CLASSES if measures.get(key, 0) > 0]


def _thumbnails_only(measures: dict[str, int]) -> bool:
    """Every exit-2 reason of the file is a page thumbnail's orphan, and
    today's CLI already marks a page thumbnail (#133)."""
    return (bool(measures.get("page_thumb_legacy"))
            and _exit_2_classes(measures) == ["orphan"]
            and measures.get("bodies_orphaned", 0)
            == measures.get("orphan_body_class_thumbnail", 0))


def _files_and_total(agree: Sequence[FileResult], key: str) -> dict[str, int]:
    return {"files": sum(1 for r in agree if r.measures.get(key, 0) > 0),
            "total": sum(r.measures.get(key, 0) for r in agree)}


def _labels_cost(agree: Sequence[FileResult]) -> dict[str, Any]:
    """3a-7e-3 (ADR 0011 items 5 and 7; R3, R6, R7), over the unflagged
    files: files in each exit-2 class and in any (each file once), and in
    one class only; the orphan classes per body; the thumbnail orphans
    whose pages today's CLI already marks (#133); XMP packets holding an
    image or binary (scanned in eval); and the graph's work against item
    7's sum."""
    classes = {name: sum(1 for r in agree if r.measures.get(key, 0) > 0)
               for name, key in _EXIT_2_CLASSES}
    hit = [r for r in agree if _exit_2_classes(r.measures)]
    only = Counter(found[0] for r in hit if len(found := _exit_2_classes(r.measures)) == 1)
    body = sorted({key for r in agree for key in r.measures
                   if key.startswith("orphan_body_class_")})
    work = sorted((r.measures.get("graph_work", 0), r.measures.get("graph_sum", 0))
                  for r in agree)
    return {
        "files": len(agree),
        "exit_2_files": len(hit),
        "exit_2_by_class": classes,
        "exit_2_only_class": dict(sorted(only.items())),
        "exit_2_files_but_thumbnails_marked_today": sum(
            1 for r in hit if not _thumbnails_only(r.measures)),
        "orphan_files_current": sum(1 for r in agree if r.measures.get("orphaned_current", 0)),
        "orphan_body_classes": {key[len("orphan_body_class_"):]: _files_and_total(agree, key)
                                for key in body},
        "page_thumb_legacy_files": sum(1 for r in agree if r.measures.get("page_thumb_legacy")),
        "thumbnail_orphan_files_marked_today": sum(
            1 for r in agree if r.measures.get("orphan_body_class_thumbnail", 0) > 0
            and r.measures.get("page_thumb_legacy")),
        "xmp": {key: _files_and_total(agree, key)
                for key in ("xmp_bodies", "xmp_image_bodies", "xmp_image_current",
                            "xmp_binary_bodies", "xmp_binary_current", "xmp_undecoded_bodies")},
        "graph_work": _percentiles((w for w, _ in work), 0),
        "graph_sum": _percentiles((s for _, s in work), 0),
        "graph_sum_of_largest_work": work[-1][1] if work else 0,
        "graph_work_per_sum": _percentiles((w / s for w, s in work if s > 0), 1),
        "measure_errors": sum(1 for r in agree if r.measures.get("page_thumb_legacy_error")),
    }


# The 3a-7e-2 measures (scorecard.inventory.drawing_measures), each reported
# as files with any and the total count.
_DRAWING_KEYS: Final = (
    "streams_current", "drawable_current", "orphaned_current", "bodies_streams",
    "bodies_drawable", "bodies_structural", "bodies_drawable_structural", "bodies_orphaned",
    "orphaned_older_only", "orphaned_current_labelled_older", "mupdf_listed", "closure_gap")


def _drawing(agree: Sequence[FileResult]) -> dict[str, Any]:
    """3a-7e-2 (ADR 0011 item 5), over the unflagged files: streams by
    label (the current revision's, and by body across revisions),
    orphans by class, /Metadata orphaned on annotations and ICC profiles,
    and the closure gap against MuPDF's page listings -- files with any,
    and the total."""
    keys = [*_DRAWING_KEYS, *sorted({key for r in agree for key in r.measures
                                     if key.startswith(("orphan_class_", "orphan_metadata_",
                                                        "closure_gap_"))})]
    return {key: {"files": sum(1 for r in agree if r.measures.get(key, 0) > 0),
                  "total": sum(r.measures.get(key, 0) for r in agree)} for key in keys}


def aggregate(results: Sequence[FileResult], config: dict[str, Any] | None = None
              ) -> dict[str, Any]:
    """The run's aggregate: counts, rates and timings only (shareable)."""
    verdicts = Counter(r.verdict for r in results)
    n = len(results)
    disagree = [r for r in results if r.verdict == DISAGREE]
    by_check = Counter(r.agreement.check.value if r.agreement and r.agreement.check
                       else Check.TILING.value for r in disagree)
    agree = [r for r in results if r.verdict == AGREE]
    oracle_runs = [r.oracle for r in results if r.oracle is not None]
    built = [r for r in results if r.build.outcome == "ok"]
    work = [r.build.payload.get("work", 0) / r.size for r in built
            if r.build.payload and r.size > 0]
    region_totals: dict[str, dict[str, int]] = {}
    for kind in ("unindexed", "contested"):
        with_kind = [r for r in built if (r.build.payload or {}).get("regions", {}).get(kind)]
        region_totals[kind] = {
            "files": len(with_kind),
            "regions": sum(int((r.build.payload or {})["regions"][kind]) for r in with_kind),
            "bytes": sum(r.regions.get(kind, {}).get("bytes", 0) for r in with_kind),
        }
    groups: dict[str, Counter[str]] = {}
    for r in results:
        if r.job.group is not None:
            groups.setdefault(r.job.group, Counter())[r.verdict] += 1
    gate = {
        "passed": n > 0 and not any(verdicts[v] for v in GATING),
        "unflagged_disagree": verdicts[DISAGREE],
        "crashes": verdicts[CRASH],
        "timeouts": verdicts[TIMEOUT],
        "unverified": verdicts[UNVERIFIED],
        "inconsistent": verdicts[INCONSISTENT],
    }
    out: dict[str, Any] = {
        "files": n,
        "bytes": sum(r.size for r in results),
        "gate": gate,
        "unflagged_agree": len(agree),
        "unflagged_agree_encrypted": sum(1 for r in agree if r.agreement and r.agreement.encrypted),
        "unflagged_disagree_by_check": dict(sorted(by_check.items())),
        "flagged": verdicts[FLAGGED],
        "flag_rate": round(verdicts[FLAGGED] / n, 4) if n else 0.0,
        "flags": _flag_counts(results),
        "regions": region_totals,
        "encrypted_files": sum(1 for r in results if r.measures.get("encrypted")),
        "encrypt_entry_files": sum(1 for r in results if r.measures.get("encrypt_entry")),
        # qpdf --check ERROR lines (exit 2) on files that otherwise agree:
        # gated since 3a-6b, so only ERROR lines the allowlists cover
        # (page-tree semantics, 3a-7's).
        "qpdf_check_errors_on_agree": sum(1 for r in agree if r.agreement
                                          and r.agreement.qpdf_check_errors),
        "compared": {
            "streams": sum(r.agreement.streams_compared for r in agree if r.agreement),
            "object_stream_members": sum(r.agreement.members_compared
                                         for r in agree if r.agreement),
            "values": sum(r.agreement.values_compared for r in agree if r.agreement),
            "annotations": sum(r.agreement.annotations_compared for r in agree if r.agreement),
            "reachable_revisions": sum(r.agreement.reachable_compared
                                       for r in agree if r.agreement),
        },
        "oracle": {
            "crashes": sum(1 for o in oracle_runs if o.outcome == "crash"),
            "timeouts": sum(1 for o in oracle_runs if o.outcome == "timeout"),
            "failed_on_flagged": sum(1 for r in results if r.verdict == FLAGGED
                                     and r.oracle is not None and r.oracle.outcome != "ok"),
            "measure_errors": sum(1 for r in results if r.measures.get("measure_error")),
        },
        "timing": {
            "build_seconds": _percentiles(r.build.elapsed for r in built),
            "oracle_seconds": _percentiles(o.elapsed for o in oracle_runs if o.outcome == "ok"),
            # The REACHABLE check's share of the oracle's time (3a-7e-1).
            "reachable_seconds": _percentiles(r.agreement.reachable_ms / 1000
                                              for r in agree if r.agreement),
            # ... per file, and over the run (3a-7e-3's cost briefing).
            "reachable_share": _percentiles(
                (r.agreement.reachable_ms / 1000 / r.oracle.elapsed for r in agree
                 if r.agreement and r.oracle is not None and r.oracle.elapsed > 0), 3),
            "reachable_share_total": round(
                sum(r.agreement.reachable_ms / 1000 for r in agree if r.agreement)
                / max(sum(r.oracle.elapsed for r in agree if r.oracle is not None), 1e-9), 4),
            "work_per_byte": _percentiles(work, 2),
        },
        "pending_decisions": _pending(results),
    }
    if groups:
        out["by_group"] = {g: dict(sorted(c.items())) for g, c in sorted(groups.items())}
    if config:
        out["config"] = config
    return out


def provenance(started: datetime) -> dict[str, Any]:
    """Where a run's numbers come from: the commit (and whether the tree
    had local changes), the readers' and Python's versions, the platform
    and the start time. No path: `git rev-parse` gives a hash, `platform`
    an OS/architecture string."""
    import platform

    import pikepdf
    import pymupdf

    def git(*argv: str) -> str | None:
        try:
            done = subprocess.run(["git", *argv], cwd=REPO_ROOT, capture_output=True,
                                  text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout if done.returncode == 0 else None
    commit = (git("rev-parse", "HEAD") or "").strip()
    status = git("status", "--porcelain")
    return {
        "commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit) else "unknown",
        "dirty": None if status is None else bool(status.strip()),
        "qpdf": tool_version("qpdf"),
        "libqpdf": str(pikepdf.__libqpdf_version__),
        "pikepdf": str(pikepdf.__version__),
        "pymupdf": str(pymupdf.VersionBind),
        "mupdf": str(pymupdf.VersionFitz),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def tool_version(tool: str) -> str:
    from .differential import _tool_version
    return _tool_version(tool)


def render(agg: dict[str, Any]) -> str:
    """The aggregate as a short text summary (counts only)."""
    gate = agg["gate"]
    lines = [
        f"inventory agreement gate: {'PASS' if gate['passed'] else 'FAIL'}",
        f"  files {agg['files']}  bytes {agg['bytes']}",
        f"  unflagged agree {agg['unflagged_agree']} "
        f"(encrypted, object set only: {agg['unflagged_agree_encrypted']})",
        f"  unflagged DISAGREE {gate['unflagged_disagree']} "
        f"{agg['unflagged_disagree_by_check'] or ''}",
        f"  crashes {gate['crashes']}  timeouts {gate['timeouts']}  "
        f"unverified {gate['unverified']}  inconsistent {gate['inconsistent']}",
        f"  flagged {agg['flagged']} (rate {agg['flag_rate']:.2%})",
    ]
    lines.append(f"  encrypted {agg['encrypted_files']} (an /Encrypt entry: "
                 f"{agg['encrypt_entry_files']})  qpdf --check errors on agreeing files "
                 f"{agg['qpdf_check_errors_on_agree']} (allowlisted)")
    for reason, count in agg["flags"]["top_reasons"]:
        lines.append(f"    {reason:28} {count}")
    for kind, totals in agg["regions"].items():
        lines.append(f"  {kind}: {totals['files']} files, {totals['regions']} regions, "
                     f"{totals['bytes']} bytes (reported, not gated)")
    timing = agg["timing"]
    lines.append(f"  build seconds {timing['build_seconds']}")
    lines.append(f"  oracle seconds {timing['oracle_seconds']}")
    lines.append(f"  reachable check seconds {timing.get('reachable_seconds', {})}")
    lines.append(f"  work per byte {timing['work_per_byte']}")
    for name, values in agg["pending_decisions"].items():
        lines.append(f"  {name}: {values}")
    for group, counts in agg.get("by_group", {}).items():
        lines.append(f"  group {group}: {counts}")
    return "\n".join(lines)


def write_detail(results: Sequence[FileResult], path: Path) -> None:
    """Per-file detail, one JSON object per line, keyed by SHA-256: LOCAL
    ONLY (the caller checks *path* with `corpus.ensure_local_only`). No
    path and no document bytes; reader messages as scrubbed templates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as out:
        for r in results:
            a = r.agreement
            out.write(json.dumps({
                "sha256": r.job.key, "label": r.job.label, "size": r.size,
                "verdict": r.verdict, "reasons": list(r.reasons),
                "check": a.check.value if a and a.check else None,
                "revision": a.revision if a else None, "obj": a.obj if a else None,
                "templates": list(a.templates) if a else [],
                "build": {"outcome": r.build.outcome, "seconds": round(r.build.elapsed, 3),
                          "error": r.build.error,
                          "work": (r.build.payload or {}).get("work")},
                "oracle": None if r.oracle is None else {
                    "outcome": r.oracle.outcome, "seconds": round(r.oracle.elapsed, 3),
                    "error": r.oracle.error},
                "measures": r.measures,
            }, sort_keys=True) + "\n")
