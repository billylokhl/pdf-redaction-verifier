# PDF Redaction Verifier

A forensic CLI that independently checks whether a PDF still contains
secrets that were supposed to be redacted — not just in the visible text,
but in every place a redaction can leave data behind.

## Quick start

```bash
conda env create -f environment.yml && conda activate pdf-redaction-verifier
```

Write the secrets you expect to be gone into a JSON file:

```json
[
  {"name": "Applicant SSN", "value": "123-45-6789"},
  {"name": "Any SSN", "class": "ssn"}
]
```

Scan a document against it:

```bash
pdf-verify --target document.pdf --secrets secrets.json
```

The exit code is the verdict:

| Code | Meaning |
| --- | --- |
| `0` | **Clean** — no secret found, and every layer actually ran. |
| `1` | **Leak** — a secret was detected. The report names where. |
| `2` | **Cannot certify** — a layer could not run, or a match needs manual review. Never treat a `2` as clean. |

That last row is the whole point: the tool fails *closed*. If it could not
look somewhere, it says so instead of certifying the document clean.

## How it works

You give it the secrets that should be gone; it looks for them everywhere
they could still be hiding and reports what it finds.

Every comparison happens on **normalized** text, so formatting cannot hide
a match. `123-45-6789`, `123 45 6789`, a version split across a line break,
and the fullwidth `１２３４５６７８９` all reduce to the same thing before
matching (NFKD fold → strip accents → casefold → keep only alphanumerics).
Secrets are also matched across page boundaries.

Rules come in three kinds (see [Rules file](#rules-file)):

- **value** — a specific known string (`"123-45-6789"`).
- **class** — a built-in kind with a validator: `ssn`, `credit-card`
  (Luhn), `email`, `us-phone` (NANP). The validator suppresses
  structurally invalid matches so random digits don't cause false alarms.
- **pattern** — your own regex.

Findings are **two-tier**. A match is a *hard* finding (exit 1) only when
the characters are genuinely adjacent on one real surface — a single
visual line, one decoded PDF string, one metadata value, one OCR pass. A
match that only appears once separate lines, table columns, or pages are
fused together is demoted to a *manual-review* warning (exit 2), so a
coincidental run of digits never hard-fails a clean document and a
possible leak is never silent. Reported samples are masked.

## What it checks — six layers

A redactor can succeed on the page and still leave the data in the file.
Each layer looks at a different place that can happen:

| Layer | Looks at | Catches a redaction that… |
| --- | --- | --- |
| **DOM** | layout-aware text, in horizontal and vertical reading order | left the text selectable, out of draw order, rotated, or split into per-character boxes |
| **OCR** | the rendered page pixels, via Apple Vision (correction on and off) | flattened the secret into an image or outlined vector text |
| **Metadata** | XMP / Info / embedded metadata, via `exiftool` | scrubbed the page but not the document properties |
| **Objects** | every PDF object walked structurally — dictionary strings and text stream bodies, with images/fonts/binary excluded by type *and* by content | drew a box over the text but left the original object in the file (reported as **ORPHANED** when nothing references it) |
| **Binary** | the whole decompressed byte stream, via `qpdf` | left content only a damaged or unusual cross-reference table reaches (value secrets only, always manual-review) |
| **Hidden** | attachments, annotations, form-field values, links, JavaScript, optional-content group names | hid the data where no page renders it at all |

The layers are independent on purpose: each uses its own extraction and
its own matching, so a flaw in one place to look cannot hide a leak from
the others. What no tool can tell you is whether your *rule set* was
complete — this proves the redactor removed what you named, not that you
named everything.

## Rules file

`--secrets` accepts either this tool's JSON array or a
a redaction tool's `redact_config.yaml`
(selected by the `.yaml`/`.yml` suffix), so one file can drive both
redaction and verification without the two drifting apart.

### JSON

Each entry has a `name` and exactly one of:

- `"value"` — a known secret string, matched through normalization.
- `"class"` — a built-in class: `ssn`, `credit-card`, `email`, `us-phone`.
- `"pattern"` — a custom regex (`re.MULTILINE`; names must be unique).

### Shared YAML config

| YAML key | Becomes |
| --- | --- |
| `exact_values` | value rules (normalized matching) |
| `patterns` | pattern rules, compiled `IGNORECASE` — the same flags the redactor uses |
| `entity_types` | this tool's built-in classes — `ssn`, `email`, `phone`→`us-phone`, `credit_card`→`credit-card` |
| `backend`, `model`, `llm_url`, `scrub_metadata` | ignored |

The other entity types — `person_name`, `address`, `date_of_birth`,
`account_number`, `drivers_license`, `passport` — are found by LLM
judgement upstream and have no regex equivalent here. They are **not
silently dropped**: the run warns it cannot verify them and exits `2`, so
a shared config never implies coverage it lacks. `phone` is only partly
verifiable (NANP numbers only) and says so; an entity type outside
the redactor's ten is a hard error, not a coverage gap.

Two YAML gotchas: **omitting `entity_types` means all ten** (matching
the redactor's default), and **quote your values and patterns** — YAML reads
unquoted `00123456` as octal and `Invoice #[0-9]{6}` as just `Invoice`;
the tool keeps the literal text and warns (exit `2`) until you quote it.

## Installation

Python 3.10+ (CI tests 3.10 and 3.12); dependencies, including the
macOS-only Apple Vision OCR bridge, are declared in `pyproject.toml`. The
Metadata and Binary layers shell out to `exiftool` and `qpdf`; without
them those layers degrade and the run exits `2` rather than certifying
clean. OCR is macOS-only; elsewhere that layer reports itself unavailable
and the run exits `2`.

conda installs everything in one command (conda-forge ships the binaries):

```bash
conda env create -f environment.yml && conda activate pdf-redaction-verifier
```

With uv or pip, install the two binaries separately:

```bash
brew install exiftool qpdf && uv pip install -e '.[test]'   # or: pip install -e '.[test]'
```

On Debian/Ubuntu the binaries are
`sudo apt-get install libimage-exiftool-perl qpdf`. Drop `[test]` if you
do not need the test suite. `python verify.py --target ... --secrets ...`
works installed or not; pass `--fail-fast` to stop at the first confirmed
finding.

[DESIGN.md](DESIGN.md) explains the design rationale — the exit-code
contract, the two-tier matching model, and the known limitations.

## Tests

```bash
pytest
```

Every regression test pins a previously confirmed bug; fixtures are
generated on the fly, nothing binary is committed, and tests needing
Apple Vision, `exiftool`, or `qpdf` skip with a reason when the tool is
absent. Two suites go further and test robustness directly:

- **`test_corpus.py`** scans the same planted secret across the on-disk
  layouts real producers emit — classic xref, object streams + xref
  stream (the PDF 1.5+ default of Acrobat/Ghostscript/Chrome), incremental
  updates, garbage-collected rewrites — asserting a leak is found and a
  clean file is never falsely accused of orphaned content in any of them.
- **`test_mutation.py`** plants a secret on each carrier surface and
  asserts detection, then neutralizes each guard in turn and asserts the
  bug it prevents comes back — so a guard whose removal changes nothing is
  caught, not just an outright regression.

To exercise **real vendor PDFs** without committing binaries, drop a
`foo.pdf` plus a `foo.pdf.secrets.json` sidecar (a list of `[name, value]`
pairs) into `tests/corpus_pdfs/`; `test_corpus.py` picks them up
automatically.
