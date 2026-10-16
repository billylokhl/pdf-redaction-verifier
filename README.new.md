# PDF Redaction Verifier

## What it does

You give it a PDF and a list of secrets that should have been removed from it.
It searches every place in the file where a secret can survive: the page text,
the picture of the page, the document properties, comments and form fields,
and data the file still stores but no page shows. It then gives one of three
results: **Clean**, **Secret found** or **Needs review**.

It is for anyone who redacts PDFs and wants to check the result before sharing
it. You don't need to know how PDFs work. It runs on macOS.

## Install

Pick one of three ways. If you're not sure, use the first. Type the commands
in Terminal, then do the check at the end.

### 1. Install as a command (recommended)

Use this to check your own files. You need [Homebrew](https://brew.sh).

```bash
brew install exiftool qpdf uv
git clone https://github.com/billylokhl/pdf-redaction-verifier.git
cd pdf-redaction-verifier
uv tool install --python 3.13 .
```

`exiftool` and `qpdf` are helper programs the checker runs. `uv` installs the
checker with its own copy of Python. Afterwards `pdf-verify` works in any
Terminal window. If it says `command not found`, run `uv tool update-shell`
and open a new window.

### 2. A local copy, for working on the project

```bash
brew install exiftool qpdf
curl -LsSf https://astral.sh/uv/0.12.3/install.sh | sh
```

Open a **new** Terminal window, then:

```bash
git clone https://github.com/billylokhl/pdf-redaction-verifier.git
cd pdf-redaction-verifier
uv sync --python 3.13
source .venv/bin/activate
```

`uv sync` needs uv 0.12 (0.12.3 or newer, not 0.13): the project pins uv's
version so everyone gets exactly the same packages, and Homebrew's uv may be
newer. (Route 1 doesn't check this.) In each new Terminal window, run
`source .venv/bin/activate` in this folder before using `pdf-verify`.

### 3. Conda

If you already use [conda](https://docs.conda.io) (Miniconda or Anaconda), it
can supply exiftool and qpdf too, so you don't need Homebrew or uv:

```bash
git clone https://github.com/billylokhl/pdf-redaction-verifier.git
cd pdf-redaction-verifier
conda env create -f environment.yml
conda activate pdf-redaction-verifier
```

Keep the folder: the checker runs from it. Run
`conda activate pdf-redaction-verifier` in each new Terminal window.

### Check that it works

In the `pdf-redaction-verifier` folder (and, for route 2 or 3, activated):

```bash
pdf-verify --target examples/ocr-check.pdf --secrets examples/ocr-check-secrets.json | grep 'STILL PRESENT'
```

This file holds a made-up SSN as a picture only, so only the OCR check can
find it. You should see one line, with `OCR` and
`page 1 (Apple Vision @ 300 dpi)` in it. If nothing prints, see
[Troubleshooting](#troubleshooting).

## Your first check

**1. List your secrets.** Save this as `secrets.json`:

```json
[
  {"name": "Applicant SSN", "value": "123-45-6789"}
]
```

The `name` is your own label. The report uses it instead of showing the secret.

**2. Run the check:**

```bash
pdf-verify --target letter.pdf --secrets secrets.json
```

**3. Read the result.** Here `letter.pdf` has a black box drawn over the SSN.
The box hides it on screen, but the text is still in the file:

```text
[*] Scanning letter.pdf (1 page(s)) for 1 secret(s) and 0 pattern rule(s)...
[*] Phase 2: Text layer (layout-aware visual text)...
[*] Phase 3: OCR layer (Apple Vision, correction on+off)...
[*] Phase 4: Metadata + binary-stream layers (exiftool / qpdf)...

======================================================================
  [FAIL]  SECRETS ARE STILL IN THIS FILE: letter.pdf
          Found: 1 of your 1 values.
          (2 findings -- one spot can be found by several checks.) If this is
          a redacted copy, the redaction did not remove them. Do not share
          this file until each one is removed or checked.
======================================================================
  ✖ STILL PRESENT | 'Applicant SSN'                      | Objects  | stored          | object 8
  ✖ STILL PRESENT | 'Applicant SSN'                      | Text     | on a page       | page 1 (visual text layer)
```

The real report goes on with a "What this means" section that explains each
line and the usual fix.

Every run ends with one of three results. Scripts can read it as the exit code
(the number a program returns when it finishes).

| Result | Exit code | The report opens with | What it means |
| --- | --- | --- | --- |
| **Clean** | `0` | `[PASS]` | None of your secrets was found, and every check ran. |
| **Secret found** | `1` | `[FAIL]` | At least one secret is still in the file. Don't share it. |
| **Needs review** | `2` | `[NOT CERTIFIED]` | No secret was confirmed, but something stopped the tool from saying Clean. The report lists why. |

**Never treat Needs review as clean.** The tool says Needs review whenever it
could not look somewhere, or found something only a person can judge. A typo in
the command, a missing file or a mistake in `secrets.json` also gives exit `2`.

Clean means only that the secrets *you listed* were not found. It does not
mean the file holds nothing else sensitive.

## Writing your secrets file

You can write it in either of two formats: this tool's own **JSON** file
(below), or a redaction tool's **YAML** config, so one file can drive both the
redaction and the check (see [Using a YAML config](#using-a-yaml-config)).

In JSON, the secrets file is a list. Each entry has a `name` and one of
`value`, `pattern` or `class`.

**Values.** A `value` is a secret you know, written out. Case, punctuation,
spacing and accents are already ignored: `123-45-6789` also finds `123 45 6789`,
and `Jordan Q. Testperson` also finds `JÖRDAN Q TESTPERSON`. Don't list those
variations. A value is also found inside longer text: `Jordan Testperson` is
found in `jordan.testperson@example.com`. So keep values specific; a bare first
name matches every word that contains it.

**List every written form.** The tool finds exactly what you list. It does not
guess reorderings, nicknames, initials or parts. `Jordan Q. Testperson` does
**not** find `TESTPERSON, JORDAN Q`. So list each form the document may use:

- **Names:** `Jordan Q. Testperson` and `Testperson, Jordan Q.`, each with and
  without the middle initial, plus any short form you want removed.
- **Email addresses and usernames:** the full address
  (`jordan.testperson@example.com`) and any login made from it (`jtestperson`).
- **Numbers:** only forms with different digits, such as an account number
  also printed with a branch code in front, or only its last four digits.

**Your own patterns.** A `pattern` finds anything of a given shape, written as
a regular expression (a text-search pattern):

```json
{"name": "Case number", "pattern": "CASE-[0-9]{6}"}
```

The report shows at most the last four characters of what a pattern matched.

**Built-in types.** A `class` looks for any SSN, card number, email address or
US phone number, not one you listed. Use `ssn`, `credit-card`, `email` or
`us-phone`:

```json
{"name": "Any SSN", "class": "ssn"}
```

By default their matches are for information only: they appear under "Not
counted in the verdict" and don't change the result. To make them count like
your own secrets, run with `--strict-classes`, or add `"strict": true` to that
entry. Strict mode reports matches on many clean files, because ordinary ID
numbers often look like phone numbers or SSNs.

### Using a YAML config

If your redaction tool keeps its settings in a YAML file, pass that file to
`--secrets` instead. Its name must end in `.yaml` or `.yml`:

```yaml
exact_values:
  - "Jordan Q. Testperson"
  - "Testperson, Jordan Q."
  - "123-45-6789"
patterns:
  - "CASE-[0-9]{6}"
entity_types:
  - ssn
  - person_name
```

```bash
pdf-verify --target letter.pdf --secrets redact_config.yaml
```

The checker reads `exact_values` as values and `patterns` as your own patterns,
exactly as in JSON. It does **not** check `entity_types`: those ask the
redaction tool to find *any* SSN or *any* name, which this checker can't judge.
They never change the result; the report says once that they weren't checked.
So list the specific names and numbers you care about under `exact_values`:
only those are verified.

Put every value and pattern in quotes. Without quotes, YAML can change them:
an unquoted `00123456` is read as a number, and a `#` starts a comment, so
`Invoice #[0-9]{6}` becomes just `Invoice`. The checker warns about these.
Other settings in the file, such as which AI model the redaction tool uses,
are ignored.

## Reading the report

**Each ✖ line** in the sample above says, from left to right: which of your
entries matched, which check found it, what kind of place it is in, and exactly
where. One secret often shows up on several lines, because several checks found
the same spot. The report never shows a listed value.

**Common reasons for Needs review**, with the line the report prints:

- **A helper program repaired the file.**
  `Binary: qpdf exited 3 (an unrecognised qpdf warning) — ...`
  The file is damaged, so one check may be incomplete. Save a fresh copy in
  your PDF editor, check it, and share the copy you checked.
- **Page thumbnails.**
  `Hidden: page 1 carries a thumbnail image (/Thumb, its own) that is NOT read or OCR'd ...`
  The file stores a small preview of the page, which may still show what was
  redacted. Remove embedded thumbnails in your PDF editor and check again.
- **A large page.**
  `OCR: page 1 not fully read (page too large for OCR: its 300 dpi render is 2550 x 4200 px, ...)`
  Pages longer than about 11.7 inches on a side (legal, A3, tabloid) can't be
  fully read as pictures. Anything found still counts, but such a file can't be
  Clean. Look at those pages yourself.
- **Leftover content it can't read.**
  `Objects: 1 ORPHANED object(s) draw text this tool cannot decode ... — NOT scanned; ...`
  The file still stores old content no page uses. Save it with your editor's
  option to remove unused objects ("optimize" or "garbage collection"), then
  check again.
- **A possible match that needs a person.**
  `Text: page 1 contains a cross-line sequence matching secret 'Applicant SSN' — ...`
  A secret appears only when text from different lines or places is joined.
  Look at that page and decide. `Binary: raw byte stream contains a sequence
  matching secret ...` is similar; font data contains runs like `123456789`,
  so a sequential value can match there by coincidence.
- **A helper program is missing.**
  `Metadata: exiftool not installed — layer NOT scanned`
  Install it (see [Install](#install)) and run the check again.

**Not counted in the verdict.** Built-in type matches are listed last, masked,
under `ℹ NOT COUNTED IN THE VERDICT`. They don't change the result: a file with
an SSN-shaped number that isn't one of your values can still be Clean. Check
them yourself, or use `--strict-classes` to count them.

## What it checks

| Check | Looks for a secret in |
| --- | --- |
| **Text** | the page's text, including text hidden under a box, coloured white or placed off the page |
| **Objects** | everything the file stores, including leftovers no page shows and older saved versions |
| **Metadata** | the document's properties, such as title, author and keywords |
| **Hidden** | comments, form fields, links, attachments, scripts and layer names |
| **OCR** | what the page looks like, read the way a person reads it, using macOS's text recognition |
| **Binary** | a second, separate read of the file's raw data, which also copes with damaged files |

## What it can't catch yet

These can still give a Clean result although a secret is in the file:

- An image with a box drawn over it. The image is still stored under the box,
  but the picture check only sees the box.
- Pictures it doesn't read: images placed off the page, previews stored in
  metadata or inside a photo, and small print in very detailed images.
- Forms of a secret you didn't list, such as a reordered name, or a first name
  inside an email address when you listed only the full name.
- Some values broken up unusually, for example into many pieces. Many give
  Needs review; some give nothing.
- Data in rarely used corners of the file, such as after its end marker.

The tool proves that what you listed is gone. It can't tell you whether you
listed everything. [COVERAGE.md](COVERAGE.md) is the full list of what is
read, what is only flagged, and what is missed.

## Options

| Option | What it does |
| --- | --- |
| `--target FILE` | The PDF to check. Required. |
| `--secrets FILE` | Your secrets file (JSON, or a redaction tool's `.yaml` config). Required. |
| `--json FILE` | Also write the report to `FILE` in a machine-readable form. The result is the same, except that a Clean result becomes Needs review if `FILE` can't be written. |
| `--visible-map OUT.pdf` | Also write a PDF that boxes where each finding is on the page, with secrets masked. It never changes the result. It only replaces a file it wrote itself; if `OUT.pdf` can't be written safely, it stops before checking, with Needs review. |
| `--strict-classes` | Count built-in type matches in the result, like your own secrets. |
| `--fail-fast` | Stop at the first secret found. Faster, but the report may not list everything. |
| `--version` | Print the version. Use it on its own. |
| `-h`, `--help` | List all options. |

## Troubleshooting

- **`exiftool not installed` or `qpdf not installed`.** Run
  `brew install exiftool qpdf`. Until both are installed, no run can be
  Clean.
- **`error: Required uv version ... does not match the running version ...`.**
  Only `uv sync` (route 2) checks this. Install uv 0.12.3 as shown there, or
  use route 1. If `uv --version` still shows another version, remove the other
  copy (for example `brew uninstall uv`).
- **`[ERROR] PDF is password-protected`.** The tool can't open files that need
  a password, and has no option to supply one. The result is Needs review. A
  file that opens without a password but restricts printing or copying is
  checked normally.
- **The OCR check prints nothing, or every page says
  `OCR: page N failed (Apple Vision request failed ...)`.** The picture check
  could not run, so no result can be Clean. OCR uses macOS's built-in text
  recognition and needs no permission prompts. Run the check from the
  Terminal app itself, not from a tool that restricts what programs may do.
  If it still fails, try route 1.
- **`pdf-verify` runs an old copy.** `which -a pdf-verify` lists every copy;
  the first one runs. Remove the ones you don't use.

## Learn more

- **How it works** (Part 2, to come): the checks, matching, and why the tool
  would rather say Needs review than Clean.
- [COVERAGE.md](COVERAGE.md): every known hiding place, and what is done about it.
- [docs/reference.md](docs/reference.md) (to come): exact matching rules, the
  JSON report and every warning code.
- [CONTRIBUTING.md](CONTRIBUTING.md) (to come): development setup and tests.
- [DESIGN.md](DESIGN.md) and [CHANGELOG.md](CHANGELOG.md): design and changes.
