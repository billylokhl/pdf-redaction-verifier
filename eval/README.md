# Evaluation

The case library that measures the verifier — see
[docs/REDESIGN.md](../docs/REDESIGN.md) §5.

## The case library (`caselib/`)

Each case is a small script that writes one PDF with fabricated data,
plus the truth about it, what a correct verifier reports, and the story
of how the redaction failed:

```python
@case("leftover.redacted-no-gc", truth="leak", cells="orphaned.plain",
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      story="The SSN was redacted on the page, but the file was saved without "
            "garbage collection, so the original content stream is still inside.",
      mistake="Saving without garbage collection.",
      recovery="Decompress the file, find the leftover stream, decode its hex strings.")
def redacted_no_gc(path):
    ...
```

- **`id`** is `<family>.<slug>`, the family saying where the content is
  (`page`, `layout`, `document`, `attachment`, `leftover`, `revision`,
  `file`). Ids never change — baselines and gallery links key on them.
- **`truth`**: `leak` (a secret is in the file) or `clean`.
- **`cells`** (leaks only): where the planted secret is — ids from
  `caselib/cells.py`, which mirrors [COVERAGE.md](../COVERAGE.md) cell by
  cell. **`features`**: other cells the document exercises (all a clean
  case has).
- **`expected`**: the correct verdict — the exit code, hard findings as
  `(rule, storage)`, warnings as `(code, storage)`, and optionally
  `layers` as `(rule, layer)` when the cell is about one layer's reading.
  For a rule the case lists, where it is found is exact: the same rule
  found in a storage class not listed (a live object also called
  ORPHANED) fails the case. Any coverage warning ("could not read") not
  listed fails it too.
- **`known_gap`**: `KnownGap(cell, today=expect(...))` when today's tool
  gets it wrong. The case is judged against `today`, so a fix, a partial
  fix or a broken generator fails the test until the label is updated.
- **`writer`**: `fitz` (PyMuPDF — also what the verifier reads with),
  `raw` (hand-assembled bytes, `caselib/rawpdf.py`), `qpdf`, or `file`
  (committed real-tool output under `caselib/real/`, `origin="redactor"`).
- **`rules`** default to `SSN` = 123-45-6789 and `Code` = BLUEHERON.
  **`requires`** (`ocr`, `qpdf`, `exiftool`) skips a case where a tool it
  needs to be judged is missing — or, with `no-ocr`, where OCR is present
  (for a text-layer gap that OCR happens to cover); other cases are judged with only the
  missing tools' "not available" warnings left out (none on a full
  environment, `REQUIRE_FULL_ENV=1`).

Tests (`tests/test_case_library.py`) also require: every ✓ and ⚑ cell has
a leak case that is caught, reported in that cell's storage class; every
✗ cell has a pinned known-gap case unless listed in `UNDOCUMENTED_GAPS`
(which may only shrink); and `cells.py` matches COVERAGE.md glyph for
glyph.

**Grids** (`grid=`, `params=`) are parameterised families: `carriers`
(six carrier surfaces × four producer layouts) and `page-text` (27 page
layouts × three rule sets × with or without the SSN pattern rule, plus the
pattern rule alone; labels and known gaps in
`families/page_text_labels.py`). Their tests carry the `grid` marker;
CI runs them on Linux, where OCR is absent — their labels hold with and
without OCR.

### The raw writer (`caselib/rawpdf.py`)

Every case above can be written with PyMuPDF, but PyMuPDF is also the
library the verifier reads with — a corpus it alone wrote could not show
a place the two disagree (docs/REDESIGN.md §5's same-author-bias guard).
`rawpdf.py` assembles a PDF's bytes by hand instead, object by object:

- **`build`** — a classic file: objects in number order, one classic
  cross-reference table, a trailer.
- **`incremental_update(pdf, objects)`** — appends one more incremental
  save to an already-built file: the given objects, a classic
  cross-reference section listing exactly those numbers, and a trailer
  whose `/Prev` chains back to `pdf`'s own last `startxref` — the same
  chain the verifier's earlier-revision walk follows, so this is what
  makes a raw `superseded.*` case possible.
- **`build_objstm`** — a PDF 1.5 file instead: the non-stream objects
  packed into one `/ObjStm`, and the cross-reference itself written as an
  `/XRef` stream rather than a table.

`caselib.NONFITZ_PENDING` (`cells.py`) lists claimed (✓/⚑) cells that
still have no *caught leak case from a non-fitz writer* — a leak, no
`known_gap`, `writer != "fitz"` — tracking the same same-author-bias
guard case by case. It may only shrink; `tests/test_case_library.py`
checks both directions (nothing claimed and uncovered is missing from
it, and nothing in it is already covered).

## Running

```bash
pytest tests/test_case_library.py
```

Build and judge cases outside pytest, keeping the PDFs and reports:

```bash
PYTHONPATH=eval:. python -m caselib.run /tmp/cases              # all
PYTHONPATH=eval:. python -m caselib.run /tmp/cases page.visible  # some
```

## The build lock

`caselib/cases.lock.json` holds each case's PDF hash, so a change in what
a generator writes (an edit, a PyMuPDF upgrade) shows up in review. Builds
are reproducible: no new file IDs, UTC, date values pinned, and payloads
that must be compressed precomputed (compressor output varies between
zlib versions). After changing a case, regenerate it:

```bash
PYTHONPATH=eval:. python -m caselib.lock
```

## The scorecard (`scorecard/`)

The scorecard (docs/REDESIGN.md §5, Phase 0c) is the case library run a
different way: instead of judging each case against its label in-process
(`caselib.run`, above, used by `tests/test_case_library.py`), it runs the
verifier's real CLI as a subprocess per file, with a timeout — the same
runner for pytest's speed and the scorecard's realism, just pointed at
two different git refs.

**Differential**: compare the working tree (the candidate) against the
pinned `eval-ref-0` tag (the reference, checked out into a disposable
worktree — never the live code) on a normalised key: exit, `error.code`,
findings as (rule, tier, storage), review warnings as (rule, "review",
storage, adjacency), other warnings as (code, layer, tool) — each as a
*multiset*, so losing one of two otherwise-identical findings (the same
rule in two separate orphaned streams) is a real, visible difference.
Exact legacy warning codes and messages are deliberately not part of the
key. `OCR_UNAVAILABLE`/`TOOL_MISSING` warnings are dropped and the exit
recomputed without them first (mirroring `caselib.run.judge`), so the key
is comparable across environments — a case that goes from a clean exit
`0` to a flagged `2` compares the same on a full-environment run and on
Linux, where an unrelated `OCR_UNAVAILABLE` would otherwise already sit
on both sides. `REQUIRE_FULL_ENV=1` disables this at the orchestration
layer (`scorecard.differential.normalization_have`) — a missing tool
there is a bug, not noise to drop; `keys.is_environmental` itself never
reads that variable, so it stays a pure function of its arguments.

Two guards keep this recomputation from ever manufacturing a verdict
that hides a real bug: an **error** report (`report["error"]` set, e.g.
`PDF_PASSWORD`) is never touched, since the crash escape hatch doesn't
follow the findings/warnings formula at all; and the *unfiltered* report
must already be **self-consistent** with its own findings/warnings
(`1` if findings, else `2` if any warning, else `0`) before anything is
recomputed — a fail-open bug reporting `0` despite a real warning stays
`0`, a real, visible difference, rather than being "fixed" by
recomputing from the same warnings the report itself failed to act on.
The process's own exit code is checked against the report's claimed
`exit_code` too (`CliResult.crashed`, mirroring `judge`'s "report says
exit X, process Y"); a mismatch is a crash, not a report to normalise.

```bash
PYTHONPATH=eval:. python -m scorecard diff                      # every case
PYTHONPATH=eval:. python -m scorecard diff page.visible          # some
PYTHONPATH=eval:. python -m scorecard diff --json /tmp/diff.json --verbose
```

Exits non-zero if any case crashed or timed out, changed in a way not
listed in `eval/accepted_diffs.yaml`, an accepted entry is now stale (see
below), or zero cases were compared at all (every id given was unknown,
or nothing in the library matches this environment — a silent 0/0 would
otherwise look identical to a clean pass). The default "every case"
selection also skips any case marked `perf` (a large, multi-minute
generator such as a 300-page scan) unless you name it explicitly — it
would very likely time out at `--timeout` (default 60s) otherwise, which
reads as a crash rather than a deliberate skip.

**`eval/accepted_diffs.yaml`**: every intended per-case change between
the reference and the candidate — case id, the reference's normalised
key ("old"), the candidate's ("new"), a reason, the COVERAGE.md cell, and
the PR. A difference the file does not list exactly (both sides) fails
the differential. When a fix or a regression changes a case's key,
`scorecard diff --json` prints both keys for the case; copy them into a
new entry (or update the existing one, if this is a further change to a
case already listed) with a reason for the change, and review it in the
PR like any other test change.

Two rules keep the file honest:

- **Never less strict, silently.** This project's severity order is
  `0 < 2 < 1` (clean, then cannot-certify, then confirmed leak). An
  entry that moves the *other* way — `1`→`2`, `1`→`0`, or `2`→`0` — must
  say so with `weaker: true`; loading the file without that flag on such
  an entry is an error. `scorecard diff` prints every matched weaker
  entry in its own **WEAKER** section so it cannot be missed in review.
- **No stale entries.** An entry that no case in a full run actually
  produced is itself a gate failure — either the case id no longer
  exists in the case library, or it does exist, ran this time, and still
  didn't produce that change. An entry for a case that exists but was
  skipped *this run* only because the environment can't run it
  (`requires: ocr` on Linux, say) is not flagged: it may be exactly
  right on the environment that does run it.

**Metrics**: the same case library, run through the candidate CLI alone
and judged against case labels — no reference needed:

```bash
PYTHONPATH=eval:. python -m scorecard metrics --json /tmp/metrics.json
```

Reports silent miss (leak case, exit 0), downgrade (expected 1, got 2),
false hard (clean case with a hard finding), review rate (clean case,
exit 2), crashes, timeouts, and runtime p50/p95 — all against `expected`
labels, so a documented `known_gap` still counts as a miss here (that is
the point: it tracks how many gaps are left, not just whether they are
labelled).

**Reference result caching**: `--cache-dir DIR` skips the reference
worktree and subprocess runs entirely for any case whose result is
already cached under today's key: the reference commit, a hash of
`caselib/cases.lock.json`, a hash of every `eval/scorecard/*.py` file
(so the cache invalidates itself the moment the comparison logic
changes, not just when someone remembers to bump a version), and this
machine's qpdf/exiftool versions, OCR availability, the PyMuPDF version,
and whether `REQUIRE_FULL_ENV` is set (it changes what `have` gets
passed to `normalize`, so it changes the stored keys, not just this
machine's raw capability) — a cache entry from a different environment
or mode is never reused. On top of that per-run key, each cached case
also carries its own `input_hash` — `sha256(pdf bytes + json(rules,
sort_keys=True))` — so changing that one case's rules (or a generator
producing different bytes the build lock didn't catch) invalidates just
that entry instead of silently serving a reference result for different
input. A crashed or timed-out reference run is never written to the
cache (and never trusted back out of it), so one flaky run doesn't
poison every run after it. CI passes a persistent cache directory
(`actions/cache`) so most PRs never re-run the reference at all.

### The real-world corpus (local only)

Clean-side metrics (false hard, review rate) on real files, stratified
by whether they carry a text layer, have more than one revision, and
producer family. Nothing here is committed: the manifest and results
live under `eval/scorecard/real_corpus/` (gitignored) — every `--manifest`
and `--out` path is refused outside it unless you pass `--allow-outside`
(for, say, an external drive; you are then responsible for never
committing or sharing that path). Entries are keyed by SHA-256, with
paths relative to a root directory you configure — that root itself is
never recorded anywhere, but **the manifest's own `path` field is a
relative file name from it**, so the manifest is exactly as sensitive as
a directory listing of your corpus and must never be committed, emailed,
or pasted anywhere. Only the coarse producer *family* (e.g. `"acrobat"`,
`"office"`) is stored — the raw `/Producer` string is read, classified,
and discarded on the spot, since real producer apps sometimes embed a
username, hostname, or email address in it.

```bash
# Build the manifest from a directory of real PDFs (never committed):
PYTHONPATH=eval:. python -m scorecard corpus build --root ~/corpus

# Report files the manifest expects that are missing or have changed:
PYTHONPATH=eval:. python -m scorecard corpus check --root ~/corpus

# Scan every manifest file, report clean-side metrics per stratum:
PYTHONPATH=eval:. python -m scorecard corpus run --root ~/corpus \
    --secrets secrets.json --json /tmp/corpus.json
```

`corpus run` also writes a **plaintext copy of `--secrets`** into `--out`
per file scanned (`<sha256-prefix>.rules.json` — the same scratch file the
CLI itself reads), so whatever values `--secrets` names live there too,
not just in the file you pointed at. `--out` defaults under
`eval/scorecard/real_corpus/scan-workdir` (gitignored, guarded the same
way as `--manifest`) for exactly this reason.

### Where each gate runs

- **Linux CI, every PR**: the non-OCR case library and the differential
  against `eval-ref-0` (`.github/workflows/tests.yml`, job
  `scorecard-diff`) — cases needing OCR are skipped automatically (no
  Vision bridge on Linux), matching `requires: ocr`/`no-ocr` filtering.
- **Scheduled, weekly, macOS**: the full case library including grids,
  with OCR (`REQUIRE_FULL_ENV=1`), so the grids' OCR-free labels are
  checked to still hold with OCR present
  (`.github/workflows/scorecard-weekly.yml`).
- **Local**: the real-world corpus (above) — never in CI, since the
  files never leave your machine.
