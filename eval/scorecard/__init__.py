"""The scorecard: differential and metrics harness for the verifier.

docs/REDESIGN.md §5 ("Scorecard", "Where each gate runs") and Phase 0c
are the spec. In short:

- One runner (`scorecard.runner`) drives the verifier's CLI as a
  subprocess per file, with a timeout, judged on the process's exit
  code — the same shape pytest's `caselib.run` uses in-process.
- `scorecard.refs` runs that CLI from any git ref via a worktree, so a
  pinned reference (the `eval-ref-0` tag) and the working tree (the
  candidate) can be scanned identically.
- `scorecard.keys` reduces a `--json` report to the normalised
  comparison key defined in REDESIGN.md's Phase 0c row: exit,
  `error.code`, findings as (rule, tier, storage), review warnings as
  (rule, "review", storage, adjacency), other warnings as (code, layer,
  tool). Exact legacy warning codes/messages are deliberately not part
  of the key.
- `scorecard.differential` runs the case library through both the
  reference and the candidate and diffs the keys, checking every
  difference against `eval/accepted_diffs.yaml`.
- `scorecard.metrics` computes the metrics table (silent miss,
  downgrade, false hard, review rate, crash/timeout, runtime) against
  case labels.
- `scorecard.corpus` builds and runs a local-only real-world corpus
  manifest (never committed) for clean-side metrics.
- `scorecard.cli` is the `python -m scorecard` entry point tying it
  together; `scorecard.report` renders the scorecard table and JSON.

See eval/README.md for how to run it.
"""

from __future__ import annotations
