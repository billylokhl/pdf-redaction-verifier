"""``python -m gallery build --out DIR [--results FILE]`` — see eval/README.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from .build import build

DEFAULT_OUT = Path("eval/gallery/_build")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gallery", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build", help="generate the static HTML gallery")
    build_p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                         help=f"output directory (default: {DEFAULT_OUT})")
    build_p.add_argument("--results", type=Path, default=None,
                         help="a `scorecard diff --json` report; when given, cases it "
                              "covers show a measured verdict instead of their label")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        result = build(args.out, results_path=args.results)
        print(f"wrote {result.out}/index.html")
        print(f"  {result.leak_cases} leak cases, {result.clean_cases} clean cases")
        print(f"  {result.misses} known misses today")
        if result.skipped_perf:
            print(f"  skipped {result.skipped_perf} perf case(s)")
        if result.skipped_unavailable:
            print(f"  skipped {result.skipped_unavailable} case(s) needing an unavailable tool")
        return 0
    return 1
