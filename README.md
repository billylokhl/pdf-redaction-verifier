# PDF Redaction Verifier

CI: [tests workflow](https://github.com/billylokhl/pdf-redaction-verifier/actions/workflows/tests.yml)
(status badges cannot render while this repository is private; restore
the `badge.svg` image if it goes public)

Forensic CLI tool that detects sensitive strings (secrets) inside a PDF across
four independent layers: layout-aware DOM text, OCR (Apple Vision), metadata
(exiftool), and decompressed binary streams (qpdf).

## Installation

Python 3.10+ (CI tests 3.10 and 3.12). All dependencies — including the
macOS-only Apple Vision OCR bridge, selected automatically by a platform
marker — are declared in `pyproject.toml`.

The tool also shells out to two **external binaries**, `exiftool` and
`qpdf`, for the Metadata and Binary layers. pip and uv cannot install
those; without them the scan degrades and exits 2 rather than certifying
a document clean. Pick whichever install route suits you:

**conda** — the only single-command route, because conda-forge ships the
two binaries as well as Python:

```bash
conda env create -f environment.yml && conda activate pdf-redaction-verifier
```

**uv** — fast, but install the binaries separately:

```bash
brew install exiftool qpdf && uv pip install -e '.[test]'
```

**pip** — same, with pip:

```bash
brew install exiftool qpdf && pip install -e '.[test]'
```

On Debian/Ubuntu the binaries are `sudo apt-get install libimage-exiftool-perl qpdf`.
Drop `[test]` if you do not need the test suite. The Apple Vision OCR
layer is macOS-only; elsewhere that layer reports itself as unavailable
and the run exits 2.

## Usage

```
pdf-verify --target document.pdf --secrets secrets.json
```

`python verify.py --target ... --secrets ...` works too, installed or not.

Pass `--fail-fast` to stop at the first confirmed finding instead of
producing a complete forensic report.

## Rules file

Each entry in the `--secrets` JSON array has a `name` and exactly one of:

- `"value"` — a known secret string. Matched through normalization, so
  dashes, spaces, newlines, and Unicode look-alikes cannot hide it.
- `"class"` — a built-in pattern class: `ssn`, `credit-card`, `email`,
  `us-phone`. Classes carry validators (Luhn for cards, SSA area/group
  rules for SSNs, NANP for phones) that suppress structurally invalid
  matches, and their inputs are folded (NFKC + dash/space variants) so
  en-dashes or fullwidth digits cannot evade them.
- `"pattern"` — a custom regex, matched against the raw extracted text
  of each layer, compiled with `re.MULTILINE` so `^`/`$` anchor per
  line. Rule names must be unique.

Pattern matching is **two-tier**. Hard findings (exit 1) come only from
surfaces where the matched characters are genuinely adjacent: a single
visual line (column gaps split a line, so the cells of a table row are
never treated as adjacent), a single decoded PDF literal, one decoded
metadata value, or either Apple Vision reading pass. Matches that appear
only once lines, columns, adjacent literals, or pages are fused together
are demoted to manual-review warnings (exit 2) — coincidental digit
fusion must never hard-fail a clean document, and a possible leak must
never be silent.
Samples in the report are masked (at most 4 trailing characters, never
more than half the match) and control characters are sanitized.

Exit codes: `0` certified clean, `1` secret detected, `2` operational
error, incomplete scan, or a raw-stream match needing manual review — a
`2` must never be treated as a clean result.

## Tests

```
pytest
```

Every test pins a previously confirmed bug (detection gaps, false
positives, exit-code contract violations). PDF fixtures are generated on
the fly — nothing binary is committed. Tests needing Apple Vision,
`exiftool`, or `qpdf` skip with a reason when the tool is unavailable,
so the suite runs (partially) on any platform.
