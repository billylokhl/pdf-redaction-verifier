# PDF Redaction Verifier

CI: [tests workflow](https://github.com/billylokhl/pdf-redaction-verifier/actions/workflows/tests.yml)
(status badges cannot render while this repository is private; restore
the `badge.svg` image if it goes public)

Forensic CLI tool that detects sensitive strings (secrets) inside a PDF across
four independent layers: layout-aware DOM text, OCR (Apple Vision), metadata
(exiftool), and decompressed binary streams (qpdf).

## Requirements

- Python 3.10+ (CI tests 3.10 and 3.12)
- `pip install -r requirements.txt` (PyMuPDF + pytest, all platforms)
- macOS only, for the Apple Vision OCR layer:
  `pip install -r requirements-macos.txt`
- CLI tools: `exiftool`, `qpdf` (`brew install exiftool qpdf`)

## Usage

```
python verify.py --target document.pdf --secrets secrets.json
```

Pass `--fail-fast` to stop at the first confirmed finding instead of
producing a complete forensic report.

## Rules file

Each entry in the `--secrets` JSON array has a `name` and exactly one of:

- `"value"` — a known secret string. Matched through normalization, so
  dashes, spaces, newlines, and Unicode look-alikes cannot hide it.
- `"class"` — a built-in pattern class: `ssn`, `credit-card`, `email`,
  `us-phone`. Classes carry validators (Luhn for cards, SSA area/group
  rules for SSNs) that suppress structurally invalid matches.
- `"pattern"` — a custom regex, matched against the raw extracted text
  of each layer (write your own separator handling, e.g. `[-\s]?`).

Pattern matches are reported with a masked sample (only the last 4
characters shown). Pattern rules match per page — a pattern hit split
across a page boundary is not detected (known-value secrets are).

Exit codes: `0` certified clean, `1` secret detected, `2` operational
error, incomplete scan, or a raw-stream match needing manual review — a
`2` must never be treated as a clean result.

## Tests

```
python -m pytest tests/
```

Every test pins a previously confirmed bug (detection gaps, false
positives, exit-code contract violations). PDF fixtures are generated on
the fly — nothing binary is committed. Tests needing Apple Vision,
`exiftool`, or `qpdf` skip with a reason when the tool is unavailable,
so the suite runs (partially) on any platform.
