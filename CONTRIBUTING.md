# Contributing

How to set up a development copy, run the checks CI runs, and open a pull
request. For what the tool does, see the [README](README.md); for why, see
[DESIGN.md](DESIGN.md).

## Setup

You need macOS, [Homebrew](https://brew.sh), and uv 0.12 (0.12.3 or newer,
below 0.13):

```bash
brew install exiftool qpdf
curl -LsSf https://astral.sh/uv/0.12.3/install.sh | sh
git clone https://github.com/billylokhl/pdf-redaction-verifier.git
cd pdf-redaction-verifier
uv sync --extra test --group dev     # test extra + lint/type-check tools
```

**The uv version is pinned.** `pyproject.toml`'s
`[tool.uv].required-version` (`>=0.12.3,<0.13`) matches the version CI
installs; a different uv refuses to run rather than resolving or installing
differently against the same lock file. Dependencies are hash-locked in
`uv.lock`, resolved for macOS and Linux on Python 3.10–3.14 (the pyobjc
packages stay macOS-only via their markers). CI installs with
`uv sync --locked`, which fails if the lock is out of date with
`pyproject.toml`.

Without uv: `pip install -e '.[test]'` plus `pip install ruff==<pinned>
mypy==<pinned>` (the `dev` group in `pyproject.toml` has the versions),
then drop the `uv run` prefix below.

### qpdf versions and OpenSSL

The inventory's reader-agreement tests (`docs/REDESIGN.md` Phase 3a)
compare the `qpdf` command against the libqpdf inside `pikepdf` (a runtime
dependency the shipped CLI does not import yet), and need the two at one
major.minor version: 12.4 with Python 3.11+, 12.3 with Python 3.10.
Homebrew's qpdf 12.4 matches. With a mismatch those tests fail closed and
the inventory gate refuses to run; install the matching release binary from
qpdf's GitHub releases, as CI does (`.github/workflows/tests.yml`).

qpdf's macOS release binary reads RC4-encrypted files (and AES-128 ones,
whose password check uses RC4) only if it finds OpenSSL's legacy provider,
in the modules directory it was built with. `verify.py` runs qpdf with a
stripped environment, so `OPENSSL_MODULES` does not reach it there. CI
links that built-in directory to Homebrew openssl@3's `lib/ossl-modules`;
do the same locally, or use Homebrew's own qpdf, which needs nothing
([#171](https://github.com/billylokhl/pdf-redaction-verifier/issues/171)).
Without the provider qpdf exits 2 on those files: the Binary layer is
flagged as not scanned, and the gate reads a split on encryption.

## Checks

```bash
uv run pytest                    # the test suite
uv run ruff check .              # lint
uv run mypy                      # types
uv run pytest tests/ --cov=verify --cov=caselib --cov=redaction_verifier  # with coverage
```

mypy checks `redaction_verifier` (strict on `inventory`, `budget`,
`ledger`, `sandbox` and `crypt`) and the evaluation harness:
`eval/caselib`, `eval/scorecard`, `eval/gallery`, `eval/redactor_set` and
`eval/check_ratchets.py`. `verify.py` itself is excluded.

**About the tests.** Every regression test pins a previously confirmed bug.
Fixtures are generated on the fly. The only committed binaries are the
small real-tool files in `eval/caselib/real/` (each with a provenance
sidecar), the red-team example round's file, and the README's
`examples/ocr-check.pdf` (made by `examples/make_ocr_check.py`). Tests
needing Apple Vision, `exiftool` or `qpdf` skip with a reason when the tool
is absent; with `REQUIRE_FULL_ENV=1` (as on CI's macOS jobs) a missing tool
fails collection instead. pytest's per-test timeout is 900 s
(`pyproject.toml`; 300 s on CI's macOS shards), so a hung Vision call fails
the run instead of stalling it.

Property tests use Hypothesis. With `CI` set (as on GitHub Actions) they
run the derandomized `ci` profile; locally the `dev` profile explores fresh
examples each run and replays any failure it found from `.hypothesis/`
until it passes (root-cause each one, then pin it as an explicit test).
`HYPOTHESIS_PROFILE=ci` reproduces CI exactly.

Suites that go further:

- **`test_case_library.py`** judges every case in the case library
  ([eval/README.md](eval/README.md)): generated PDFs, each with the correct
  verdict and the story of how its redaction failed. They include real
  redaction-tool output, known gaps pinned at today's wrong answer, and
  parameter grids: each carrier surface in each on-disk layout real
  producers emit (classic xref, object streams, incremental updates,
  garbage-collected rewrites), and page-text layouts that split a value
  across lines, pages, form boxes, columns and rotations. Its
  `TestCoverageDoc` checks COVERAGE.md's table against
  `eval/caselib/cells.py`, cell by cell.
- **`test_known_gaps_coverage.py`** keeps
  [COVERAGE.md's Known gaps](COVERAGE.md#known-gaps) in step with the case
  library, both ways (see the section's own note).
- **`test_mutation.py`** neutralizes each guard in turn and asserts the bug
  it prevents comes back, so a guard whose removal changes nothing is
  caught.
- **`test_corpus.py`** runs real vendor PDFs without committing them: drop
  a `foo.pdf` plus a `foo.pdf.secrets.json` sidecar (a list of
  `[name, value]` pairs) into `tests/corpus_pdfs/` (see its README). Never
  commit real documents.

The **scorecard** (`PYTHONPATH=eval:. python -m scorecard diff`) runs the
case library through the real CLI as a subprocess and diffs it against a
pinned reference version, so every intended verdict change is reviewed
explicitly in `eval/accepted_diffs.yaml`. It runs one scan per usable core
by default (fewer if RAM is short), so its per-file timeout measures the
file rather than contention; see
[eval/README.md](eval/README.md#the-scorecard-scorecard).

The **gallery** (`PYTHONPATH=eval:. python -m gallery build --out DIR`) is
a static HTML page of how each leak case's redaction fails, generated from
the same case library: illustrative only, never gating; see
[eval/README.md](eval/README.md#gallery-gallery).

## CI

`.github/workflows/tests.yml` runs on every pull request and push to
`main`:

- **full-macos:** the whole suite with Apple Vision, exiftool and qpdf
  (`REQUIRE_FULL_ENV=1`), except the case library's parameter grids
  (`-m "not grid"`). It runs as four parallel shards; `tests/ci_shards.py`
  defines the split, and a new test file lands in the last shard by
  default.
- **partial-linux:** the suite on Python 3.10, 3.12, 3.13 and 3.14 without
  OCR (OCR tests skip with a reason), each with the qpdf release matching
  that Python's pikepdf. It guards the degrade-loudly behaviour off macOS;
  the 3.12 leg also reports coverage.
- **scorecard-diff:** the scorecard differential, without OCR.
- **lint:** ruff and mypy.
- **ratchets:** `eval/check_ratchets.py` diffs every may-only-shrink list
  against the merge base, which a single checkout's tests cannot do.
- **gallery:** built and uploaded; never gating.
- **ci-ok:** one job that fails unless every gating job succeeded.

`.github/workflows/scorecard-weekly.yml` runs weekly (and on demand, as a
branch's pre-merge macOS+OCR gate): the full case library, grids
included, with OCR, plus the scorecard differential and metrics.

## Pull requests

- **Independent review.** Every PR gets review by someone other than its
  author on its final content. The PR body ends with a `## Review`
  section: a table of `Round | Commit reviewed | Verdict`, then
  `**Last reviewed commit: <sha>, the current head.**`. Any change after
  review, however small, gets a confirmation pass on the new head; merge
  only when the last reviewed commit is the head.
- **Docs in sync.** A PR updates every doc its change touches: README,
  docs/reference.md, CONTRIBUTING.md, DESIGN.md, COVERAGE.md (cells, ✓/⚑/✗
  parts, matching limits, Known gaps), eval/README.md, docs/REDESIGN.md and
  CHANGELOG.md.
- **Verdict changes.** Any change that can move a file's exit code (clean
  to uncertifiable, or certified to leak) is listed under **Verdict
  changes** in [CHANGELOG.md](CHANGELOG.md) and, if it moves a
  case-library case, accepted in `eval/accepted_diffs.yaml`.
- **Dates** in decisions, ADRs, the CHANGELOG, issues and PR bodies are
  the owner's local date, Pacific time.
- **Public repository.** Fabricated data only: no real names, documents,
  secrets or local paths, and no third-party redaction tool named (write
  "a redaction tool").
