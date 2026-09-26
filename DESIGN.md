# Design

Why this tool is built the way it is. For *how to use it*, see the
[README](README.md); for *what it does*, read `verify.py`.

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
limitations). Only stream *bodies* are skipped; every dictionary is
scanned.

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
match can be coincidence.

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
  visual line of the horizontal reading or of either OCR reading, one
  decoded PDF literal, one metadata value.
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
reading — the natural continuation of the text. Every other join stays
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
readings, all eligible for hard findings. Two more readings stack *every*
glyph into vertical columns. They are *reconstructions*: the only way to
read a value written one character per line, but on an ordinary page a
column stacks one glyph from each of many lines — a ledger's last digits,
a numbered list's numbers — which assembled the README's own example
value out of a clean numbered list. Their matches are only ever manual
review, for values and patterns alike.

OCR is different: both Apple Vision passes (language correction on and
off) are genuine full-page reads of the same pixels, not reconstructions,
so both feed the hard tier. Correction-off is run precisely because the
language model can "correct" digits in codes and serial numbers.

## Bounded resources

The tool must survive documents that are hostile or merely huge. qpdf
output is streamed in chunks rather than buffered, with rolling scanners
that keep only enough tail to catch matches spanning a boundary. Pattern
feeds are batched (per-literal regex sweeps measured ~100x slower), rules
already matched are skipped, and every subprocess has a deadline — qpdf's
stream is read under `select`, exiftool runs under `communicate(timeout)`
— so a stalled tool cannot hang the run. Report samples are
masked — at most four trailing characters, never more than half — and
control characters are stripped, so a crafted PDF cannot inject escape
sequences into the terminal and the report cannot re-leak what it found.

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
  elsewhere that layer reports unavailable and the run exits `2`.
- **Password-protected PDFs are rejected** (exit `2`). A PDF encrypted
  with only an owner password opens without one and is scanned normally.
- **Pattern rules cannot span pages as hard findings** — they surface as
  review warnings. Value rules are hard only at a page seam (see the
  two-tier model).
- **Remaining silent misses** are the ✗ cells in COVERAGE.md: pixels the
  tool does not OCR (under a drawn box, off the page, thumbnails, other
  embedded files); font-coded text or images in switched-off layers,
  hidden annotations and never-drawn resources; unindexed bytes; leftover
  text in fonts that map ordinary-looking codes to other glyphs;
  text-encoded containers; pattern rules on non-attachment embedded files
  and on scripts stored as streams on actions; and the matching limits —
  page-break splits with something between the halves, multi-column
  wraps, dash-less pattern wraps, extreme coordinates.
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
