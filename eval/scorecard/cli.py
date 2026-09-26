"""`python -m scorecard` — the scorecard's command-line entry point.

    PYTHONPATH=eval:. python -m scorecard diff [CASE_ID ...]
    PYTHONPATH=eval:. python -m scorecard metrics [CASE_ID ...]
    PYTHONPATH=eval:. python -m scorecard corpus build --root DIR
    PYTHONPATH=eval:. python -m scorecard corpus check --root DIR
    PYTHONPATH=eval:. python -m scorecard corpus run --root DIR --secrets FILE

See eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import corpus as corpus_mod
from . import report
from .accepted import DEFAULT_PATH as DEFAULT_ACCEPTED_DIFFS_PATH
from .differential import DEFAULT_TIMEOUT, run_candidate, run_differential
from .metrics import compute_metrics
from .refs import REFERENCE_REF


def _cmd_diff(args: argparse.Namespace) -> int:
    diffs = run_differential(
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
    print(report.render_differential_summary(diffs))
    if args.json:
        args.json.write_text(report.dump_json(report.differential_to_jsonable(diffs)) + "\n")
    return 0 if all(d.ok for d in diffs) else 1


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
    entries = corpus_mod.build_manifest(args.root, manifest_path=args.manifest)
    print(f"wrote {len(entries)} entries to {args.manifest}")
    return 0


def _cmd_corpus_check(args: argparse.Namespace) -> int:
    entries = corpus_mod.load_manifest(args.manifest)
    check = corpus_mod.check_manifest(entries, args.root)
    for path in check.missing:
        print(f"MISSING  {path}")
    for path in check.changed:
        print(f"CHANGED  {path}")
    if check.ok:
        print(f"ok: {len(entries)} files present and unchanged")
    return 0 if check.ok else 1


def _cmd_corpus_run(args: argparse.Namespace) -> int:
    from .refs import candidate_verify_path, repo_root

    entries = corpus_mod.load_manifest(args.manifest)
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
        workdir=args.out,
        timeout=args.timeout,
    )
    strata = corpus_mod.stratify(runs)
    print(report.render_stratified_table(strata))
    if args.json:
        payload = {name: sc.to_jsonable() for name, sc in strata.items()}
        args.json.write_text(report.dump_json(payload) + "\n")
    return 0


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

    build_p = corpus_sub.add_parser("build", help="scan a directory into the manifest")
    build_p.add_argument("--root", type=Path, required=True, help="directory of real PDFs")
    build_p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    build_p.set_defaults(func=_cmd_corpus_build)

    check_p = corpus_sub.add_parser("check", help="report missing/changed manifest files")
    check_p.add_argument("--root", type=Path, required=True)
    check_p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    check_p.set_defaults(func=_cmd_corpus_check)

    run_p = corpus_sub.add_parser("run", help="scan the manifest, report clean-side metrics")
    run_p.add_argument("--root", type=Path, required=True)
    run_p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    run_p.add_argument("--secrets", type=Path, required=True, help="rules file (see README)")
    run_p.add_argument("--out", type=Path, default=Path("/tmp/scorecard-corpus"))
    run_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    run_p.add_argument("--json", type=Path, default=None)
    run_p.set_defaults(func=_cmd_corpus_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
