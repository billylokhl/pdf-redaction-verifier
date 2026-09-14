# PDF Redaction Verifier

CI: [tests workflow](https://github.com/billylokhl/pdf-redaction-verifier/actions/workflows/tests.yml)
(status badges cannot render while this repository is private; restore
the `badge.svg` image if it goes public)

Forensic CLI tool that detects sensitive strings (secrets) inside a PDF across
five independent layers: layout-aware DOM text, OCR (Apple Vision), metadata
(exiftool), decompressed binary streams (qpdf), and hidden objects —
attachments, annotations, form fields, links, scripts and layer names — that
no page renders.

[DESIGN.md](DESIGN.md) explains why it is built this way — the exit-code
contract, the two-tier matching model, and the known limitations.

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

The `--secrets` argument accepts either this tool's JSON rules array or a
a redaction tool's `redact_config.yaml`
(selected by the `.yaml`/`.yml` suffix), so one file can drive both
redaction and verification without the two drifting apart.

### Shared YAML config

| YAML key | Becomes |
| --- | --- |
| `exact_values` | value rules (normalized matching) |
| `patterns` | pattern rules, compiled `IGNORECASE` — exactly the flags the redactor uses, so a shared rule means the same thing in both tools |
| `entity_types` | **this tool's own** built-in classes — `ssn`, `email`, `phone`→`us-phone`, `credit_card`→`credit-card` |
| `backend`, `model`, `llm_url`, `scrub_metadata` | ignored |

The remaining entity types — `person_name`, `address`, `date_of_birth`,
`account_number`, `drivers_license`, `passport` — are found by LLM
judgement and have no regex equivalent. They are **not silently
dropped**: the run warns that it cannot verify them and exits `2`, never
`0`, so a shared config cannot imply coverage it does not have. `phone`
is only *partly* verifiable (this tool checks NANP numbers only) and says
so. An entity type outside the redactor's ten is a **hard error**, not a
coverage gap — a typo must be fixed, not waved through.

Two YAML details the shared workflow depends on:

- **Omitting `entity_types` means all ten**, matching the redactor's own
  default — so an omitted key produces the same scope warning a full
  list would, rather than quietly scanning for nothing.
- **Quote your values and patterns.** YAML reads unquoted `00123456` as
  octal `42798` and `Invoice #[0-9]{6}` as just `Invoice`. This tool
  keeps the literal text and warns, but the redactor reading the same
  file does not — it removes the coerced string. An unquoted value
  therefore forces exit `2` until you quote it in the config.

Verification stays independent where it matters: `entity_types` map to
this tool's regexes and validators, never the redactor's, and the four
layers search places the redactor may never have touched. What a shared
file *cannot* tell you is whether the rule set itself was complete — it
proves the redactor did what it was told, not that it was told enough.

### JSON rules file


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
