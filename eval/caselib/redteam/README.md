# The blind red-team slot

Every other case in this library is written by someone who has read
`verify.py`: they know exactly what the tool checks, which is exactly
the bias the case library exists to correct for (docs/REDESIGN.md §5,
§9 "corpora unrepresentative"). This directory holds cases written the
other way round.

## The protocol

A red-team round is run by someone who:

1. **Has not read `verify.py`** (or any of the redesign's implementation
   modules). They may read `COVERAGE.md`, the threat model
   (`docs/REDESIGN.md`), `eval/README.md`'s description of the case
   schema, and `eval/caselib/model.py` / `cells.py` far enough to know
   the shape of a case — id, truth, cells, expected, story — the same
   way any external contributor would.
2. Is told which commit of `COVERAGE.md` they are working from.
3. Builds cases — real PDFs (or a builder script, `caselib.rawpdf`-style)
   — that plant a secret somewhere they believe the coverage table or
   threat model says the tool should (or should not) catch it, **without
   ever running `verify.py` against their own drafts to fish for a
   passing label.** They may sanity-check that their PDF actually
   contains the planted value (e.g. by opening it in a viewer), but the
   `expected` verdict they write down must be their own prediction from
   the coverage table and threat model — not something reverse-engineered
   from the tool's actual output. Recording *how* the drafts were built
   is enough for a reviewer to judge whether that line was crossed; there
   is no way to make it unenforceable.
4. Commits the round under `redteam/<round>/`: the case PDFs (or builder
   scripts), one `labels.json` (the frozen predictions), a `round.json`
   (metadata, including the frozen label-file hash), and a `README.md`
   naming the author, the date, the `COVERAGE.md` commit they were
   given, and the no-code-access statement above in their own words.

The round is then run like any other part of the suite. Where the tool's
actual verdict disagrees with the round's frozen prediction, that is
either a real finding (docs/REDESIGN.md's known-gap mechanism: the case
gets a `known_gap` pinning today's wrong answer, same as any other
library case) or a mistaken prediction — either way it goes through
review, never a silent edit.

## Why labels are frozen

A maintainer *does* know the code. If they could freely edit a red-team
round's labels after the fact, the round would stop being independent
evidence — a label could quietly be nudged to match whatever the tool
already does. So:

- `round.json` records `initial_labels_sha256` (set once, at the round's
  first commit, and never touched again) and `labels_sha256` (the hash
  `labels.json` must currently match — checked by
  `tests/test_case_library.py`).
- Changing a label means editing `labels.json`, recomputing its sha256,
  writing the new value into `labels_sha256`, **and** appending an entry
  to `adjudications.log` (one JSON object per line: `round`, `case_id`,
  `date`, `adjudicator`, `reason`, `old_sha256`, `new_sha256`). A test
  requires that whenever `labels_sha256 != initial_labels_sha256`, the
  log holds an entry for that round whose `new_sha256` matches — i.e. the
  live hash can only differ from the frozen one through a recorded,
  attributed change.

This does not require inspecting git history (fragile across shallow
clones, squashes and rebases) — it only requires that a silent edit to
`labels.json` shows up as a **mismatch** between the file and its
recorded hash, which is what the test actually catches, and that a
*deliberate* edit leaves a paper trail. `initial_labels_sha256` itself
is trusted at review time, the same way any other frozen constant in
this repository is: reviewers read the diff.

## Cells COVERAGE.md doesn't have yet

A round may find a real storage place the coverage table has no row for.
Rather than rejecting the case outright, it may use the placeholder
namespace **`new.<slug>`** as its cell id — but only for a case whose
`origin` is `"redteam"`, and only once the id is listed in
`cells.NEW_CELL_ALLOWLIST` (`eval/caselib/cells.py`) mapped to the GitHub
issue tracking a real `COVERAGE.md` row for it. `model.Case` rejects a
`new.*` id anywhere else — a non-red-team case, or an unlisted one —
at construction time, so the placeholder can never quietly become a
permanent way to skip the coverage table. A test
(`tests/test_case_library.py`) enforces that every allowlist entry maps
to a real issue number and that no id lingers there once it has a real
`COVERAGE.md` row (i.e. once it's in `cells.CELLS`, it must be removed
from the allowlist).

This is deliberately the simplest sound design available: it needs no
extra state beyond a dict literal, the failure mode (an id stuck in
`new.*` forever) is caught by the "remove once real" test, and it costs
a red-teamer nothing extra — they use the placeholder, open an issue,
and a maintainer adds the one allowlist line in review.

## Loading

`eval/caselib/families/redteam.py` is an ordinary family module (auto
loaded by `caselib.load()`, like every other file under `families/`): it
walks `redteam/*/`, verifies each round's label hash, and registers each
case into the same `REGISTRY` via `model.case()`, with `origin="redteam"`
(`model.ORIGINS`). From there a red-team case is judged exactly like any
other — `tests/test_case_library.py`'s `test_case` builds it, scans it,
and checks the verdict against `expected` (or `known_gap.today`).

## `round-0-example`

Ships with this machinery so the loader, the hash lock and the
adjudication path are all exercised in CI without waiting for a real
round. See its own README — it is explicitly not blind.
