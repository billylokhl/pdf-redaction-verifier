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
