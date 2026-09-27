# Phase 1 measurement results

**This file has been revised three times after review.** The first pass
under-measured several numbers (all-files rate where text-bearing is
what matters; a witness reconciliation rate later found to depend on a
bug in the measuring script). The second pass root-caused that bug (and
two more in the same script), found a second script bug (widget
appearance text, in addition to annotations), and added two new
measurements (interpretation warnings, tiny images) plus a corrected
false-hard number that excludes `email` (a true positive, not a pattern
miscoloring). The third pass rebased onto `main` (which had, among other
things, bumped PyMuPDF from 1.27.2.3 to 1.28.2) and re-ran every script:
every number held except the interpretation-warning rate, which moved
3x -- see that section below. Every number below is the latest corrected
one, and states which script produced it. See each ADR's own
"Measurement" / "Correction" section for the reasoning.

Most recent full run: 2026-09-27, same machine. macOS (Darwin 25.6.0),
PyMuPDF 1.28.2 (up from 1.27.2.3 as of the second pass), qpdf 12.4.1.
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
positive if one existed. See docs/adr/0005 for the decision this changes
(recommend built-ins stay hard until Phase 5).

## Parser-agreement flag rate -- docs/adr/0002

`eval/spikes/measure_corpus.py`, tightened benign-category rules (only
two survive review, each verified per instance -- see docs/adr/0002):

| | All files | Text-bearing |
| --- | --- | --- |
| qpdf raw flag rate (any warning) | 9.0% | 32.4% |
| qpdf refined flag rate | 2.9% | 7.0% |
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

Every affected file is, by this sniff's own construction, text-bearing
(it only counts an object as an orphan when it shows a text object), so
11.4% -- not 2.7% -- is the rate that describes this rule's real cost on
the relevant population.

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
described in detail in docs/adr/0003 and 0007.

**Object-count distribution** (same script, `inventory_lite.tile()`'s
`object_count` field, cited by docs/adr/0006): median 13, 95th percentile
100, **maximum 32,971** (two files near 2,063, then a long tail). The
first version of docs/adr/0006 cited "largest 258" from an unrepresentative
40-file sample; corrected here. This count is itself a rough proxy (it
misses objects compressed inside an `/ObjStm`, and a REDESIGN "unit" is
not the same thing as an "object") -- see docs/adr/0006.

## Tiny stored images -- docs/adr/0004

`eval/spikes/image_envelope_stats.py`: every image object's `/Width` and
`/Height`, read directly (no decompression):

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 image under 8 px (either dimension) | 1.9% (39/2,031) | 5.6% (27/484) |
| Total images under 8 px | 248 | 224 |

The first version of docs/adr/0004 claimed no such images existed in
this corpus; corrected here. See docs/adr/0004 for the resulting
recommendation (excuse via today's `_text_sized` logic, don't flag every
one).

## Interpretation warnings -- docs/adr/0009 (new)

`eval/spikes/interpretation_warnings.py`: every `fitz.TOOLS.
mupdf_warnings()` collected around `page.get_texttrace()` (MuPDF's page
*interpreter*, not the parse-level open/qpdf warnings docs/adr/0002
measures), categorised by a normalised (numbers stripped) warning line.

**This number moved 3x between two runs, from a dependency bump alone.**
First measured against PyMuPDF 1.27.2.3; rebasing this branch onto
`main` picked up a Dependabot bump to 1.28.2, and re-running the exact
same script found a materially different rate:

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 interpretation warning, PyMuPDF 1.27.2.3 (first measurement) | 32.2% (653/2,031) | 53.5% (259/484) |
| **Files with >= 1 interpretation warning, PyMuPDF 1.28.2 (current)** | **11.2% (227/2,031)** | **45.7% (221/484)** |
| Pages with >= 1 interpretation warning (current) | 5.0% (459/9,219) | 6.9% (453/6,543) |

`invalid marked content and clip nesting` (425 files on 1.27.2.3, the
dominant category) **does not appear at all** in the 1.28.2 measurement
-- the newer bundled MuPDF evidently stopped emitting it. The remaining
categories are stable across both versions: several distinct
`FT_Get_Advance(<font>,<n>): invalid glyph index` categories (4-36 files
each, one per embedded font subset), `JPX numcomps (<n>) doesn't match
color_space (<n>)` (33 files), `openjpeg warning: Found a misplaced
'cmap' box outside jp2h box` (30 files). Font names like "HelveticaNeue"
are generic system font names, not personal data.

This is the real cost of REDESIGN §4's "any MuPDF warning while
interpreting the stream also means not `DECODED`" as literally written,
**as of the currently pinned PyMuPDF version** -- 11.2%/45.7%
(all/text-bearing), still the single largest Phase 1 review-rate
finding. The 3x swing is itself evidence for docs/adr/0009's
recommendation: a warning category that disappears on a routine
dependency bump was never a stable signal about document content, so a
name-based benign/not-benign list would silently change behaviour on the
next upgrade. See docs/adr/0009.

## S1b: the consumption witness -- docs/adr/0008

`eval/spikes/s1b_consumption_witness.py`. **This script has been fixed
twice.** The first version had an operand-stack bug: numeric content-
stream operands were dropped rather than kept on the operand stack, so
`/F0 12 Tf` never set the tracked font
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

**The exclusion is removed entirely in the current version.** What
actually explained the residual mismatch, root-caused by re-running this
script's own output against real content:

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
   origin) across the whole page's trace, not per span.
5. A few mixed-width CJK CMaps (e.g. `90msp-RKSJ-H`) are not 1- or
   2-byte fixed-width and are not modelled; a page using one is now
   skipped, not silently miscounted.

### Ten hand-built content streams (on a scratch page)

| Case | code_count | glyph_count | Mismatch | MuPDF warned |
| --- | --- | --- | --- | --- |
| Unknown operator between two shows | 21 | 21 | no | yes |
| Nested `BT` without matching `ET` | 10 | 10 | no | no |
| Stray extra `Q` before content | 12 | 12 | no | no |
| Well-formed inline image (control) | 23 | 23 | no | no |
| Inline image whose declared size exceeds its data | 23 | 12 | **yes** | yes |
| Ordinary invisible text (`3 Tr`) -- comparison | 6 | 6 | no | no |
| Fill+stroke text (`2 Tr`) -- de-duplicated | 6 | 6 | no | no |
| **Clip-only text (`7 Tr`)** | 13 | **0** | **yes** | **no** |
| **Inline image data spells a phantom `Tj`** | 16 | **9** | **yes** | **no** |
| **Text in a switched-off optional-content group** | 18 | **7** | **yes** | **no** |

The last three rows are the leak shapes review asked for: cases the
witness catches with **zero MuPDF warning** to fall back on.
"Inline image data spells a phantom `Tj`": the image's declared-length
pixel data literally contains a second, complete text-show operation
(`... EI (PHANTOM) Tj ...`, still inside the image's own byte range);
MuPDF correctly reads the whole range as opaque pixels (glyph_count 9 =
"REAL" + "AFTER" only), but this script's own naive `EI`-search inline-
image skip (the same technique `verify.py`'s current tokenizer uses)
stops early at the embedded literal `EI` and re-parses the tail as real
content, finding a phantom show operation that was never drawn -- the
safe direction (over-counting), but proof a naive image-length skip is
unsound. "Text in a switched-off optional-content group": REDESIGN §8's
K6 shape, reproduced directly -- absent from both `get_texttrace()` and
`get_text()`, present in the raw stream.

### Real-corpus reconciliation, corrected twice

"Text pages" excludes a page where both code_count and glyph_count are
zero (most corpus pages show no text at all and would otherwise pad the
rate with a meaningless 0-equals-0 "match").

| Pass | Reconciliation rate (text pages) | In-scope files | Files with any mismatch |
| --- | --- | --- | --- |
| Naive whole-page | 96.1% (2,417/2,515) | 436 | 18 (4.1%) |
| **Per-decoding-unit** (skips Form-XObject and mixed-width-CMap pages) | **98.7%** (2,235/2,264) | 326 | **9 (2.8%)** |

The first version reported 92.7%; the second, after removing the unsound
glyph exclusion but before fixing the CRLF and widget bugs, reported
71.2%. **98.7% is the corrected number**, after all five root causes
above. A small residual remains and is **not fully root-caused**: the 9
mismatching in-scope files show small (1-3 code), not-render-mode-related
discrepancies on otherwise ordinary MacRoman-encoded text with no
control characters, no annotations/widgets, and no Form XObjects.

**Decision** (docs/adr/0008, revised): recommend a **hard gate** --
mismatch means `FLAGGED`, unconditionally, not advisory. Advisory is
fail-open with respect to REDESIGN §2 ("discharged only when... proved
by a witness") and Principle 2 ("`DECODED` is accepted only when the
decoder's witness balances"), masked only by worst-of shipping until
Phase 6 retires legacy. At a corrected cost of ~2.8% of in-scope files,
this is now affordable; the earlier advisory recommendation was a
response to the (wrong) 71.2% figure, not a defensible design choice on
its own terms.
