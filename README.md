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
| `1` | **Leak** — a secret was detected. The report names where. Also the code for an internal crash that happened *after* a secret was already confirmed: a confirmed leak is never downgraded just because the scan could not finish. |
| `2` | **Cannot certify** — a layer could not run, a match needs manual review, the tool found content it cannot read, the rules ask for something this tool can't check, the PDF needs a password to open, or the tool crashed before confirming a leak. Never treat a `2` as clean. |

That last row is the whole point: the tool fails *closed*. If it could not
look somewhere — including because it crashed — it says so instead of
certifying the document clean; the severity order is `0 < 2 < 1`, so a
crash can only ever push the verdict *up* from a clean-so-far `2`, never
down from a confirmed `1`.

For scripts, add `--json report.json` to also write a machine-readable
report: the verdict, each finding (samples masked), and each warning with
a stable `code` (e.g. `LEFTOVER_IMAGE`) and `kind` — `review` (a possible
match to judge), `coverage` (something not fully read) or `scope` (the
rules ask for something the tool cannot check). Both carry structured
fields: the layer, where the content is stored (`live`, `orphaned`,
`unreferenced`, `superseded`), the object, revision and page when known,
and (only on `TOOL_EXIT_NONZERO`) the tool's own `returncode`, so a real
qpdf failure can be told apart from its benign, version-dependent exit 3
without parsing the message text. An internal crash is reported the same
way any other operational failure is — `error: {"code": "INTERNAL_ERROR",
"message": ...}` alongside whatever findings and warnings were already
recorded, message text never more than the exception's type plus a
truncated summary, never raw document content. The file is replaced
atomically and removed at the start of each run, so a run that dies
leaves no report rather than an old one — but the exit code stays the
authority. The format is experimental; `schema_version` changes if a
field is renamed or removed. `--version` (on its own) prints the tool
version.

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
`123 45 6789`, `１２３４５６７８９`, or the value continuing from one page
onto the next — but not `123-45-6788`.

**Pattern rules → regular expressions (Python `re`).** Built-in classes
and your own regexes run on the extracted text after NFKC normalization
(fullwidth digits become ASCII) and a fold of common Unicode dashes
(U+00AD soft hyphen, U+2010–2015, U+2043, U+2212) to `-` and Unicode
spaces to a plain space. Each
built-in class is a regex plus a validator that rejects matches which
cannot be real:

| Class | Regex finds | Validator rejects |
| --- | --- | --- |
| `ssn` | `NNN-NN-NNNN` (separator `-`, space or `.`, used consistently) or any bare 9-digit number | SSA-impossible numbers: area `000`, `666` or `9xx`; group `00`; serial `0000` |
| `credit-card` | card-number digit runs (13–19 digits) | numbers failing the Luhn checksum, and 14-digit PDF date stamps |
| `email` | `name@domain.tld` | image asset names like `logo@2x.png` |
| `us-phone` | 10-digit numbers, optional `+1` | numbers breaking NANP rules (area code and exchange must start 2–9) |
| `pattern` | your regex, as written | — |

**Two tiers.** A *hard* finding (exit `1`) is a match on one real surface:
one line of a genuine reading (see the Text and OCR layers), one decoded
PDF string, one form or annotation field. Known values are also hard when
split across the strings of one PDF object, or when they run from the
last line of one page onto the first line of the next. Anything else the
tool finds by joining text — lines or table columns within a page, a
vertical column of an ordinary page, any other join across a page break —
is a *manual-review* warning (exit `2`), so a coincidental run of digits
never hard-fails a clean document. Demotion never discards a match; what
the tool cannot see at all is listed under [Known gaps](#known-gaps).
Regex patterns are held to the stricter rule: hard only within one line
of a genuine reading or one decoded string. Reported samples are masked
(`****6789`).

## What it checks — six layers, most likely to find a leak first

A redactor can succeed on the page and still leave the data in the file.
[COVERAGE.md](COVERAGE.md) maps the known places a secret can be stored
against what reads each one. The layers are ordered by how common their failure mode is
in practice —
a judgment from well-known redaction failures and the cases this project
has seen, not measured statistics.

| # | Layer | Finds a secret that… | Method | Tools |
| --- | --- | --- | --- | --- |
| 1 | **Text** | is covered by a box but still selectable | characters read with positions, rebuilt into reading order, matched | PyMuPDF |
| 2 | **Objects** | the page no longer shows but the file still stores | every PDF object decoded and matched; unreferenced ones flagged ORPHANED | PyMuPDF + built-in PDF string parser |
| 3 | **Metadata** | sits in document properties | every metadata tag dumped and matched | exiftool, PyMuPDF |
| 4 | **Hidden** | no page renders at all | attachments, annotations, form fields, links, JavaScript, layer names read and matched | PyMuPDF |
| 5 | **OCR** | exists only as pixels | each page rendered to an image, text recognized, matched | PyMuPDF, Apple Vision |
| 6 | **Binary** | survives in the objects qpdf rebuilds — a cross-check for damaged files | reachable objects decompressed, searched for known values | qpdf |

### 1. Text — the page's text layer

The most common redaction mistake is drawing a black box over text that is
still there: invisible on screen, but selectable and copyable.

- **Method:** PyMuPDF `page.get_text("rawdict")` returns every character
  with its position and writing direction. The tool rebuilds lines itself
  — clustering characters by position (tolerance scales with font size)
  and sorting within each line — so text drawn out of order or one digit
  per form box still reads back as `123-45-6789`. A wide gap inside a line
  is treated as a column break, so neighbouring table cells never merge
  into a false number. Text placed outside the visible page area (the
  crop or media box) is read too, as its own reading — invisible to a
  viewer, but still in the file — and kept apart from the visible text so
  it cannot come between the halves of a value split across a page break.
- **Readings:** three *genuine* readings — horizontally written text read
  left to right, and text written vertically (rotated) read along its own
  direction both ways — plus two *reconstructed* readings of every glyph
  in vertical columns. The reconstructions are the only reading of a value
  written one character per line, but a column of an ordinary page also
  stacks unrelated lines (a numbered list's numbers), so they only ever
  give manual-review warnings.
- **Across pages:** each page break is checked on its own. A value running
  from the last line of one page onto the first line of the next, in a
  genuine reading, is hard. Any other match crossing the break — needing
  other lines, or across a blank or unreadable page — is a manual-review
  warning at the page it crosses into.
- **Tier:** a known value is hard on one line of a genuine reading, or at
  a page seam as above. A pattern is hard only on one line of a genuine
  reading. Everything that needs text joined is manual-review.

### 2. Objects — everything the file stores

A redaction tool can remove text from the page while the original stays in
the file as an object nothing uses any more. This layer caught every leak
in this project's real-document case.

- **Method:** walks every numbered PDF object with PyMuPDF (`xref_object`,
  `xref_stream`) and decodes its string tokens with a built-in parser that
  understands PDF syntax — `(literal)` strings with escapes and nested
  parentheses, `<hex>` strings — as latin-1, or UTF-16 when marked. Fonts
  and encodings are not applied, so text in a font whose codes are not
  plain characters — CID/Identity-H fonts (standard for Word, Chrome and
  embedded TrueType), custom `/Differences` encodings, subset fonts with
  sequential codes, Type3 fonts — cannot be read here. Stream bodies are
  skipped when their dictionary marks them as an image, font program,
  attachment, object stream or cross-reference stream, or when under 85%
  of their first 64 KB is printable; so an inline image making up more
  than about 15% of a content stream also hides the text around it. Every
  dictionary is always scanned. References are followed from the file's
  root; any object not reached is reported **ORPHANED** — left behind, not
  displayed.
- **Leftover content:** orphaned objects, and every object an incremental
  update rewrote (earlier revisions are found through the file's own
  cross-reference chain and rebuilt). Leftover XMP packets, attachment
  bodies and other leftover text that is not PDF syntax are searched as
  raw text. Leftover content the tool finds but cannot read — text in
  font codes that are not plain characters, images (including inline
  ones), containers such as zip or PDF — is **flagged** as a
  manual-review warning.
- **Tier:** a known value is hard anywhere in one object, even split
  across its strings; a pattern is hard only within one decoded string.

### 3. Metadata — document properties

Title, author, subject, keywords and custom fields often keep a name or
case number that was scrubbed from the page.

- **Method:** `exiftool -json` dumps the metadata tags. Known values are
  searched across the whole dump (tag names included); patterns run on
  each value separately. When the Info dictionary and XMP use the same tag
  name, exiftool reports only one of them — the Objects layer still reads
  the Info strings. Nine tags describing the local file are ignored
  (`SourceFile`, `ExifToolVersion`, `FileName`, `Directory`, `FileSize`,
  `FileModifyDate`, `FileAccessDate`, `FileInodeChangeDate`,
  `FilePermissions`), so the file's own path cannot match. The XMP block is
  also read in-process with PyMuPDF `get_xml_metadata()`.
- **Tier:** hard.

### 4. Hidden — content no page renders

- **Method:** PyMuPDF APIs read each hidden place directly: attachments
  (`embfile_*` — names, descriptions, and contents up to 16 MB decoded as
  UTF-8 text; an attachment that is not text — zip, Office, image, nested
  PDF, recognised by file signature — is flagged as not scanned),
  annotation text and attached files (`page.annots()`), form-field values
  and names (`page.widgets()`), link targets (`page.get_links()`),
  JavaScript in the catalog's `/OpenAction` and the top level of its named
  JavaScript tree, and optional-content group names (`get_ocgs()`).
  Scripts on links, form fields, pages or the catalog's other actions are
  not reported here; a script stored as a string is still caught by the
  Objects layer, and one stored as a stream by Binary (manual review).
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
  are still stored in the file. An image placed outside the visible page
  area is not rendered, so OCR does not see it either.

### 6. Binary — the raw decompressed file

A cross-check using a second PDF parser: qpdf can recover objects from a
damaged cross-reference table, and it decompresses attachment contents.

- **Method:** `qpdf --qdf --object-streams=disable` rewrites the objects
  reachable from the file's root, decompressing their streams (image
  codecs such as JPEG stay encoded); the output is streamed through the
  same normalized substring search, for value secrets only. Regex patterns
  are not run here — raw bytes produce too many coincidental matches.
  Unreferenced (orphaned) objects and superseded object versions are
  **not** in qpdf's output; the Objects layer covers those.
  Decompressed font programs *are* in it, and their tables contain byte
  runs such as `123456789`, so a sequential value can raise a coincidental
  Binary warning on a clean document.
- **Tier:** always manual-review, never a hard finding.

Each layer extracts text independently, so a blind spot in one extraction
cannot hide a leak from the others. What no tool can tell you is whether
your *rule set* was complete — this proves the redactor removed what you
named, not that you named everything.

### Known gaps

[COVERAGE.md](COVERAGE.md) is the full known map; its ✗ cells are where a
secret can still be **not detected** (exit `0`). In short:

- **Pixels the tool does not OCR:** under a box drawn over an image, off
  the page, in page or XMP thumbnails, in embedded files other than
  listed attachments.
- **Content no page shows:** font-coded text or images in a switched-off
  optional-content layer, a hidden annotation's appearance, or a page
  resource that is never drawn (plain text there *is* read).
- **Unindexed bytes:** anything after the final `%%EOF`, in comments, or in
  an object whose table entry is marked free.
- **Leftover text in a font that maps ordinary-looking codes to other
  glyphs**, and **containers encoded as text** (a base64 email part, an
  HTML `data:` URI).
- **Pattern rules** on embedded files other than attachments and on
  scripts stored as streams on links, fields or pages (known values are
  still found there, as manual review).
- **Split values:** a page-break split with a header, footer or page
  number between its halves, or where a page's last line is not its
  reading-order last line; a value wrapped inside one column of a
  multi-column page; dash-less pattern numbers wrapped at a line or page
  break; text at extreme coordinates.

Leftover images, leftover text in non-plain font codes, and attachments
that are not text are **flagged** (exit `2`) rather than read: a clean
document containing them cannot certify as `0` until the tool learns to
read them.

Known false positives: a short value such as a 5-digit ZIP can be
assembled as a hard finding at a page seam (a page number followed by the
next page's first line); a value assembled down a vertical column or
through several page-number-only pages is only ever manual review; and
decompressed font tables can raise coincidental Binary warnings (see
Binary). Prefer longer, more specific values.

A rewrite that closes these gaps by accounting for every byte of the
file, rather than searching the places this tool knows about, is planned
in [docs/REDESIGN.md](docs/REDESIGN.md); its Phase 1 design questions
are worked through in [docs/adr/](docs/adr/) -- most as proposals
awaiting owner approval, not settled decisions -- each backed by a
measurement in [eval/spikes/](eval/spikes/).

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
| `backend`, `model`, `llm_url`, `ollama_url`, `scrub_metadata` | ignored |
| any other key | warning (exit `2`) — a typo or a newer redactor key is surfaced, not skipped |

The other entity types — `person_name`, `address`, `date_of_birth`,
`account_number`, `drivers_license`, `passport` — are found by LLM
judgement upstream and have no regex equivalent here. They are **not
silently dropped**: the run warns it cannot verify them and exits `2`, so
a shared config never implies coverage it lacks. `phone` is only partly
verifiable (NANP numbers only) and says so; an entity type outside
the redactor's ten is a hard error, not a coverage gap.

Because unverifiable types always raise a warning, a config that lists
`phone`, any LLM-only type, or omits `entity_types` (which means all ten,
matching the redactor's default) can never exit `0`. A pattern with a
capturing group also warns (exit `2`): the redactor removes only the
captured part, so the rest of the match can survive redaction.

**Quote your values and patterns.** YAML reads an unquoted `00123456` in
`exact_values` as an octal number; the tool keeps the literal text and
warns (exit `2`), because the redactor reading the same file uses the
number instead. For `patterns` the tool also keeps the literal text (an
unquoted `0777` is searched as `0777`) but does not warn, although the
redactor will use `511`. A `#`
starts a YAML comment, so unquoted `Invoice #[0-9]{6}` becomes just
`Invoice`: the tool warns when a pattern looks truncated but still scans
the truncated rule, which can match ordinary words (exit `1`), and it does
not check `exact_values` for this at all.

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
pytest                                    # Run the test suite
ruff check .                              # Lint code
mypy                                      # Type check (eval/caselib, eval/scorecard)
pytest tests/ --cov=verify --cov=caselib  # Run tests with coverage report
```

Every regression test pins a previously confirmed bug; fixtures are
generated on the fly, nothing binary is committed, and tests needing
Apple Vision, `exiftool`, or `qpdf` skip with a reason when the tool is
absent. CI also runs ruff (correctness) and mypy (types, on the case
library), and reports coverage on the Linux Python 3.12 job; the commands
above run the same checks locally. Two suites go further and test
robustness directly:

- **`test_case_library.py`** judges every case in the case library
  ([eval/README.md](eval/README.md)): generated PDFs, each with the
  correct verdict and the story of how its redaction failed — real
  redaction-tool output, known gaps pinned at today's wrong answer, and
  parameter grids: each carrier surface in each on-disk layout real
  producers emit (classic xref, object streams, incremental updates,
  garbage-collected rewrites), and page-text layouts that split a value
  across lines, pages, form boxes, columns and rotations.
- **`test_mutation.py`** neutralizes each guard in turn and asserts the
  bug it prevents comes back — so a guard whose removal changes nothing
  is caught, not just an outright regression.

To exercise **real vendor PDFs** without committing binaries, drop a
`foo.pdf` plus a `foo.pdf.secrets.json` sidecar (a list of `[name, value]`
pairs) into `tests/corpus_pdfs/`; `test_corpus.py` picks them up
automatically.

Any change that can move a file's exit code (clean → uncertifiable, or
certified → leak) must be listed under **Verdict changes** in
[CHANGELOG.md](CHANGELOG.md).

The **scorecard** (`PYTHONPATH=eval:. python -m scorecard diff`) runs
this same case library through the real CLI as a subprocess and diffs it
against a pinned reference version, so every intended verdict change is
reviewed explicitly in `eval/accepted_diffs.yaml` — see
[eval/README.md](eval/README.md#the-scorecard-scorecard).

The **gallery** (`PYTHONPATH=eval:. python -m gallery build --out DIR`) is
a static HTML page of how each leak case's redaction actually fails —
generated from the same case library, illustrative only, and never
gating — see [eval/README.md](eval/README.md#gallery-gallery).

## Glossary

### What is inside a PDF

A PDF is a pile of numbered **objects**. A **cross-reference (xref)
table** at the end of the file records where each one lives. Some objects
are **dictionaries** — key–value records like `<< /Title (Quarterly Report) >>`
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
| **Genuine reading** | Page text read in the direction it was written (horizontal, rotated, or an OCR pass); its matches can be hard findings. |
| **Guard** | A check in the code that prevents a known bug. |
| **Hard finding** | A match strong enough to count as a confirmed leak (exit `1`) — see each layer's *Tier* for the exact rule. |
| **Incremental update** | Edits appended to the end of a file; earlier revisions stay inside it, and no layer reads them (see Known gaps). |
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
| **qpdf** | External command-line tool that rewrites a PDF's reachable objects with their streams decompressed. |
| **Reconstructed reading** | Every glyph stacked into vertical columns — catches a value written one character per line, but its matches are only ever manual review. |
| **Regex** | A text-search pattern language. |
| **Rendered page** | The page drawn as an image — what a viewer sees. |
| **Sidecar** | A small companion file next to a PDF listing the secrets it contains. |
| **Substring search** | Checking whether one string appears exactly inside another. |
| **Surface** | One real piece of text: a line, a decoded PDF string, a form or annotation field. |
| **UTF-16** | A text encoding PDFs use for non-ASCII strings; the Objects layer decodes it. |
| **Validator** | A check that a match is structurally real (e.g. SSNs never start with `000` or `666`). |
| **Walked structurally** | Reading objects one by one through the PDF's own structure rather than scanning raw bytes. |