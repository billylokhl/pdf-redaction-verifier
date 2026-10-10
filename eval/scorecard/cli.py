"""`python -m scorecard` — the scorecard's command-line entry point.

    PYTHONPATH=eval:. python -m scorecard diff [CASE_ID ...]
    PYTHONPATH=eval:. python -m scorecard metrics [CASE_ID ...]
    PYTHONPATH=eval:. python -m scorecard corpus build --root DIR
    PYTHONPATH=eval:. python -m scorecard corpus check --root DIR
    PYTHONPATH=eval:. python -m scorecard corpus run --root DIR --secrets FILE
    PYTHONPATH=eval:. python -m scorecard inventory run --root DIR [--json OUT]
    PYTHONPATH=eval:. python -m scorecard inventory caselib [--json OUT]
    PYTHONPATH=eval:. python -m scorecard inventory fuzz [--count N] [--seed S] [--json OUT]

See eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import corpus as corpus_mod
from . import report
from .accepted import DEFAULT_PATH as DEFAULT_ACCEPTED_DIFFS_PATH
from .differential import DEFAULT_TIMEOUT, run_candidate, run_differential
from .metrics import compute_metrics
from .refs import REFERENCE_REF


def _cmd_diff(args: argparse.Namespace) -> int:
    run = run_differential(
        args.case_id or None,
        out_dir=args.out,
        reference_ref=args.reference,
        accepted_path=args.accepted_diffs,
        timeout=args.timeout,
        max_workers=args.workers,
        progress=(lambda d: print(f"{'FAIL' if not d.ok else 'ok':5} {d.case_id}"))
        if args.verbose
        else None,
        cache_dir=args.cache_dir,
    )
    if not run.diffs:
        print(
            "ERROR: zero cases compared — every requested case id was unknown, or none of "
            "the case library's cases matched this environment. A silent 0/0 would look "
            "identical to a clean pass; refusing to report it as one.",
            file=sys.stderr,
        )
        return 1
    print(report.render_differential_summary(run.diffs, run.stale))
    if args.json:
        args.json.write_text(
            report.dump_json(report.differential_to_jsonable(run.diffs, run.stale)) + "\n"
        )
    return 0 if all(d.ok for d in run.diffs) and not run.stale else 1


def _cmd_metrics(args: argparse.Namespace) -> int:
    rows = run_candidate(
        args.case_id or None, out_dir=args.out, timeout=args.timeout, max_workers=args.workers
    )
    scorecard = compute_metrics(rows)
    print(report.render_metrics_table(scorecard))
    if args.json:
        args.json.write_text(report.dump_json(scorecard.to_jsonable()) + "\n")
    return 0


def _cmd_corpus_build(args: argparse.Namespace) -> int:
    manifest = corpus_mod.ensure_local_only(args.manifest, allow_outside=args.allow_outside)
    entries = corpus_mod.build_manifest(args.root, manifest_path=manifest)
    print(f"wrote {len(entries)} entries to {manifest}")
    return 0


def _cmd_corpus_check(args: argparse.Namespace) -> int:
    manifest = corpus_mod.ensure_local_only(args.manifest, allow_outside=args.allow_outside)
    entries = corpus_mod.load_manifest(manifest)
    check = corpus_mod.check_manifest(entries, args.root)
    for path in check.missing:
        print(f"MISSING  {path}")
    for path in check.changed:
        print(f"CHANGED  {path}")
    if check.ok:
        print(f"ok: {len(entries)} files present and unchanged")
    return 0 if check.ok else 1


def _cmd_corpus_run(args: argparse.Namespace) -> int:
    from caselib.run import available

    from .differential import normalization_have
    from .refs import candidate_verify_path, repo_root

    manifest = corpus_mod.ensure_local_only(args.manifest, allow_outside=args.allow_outside)
    out = corpus_mod.ensure_local_only(args.out, allow_outside=args.allow_outside)
    entries = corpus_mod.load_manifest(manifest)
    check = corpus_mod.check_manifest(entries, args.root)
    if not check.ok:
        for path in check.missing:
            print(f"MISSING  {path}", file=sys.stderr)
        for path in check.changed:
            print(f"CHANGED  {path}", file=sys.stderr)
    present = {e.path for e in entries} - set(check.missing) - set(check.changed)
    usable = [e for e in entries if e.path in present]
    rules = json.loads(args.secrets.read_text())
    runs = corpus_mod.scan_corpus(
        usable,
        args.root,
        rules,
        verify_path=candidate_verify_path(repo_root()),
        workdir=out,
        timeout=args.timeout,
    )
    strata = corpus_mod.stratify(runs, have=normalization_have(available()))
    print(report.render_stratified_table(strata))
    if args.json:
        payload = {name: sc.to_jsonable() for name, sc in strata.items()}
        args.json.write_text(report.dump_json(payload) + "\n")
    return 0


def _inventory_gate(args: argparse.Namespace, jobs: list[Any], workdir: Path,
                    config: dict[str, object]) -> int:
    """Run the 3a-6 gate over *jobs*: the aggregate to stdout (and --json),
    per-file detail only to the local-only --detail file."""
    from . import inventory_gate as gate

    source = str(config["source"])
    detail = corpus_mod.ensure_local_only(
        args.detail or corpus_mod.REAL_CORPUS_DIR / f"inventory-gate-{source}.jsonl",
        allow_outside=args.allow_outside)

    def progress(done: int, total: int) -> None:
        if done % 50 == 0 or done == total:
            print(f"inventory gate: {done}/{total}", file=sys.stderr, flush=True)
    results = gate.run_gate(
        jobs, workdir=workdir, workers=args.workers, progress=progress,
        build_timeout=args.timeout, oracle_timeout=args.oracle_timeout)
    config = config | gate.provenance(args.started) | {"build_timeout": args.timeout, "oracle_timeout": args.oracle_timeout,
                       "workers": args.workers}
    agg = gate.aggregate(results, config)
    gate.write_detail(results, detail)
    print(gate.render(agg))
    if args.json:
        args.json.write_text(report.dump_json(agg) + "\n")
    return 0 if agg["gate"]["passed"] else 1


def _inventory_preflight(args: argparse.Namespace) -> int | None:
    """Before any gate run: note the start, remove a stale --json (a failed
    run must never leave an old aggregate that passes for a fresh one),
    and refuse without qpdf, or with a qpdf CLI whose major.minor version is
    not pikepdf's libqpdf's. Returns an exit status to stop with, or None."""
    import shutil
    from datetime import datetime, timezone

    args.started = datetime.now(timezone.utc)
    if args.json is not None:
        args.json.unlink(missing_ok=True)
    if shutil.which("qpdf") is None:
        print("ERROR: qpdf is not on PATH -- the gate compares against qpdf and MuPDF. "
              "Install qpdf and run again.", file=sys.stderr)
        return 2
    from .inventory import qpdf_versions, qpdf_versions_match
    if not qpdf_versions_match():
        cli_version, lib_version = qpdf_versions()
        print(f"ERROR: the qpdf CLI is {cli_version} but pikepdf's libqpdf is {lib_version}: "
              "the gate needs one qpdf major.minor version (their messages and limits "
              "differ across releases). Install a matching qpdf.", file=sys.stderr)
        return 2
    return None


def _cmd_inventory_run(args: argparse.Namespace) -> int:
    import tempfile

    from .inventory_gate import Job

    if (stop := _inventory_preflight(args)) is not None:
        return stop
    args.root = args.root.resolve()  # the children run from the repository root
    manifest = corpus_mod.ensure_local_only(args.manifest, allow_outside=args.allow_outside)
    entries = corpus_mod.load_manifest(manifest)
    if not entries:
        print("ERROR: the manifest is empty or missing -- build it with `corpus build`. "
              "Refusing to report 0 files as a pass.", file=sys.stderr)
        return 2
    check = corpus_mod.check_manifest(entries, args.root)
    if not check.ok:
        # Counts only: `corpus check` lists the files, on this machine.
        print(f"ERROR: the corpus drifted from its SHA-pinned manifest: {len(check.missing)} "
              f"missing, {len(check.changed)} changed. Run `corpus check` to list them, then "
              "restore the files or rebuild the manifest. Refusing to run.", file=sys.stderr)
        return 2
    jobs = [Job(e.sha256, args.root / e.path) for e in entries]
    with tempfile.TemporaryDirectory(prefix="inventory-gate-") as work:
        return _inventory_gate(args, jobs, Path(work), {"source": "corpus"})


def _cmd_inventory_caselib(args: argparse.Namespace) -> int:
    import hashlib
    import tempfile

    from caselib import load
    from caselib.run import available, build

    from .inventory_gate import Job

    if (stop := _inventory_preflight(args)) is not None:
        return stop
    have = available()
    cases = sorted(load().items())
    skipped = sum(1 for _, case in cases if case.requires - have)
    with tempfile.TemporaryDirectory(prefix="inventory-gate-") as work:
        root = Path(work) / "files"
        root.mkdir()
        jobs: list[Job] = []
        for case_id, case in cases:
            if case.requires - have:
                continue
            path = build(case, root / f"{len(jobs):04d}.pdf")
            key = hashlib.sha256(path.read_bytes()).hexdigest()
            jobs.append(Job(key, path, case_id, case_id.split(".", 1)[0]))
        return _inventory_gate(args, jobs, Path(work),
                               {"source": "caselib", "skipped_needing_tools": skipped})


def _cmd_inventory_fuzz(args: argparse.Namespace) -> int:
    import hashlib
    import random
    import tempfile

    from .inventory_gate import Job
    from .pdfgen import random_case

    if (stop := _inventory_preflight(args)) is not None:
        return stop
    rng = random.Random(args.seed)
    with tempfile.TemporaryDirectory(prefix="inventory-gate-") as work:
        root = Path(work) / "files"
        root.mkdir()
        jobs: list[Job] = []
        for i in range(args.count):
            kind, data = random_case(rng)
            path = root / f"{i:05d}.pdf"
            path.write_bytes(data)
            jobs.append(Job(hashlib.sha256(data).hexdigest(), path, f"fuzz-{args.seed}-{i}", kind))
        return _inventory_gate(args, jobs, Path(work),
                               {"source": "fuzz", "seed": args.seed, "count": args.count})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scorecard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("case_id", nargs="*", help="restrict to these case ids (default: all)")
        p.add_argument("--out", type=Path, default=Path("/tmp/scorecard"), help="scratch directory")
        p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-file timeout (s)")
        p.add_argument("--workers", type=int, default=None, help="parallel subprocesses")
        p.add_argument("--json", type=Path, default=None, help="also write JSON to this path")

    diff_p = sub.add_parser("diff", help="differential vs the pinned reference")
    add_common(diff_p)
    diff_p.add_argument("--reference", default=REFERENCE_REF, help="git ref to compare against")
    diff_p.add_argument(
        "--accepted-diffs", type=Path, default=DEFAULT_ACCEPTED_DIFFS_PATH,
        help="eval/accepted_diffs.yaml",
    )
    diff_p.add_argument("--verbose", action="store_true", help="print per-case progress")
    diff_p.add_argument(
        "--cache-dir", type=Path, default=None,
        help="cache reference results here, keyed by ref commit + case lock hash",
    )
    diff_p.set_defaults(func=_cmd_diff)

    metrics_p = sub.add_parser("metrics", help="label-based metrics for the candidate alone")
    add_common(metrics_p)
    metrics_p.set_defaults(func=_cmd_metrics)

    corpus_p = sub.add_parser("corpus", help="the local-only real-world corpus")
    corpus_sub = corpus_p.add_subparsers(dest="corpus_command", required=True)

    def add_allow_outside(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--allow-outside", action="store_true",
            help="allow --manifest/--out outside the gitignored eval/scorecard/real_corpus/ "
            "(you are then responsible for never committing or sharing that path)",
        )

    build_p = corpus_sub.add_parser("build", help="scan a directory into the manifest")
    build_p.add_argument("--root", type=Path, required=True, help="directory of real PDFs")
    build_p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    add_allow_outside(build_p)
    build_p.set_defaults(func=_cmd_corpus_build)

    check_p = corpus_sub.add_parser("check", help="report missing/changed manifest files")
    check_p.add_argument("--root", type=Path, required=True)
    check_p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    add_allow_outside(check_p)
    check_p.set_defaults(func=_cmd_corpus_check)

    run_p = corpus_sub.add_parser("run", help="scan the manifest, report clean-side metrics")
    run_p.add_argument("--root", type=Path, required=True)
    run_p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    run_p.add_argument("--secrets", type=Path, required=True, help="rules file (see README)")
    run_p.add_argument("--out", type=Path, default=corpus_mod.REAL_CORPUS_DIR / "scan-workdir")
    run_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    run_p.add_argument("--json", type=Path, default=None)
    add_allow_outside(run_p)
    run_p.set_defaults(func=_cmd_corpus_run)

    inv_p = sub.add_parser("inventory", help="the 3a-6 inventory agreement gate")
    inv_sub = inv_p.add_subparsers(dest="inventory_command", required=True)

    def add_gate(p: argparse.ArgumentParser) -> None:
        from .inventory_gate import BUILD_TIMEOUT, ORACLE_TIMEOUT
        p.add_argument("--json", type=Path, default=None,
                       help="also write the aggregate (counts only: shareable) as JSON here")
        p.add_argument("--detail", type=Path, default=None,
                       help="per-file detail keyed by SHA-256 (local only, never share); "
                       "default eval/scorecard/real_corpus/inventory-gate-SOURCE.jsonl, "
                       "SOURCE being corpus, caselib or fuzz")
        p.add_argument("--timeout", type=float, default=BUILD_TIMEOUT,
                       help="build_inventory's per-file timeout (s); past it is a failure")
        p.add_argument("--oracle-timeout", type=float, default=ORACLE_TIMEOUT,
                       help="the reader differential's per-file timeout (s)")
        p.add_argument("--workers", type=int, default=1, help="files in parallel")
        add_allow_outside(p)

    inv_run = inv_sub.add_parser("run", help="the gate over the SHA-pinned local corpus")
    inv_run.add_argument("--root", type=Path, required=True)
    inv_run.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    add_gate(inv_run)
    inv_run.set_defaults(func=_cmd_inventory_run)

    inv_cases = inv_sub.add_parser("caselib", help="the gate over the case library")
    add_gate(inv_cases)
    inv_cases.set_defaults(func=_cmd_inventory_caselib)

    inv_fuzz = inv_sub.add_parser("fuzz", help="the gate over generated and mutated files")
    inv_fuzz.add_argument("--count", type=int, default=1000)
    inv_fuzz.add_argument("--seed", type=int, default=0)
    add_gate(inv_fuzz)
    inv_fuzz.set_defaults(func=_cmd_inventory_fuzz)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
