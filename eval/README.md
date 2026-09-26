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
storage, adjacency), other warnings as (code, layer, tool). Exact legacy
warning codes and messages are deliberately not part of the key.

```bash
PYTHONPATH=eval:. python -m scorecard diff                      # every case
PYTHONPATH=eval:. python -m scorecard diff page.visible          # some
PYTHONPATH=eval:. python -m scorecard diff --json /tmp/diff.json --verbose
```

Exits non-zero if any case crashed or timed out, or changed in a way not
listed in `eval/accepted_diffs.yaml`.

**`eval/accepted_diffs.yaml`**: every intended per-case change between
the reference and the candidate — case id, the reference's normalised
key ("old"), the candidate's ("new"), a reason, the COVERAGE.md cell, and
the PR. A difference the file does not list exactly (both sides) fails
the differential. When a fix or a regression changes a case's key,
`scorecard diff --json` prints both keys for the case; copy them into a
new entry (or update the existing one, if this is a further change to a
case already listed) with a reason for the change, and review it in the
PR like any other test change.

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
already cached under today's key (the reference commit plus a hash of
`caselib/cases.lock.json` — so it invalidates itself whenever the
reference moves or a case generator changes). CI passes a persistent
cache directory (`actions/cache`) so most PRs never re-run the reference
at all.

### The real-world corpus (local only)

Clean-side metrics (false hard, review rate) on real files, stratified
by whether they carry a text layer, have more than one revision, and
producer family (from `/Producer`). Nothing here is committed: the
manifest and results live under `eval/scorecard/real_corpus/`
(gitignored), keyed by SHA-256 with paths relative to a root directory
you configure — that root itself is never recorded anywhere.

```bash
# Build the manifest from a directory of real PDFs (never committed):
PYTHONPATH=eval:. python -m scorecard corpus build --root ~/corpus

# Report files the manifest expects that are missing or have changed:
PYTHONPATH=eval:. python -m scorecard corpus check --root ~/corpus

# Scan every manifest file, report clean-side metrics per stratum:
PYTHONPATH=eval:. python -m scorecard corpus run --root ~/corpus \
    --secrets secrets.json --json /tmp/corpus.json
```

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
