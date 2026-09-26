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

New to PDF internals? Unfamiliar terms are defined in the
[Glossary](#glossary) at the end.

You give it the secrets that should be gone. Each of six **layers**
extracts text from a different part of the file, and every layer runs the
same **matchers** over what it extracted.

### How a match is found

There is no AI or fuzzy matching — only exact comparison and regular
expressions, so every finding is reproducible.

**Value rules → normalized exact substring search.** Both the extracted
text and each secret are normalized with Python's `unicodedata`: NFKD
decomposition (fullwidth `１` → `1`), accents stripped, casefolded, then
every character that is not a letter or digit removed. The tool then looks
for each normalized secret as an exact substring, using one compiled
Python `re` pattern built from all the secrets. So `123-45-6789` matches
`123 45 6789`, `１２３４５６７８９`, or the value split by a page break —
but not `123-45-6788`.

**Pattern rules → regular expressions (Python `re`).** Built-in classes
and your own regexes run on the extracted text after Unicode dashes,
spaces and fullwidth digits are folded to plain ASCII (NFKC). Each
built-in class is a regex plus a validator that rejects matches which
cannot be real:

| Class | Regex finds | Validator rejects |
| --- | --- | --- |
| `ssn` | `NNN-NN-NNNN` with consistent separators | SSA-impossible numbers: area `000`, `666` or `9xx`; group `00`; serial `0000` |
| `credit-card` | card-number digit runs (13–19 digits) | numbers failing the Luhn checksum, and 14-digit PDF date stamps |
| `email` | `name@domain.tld` | image asset names like `logo@2x.png` |
| `us-phone` | 10-digit numbers, optional `+1` | numbers breaking NANP rules (area code and exchange must start 2–9) |
| `pattern` | your regex, as written | — |

**Two tiers.** A *hard* finding (exit `1`) needs the match to be
genuinely present on one real surface: a single visual line, one PDF
object, one metadata value, one OCR reading. A known value split across a
page break, or across strings inside one PDF object, still counts as hard,
because splitting a secret is a real way to hide it. A match that only
appears after separate lines or table columns on a page are joined is a
*manual-review* warning (exit `2`). That way a coincidental run of digits
never hard-fails a clean document, and a possible leak is never silent.
Regex patterns are held to the stricter rule: hard only within one line or
one decoded string. Reported samples are masked (`****6789`).

## What it checks — six layers, most likely to find a leak first

A redactor can succeed on the page and still leave the data in the file.
The layers are ordered by how common their failure mode is in practice —
a judgment from well-known redaction failures and the cases this project
has seen, not measured statistics.

| # | Layer | Finds a secret that… | Method | Tools |
| --- | --- | --- | --- | --- |
| 1 | **Text** | is covered by a box but still selectable | characters read with positions, rebuilt into reading order, matched | PyMuPDF |
| 2 | **Objects** | the page no longer shows but the file still stores | every PDF object decoded and matched; unreferenced ones flagged ORPHANED | PyMuPDF + built-in PDF string parser |
| 3 | **Metadata** | sits in document properties | every metadata tag dumped and matched | exiftool, PyMuPDF |
| 4 | **Hidden** | no page renders at all | attachments, annotations, form fields, links, JavaScript, layer names read and matched | PyMuPDF |
| 5 | **OCR** | exists only as pixels | each page rendered to an image, text recognized, matched | PyMuPDF, Apple Vision |
| 6 | **Binary** | only a damaged file structure still reaches | whole file decompressed, searched for known values | qpdf |

### 1. Text — the page's text layer

The most common redaction mistake is drawing a black box over text that is
still there: invisible on screen, but selectable and copyable.

- **Method:** PyMuPDF `page.get_text("rawdict")` returns every character
  with its position. The tool rebuilds lines itself — clustering
  characters by position (tolerance scales with font size) and sorting
  within each line — so text drawn out of order, rotated, or one digit per
  form box still reads back as `123-45-6789`. It builds three readings:
  horizontal, vertical top-down and vertical bottom-up. A wide gap inside
  a line is treated as a column break, so neighbouring table cells never
  merge into a false number. Secrets are also matched across page
  boundaries.
- **Tier:** a known value is hard when it appears within one line of any
  reading or across a page break; a pattern is hard only within one line of
  the horizontal reading. Matches that need lines joined on a page are
  manual-review.

### 2. Objects — everything the file stores

A redaction tool can remove text from the page while the original stays in
the file as an object nothing uses any more. This layer caught every leak
in this project's real-document case.

- **Method:** walks every numbered PDF object with PyMuPDF (`xref_object`,
  `xref_stream`) and decodes its strings with a built-in parser that
  understands PDF syntax — `(literal)` strings with escapes and nested
  parentheses, `<hex>` strings, UTF-16 text. Images, fonts and embedded
  files are skipped (identified by their dictionary keys, backed by a
  byte-content check) so binary data cannot produce false matches.
  References are followed from the file's root; any object not reached is
  reported **ORPHANED** — left behind, not displayed.
- **Tier:** a known value is hard anywhere in one object, even split
  across its strings; a pattern is hard only within one decoded string.

### 3. Metadata — document properties

Title, author, subject, keywords and custom fields often keep a name or
case number that was scrubbed from the page.

- **Method:** `exiftool -json` dumps every metadata tag; each value is
  matched on its own. Tags describing the local file (`SourceFile`,
  `FileName`, `Directory`) are ignored, so the file's own path cannot
  match. The XMP metadata block is also read in-process with PyMuPDF
  `get_xml_metadata()`.
- **Tier:** hard findings per metadata value.

### 4. Hidden — content no page renders

- **Method:** PyMuPDF APIs read each hidden place directly: attachments
  (`embfile_*` — names, descriptions and contents up to 16 MB),
  annotation text and attached files (`page.annots()`), form-field values
  and names (`page.widgets()`), link targets (`page.get_links()`),
  JavaScript (found by walking the document's action entries), and
  optional-content group names (`get_ocgs()`).
- **Tier:** hard findings for text fields; attachment *contents* are
  arbitrary bytes, so matches there are manual-review.

### 5. OCR — what a viewer sees

Needed when the secret exists only as pixels: scanned pages, pages
flattened to images, text converted to vector outlines.

- **Method:** PyMuPDF renders each page to a 300 dpi grayscale image
  (`get_pixmap`). Apple Vision (`VNRecognizeTextRequest`, accurate mode,
  called through PyObjC) recognizes the text twice — with language
  correction on and off, because autocorrect can silently change digits.
  Both readings are matched. macOS only; elsewhere this layer reports
  itself unavailable and the run exits `2`.
- **Tier:** same rules as the Text layer, with both readings eligible for
  hard findings.
- **Not yet covered:** OCR reads the rendered page, so a box drawn *over*
  a scanned image hides the pixels underneath from it, even though they
  are still stored in the file.

### 6. Binary — the raw decompressed file

A backstop for files whose structure is damaged or unusual enough that
the object walk cannot reach every object.

- **Method:** `qpdf --qdf --object-streams=disable` rewrites the file with
  every stream decompressed; the output is streamed through the same
  normalized substring search, for value secrets only. Regex patterns are
  not run here — raw bytes produce too many coincidental matches.
- **Tier:** always manual-review, never a hard finding.

Each layer extracts text independently, so a blind spot in one extraction
cannot hide a leak from the others. What no tool can tell you is whether
your *rule set* was complete — this proves the redactor removed what you
named, not that you named everything.

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

## Glossary

### What is inside a PDF

A PDF is a pile of numbered **objects**. A **cross-reference (xref)
table** at the end of the file records where each one lives. Some objects
are **dictionaries** — key–value records like `<< /Title (Tax Return) >>`
— whose text values are **strings**, written `(like this)` or as hex
`<4A6F…>`. A **stream** is an object carrying a blob of (usually
compressed) data; a page's **content stream** holds its drawing
instructions ("set font, move here, draw these characters"). Objects
**reference** one another starting from the file's root; an object
nothing references is an **orphan** — still stored in the file, never
shown. Most redaction leaks live in that gap between what a page shows
and what the file stores.

### Terms

| Term | Meaning |
| --- | --- |
| **Annotation** | Something layered on a page: comment, sticky note, highlight, link. |
| **Apple Vision** | macOS's built-in text-recognition engine, used by the OCR layer. |
| **Carrier surface** | Any place in a PDF a secret can be stored (content stream, metadata, form field, …). |
| **Casefold** | Aggressive lowercasing that also handles non-English cases (`ß` → `ss`). |
| **Correction on/off** | Vision's autocorrect. It helps words but can "fix" digits wrongly, so both modes run. |
| **Decompressed** | File contents with compression undone, so the stored text is visible. |
| **DPI** | Dots per inch — the resolution a page is rendered at before OCR (300 here). |
| **Draw order** | The order a PDF paints characters in, which need not match reading order. |
| **exiftool** | External command-line tool that reads every metadata tag in a file. |
| **Exit code** | Number a program returns when it ends; scripts and CI read it as the verdict. |
| **Fail closed** | When unsure, report "cannot certify" rather than "clean" — silence never counts as success. |
| **Fixture** | A sample PDF generated for a test. |
| **Flattened** | A page converted to a picture, leaving no text data. |
| **Form-field value** | Data typed into a fillable form. |
| **Fused** | Separate pieces of text joined together (e.g. two table cells), which can accidentally form a digit sequence. |
| **Garbage-collected rewrite** | Saving a PDF so unreferenced (orphaned) objects are dropped. The fix for leftover-content leaks. |
| **Guard** | A check in the code that prevents a known bug. |
| **Hard finding** | A match on one genuinely contiguous piece of text; exits `1`. |
| **Incremental update** | Edits appended to the end of a file; earlier versions of objects stay inside it. |
| **Info / XMP** | The two places PDFs store metadata: the older Info dictionary and the newer XML block. |
| **Layout-aware** | Rebuilds lines from where characters sit on the page, not the order the file draws them. |
| **LLM** | Large language model — the AI the redactor uses to find names and addresses. |
| **Luhn** | Checksum built into credit-card numbers; random 16-digit strings usually fail it. |
| **Manual-review warning** | A possible match that needs a human to check it; exits `2`. |
| **Masked** | The report shows only the tail of a match (`****6789`) so it doesn't leak the secret itself. |
| **Mutation testing** | Deliberately breaking a guard to prove the tests notice. |
| **NANP** | North American Numbering Plan — the rules for valid US/Canada phone numbers. |
| **NFKC** | Unicode step that folds look-alike characters (fullwidth digits, fancy dashes) to plain ones without splitting accents. |
| **NFKD fold** | Unicode step turning look-alike characters into plain ones (fullwidth `１` → `1`, `é` → `e`). |
| **Normalized** | Text reduced to a canonical form before comparing, so formatting can't hide a match. |
| **Object streams / xref stream** | The PDF 1.5+ layout that packs many objects into one compressed container — the default for Acrobat and Chrome. |
| **OCR** | Optical character recognition: reading text from pixels. |
| **Octal coercion** | YAML gotcha: an unquoted `00123456` is read as a base-8 number, changing its value. |
| **Optional-content group** | A PDF "layer" that can be switched on or off; its name alone can leak data. |
| **ORPHANED** | Label for a finding in an object nothing references — present in the file, never displayed. |
| **Outlined vector text** | Letters converted into drawn shapes: looks like text, isn't text data. |
| **PyMuPDF** | Python library for reading and rendering PDFs; most layers use it. |
| **PyObjC** | Bridge that lets Python call macOS frameworks such as Apple Vision. |
| **qpdf** | External command-line tool that rewrites a PDF with everything decompressed. |
| **Regex** | A text-search pattern language. |
| **Rendered page** | The page drawn as an image — what a viewer sees. |
| **Sidecar** | A small companion file next to a PDF listing the secrets it contains. |
| **Substring search** | Checking whether one string appears exactly inside another. |
| **Surface** | One real piece of text: a line, a decoded string, a metadata value. |
| **UTF-16** | A text encoding PDFs use for non-ASCII strings; the Objects layer decodes it. |
| **Validator** | A check that a match is structurally real (e.g. SSNs never start with `000` or `666`). |
| **Walked structurally** | Reading objects one by one through the PDF's own structure rather than scanning raw bytes. |