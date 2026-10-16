# Reference

Exact rules and formats, for lookup. The [README](../README.md) explains
how to use the tool and how it works in plain terms; this page holds the
detail behind it. Unfamiliar terms are in the [Glossary](#glossary).

- [Results and exit codes](#results-and-exit-codes)
- [The terminal report](#the-terminal-report)
- [Matching rules](#matching-rules): [values](#values),
  [patterns and built-in classes](#patterns-and-built-in-classes),
  [notices and strict mode](#notices-and-strict-mode), [tiers](#tiers),
  [masking](#masking)
- [Rules files](#rules-files): [JSON](#json),
  [shared YAML config](#shared-yaml-config),
  [quoting pitfalls](#quoting-pitfalls)
- [The JSON report](#the-json-report): [fields](#fields),
  [warning codes](#warning-codes), [error codes](#error-codes),
  [`schema_version`](#schema_version)
- [The six layers](#the-six-layers)
- [The visible map](#the-visible-map)
- [Requirements and running from source](#requirements-and-running-from-source)
- [Glossary](#glossary)

What the tool cannot see is listed in
[COVERAGE.md's Known gaps](../COVERAGE.md#known-gaps).

## Results and exit codes

| Code | Result | Meaning |
| --- | --- | --- |
| `0` | **Clean** | No secret found, and every layer actually ran. |
| `1` | **Secret found** | A secret was detected. The report names where. Also the code for an internal crash that happened *after* a secret was already confirmed: a confirmed leak is never downgraded just because the scan could not finish. |
| `2` | **Needs review** | A layer could not run, a match needs manual review, the tool found content it cannot read, the rules ask for something this tool can't check, the PDF needs a password to open, the command line or rules file is invalid, or the tool crashed before confirming a leak. Never treat a `2` as clean. |

The tool fails *closed*. If it could not look somewhere, including
because it crashed, it says so instead of certifying the document clean.
The severity order is `0 < 2 < 1`, so a crash can only ever push the
verdict *up* from a clean-so-far `2`, never down from a confirmed `1`.

With `--json`, a run whose verdict is `0` becomes `2` if the JSON report
cannot be written. `--fail-fast` stops at the first confirmed finding
(exit `1`); the layers it skips are listed as `SKIPPED_FAIL_FAST`, so the
report may not list every place the secret is.

Exit `0` means that none of the values you listed, and none of your own
`pattern` rules, was found in any layer the tool read, and that nothing
stopped it reading a layer. By default it says nothing about other SSN-,
card-, email- or phone-shaped values in the file; built-in class matches
are [notices](#notices-and-strict-mode).

## The terminal report

The report printed on the terminal says the same thing as the exit code,
in plain words.

- A leak opens with **`[FAIL]  SECRETS ARE STILL IN THIS FILE`** and how
  many of your rules matched. A run that found nothing but cannot certify
  (exit `2`) opens with **`[NOT CERTIFIED]`** and says why, including
  when the `--json` report could not be written. Only exit `0` prints
  `[PASS]`.
- Each finding is one line,
  `✖ STILL PRESENT | rule | layer | where | location`, meaning that text
  matching that rule is still in the file, so a redaction did not remove
  it.
- **The rule** is named by the `name` you gave it in a JSON rules file
  (`'Applicant SSN'`), or by its place in a shared YAML config
  (`exact value #2 (exact_values[1])`, `pattern #5 (patterns[4])`). An
  exact value's characters are never shown; a pattern's match at most its
  last four (`****5916`). See [Masking](#masking).
- **Where** says what kind of problem it is: `on a page` (the page's text
  or what it shows), `stored` (in the file's objects), `left over` (no
  page shows it but the file still stores it; see ORPHANED in the
  [Glossary](#glossary); `left over?` when that could not be confirmed),
  `earlier version` (an older save inside the file), `properties`
  (metadata), or the hidden part it is in (`comment or note`,
  `form field`, `link`, `attachment`, `script`, `layer name`).
- A **"What this means"** section after the findings explains, once each,
  every kind that occurred, with the usual fix.
- Built-in class matches that do not count are listed last, masked, under
  **`ℹ NOT COUNTED IN THE VERDICT`**.

`--version` (on its own) prints the tool version.

## Matching rules

There is no AI or fuzzy matching, only exact comparison and regular
expressions, so every finding is reproducible.

### Values

**Value rules use a normalized exact substring search.** Both the
extracted text and each secret are normalized with Python's
`unicodedata`: NFKD decomposition (fullwidth `１` → `1`), accents
stripped, casefolded, then every character that is not a letter or digit
removed. The tool then looks for each normalized secret as an exact
substring, using one compiled Python `re` pattern built from all the
secrets. So `123-45-6789` matches `123 45 6789`, `１２３４５６７８９`, or
the value continuing from one page onto the next, but not `123-45-6788`.

A listed value is therefore also found inside longer text: a listed
"Jordan Testperson" is found within `jordan.testperson@example.com`,
because the address reads "jordantestpersonexamplecom" once normalized. A
bare first name is found in every word that contains it, and across word
boundaries. The tool generates no reorderings, nicknames, initials or
partial forms; list each written form as its own value (see the README's
[Writing your secrets file](../README.md#writing-your-secrets-file)).
Forms that differ only in case, punctuation, spacing, accents or
fullwidth characters are one value to the tool and need no entry.

### Patterns and built-in classes

**Pattern rules are regular expressions (Python `re`).** Built-in classes
and your own regexes run on the extracted text after NFKC normalization
(fullwidth digits become ASCII) and a fold of common Unicode dashes
(U+00AD soft hyphen, U+2010–2015, U+2043, U+2212) to `-` and Unicode
spaces to a plain space. Each built-in class is a regex plus a validator
that rejects matches which cannot be real:

| Class | Regex finds | Validator rejects |
| --- | --- | --- |
| `ssn` | `NNN-NN-NNNN` (separator `-`, space or `.`, used consistently) or any bare 9-digit number | SSA-impossible numbers: area `000`, `666` or `9xx`; group `00`; serial `0000` |
| `credit-card` | card-number digit runs (13–19 digits) | numbers failing the Luhn checksum, and 14-digit PDF date stamps |
| `email` | `name@domain.tld` | image asset names like `logo@2x.png` |
| `us-phone` | 10-digit numbers, optional `+1` | numbers breaking NANP rules (area code and exchange must start 2–9) |
| `pattern` | your regex, as written | — |

A JSON rules file's `pattern` is compiled with `re.MULTILINE`, so `^` and
`$` match per line of extracted text. A shared YAML config's `patterns`
are compiled with `re.IGNORECASE` only, the same flags the redaction tool
uses.

### Notices and strict mode

**Built-in classes** (`ssn`, `credit-card`, `email`, `us-phone`) match any
value of that shape, not a secret you listed. By default their matches
are reported in a separate section after the verdict, "Not counted in
the verdict", and in the JSON report's [`notices`](#fields) array, and
they never change the exit code.

If you rely on the classes as a net for secrets you did not list, use
strict mode (`--strict-classes`, or `"strict": true` on a class rule):
class matches then count like any rule, exactly as before
[ADR 0012](adr/0012-pattern-classes-inform.md). A `"strict"` on a class
rule that is not `true` or `false` is a rules error (exit `2`) unless
`--strict-classes` is given. On a `value` or `pattern` rule the key is
ignored, as any unknown key is.

Strict mode raises hard findings on many clean files: on 488 real
text-bearing PDFs with no secret in them, `ssn` or `us-phone` did on
28.3%, mostly digit runs inside hex or alphanumeric ids
([eval/spikes/RESULTS.md](../eval/spikes/RESULTS.md), "Phase 5
baseline").

### Tiers

A *hard* finding (exit `1`) is a match on one real surface: one line of a
genuine reading (see the [Text](#text-layer) and [OCR](#ocr-layer)
layers), one decoded PDF string, one form or annotation field. Known
values are also hard when split across the strings of one PDF object, or
when they run from the last line of one page onto the first line of the
next. Anything else the tool finds by joining text (lines or table
columns within a page, a vertical column of an ordinary page, any other
join across a page break) is a *manual-review* warning (exit `2`), so a
coincidental run of digits never hard-fails a clean document. Demotion
never discards a match; what the tool cannot see at all is listed under
[Known gaps](../COVERAGE.md#known-gaps).

Regex patterns are held to the stricter rule: hard only within one line of
a genuine reading or one decoded string. Pattern rules therefore cannot
span pages as hard findings; they surface as review warnings. Each layer's
own tier is under [The six layers](#the-six-layers).

### Masking

Reported samples are masked (`****6789`). When a sample contains one of
your exact values, or any character it would show is a character of one,
only `****` is shown, so a report never shows any character of an exact
value (compared ignoring case, accents and punctuation, for letters and
digits of any script). The check is per character, not per position,
because the search can assemble a value from pieces across lines or down
a column; with numeric exact values, most numeric samples therefore show
only `****`.

Characters that merely look like one another (a Greek or Cyrillic letter
for a Latin one) are not recognised as equal, so a sample read by OCR,
where such misreadings happen, is always fully masked (`****`), in the
report, `--json` and warnings.

## Rules files

`--secrets` accepts either this tool's JSON array or a redaction tool's
`redact_config.yaml` (selected by the `.yaml`/`.yml` suffix), so one file
can drive both redaction and verification without the two drifting apart.
A rules file that cannot be read, or that defines no value or pattern to
check, is an error (`RULES_INVALID`, exit `2`).

### JSON

Each entry has a `name` (unique within the file) and exactly one of:

- `"value"`: a known secret string, matched through
  [normalization](#values).
- `"pattern"`: a custom regex (`re.MULTILINE`).
- `"class"`: a built-in class: `ssn`, `credit-card`, `email`, `us-phone`.
  Optional `"strict": true` keeps this class's matches in the verdict
  (see [Notices and strict mode](#notices-and-strict-mode)).

Unknown keys are ignored.

### Shared YAML config

The verifier checks only what the config defines explicitly: exact values
and patterns.

| YAML key | Becomes |
| --- | --- |
| `exact_values` | value rules (normalized matching) |
| `patterns` | pattern rules, compiled `IGNORECASE`, the same flags the redaction tool uses |
| `entity_types` | **not checked** (see below) |
| `backend`, `model`, `llm_url`, `ollama_url`, `scrub_metadata` | ignored |
| any other key | warning (`RULES_UNKNOWN_KEY`, exit `2`): a typo or a newer key is surfaced, not skipped |

**`entity_types` are not checked** (owner decision, 2026-10-16). They ask
the redaction tool to find *any* SSN, name, address and so on; this tool
proves that the values and patterns you listed are gone, and does not
judge whole categories. They produce no rule, no match, no notice and no
warning, and never change the result; `--strict-classes` does not change
that. The report says once which entity types were not checked. A config
that leaves out `entity_types` is read by the redaction tool as all ten
types, and the report says so. An entry that is not one of the redaction
tool's ten type names (`person_name`, `ssn`, `email`, `phone`, `address`,
`date_of_birth`, `account_number`, `credit_card`, `drivers_license`,
`passport`) is a rules error (exit `2`), and the entry is not echoed,
since a value pasted under the wrong key may be a secret. A config with
no `exact_values` and no `patterns` is a rules error too: there is nothing
to verify. To have specific names and numbers checked, list them under
`exact_values`.

A pattern with a capturing group warns (`RULES_CAPTURING_GROUP`, exit
`2`): the redaction tool removes only the captured part, so the rest of
the match can survive redaction.

### Quoting pitfalls

**Quote your values and patterns.** YAML reads some unquoted scalars as
other types: an unquoted `00123456` in `exact_values` is an octal number,
`yes` a boolean, `2026-10-16` a date. The tool keeps the literal text and
warns (`RULES_UNQUOTED_VALUE`, exit `2`), because a redaction tool reading
the same file uses the converted value instead. The warning names only
the type, never the value. For `patterns` the tool also keeps the literal
text (an unquoted `0777` is searched as `0777`) but does not warn,
although the redaction tool will use `511`.

A `#` starts a YAML comment, so an unquoted `Invoice #[0-9]{6}` becomes
just `Invoice`. The tool warns (`RULES_TRUNCATED_PATTERN`, exit `2`) when
a pattern looks truncated, but still scans the truncated rule, which can
match ordinary words (exit `1`). It does not check `exact_values` for
this at all.

## The JSON report

`--json FILE` also writes a machine-readable report. The format is
experimental. The exit code stays the authority: the file is removed at
the start of each run and replaced atomically (written beside it, then
renamed; private, mode `0600`), so a run that dies leaves no report
rather than an old one. `--json` must not name the target PDF or the
rules file (`JSON_PATH_CONFLICT`).

### Fields

Every field is always present (null when it does not apply), and lists
are sorted, so the output is identical across runs. Paths given on the
command line appear only as base names.

| Field | Meaning |
| --- | --- |
| `schema_version` | see [below](#schema_version) |
| `tool` | `name` and `version` |
| `environment` | `python`, `platform`, `pymupdf` and `ocr_available` |
| `target` | the PDF's file name |
| `exit_code`, `verdict` | `0`/`pass`, `1`/`fail` or `2`/`uncertified` |
| `error` | `null`, or `{"code": ..., "message": ...}` for a run that stopped (see [Error codes](#error-codes)) |
| `findings` | hard findings |
| `warnings` | everything that blocks a Clean result |
| `notices` | built-in class matches that do not count |

**A finding** has `layer`, `rule`, `tier` (`hard`), `storage`, `object`,
`revision`, `page`, `location` and a masked `sample`.

**A warning** has a stable `code` (e.g. `LEFTOVER_IMAGE`), a `kind`, the
`layer`, structured fields (`storage`, `rule`, `adjacency`, `tool`,
`page`, `object`, `revision`, `returncode`) and the `message`. `storage`
says where the content is stored: `live`, `orphaned`, `unreferenced` (not
reached by a reachability walk that is not trusted) or `superseded`.
`adjacency` says how a review match's text was assembled: `JOINED_LINES`,
`JOINED_PAGES`, `JOINED_LITERALS` or `NOISY_SOURCE`. `returncode` (only
on `TOOL_EXIT_NONZERO`) is the tool's own exit status, so a real qpdf
failure can be told apart from its exit 3 without parsing the message
text. (An exit 3 whose every warning checks out as harmless gives no
warning at all; see the [Binary layer](#binary-layer).) A warning without
a code would appear as `UNCODED`, kind `coverage`.

**A notice** (`kind: "class-match"`) has every field a finding or a
warning has, null when it does not apply; `tier` `hard` or `review`; and
a review warning's own kind as `warning_kind`.

An internal crash is reported the way any other operational failure is,
as `error: {"code": "INTERNAL_ERROR", "message": ...}` alongside whatever
findings and warnings were already recorded. The message text is never
more than the exception's type plus a truncated summary, never raw
document content.

### Warning codes

A warning's `kind` says why it blocks certification: `review` (a possible
match a human must judge), `coverage` (something not fully read) or
`scope` (the rules ask for something the tool cannot check). Every
warning makes the run exit `2` unless a finding makes it `1`.

| Code | Kind | Raised when |
| --- | --- | --- |
| `REVIEW_CROSS_LINE` | review | a value appears only when lines or columns of a page are joined |
| `REVIEW_CROSS_PAGE` | review | a value appears only across a page break, other than a hard page seam |
| `REVIEW_FUSED_PATTERN` | review | a pattern matches only when lines, columns or pages are joined |
| `REVIEW_HIDDEN_TEXT` | review | a match in an attachment's contents or other arbitrary hidden text |
| `REVIEW_METADATA_RAW` | review | exiftool's output was not valid JSON, and a pattern matches its raw text (which includes file-system fields) |
| `REVIEW_OBJECT_TEXT` | review | a match in an object's raw text (untyped streams, leftover payloads, trailer comments, a hex `/ID`) |
| `REVIEW_ADJACENT_LITERALS` | review | adjacent string literals in one object join into a pattern match |
| `REVIEW_BINARY` | review | the Binary layer's raw byte search found a value |
| `PAGE_FAILED` | coverage | OCR could not fully read a page (error, missing reading, stall, or a page too large) |
| `LAYER_CRASHED` | coverage | a layer raised an error |
| `OCR_UNAVAILABLE` | coverage | the Apple Vision bridge is not available |
| `SKIPPED_FAIL_FAST` | coverage | `--fail-fast` skipped a layer after a finding |
| `EMPTY_DOCUMENT` | coverage | the PDF has no pages |
| `TOOL_MISSING` | coverage | exiftool or qpdf is not installed |
| `TOOL_START_FAILED` | coverage | exiftool or qpdf could not start |
| `TOOL_TIMEOUT` | coverage | exiftool or qpdf timed out |
| `TOOL_EXIT_NONZERO` | coverage | exiftool or qpdf exited with an error (with `returncode`) |
| `TOOL_NO_OUTPUT` | coverage | exiftool or qpdf produced no output |
| `TOOL_OUTPUT_MALFORMED` | coverage | exiftool's output could not all be read |
| `XMP_UNREADABLE` | coverage | the XMP packet could not be read |
| `XREF_UNREADABLE` | coverage | the cross-reference table could not be read |
| `TRAILER_UNREADABLE` | coverage | the trailers could not be read |
| `OBJECT_UNREADABLE` | coverage | some objects could not be read |
| `UNTERMINATED_STRING` | coverage | an object held an unterminated string, so content after it was not read |
| `LEFTOVER_UNDECODABLE_TEXT` | coverage | leftover content draws text in font codes that are not plain characters |
| `LEFTOVER_IMAGE` | coverage | a leftover image (stored images are not OCR'd) |
| `LEFTOVER_CONTAINER` | coverage | a leftover payload that is not text (zip, Office, image, ...) |
| `PAYLOAD_TRUNCATED` | coverage | a payload over 16 MB was read only in part |
| `REVISION_SCAN_FAILED` | coverage | earlier revisions could not be scanned |
| `REVISION_UNREADABLE` | coverage | one earlier revision could not be read cleanly |
| `REVISION_CAP` | coverage | more than 50 revisions; only the original and the latest 49 were read |
| `HIDDEN_ITEM_FAILED` | coverage | a hidden item (attachment index, annotation, field, ...) could not be read |
| `ATTACHMENT_TOO_LARGE` | coverage | an attachment declares more than 16 MB |
| `ATTACHMENT_EMPTY` | coverage | an attachment declares content but decodes empty |
| `ATTACHMENT_NOT_TEXT` | coverage | an attachment is not text (zip, Office, image, nested PDF, ...) |
| `PAGE_THUMBNAIL` | coverage | a page carries a `/Thumb` preview image, which is not read |
| `IMAGE_COVERED` | coverage | an image is covered by an opaque fill drawn after it |
| `RULES_UNKNOWN_KEY` | scope | a YAML config has a key this tool does not know |
| `RULES_UNQUOTED_VALUE` | scope | YAML converted an unquoted exact value to another type |
| `RULES_TRUNCATED_PATTERN` | scope | a YAML pattern looks cut short by an unquoted `#` |
| `RULES_CAPTURING_GROUP` | scope | a YAML pattern has a capturing group |

The authoritative list is `WARNING_CODES` in `redaction_verifier/model.py`.

### Error codes

`error` is set when a run stops before or during the scan. All exit `2`,
except `INTERNAL_ERROR` after a confirmed finding, which stays `1`.

| Code | When |
| --- | --- |
| `RULES_INVALID` | the rules file cannot be read, is malformed, or defines nothing to check |
| `TARGET_NOT_FOUND` | the target PDF does not exist |
| `PDF_UNREADABLE` | the PDF cannot be opened |
| `PDF_PASSWORD` | the PDF needs a password to open (a PDF with only an owner password opens without one and is scanned normally) |
| `JSON_PATH_CONFLICT` | `--json` names the target or the rules file |
| `JSON_PATH_UNWRITABLE` | an existing `--json` report cannot be removed |
| `MAP_PATH_CONFLICT`, `MAP_PATH_UNWRITABLE` | the `--visible-map` path cannot be written safely (see [The output file](#the-output-file)) |
| `INTERNAL_ERROR` | the tool crashed |

A command line argparse rejects (a typo, a missing required option) exits
`2` before any of this, with no JSON report.

### `schema_version`

`schema_version` changes when a field is renamed, removed or changes
meaning; adding a field does not change it. It is `2` since `notices`
([ADR 0012](adr/0012-pattern-classes-inform.md)): with built-in classes
informational, a class match no longer appears in `findings` or
`warnings`, which changes what those mean.

## The six layers

Each of six **layers** extracts text from a different part of the file,
and every layer runs the same matchers over what it extracted. The layers
are ordered by how common their failure mode is in practice: a judgment
from well-known redaction failures and the cases this project has seen,
not measured statistics. [COVERAGE.md](../COVERAGE.md) maps the known
places a secret can be stored against what reads each one.

| # | Layer | Finds a secret that… | Method | Tools |
| --- | --- | --- | --- | --- |
| 1 | **Text** | is covered by a box but still selectable | characters read with positions, rebuilt into reading order, matched | PyMuPDF |
| 2 | **Objects** | the page no longer shows but the file still stores | every PDF object decoded and matched; unreferenced ones flagged ORPHANED | PyMuPDF + built-in PDF string parser |
| 3 | **Metadata** | sits in document properties | every metadata tag dumped and matched | exiftool, PyMuPDF |
| 4 | **Hidden** | no page renders at all | attachments, annotations, form fields, links, JavaScript, layer names read and matched; page thumbnails flagged | PyMuPDF |
| 5 | **OCR** | exists only as pixels | each page rendered to an image, text recognized, matched | PyMuPDF, Apple Vision |
| 6 | **Binary** | survives in the objects qpdf rebuilds: a cross-check for damaged files | reachable objects decompressed, searched for known values | qpdf |

Each layer extracts text independently, so a blind spot in one extraction
cannot hide a leak from the others.

### Text layer

The most common redaction mistake is drawing a black box over text that is
still there: invisible on screen, but selectable and copyable.

- **Method:** PyMuPDF `page.get_text("rawdict")` returns every character
  with its position and writing direction. The tool rebuilds lines itself,
  clustering characters by position (tolerance scales with font size) and
  sorting within each line, so text drawn out of order or one digit per
  form box still reads back as `123-45-6789`. A wide gap inside a line is
  treated as a column break, so neighbouring table cells never merge into
  a false number. Text placed outside the visible page area (the crop box
  ∩ the media box) is read too, as its own reading (invisible to a viewer,
  but still in the file), and kept apart from the visible text so it
  cannot come between the halves of a value split across a page break. It
  is also read together with the visible text, across the visible edge,
  so a value that starts on the page and runs off it, or that is written
  vertically off the page, is read whole. Across the edge, lines are told
  apart with the visible text's tolerance.
- **Readings:** seven *genuine* readings: the visible text, written
  horizontally (read left to right) or vertically (rotated, read along its
  own direction both ways); the off-page text; and the same three
  directions across the visible edge, visible and off-page text together.
  Plus three *reconstructed* readings: every visible glyph in vertical
  columns, both ways, and all the horizontal text, visible or not. The
  columns are the only reading of a value written one character per line,
  but a column of an ordinary page also stacks unrelated lines (a numbered
  list's numbers), so reconstructions only ever give manual-review
  warnings.
- **Across pages:** each page break is checked on its own. A value running
  from the last line of one page onto the first line of the next, in a
  genuine reading of the visible text (a visible line read on across the
  visible edge included) or of the off-page text, is hard. Any other
  match crossing the break (needing other lines, across a blank or
  unreadable page, or from text wholly off the page, such as a slug below
  the page or beside visible text across a column break, onto the next
  page's first line, or onto text wholly off the next page, above its top
  edge) is a manual-review warning at the page it crosses into. A value
  wrapped from a page's last visible line onto a line wholly below the
  page is a manual-review warning too. The limit: a slug only a word space
  from visible text on its baseline ('Page 7' then 'Job 123-45' just past
  the edge) reads as one line with it, exactly like a value running past
  the edge, so if it and the next page's first line together form a value,
  that is a (false) finding.
- **Split values:** the review net assembles a known value from two or
  three pieces of different lines within five consecutive lines, in any
  order, so a value split across a page break with a header, footer or
  page number between its halves, a value wrapped inside one column of a
  two-column page, or one whose halves come in reverse order is a
  manual-review warning (see COVERAGE.md's
  [Matching limits](../COVERAGE.md#matching-limits)).
- **Tier:** a known value is hard on one line of a genuine reading, or at
  a page seam as above. A pattern is hard only on one line of a genuine
  reading, the readings across the visible edge included. On a line read
  across the edge, a wide gap (a column break) keeps a visible line and an
  off-page slug on its baseline apart: the slug is left to the off-page
  reading. A value the two form across that gap is a manual-review warning
  (owner decision, 2026-10-11): never a finding, never silent, and by
  design also raised on a clean page whose page number or footer and a
  slug past the edge happen to form a value. A narrower gap does not keep
  them apart: the two read as one line, like a value running past the
  edge. Everything that needs text joined is manual review.

### Objects layer

A redaction tool can remove text from the page while the original stays in
the file as an object nothing uses any more. This layer caught every leak
in this project's real-document case.

- **Method:** walks every numbered PDF object with PyMuPDF (`xref_object`,
  `xref_stream`) and decodes its string tokens with a built-in parser that
  understands PDF syntax (`(literal)` strings with escapes and nested
  parentheses, `<hex>` strings) as latin-1, or UTF-16 when marked. Fonts
  and encodings are not applied, so text in a font whose codes are not
  plain characters (CID/Identity-H fonts, standard for Word, Chrome and
  embedded TrueType; custom `/Differences` encodings; subset fonts with
  sequential codes; Type3 fonts) cannot be read here. Stream bodies are
  skipped when their dictionary marks them as an image, font program,
  attachment, object stream or cross-reference stream, or when under 85%
  of their first 64 KB is printable; so an inline image making up more
  than about 15% of a content stream also hides the text around it. Every
  object's dictionary is always scanned, and so is every trailer
  dictionary (the document `/ID` and any other key, in the current trailer
  and every earlier revision's), which is not an object. A pattern-rule
  match in a `/ID` element written as nothing but hex digits is manual
  review: such generated identifiers hold digit runs by coincidence.
  References are followed from the file's root; any object not reached is
  reported **ORPHANED**: left behind, not displayed.
- **Leftover content:** orphaned objects, and every object an incremental
  update rewrote (earlier revisions are found through the file's own
  cross-reference chain and rebuilt; up to 50 revisions, the original and
  the latest 49). Leftover XMP packets, attachment bodies and other
  leftover text that is not PDF syntax are searched as raw text. Leftover
  content the tool finds but cannot read (text in font codes that are not
  plain characters, images including inline ones, containers such as zip
  or PDF) is **flagged** as a manual-review warning. Leftover payloads are
  read up to 16 MB decompressed.
- **Tier:** a known value is hard anywhere in one object, even split
  across its strings; a pattern is hard only within one decoded string.

### Metadata layer

Title, author, subject, keywords and custom fields often keep a name or
case number that was scrubbed from the page.

- **Method:** `exiftool -json` dumps the metadata tags. Known values are
  searched across the whole dump (tag names included); patterns run on
  each value separately. When the Info dictionary and XMP use the same tag
  name, exiftool reports only one of them; the Objects layer still reads
  the Info strings. Nine tags describing the local file are ignored
  (`SourceFile`, `ExifToolVersion`, `FileName`, `Directory`, `FileSize`,
  `FileModifyDate`, `FileAccessDate`, `FileInodeChangeDate`,
  `FilePermissions`), so the file's own path cannot match. The XMP block
  is also read in-process with PyMuPDF `get_xml_metadata()`.
- **Tier:** hard.

### Hidden layer

- **Method:** PyMuPDF APIs read each hidden place directly: attachments
  (`embfile_*`: names, descriptions, and contents up to 16 MB decoded as
  UTF-8 text; an attachment that is not text, such as zip, Office, image
  or nested PDF, recognised by file signature, is flagged as not scanned),
  annotation text and attached files (`page.annots()`), form-field values
  and names (`page.widgets()`), link targets (`page.get_links()`),
  JavaScript in the catalog's `/OpenAction` and the top level of its named
  JavaScript tree, and optional-content group names (`get_ocgs()`). A page
  carrying a `/Thumb` preview image (on the page, or on a Pages node above
  it) is flagged `PAGE_THUMBNAIL` (exit `2`): the thumbnail is not decoded
  or OCR'd, and can still show what the page no longer does. Scripts on
  links, form fields, pages or the catalog's other actions are not
  reported here; a script stored as a string is still caught by the
  Objects layer, and one stored as a stream by Binary (manual review).
- **Tier:** hard findings for text fields; attachment *contents* are
  arbitrary bytes, so matches there are manual review.

### OCR layer

Needed when the secret exists only as pixels: scanned pages, pages
flattened to images, text converted to vector outlines.

- **Method:** PyMuPDF renders each page to a 300 dpi grayscale image
  (`get_pixmap`). Apple Vision (`VNRecognizeTextRequest`, accurate mode,
  called through PyObjC) recognizes the text twice, with language
  correction on and off, because autocorrect can silently change digits.
  Both readings are matched. Without the Vision bridge this layer reports
  itself unavailable (`OCR_UNAVAILABLE`) and the run never exits `0`.
- **Pages it cannot fully read** get a `PAGE_FAILED` warning, so the run
  never exits `0` (`2`, or `1` if another layer found a leak):
  - a page where Vision raises an error (before the fix in the CHANGELOG,
    this happened to every page on macOS 27);
  - a page where Vision reports success without delivering both readings:
    a reading counts only once its request's completion handler has run
    to the end;
  - a page where a Vision call does not return within 120 seconds (a
    stalled Vision, [#115](https://github.com/billylokhl/pdf-redaction-verifier/issues/115)).
    While that call still has not returned, the later pages fail at once
    instead of each waiting, so the run ends with exit `2` (or `1`)
    instead of hanging;
  - a page whose 300 dpi render is longer than 3510 px on a side (over
    about 11.7 in: legal, A3, tabloid, plans and posters; letter and A4
    pages are not). It is read, and anything found on it counts, but on
    such a render Vision reads less and less of the smaller print, and
    then nothing, without an error
    ([#128](https://github.com/billylokhl/pdf-redaction-verifier/issues/128)).

  Otherwise a page Vision reads without error but with text missing is
  not detected; see DESIGN's known limitations and COVERAGE.md footnote
  ¹².
- **Images painted over** (K12, K38): OCR reads the rendered page, so an
  image with an opaque box drawn over it reads as the box. A page with an
  image that one opaque fill drawn after it covers by a quarter of its
  area or more gets an `IMAGE_COVERED` warning (exit `2`, with or without
  OCR), and with OCR the page is rebuilt from its images alone, each at
  its drawn place with nothing over it, and read again: a listed value
  there is a finding (exit `1`), so strips of one picture drawn under a
  box rejoin and are read. A blank image under a box is flagged too. See
  COVERAGE.md footnote ¹⁷ for the limits.
- **Vertical lines:** on macOS 27, Vision returns a column of characters
  written one per line as a single line read sideways, often misread
  (`199140190` for the digits 1 to 9). So a vertical line's box is turned
  upright and read again: rotated text reads back the same, a stack
  usually does not (a stack of `0`, `1`, `8`, `O`, `I`, `H` can repeat its
  misread:
  [#65](https://github.com/billylokhl/pdf-redaction-verifier/issues/65)).
  A page gets at most 64 re-reads, retries included; vertical lines past
  that are manual review, and so, sometimes, is small slanted text near a
  page edge whose re-read does not match. A page with any vertical line is
  also read turned 90° and 270° (Vision drops lines of sideways text), and
  each unconfirmed vertical line is unstacked into one line and read; both
  only ever give manual-review warnings.
- **Tier:** same rules as the Text layer, with both readings eligible for
  hard findings, except a line Vision reads vertically that an upright
  re-read does not confirm. Like the Text layer's reconstructed readings,
  a match in an unconfirmed line is only ever a manual-review warning.
- **Not yet covered:** an image placed outside the visible page area is
  not rendered, so OCR does not see it. A scanned value written one
  character per line that Vision does not report as a line at all passes
  as clean with a value rule
  ([#81](https://github.com/billylokhl/pdf-redaction-verifier/issues/81));
  on macOS 26 so does the stack that macOS 27 unstacks
  (`layout.scanned-vertical-stack`, K42), since Vision there reads it
  differently; a page Vision reads with lines missing passes too, unless a
  line on it is read vertically
  ([#67](https://github.com/billylokhl/pdf-redaction-verifier/issues/67),
  [#82](https://github.com/billylokhl/pdf-redaction-verifier/issues/82)).

### Binary layer

A cross-check using a second PDF parser: qpdf can recover objects from a
damaged cross-reference table, and it decompresses attachment contents.

- **Method:** qpdf (`qpdf --qdf --object-streams=disable
  --deterministic-id`) rewrites the objects reachable from the file's
  root, decompressing their streams (image codecs such as JPEG stay
  encoded); the output is streamed through the same normalized substring
  search, for value secrets only. Regex patterns are not run here: raw
  bytes produce too many coincidental matches. Unreferenced (orphaned)
  objects and superseded object versions are **not** in qpdf's output;
  the Objects layer covers those. Decompressed font programs *are* in it,
  and their tables contain byte runs such as `123456789`, so a sequential
  value can raise a coincidental Binary warning on a clean document.
- **The document `/ID`:** qpdf writes a `/ID` of its own in the trailer
  (keeping the file's first element, generating the second, or both if the
  file has none or its first element is empty). The tool asks for a
  deterministic one (`--deterministic-id`), so they do not change from one
  run to the next, and blanks the elements qpdf generated before scanning
  whenever it can tell them apart from the file's own (when it cannot, as
  for a file whose first `/ID` element is empty, they are still scanned,
  and a coincidental match is the same on every run).
- **qpdf's exit 3** ("succeeded with warnings") makes the run exit `2`
  (`TOOL_EXIT_NONZERO`) unless every warning qpdf printed is one of five
  kinds the tool can check against the file's own bytes and the check
  passes (owner decision, 2026-10-15): a `(0 0 R)` reference, an xref
  entry at offset 0 with no stored body of that object anywhere, a
  duplicated key with identical values, a missing `endobj` followed only
  by whitespace, or a Flate stream without its end marker that is not an
  object stream (an xref stream only if every row decodes). Any other
  warning, including a rebuilt cross-reference table, still exits `2`
  (COVERAGE.md footnote ¹⁶).
- **Tier:** always manual review, never a hard finding.

## The visible map

```bash
pdf-verify --target document.pdf --secrets secrets.json --visible-map map.pdf
```

also writes `map.pdf`: a masked picture of *where* each finding is: a
summary page, then one page per original page that has something to box,
at the same displayed size, with a box over each place and a numbered list
saying what each box is. It is a picture of the scan, **not a verdict**:
it is built after the report is printed, in a separate process, and
nothing it does can change the exit code, stdout or the `--json` report.
The one exception is before scanning: if `map.pdf` cannot safely be
written, the run stops with exit `2` and `MAP_PATH_CONFLICT` or
`MAP_PATH_UNWRITABLE`.

### The output file

The map is only ever written as a new file. An existing `map.pdf` is
replaced only if it is one of this tool's own maps: a regular file you own
whose second line and last line are both `%PRV-VISIBLE-MAP-1`. It is
removed at the start of every run that gets as far as checking it (a run
refused for a malformed command line does not), so a run that dies never
leaves an old map behind. Anything else at that path (another PDF, a
symbolic link, a directory, the target, the rules file or the `--json`
report) is refused untouched; there is deliberately no `--force`. A file
with only the line-2 marker is a map an interrupted run left half-written;
it is refused by name, and you delete it. Hidden temp files a killed run
left beside it (`.map.pdf.<pid>.<token>.tmp`, `.old` or `.probe`,
carrying the marker, or still empty) are removed by a later run once they
are older than 20 minutes and no process with that id is running. The
flag must be spelled out in full: `--v` and other abbreviations of it are
refused.

### Status lines and codes

Whether a map was written, and if not why, is one line on stderr: `[MAP]
MAP_WRITTEN: ...` or `[ERROR] <code>: visible map not written: ...`. A
run that ends without a map leaves no file at all. The codes:

| Code | Meaning |
| --- | --- |
| `MAP_PATH_CONFLICT` | something other than this tool's own map is at the path, or it names an input; before scanning this stops the run (exit `2`), afterwards (the path changed while the map was built) there is just no map |
| `MAP_PATH_UNWRITABLE` | the path cannot be checked or written (refused before scanning, exit `2`) |
| `MAP_TIMEOUT` | the map took too long |
| `MAP_TARGET_CHANGED` | the target changed after the scan, or while the map read it |
| `MAP_TARGET_UNREADABLE` | the target could not be reopened for the map |
| `MAP_SELF_CHECK_FAILED` | the finished map failed its own check for secrets (see [Masking and the self-check](#masking-and-the-self-check)) |
| `MAP_CHILD_FAILED` | the map process failed |
| `MAP_OUTPUT_INVALID` | the map process's output did not check out |
| `MAP_INTERRUPTED` | the run was interrupted while the map was built |

### What it shows

Each box is a place a finding can be seen, and how:

| Box | Meaning |
| --- | --- |
| solid | On the page: selectable text matching a rule (whether or not it is covered; see the [limits](#limits)), or an annotation or form field whose appearance shows it, or an open note |
| hatched | A form field whose stored value differs from what its appearance shows: on the page in viewers that redraw form fields |
| dashed | On click, hover or scroll: a comment's text, author or subject, a link's address, a list box's hidden choice, an annotation shown only while the pointer is over it (ToggleNoView) |
| dotted | When printed (an annotation flagged "print, but do not show") |

An open note is solid for its text only: its author and subject stay
dashed. Every copy of a value on a page is boxed or listed on its own,
including text drawn out of reading order. A value that runs past the
edge of the visible page is boxed where it shows, and its hidden part is
listed as outside the crop.

Findings that cannot be boxed are listed on the summary page, by page:
found by OCR, outside the crop (recoverable by uncropping), on a
switched-off layer, behind a layer rule the map does not evaluate, in a
panel (document properties, attachment names, form field names, layer
names), when a script runs, live content that may be visible (with the
object's role), possible matches left for review (among them an
attachment's or an annotation's attached file's contents, which the scan
itself only flags for review), and anything that **could not be
positioned**. Three more lists:

- **Stored, not shown**: a checkbox's or radio button's export value.
- **Stored; not shown by viewers that honour the password flag**: a
  password field's value. It counts as shown on click, unlike the design's
  "stored" row, because a viewer that ignores the flag displays it.
- **Also stored in a boxed annotation's or field's own objects**: the
  Objects layer finding the same value in the dictionary or appearance of
  an annotation, field or link the map has already boxed for that rule
  (every one of them, when one appearance serves several). It adds nothing
  a reader could not already see, so it does not make the map incomplete.

A field listed as stored, hidden or password keeps its live copy flagged:
the Objects layer may have matched a key the Hidden layer never reads,
such as a tooltip shown on hover. A page's content stream and every other
live object always do. Content stored outside the document (orphaned,
unreferenced, superseded) and annotations flagged hidden (or "do not
show" without "print") are only counted, under **Not visible**. Every
finding is either boxed, listed, or listed as could not be positioned;
none is dropped. A built-in class match that is only a notice (the
default) is never boxed: it is listed last, masked like any sample, under
**Not counted in the verdict**, and sets no flag.

### The headline

The headline at the top of the summary is exactly one of:

| Headline | When |
| --- | --- |
| VISIBLE SECRETS FOUND | a solid or hatched box, or an OCR hit (then: "by OCR, not positioned") |
| MAP INCOMPLETE — a secret may be visible that is not drawn (*N* incomplete, *N* possible, *N* in live content; *N* shown on click / hover / ...) | something could not be placed or was not scanned, a switched-off layer, a layer rule, a page with area outside its crop, a coverage or scope warning, a possible match, or live stored content; the counts are of each reason and of the items shown on click, hover, print or open, in panels or when uncropped |
| SHOWN ON CLICK / HOVER / PRINT / OPEN / IN PANELS / WHEN UNCROPPED — none drawn on the page | only dashed or dotted boxes and such lists |
| LEAK, NOT VISIBLE — the file stores a secret no page shows | exit `1`, and nothing above |
| Nothing visible found; uncertified (exit 2) for: *codes* | exit `2` only for content stored outside the document |
| Nothing visible found — every check ran; JSON report not written (exit 2) | exit `2` only because `--json` could not be written |
| Nothing visible found — every check ran (exit 0) | exit `0`, and nothing above; by default (not under `--strict-classes`), with *N* > 0 notices (the report lists one per place a match is found, by layer, rule and location such as page or object, not one per distinct value) it ends "; *N* notice(s) not counted in the verdict" |
| NOT CERTIFIED (exit *N*): *codes* | anything else; unreachable by design (every warning code makes the map incomplete, marks a possible match, or is an "outside" code), kept as the fail-closed answer |

When the `--json` report was not written every headline says so, and a
map that is incomplete also carries a red INCOMPLETE banner naming why
(warning codes with their pages).

### Masking and the self-check

The map never shows an exact value, and no sample it shows reveals a
character of one. A sample is masked by the same rule as the reports
(`****6789`, or only `****` when the match contains one of your exact
values or any character it would show is a character of one, ignoring
case, accents and the script of a digit), and the map then checks the
tail as it is drawn: a character outside Latin-1 is drawn as `\u{XXXX}`,
so a tail whose escape holds a character of an exact value (文 is
`\u{6587}`) shows only `****` too. A pattern match drawn on any glyph of
an exact value shows no tail at all.

The target's file name is shown only when no two consecutive characters
of it, as written or as drawn, occur together in an exact value (letters
of any script count) and it holds no `****`; otherwise the map says
"target", and the sha256 identifies the file. A rules file entry from
this tool's own JSON format is shown by number (`value rule #3`, explained
as "3rd value entry, rules-file entry 5"), because its `name` is your own
words and may be the secret; a shared YAML config's rules keep their
report labels (`exact value #2 (exact_values[1])`).

Before the map is written it is checked against every rule twice: each
label, name and masked sample on its own, then the saved file's text,
metadata and streams, with escapes decoded. A whole value, a pattern
match or a masked tail showing a value's character anywhere except the
tool's own fixed wording (headings, counts, page numbers) means no map
(`MAP_SELF_CHECK_FAILED`).

### Limits

It draws the masked map only. OCR hits are listed, not positioned. A
switched-off layer's content is not fully checked (images and font-coded
text on it are not read), so a document with one is always MAP
INCOMPLETE, even at exit `0`. The same holds for any layer rule (an OCMD,
or any `/VE` visibility expression), and for any page whose MediaBox
reaches past its CropBox on any side (a smaller CropBox, or a shifted
one): images there are not checked, so the map says so instead of
"Nothing visible found". A switched-off layer is one MuPDF evaluates as
off, or one the default configuration's `/OFF` or `/BaseState /OFF`
turns off (MuPDF's own answer misses a layer that carries a `/Usage`
dictionary). A solid text box means the text is in the page's text
layer; it does not say whether the text is drawn, covered by a box, white
on white or invisible. Deciding that from pixels is a later step.
Annotations gated by a visibility expression (an OCMD) are listed "behind
a layer rule (may be visible)" unless the page text shows them.

## Requirements and running from source

- **Python** 3.10 or newer (CI tests 3.10, 3.12, 3.13 and 3.14).
  Dependencies, including the macOS-only Apple Vision OCR bridge, are
  declared in `pyproject.toml` and hash-locked in `uv.lock`.
- **exiftool and qpdf.** The Metadata and Binary layers run them; without
  them those layers report themselves not scanned and the run exits `2`
  rather than certifying clean.
- **qpdf and encrypted files.** qpdf's macOS release binary reads
  RC4-encrypted files (and AES-128 ones, whose password check uses RC4)
  only if it finds OpenSSL's legacy provider, in the modules directory it
  was built with. Homebrew's own qpdf needs nothing. See
  [CONTRIBUTING.md](../CONTRIBUTING.md#qpdf-versions-and-openssl) if you
  use the release binary.
- **Without uv:** `pip install -e .` works from `pyproject.toml` directly
  (not the lock file).
- **Running `verify.py` directly:** `python verify.py --target ...
  --secrets ...` works installed or not. `verify.py` imports its pure parts
  from the `redaction_verifier` package, so it must either be installed
  (`pip install` or `uv sync`) or run from a copy of the repository that
  still has `redaction_verifier/` next to it; copying `verify.py` out on
  its own does not work.

[DESIGN.md](../DESIGN.md) explains the design rationale: the exit-code
contract, the two-tier matching model, and the known limitations.

## Glossary

### What is inside a PDF

A PDF is a pile of numbered **objects**. A **cross-reference (xref)
table** at the end of the file records where each one lives. Some objects
are **dictionaries**, key–value records like `<< /Title (Quarterly Report) >>`,
whose text values are **strings**, written `(like this)` or as hex
`<4A6F…>`. A **stream** is an object carrying a blob of (usually
compressed) data; a page's **content stream** holds its drawing
instructions ("set font, move here, draw these characters"). Objects
**reference** one another starting from the file's root; an object
nothing references is an **orphan**: still stored in the file, never
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
| **DPI** | Dots per inch: the resolution a page is rendered at before OCR (300 here). |
| **Draw order** | The order a PDF paints characters in, which need not match reading order. |
| **exiftool** | External command-line tool that reads every metadata tag in a file. |
| **Exit code** | Number a program returns when it ends; scripts and CI read it as the verdict. |
| **Fail closed** | When unsure, report "cannot certify" rather than "clean": silence never counts as success. |
| **Fixture** | A sample PDF generated for a test. |
| **Flattened** | A page converted to a picture, leaving no text data. |
| **Form-field value** | Data typed into a fillable form. |
| **Fused** | Separate pieces of text joined together (e.g. two table cells), which can accidentally form a digit sequence. |
| **Garbage-collected rewrite** | Saving a PDF so unreferenced (orphaned) objects are dropped. The fix for leftover-content leaks. |
| **Genuine reading** | Page text read in the direction it was written (horizontal, rotated, or an OCR pass), on the page, off it, or across the visible edge; its matches can be hard findings. |
| **Guard** | A check in the code that prevents a known bug. |
| **Hard finding** | A match strong enough to count as a confirmed leak (exit `1`); see [Tiers](#tiers) and each layer's *Tier*. |
| **Incremental update** | Edits appended to the end of a file; earlier revisions stay inside it. The Objects layer reads the objects a later save rewrote (`earlier version` in the report). |
| **Info / XMP** | The two places PDFs store metadata: the older Info dictionary and the newer XML block. |
| **Layout-aware** | Rebuilds lines from where characters sit on the page, not the order the file draws them. |
| **LLM** | Large language model: the AI a redaction tool may use to find names and addresses. |
| **Luhn** | Checksum built into credit-card numbers; random 16-digit strings usually fail it. |
| **Manual-review warning** | A possible match that needs a human to check it; exits `2`. |
| **Marker** | The line `%PRV-VISIBLE-MAP-1`, written as a visible map's second and last line; only a file carrying both is ever replaced. |
| **Masked** | The report shows only the tail of a match (`****6789`) so it doesn't leak the secret itself; see [Masking](#masking). |
| **Mutation testing** | Deliberately breaking a guard to prove the tests notice. |
| **NANP** | North American Numbering Plan: the rules for valid US/Canada phone numbers. |
| **NFKC** | Unicode step that folds look-alike characters (fullwidth digits, fancy dashes) to plain ones without splitting accents. |
| **NFKD fold** | Unicode step turning look-alike characters into plain ones (fullwidth `１` → `1`, `é` → `e`). |
| **Normalized** | Text reduced to a canonical form before comparing, so formatting can't hide a match. |
| **Object streams / xref stream** | The PDF 1.5+ layout that packs many objects into one compressed container: the default for Acrobat and Chrome. |
| **OCR** | Optical character recognition: reading text from pixels. |
| **Octal coercion** | YAML gotcha: an unquoted `00123456` is read as a base-8 number, changing its value. |
| **Optional-content group** | A PDF "layer" that can be switched on or off; its name alone can leak data. |
| **ORPHANED** | Label for a finding in an object nothing references: present in the file, never displayed. The terminal report tags it `left over`. |
| **Outlined vector text** | Letters converted into drawn shapes: looks like text, isn't text data. |
| **PyMuPDF** | Python library for reading and rendering PDFs; most layers use it. |
| **PyObjC** | Bridge that lets Python call macOS frameworks such as Apple Vision. |
| **qpdf** | External command-line tool that rewrites a PDF's reachable objects with their streams decompressed. |
| **Reconstructed reading** | Every glyph stacked into vertical columns: catches a value written one character per line, but its matches are only ever manual review. OCR's equivalent is Vision's own reading of a vertical line no upright re-read confirms. |
| **Regex** | A text-search pattern language. |
| **Rendered page** | The page drawn as an image: what a viewer sees. |
| **Sidecar** | A small companion file next to a PDF listing the secrets it contains. |
| **Substring search** | Checking whether one string appears exactly inside another. |
| **Surface** | One real piece of text: a line, a decoded PDF string, a form or annotation field. |
| **Switched-off layer** | An optional-content group the document's default configuration turns off: its content is in the file but not drawn until a reader switches it on. |
| **UTF-16** | A text encoding PDFs use for non-ASCII strings; the Objects layer decodes it. |
| **Validator** | A check that a match is structurally real (e.g. SSNs never start with `000` or `666`). |
| **Visible map** | The masked PDF `--visible-map` writes: where each finding is, and how a reader would come across it. A picture of the scan, never a verdict. |
| **Walked structurally** | Reading objects one by one through the PDF's own structure rather than scanning raw bytes. |
