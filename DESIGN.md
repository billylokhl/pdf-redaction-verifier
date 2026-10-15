# Design

Why this tool is built the way it is. For *how to use it*, see the
[README](README.md); for *what it does*, read `verify.py` and the
`redaction_verifier/` package it imports its pure parts from (see
`docs/REDESIGN.md` §4/§6 for why the code is split that way).

## Problem

A redaction tool draws a black box. It does not necessarily remove the
text underneath, the copy in the metadata, or the original object it
replaced — still in the file, unreferenced. Someone then has to answer: **is this
document actually safe to release?**

Answering it by opening the PDF and looking is exactly the method that
fails, because the failure modes are invisible to a reader. This tool
answers it mechanically, and its verdict is meant to gate a release.

## The central invariant: fail closed

Everything else follows from this. Three exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Certified clean — every layer ran, nothing found |
| `1` | A secret or pattern was found |
| `2` | Cannot certify — an error, a layer that could not run, content found but not readable, or a match needing human review |

**A `2` is not a pass.** If `qpdf` is missing, if the OCR bridge fails to
import, if qpdf exits non-zero and may have truncated its output, if a
match is plausible but might be coincidence — the answer is `2`, never
`0`. A scanner that silently downgrades coverage and still says "clean"
is worse than no scanner, because it converts an unknown into a false
assurance.

The same rule governs the tool's own crashes, not just the document's
content: an exception that escapes every per-layer guard — a bug in the
report writer, say — must never be certified `0`, and must never read as
`1` either unless a secret had already been confirmed before the crash.
`main()`'s entire scan pipeline runs under one last `except BaseException`
(not `Exception`: a stray `SystemExit` must not carry its own arbitrary
code out) that answers `2` if nothing had been found yet, or keeps `1` if
something had — the severity order is `0 < 2 < 1`, so a crash can only
ever push the verdict *up* from a clean-so-far `2`, never take back a
confirmed leak. The `--json` report still records the crash
(`error.code = "INTERNAL_ERROR"`) alongside whatever findings and
warnings had already been collected.

The rule has two halves, and the second took longer to see. A layer that
cannot *run* is obvious. A layer that runs, *finds* content, and cannot
*read* it is not — a leftover content stream in glyph codes, a leftover
image, a zip attachment — and for a long time the tool passed those
silently. Several rounds of adversarial review kept finding new instances
of that one flaw, because coverage had grown from what each extraction
method happened to decode rather than from a model of where data can be
stored. [COVERAGE.md](COVERAGE.md) is that model: the known places a secret
can be stored, crossed with how it can be encoded, each cell read (✓),
flagged as unreadable (⚑, exit `2`), or an admitted gap (✗). It is a
known map, not a proof of completeness — review still finds new rows — so
a new place is added as ✗ first. A flag is only ever removed by learning
to read the cell.

The corollary is that `1` must be trustworthy too. A tool that cries wolf
on clean documents gets ignored, and then it may as well not exist. Most
of the design below is about keeping `1` and `0` both honest, with `2` as
the pressure valve for everything uncertain.

## Six independent layers

No single extraction method sees everything, so six run and any one can
raise a finding.

| Layer | Sees | Catches what the others miss |
| --- | --- | --- |
| **Text** | Text objects, positioned | Text under a redaction box; glyphs drawn out of order |
| **OCR** | Rendered pixels (Apple Vision) | Text with no text objects: scans, vector outlines |
| **Metadata** | exiftool fields + the XMP packet | Copies in Info/XMP that no reader displays |
| **Objects** | PDF objects walked structurally (PyMuPDF) | Orphaned content streams, dictionary strings |
| **Binary** | qpdf's decompressed rewrite of the reachable objects | Objects qpdf recovers from a damaged file; decompressed attachment bytes (value rules only) |
| **Hidden** | Attachments, annotations, form fields, links, scripts, layer names (PyMuPDF) | Content no page renders at all |

They are independent on purpose: a leak that defeats extraction usually
does not also defeat rasterization, and vice versa.

The Hidden layer exists because a PDF carries more than its pages.
Acrobat splits its own tooling the same way — redaction removes visible
content, and a *separate* "Remove Hidden Information" pass handles
attachments, annotations, form data, scripts and layers — and its
documentation notes that users routinely assume the first step did the
second. An attachment is the case that forces a dedicated layer rather
than trusting the qpdf sweep: qpdf's rewrite does decompress it, but
there a match is anonymous, value-rule-only, manual-review, and present
only when qpdf is installed. Annotations and link targets are likewise
visible to qpdf, but surface as anonymous bytes rather than naming the
carrier. This layer needs no external
binary and says exactly where the leak lives.

It honours the two-tier model rather than claiming exemption from it.
Discrete field values — a filename, an annotation's text, a form field,
a link target, a script body — are hard findings, and each is matched on
its own so one carrier's tail cannot join the next one's head. Attachment
*bodies* are not discrete: they are runs of arbitrary text whose lines
fuse exactly like page text, and a binary attachment decoded as text is
mojibake that reliably synthesizes e-mail-shaped matches. Those are
demoted to manual review, like every other fusion-prone surface.

Two mistakes here are worth recording because both produced confident
wrong answers. Scanning object *source* for the substring `/JS` fed whole
object dictionaries to the matchers, so an ordinary widget `/Rect`
normalized into a Luhn-valid digit run and hard-FAILed clean Acrobat
forms — while missing scripts stored as streams, which is how producers
store anything non-trivial. The fix is to walk the action graph and take
only the `/JS` value. And locations must carry identity, never document
text: an attachment named after the secret printed it in cleartext,
because locations are never masked. The metadata and
binary subprocesses start before the in-process layers so they run
concurrently.

## Why literals are decoded structurally

The Binary layer originally found PDF string literals by running a
PDF-syntax regex across qpdf's whole byte stream. That stream interleaves
structure with image data, and a text parser cannot tell them apart:
roughly one byte in 256 of a JPEG is `(`, which stalls a literal scanner
exactly as a real unclosed string would. A 64KB carry cap contained the
stall, and a warning reported it — so most image-bearing PDFs could never
reach a certified-clean verdict.

Two attempts to silence that warning by inspecting the dropped bytes both
failed, in opposite directions, and are worth recording. The raw sweep is
not an equivalent backstop: it is never given pattern rules at all, and
`normalize_string` keeps an octal escape's digits, so `(Jos\351 M\374ller)`
reduces to `jos351m374ller` rather than `josemuller`. And no content-class
heuristic separated signal from noise — `\xfe\xff` fires on image bytes
about once per 64KB while the real UTF-16 form qpdf emits, `\376\377`, is
never matched.

Walking objects removes the category error instead of compensating for
it. Every unit is bounded and typed: a dictionary is always text, a
stream body arrives decompressed, and a body that holds program data is
never parsed as text. No carry, no cap, no truncation warning.

What counts as program data is read from the object's own `/Subtype`,
`/Type`, `/Length1` and `/Filter` keys, never by searching its dictionary
source for a marker. `/Image` occurs as a substring of the
`/ProcSet [/PDF /Text /ImageB /ImageC /ImageI]` array that countless
producers emit on ordinary text-bearing Form XObjects, so a substring
test skipped those and lost their text silently. Excluded by key:
image samples (`/Subtype /Image`, image-codec filters), font programs
(`/Length1`, `/Type1C`, `/CIDFontType0C`, `/OpenType`) — a TrueType
`glyf` table tokenizes into literals whose bytes normalize into digit
runs, which decoded two hard SSN findings out of a clean real-world document —
embedded files, which the Hidden layer scans at the manual-review tier
arbitrary binary deserves, and the `/ObjStm` and `/XRef` containers,
whose bytes are other objects' serialized form. A stream that passes
the key test is still skipped if under 85% of its first 64 KB is
printable. That catches marker-less binary, but it also skips a content
stream carrying a large inline image — text and all (see Known
limitations). Only stream *bodies* are skipped; every object's
dictionary is scanned.

A classic trailer dictionary is not an object, so the object walk never
reached it, and qpdf writes only the current trailer, with the file's
first `/ID` element hex-encoded and the second replaced by its own: a
secret in the document `/ID`, in a private trailer key, or in an earlier
revision's trailer was certified clean (K40, K41 in docs/REDESIGN.md
§8). Trailers are now read from the file's own bytes: every trailer the
cross-reference chain reaches (the current one, `live`; an earlier
revision's, `superseded`; a hybrid file's `/XRefStm` stream with its
section), then any other `trailer << ... >>` in the file (`unreferenced`; a
dictionary with no keyword before it, or junk between the two, is
unindexed bytes, not read — issue #76;
one that never closes is a warning, and so is a chain section whose
trailer cannot be read). Comments are skipped when reading a trailer's
strings — a `>>` or `(` inside one cannot cut the dictionary short —
and read themselves as raw text at manual review. A cross-reference stream's
dictionary is an object the walk reads, but nothing references it, so it
used to be called `ORPHANED`; the chain now names it as the trailer.
Each trailer is matched like any object dictionary, with one exception,
the owner's decision on its tier: a pattern-rule match in an element
of the dictionary's own top-level `/ID` made only of hex digits and
dashes is manual review (a value split across its elements still gets
the adjacent-literals review). The spec
expects `/ID` to be generated, and a 32-character hex-text identifier
holds a run some built-in class accepts about one time in thirteen;
readable text there (`SSN 123-45-6789`) stays a hard finding, and a
known value is a hard finding anywhere in a trailer.

Only PDF string tokens are matched, decoded as latin-1 or as UTF-16 when
they carry a byte-order mark. Text shown through a CID font with
Identity-H encoding — standard for Word, Chrome and any embedded
TrueType — is stored as glyph IDs, which this layer cannot map back to
characters.

Literals are read the way a PDF parser reads them, tracking nesting
depth. `(SSN (mine): 123-45-6789)` is one string whose text contains
parentheses; the spec requires escaping only unbalanced ones. A regex
that forbade `(` inside the body matched the inner `(mine)` instead and
dropped everything after it, so a legal content stream could hide a
secret in plain sight. An unterminated literal still yields nothing:
its extent is unknowable, and guessing one would fuse the rest of the
object into a token that hard pattern rules could match across.

qpdf still runs, as a cross-check over the objects it rebuilds: it can
recover objects from a damaged cross-reference table, and it decompresses
attachment streams. It is *not* an orphan backstop — `qpdf --qdf`
rewrites only objects reachable from the trailer, so unreferenced objects
and earlier revisions of an incrementally saved file are absent from its
output. Its matches stay manual-review warnings, because a byte-level
match can be coincidence. It runs with `--deterministic-id`: otherwise
qpdf writes a trailer `/ID` hashed from the current second into the
output being swept, and a short all-hex value matched it at random — a
verdict that depended on the clock, not the file.

Only the file's bytes are scanned there, not bytes qpdf invents. The
`/ID` qpdf writes is now the same on every run, but it is still not in
the file: qpdf keeps the input's first element and generates the second
(both, if the file has none or its first is empty), so the generated
elements are blanked before the scan. Blanking is deliberately narrow:
only in a trailer of exactly qpdf's shape at the very end of its output,
only elements that are none of the input's own `/ID` elements, and only
when the input accounts for qpdf's first element — either it is one of
the input's own (qpdf keeps a non-empty first element), or no `/ID` was
found in the input and qpdf's two elements are identical, as qpdf writes
them when it generates both. The second rule matters because qpdf reads
`/ID` keys this tool's parser does not (`/I#44`, `/ID` followed by NUL):
it keeps the file's first element and generates a different second one,
so nothing is blanked. Nor is anything blanked for a file whose first
`/ID` element is empty: qpdf replaces that element, so it is not the
input's own, and the input did have an `/ID`, so neither rule holds.
Anything unexpected leaves the bytes as they are: a mistake costs at
most a false alarm, now the same on every run. The one file element that
can still be blanked is one byte-for-byte equal to the `/ID` qpdf
generates for that same file.

A finding names its carrier and says whether the document still
references it. An object nothing references is the founding failure mode
— a redactor that drew a box and left the original content stream
behind — so the `ORPHANED` label has to be earned. Reachability is
walked from the trailer, not guessed from the page tree: annotations,
form fields, appearance streams, `/Info` and the name tree hang off the
catalog rather than off a page, and a page-only walk called 61 of 73
findings on a real document orphaned. When the walk fails, the label is
dropped rather than applied to everything.

## Matching: two kinds of rule

### Value rules — a known secret

Both the secret and the extracted text are **normalized** before
comparison: NFKD decomposition, combining marks stripped, casefolded,
then reduced to alphanumerics. `123-45-6789`, `123 45 6789`, and
`１２３－４５－６７８９` all become `123456789`.

This is aggressive on purpose. The threat is a *known* string appearing
in *any* rendering, and a specific 9-digit sequence appearing by accident
is vanishingly unlikely — so recall is worth far more than precision
here.

### Pattern rules — a class of secret

Built-in classes (`ssn`, `credit-card`, `email`, `us-phone`) and custom
regexes answer "is there *any* SSN in here", where the value is not known
in advance.

Normalization cannot be reused here, because it destroys the very
structure the pattern depends on. So patterns match raw text, with two
compensations: input is folded (NFKC plus a dash/space table) so Unicode
look-alikes cannot evade an ASCII regex, and each class carries a
**validator** — Luhn for cards, SSA area/group/serial rules for SSNs,
NANP for phones — that rejects structurally impossible matches.

Validators help unevenly. Luhn rejects about 90% of random card-length
digit runs, but the SSA rules reject only about 11% of random 9-digit
numbers — and the `ssn` class also matches any bare 9-digit run. For SSNs
it is mostly the two-tier model, not the validator, that keeps false
positives survivable.

## The two-tier model

This is the least obvious part of the design, and the part that took the
most iteration.

A class regex matches *shape*, so it fires on any coincidence with that
shape. Extraction manufactures coincidences: whitespace glyphs are
dropped during reconstruction, lines get joined, adjacent PDF string
literals sit back to back, pages concatenate. Fuse enough unrelated
digits and you can synthesize a Luhn-valid card out of a timestamp and an
invoice column.

The resolution is that **not all matches are equally trustworthy**:

- **Hard findings (exit 1)** for a pattern come only from surfaces where
  the matched characters were genuinely adjacent in the document: one
  visual line of a genuine page reading (horizontal, rotated, off-page or
  across the visible edge) or of either OCR reading, one decoded PDF
  literal, one metadata value.
- **Soft findings (exit 2, manual-review warnings)** come from anything
  that required fusing separate things: multi-line text, vertical column
  reconstructions, adjacent literals, page boundaries.

Demotion is never silence: a match the tool finds by joining text is
reported as a `2` demanding review rather than a `1` asserting a breach,
which is the correct confidence for that evidence. (What the tool cannot
see at all is a different matter — see Known limitations.)

**Value rules relax two joins, and only two.** A known value is exact and
specific, so a split at a *structural* seam is treated as deliberate, not
coincidental: across the strings of one PDF object, and from the last
line of one page onto the first line of the next page, in a genuine
reading of the visible or the off-page text — the natural continuation
of the text. Every other join stays
manual review: two lines within a page, a vertical column of an ordinary
page, a page break reached only by also joining other lines, or a break
across a blank, image-only or unreadable page (what lies between the
halves is unseen).

Each page break is checked on its own. For every reading the tool keeps
the last few characters read so far, and asks one positional question at
each break: does a secret *straddle* this join? Asking by position rather
than by which names appear on each side matters twice over: a whole copy
of a value on one side cannot mask a separate copy crossing the join, and
every place a value crosses a break is reported where it happens. Earlier
designs got this wrong in instructive ways — a rolling scan fed straight
to the hard tier escalated same-page joins; a seam-only check lost values
spread over three pages; and suppressing the backstop by secret name let
one coincidental warning hide a real split elsewhere in the document.

**Fences** are how the hard tier stays honest. The class regexes allow at
most one separator character between digit groups, so inserting *two*
newlines between two pieces of text makes it impossible for one match to
span both. Decoded literals are fenced this way; so are metadata values;
and reconstructed lines are split at column gaps.

Detecting a column gap is subtle, because "wide gap" alone is wrong: a
per-character form box spaces every glyph widely, and its digits *are*
one number. The distinguishing signal is that form-box gaps are wide but
**uniform**, while table columns are wide **outliers** against tight
intra-cell spacing. So a gap breaks a line only when it is both a large
outlier on that line and wider than a character. Ordinary word spaces are
narrower than a character and never break.

## Layout reconstruction

Content-stream order is not reading order. Form-box digits are frequently
drawn out of sequence, so naive extraction returns `478593612` for a
document that plainly reads `123456789`.

Reconstruction sorts glyphs geometrically: cluster into lines by
perpendicular position, then order along the line. The clustering
tolerance **scales with glyph size** rather than being a fixed constant —
a fixed 4pt tolerance split 30pt digits with 6pt baseline jitter into
interleaved pseudo-lines — with a ceiling so a large watermark cannot
inflate the tolerance enough to merge body text.

Text is read along the direction it was written. PyMuPDF reports each
line's writing direction, so horizontally written glyphs form the
horizontal reading and glyphs written vertically (a rotated matrix) are
read in their own columns, top-down and bottom-up: three *genuine*
readings, all eligible for hard findings. Text outside the visible area
(the crop box ∩ the media box) has a reading of its own, so a slug below
a page never becomes that page's last line, and the same three
directions are read again across the visible edge, visible and off-page
glyphs together, so a value that runs off the page, or is written
vertically off it, is read whole: seven genuine readings in all. The
horizontal one keeps only the stretches of a line between column breaks
that hold a visible glyph, so its last line is the visible text's last
line read on past the edge, and it feeds the hard page seam; an off-page
slug a column break away from a page number is left to the off-page
reading, but one a mere word space away cannot be told from a value
running past the edge, and is read with it (a known false alarm). The
vertical ones also read columns wholly off the
page, whose last column may be a slug that does not run on into the next
page, so they feed the seam only at the manual-review tier. Text wholly
off the page joined with the next page's first line is manual review
too. Across the edge, lines are told apart with the visible text's
tolerance, so a large off-page banner cannot merge two lines of visible
fine print. Three more readings are *reconstructions*, whose matches are
only ever manual review, for values and patterns alike. Two stack
*every* visible glyph into vertical columns: the only way to read a
value written one character per line, but on an ordinary page a column
stacks one glyph from each of many lines — a ledger's last digits, a
numbered list's numbers — which assembled the README's own example
value out of a clean numbered list. The third reads all the horizontal
text, visible or not, so a value formed by visible and off-page parts —
a column break apart on one line, wrapped onto an off-page line, or
carried over a page break onto off-page text above the next page — is
reviewed rather than silent (the owner's decisions, 2026-10-11), at the
cost, by design, of a review warning on the odd clean page whose
visible text and an off-page slug happen to form a value. Across the
edge, a line's column breaks, like its tolerance, are measured on the
visible glyphs alone, so off-page text on a table row's baseline cannot
fuse the row's visible cells into one number.

OCR is different: both Apple Vision passes (language correction on and
off) are genuine full-page reads of the same pixels, not reconstructions,
so both feed the hard tier. Correction-off is run precisely because the
language model can "correct" digits in codes and serial numbers.

One exception. On macOS 27, Vision returns a one-character-per-line
stack as a single line rotated 270° (in every case measured) — an
orientation it also gives rotated text — and reads the upright glyphs sideways, often wrongly
(`199140190` for the digits 1 to 9: a valid SSN shape that is not on the
page). So a line Vision reads vertically counts as a line of the page
only if an upright re-read confirms it: its box is cropped, turned 90°
and 270°, and read again, and a re-read in the same pass must contain
its text (one retry with a wider crop). Rotated text turned upright is
an ordinary line and reads back the same; a stack turns into a row of
sideways glyphs and usually does not. Shape alone
cannot tell them apart: letter-spaced or space-separated rotated text
is as sparse along the line as a stack. In the genuine readings an
unconfirmed line is split one character per line, as the Text layer's
genuine readings hold a stack, so nothing in it is hard; Vision's own
line is kept in two more, reconstructed, readings, so a value or
pattern in it is still manual review: their lines are matched as
written as well as with separators collapsed, and the cross-page pattern
scan reads the correction-on one. On 1,728 generated pages under
macOS 27 — 8 fonts, 8–24 pt, plain, letter-spaced and space-separated
rotated text, and stacks of `123456789`, `123-45-6789` and `SECRET`,
each as text and as a scanned image — no stack's genuine readings held
its value or matched a pattern, and 688 of the 692 rotated values
Vision read stayed hard (the other 4, including plain 16 pt Helvetica
at 90°, are manual review). Two limits: a stack of glyphs that look
alike turned sideways (`0`, `1`, `8`, `O`, `I`, `H`) can re-read as the
same misread and stay hard, a finding on a number not on the page
([#65](https://github.com/billylokhl/pdf-redaction-verifier/issues/65));
and a page gets at most 64 re-reads, retries included, so on a page of
sideways text (a scan inserted rotated) the lines past that are manual
review. A re-read Vision fails on confirms nothing (manual review); it
does not fail the page. Small slanted text is sometimes not confirmed
either: in a further 1,080-page sweep of a slanted SSN line (30–60° in
each quadrant, 7–14 pt, centred and at the page edges), 10 of the 1,047
lines Vision read, mostly 7 pt near an edge, were manual review. Lines
read horizontally, such as a row of form boxes, are not re-read.
Outside the grids, `page.digit-column` (clean: a lone digit column) and
`page.scan-image-sideways-90`/`-270` (an SSN that exists only as a turned
image) pin both sides: no hard finding from a misread column, and a hard
one from a genuine sideways scan.

A page with any line read vertically gets two more reconstructed
readings, which can turn some of what Vision misses there into manual
review (measured on macOS 27; not all of it, see the counts below).
The page is read again turned 90° and 270°: on macOS 27 Vision reads
sideways text in a page image only in part (a dense page of 40 lines
lost 14–21; the same page at 200 or 400 dpi, none) and reads it whole
turned upright (#67). Turning also makes a one-character-per-line stack a row
of sideways glyphs, which Vision misread as a number in 13 of 48 stack
pages, so a turned reading is never hard. And each vertical line no
re-read confirmed is unstacked: the runs of inked pixel rows in its box
are laid out left to right as one line and read (#61). In a stack
Vision reports as a line, that reads the value as written (21 of 23
generated stacks); in 25 of 48 stack pages Vision reported no vertical
line at all, and those are not read (#81). These counts are macOS 27's;
on CI's macOS 26, Vision reads `layout.scanned-vertical-stack` so that
it still passes as clean, and reads `page.dense-sideways-scan`'s SSN
line whole (a hard finding). The cost is noise on some
clean scans: turned, a column of repeated digits reads as a row, so a
questionnaire with sideways headers and answer columns of 1–5 matched
the phone pattern (manual review; 1 of 14 generated clean scans moved).

**The readings with their glyphs.** `views.text.visual_text_glyphs`
gives the same readings as `extract_visual_text`, built by the same code,
with every line as the indices of the glyphs it was read from, and each
glyph's character, box and visibility: the code runs once with each
glyph's character replaced by a tag naming its index, and since lines are
clustered, ordered and split at column breaks from geometry alone, the
tagged readings are the real ones line for line. It is for callers that
must say *where* the scan read something (the visible map) without a
second copy of the reading code; nothing in the scan calls it, and a test
checks its text equals `extract_visual_text`'s, reading for reading, on
every page of every case in the case library and on fabricated pages at
every rotation.

## Bounded resources

The tool must survive documents that are hostile or merely huge. qpdf
output is streamed in chunks rather than buffered, with rolling scanners
that keep only enough tail to catch matches spanning a boundary. Pattern
feeds are batched (per-literal regex sweeps measured ~100x slower), rules
already matched are skipped, and every subprocess has a deadline — qpdf's
stream is read under `select`, exiftool runs under `communicate(timeout)`
— so a stalled tool cannot hang the run. Report samples are
masked — at most four trailing characters, never more than half, and
none at all (just `****`) when the sample contains an exact value or its
revealed tail shares two consecutive characters (or a lone character,
or a single edge character) with one (compared both as the form the matcher
searches and as letters and digits of any script, case, accents and
punctuation ignored, non-ASCII decimal digits as ASCII; look-alike
characters from other scripts are not recognised, so OCR-layer samples,
where misreadings happen, never show a tail at all; the index of exact values is
built once per run) — and
control characters are stripped, so a crafted PDF cannot inject escape
sequences into the terminal and the report cannot re-leak what it found.
OCR's upright re-reads of vertical lines are capped at 64 per page,
retries included (at most 128 extra Vision calls): a page of sideways
text costs about 8–11 s and one of text slanted near 45°, whose crops
are squares around whole lines, about a minute, instead of under half a
second (measured on macOS 27), so a long sideways scan is slow, not
unbounded.
A page with a vertical line also costs two Vision calls on the whole
page turned, and up to 16 on unstacked lines, each capped at 4 Mpx (on
the #67 page about 0.3 s more than before, of about 5 s). The worst
case per page is about 147 Vision calls: 1, 64 re-reads of 2, 2 turned,
16 unstacked.
Each Vision call has a deadline of its own (`VISION_CALL_TIMEOUT_S`,
120 s; #115): `performRequests` has no timeout, and a stalled Vision
once held a test run for about 25 minutes with no CPU in use. The call
runs on a worker thread joined with that deadline; past it, its
requests are cancelled, the thread is left behind (a thread blocked in
Vision cannot be stopped from Python; it is a daemon, so it never holds
up the exit) and the page is `PAGE_FAILED`, a re-read or extra reading
included: a timeout is not one of the read failures those drop. While a
timed-out call has still not returned, every later call fails at once
without calling Vision, so a stuck Vision costs one deadline per run,
not one per call or page. A page whose calls each return in time can
still take up to its 147 calls' worth of deadlines; that is slow, not
unbounded.

The subprocesses themselves are launched defensively, not just bounded:
`qpdf`/`exiftool` are resolved once to an absolute path (`shutil.which`)
rather than letting `exec()` search `PATH`, the target is always passed
as that same absolute path so a filename starting with `-` can never be
read as an option, and exiftool gets `-config ""` plus its own documented
`--` end-of-options convention. `-config ""` is what actually stops
exiftool from consulting any config file at all — `EXIFTOOL_HOME`,
`HOME`, `HOMEDRIVE`+`HOMEPATH`, or its own *working-directory* fallback
once none of those is set — verified directly against the real binary in
`tests/test_cli.py` (a planted config registers a user-defined tag whose
value shows up as an unambiguous new JSON key when the config is actually
read; an earlier version of that test instead planted invalid Perl, which
exiftool — and, it turns out, even an explicit `die` inside a config —
only warns about on stderr while still exiting 0 with unchanged JSON
either way, so it could never have caught a regression). Both also run
with a minimal
environment (`PATH`, `LANG=C`, `LC_ALL=C`) from a private, empty scratch
directory, but that is defence in depth rather than the fix itself: if
`-config ""` were ever dropped, or a future exiftool version behaved
differently, the working directory it would then fall back to must still
not be the caller's own. Every exit from the scan that started them —
normal completion, an early `return`, or any exception — kills whichever
of the two are still running and removes that scratch directory
(best-effort: a cleanup failure is reported but never allowed to
overwrite an already-decided, already-printed verdict), so neither
survives as an orphan.

## Sharing a config with the redactor

`--secrets` also accepts a redactor's `redact_config.yaml`, so the file
that tells the redactor what to remove tells this tool what to look for.
That removes a drift risk, and introduces a subtler one worth naming.

Independence of *method* survives: `entity_types` are mapped onto this
tool's own class regexes and validators, never the redactor's patterns,
so a flaw in the redactor's detection cannot hide itself from the check,
and the six layers still look where the redactor may not have.

Independence of *scope* does not. A category the config omits is one this
tool never searches for, so verification proves the redactor executed its
instructions — not that the instructions were sufficient. Six of the ten
entity types are LLM-detected with no regex equivalent at all. Following
the fail-closed rule, those are reported as unverifiable and force a `2`:
the gap is stated rather than implied away.

## Known limitations

- **OCR is macOS-only.** Apple Vision has no portable equivalent here;
  elsewhere that layer reports unavailable and the run never exits `0`.
- **CI tests one macOS version** (`macos-latest`, currently macOS 26).
  Vision's behaviour can change with an OS release CI does not run yet.
  A change that makes Vision *raise* fails closed: the page is
  `PAGE_FAILED` and the run never exits `0`. macOS 27 did this to every
  page (Vision began removing keys from a copy of the image handler's
  options, which a PyObjC-bridged Python dict rejects) until the bridge
  switched to a native `NSDictionary`; see the CHANGELOG. A change that
  stops Vision delivering a reading — a request reported as performed
  whose completion handler never ran to the end, got no results at all
  (`results()` is `None`), or raised (handlers run on Vision's worker
  threads, as seen on macOS 27, so the bridge catches the exception
  there instead of letting it abort the process) — also fails closed. A
  change that makes Vision *read less* — fewer or empty results in a
  reading it does deliver, no error — does not: nothing the bridge
  checks tells an empty or partial reading from a page with that little
  text (a raster check — ink on the page, no text read — could catch the
  empty case, but none exists), so a secret only OCR can see would pass
  as clean (#67: on macOS 27, a dense sideways scan whose SSN line
  Vision drops exited `0`; pinned as `page.dense-sideways-scan`, K43 in
  docs/REDESIGN.md §8, now manual review through the turned readings
  above, which only a page with a vertical line gets; a raster check is
  #82). Today only running OCR tests (the case
  library, the OCR unit tests) on that OS can catch it, and only for the
  content they cover. A change in *how* Vision reads can also move
  verdicts: on macOS 27 Vision reads a one-character-per-line stack as
  one line, which made a misread number a hard finding until a
  vertical line had to be confirmed by an upright re-read (see the
  two-tier model above). That check is measured on generated pages
  only.
- **Password-protected PDFs are rejected** (exit `2`). A PDF encrypted
  with only an owner password opens without one and is scanned normally.
- **Pattern rules cannot span pages as hard findings** — they surface as
  review warnings. Value rules are hard only at a page seam (see the
  two-tier model).
- **Remaining silent misses** are the ✗ cells in COVERAGE.md: pixels the
  tool does not OCR (under a drawn box, off the page, thumbnails, other
  embedded files, an image's undeclared extra rows, one render split into
  strips a page never shows joined); font-coded text or images in
  switched-off layers, hidden annotations and never-drawn resources;
  unindexed bytes; leftover text in fonts that map ordinary-looking codes
  to other glyphs; text-encoded containers; a JPEG's own EXIF/other
  thumbnail, a second decoded picture the tool never looks at; pattern
  rules on non-attachment embedded files, on scripts stored as streams on
  actions, and on an image's own raw sample or codec bytes (a known value
  there is still found, via the Binary layer's raw byte sweep, when the
  image is drawn) and on an editor's private data; text after a stream's
  compression end marker; leftover plain text labelled as a font or an
  image, leftover outlines, leftover images just under the size gate,
  leftover one-character strings, and a removed attachment taken for page
  content; font-coded text off the page without a `/ToUnicode` map or
  drawn from the same point as other text; a zip in a non-attachment
  embedded file; and the matching limits — page-break splits with
  something between the halves, pattern-rule page-break splits in
  layouts no reading joins, multi-column wraps, dash-less pattern wraps,
  extreme coordinates. README's Known gaps lists each, with its K-number where it has one;
  a dense sideways scan on macOS 27 is manual review only (K43).
- **Flags cost certainty.** Leftover images, leftover text in non-plain
  font codes, and attachments that are not text are flagged (exit `2`),
  so a clean document that merely contains them cannot certify as `0`
  until the tool learns to read them.
- **Some false positives remain.** A short value (a 5-digit ZIP) can be
  assembled as a hard finding at a page seam — a page number followed by
  the next page's first line — and decompressed font tables in the
  Binary sweep can raise coincidental manual-review warnings for a
  sequential value such as `123456789`. Prefer longer, more specific
  values.
- **Custom regexes are trusted.** A pathological pattern can be slow; the
  built-in classes are anchored to avoid quadratic backtracking, but a
  user-supplied one is the user's responsibility.
- **A pattern class is a heuristic, not a proof.** `us-phone` will match
  a 10-digit order number that satisfies NANP rules. Tune the rules file
  to the document set.

[docs/REDESIGN.md](docs/REDESIGN.md) is the plan to replace enumeration
(searching the places this tool knows about) with an accounted ledger
(every byte of the file, every obligation discharged or flagged); its
[docs/adr/](docs/adr/) records the nine accepted Phase 1 design
decisions, with the real corpus measurements behind them.

## Testing

Every test pins a bug that was once real, and its comment says which. The
suite is the accumulated memory of the failure modes above — cross-page
splits, jittered form boxes, rotated text, fullwidth digits, truncated
qpdf output, path-based false positives, table-column fusion, PDF date
stamps passing Luhn.

Two properties are enforced structurally rather than by convention:
fixtures are generated at runtime so no sensitive binary is ever
committed, and CI's macOS job sets `REQUIRE_FULL_ENV=1` so a broken OCR
bridge fails the build instead of quietly skipping the tests that depend
on it. A green run means the tests actually ran.
