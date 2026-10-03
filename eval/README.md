# Evaluation

The case library that measures the verifier — see
[docs/REDESIGN.md](../docs/REDESIGN.md) §5. Feasibility probes and real-corpus
measurements behind the redesign's Phase 1 ADRs live separately in
[`spikes/`](spikes/README.md): throwaway scripts, not run in CI and not
part of the case library.

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

### Real-tool files and their provenance (`caselib/real/`)

The only binaries in the library: committed output of a redaction tool,
because a generated corpus can only show a failure someone thought to
build. All data in them is fabricated (`families/redactors.py`'s
docstring). The tool itself is never named — only described generically
("a PyMuPDF-based redaction tool") — since this is a public repository.

Every `real/<name>.pdf` has a sidecar, `real/<name>.json`: its sha256
(re-checked against the file on disk, never trusted from the sidecar
alone), a declared per-file size cap, the tool's generic description,
version and run date, the exact settings and values used, which caselib
case (if any) generated its pre-redaction input, an explicit
"fabricated data" statement, the list of metadata fields scrubbed
(empty when the tool's own output already had none to scrub), and a
summary of the label (`families/redactors.py`'s `case()` call) it backs.
`families/redactors.py` cross-checks its own cases against their
sidecars at import time (`_sidecar`); `tests/test_case_library.py`'s
`TestProvenance` re-derives the same facts independently, so a sidecar
can't drift from either the file or the registered case without a test
failing.

### Privacy scrub

Every case whose bytes are committed as-is (`writer="file"` — today
`real/*.pdf` and the red-team round's PDFs) is scanned by
`TestPrivacyScrub`, in its raw bytes, every stream PyMuPDF can
decompress, every PDF string token (literal `(...)` or hex `<...>`)
those contain — decoded, including a hex string's packed bytes and a
UTF-16BE/LE literal's actual text, since a leak hiding behind either is
invisible to a plain ASCII scan of the surrounding bytes — the
document's metadata and XMP, and any embedded file's name or
description. It looks for home-directory paths (`/Users/…`, `/home/…`,
`C:\Users\…`), email addresses outside `example.com` / `example.org` /
`*.test`, hostname-shaped strings (`*.local`, `*.lan`, `*.corp`, …), and
XMP document/instance ids that look machine-generated (a real UUID in
`xmpMM:DocumentID` or `xmpMM:InstanceID`).

A genuine exception is a `Case.privacy_allowlist` entry of exactly
`{"kind": ..., "text": ..., "reason": ...}`: `"kind"` one of
`model.PRIVACY_KINDS`, `"text"` the finding's text exactly as the scrub
reports it, and a `"reason"`. A home-path finding stops at the username
(`/Users/jsmith`, never the full path), so a home-path entry excuses
every path under that one home directory — all of `/Users/jsmith/...` —
not a single file. An entry excuses a
finding only when both its kind and its text are equal (`==`) to the
finding's: it is a literal, never a pattern, so it can excuse nothing but
the one finding it names, and an `"email"` entry never excuses the same
text reported as a `"hostname"`. `model.Case` rejects any other shape at
construction, and `TestPrivacyScrub` also requires every entry to match
a finding its case actually produces, so a dead entry — or a regex
written where the literal belongs — fails. (Entries used to be
`re.fullmatch` patterns screened for breadth against fixed probe
strings; every screen tried was defeatable — `/Users/[^/]+`, `.+\.home`,
`[^@]+@acme\.com`, a conditional group — and no case used the
allowlist, so it is literal-only now.)

### The blind red-team slot (`caselib/redteam/`)

Every other case is written by someone who has read `verify.py`, which
is exactly the bias this corpus needs correcting for. `redteam/<round>/`
holds rounds built by someone working only from `COVERAGE.md` and the
threat model — see [`redteam/README.md`](caselib/redteam/README.md) for
the full protocol: how a round is authored (and what it must attest —
author, date, the `COVERAGE.md` commit it was given, a no-code-access
statement, a `README.md`; `families/redteam.py`'s `check_attestation`
enforces this at load time), why its `labels.json` is frozen (a sha256
in `round.json`, changeable only through a recorded `adjudications.log`
entry), and how a round may report a storage place COVERAGE.md has no
row for yet through the `new.<slug>` placeholder namespace
(`cells.NEW_CELL_ALLOWLIST`, redteam-only). Red-team cases load through
the ordinary family mechanism (`families/redteam.py`) and land in the
same `REGISTRY` with `origin="redteam"` — an invariant
(`TestRedTeam.test_origin_matches_the_loader`) checks that the set of
ids claiming that origin equals exactly the ids every round's
`labels.json` lists **on disk**, recomputed independently there rather
than read back from the loader's own (mutable, in-process)
`REGISTERED_IDS` bookkeeping — since nothing else stops an ordinary
family from setting `origin="redteam"` on its own, and trusting
`REGISTERED_IDS` alone would only catch a bypass the loader's own code
happened to also get wrong the same way.

`round.json`'s `labels_sha256` can be checked for internal consistency
by a test that only sees one commit — but nothing stops that same
commit from moving `labels_sha256` **and** `initial_labels_sha256`
together, since the test would just compare the new file against
itself. See "The ratchet check" below for how that's actually caught —
including the confirmed bypass of also renaming the round's directory so
a naive comparison-by-path never notices the old anchor is gone.

### Gallery fields ratchet

A leak case is shown in a gallery of how redaction fails, so it should
carry `mistake` (what caused it) and `recovery` (how it's found by
hand). `model.GALLERY_FIELDS_PENDING` is a may-only-shrink allowlist —
the same shape as `UNDOCUMENTED_GAPS` — for the leak cases that don't
carry both yet; `tests/test_case_library.py`'s `TestGalleryFields`
checks it is exact (nothing missing is left off it, and nothing on it is
actually filled in already).

### The ratchet check (`eval/check_ratchets.py`)

`UNDOCUMENTED_GAPS`, `NONFITZ_PENDING` and `GALLERY_FIELDS_PENDING` may
only shrink; a red-team round's `initial_labels_sha256` may never move.
Each is checked for internal consistency by
`tests/test_case_library.py` — but that suite only ever sees one
checkout, so nothing there stops a single commit from editing one of
these *and* loosening its own check to match, in lockstep. Catching that
needs a second, different commit to compare against:

`eval/check_ratchets.py` diffs every ratchet against the merge-base with
`main` (a pull request) or `HEAD~1` (a direct push to `main`, which
only catches a single-commit rewrite — see the script's docstring for
what that does and doesn't cover) and fails if anything grew, if an
`initial_labels_sha256` moved, if a ratchet the base's own registry
lists is no longer checked (`check_registry`, below), or if the base
can't be read well enough to tell. It is CI's `ratchets` job
(`.github/workflows/tests.yml`, needs `fetch-depth: 0` for the history to
diff against) and can also be run locally:

```bash
python eval/check_ratchets.py                 # resolves the base itself
python eval/check_ratchets.py --base <ref>     # or name one explicitly
```

The check reads each ratchet statically from its module's source,
never by importing it, so it must be sure the literal it reads is the
value Python ends up binding. It fails closed ("cannot verify") unless a
ratchet name has **exactly one** possible writer anywhere in its module,
and that writer is a top-level `NAME = frozenset({...})` (or annotated)
literal assignment. Everything in the module is walked, not just its top
level, and anything that could bind the name counts as a writer: a
store or delete in any block (`if`/`try`/`with`/`for`/`while`, a class
body, a function), tuple or starred unpacking, `for`/`with ... as`/
walrus targets, augmented assignment, `global NAME`, `def`/`class NAME`,
an import binding it (including an alias), `except ... as NAME`, a
`match` capture, a parameter or keyword argument named it
(`globals().update(NAME=...)`), any star import, and any string or bytes
constant containing it other than a docstring (`globals()["NAME"]`,
`vars()`/`locals()` subscripts, `setattr(sys.modules[__name__], "NAME",
...)`, `exec("NAME = ...")`). Rebinding `frozenset` itself also fails.
The string rule is deliberately broad: even an error message or comment
string that mentions the name outside a docstring counts, and the
failure names each writer and why it counts ("a string constant
mentioning UNDOCUMENTED_GAPS at line 12", "def UNDOCUMENTED_GAPS at line
40", ...), so a harmless one is quick to spot and reword.
Consumers import the sets through the `caselib` package, so
`eval/caselib/__init__.py` is checked too (`check_reexports`): it may
bind a ratchet name only by re-exporting it unchanged
(`from .cells import UNDOCUMENTED_GAPS`), nothing else.

The tests that use the sets (`test_every_gap_has_a_pinned_case`,
`test_claimed_cells_have_non_fitz_evidence`, `TestGalleryFields`) don't
read an imported global: they call `_verified(NAME)`, which parses the
literal from the defining file with the same `_frozenset_literal` — the
value this check verified — so nothing that rebinds a global at runtime
changes what they compare against. Separately,
`test_ratchet_sets_in_effect_equal_their_literal` asserts that
`caselib.<NAME>` and the defining module's attribute (after every family
has loaded) equal that literal, which catches a rebind done while caselib
imports (a name built at runtime, `exec` of a computed string) for any
other importer. It cannot catch code that rebinds them later.

The base's own list of ratchets counts too (`check_registry`): the
`RATCHET_SETS` and `RATCHET_REEXPORTS` in the base's copy of
`eval/check_ratchets.py` — read from the base ref, not the running
script — must all still be checked. Deleting an entry, or renaming or
moving a set along with a matching entry, would otherwise pass ("nothing
to shrink from") and let the set grow unseen. A ratchet the base lists
must also be readable in the base, so a set that is missing there was
renamed or moved, not introduced. An unreadable base registry fails, and
so does a base that has ratchet files but no copy of the checker at all
(only a base that predates the ratchets entirely has nothing to compare).
**Retiring or renaming a ratchet is unsupported by design**: an emptied
ratchet stays, with its test, for good — it costs nothing and guards the
"no exceptions" state. Adding a new ratchet is always fine.

**Threat model.** The ratchets guard against accidental or unreviewed
growth: a set grown by any ordinary edit, a rebind in some block or
form the author didn't think of, a rename, move or removal of a ratchet,
or a re-export that changes it. They do not defend against deliberately
malicious code in a pull request: edits to this checker's own logic, or
runtime code planted to mutate state the tests read (monkeypatching
`_frozenset_literal`, say). Both are visible in the diff, and adversarial
review of every pull request is mandatory on this repository; that is
what those rely on.

Its comparison logic (`run_all_checks` and friends) takes an abstract
"tree" (`read(path)`, `glob(pattern)`), so `tests/test_check_ratchets.py`
exercises it against fake in-memory trees — no git, no filesystem —
independently of `GitTree`'s subprocess calls.

Two properties a confirmation review specifically probed and confirmed
were missing, both now closed:

- **A round is matched by identity, not by path.** `check_redteam_anchors`
  reads round.json's own `"round"` field, not the directory it lives in.
  `git mv`-ing a round's directory changes nothing by itself; `git
  mv`-ing it *and* forging a fresh `initial_labels_sha256` at the new
  path is still caught, because the old anchor is looked up by identity
  and found regardless of where the file now lives. A round whose
  identity disappears between the two trees entirely — deleted, or its
  `"round"` field itself changed, indistinguishable from delete-and-
  recreate — is always a failure: a round's history may never simply
  vanish.
- **An unresolvable base fails closed in CI.** If `eval/check_ratchets.py`
  can't work out what to diff against (`origin/main` unreachable, no
  merge-base, no `HEAD~1`), that used to print a message and exit `0` —
  silently skipping the entire check. Now `is_ci_context` (`GITHUB_ACTIONS
  =true`, or `GITHUB_BASE_REF` / `GITHUB_EVENT_NAME` set at all) decides:
  inside anything that looks like CI, no base to compare against is a
  hard failure; only a genuinely local invocation with none of those
  variables set may skip cleanly.

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

Perf cases (below) are excluded (`caselib.lock.lockable`): they are large
and slow on purpose, and nothing reviews their bytes line by line the way
it does an ordinary case's.

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

`corpus build` picks up only files named `*.pdf` in lower case: rename or
link any `.PDF` files first, or they are silently left out.

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

### The inventory agreement gate (Phase 3a-6)

ADR 0010's gate for the inventory (`redaction_verifier.inventory`, in
shadow mode): on every file, `build_inventory` runs in a child process
with a 60 s timeout, then the reader differential
(`eval/scorecard/inventory.py`, the same oracle the tests use) compares
the inventory against MuPDF and qpdf in a second child with its own
timeout (default 900 s, reported separately; each qpdf call inside it
may use the whole budget, so a huge but valid file is not cut short). **The gate passes only
with zero unflagged disagreements** (a file that does not tile counts as
one), zero crashes, zero timeouts, and every unflagged file actually
verified: an oracle that crashes or times out on an unflagged file, or
two children that flag the same file differently, fail it too. Flagged
files, UNINDEXED/CONTESTED regions and flag rates are reported, never
gated (ADR 0010: measure before enforcing). Exit status: 0 pass, 1 fail,
2 refused (a drifted or empty manifest, or no qpdf on PATH). A `--json`
file left from an earlier run is deleted first, so a stale aggregate
never passes for a fresh one.

```bash
# The real corpus: the manifest is verified first -- any file missing or
# changed since `corpus build` and the run is refused (exit 2, counts only;
# `corpus check` lists the files). Hours on 2,031 files: --workers N helps,
# but keep N below your core count or the 60 s timeout measures contention.
PYTHONPATH=eval:. python -m scorecard inventory run --root ~/corpus \
    --json /tmp/inventory-gate.json

# The same gate over the case library (every case this machine can build)
# and over generated and mutated files (seeded, reproducible):
PYTHONPATH=eval:. python -m scorecard inventory caselib --json /tmp/caselib-gate.json
PYTHONPATH=eval:. python -m scorecard inventory fuzz --count 2000 --seed 0 \
    --json /tmp/fuzz-gate.json
```

What each part of the output means:

- `gate`: `passed`, and the gating counts: `unflagged_disagree` (by
  failed check in `unflagged_disagree_by_check`: `object_set`,
  `stream_data`, `member_value`, `dead_bodies`, `qpdf_check`,
  `mupdf_warning`, `needs_password`, `encryption`, `oracle_error`,
  `tiling`),
  `crashes`, `timeouts`, `unverified`, `inconsistent`.
- `unflagged_agree`, and `unflagged_agree_encrypted`: encrypted files
  whose object sets agree but whose bytes and values are not compared
  until 3a-8/9. A revision is exempted only when our trailer, MuPDF and
  qpdf all read it encrypted; any split (an `/Encrypt null` both readers
  ignore, say) is an `encryption` disagreement. `encrypted_files` counts
  files MuPDF reads encrypted, `encrypt_entry_files` those whose
  trailers carry an `/Encrypt` entry.
- `qpdf_check_errors_on_agree`: agreeing files where `qpdf --check`
  printed an `ERROR` line (exit 2) -- so far page-tree semantics, the
  reference graph's (3a-7); counted, not gated.
- `flagged`, `flag_rate`, and `flags`: files per flag reason, the top ten
  reasons, and files with exactly one reason. A flagged file is never
  compared with the readers: the inventory said the readers may disagree,
  and a flagged file can never exit 0 once the inventory is wired in.
- `regions`: files, regions and bytes left UNINDEXED or CONTESTED in the
  file's own tiling (object streams' decoded tilings are not included).
- `timing`: percentiles of the build child's wall clock (interpreter
  start included), the oracle's, and the work units charged per byte.
- `pending_decisions`: counts for the owner's open decisions --
  incremental updates to linearized files (#44 item 1: a linearized file
  with more than one revision; `/L` off the file's length is reported
  apart, since trailing bytes alone do that), comment lines
  claimed after the header or an intermediate `%%EOF` (#46 item 1: files,
  lines, the longest line in bytes, lines with non-printable bytes or an
  `N G obj`), streams whose indirect `/Length` lives in an object stream,
  `REVISION_AMBIGUOUS` flags whose value is equal in every revision, and
  dead object streams (#46 item 4).
- `config`: provenance -- the git commit and whether the tree had local
  changes, the qpdf, PyMuPDF, MuPDF and Python versions, the platform
  (OS, release, architecture), the start time (UTC) and the run's
  options.
- `compared`: how many streams and object-stream members were compared;
  every member is compared with MuPDF, the first `--qpdf-members` (8) per
  revision also with qpdf, one qpdf process each.

**What to share: the `--json` aggregate only.** It holds counts, rates
and timings -- no file name, path, SHA-256, document bytes or reader
message -- and so does stdout. Per-file detail goes to `--detail`
(default `eval/scorecard/real_corpus/inventory-gate-SOURCE.jsonl`, SOURCE
being `corpus`, `caselib` or `fuzz`, so one run never overwrites
another's; gitignored and guarded like `--manifest`): one line per file keyed by
SHA-256, with its verdict, flag reasons, the failed check and where
(revision, object number), timings, the measurements, and reader
messages only as scrubbed templates (numbers, names, strings and quoted
text replaced; a child's failure only by its exception class). It is as sensitive as
the manifest -- never commit or paste it; use it to find a file on your
own machine (`sha256sum`) when a number needs explaining.

### Where each gate runs

- **Linux CI, every PR**: the non-OCR case library and the differential
  against `eval-ref-0` (`.github/workflows/tests.yml`, job
  `scorecard-diff`) — cases needing OCR are skipped automatically (no
  Vision bridge on Linux), matching `requires: ocr`/`no-ocr` filtering.
- **Scheduled, weekly, macOS**: the full case library including grids,
  with OCR (`REQUIRE_FULL_ENV=1`), so the grids' OCR-free labels are
  checked to still hold with OCR present
  (`.github/workflows/scorecard-weekly.yml`).
- Both macOS jobs — this one and `full-macos` in `tests.yml`, which runs
  the suite on every PR — use `macos-latest` (macOS 26 as of this
  writing), so a newer macOS is covered only by local runs. Run the case
  library locally after an OS upgrade: on macOS 27 that is how an OCR
  failure on every page was found (fixed; see the CHANGELOG).
- **Local**: the real-world corpus (above) — never in CI, since the
  files never leave your machine.

## Performance files (`marked perf=True`)

Three large, generated files representative of real workloads
(`families/perf.py`), for measuring runtime rather than correctness:

- `file.perf-scanned-300` — a ~300-page scanned-style document (every page
  an image of text), for the OCR layer's per-page cost.
- `file.perf-text-500` — a ~500-page text document in an embedded font
  (Word-export-like), for the Text layer's per-page cost.
- `file.perf-image-heavy` — an image-heavy ~50 MB file (a handful of
  large, incompressible images), for throughput and memory on a
  big-but-few-objects file.

These are synthetic runtime proxies checked into this repository, not the
real-world corpus files docs/REDESIGN.md §5 asks for in Phase 0c (a local,
SHA-256-keyed, non-fabricated manifest of representative vendor files) —
they exercise the same per-page/per-object cost shapes without needing
any real document.

They are excluded from the default test run — `pytest` skips anything
marked `perf` unless `RUN_PERF=1` is set:

```bash
RUN_PERF=1 pytest tests/test_case_library.py -k perf -v
```

or, outside pytest, to keep the PDF and time the scan directly:

```bash
PYTHONPATH=eval:. python -m caselib.run /tmp/perf file.perf-scanned-300
```

## Gallery (`gallery/`)

A static, self-contained HTML page generated from the case library
(docs/REDESIGN.md's Phase 0e): "what a reader sees, the recovered secret,
the tool's verdict." **It never gates anything** — it is not run by
`pytest`, not part of `ci-ok`, and has no verdict of its own.

```bash
PYTHONPATH=eval:. python -m gallery build --out /tmp/gallery
PYTHONPATH=eval:. python -m gallery build --out /tmp/gallery --results /tmp/diff.json
open /tmp/gallery/index.html
```

For every leak case (grouped by family, then by COVERAGE.md cell) it
shows the id, the story, the **mistake** (what the redactor or a tool did
wrong) and the **recovery** (how a person actually gets the secret back
out), a page-1 PNG rendered fresh at build time (never committed —
"what a reader sees"), the pinned secret(s) planted in it (fabricated:
the canonical example SSN, 123-45-6789), and today's verdict. Clean and
false-alarm cases get their own section. Each render may carry a short
caption saying where the pinned value is relative to page 1, derived
from the case's primary cell rather than a hand list:

- A row that never reaches the rendered page (off-page, a switched-off
  layer, an orphaned or superseded object, metadata, an attachment, a
  script, private data, unindexed bytes, ...): "Nothing visible here: the
  secret is elsewhere in the file".
- "live" (drawn on a page): left uncaptioned by design; the value may be
  covered (a box over it, say) — the caption is about where the value
  is, not whether a reader can make it out.
- "match" (a layout-splitting limit): the row alone can't say, so it is
  decided from the pages' own extracted text, rule by rule, with letters
  and digits compared and everything else (separators, spaces, line
  breaks) ignored. The whole value on one line of page 1 gets no
  caption; the whole value on page 1 but only reading across lines
  (`match.line-wrap` and friends, a two-column wrap) gets "The value is
  on this page, split across lines"; at least 3 characters of it on
  page 1 with the rest on a later page (`match.page-break` and friends)
  gets "Only part of the value is on page 1; the rest is on a later
  page"; none of it on page 1 but all of it further on gets "Not on page
  1: the value appears later in the document"; and in no page's text at all
  gets "Nothing visible here" (e.g. `match.extreme-coordinates`, K35,
  which draws its text where PyMuPDF's extraction never returns it, so
  the page-1 PNG is blank). A rule with no literal value (a built-in
  class like `ssn`) is judged by where its own regex finds a match its
  validator accepts, one line at a time or across joined lines — never
  over the whole page with every separator stripped, which would fuse
  digits from unrelated lines. With several rules, the one page 1 shows
  most of decides. `tests/test_gallery.py` pins the answer for every
  `match.*` leak case against a hand-checked table
  (`tests/gallery_visibility.json`).

Captions report where the value is in the page's text, not whether it is
legible: a value under a box, drawn white on white, or in invisible
text render mode 3 is on page 1 all the same, so it gets no caption.

**The miss marker** — the gallery's most important one — is "MISSES IT
TODAY" on any leak case whose *shown* verdict has exit `0`: not the
pinned label directly, but whatever `gallery.verdicts.verdict_for`
actually displays (measured, when `--results` covers the case; the
label otherwise). A leak case with **no** `known_gap` at all showing exit
`0` is the dangerous direction — a real, undocumented miss — and gets a
distinct "NEW MISS (not a known gap)" badge instead, so it's never
confused with an already-tracked gap. When a measured result disagrees
with a pinned known gap in the *reassuring* direction (the label says
exit `0`, this run's measured verdict caught it anyway), the page says so
in a note rather than showing a stale badge — the gap may already be
closed, or this run's environment/version differs from the one the
label was pinned against. A crashed measured run is never a miss —
specifically, a crashed **candidate** run: a `scorecard diff --json`
row's own `"crashed"` field is true when *either* side crashed
(`differential.CaseDiff.crashed`), so `gallery.verdicts.load_results`
keys off the row's `"candidate"` key instead (`None` exactly when the
candidate itself crashed) — a reference-only crash no longer hides a
real candidate result (including a real miss) behind "crashed". The
candidate's result is shown as usual, with a note that the reference run
crashed on this case, so there is nothing to compare it against.

Each case links to its COVERAGE.md cell and, where its known gap is one
of docs/REDESIGN.md §8's numbered gaps, to its K-number, via
`gallery.knumbers.CASE_TO_K` — an explicit, hand-verified table (an
earlier version derived this by fuzzy-matching each case's story against
§8's table; a review found that untested and non-deterministic, so it's
now a plain dict, checked by `tests/test_gallery.py` against
`documented_k_numbers` — parsed straight from §8 — so the two can never
silently drift apart). COVERAGE.md's cell ids don't appear verbatim
anywhere in COVERAGE.md's own prose (confirmed: none of the case
library's ~70 cell descriptions are literal substrings of it either), so
the link points at the file as a whole rather than a GitHub text-fragment
anchor (`#:~:text=...`) — that mechanism does work on GitHub's rendered
markdown (verified against a live example), there's just no reliably
derivable search string to hand it per cell.

**The verdict shown**: without `--results`, it's the case's own label
(`expected`, or `known_gap.today` for a known gap) — accurate as of the
last green test run, but not measured by the gallery build itself, and
labelled as such. With `--results`, a `scorecard diff --json` report
(eval/README.md's "The scorecard", above) — the gallery reuses that
runner rather than scanning cases itself; a case the report doesn't
cover (skipped this environment) still falls back to its label.

Perf cases and any case whose `requires` this machine can't meet are
skipped, the same as `caselib.run`. Nothing is written outside `--out`;
PDFs are built into a temporary directory. CI builds it on Linux (no
OCR, so gap labels are shown, never measured — running the scorecard
first isn't worth the extra time for a page that doesn't gate anything)
and uploads it as a `continue-on-error` artifact, outside `ci-ok`'s
`needs:`.
