"""python -m redactor_set (with PYTHONPATH=eval:.)

  generate [--root R] [--only D01,D02] [--seed N]   the 20 templates -> R/inputs
  probes   [--root R] [--only T1,F1]                 one-location probes -> R/probes
  config   [--root R]                                write the fabricated config
  run      [--root R] --set inputs|probes            methods 1 and 2 (needs
                                                     REDACTOR_CMD, REDACTOR_PYTHON)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import generate as gen
from .probes import PROBES
from .run import run_set
from .templates import TEMPLATES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redactor_set")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("generate", "probes", "config", "run"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, default=gen.LOCAL_ROOT)
        p.add_argument("--allow-outside", action="store_true")
        if name in ("generate", "probes"):
            p.add_argument("--only", default="")
            p.add_argument("--seed", type=int, default=0)
        if name == "run":
            p.add_argument("--set", choices=("inputs", "probes"), required=True)
    args = parser.parse_args(argv)
    root = gen.local_root(args.root, allow_outside=args.allow_outside)
    config = gen.write_config(root)
    if args.cmd in ("generate", "probes"):
        builders = TEMPLATES if args.cmd == "generate" else PROBES
        out_dir = root / ("inputs" if args.cmd == "generate" else "probes")
        only = [s for s in args.only.split(",") if s] or None
        unknown = set(only or ()) - set(builders)
        if unknown:
            parser.error(f"unknown ids: {', '.join(sorted(unknown))}")
        rows = gen.generate(builders, out_dir, seed=args.seed, only=only)  # type: ignore[arg-type]
        gen.write_manifest(out_dir, rows, args.cmd)
    elif args.cmd == "run":
        run_set(root, args.set, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
