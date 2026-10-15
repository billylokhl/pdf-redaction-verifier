"""The owner's policy 1 (ADR 0012) on the real corpus: each file's exit
recomputed from the per-file reports `scorecard.pattern_baseline` left in
its workdir, without the built-in classes' findings and review warnings.
Counts only; it reads the local reports and scans nothing.

    PYTHONPATH=eval:. python -m scorecard.pattern_policy1_corpus \\
        [--manifest M] [--workdir W] [--json OUT]

The baseline ran with only the four classes as rules, so under policy 1 a
file's exit is set by its coverage warnings alone. A warning is the
class's when its `rule` is a class rule (every warning pattern matching
raises carries one; `pattern_recall.unattributed_class_warnings` checks
that); `pattern_recall.policy1_exit` does the recomputation, and the exit
today is `keys.effective_exit` with the same environment filter.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import corpus as corpus_mod
from .pattern_baseline import CLASSES

STRATA = ("all", "text-bearing", "text-free", "text-unknown")


def policy1_counts(files: Iterable[tuple[str, Mapping[str, Any]]],
                   have: frozenset[str] | None) -> dict[str, Any]:
    """Per stratum: exit codes today and under policy 1, and the moves
    between them. *files* holds (stratum, report) for each measured file."""
    from .keys import effective_exit
    from .pattern_recall import policy1_exit, unattributed_class_warnings

    names = frozenset(CLASSES)
    before: dict[str, Counter[int]] = {s: Counter() for s in STRATA}
    after: dict[str, Counter[int]] = {s: Counter() for s in STRATA}
    moves: dict[str, Counter[str]] = {s: Counter() for s in STRATA}
    unattributed = 0
    for stratum, report in files:
        b, a = effective_exit(dict(report), have), policy1_exit(report, names, have)
        unattributed += unattributed_class_warnings(report, names)
        for s in ("all", stratum):
            before[s][b] += 1
            after[s][a] += 1
            if a != b:
                moves[s][f"{b}->{a}"] += 1
    return {
        s: {"files": sum(before[s].values()),
            "today": {str(k): v for k, v in sorted(before[s].items())},
            "policy1": {str(k): v for k, v in sorted(after[s].items())},
            "moves": dict(sorted(moves[s].items()))}
        for s in STRATA if before[s]
    } | {"unattributed_class_warnings": unattributed}


def main(argv: list[str] | None = None) -> int:
    from caselib.run import available

    from .differential import normalization_have

    p = argparse.ArgumentParser(prog="python -m scorecard.pattern_policy1_corpus",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    p.add_argument("--workdir", type=Path,
                   default=corpus_mod.REAL_CORPUS_DIR / "pattern-baseline-workdir")
    p.add_argument("--json", type=Path, default=None)
    p.add_argument("--allow-outside", action="store_true")
    args = p.parse_args(argv)
    manifest = corpus_mod.ensure_local_only(args.manifest, allow_outside=args.allow_outside)
    workdir = corpus_mod.ensure_local_only(args.workdir, allow_outside=args.allow_outside)

    files = []
    missing = 0
    for e in corpus_mod.load_manifest(manifest):
        path = workdir / f"{e.sha256[:16]}.report.json"
        if not path.exists():
            missing += 1  # unmeasured in the baseline run (a crash or timeout)
            continue
        stratum = ("text-unknown" if e.probe != "ok"
                   else "text-bearing" if e.text_bearing else "text-free")
        files.append((stratum, json.loads(path.read_text())))
    out = policy1_counts(files, normalization_have(available()))
    out["unmeasured"] = missing
    print(json.dumps(out, indent=2, sort_keys=True))
    if args.json:
        args.json.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 1 if out["unattributed_class_warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
