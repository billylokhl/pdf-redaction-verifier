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

(`"Any SSN"` is a built-in class: by default its matches are reported as
notices that do not change the verdict; add `"strict": true` to make them
count. See **Built-in classes** below.)

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

The report printed on the terminal says the same thing in plain words.
A leak opens with **`[FAIL]  SECRETS ARE STILL IN THIS FILE`** and how
many of your rules matched; each finding is one line —
`✖ STILL PRESENT | rule | layer | where | location` — meaning that text
matching that rule is still in the file, so a redaction did not remove
it. The rule is named by its place in your rules file (`exact value #2
(exact_values[1])`, `pattern #5 (patterns[4]) ****5916`, `built-in type:
SSN`, or the `name` you gave it): an exact value's characters are never
shown, a pattern's match at most its last four. *Where* says what kind of
problem it is — `on a page` (the page's text or what it shows), `stored`
(in the file's objects), `left over` (no page shows it but the file still
stores it — see ORPHANED in the [Glossary](#glossary); `left over?` when
that could not be confirmed), `earlier version`
(an older save inside the file), `properties` (metadata), or the hidden
part it is in (`comment or note`, `form field`, `link`, `attachment`,
`script`, `layer name`). A "What this means" section after the findings
explains, once each, every kind that occurred, with the usual fix. A run
that found nothing but cannot certify (exit `2`) opens with
**`[NOT CERTIFIED]`** and says why — including when the `--json` report
could not be written; only exit `0` prints `[PASS]`.

For scripts, add `--json report.json` to also write a machine-readable
report: the verdict, each finding (samples masked), and each warning with
a stable `code` (e.g. `LEFTOVER_IMAGE`) and `kind` — `review` (a possible
match to judge), `coverage` (something not fully read) or `scope` (the
rules ask for something the tool cannot check). Both carry structured
fields: the layer, where the content is stored (`live`, `orphaned`,
`unreferenced`, `superseded`), the object, revision and page when known,
and (only on `TOOL_EXIT_NONZERO`) the tool's own `returncode`, so a real
qpdf failure can be told apart from its benign, version-dependent exit 3
without parsing the message text. (An exit 3 whose every warning checks
out as harmless gives no warning at all; see Binary below.) An internal crash is reported the same
way any other operational failure is — `error: {"code": "INTERNAL_ERROR",
"message": ...}` alongside whatever findings and warnings were already
recorded, message text never more than the exception's type plus a
truncated summary, never raw document content. The file is replaced
atomically and removed at the start of each run, so a run that dies
leaves no report rather than an old one — but the exit code stays the
authority. The format is experimental; `schema_version` changes if a
field is renamed or removed (2 since `notices`, below). `--version` (on its
own) prints the tool version.

**Built-in classes** (`ssn`, `credit-card`, `email`, `us-phone`) match any
value of that shape, not a secret you listed. By default their matches
are reported in a separate section after the verdict, "Not
counted in the verdict", and in the JSON report's `notices` array (`kind:
"class-match"`; every field a finding or a warning has, null when it
does not apply; `tier` `hard` or `review`; a review warning's own kind as
`warning_kind`), and they never change the exit code: exit `0` then means that
none of the values you listed, and none of your own `pattern` rules, was
found in any layer the tool read, and that nothing stopped it reading a
layer -- it says nothing about other SSN-, card-, email- or phone-shaped
values in the file, which the notices show. If you rely on the classes as
a net for secrets you did not list, use strict mode (`--strict-classes`,
or `"strict": true` on a class rule): class matches then count like any
rule, exactly as before ADR 0012
([ADR 0012](docs/adr/0012-pattern-classes-inform.md)). A `"strict"` on a
class rule that is not `true` or `false` is a rules error (exit `2`)
unless `--strict-classes` is given.

## Visible map (`--visible-map`)

```bash
pdf-verify --target document.pdf --secrets secrets.json --visible-map map.pdf
```

also writes `map.pdf`: a masked picture of *where* each finding is — a
summary page, then one page per original page that has something to box,
at the same displayed size, with a box over each place and a numbered
list saying what each box is. It is a picture of the scan, **not a
verdict**: it is built after the report is printed, in a separate
process, and nothing it does can change the exit code, stdout or the
`--json` report. The one exception is before scanning: if `map.pdf`
cannot safely be written the run stops with exit `2` and
`MAP_PATH_CONFLICT` or `MAP_PATH_UNWRITABLE`. Whether a map was written,
and if not why (`MAP_TIMEOUT`, `MAP_TARGET_CHANGED`,
`MAP_SELF_CHECK_FAILED`, ...), is one line on stderr; a run that ends
without a map leaves no file at all.

**The output file.** The map is only ever written as a new file. An
existing `map.pdf` is replaced only if it is one of this tool's own maps
— a regular file you own whose second line and last line are both
`%PRV-VISIBLE-MAP-1` — and it is removed at the start of every run that
gets as far as checking it (a run refused for a malformed command line
does not), so a run that dies never leaves an old map behind. Anything else at that
path (another PDF, a symbolic link, a directory, the target, the rules
file or the `--json` report) is refused untouched; there is deliberately
no `--force`. A file with only the line-2 marker is a map an interrupted
run left half-written; it is refused by name, and you delete it. Hidden
temp files a killed run left beside it (`.map.pdf.<pid>.<token>.tmp`,
`.old` or `.probe`, carrying
the marker, or still empty) are removed by a later run once they are
older than 20 minutes and no process with that id is running. The flag must be spelled out in
full: `--v` and other abbreviations of it are refused.

**What it shows.** Each box is a place a finding can be seen, and how:

| Box | Meaning |
| --- | --- |
| solid | On the page: selectable text matching a rule (whether or not it is covered — see the limits), or an annotation or form field whose appearance shows it, or an open note |
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
**Stored, not shown** (a checkbox's or radio button's export value);
**Stored; not shown by viewers that honour the password flag** (a
password field's value — counted as shown on click, unlike the design's
"stored" row, because a viewer that ignores the flag displays it); and
**Also stored in a boxed annotation's or field's own objects** (the
Objects layer finding the same value in the dictionary or appearance of
an annotation, field or link the map has already boxed for that rule —
every one of them, when one appearance serves several — which adds
nothing a reader could not already see, so it does not make the map
incomplete). A field listed as stored, hidden or password keeps its live
copy flagged: the Objects layer may have matched a key the Hidden layer
never reads, such as a tooltip shown on hover. A page's content stream
and every other live object always do. Content stored outside the document (orphaned, unreferenced,
superseded) and annotations flagged hidden (or "do not show" without
"print") are only counted, under **Not visible**. Every finding is either
boxed, listed, or listed as could not be positioned — none is dropped. A
built-in class match that is only a notice (the default; it does not count
in the verdict) is never boxed: it is listed last, masked
like any sample, under **Not counted in the verdict**, and sets no flag.

**The headline** at the top of the summary is exactly one of:

| Headline | When |
| --- | --- |
| VISIBLE SECRETS FOUND | a solid or hatched box, or an OCR hit (then: "by OCR, not positioned") |
| MAP INCOMPLETE — a secret may be visible that is not drawn (*N* incomplete, *N* possible, *N* in live content; *N* shown on click / hover / ...) | something could not be placed or was not scanned, a switched-off layer, a layer rule, a page with area outside its crop, a coverage or scope warning, a possible match, or live stored content; the counts are of each reason and of the items shown on click, hover, print or open, in panels or when uncropped |
| SHOWN ON CLICK / HOVER / PRINT / OPEN / IN PANELS / WHEN UNCROPPED — none drawn on the page | only dashed or dotted boxes and such lists |
| LEAK, NOT VISIBLE — the file stores a secret no page shows | exit `1`, and nothing above |
| Nothing visible found; uncertified (exit 2) for: *codes* | exit `2` only for content stored outside the document |
| Nothing visible found — every check ran; JSON report not written (exit 2) | exit `2` only because `--json` could not be written |
| Nothing visible found — every check ran (exit 0) | exit `0`, and nothing above; by default (not under `--strict-classes`), with *N* > 0 notices (the report lists one per place a match is found — layer, rule and location such as page or object — not one per distinct value) it ends "; *N* notice(s) not counted in the verdict" |
| NOT CERTIFIED (exit *N*): *codes* | anything else; unreachable by design (every warning code makes the map incomplete, marks a possible match, or is an "outside" code), kept as the fail-closed answer |

When the `--json` report was not written every headline says so, and a
map that is incomplete also carries a red INCOMPLETE banner naming why
(warning codes with their pages).

**Masked, always.** The map never shows an exact value, and no sample
it shows reveals a character of one. A sample is masked by the same rule as the reports (`****6789`,
or only `****` when the match contains one of your exact values or any
character it would show is a character of one, ignoring case, accents
and the script of a digit), and the map then checks the tail as it is
drawn: a character outside Latin-1 is drawn as `\u{XXXX}`, so a tail
whose escape holds a character of an exact value (文 is `\u{6587}`) shows
only `****` too. A pattern match drawn on any glyph of an exact value
shows no tail at all. The target's file name is shown only when no
two consecutive characters of it, as written or as drawn, occur
together in an exact value (letters of any script count) and it holds
no `****`; otherwise the
map says "target", and the sha256 identifies the file. A
rules file entry from this tool's own JSON format is shown by number
(`value rule #3`, explained as "3rd value entry, rules-file entry 5"),
because its `name` is your own words and may be the secret; a shared
YAML config's rules keep their report labels (`exact value #2
(exact_values[1])`). Before the map is written it is checked against
every rule twice — each label, name and masked sample on its own, then
the saved file's text, metadata and streams, with escapes decoded — and
a whole value, a pattern match or a masked tail showing a value's
character anywhere except the tool's own fixed wording (headings,
counts, page numbers) means no map (`MAP_SELF_CHECK_FAILED`).

**Limits of this first version.** It draws the masked map only. OCR hits
are listed, not positioned. A switched-off layer's content is not fully
checked (images and font-coded text on it are not read), so a document
with one is always MAP INCOMPLETE, even at exit `0`. The same holds for
any layer rule (an OCMD, or any `/VE` visibility expression), and for
any page whose MediaBox reaches past its CropBox on any side (a
smaller CropBox, or a shifted one): images there are not
checked, so the map says so instead of "Nothing visible found". A
switched-off layer is one MuPDF evaluates as off, or one the default
configuration's `/OFF` or `/BaseState /OFF` turns off (MuPDF's own
answer misses a layer that carries a `/Usage` dictionary). A solid text box
means the text is in the page's text layer — it does not say whether the
text is drawn, covered by a box, white on white or invisible; deciding
that from pixels is a later step. Annotations gated by a visibility
expression (an OCMD) are listed "behind a layer rule (may be visible)"
unless the page text shows them.

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
(`****6789`); when a sample contains one of your exact values, or any character it
would show is a character of one, only `****` is shown, so a report never shows
any character of an exact value (compared ignoring case, accents and punctuation,
for letters and digits of any script). The check is per character, not per
position, because the search can assemble a value from pieces across lines or
down a column; with numeric exact values, most numeric samples therefore show
only `****`. Characters that merely look
like one another (a Greek or Cyrillic letter for a Latin one) are not
recognised as equal, so a sample read by OCR, where such misreadings happen,
is always fully masked (`****`), in the report, `--json` and warnings.

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
| 4 | **Hidden** | no page renders at all | attachments, annotations, form fields, links, JavaScript, layer names read and matched; page thumbnails flagged | PyMuPDF |
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
  crop box ∩ the media box) is read too, as its own reading — invisible
  to a viewer, but still in the file — and kept apart from the visible
  text so it cannot come between the halves of a value split across a
  page break. It is also read together with the visible text, across the
  visible edge, so a value that starts on the page and runs off it, or
  that is written vertically off the page, is read whole.
- **Readings:** seven *genuine* readings — the visible text, written
  horizontally (read left to right) or vertically (rotated, read along its
  own direction both ways); the off-page text; and the same three
  directions across the visible edge, visible and off-page text together —
  plus three *reconstructed* readings: every visible glyph in vertical
  columns, both ways, and all the horizontal text, visible or not. The
  columns are the only reading of a value written
  one character per line, but a column of an ordinary page also stacks
  unrelated lines (a numbered list's numbers), so reconstructions only
  ever give manual-review warnings.
- **Across pages:** each page break is checked on its own. A value running
  from the last line of one page onto the first line of the next, in a
  genuine reading of the visible text (a visible line read on across the
  visible edge included) or of the off-page text, is hard. Any other
  match crossing the break — needing other lines, across a blank or
  unreadable page, or from text wholly off the page (a slug below the
  page, or beside visible text across a column break) onto the next
  page's first line, or onto text wholly off the next page, above its
  top edge — is a manual-review warning at the page it crosses into. A
  value wrapped from a page's last visible line onto a line wholly below
  the page is a manual-review warning too. The limit: a slug only a word space from visible text on its
  baseline ('Page 7' then 'Job 123-45' just past the edge) reads as one
  line with it, exactly like a value running past the edge, so if it and
  the next page's first line together form a value, that is a (false)
  finding.
- **Tier:** a known value is hard on one line of a genuine reading, or at
  a page seam as above. A pattern is hard only on one line of a genuine
  reading, the readings across the visible edge included. On a line read
  across the edge, a wide gap (a column break) keeps a visible line and
  an off-page slug on its baseline apart: the slug is left to the
  off-page reading. A value the two form across that gap is a
  manual-review warning (the owner's decision, 2026-10-11): never a
  finding, never silent, and by design also raised on a clean page whose
  page number or footer and a slug past the edge happen to form a value.
  A narrower gap does not keep them apart: the two read as one line,
  like a value running past the edge. Everything that needs text joined
  is manual-review.

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
  object's dictionary is always scanned, and so is every trailer
  dictionary — the document `/ID` and any other key, in the current
  trailer and every earlier revision's — which is not an object (a
  pattern-rule match in a `/ID` element written as nothing but hex
  digits is manual review: such generated identifiers hold digit runs by
  coincidence). References are followed from the file's
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
  A page carrying a `/Thumb` preview image (on the page, or on a Pages
  node above it) is flagged `PAGE_THUMBNAIL` (exit `2`): the thumbnail is
  not decoded or OCR'd, and can still show what the page no longer does.
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
  itself unavailable and the run never exits `0`. A page where Vision
  raises an error is a `PAGE_FAILED` warning, so the run never exits `0`
  (`2`, or `1` if another layer found a leak). (Before the fix in the
  CHANGELOG, this happened to every page on macOS 27.) A page where
  Vision reports success without delivering both readings is also
  `PAGE_FAILED`: a reading counts only once its request's completion
  handler has run to the end. So is a page where a Vision call does
  not return within 120 seconds (a stalled Vision, #115); while that
  call still has not returned, the later pages fail at once instead of
  each waiting, so the run ends with exit `2` (or `1`) instead of
  hanging. A page whose 300 dpi render is longer than 3510 px on a side
  (over about 11.7 in: legal, A3, tabloid, plans and posters; letter and
  A4 pages are not) is read, and anything found on it counts, but it also gets a
  `PAGE_FAILED` warning, so it never exits `0`: on such a render Vision
  reads less and less of the smaller print, and then nothing, without an
  error (#128). Otherwise a page Vision reads without error but
  with text missing is not detected; see DESIGN's known limitations.
- **Tier:** same rules as the Text layer, with both readings eligible for
  hard findings — except a line Vision reads vertically that an upright
  re-read does not confirm. On macOS 27, Vision returns a column of
  characters written one per line as a single line read sideways, often
  misread (`199140190` for the digits 1 to 9). So a vertical line's box
  is turned upright and read again: rotated text reads back the same, a
  stack usually does not (a stack of `0`, `1`, `8`, `O`, `I`, `H` can
  repeat its misread: #65). Like the Text layer's reconstructed
  readings, a match in an unconfirmed line is only ever a manual-review
  warning. A page gets at most 64 re-reads, retries included; vertical
  lines past that are manual review, and so, sometimes, is small slanted
  text near a page edge whose re-read does not match. A page with any
  vertical line is also read turned 90° and 270° (Vision drops lines of
  sideways text), and each unconfirmed vertical line is unstacked into
  one line and read; both only ever give manual-review warnings.
- **Not yet covered:** OCR reads the rendered page, so a box drawn *over*
  a scanned image hides the pixels underneath from it, even though they
  are still stored in the file. An image placed outside the visible page
  area is not rendered, so OCR does not see it either. A scanned value
  written one character per line that Vision does not report as a line
  at all passes as clean with a value rule (#81); on macOS 26 so does
  the stack that macOS 27 unstacks (`layout.scanned-vertical-stack`,
  K42), since Vision there reads it differently; a page Vision reads
  with lines missing passes too, unless a line on it is read vertically
  (#67, #82).

### 6. Binary — the raw decompressed file

A cross-check using a second PDF parser: qpdf can recover objects from a
damaged cross-reference table, and it decompresses attachment contents.

- **Method:** qpdf (`qpdf --qdf --object-streams=disable
  --deterministic-id`) rewrites the objects reachable from the file's root, decompressing their streams (image
  codecs such as JPEG stay encoded); the output is streamed through the
  same normalized substring search, for value secrets only. Regex patterns
  are not run here — raw bytes produce too many coincidental matches.
  Unreferenced (orphaned) objects and superseded object versions are
  **not** in qpdf's output; the Objects layer covers those.
  Decompressed font programs *are* in it, and their tables contain byte
  runs such as `123456789`, so a sequential value can raise a coincidental
  Binary warning on a clean document. qpdf also writes a document `/ID`
  of its own in the trailer (keeping the file's first element, generating
  the second, or both if the file has none or its first element is
  empty). The tool asks for a deterministic one (`--deterministic-id`),
  so they no longer change from one run to the next, and blanks the
  elements qpdf generated before scanning whenever it can tell them apart
  from the file's own (when it cannot, as for a file whose first `/ID`
  element is empty, they are still scanned, and a coincidental match is
  the same on every run).
- **qpdf's exit 3** ("succeeded with warnings") makes the run exit `2`
  (`TOOL_EXIT_NONZERO`) unless every warning qpdf printed is one of five
  kinds the tool can check against the file's own bytes and the check
  passes (owner decision 2026-10-15): a `(0 0 R)` reference, an xref
  entry at offset 0 with no stored body of that object anywhere, a
  duplicated key with identical values, a missing `endobj` followed only
  by whitespace, or a Flate stream without its end marker that is not an
  object stream (an xref stream only if every row decodes). Any other
  warning, including a rebuilt cross-reference table, still exits `2`
  (COVERAGE.md, footnote 16).
- **Tier:** always manual-review, never a hard finding.

Each layer extracts text independently, so a blind spot in one extraction
cannot hide a leak from the others. What no tool can tell you is whether
your *rule set* was complete — this proves the redactor removed what you
named, not that you named everything.

### Known gaps

[COVERAGE.md](COVERAGE.md) is the full known map; its ✗ cells are where a
secret can still be **not detected** (exit `0`). In short:

- **Pixels the tool does not OCR:** under a box drawn over an image
  (K12), off the page (K14), in XMP thumbnails (K22, K23),
  in embedded files other than listed attachments (K27), and in image
  data beyond an image's declared size (rows past its declared height
  are never drawn, K37). One picture cut into thin strips, each too
  small to read alone, is also missed when a box covers the strips
  (K38), when they are not drawn next to each other, or when they are
  never drawn.
  <!-- gap-cells: live.pixels.under-box off-page.pixels metadata.pixels leftover-xmp.pixels embedded-other.pixels live.pixels.extra-rows -->
- **Content no page shows:** font-coded text (K15), outlines (K6) or
  images in a switched-off optional-content layer, a hidden
  annotation's appearance (K16, K17), or a page resource that is never
  drawn (K18, K19; plain text there *is* read).
  <!-- gap-cells: oc-off.font oc-off.pixels annot-appearance.font annot-appearance.pixels unused-resource.font unused-resource.pixels -->
- **Unindexed bytes:** anything after the final `%%EOF` (plain text K7,
  font-coded text K31, pixels K32, a container K33), in comments, or in
  an object whose table entry is marked free.
  <!-- gap-cells: unindexed.plain unindexed.font unindexed.pixels unindexed.container -->
- **Streams and leftovers nothing reads or flags:** text after a
  stream's compression end marker, in a live page (K1) or a leftover
  stream (K2); leftover plain text labelled as a font program or a 1×1
  image (K3, K4); leftover text converted to outlines (K5); a leftover
  image just under the 8×32 size gate, whether it is a picture still
  readable when enlarged, one picture cut into strips (K21), or raw
  sample bytes holding the value literally (a known value there is
  found only when the image is drawn); leftover font-coded text shown one character per string
  (K10); and a removed attachment whose text holds three words that are
  also content operators, so it is taken for page content (K11).
  <!-- gap-cells: live.plain.after-stream-end orphaned.plain.after-stream-end orphaned.plain.mislabelled orphaned.pixels.outlines orphaned.pixels.small orphaned.font.single-glyph-strings orphaned-attachment.plain.operator-words -->
- **Leftover text in a font that maps ordinary-looking codes to other
  glyphs** (K20), **containers encoded as text** (a base64 email part, an
  HTML `data:` URI; K25), and a zip container in an embedded file other
  than a listed attachment (K28).
  <!-- gap-cells: orphaned.font.ordinary-codes attachment.container.text-encoded embedded-other.container -->
- **Font-coded text no reading recovers:** off the page in an embedded
  font whose `/ToUnicode` map was stripped (K13), and text drawn from the
  same starting point as other text, so the two interleave glyph by
  glyph (K36).
  <!-- gap-cells: off-page.font.no-unicode live.font.overprinted -->
- **Pattern rules** on embedded files other than attachments (K26), on
  scripts stored as streams on links, fields or pages (K29), on an
  editor's private data, `/PieceInfo` (K30), and on a drawn image's own
  raw sample or codec bytes (known values are still found there, as
  manual review — a known value hidden in a drawn image's raw samples, or
  in a JPEG comment segment, is a Binary-layer manual-review warning
  today, but the same secret found only by a pattern rule is not).
  <!-- gap-cells: embedded-other.plain.pattern-rules javascript.plain.pattern-rules-in-streams private-data.plain.pattern-rules live.pixels.hidden-samples.pattern-rules -->
- **Image data beyond the main picture:** a JPEG's own EXIF (or other)
  thumbnail can hold a second, fully decodable image the tool never
  looks at — only the main picture is ever drawn or OCR'd (K39).
  <!-- gap-cells: live.pixels.exif-thumbnail -->
- **A dense sideways scan on macOS 27:** on a scanned page of 40 small
  lines turned 90°, Vision reports success but leaves some lines out,
  and nothing compares its reading with the ink on the page. OCR now
  also reads such a page turned, which finds the value, but only as
  manual review (exit `2`), not a finding (K43; on macOS 26 Vision reads
  the line and the page exits `1`). A page whose dropped lines are
  horizontal, or where Vision reports no vertical line, can still pass as
  clean ([#82](https://github.com/billylokhl/pdf-redaction-verifier/issues/82)).
  <!-- gap-cells: live.pixels.ocr-dropped-line -->
- **An oversized image:** OCR reads each page as a render at its usual
  resolution, so an image with far more detail than that render (60,000
  pixels across a page, say) is read only at the render's resolution,
  and small text inside it can pass as clean. Phase 4b bounds the image
  sizes it reads in full.
  <!-- gap-cells: live.pixels.oversized-image -->
- **Split values:** a value split around other text into more than three
  pieces, or with its pieces more than five lines apart (a page-break
  split with a header, footer or page number between its halves, a value
  wrapped inside one column of a multi-column page, or halves in reverse
  order are manual review, exit `2`); a letter-spaced value in fine print
  that the page's reading breaks into one-character lines around other
  text; a value wrapped over two fine-print lines closer together than
  the line tolerance, which merges them into one interleaved line; a
  page-break split whose halves are written in different directions (one
  horizontal, one vertical);
  dash-less pattern numbers wrapped at a line or page break; a value
  that only a pattern rule looks for, split at a page break where the
  split needs more than the last line before the break, runs over four
  pages, is rotated, sits in form boxes or is written one digit per line
  (a plain page-break split under a pattern rule is manual review, exit
  `2`); text at extreme coordinates (K35).
  <!-- gap-cells: match.letter-spaced-stack match.tight-fine-print match.mixed-direction-split match.undashed-wrap match.pattern-split match.extreme-coordinates -->
- **Name forms not listed:** a reordered name on one line ("TESTPERSON,
  JORDAN Q" in the document, "Jordan Q. Testperson" in the rules) and a fragment
  of a listed name (the first name inside an email address, when only
  the full name is listed). The tool matches the exact values you list,
  after normalization, and does not generate reorderings or search for
  parts of a name; list each form as its own value (see
  [Rules file](#rules-file)).
  <!-- gap-cells:  -->

Other leftover images, other leftover text in non-plain font codes,
attachments that are not text, and page thumbnails (`/Thumb` preview
images on any page) are **flagged** (exit `2`) rather than read: a
clean document containing them cannot certify as `0` until the tool
learns to read them.

Known false positives: a short value such as a 5-digit ZIP can be
assembled as a hard finding at a page seam (a page number followed by the
next page's first line), and so can a print slug just past the page's
edge beside the page number, joined with the next page's first line; a
value assembled down a vertical column or through several
page-number-only pages is only ever manual review; and
decompressed font tables, or an image's raw bytes, can raise
coincidental Binary warnings (see Binary); a document `/ID` written as
hex text whose digits happen to form an SSN, card or phone number is
manual review under a pattern rule. Prefer longer, more specific values.
In strict mode (`--strict-classes`) the built-in classes raise hard
findings on many clean files: on 488 real text-bearing PDFs with no secret
in them, `ssn` or `us-phone` did on 28.3%, mostly digit runs inside hex or
alphanumeric ids (eval/spikes/RESULTS.md, "Phase 5 baseline"). By default
those matches are notices that do not change the verdict (ADR 0012).
<!-- gap-cells: false-alarm.slug-beside-visible-line false-alarm.binary-value-collision false-alarm.generated-id-digits -->

A rewrite that closes these gaps by accounting for every byte of the
file, rather than searching the places this tool knows about, is planned
in [docs/REDESIGN.md](docs/REDESIGN.md); its Phase 1 design questions
are decided in [docs/adr/](docs/adr/) -- nine accepted decisions, each
backed by a measurement in [eval/spikes/](eval/spikes/).

## Rules file

`--secrets` accepts either this tool's JSON array or a
a redaction tool's `redact_config.yaml`
(selected by the `.yaml`/`.yml` suffix), so one file can drive both
redaction and verification without the two drifting apart.

### List every written form of a secret

A `value` rule matches exactly the value you list, and nothing derived
from it. The tool does not generate reorderings, nicknames, initials or
partial forms. A person who appears as "Jordan Q. Testperson" in the
rules is not found as "TESTPERSON, JORDAN Q" unless that form is listed
too. So list each form the document may use as its own value:

- **Names:** "Jordan Q. Testperson" and "Testperson, Jordan Q.", each
  with and without the middle initial ("Jordan Testperson", "Testperson,
  Jordan"), and any shortened form you want treated as a secret.
- **Email addresses and usernames:** the full address
  (`jordan.testperson@example.com`) and any login or handle derived from
  it (`jtestperson`, `jqtest`).
- **Numbers:** only forms that differ in their digits, such as an account
  number also printed with a leading branch code or cut to its last four
  digits. Forms that differ only in separators need no entry.

Matching already ignores case, punctuation, spacing and accents, and
folds fullwidth characters (see [How a match is found](#how-a-match-is-found)).
So do **not** list "JORDAN Q TESTPERSON" next to "Jordan Q. Testperson",
or "123 45 6789" next to "123-45-6789": each pair is one value to the
tool. A listed value is also found inside longer text, so a listed
"Jordan Testperson" is found within `jordan.testperson@example.com`,
because the address reads "jordantestpersonexamplecom" once normalized.
Keep each value long enough to be specific; a bare first name is found
in every word that contains it, and across word boundaries.

### JSON

Each entry has a `name` and exactly one of:

- `"value"` — a known secret string, matched through normalization.
- `"class"` — a built-in class: `ssn`, `credit-card`, `email`, `us-phone`.
  Optional `"strict": true` keeps this class's matches in the verdict
  (ADR 0012; by default they are notices); a `"strict"` that is not `true`
  or `false` is a rules error unless `--strict-classes` is given. On a
  `value` or `pattern` rule the key is ignored, as any unknown key is.
- `"pattern"` — a custom regex (`re.MULTILINE`; names must be unique).

### Shared YAML config

| YAML key | Becomes |
| --- | --- |
| `exact_values` | value rules (normalized matching) |
| `patterns` | pattern rules, compiled `IGNORECASE` — the same flags the redactor uses |
| `entity_types` | this tool's built-in classes — `ssn`, `email`, `phone`→`us-phone`, `credit_card`→`credit-card`. Like any built-in class, their matches are informational notices by default; `--strict-classes` makes the config's entity types count toward the verdict (a YAML config has no per-type `strict`) |
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

Python 3.10+ (CI tests 3.10, 3.12, 3.13 and 3.14); dependencies, including the
macOS-only Apple Vision OCR bridge, are declared in `pyproject.toml`. The
Metadata and Binary layers shell out to `exiftool` and `qpdf`; without
them those layers degrade and the run exits `2` rather than certifying
clean. OCR is macOS-only; elsewhere that layer reports itself unavailable
and the run exits `2`.

conda installs everything in one command (conda-forge ships the binaries):

```bash
conda env create -f environment.yml && conda activate pdf-redaction-verifier
```

Dependencies are hash-locked in `uv.lock`, resolved for macOS and Linux on
Python 3.10–3.14 (the pyobjc packages stay macOS-only via their markers).
With [uv](https://docs.astral.sh/uv/), install the two binaries separately:

```bash
brew install exiftool qpdf && uv sync --extra test
```

On Debian/Ubuntu the binaries are
`sudo apt-get install libimage-exiftool-perl qpdf`. Drop `--extra test` if
you do not need the test suite. The inventory's reader-agreement tests
(Phase 3a) compare against both the `qpdf` command and the libqpdf inside
`pikepdf` (a runtime dependency, which the shipped CLI does not import
yet), and need the two at one major.minor
version: 12.4 with Python 3.11+, 12.3 with Python 3.10. Homebrew's qpdf
12.4 matches; Debian/Ubuntu's packaged qpdf is older, so install the
matching release binary from qpdf's GitHub releases, as CI does
(`.github/workflows/tests.yml`). With a mismatch those tests fail closed
and the inventory gate refuses to run. qpdf's macOS release binary reads
RC4-encrypted files (and AES-128 ones, whose password check uses RC4)
only if it finds OpenSSL's legacy provider, in the modules directory it
was built with; `verify.py` runs qpdf with a stripped environment, so
`OPENSSL_MODULES` does not reach it there. CI links that built-in
directory to Homebrew openssl@3's `lib/ossl-modules`; do the same
locally, or use Homebrew's own qpdf, which needs nothing (#171). Without the provider, qpdf exits 2 on those files:
the Binary layer is flagged as not scanned, and the gate reads a split
on encryption. Without uv, `pip install -e '.[test]'`
still works (from `pyproject.toml` directly, not the lock file).
`python verify.py --target ... --secrets ...` works installed or not; pass
`--fail-fast` to stop at the first confirmed finding. `verify.py` imports
its pure parts from the `redaction_verifier` package (`docs/REDESIGN.md`
§4/§6), so it must either be installed (`pip install` / `uv sync`, as
above) or run from a copy of the repository that still has
`redaction_verifier/` sitting next to it — copying `verify.py` out on its
own does not work.

[DESIGN.md](DESIGN.md) explains the design rationale — the exit-code
contract, the two-tier matching model, and the known limitations.

## Development

Requires uv 0.12.3 (`pyproject.toml`'s `[tool.uv].required-version` pins
the same range CI installs — a mismatched uv refuses to run rather than
resolving or installing differently against the same lock file):

```bash
uv sync --extra test --group dev          # Install test + lint/type-check tools
uv run pytest                             # Run the test suite
uv run ruff check .                       # Lint code
uv run mypy                               # Type check (redaction_verifier -- strict on inventory/budget/ledger/sandbox -- eval/caselib, eval/scorecard, eval/gallery, eval/check_ratchets.py)
uv run pytest tests/ --cov=verify --cov=caselib --cov=redaction_verifier  # Run tests with coverage report
```

Without uv: `pip install -e '.[test]'` plus `pip install ruff==<pinned>
mypy==<pinned>` (see the `dev` group in `pyproject.toml` for the pinned
versions), then drop the `uv run` prefix from the commands above.

Every regression test pins a previously confirmed bug; fixtures are
generated on the fly, nothing binary is committed, and tests needing
Apple Vision, `exiftool`, or `qpdf` skip with a reason when the tool is
absent. CI installs from `uv.lock` (`uv sync --locked`, which fails the
build if the lock is out of date with `pyproject.toml`) and also runs
ruff (correctness) and mypy (types, on `redaction_verifier` and the
evaluation harness — `eval/caselib`, `eval/scorecard`, `eval/gallery`,
`eval/check_ratchets.py`; `verify.py` itself is excluded), and reports
coverage on the Linux Python 3.12 job; the commands above run the same
checks locally. Property tests use Hypothesis: with `CI` set (as on
GitHub Actions) they run the derandomized `ci` profile, locally the `dev`
profile explores fresh examples each run and replays any failure it
found from `.hypothesis/` until it passes (each one root-caused, then
pinned as an explicit test); `HYPOTHESIS_PROFILE=ci` reproduces CI exactly. Two suites go further and test robustness directly:

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
reviewed explicitly in `eval/accepted_diffs.yaml`. It runs one scan per
usable core by default (fewer if RAM is short), so its per-file timeout
measures the file rather than contention — see
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
| **Genuine reading** | Page text read in the direction it was written (horizontal, rotated, or an OCR pass) — on the page, off it, or across the visible edge; its matches can be hard findings. |
| **Guard** | A check in the code that prevents a known bug. |
| **Hard finding** | A match strong enough to count as a confirmed leak (exit `1`) — see each layer's *Tier* for the exact rule. |
| **Incremental update** | Edits appended to the end of a file; earlier revisions stay inside it, and no layer reads them (see Known gaps). |
| **Info / XMP** | The two places PDFs store metadata: the older Info dictionary and the newer XML block. |
| **Layout-aware** | Rebuilds lines from where characters sit on the page, not the order the file draws them. |
| **LLM** | Large language model — the AI the redactor uses to find names and addresses. |
| **Luhn** | Checksum built into credit-card numbers; random 16-digit strings usually fail it. |
| **Manual-review warning** | A possible match that needs a human to check it; exits `2`. |
| **Masked** | The report shows only the tail of a match (`****6789`) so it doesn't leak the secret itself. If the match contains one of your exact values, or any character the tail would show is a character of one (case, accents and script-specific digits ignored), only `****` is shown; samples from the OCR layer always show only `****`. |
| **Mutation testing** | Deliberately breaking a guard to prove the tests notice. |
| **NANP** | North American Numbering Plan — the rules for valid US/Canada phone numbers. |
| **NFKC** | Unicode step that folds look-alike characters (fullwidth digits, fancy dashes) to plain ones without splitting accents. |
| **NFKD fold** | Unicode step turning look-alike characters into plain ones (fullwidth `１` → `1`, `é` → `e`). |
| **Normalized** | Text reduced to a canonical form before comparing, so formatting can't hide a match. |
| **Object streams / xref stream** | The PDF 1.5+ layout that packs many objects into one compressed container — the default for Acrobat and Chrome. |
| **OCR** | Optical character recognition: reading text from pixels. |
| **Octal coercion** | YAML gotcha: an unquoted `00123456` is read as a base-8 number, changing its value. |
| **Optional-content group** | A PDF "layer" that can be switched on or off; its name alone can leak data. |
| **ORPHANED** | Label for a finding in an object nothing references — present in the file, never displayed. The terminal report tags it `left over`. |
| **Outlined vector text** | Letters converted into drawn shapes: looks like text, isn't text data. |
| **PyMuPDF** | Python library for reading and rendering PDFs; most layers use it. |
| **PyObjC** | Bridge that lets Python call macOS frameworks such as Apple Vision. |
| **qpdf** | External command-line tool that rewrites a PDF's reachable objects with their streams decompressed. |
| **Reconstructed reading** | Every glyph stacked into vertical columns — catches a value written one character per line, but its matches are only ever manual review. OCR's equivalent is Vision's own reading of a vertical line no upright re-read confirms. |
| **Regex** | A text-search pattern language. |
| **Rendered page** | The page drawn as an image — what a viewer sees. |
| **Sidecar** | A small companion file next to a PDF listing the secrets it contains. |
| **Substring search** | Checking whether one string appears exactly inside another. |
| **Surface** | One real piece of text: a line, a decoded PDF string, a form or annotation field. |
| **UTF-16** | A text encoding PDFs use for non-ASCII strings; the Objects layer decodes it. |
| **Validator** | A check that a match is structurally real (e.g. SSNs never start with `000` or `666`). |
| **Visible map** | The masked PDF `--visible-map` writes: where each finding is, and how a reader would come across it. A picture of the scan, never a verdict. |
| **Switched-off layer** | An optional-content group the document's default configuration turns off: its content is in the file but not drawn until a reader switches it on. |
| **Marker** | The line `%PRV-VISIBLE-MAP-1`, written as a visible map's second and last line; only a file carrying both is ever replaced. |
| **Walked structurally** | Reading objects one by one through the PDF's own structure rather than scanning raw bytes. |