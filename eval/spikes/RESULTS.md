# Phase 1 measurement results

**This file has been revised four times after review.** The first pass
under-measured several numbers (all-files rate where text-bearing is
what matters; a witness reconciliation rate later found to depend on a
bug in the measuring script). The second pass root-caused that bug (and
two more in the same script), found a second script bug (widget
appearance text, in addition to annotations), and added two new
measurements (interpretation warnings, tiny images) plus a corrected
false-hard number that excludes `email` (a true positive, not a pattern
miscoloring). The third pass rebased onto `main` (which had, among other
things, bumped PyMuPDF from 1.27.2.3 to 1.28.2) and re-ran every script:
every number held except the interpretation-warning rate, which fell
from 32.2% to 11.2% of all files but only from 53.5% to 45.7% of
text-bearing files -- see that section below. The fourth pass fixed two
more bugs in the S1b witness spike (which explained its whole remaining
residual), measured docs/adr/0009's then-proposed (since accepted) rule, and extended the
image-size measurement to today's 8×32 rule and the 35 Mpx cap. Every
number below is the latest corrected one, and states which script
produced it. See each ADR's own "Measurement" / "Correction" section for
the reasoning.

Most recent full run: 2026-09-27, same machine. macOS (Darwin 25.6.0),
PyMuPDF 1.28.2 (up from 1.27.2.3 as of the second pass; the fourth pass
re-ran `s1b_consumption_witness.py`, `interpretation_warnings.py` and
`image_envelope_stats.py`, the three scripts it changed), qpdf 12.4.1.
Corpus: 2,031 PDFs discovered by `corpus.py` under `/System/Library`,
`/Library`, and `/Applications` (no user data); **484 of them
text-bearing** (`corpus.is_text_bearing`: >=1 page with non-empty
`get_text()`). Reproduce with:

```bash
python3 eval/spikes/measure_corpus.py
python3 eval/spikes/s1b_consumption_witness.py
python3 eval/spikes/pattern_class_false_hard.py
python3 eval/spikes/interpretation_warnings.py
python3 eval/spikes/image_envelope_stats.py
```

Only aggregate numbers appear below -- no file paths, filenames, or
document contents, per `eval/spikes/README.md`. The corpus is a live
directory listing (see that file's "Known limitations"): exact counts
drift by a handful of files between runs on the same machine; the counts
below are from one specific run, not a fixed reference corpus.

**Both denominators are reported everywhere a rate depends on files
having extractable content**: all 2,031 files, and the 484 text-bearing
files that are the population several of these decisions (docs/adr/0005,
docs/adr/0007) actually gate on. Reporting only the all-files rate
understated every one of those in the first version of this document.

## Pattern-class false-hard rate -- docs/adr/0005

`eval/spikes/pattern_class_false_hard.py`. Scope, stated plainly: this
scans page **text only** -- not Metadata (XMP/Info) or Objects (string
literals, decoded streams), both of which today's tool also runs pattern
classes over. The rate below is a lower bound.

| Class | All files (2,031) | Text-bearing (484) |
| --- | --- | --- |
| `ssn` | 5.4% | 22.7% |
| `credit-card` | 0.0% | 0.0% |
| `email` | 5.5% | 22.9% |
| `us-phone` | 0.1% | 0.6% |
| Any of the four (union) | 10.8% | 45.5% |
| **`ssn` or `us-phone` only** | **5.5%** | **23.1%** |

**23.1%, not 45.5%, is the number that describes an actual false hard.**
A validated `email` match is a true positive ("there is an email address
here"), not a pattern miscoloring unrelated digits the way an SSN-shaped
date is -- folding it into the four-class union overstates the case for
demotion by roughly double. `credit-card`'s 0% is a sample-size limit (no
corpus file happens to contain a Luhn-valid, non-date digit run of the
right length), not evidence its validator would reject a real false
positive if one existed. See docs/adr/0005 for the decision this informed
(built-ins stay hard until Phase 5; accepted).

## Parser-agreement flag rate -- docs/adr/0002

`eval/spikes/measure_corpus.py`, tightened benign-category rules (only
two survive review, each verified per instance -- see docs/adr/0002):

| | All files | Text-bearing |
| --- | --- | --- |
| qpdf raw flag rate (any warning) | 9.0% | 32.4% |
| qpdf refined flag rate | 2.8% | 7.0% |
| MuPDF flag rate | 0.3% | 1.4% |
| **Combined refined flag rate** | **2.9%** | **7.2%** |

The first version reported 0.69% combined, from four benign categories
taken on qpdf's own wording without verification. Two of those
categories were dropped after review found a concrete counter-example
(an offset-0 xref entry that qpdf treats as null *without* scanning for
the real body -- real data loss, not "handled correctly") and a
mislabelling (a warning attributed to "inline images" that is actually
qpdf's unterminated-Flate-stream warning, the K1/K2 filter-chain
problem). The two that survive are verified per instance against the
file's own bytes, not assumed from the warning text. Known gaps in that
verification (an `/ObjStm`-compressed body is invisible to the "no body
anywhere" check; the duplicate-key value comparison captures only the
first token of an indirect reference) are documented in docs/adr/0002
and deferred to Phase 3c, not fixed in this spike.

## Orphaned-content-stream rate -- docs/adr/0007

`eval/spikes/measure_corpus.py`, `inv.orphaned_content_streams` (current-
revision reachability + `verify._is_content_stream` sniff -- a **lower
bound**: it does not count a paint-only orphan with no shown text, the
K5 shape, or a body with no current xref entry at all):

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 orphaned content-like stream | 2.7% (55/2,031) | **11.4% (55/484)** |
| Total orphaned content-like streams | 836 | 836 |
| Median count among affected files | 1 | 1 |
| Files with exactly 1 | 28 | 28 |
| Two largest counts | 356, 356 | (same files) |

All 55 affected files happen to be text-bearing -- an observation about
this corpus, not a guarantee (an orphan is an unreferenced stream, while
text-bearing is judged on the live pages), so 11.4% -- not 2.7% -- is
the rate that describes this rule's cost on the population it falls on.
It is the gross cost: today's tool already scans orphaned streams and
exits `2` for one it cannot decode, and that overlap was not measured.

## Unindexed non-whitespace byte rate -- docs/adr/0003, 0007

`eval/spikes/measure_corpus.py`, `inventory_lite.tile()`:

| | All files | Text-bearing |
| --- | --- | --- |
| Byte-level rate | 3.1e-6 (399 / 126.8 MB) | 1.4e-6 (127 / 87.9 MB) |
| Files with >= 1 unindexed non-whitespace byte | 0.39% (8/2,031) | 0.21% (1/484) |

Byte-level rate is vanishingly small either way; three observed shapes
(ExifTool `%BeginExifToolUpdate` markers, a linearized file's repeated
header comment, one file with a genuinely unreachable xref table --
confirmed independently by `qpdf --check` also needing to repair it) are
described only here; the ADRs (docs/adr/0007, for one) point to this
section rather than repeat it.

**Object-count distribution** (same script, `inventory_lite.tile()`'s
`object_count` field, cited by docs/adr/0006): median 13, 95th percentile
100, **maximum 32,971** (two files near 2,063, then a long tail). The
first version of docs/adr/0006 cited "largest 258" from an unrepresentative
40-file sample; corrected here. This count is itself a rough proxy (it
misses objects compressed inside an `/ObjStm`, and a REDESIGN "unit" is
not the same thing as an "object") -- see docs/adr/0006.

## Stored-image sizes -- docs/adr/0004

`eval/spikes/image_envelope_stats.py`: every image object's `/Width` and
`/Height`, read directly (no decompression). "8×32" is today's
`verify._text_sized` rule (imported, not reimplemented): an image under
8 px on its short side *or* under 32 px on its long side is one today's
tool treats as too small to be worth flagging as a leftover (a premise
docs/adr/0004 now rejects).

| | All files (2,031) | Text-bearing (484) |
| --- | --- | --- |
| Images under 8 px on either side | 248 images in 39 files (1.9%) | 224 images in 27 files (5.6%) |
| Images the 8×32 rule excuses | 299 images in 51 files (2.5%) | 235 images in 27 files (5.6%) |
| ...of which not under 8 px (the 8×32 rule's addition) | 51 images in 13 files | 11 images in 1 file |
| Images over the 35 Mpx cap | 2 images in 1 file | 2 images in 1 file |
| Largest stored image | 4464×8579 (38.3 Mpx) | (same file) |

The first version of docs/adr/0004 claimed no small images existed in
this corpus, and the second that none came near 35 Mpx; both corrected
here. See docs/adr/0004 for the resulting decision: the owner removed
the size excusal (2026-09-27), so these small images are enlarged and
OCR'd like any other image, and Phase 4b's recall bound must cover
them; an unused one is always flagged whatever OCR finds (owner
decision D). The script does not split used from unused images, so
decision D's cost is not measured here (Phase 3a measures it).

## Interpretation warnings -- docs/adr/0009

`eval/spikes/interpretation_warnings.py`: every `fitz.TOOLS.
mupdf_warnings()` collected around `page.get_texttrace()` (MuPDF's page
*interpreter*, not the parse-level open/qpdf warnings docs/adr/0002
measures), categorised by a normalised (numbers stripped) warning line,
with `get_texttrace()` run first on a freshly opened document.

**This number moved on a dependency bump.** First measured against
PyMuPDF 1.27.2.3; rebasing this branch onto `main` picked up a
Dependabot bump to 1.28.2, and re-running the same script found:

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 interpretation warning, PyMuPDF 1.27.2.3 (first measurement) | 32.2% (653/2,031) | 53.5% (259/484) |
| **Files with >= 1 interpretation warning, PyMuPDF 1.28.2 (current)** | **11.2% (227/2,031)** | **45.7% (221/484)** |
| Pages with >= 1 interpretation warning (current) | 5.0% (459/9,231) | 6.9% (453/6,543) |

`invalid marked content and clip nesting` (425 files on 1.27.2.3, only
37 of them text-bearing) **does not appear at all** in the 1.28.2
measurement -- the newer bundled MuPDF evidently stopped emitting it. So
the swing is about 3x on all files but about 1.2x on text-bearing files
(259 to 221). On 1.28.2, one font-warning family,
`FT_Get_Advance(<font>,<n>): invalid glyph index` (one category per
embedded font subset: 67 categories, 1-36 files each, 44 of them a
single file -- counted over every category, not the script's printed
top 15), covers 186 of the 221 warned
text-bearing files; the others are `JPX numcomps (<n>) doesn't match
color_space (<n>)` (33 files), `openjpeg warning: Found a misplaced
'cmap' box outside jp2h box` (30), a `bogus font ... ascent/descent
values` warning (2) and `premature end of data in flate filter` (1).
`... repeated <n> times...` (121 files) is MuPDF's own repeat
suppression, not a category. Font names like "HelveticaNeue" are generic
system font names, not personal data.

**Call order matters.** With `get_text()` run over the whole document
before `get_texttrace()`, the warned files drop to 187 (9.2%) of all
files and 186 (38.4%) of text-bearing files, and the JPEG 2000
categories vanish: MuPDF emits some warnings only on the first load of a
resource. The numbers above, and the rule below, use `get_texttrace()`
first on a fresh document, the order a rule relying on warnings must use.

**docs/adr/0009's rule (accepted with guard 4), measured** (same script; pages as
units; `s1b_consumption_witness.witness(unit_only=True)` as the
witness). Guards: (1) 0 = 0 is not balance; (2) a filter/decode warning
is never excused; (3) an image-decoder warning is never vouched for by a
text count.

| Text-bearing | Warned files still flagged | Warned pages excused |
| --- | --- | --- |
| Raw rule | 221 of 484 (45.7%) | 0 of 453 |
| Witness balances, no guards | 3 of 221 (1.4%) | 436 of 453 |
| Witness balances, guards 1-3 | 34 of 221 (15.4%); 7.0% of 484 | 403 of 453 |
| **Guards 1-4, also not excusing warned pages with annotations/widgets -- the accepted rule** | **37 of 221 (16.7%); 7.6% of 484** | **395 of 453** |

Without the guards, 31 pages carrying JPEG 2000 warnings and 2 carrying
filter warnings (both at 0 = 0; 7 across all files, all at 0 = 0) would
be excused. Of the 50 warned text-bearing pages still flagged under
guards 1-3, 46 are unmeasured XObjects (31 image-decoder warnings, 15
Form XObject draws), 2 use an unmodelled CJK CMap, 2 carry a filter
warning. All files, guards 1-3: 39 of 227 warned files still flagged
(17.2%; 1.9% of 2,031).

This is the real cost of REDESIGN §4's "any MuPDF warning while
interpreting the stream also means not `DECODED`" as literally written,
**as of the currently pinned PyMuPDF version** -- 11.2%/45.7%
(all/text-bearing), still the single largest Phase 1 review-rate
finding, and 7.6% of text-bearing files under the accepted guarded rule
(7.0% without guard 4). A
category that disappears on a routine dependency bump was never a stable
signal about document content, so a name-based benign/not-benign list
would silently change behaviour on the next upgrade. See docs/adr/0009.

## S1b: the consumption witness -- docs/adr/0008

`eval/spikes/s1b_consumption_witness.py`. **This script has been fixed
three times.** The first version had an operand-stack bug: numeric
content-stream operands were dropped rather than kept on the operand
stack, so `/F0 12 Tf` never set the tracked font
(`_code_counts("BT /F0 12 Tf <00480065> Tj ET", {"/F0": 2})` returned 4,
not the correct 2) -- every font silently counted as width 1. The second
version added a per-font "does this code have a real glyph" exclusion
via `doc.get_char_widths`, which looked clean (92.7% reconciliation) but
was itself unsound: `get_char_widths` resolves a code through the font's
own cmap as a Unicode code point, ignoring `/Encoding`, `/Differences`,
and `/CIDToGIDMap`, and returns an empty table for a font it cannot load
this way -- for 690 real pages that fabricated "this font has no glyphs
at all," hiding the real question behind a false 0-equals-0 match. The
premise was also false: `get_texttrace()` DOES emit a char entry
(replacement character, glyph id 0) for a code with no glyph --
`(A\x01\x02B\x7f\x81)` shown in Helvetica traces 6 chars, not 2, in a
direct check.

**The exclusion is removed entirely.** Together with it, seven more
causes explain every mismatch the earlier versions reported -- eight
in all, the same eight docs/adr/0008 lists:

1. This script's own literal-string decoder had the same two spec
   violations `verify._decode_pdf_string` has (a backslash-end-of-line
   continuation kept as a literal newline instead of contributing
   nothing; a raw CRLF not normalised to a single LF) -- fixed **locally
   in this spike script**, not in `verify.py`.
2. **Annotation *and form-widget* appearance text is included in a
   whole-page `get_texttrace()` call.** `page.annots()` does not
   enumerate widgets (PyMuPDF surfaces those separately via
   `page.widgets()`), so an intermediate fix that deleted only
   `page.annots()` still left a filled-in form field's value showing up
   as an unexplained mismatch -- both must be deleted from the in-memory
   page before tracing.
3. A ToUnicode continuation entry (glyph id -1) was counted as an extra
   glyph -- excluded now.
4. A fill-then-stroke render mode (`Tr` 2/6) draws, and `get_texttrace()`
   reports, the same glyph twice -- as **two separate spans**, not two
   entries in one span, confirmed directly. De-duplicated by (glyph id,
   origin) across the page's trace.
5. A few mixed-width CJK CMaps (e.g. `90msp-RKSJ-H`) are not 1- or
   2-byte fixed-width and are not modelled; a page using one is skipped,
   not silently miscounted.
6. **The de-duplication in 4 was applied to every page** and removed
   real draws (a run of zero-width glyphs at one position, the same text
   painted twice at one spot) -- 8 of the 9 files the third pass
   reported as mismatching. It now applies only on a page that sets
   `2 Tr` or `6 Tr`.
7. **The Form XObject skip was a regex** that missed `0 TL/Fm0 Do` (no
   whitespace before the name -- the ninth file) and skipped any page
   drawing *any* XObject, images included. A `Do` operand is now
   resolved in the page's resources; only a real `/Subtype /Form` draw
   skips the page, so image-only pages are now measured.

### Eleven hand-built content streams (on a scratch page)

| Case | code_count | glyph_count | Mismatch | MuPDF warned |
| --- | --- | --- | --- | --- |
| Unknown operator between two shows | 21 | 21 | no | yes |
| Nested `BT` without matching `ET` | 10 | 10 | no | no |
| Stray extra `Q` before content | 12 | 12 | no | no |
| Well-formed inline image (control) | 23 | 23 | no | no |
| Inline image whose declared size exceeds its data | 23 | 12 | **yes** | yes |
| Ordinary invisible text (`3 Tr`) -- comparison | 6 | 6 | no | no |
| Fill+stroke text (`2 Tr`) -- de-duplicated | 6 | 6 | no | no |
| Same text painted twice at one spot (`0 Tr`) -- not de-duplicated | 10 | 10 | no | no |
| **Clip-only text (`7 Tr`)** | 13 | **0** | **yes** | **no** |
| **Inline image data spells a phantom `Tj`** | 16 | **9** | **yes** | **no** |
| **Text in a switched-off optional-content group** | 18 | **7** | **yes** | **no** |

The last three rows mismatch with **zero MuPDF warning** to fall back
on. None of them is a false exit `0` under today's tool, and one is not
a leak at all:

- "Inline image data spells a phantom `Tj`": the image's declared-length
  pixel data literally contains a second, complete text-show operation
  (`... EI (PHANTOM) Tj ...`, still inside the image's own byte range);
  MuPDF correctly reads the whole range as opaque pixels (glyph_count 9 =
  "REAL" + "AFTER" only), but this script's own naive `EI`-search
  inline-image skip (the same technique `verify.py`'s current tokenizer
  uses) stops early at the embedded literal `EI` and re-parses the tail
  as content. **That is this script's tokenizer over-counting -- a false
  flag, not a leak shape** -- though it does show a naive image-length
  skip is unsound.
- `7 Tr` text and text in a switched-off layer (REDESIGN §8's K6 shape,
  absent from both `get_texttrace()` and `get_text()`, present in the raw
  stream) each exit `1` under today's `verify.py` with a value rule, found
  by its Objects layer (and, for `7 Tr`, its Text layer too). The gate
  matters for the rewrite's `DECODED` discharge (REDESIGN §2), not for
  today's tool.

### Real-corpus reconciliation

"Text pages" excludes a page where both code_count and glyph_count are
zero (most corpus pages show no text at all and would otherwise pad the
rate with a meaningless 0-equals-0 "match").

| Pass | Reconciliation rate (text pages) | In-scope files | Files with any mismatch |
| --- | --- | --- | --- |
| Naive whole-page | 97.3% (2,448/2,515) | 436 | 7 (1.6%) |
| **Per-decoding-unit** (skips Form-XObject and mixed-width-CMap pages) | **100.0%** (2,345/2,345) | 399 | **0** |

The first version reported 92.7%; the second, after removing the unsound
glyph exclusion but before fixing the CRLF and widget bugs, 71.2%; the
third 98.7% (2,235/2,264; 9 of 326 files). **100.0% among measured pages
is the corrected number**: the third pass's residual was causes 6 and 7
above, both spike artefacts, so no unexplained mismatch remains.

**What is not measured**: of the corpus's 9,231 pages, the per-unit pass
measured 8,363 (6,018 of them showing no text). **856 pages, in 206
files, draw a Form XObject** and are skipped -- their text is inside the
form, which REDESIGN §4 measures as its own unit and this spike does
not. **12 pages, in 9 files, use an unmodelled mixed-width CJK CMap** and
are skipped. No `Do` operand failed to resolve (0 pages). The witness
compares counts, not content, so a tokenizer over-count and an
interpreter over-count could in principle cancel on one page; not
observed.

**Decision** (docs/adr/0008, accepted 2026-09-27): a **hard gate** --
mismatch means `FLAGGED`, unconditionally, not advisory and not staged
through shadow mode. Advisory is fail-open with respect to REDESIGN §2 ("discharged
only when... proved by a witness") and Principle 2 ("`DECODED` is
accepted only when the decoder's witness balances"), masked only by
worst-of shipping until Phase 6 retires legacy. Its cost on measured
pages is 0 unexplained mismatches (0 of 399 in-scope files); the open
cost is the 856 form-drawing pages (206 files) and 12 CJK pages (9
files) the spike cannot yet measure.

## Phase 3a-6: the inventory agreement gate -- docs/adr/0010

The gate is `python -m scorecard inventory` (eval/README.md, "The
inventory agreement gate"). For each file it runs `build_inventory` in a
child process with a 60 s timeout. A second child then runs the shared
reader differential (`eval/scorecard/inventory.py`) against MuPDF 1.28.2
and qpdf. **Gate:** zero unflagged disagreements, 0 crashes, 0 timeouts,
and every unflagged file verified. Flag rates are reported and never gated.

Run 2026-09-28 on Linux with qpdf 11.9.0 and PyMuPDF 1.28.2. Both runs
reproduce with the command shown in each subsection. Both were re-run
after the PR #47 review, which added the ENCRYPTION check (a revision is
exempted as encrypted only when our trailer, MuPDF and qpdf all read it
so), qpdf `--check` ERROR counts and provenance in the aggregate's
`config`: every number below held (the case library's one encrypted
file is encrypted in all three views; no agreeing file has a qpdf
`--check` ERROR).

### Case library

Command: `inventory caselib`. 398 of 402 cases ran; the other 4 need a
tool this Linux machine lacks.

**The gate passed.**

| Result | Files |
| --- | --- |
| Unflagged, agree | 394 |
| Unflagged, agree, encrypted (object set only) | 1 of the 394 |
| Unflagged, disagree | **0** |
| Crash | 0 |
| Timeout | 0 |
| Flagged | 4 (1.0%) |

- **Flagged files.** All 4 are the after-`%%EOF` leak cases, flagged
  XREF_TAIL and UNINDEXED_NON_WHITESPACE. Their 4 UNINDEXED regions hold
  1,612 bytes. No CONTESTED regions.
- **What was compared.** 9,773 stream objects, counted once per revision,
  and 92 object-stream members. Every member was compared with MuPDF; up
  to 8 per revision also with qpdf.
- **Timing.**

  | Measure | p50 | p99 | max |
  | --- | --- | --- | --- |
  | Build child (s) | 0.07 | 0.40 | 1.34 |
  | Oracle child (s) | 0.20 | 1.8 | 3.5 |
  | Work units per byte | 3.6 | 4.4 | 4.4 |

  The limit is 64 work units per byte.

**Pending decisions, measured:**

- **Linearized files with an update (#44 item 1).** 1 linearized file;
  0 updated.
- **Comment lines claimed after the header, beyond the binary marker, or
  after an intermediate `%%EOF` (#46 item 1).**
  - 322 files have them after the header, 30 after an intermediate
    `%%EOF`.
  - 533 lines in all. The longest is 25 bytes.
  - 0 lines hold a non-printable byte; 0 look like `N G obj`.
- **Streams whose `/Length` lives in an object stream.** 0.
- **REVISION_AMBIGUOUS.** 0.
- **Dead object streams.** 0. There are 20 dead bodies, all in the 4
  flagged files.

### Fuzz

Command: `inventory fuzz --count 3000 --seed 0`. Half the files are
generated as `pdf_files()` generates them, with junk in a gap. The other
half are junk-free files with 1-3 bytes overwritten in an object, an
object stream's header table or probe member, or a gap.

**The gate passed.**

| Result | Files |
| --- | --- |
| Unflagged, agree | 740 |
| Unflagged, disagree | **0** |
| Crash | 0 |
| Timeout | 0 |
| Flagged | 2,260 (75.3%) |

By kind of file:

| Kind | Agree | Flagged |
| --- | --- | --- |
| Generated | 554 | 979 |
| Mutated, gap | 34 | 470 |
| Mutated, object | 74 | 398 |
| Mutated, object stream | 78 | 413 |

- **Top flag reasons, in files.**

  | Reason | Files |
  | --- | --- |
  | UNINDEXED_NON_WHITESPACE | 1,485 |
  | MISSING_ENDOBJ | 491 |
  | EXTRA_TOKENS | 456 |
  | UNEXPECTED_TOKEN | 325 |
  | XREF_OFFSET_MISMATCH | 294 |

- **Regions.** UNINDEXED in 1,248 files. CONTESTED in 55 files, 385
  bytes.
- **Timing.**

  | Measure | p50 | p99 | max |
  | --- | --- | --- | --- |
  | Build child (s) | 0.07 | 0.10 | 0.18 |
  | Work units per byte | 4.1 | 4.7 | 5.0 |

- **Comment lines that look like `N G obj`.** 32 such lines, in 32
  files, all flagged -- 31 of them CONTESTED_SPAN: a mutated `%` turns an
  object's first line into a comment the header or epilogue claims.
- **Dead object streams.** 31 in 25 files, all flagged.

**The oracle bug the first fuzz run found.** It found 2 unflagged
"disagreements" on object-stream members holding an empty name
(`[/ 2.5 ...]` and `<< /  true >>`). Our inventory, qpdf and MuPDF's own
object model all read these alike. The fault was in MuPDF's tight
printer (`xref_object(..., compressed=True)`): it writes no separator
after an empty name, so its text re-parses as a single name (`/2.5`).
The oracle now parses MuPDF's pretty print instead. The table above is
the re-run.

### The corpus (owner's local runs, 2026-10-03)

The corpus is the macOS system and application PDFs `corpus.py` finds
(2,073 files that day, 127.7 MB; the 2,031 above was 2026-09-27's
count -- it drifts with OS and app updates), staged as hashed symlinks
(eval/README.md). Command: `inventory run --root <links> --workers 8`.
Aggregates only. Both runs: macOS 27 (Darwin 27.0.0, arm64), qpdf 12.4.1,
PyMuPDF/MuPDF 1.28.2, Python 3.12.13; 0 encrypted files, 0 crashes, 0
timeouts, 0 unverified.

**First run (3a-6, commit 2218102): the gate failed.** 1,526 agree, 342
flagged (16.5%), **205 unflagged disagreements, all qpdf `--check`
warnings**, root-caused in 3a-6b (docs/phase3a-plan.md): 175 files only
linearization hint-table lint, 22 Flate data cut short in content, form
or image streams (1 more with lint), 6 a reference to object 0 (all with
lint), 1 a reference number past 2^63 that qpdf reads as nulling its
whole object.

**Re-run (3a-6b, commit ac545d9, clean tree, libqpdf 12.4.2 via
pikepdf 10.16.0): the gate passed.**

| Quantity | Value |
| --- | --- |
| Gate | **PASS**: 0 unflagged disagreements, 0 crashes, 0 timeouts, 0 unverified, 0 inconsistent |
| Unflagged, agree | 1,730 (0 encrypted) |
| Flagged (rate) | 343 (16.55%) |
| Top flag reasons (files) | xref_offset_mismatch 231, xref_table_malformed 178, unindexed_non_whitespace 111, length_mismatch 96, missing_root 85, xref_epilogue_mismatch 80, missing_endobj 26, duplicate_key 21, xref_stream_malformed 5, number_out_of_range 3 |
| Files with one reason only | xref_offset_mismatch 130, unindexed_non_whitespace 7, number_out_of_range 1 |
| qpdf `--check` ERROR on agreeing files (allowlisted) | 0 |
| Compared | 20,390 streams, 3,747 object-stream members, 83,034 values (objects, members and trailers, per revision) |
| UNINDEXED / CONTESTED | 111 files, 219 regions, 196,860 bytes / none |
| Build seconds p50 / p99 / max | 0.11 / 0.24 / 12.1 |
| Oracle seconds p50 / p99 / max | 0.40 / 0.82 / 37.8 |
| Work units per byte p50 / max | 2.27 / 4.76 (limit 64) |
| Linearized files; updated (all flagged); `/L` off the length | 479; 80 (80); 101, 21 of them single-revision |
| Comment lines: files, longest, non-printable, `N G obj` | 6 files (6 lines, all after an intermediate `%%EOF`), 20 bytes, 0, 0 |
| `/Length` in an object stream: files, streams | 0, 0 |
| REVISION_AMBIGUOUS: equal / differ | 0 / 0 |
| Dead object streams: files (unflagged) | 7 (0); 8,167 dead bodies in 105 files |

The 204 files of the first run's 205 now agree; the one with the
oversized reference number is flagged NUMBER_OUT_OF_RANGE, as are 2
already-flagged updated linearized files. The re-run's case library
(macOS, 400 of 402 cases buildable) also passes: 396 agree (1
encrypted), 4 flagged, 0 unflagged disagreements, 9,785 streams, 92
members and 25,563 values compared; fuzz (3,000, seed 0) passes: 740
agree, 2,260 flagged, 0 unflagged disagreements, 19,144 values compared.
A first fuzz re-run had found 2 ORACLE_ERRORs (a dictionary key that is
not UTF-8, which pikepdf will not look up by its string): fixed, with a
test and the keys added to the value fuzzer.

**Re-run after the owner's decisions (3a-6c, commit 30f2a27, and
unchanged after the #53 review fixes at commit 3c714f9; clean
tree): the gate passed.** Updated linearized files are canonical, an
in-use xref entry at offset 0 is no object, odd comment lines are
flagged (owner decision 7).

| Quantity | Value |
| --- | --- |
| Gate | **PASS**: 0 unflagged disagreements, 0 crashes, 0 timeouts, 0 unverified, 0 inconsistent |
| Unflagged, agree | 1,939 (from 1,730) |
| Flagged (rate) | 134 (6.46%, from 16.55%) |
| Top flag reasons (files) | unindexed_non_whitespace 111, xref_table_malformed 98, length_mismatch 96, missing_endobj 26, xref_offset_mismatch 22, duplicate_key 21, comment_line 5, missing_root 5, xref_stream_malformed 5, number_out_of_range 1 |
| Files with one reason only | unindexed_non_whitespace 7, number_out_of_range 1, xref_offset_mismatch 1 |
| Compared | 31,607 streams, 6,239 object-stream members, 125,866 values |
| Build / oracle seconds p50 / p99 / max | 0.09 / 0.17 / 9.6; 0.33 / 0.79 / 31.3 |
| Updated linearized files flagged | 0 of 80 (all agree with both readers) |

The 209 files that left the flagged set (80 updated linearized, 129
offset-0) all agree with both readers. `comment_line` fires on 5 files,
each a binary-marker line holding a control byte, and each already
flagged for other reasons, so the decision flags no new file. Case
library (396 agree, 4 flagged) and fuzz (740 agree, 2,260 flagged) are
unchanged, 0 unflagged disagreements in both.


**Re-run with 3a-7c's resource scopes and name trees (commit 4a51f65,
clean tree; owner's machine, 2026-10-12: macOS 27, qpdf 12.4.1, PyMuPDF/MuPDF
1.28.2): the gate passed.** The runs for 3a-7a and 3a-7b are recorded in
docs/phase3a-plan.md (1,932 agree, 141 flagged).

| Quantity | Value |
| --- | --- |
| Gate | **PASS**: 0 unflagged disagreements, 0 crashes, 0 timeouts, 0 unverified, 0 inconsistent |
| Unflagged, agree | 1,920 (from 1,932) |
| Flagged (rate) | 153 (7.38%, from 6.80%) |
| `NAME_TREE` / `RESOURCE_SCOPE` (files) | 13 (12 with no other reason) / 0 |
| Compared | 21,902 streams, 5,942 object-stream members, 104,709 values |
| Build / oracle seconds p50 / p99 / max | 0.15 / 0.53 / 12.5; 0.63 / 2.37 / 20.6 |
| Work units per byte p50 / max | 2.32 / 4.84 (limit 64) |

Before the owner's decision 10 the same code flagged `NAME_TREE` on 85
files (1,848 agree, 225 flagged, 10.85%): 72 with plain-encoded keys
ending in one NUL byte, 10 whose keys sort by bytes but not as text (9
mixing UTF-16 and plain-encoded keys), 3 with bytes 0x7F-0xA0. Re-run after review round 1 (commit 50e937b: 0xAD and U+0000 in a UTF-16 key flag, keys in order under each reader's reading): unchanged -- the class with 0xAD included is still 3 files, no key holds 0xAD or a UTF-16 U+0000. Re-run after review round 2 (commit 488248e: U+001B in a UTF-16 key flags, the trailing NUL only after printable ASCII, /Limits comparisons charged): unchanged again -- all 72 trailing-NUL files are printable ASCII. Re-run after review round 3 (commit 3ad3217: BOM-less keys MuPDF may read as UTF-8 flag; catalog rewrites reuse the trees; main merged): corpus unchanged (1,920 / 153, 7.38%); case library 426 agree, 27 flagged, 0 unflagged disagreements (453 cases after main's additions; `RESOURCE_SCOPE` still 20); fuzz unchanged. The NUL
class is now allowed (a reader-agreement test shows both readers resolve
such a key to the same object); the other 13 stay flagged. Case library
(413 cases): 387 agree, 26 flagged, 0 unflagged disagreements -- 20
cases newly flagged `RESOURCE_SCOPE`, annotation appearance streams that
draw only paths and carry no `/Resources`. Fuzz (2,000, seed 0): 501
agree, 1,499 flagged, 0 unflagged disagreements, unchanged.

## Phase 3a-7d spike: which annotations MuPDF synthesizes -- eval/spikes/annotation_appearance.py

Question (docs/phase3a-plan.md 3a-7d; owner decision P1 if no exact method exists): how can the
gate tell, per annotation, whether MuPDF draws the stored /AP or an appearance it synthesizes?

- No object-level signal works (MuPDF 1.28.2): `pdf_annot_ap` (before drawing, and after drawing
  and `pdf_update_annot`) and `pdf_annot_needs_resynthesis` leave a synthesized annotation looking
  like a stored one (`needs_resynthesis` is -1 only under /NeedAppearances).
- A drawing comparison works, batched per file: copy A (every page's /Rotate removed, every
  annotation's /F and /OC cleared) against copy B (copy A with every annotation that has /AP
  replaced by a stand-in /Square with the same /Rect, /AP and /AS). Per annotation MuPDF's trace
  (`fz_new_trace_device` + `pdf_run_annot`) and its 4x pixels drawn alone are compared:
  NOTHING (no mark; any text element is a mark, a path clip alone is not), STORED (equal
  trace and pixels), SYNTHESIZED. The copies pair by each annotation's page and /Annots
  position, every page read.
- On 35 fabricated fixtures covering ADR 0011 item 5's conditions -- /AP stream, state
  dictionaries with direct, indirect, missing and non-stream /AS entries, /AP empty, /D only, /N
  a number, direct annotations, a Text annotation on a rotated page, hidden, NoView and OC-off
  annotations, FreeText with /Contents and /RC, text widgets with and without /AP, inherited
  /FT and /V, /RV, checkbox, push button and combo box under /NeedAppearances, signed,
  unsigned, inherited and /DocTimeStamp signatures -- MuPDF's reading agrees with the ADR in
  every case (ours SYNTHESIZED ~ SYNTHESIZED or NOTHING; not synthesized ~ STORED or NOTHING).
- Not exact: drawings equal in trace and in 4x pixels are read as the same, so content that
  differs only below that resolution (an image's samples, a glyph program) is not told apart --
  owner decision P1. ADR 0011 item 5 already flags a synthesized unit whose rich text could draw
  an image.
- Link and Popup are not listed by `pdf_first_annot`/`pdf_first_widget` (MuPDF draws neither);
  lists compare as multisets by object number (by /Annots position for a direct annotation).
  Also covered: a stored /AP holding only clip-mode text, and pages with a widget before a
  square in /Annots. Cost: two document copies per file, linear -- roughly 0.03-0.2 s for 100
  annotations and 0.5-0.8 s for 1,600, by machine.

## Phase 3a-7d: the annotation gate on the implementation

The spike's method (owner decision 11, P1 = (a)) in `eval/scorecard/annotations.py`, against
`redaction_verifier/inventory/annotations.py`'s classification. Two corrections to the spike's
code: the copies pair on a (page, /Annots index) key, not page * 10^6 + index; and whether an
annotation has /AP is read as written before MuPDF loads the copy -- MuPDF writes the appearance
it synthesizes into an annotation written directly in /Annots, which the spike would then read
as STORED. MuPDF's warnings while it loads and draws the two copies are discarded (an /AS entry
that is not a stream draws nothing but warns "content stream is not a stream" when drawn); the
file's own warnings, its pages reloaded for the list included, are gated as before.

Implementing it found where MuPDF 1.28 reads past ADR 0011 item 5's text (each pinned in
`tests/test_inventory_annotations.py` against MuPDF's drawing; ADR 0011's notes on item 5):
the signature test reads /FT as written (a reference to /Sig is no signature field) and
resolves /V and its /Type; a direct null is an absent key; references are followed up to nine
in a row; only widgets climb /Parent (a Square's /Parent cycle draws fine); a widget whose
/Parent chain has a cycle makes MuPDF fail ("cycle in tree"): `FIELD_TREE`. An annotation MuPDF
would synthesize but cannot makes it warn "cannot create appearance stream" and draw the stored
/AP instead -- under /NeedAppearances that is STORED where the ADR says synthesized -- or
nothing: a /Subtype outside the 18 it builds, or a widget whose /FT is missing, unknown, or a
reference to /Btn. Probed for each field type given by reference (as written on the widget, on
its parent, and through a chain of two references; with and without /NeedAppearances): /Tx,
/Ch and /Sig are built through references without a warning, /Btn (a check box, a push
button, a radio button) only written as a name. The inventory flags exactly those
(`APPEARANCE_UNSUPPORTED`, coordinator decision, 2026-10-15). qpdf --check's own annotation
and AcroForm analysis -- a widget more than 100 field levels below /Fields ("not reachable from
/AcroForm"), an annotation listed twice in one /Annots ("duplicate entry") -- is issue #144; a
first version flagged a /Parent chain deeper than 100, which neither matched qpdf's
downward walk nor caught it, and was dropped in review.

| Run | Agree | Flagged | Unflagged disagreements |
| --- | --- | --- | --- |
| Corpus (2,073) | 1,920 | 153 (7.38%) | 0 |
| Case library (461) | 433 | 28 | 0 |
| Fuzz (2,000, seed 0) | 501 | 1,499 | 0 (no annotation in any generated file) |

Corpus: 778 annotations compared; `FIELD_TREE` and `APPEARANCE_UNSUPPORTED` on no file;
`SYNTHESIZED` units in 139 files, 400 annotations in the current revisions, all widgets (130
files, 373 annotations unflagged) -- ADR 0011 measured 139 and 400. Older revisions: 14
(body, /Annots index) pairs in 9 files are units not among the current revision's (a widget
whose body a later revision rewrote counts here); by ADR 0011's definition -- a widget, by
object number, synthesized in an older revision but not now -- 0 ("none of them new", the ADR
said, of its 13 widgets in 8 files); 57 widgets in 23 unflagged files are synthesized in some
older revision. Case library: 27 annotations compared. Oracle seconds on the final run (6
workers): p50 0.32, max 17.0 (3a-7c's run: 0.63 and 20.6); the two copies cost only on
revisions with annotations.

## Phase 3a-7e: what the labels cost -- the owner's briefing before 3b

Re-measured on the implementation (3a-7e-1 to 7e-3; `scorecard inventory run`, aggregate
`pending_decisions.labels_cost`, 2026-10-15), against ADR 0011's "What it costs", which came
from a prototype. Nothing here changes a verdict: labels add no flag (R3), and the ledger
verdict is shadow-only until it is enforced. These are the files that would exit `2` once
labels gate -- 3b's verdict maps orphans to `FLAGGED`, and each kind without a decoder stays
unexamined until its decoder lands (4a for synthesized annotations).

Denominators: 2,073 corpus files, 153 flagged by the inventory (7.38%), 1,920 unflagged. The
ADR measured 1,939 unflagged files (134 flagged) before 3a-7's flags, so its counts are over
19 more files.

**Orphans, per body (ADR 0011 item 5's definition): 242 files (12.6%), 2,513 streams.**
In the current revision's sense, 294 files (3,056 streams; the ADR's prototype measured 307).
The 52 files between the two have orphans now that were drawn or structural in an older
revision they live in: item 5 labels a body across all its revisions, and decision D's
stricter "drawn by the current revision" is 4b's, by running content. By class, per body
(files; a file can be in several):

| Class | Files | Streams | Note |
| --- | --- | --- | --- |
| `/PieceInfo` private data | 195 | 847 | ADR: 203 |
| Page thumbnails | 194 | 682 | ADR: 202. Every one is on a page today's CLI already marks (#133, `PAGE_THUMBNAIL`) |
| Appearances not drawn (an `/AP` state, a widget on no page) | 24 | 24 | |
| `/Properties` holding forms or `/Contents` | 16 | 77 | Fails closed as item 5 says (optional-content property lists draw nothing) |
| Unreferenced | 10 | 858 | ADR: 82 in the current revision (78 now, and 36 reached only from unreachable objects); per body, most were labelled in an older revision |
| Fields, a font program, an XObject, a soft mask under `/PieceInfo`, an older body, one reached only from an unreachable object | 1-3 each | 25 | |

`/Metadata` orphaned on an annotation or an ICC profile: 0 (corpus, case library and fuzz),
so the `/Metadata` holder allowlist costs nothing today.

**Exit `2` once labels gate, by class** (unflagged files; each file once in the total):

| Class | Files | ADR 0011 | Deviation |
| --- | --- | --- | --- |
| Orphan (R3), per body | 242 | 307 (current revision, prototype) | -65: the per-body definition (current revision: 294, -13) |
| Linearization hint stream (R6, ADR 0003) | 400 | 416 | -16 (76 under a stale `/L`, counted apart: decision Q8) |
| XMP packet holding an image (R7, decision D) | 516 | 519 | -3. 558 packets; no packet holds other binary; none undecoded |
| JavaScript stream (no decoder yet) | 86 | 89 | -3 (159 streams) |
| Synthesized annotation (until 4a) | 130 | 139 | -9: 139 is over the whole corpus, 9 of those files are flagged |
| Output-intent profile | 0 | 0 | -- |
| **Any of these** | **707 (36.8%)** | about 728 | -21 |

707 files have at least one class; 144 have only an XMP image, 63 only a hint stream, 6 only an
orphan. Orphans, hint streams and XMP images alone cover all 707, so decoding synthesized
annotations (4a) and JavaScript does not lower the count. The thumbnail orphans add no file
of their own: 8 files have only thumbnail orphans, and each also has another class. Whether
these files already exit `2` in today's CLI for other reasons is 3a-12's measurement (ADR
0007's overlap); only the thumbnails' overlap (#133) is measured here.

| Scenario (whole corpus, 2,073 files) | Measured | ADR 0011 |
| --- | --- | --- |
| Inventory flags (3a-7) | 153 (7.38%) | 141 (6.80%) |
| Every kind enforced | 860 (41.5%) | about 865 (42%) |
| -- after 4a and a JavaScript decoder | 860 (41.5%) | about 865 (42%) |
| -- hint streams not flagged (R6 (b), at best) | 797 (38.4%) | about 840 (40-41%) |
| -- orphans and XMP images only | 728 (35.1%) | about 775 (37%) |
| -- orphans alone | 395 (19.1%) | about 441 (21%) |

No cost differs from the ADR's figure in a way that changes the picture: every class is within
3% of its file count except orphans, which are lower under the per-body definition.

**Item 7, the graph's cost.** The reference graph's whole stage (edges, page lists, name
trees, annotations, labels) charged at most 322,346 units on a corpus file, whose sum of live
objects and edges over its revisions is 6,062 (53 units each); p50 3,221, p99 58,966. The
largest sum is 6,381 (p50 24, p99 997). Units per object or edge: p50 120, p99 224, max 243,
falling to 18-71 on files whose sum is 500 or more -- small files pay the fixed charges for
what the labels keep, at most a byte per unit. Work per byte of the file stays at most 6.52
of the budget's 64. The bound, for the owner: **R revisions, each changing a reachable set of
N objects and edges, cost about 64·N·R units** (measured 50-70 per object or edge per
revision walked again on the item-7 test's shape); a 1 MB file with 5,000 objects and 200
such saves reaches the limit and is flagged `BUDGET_EXHAUSTED`, failing closed. **Memory:**
the labels keep at most one byte per unit of work, so at most 64 times the file's size plus
about 1 MB (DESIGN.md, "Bounded resources").

**The oracle's time** (corpus, 8 workers, on a heavily loaded machine: load average 25-35,
build p50 0.108 s against 0.074 s in 3a-7e-2's run): p50 0.616 s, p90 0.897, p99 2.07, max 54.5
(3a-7e-2: 0.237 and 14.5 unloaded). The `REACHABLE` check: p50 0.003 s, p99 0.12, max 2.3;
its share of each file's oracle time p50 0.5%, p99 8.6%, max 22.7%, and 1.9% of the run. The
cost measures add about 7 ms a file (83 sampled files, in-process). Re-measure on an idle
machine before reading the absolute times.

| Run | Agree | Flagged | Unflagged disagreements |
| --- | --- | --- | --- |
| Corpus (2,073) | 1,920 | 153 | 0 (unchanged) |
| Case library (467) | 440 | 27 | 0 |
| Fuzz (2,000, seed 0) | 501 | 1,499 | 0 |

**What is left, at the agents' pace.** Phase 3a has five PRs left: 3a-8 and 3a-9 (encryption,
Lane A2), 3a-12 and 3a-13 (the unit budget and orphan overlap; decision D's cost, Lane G), and
3a-14 (close-out). 3b's row in REDESIGN names six deliverables -- obligations and parent
anchors, the child protocol and completion sentinel, witnesses, the verdict function (with the
orphan mapping and its test), shadow and enforced verdicts with worst-of shipping, `--explain`
-- so about 7 to 9 PRs with its plan. Lane A merged five 3a-7 PRs on 2026-10-15, reviews
included; the coordinator re-baselines from PRs per day.

## Phase 5 baseline: built-in pattern classes, full CLI -- eval/scorecard/pattern_baseline.py

Measured 2026-10-14 (commit 90669ef), on the owner's local corpus: 2,073
macOS system and application PDFs (eval/README.md, "Pattern-class
false-hard baseline"). The rules file holds only the four built-in classes,
so every hard finding is a false hard; `email` is reported but, per ADR
0005, it is a true positive for "an address is here". Unlike ADR 0005's
page-text scan, this covers every layer the CLI runs a class over. 1 file
was unmeasured (a 300 s timeout, text-bearing); no finding was unjoined.
Counts only.

Files with at least one hard finding, text-bearing stratum (488 measured):

| Class | Files | Text | OCR | Objects | Metadata | Hidden |
| --- | --- | --- | --- | --- | --- | --- |
| `ssn` | 132 (27.0%) | 89 | 87 | 23 | 13 | -- |
| `us-phone` | 14 (2.9%) | 3 | 8 | 8 | -- | -- |
| `credit-card` | 7 (1.4%) | 1 | -- | 6 | -- | -- |
| `email` | 112 (23.0%) | 111 | 111 | 91 | -- | 24 |
| **`ssn` or `us-phone`** | **138 (28.3%)** | | | | | |

All files (2,072 measured): `ssn` 7.8%, `credit-card` 3.7%, `us-phone`
1.9%, `email` 5.5%; `ssn` or `us-phone` 8.7%. On text-free files the
false hards come only from Metadata and Objects (credit-card's 58
Metadata files: number lists 28, hex ids 26, UUIDs 4).

**What the matches are** (text-bearing, files per shape; a file can have
several):

| Class | hex-id | alnum-id | uuid | bare-digits | number-list | formatted |
| --- | --- | --- | --- | --- | --- | --- |
| `ssn` | 69 | 44 | 11 | 12 | -- | 3 |
| `us-phone` | -- | -- | -- | 12 | -- | 2 |
| `credit-card` | -- | 1 | -- | 2 | 4 | -- |

No `date-or-time` match was found. For `ssn` and `us-phone` that shape can
only come from a `D:` prefix, because the classes' digit guards keep a
9- or 10-digit match out of a longer digit run, so this is not evidence
that the corpus holds no dates. `formatted` (grouped as the class's real
values are) is decided first, so a run that spells a real grouping inside
a longer id counts as `formatted`; this errs toward keeping files hard.

**What each demotion option would leave hard** (text-bearing `ssn` or
`us-phone`; an estimate from every match of the class, not only the one
reported, that errs toward keeping files hard -- eval/README.md):

| Demoted to review | Still hard |
| --- | --- |
| nothing (today) | 138 (28.3%) |
| matches inside a token (`uuid`, `hex-id`, `alnum-id`) and dates | 25 (5.1%) |
| ... and number lists | 25 (5.1%) |
| ... and bare digit runs | 5 (1.0%) |
| the whole class | 0 |

The 5 left are 3 files with a dashed SSN-shaped value (all in Objects)
and 2 with a punctuated phone number (Text, OCR and Objects): matches that
look exactly like the real thing.

Reading:
- 28.3% replaces ADR 0005's 23.1%, which scanned page text only, on an
  earlier snapshot of the corpus (2,031 files, 484 text-bearing).
- A context rule that only demotes matches inside a token, or dates,
  leaves 5.1%, far above the gate.
- Phase 5's gate (< 1%) is not met by any option short of demoting what
  looks exactly like a real SSN or phone number: 5/488 is 1.02%.
- `credit-card` is not 0% under the full CLI.

This measures false hards only. It does **not** measure what a demotion
would cost in recall: a real SSN written next to a label with the space
lost ("SSN123456789") is shaped `alnum-id` and would be demoted with the
rest. That cost has to be measured on leak cases before any demotion
ships (Phase 5, PR 2; ADR 0012).

**Policy 1 on the corpus** (ADR 0012; `eval/scorecard/pattern_policy1_corpus.py`
over the per-file reports of the run above, recomputed without the
built-in classes' findings and review warnings, which every pattern
warning names in its `rule` field; 0 unattributed): since the rules were
the four classes only, each file's exit is then set by its coverage
warnings alone.

| Text-bearing, 488 | Exit 0 | Exit 1 | Exit 2 |
| --- | --- | --- | --- |
| today | 131 (26.8%) | 235 (48.2%) | 122 (25.0%) |
| policy 1 | 248 (50.8%) | 0 | 240 (49.2%) |

Moves: 111 files 1 → 0, 124 files 1 → 2 (a coverage warning remains), 6
files 2 → 0. All 2,072 measured files: exit 0 from 1,579 to 1,785, exit 1
from 337 to 0, exit 2 from 156 to 287 (198 1 → 0, 139 1 → 2, 8 2 → 0).

With `credit-card` in the union (`ssn`, `us-phone` or `credit-card`,
text-bearing): 139 files (28.5%) today; under the demote-only options A,
B and C, 25 (5.1%), 5 (1.0%) and 0.

## Phase 5 recall: what each option costs -- eval/scorecard/pattern_recall.py

Measured 2026-10-15 on the case library (fabricated), macOS 27: every case
with a built-in class rule, scanned with the pattern recorder. 109 are leak
cases, and **every one names the `ssn` class**: no `us-phone`,
`credit-card` or `email` leak case exists, so those classes' recall is not
measured. 52 of the 109 exit 1 today: 43 are held there by a value rule
(no option touches it) and 9 by `ssn` findings only. One case expected at
exit 1 cannot be judged here: `page.dense-sideways-scan` (K43) exits 2
today on macOS 27 (its story says 1 on macOS 26). No finding's shape was
unknown or ambiguous.

**ADR 0012's demote-only options** (exit 1 → 2; the lower and upper bounds
agree for every option):

| Option | Leak cases moved 1 → 2 | ... if the cases' value rules were absent |
| --- | --- | --- |
| A: demote matches inside a token or a dated run | 0 | 0 |
| B: A, plus bare digit runs and number lists | 1: `layout.form-boxes-pattern-only` | 4 |
| C: demote the class | 9 | 24 |

`layout.form-boxes-pattern-only` is an SSN typed one digit per form box,
which the Text layer reads as a bare `123456789`. The 9 under C are every
leak case caught at exit 1 by the class rule alone (listed below).

**Policy 1** (the owner's direction of 2026-10-15: a built-in class match
no longer affects the exit code). 24 leak cases reach exit 0, all `ssn`:

- from exit 1 (9): `page.visible-pattern-rule`, `layout.single-line-pattern-only`,
  `layout.rotated-pattern-only`, `layout.form-boxes-pattern-only`,
  `layout.overlapping-values-pattern-only`,
  `layout.overlapping-split-short-tail-pattern-only`,
  `document.trailer-key-pattern-rule`, `page.scan-image-sideways-90`,
  `page.scan-image-sideways-270`;
- from exit 2 (14; their only warnings were class reviews): `layout.split-lines-pattern-only`,
  `layout.split-three-lines-pattern-only`, `layout.split-pages-pattern-only`,
  `layout.split-pages-needs-later-lines-pattern-only`,
  `layout.split-three-pages-pattern-only`, `layout.split-over-blank-page-pattern-only`,
  `layout.two-page-breaks-pattern-only`, `layout.overlapping-split-pages-pattern-only`,
  `layout.repeated-line-wraps-pattern-only`, `layout.several-values-pattern-only`,
  `layout.table-rows-pattern-only`, `layout.table-rows-split-pages-pattern-only`,
  `layout.vertical-stack-pattern-only`, `document.trailer-id-digits-pattern-rule`;
- the known gap `page.dense-sideways-scan` (K43): exit 2 today, 1 expected,
  0 under policy 1.

Two clean cases change: `document.generated-id-digits` (2 → 0; expected
0) and `page.digit-column` (2 → 0; expected 2, the clean side of #74).

**Policy 1 on the case labels** (every case with a class rule, scanned or
not). A label's exit also changes where the label's target was set by the
class alone: 11 more `ssn` leak cases, all known gaps that exit 0 today and
are expected at exit 2 only from a class review warning, are expected at
exit 0 under policy 1. Four are REVIEW_BINARY targets (`document.af-pattern-rule`,
`document.js-stream-pattern-rule`, `document.piece-info-pattern-rule`,
`page.hidden-sample-bytes-pattern-rule`). Seven are REVIEW_FUSED_PATTERN
targets (`layout.form-boxes-split-pages-pattern-only`,
`layout.rotated270-split-pages-pattern-only`, `layout.split-four-pages-pattern-only`,
`layout.split-pages-needs-earlier-lines-pattern-only`,
`layout.vertical-stack-split-pages-pattern-only`, `layout.undashed-ssn-wrapped`,
and `layout.rotated-split-pages-pattern-only`, which needs `no-ocr` and is
judged from its label only). In all, 35 leak cases reach exit 0 under
policy 1; the owner signed off all 35 (ADR 0012). The 31 cases with a
value rule beside the class rule cannot be judged from their labels (a
label lists warning codes, not their rule); for each, this scan's report
satisfies the label exactly (`caselib.run.judge`) and names each
warning's rule, which settles it: none changes (`settled_by_scan` lists
all 31).

These lists describe macOS 27 with every tool installed: OCR differs by
macOS version (K43) and a case needing a missing tool is not scanned.

## Value false hards with a sequential test value -- Phase 5 item 4

Measured 2026-10-15 on the owner's local corpus (counts only). With the
case library's exact-value rule `SSN` = `123-45-6789`, Lane B's corpus run
found false hard findings on 17 entries (15 unique files), all in the
Objects layer, live storage, plus 132 text-bearing REVIEW_BINARY warnings
naming the SSN rule. With a realistic value, `666-51-4073`, over all 2,073
entries: 0 false hard findings in any layer (0 of 489 text-bearing) and no
warning naming the rule.

Root cause, on all 15 files: the Objects layer extracts PDF string literals
(`(...)`, `<hex>`) from stream bodies and the value scanner reads them back
to back, so a value split across literals is caught (by design). In 12 of
the 15, one hex string decodes to a character table beginning
`0123456789`; in the other 3, a run of one-character strings decodes to
`1`, `2`, ... `9` in order. Either way the normalized value `123456789` is
really there: the matcher is correct, and the false hards come from the
test value being sequential. The 132 REVIEW_BINARY warnings were not
traced; they are presumably the same digit-table collision in decompressed
binary data (a known source of coincidental Binary warnings), and they too
vanish with the non-sequential value. Decision (coordinator, 2026-10-15): no matcher
change; corpus false-hard measurements use a non-sequential value.

