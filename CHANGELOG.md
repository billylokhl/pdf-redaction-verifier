# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added
- `--json` report output format for machine-readable scan results
- `--version` flag to display tool version
- Tokenization of leftover content streams (compact inline images, long/compactly written text)
- Case library (`eval/caselib`) with parameter grids and raw-writer cases for robust testing
- Scorecard (`eval/scorecard`): a per-case differential of the CLI against a pinned reference version (environment-independent: drops OCR/tool-availability noise, and every accepted difference must be tracked in `eval/accepted_diffs.yaml`, with a `weaker: true` flag required on any verdict-loosening entry and a check for entries no case still produces), label-based metrics (silent miss, downgrade, false hard, review rate, crash/timeout, runtime), and a local-only, path-guarded real-world corpus for clean-side metrics (producer family only, never the raw `/Producer` string)
- Provenance sidecars for committed real-tool files (`eval/caselib/real/*.json`), a privacy scrub over every committed binary (raw bytes, decompressed streams, decoded PDF string tokens including UTF-16 and hex strings, metadata, and XMP), a blind red-team case slot (`eval/caselib/redteam/`) with frozen, adjudicated labels, and a gallery-fields ratchet (`mistake`/`recovery` required on leak cases)
- `eval/check_ratchets.py` and a CI job comparing every "may only shrink" allowlist and the red-team slot's frozen label anchor against the merge-base with `main` (or `HEAD~1` on a direct push), so a list and its own same-commit consistency check can no longer move together undetected
- Case library: pinned cases for every remaining `UNDOCUMENTED_GAPS` cell (24 silent misses and one false alarm) plus one new gap found while reviewing them (`live.font.overprinted`) — 25 silent misses total, documented as K12-K36 in `docs/REDESIGN.md` §8 — and three large `perf`-marked performance fixtures (a ~300-page scanned document, a ~500-page text document, a ~50 MB image-heavy file), run only with `RUN_PERF=1`. No change to `verify.py`'s own behavior or verdicts.
- Gallery (`eval/gallery`, `docs/REDESIGN.md` Phase 0e): a static, self-contained HTML page generated from the case library — the mistake, the recovery method, a page-1 render, the pinned (fabricated) secret and today's verdict for every leak case, with a clear marker on each one today's tool still certifies clean, plus a section for clean and false-alarm cases. `PYTHONPATH=eval:. python -m gallery build --out DIR [--results FILE]`; nothing it generates is committed, and it never gates anything — a non-gating CI job builds it and uploads it as an artifact. No change to `verify.py`'s own behavior or verdicts.

### Changed
**Security**: exiftool and qpdf now run with absolute paths, exiftool with `-config ""` disabled, minimal environment variables, and a private working directory. Tool exit warnings now include a `returncode` field. The unquoted-YAML-value warning in configuration parsing now reports only the YAML type rather than the converted value.

**Dependencies**: PyMuPDF 1.27.2.3 → 1.28.2 (plus pyyaml, pytest, pytest-cov patch/minor bumps). The case library's generated PDFs change byte-for-byte (lock regenerated), but the scorecard shows no verdict change on any case.

### Verdict changes
The following changes can move a file's exit code:
- **Exit 0 → 2**: Files with compactly written content streams (no spaces between operators), long inline images or text in leftover streams without BT operators, or font-before-BT patterns in leftover streams (now properly tokenized). The scanner cannot certify these files as fully clean because it cannot reliably extract the content.
- **Exit 1 → 2**: An unexpected internal error in the tool now exits 2 (uncertifiable) unless a secret had already been found, in which case it stays 1 (leak). The JSON report includes `error.code` as `INTERNAL_ERROR` when applicable.

## [0.1.0] - Initial release

### Added
- Initial release of pdf-redaction-verifier
- Multi-layer PDF scanning: text, OCR (macOS), metadata, objects, binary streams, hidden objects
- Support for exact value and regex-based secret matching
- Configurable rules via JSON and YAML formats
- Exit codes: 0 (clean), 1 (secret detected), 2 (operational error / uncertifiable)
- Cross-platform support (macOS with full environment, Linux with partial)
