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
[*] Phase 3b: images painted over (images-only OCR)...
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
`Invoice #[0-9]{6}` becomes just `Invoice`. The checker warns about some of
these mistakes, but not all, so quoting is the safe habit.
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

- Some pictures with something drawn over them. A box over a picture is
  flagged, but not, for example, several small boxes, or one picture drawn
  over another: the picture check only sees what is on top.
- Pictures it doesn't read: images placed off the page, previews stored in
  metadata or inside a photo, and small print in very detailed images.
- Forms of a secret you didn't list, such as a reordered name, or a first name
  inside an email address when you listed only the full name.
- Some values broken up unusually, for example into many pieces. Many give
  Needs review; some give nothing.
- Data in rarely used corners of the file, such as after its end marker.

The tool proves that what you listed is gone. It can't tell you whether you
listed everything. [COVERAGE.md](COVERAGE.md) is the full list of what is
read, what is only flagged, and what is missed; its
[Known gaps](COVERAGE.md#known-gaps) section lists every known miss.

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

## How it works

The sections above show how to use the tool. This part explains the ideas behind it,
so you can judge what a result does and doesn't promise. The exact rules
are in [docs/reference.md](docs/reference.md).

### Why "Needs review" exists

A checker can fail in two ways. It can raise a false alarm, which costs
you a few minutes. Or it can say Clean when a secret is still in the
file, which can cost far more, because you then share the file. This tool
is built so the second kind of failure is as rare as it can make it.

So **Clean is a claim the tool has to earn.** It says Clean only when
every check ran to the end and found none of your secrets. Not finding
something is only evidence if the tool actually looked. When it couldn't
look somewhere, it doesn't stay silent: it says Needs review and tells
you why. A helper program that is missing, a page the picture check
couldn't finish, a part of the file it can find but can't read: each one
means "I can't vouch for this file", not "this file is fine".

This is what "fails closed" means: when in doubt, the tool chooses the
answer that keeps you careful. The order of the three results reflects
that. Secret found always wins: once a secret is confirmed, nothing that
happens later can turn the result into Needs review or Clean, not even
the tool crashing. A crash before anything is confirmed gives Needs
review, never Clean.

The price is that some files that are in fact clean get Needs review.
The report says exactly why, so you can look at those spots yourself, or
fix the file (for example, by saving a fresh copy) and check again.

### Where secrets hide in a PDF

A PDF looks like pages, but inside it's a collection of numbered parts:
pieces of text, pictures, fonts, settings, and drawing instructions that
say "put this text here, draw that picture there". A page shows only what
its drawing instructions use. Everything else stays in the file, unseen.

That gap is where most redaction failures live. A secret can survive:

- **under a box.** The text is still on the page; a black rectangle is
  just drawn on top. Copy and paste, or search, still finds it.
- **in leftovers.** The page no longer uses the old text, but the file
  still stores it. Editors often save changes by adding to the end of the
  file, so an earlier version of the page can stay inside it too.
- **in the document's properties.** Title, author, subject and keywords
  often keep a name or case number that was removed from the page.
- **in parts no page draws.** Comments, form fields, link addresses,
  attachments, scripts and the names of layers.
- **in a picture.** A scanned page has no text at all, only pixels, so a
  text search can't see it.

Each of the six checks reads one kind of place, in its own way:

| Check | Where it looks | How |
| --- | --- | --- |
| **Text** | the text on each page, including covered, white or off-page text | rebuilds lines from where each character sits |
| **Objects** | every part the file stores, used or not, and earlier versions | decodes the text in each part |
| **Metadata** | the document's properties | reads every property with exiftool |
| **Hidden** | comments, form fields, links, attachments, scripts, layer names | reads each one directly |
| **OCR** | what each page looks like | draws the page as a picture and reads it with macOS's text recognition |
| **Binary** | the file's raw data, as a second program sees it | qpdf unpacks the file and the tool searches the result |

The checks overlap on purpose. Each one reads the file independently, so
a blind spot in one is often covered by another. That's why one secret
often appears on several lines of the report.

### How matching works

The tool doesn't guess, and it uses no AI. It compares text exactly, so
on the same Mac the same file and secrets give the same result.

**Values are compared after tidying both sides.** Before comparing, the
tool lowercases the text and your value, removes accents, turns
look-alike characters (such as full-width digits) into plain ones, and
drops everything that is not a letter or a digit. Then it looks for your
value inside the text. That is why `123-45-6789` also finds
`123 45 6789`, and why a name is found inside an email address. It is
also why the tool never finds a form you didn't list: a reordered name
is a different sequence of letters.

**Patterns describe a shape.** Your own patterns are regular expressions,
run on the text with only light tidying (look-alike digits, dashes and
spaces made plain), so the shape you wrote still holds. The built-in
types (SSN, card number, email, US phone) are patterns too, each with an
extra test that rejects matches that can't be real, such as a card number
that fails its checksum. By default their matches are shown but not
counted, because ordinary ID numbers often have the same shape.

**Not every match is equally strong.** The tool sorts what it finds into
two kinds:

- **A finding** (Secret found) is your value in one real piece of text:
  one line on a page, one stored string, one form field. A value you
  listed that continues from the end of one page onto the start of the
  next counts too.
- **A possible match** (Needs review) is one the tool only gets by
  joining text that may not belong together: lines from different parts
  of a page, table columns, characters stacked down a page, or raw data
  where digit runs occur by chance. A coincidence there is easy, so the
  tool asks a person instead of failing a clean file.

A possible match is never thrown away; it just doesn't count as proof.

**The report hides your secrets.** It names each match by your label,
never by the value. For a pattern match it shows at most the last few
characters, and none at all if they would reveal part of a value you
listed. Text read from pictures is always fully hidden, because
recognition can misread one character as another.

### Why some things are only flagged

Sometimes the tool can find a part of the file but can't read it: a
stored picture no page shows, text in a leftover part whose font uses
private codes instead of letters, an attachment that is a zip or an
Office file, a small preview picture of a page, or a picture with a box
drawn over it. A secret could be there, and the tool has no way to
check.

Guessing either way would be wrong. Calling it a finding would fail many
clean files; ignoring it would let a real secret through. So the tool
**flags** it: the result is Needs review, and the report says what it
found and where. The same goes for a damaged file that a helper program
had to repair, since part of what it read may be incomplete.

Flags cost certainty: a clean file that merely contains such a part can't
be Clean. The usual fix is to remove the part (save without unused
objects, remove thumbnails, flatten the page) and check again. Each flag
goes away once the tool learns to read that kind of content.

### What it can't catch, and why

**It can only look for what you list.** The tool proves that the values
and patterns you gave it are gone. It can't know about a secret you didn't
list, or a form of it you didn't think of. This is the most important
limit, and no tool can remove it for you.

**Some places aren't read yet.** The tool searches the places it knows a
secret can be. A few are still out of reach: pictures it doesn't render
(a preview stored inside a photo, an image placed off the page), some
content that only appears when a hidden layer is switched on, and bytes
outside the file's own structure. A secret in one of those can still give Clean.

**Text recognition can miss text without saying so.** When macOS reads a
page and returns fewer lines than the page holds, nothing tells the tool
a line is missing. It flags the situations where it knows recognition is
unreliable, such as very large pages, but it can't catch every silent
miss.

**A value broken up unusually may not be put back together.** The tool
rejoins a value split across lines or pages in the common ways, but not
every way; for instance, not one split into many small pieces.

[COVERAGE.md](COVERAGE.md) is the full, honest list: every known place a
secret can be stored, what reads it, what is only flagged, and the
[Known gaps](COVERAGE.md#known-gaps) where a secret can still be missed.
A place missing from it means "not yet known", never "safe".

### What the visible map shows

The report says *whether* a secret is in the file. `--visible-map` adds a
second PDF that shows *where*, so you can see what a reader would see.

It starts with a summary page, then has one page for each page of your
file that has something to mark, at the same size, with a box over each
place and a numbered list saying what each box is. The style of a box says how a reader would come across it:

- **solid:** on the page itself;
- **hatched:** a form field whose stored value differs from what it
  shows, which some viewers display;
- **dashed:** only on click, hover or scroll, such as a comment's text
  or a link's address;
- **dotted:** only when printed.

Anything that can't be boxed is listed on the summary page instead: text
found by OCR, content outside the visible edge of the page, document
properties, attachment and layer names, and content stored in the file
but not on any page. Nothing is dropped. A headline at the top sums it up,
from "visible secrets found" to "nothing visible found".

The map never shows a secret: values are masked the same way as in the
report, and the finished map is checked for your values before it is
saved. It also never changes the result. It's built after the report, as
a separate step, and if anything goes wrong you get no map, never a
different verdict. (The one exception comes before checking starts: if
the map's file name is taken by something that isn't one of its own
maps, the tool stops there with Needs review rather than overwrite it.) To keep your files safe, it only ever creates a new
file or replaces a map it wrote itself.

The map draws what the checks found, not what the eye sees: a solid box
means the text is on the page, whether or not something covers it.

Details of every list and headline are in
[docs/reference.md](docs/reference.md#the-visible-map).

## Learn more

- [How it works](#how-it-works) (above): the checks, matching, and why the
  tool would rather say Needs review than Clean.
- [COVERAGE.md](COVERAGE.md): every known hiding place, and what is done
  about it.
- [docs/reference.md](docs/reference.md): exact matching rules, the JSON
  report and every warning code, the visible map in detail, and a glossary.
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and tests.
- [DESIGN.md](DESIGN.md) and [CHANGELOG.md](CHANGELOG.md): design and changes.
