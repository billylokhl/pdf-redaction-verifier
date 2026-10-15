"""Phase 5's false-hard baseline: the full CLI with only the built-in
pattern classes as rules, over the local real corpus, counted per class,
per layer and per match shape (docs/REDESIGN.md §1 and §7's Phase 5 row,
ADR 0005; eval/README.md, "Pattern-class false-hard baseline").

    PYTHONPATH=eval:. python -m scorecard.pattern_baseline \\
        --root eval/scorecard/real_corpus/links [--workers 4] [--json OUT]

Measurement only: it changes no verdict. Each file is scanned by
`scorecard.pattern_recorder` -- the CLI unchanged, plus a record of each
pattern match's shape -- with `builtin_classes.rules.json` (the four
classes, nothing else), so every hard finding on this corpus, which holds
no planted secret, is a false hard. ADR 0005's 23.1% scanned page text
only; this covers every layer the CLI runs a class over.

Output is counts only: no file name, path, hash or matched text is
printed or written to --json. The per-file scratch reports stay in the
gitignored real_corpus/ (`corpus.ensure_local_only`).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import corpus as corpus_mod
from .match_shapes import OUT_ENV, SALT_ENV, record_key
from .runner import DEFAULT_TIMEOUT, CliResult, current_python, run_cli

HERE = Path(__file__).resolve().parent
RECORDER = HERE / "pattern_recorder.py"
BUILTIN_RULES = HERE / "builtin_classes.rules.json"
CLASSES: tuple[str, ...] = ("ssn", "credit-card", "email", "us-phone")
# ADR 0005: a validated email match is a true positive for "an address is
# here", and credit-card measured 0%; the union a Phase 5 fix must bring
# under 1% is the digit classes that miscolour unrelated numbers.
UNIONS: dict[str, tuple[str, ...]] = {
    "ssn|us-phone": ("ssn", "us-phone"),
    "ssn|us-phone|credit-card": ("ssn", "us-phone", "credit-card"),
    "any built-in": CLASSES,
}
# What a context rule might leave hard: the files that would still have a
# hard finding if every match of a shape outside the kept set were
# demoted to review (ADR 0012's options, estimated; nothing is demoted).
# See `_residuals` for how, and which way the estimate errs.
RESIDUALS: dict[str, frozenset[str]] = {
    "demote-embedded-and-dates": frozenset({"formatted", "number-list", "bare-digits"}),
    "also-number-lists": frozenset({"formatted", "bare-digits"}),
    "also-bare-digits": frozenset({"formatted"}),
}
UNJOINED = "unjoined"  # a hard finding with no recorder record: must stay 0
UNKNOWN = frozenset({UNJOINED, "classifier-error"})  # never counted as demoted
# When one key (rule, masked sample) carries several shapes -- the same
# digits read on two layers, say -- a finding takes the shape kept longest
# by RESIDUALS, so an ambiguity never counts a finding as demoted.
KEEP_ORDER: tuple[str, ...] = (
    "classifier-error", "email-address", "formatted", "number-list", "bare-digits",
    "alnum-id", "date-or-time", "hex-id", "uuid",
)


@dataclass(frozen=True)
class HardFinding:
    rule: str
    layer: str
    storage: str | None
    shape: str


@dataclass(frozen=True)
class FileResult:
    """One file's outcome, reduced to what the counts need."""

    stratum: str  # "text-bearing", "text-free" or "text-unknown"
    measured: bool  # False: see unmeasured_reason
    exit_code: int | None
    hard: tuple[HardFinding, ...] = ()
    review_rules: frozenset[str] = field(default_factory=frozenset)
    # Per class, the shape of every validated match in a call that could
    # yield a hard finding (pattern_recorder's `shapes`).
    match_shapes: dict[str, frozenset[str]] = field(default_factory=dict)
    unmeasured_reason: str | None = None  # "crash" (incl. timeout) or the CLI error code


def _stratum(entry: corpus_mod.CorpusEntry) -> str:
    if entry.probe != "ok":
        return "text-unknown"
    return "text-bearing" if entry.text_bearing else "text-free"


def reduce_result(stratum: str, result: CliResult, records: dict[str, Any],
                  salt: str = "") -> FileResult:
    """Join each hard finding in the CLI's --json report to the recorder's
    shape for the same `record_key(salt, rule, masked sample)`. A finding
    with no record is counted as UNJOINED, never dropped. A crash, timeout
    or CLI error with no finding (an unreadable or encrypted file) is
    unmeasured: it has nothing to count, so it never pads a rate's
    denominator."""
    report = result.report
    if result.crashed or report is None:
        return FileResult(stratum, False, result.returncode, unmeasured_reason="crash")
    if report.get("error") and not report.get("findings"):
        # An error with findings (INTERNAL_ERROR after a hard finding, exit
        # 1) is measured: its findings are real false hards.
        return FileResult(stratum, False, report.get("exit_code"),
                          unmeasured_reason=str(report["error"].get("code")))
    joins: dict[str, set[str]] = {}
    for rec in records.get("joins", []):
        joins.setdefault(rec["key"], set()).add(rec["shape"])
    hard = []
    for f in report.get("findings", []):
        if f.get("tier") != "hard" or f.get("rule") not in CLASSES:
            continue
        found = joins.get(record_key(salt, f["rule"], f.get("sample", "")))
        shape = (min(found, key=lambda s: KEEP_ORDER.index(s) if s in KEEP_ORDER else -1)
                 if found else UNJOINED)
        hard.append(HardFinding(f["rule"], f.get("layer") or "?", f.get("storage"), shape))
    review = frozenset(
        w["rule"] for w in report.get("warnings", []) if w.get("rule") in CLASSES
    )
    shapes = {rule: frozenset(v) for rule, v in records.get("shapes", {}).items()}
    return FileResult(stratum, True, report.get("exit_code"), tuple(hard), review, shapes)


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _residuals(measured: list[FileResult], members: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Per RESIDUALS option: files of *measured* that would still have a
    hard finding of a class in *members* if every match of a shape outside
    the option's kept set were demoted to review.

    A file counts if it has a hard finding of the class today and any
    validated match of that class the recorder saw -- every match, not
    only the first the tool reports, in every call that could yield a
    hard finding -- has a kept shape, or a finding of it has no known
    shape. An email address is always kept (no option demotes it, ADR
    0005). The estimate errs toward *keeping* a file hard: a kept-shape
    match seen only in a call whose result is a review warning (Metadata's
    raw fallback, say) still counts. Its one blind spot runs the other
    way: once every rule has hit, a layer's rolling scanner stops feeding
    text to the matcher, so a later match there is never seen."""
    n = len(measured)
    out = {}
    for option, kept in RESIDUALS.items():
        keep = kept | UNKNOWN | {"email-address"}

        def still_hard(r: FileResult, cls: str) -> bool:
            if not any(h.rule == cls for h in r.hard):
                return False
            if cls == "email" or any(h.rule == cls and h.shape in UNKNOWN for h in r.hard):
                return True
            return bool(r.match_shapes.get(cls, frozenset()) & keep)

        files = sum(1 for r in measured if any(still_hard(r, c) for c in members))
        out[option] = {"files": files, "pct": _pct(files, n)}
    return out


def aggregate_stratum(results: Iterable[FileResult]) -> dict[str, Any]:
    """Counts for one stratum. Rates are over measured files (a crash,
    timeout or CLI error is counted, never folded into a denominator)."""
    results = list(results)
    measured = [r for r in results if r.measured]
    n = len(measured)

    def files_where(pred) -> int:
        return sum(1 for r in measured if pred(r))

    per_class = {}
    for cls in CLASSES:
        hard_files = files_where(lambda r: any(h.rule == cls for h in r.hard))
        by_layer: Counter[str] = Counter()
        by_shape_files: Counter[str] = Counter()
        by_shape_findings: Counter[str] = Counter()
        by_layer_shape: dict[str, Counter[str]] = {}
        for r in measured:
            mine = [h for h in r.hard if h.rule == cls]
            for layer in {h.layer for h in mine}:
                by_layer[layer] += 1
            for shape in {h.shape for h in mine}:
                by_shape_files[shape] += 1
            for h in mine:
                by_shape_findings[h.shape] += 1
            for layer, shape in {(h.layer, h.shape) for h in mine}:
                by_layer_shape.setdefault(layer, Counter())[shape] += 1
        review_files = files_where(lambda r: cls in r.review_rules)
        residual = _residuals(measured, (cls,))
        per_class[cls] = {
            "hard_files": hard_files,
            "hard_pct": _pct(hard_files, n),
            "hard_files_by_layer": dict(sorted(by_layer.items())),
            "hard_pct_by_layer": {k: _pct(v, n) for k, v in sorted(by_layer.items())},
            "hard_files_by_shape": dict(sorted(by_shape_files.items())),
            "hard_findings_by_shape": dict(sorted(by_shape_findings.items())),
            "hard_files_by_layer_and_shape": {
                layer: dict(sorted(c.items())) for layer, c in sorted(by_layer_shape.items())},
            "review_files": review_files,
            "review_pct": _pct(review_files, n),
            "residual_hard_files": residual,
        }
    unions = {}
    for name, members in UNIONS.items():
        hard_files = files_where(lambda r: any(h.rule in members for h in r.hard))
        unions[name] = {
            "hard_files": hard_files,
            "hard_pct": _pct(hard_files, n),
            "residual_hard_files": _residuals(measured, members),
        }
    by_layer_all: Counter[str] = Counter()
    for r in measured:
        for layer in {h.layer for h in r.hard}:
            by_layer_all[layer] += 1
    return {
        "files": len(results),
        "measured": n,
        "unmeasured": len(results) - n,
        "unmeasured_by_reason": dict(sorted(Counter(
            r.unmeasured_reason or "?" for r in results if not r.measured).items())),
        "exit_codes": {str(k): v for k, v in sorted(
            Counter(r.exit_code for r in measured).items(), key=lambda kv: str(kv[0]))},
        "classes": per_class,
        "unions": unions,
        "hard_files_by_layer": dict(sorted(by_layer_all.items())),
        "unjoined_findings": sum(
            1 for r in measured for h in r.hard if h.shape == UNJOINED),
    }


def aggregate(results: list[FileResult]) -> dict[str, Any]:
    strata = {"all": results}
    for r in results:
        strata.setdefault(r.stratum, []).append(r)
    return {name: aggregate_stratum(group) for name, group in strata.items()}


def render(agg: dict[str, Any]) -> str:
    lines = []
    for name in ("all", "text-bearing", "text-free", "text-unknown"):
        s = agg.get(name)
        if s is None:
            continue
        lines.append(f"== {name}: {s['measured']} measured, {s['unmeasured']} unmeasured "
                     f"{s['unmeasured_by_reason']}; exit codes {s['exit_codes']}")
        for cls, c in s["classes"].items():
            lines.append(f"  {cls:12} hard {c['hard_files']:5} ({c['hard_pct']}%)  "
                         f"review {c['review_files']:5} ({c['review_pct']}%)")
            if c["hard_files_by_layer"]:
                lines.append("    by layer: " + ", ".join(
                    f"{layer} {count} ({c['hard_pct_by_layer'][layer]}%)"
                    for layer, count in c["hard_files_by_layer"].items()))
            if c["hard_files_by_shape"]:
                lines.append(f"    by shape (files): {c['hard_files_by_shape']}")
        for union, u in s["unions"].items():
            lines.append(f"  union {union:26} hard {u['hard_files']:5} ({u['hard_pct']}%); "
                         "if demoted, still hard: " + ", ".join(
                             f"{opt} {v['files']} ({v['pct']}%)"
                             for opt, v in u["residual_hard_files"].items()))
        lines.append(f"  hard files by layer (any class): {s['hard_files_by_layer']}")
        lines.append(f"  unjoined findings: {s['unjoined_findings']}")
    return "\n".join(lines)


def scan_one(entry: corpus_mod.CorpusEntry, root: Path, rules: list[dict[str, str]],
             workdir: Path, timeout: float, salt: str) -> FileResult:
    case_id = entry.sha256[:16]
    sidecar = workdir / f"{case_id}.patterns.json"
    sidecar.unlink(missing_ok=True)  # never join a stale record
    result = run_cli(current_python(), RECORDER, root / entry.path, rules, workdir, case_id,
                     timeout=timeout,
                     env={**os.environ, OUT_ENV: str(sidecar), SALT_ENV: salt})
    try:
        records = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        records = {}
    return reduce_result(_stratum(entry), result, records, salt)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scorecard.pattern_baseline",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--manifest", type=Path, default=corpus_mod.DEFAULT_MANIFEST)
    p.add_argument("--secrets", type=Path, default=BUILTIN_RULES,
                   help="rules file (default: the four built-in classes only)")
    p.add_argument("--out", type=Path,
                   default=corpus_mod.REAL_CORPUS_DIR / "pattern-baseline-workdir")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--json", type=Path, default=None,
                   help="also write the counts (shareable: no names) as JSON here")
    p.add_argument("--allow-outside", action="store_true")
    args = p.parse_args(argv)

    manifest = corpus_mod.ensure_local_only(args.manifest, allow_outside=args.allow_outside)
    out = corpus_mod.ensure_local_only(args.out, allow_outside=args.allow_outside)
    out.mkdir(parents=True, exist_ok=True)
    entries = corpus_mod.load_manifest(manifest)
    check = corpus_mod.check_manifest(entries, args.root)
    skipped = set(check.missing) | set(check.changed)
    usable = [e for e in entries if e.path not in skipped]
    if not usable:
        print("ERROR: no manifest file to scan.", file=sys.stderr)
        return 1
    rules = json.loads(args.secrets.read_text())

    salt = secrets.token_hex(16)  # this run's only copy: never written
    done = 0
    lock = threading.Lock()

    def job(entry: corpus_mod.CorpusEntry) -> FileResult:
        nonlocal done
        r = scan_one(entry, args.root, rules, out, args.timeout, salt)
        with lock:
            done += 1
            if done % 100 == 0 or done == len(usable):
                print(f"pattern baseline: {done}/{len(usable)}", file=sys.stderr, flush=True)
        return r

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(job, usable))
    agg = aggregate(results)
    agg["config"] = {"rules": [r.get("class", r.get("name")) for r in rules],
                     "timeout": args.timeout, "workers": args.workers,
                     "skipped_missing_or_changed": len(skipped)}
    print(render(agg))
    if args.json:
        args.json.write_text(json.dumps(agg, indent=2, sort_keys=True) + "\n")
    if skipped:
        print(f"ERROR: {len(check.missing)} missing and {len(check.changed)} changed manifest "
              "file(s) were not scanned (`scorecard corpus check` lists them).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
