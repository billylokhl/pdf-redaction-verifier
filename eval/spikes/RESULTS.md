# Phase 1 measurement results

**This file was substantially revised after review.** The first version
under-measured several numbers (reporting only the all-files rate where
the text-bearing rate is what matters, and reporting a witness
reconciliation rate later found to depend on a bug in the measuring
script itself). Every number below is the corrected one, and states
which script produced it. See each ADR's own "Measurement" / "Correction"
section for the reasoning.

One run, on the author's machine, 2026-09-26 (revised same day after
review). macOS (Darwin 25.6.0), PyMuPDF 1.27.2.3, qpdf 12.4.1. Corpus:
2,031 PDFs discovered by `corpus.py` under `/System/Library`, `/Library`,
and `/Applications` (no user data); **484 of them text-bearing**
(`corpus.is_text_bearing`: >=1 page with non-empty `get_text()`).
Reproduce with:

```bash
python3 eval/spikes/measure_corpus.py
python3 eval/spikes/s1b_consumption_witness.py
python3 eval/spikes/pattern_class_false_hard.py
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
understated every one of those by 3-4x in the first version of this
document.

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
| **Any of the four (union)** | **10.8%** | **45.5%** |

**45.5%, not 10.8%, is the number that matters** -- a file with no
extractable text cannot produce a pattern-class false hard via this
path, so the all-files rate is diluted by the corpus's ~76% text-free
majority. `credit-card`'s 0% is a sample-size limit (no corpus file
happens to contain a Luhn-valid, non-date digit run of the right length),
not evidence its validator would reject a real false positive if one
existed. See docs/adr/0005 for the decision this changes (recommend
built-ins stay hard until Phase 5 -- the first version demoted them,
which contradicted REDESIGN's transition invariant).

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
file's own bytes, not assumed from the warning text.

Category breakdown (all files) behind the two surviving categories --
see `eval/spikes/measure_corpus.py`'s `_offset_warning_benign` and
`_dup_key_benign` for how each instance is verified:

- Wrong/zero xref offset, verified benign (no object body found
  anywhere for that number): included in the 2.9%/7.0% "refined" rates
  above as excluded.
- Duplicated dictionary key, verified benign (both values identical):
  included in the 2.9%/7.0% "refined" rates above as excluded.
- Everything else that produced a qpdf warning -- including a wrong
  offset where a body *does* exist elsewhere (data loss), a duplicated
  key with differing values, missing/misplaced `endobj`, and
  unterminated-Flate-stream warnings -- counts toward the refined rate.

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

Every affected file is text-bearing (expected: the sniff requires a
shown text object), so 11.4% -- not 2.7% -- is the rate that describes
this rule's real cost on the relevant population.

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
40-file sample and "all files under 1,300" from nowhere reproducible;
both are corrected here.

## S1b: the consumption witness -- docs/adr/0008

`eval/spikes/s1b_consumption_witness.py`. **This script had a real bug
in its first version**: numeric content-stream operands were dropped
rather than kept on the operand stack, so `/F0 12 Tf` never set the
tracked font (`_code_counts("BT /F0 12 Tf <00480065> Tj ET", {"/F0": 2})`
returned 4, not the correct 2). Every font silently counted as width 1.
The first version's control-code exclusion (by raw byte value, 0x00-0x1F)
then coincidentally cancelled much of this bug's effect, because a
2-byte CID code's high byte is often `0x00`. Both bugs are fixed in the
current script: numeric operands are kept, and glyph exclusion is now
computed per font from that font's own glyph table
(`doc.get_char_widths`), never from a raw byte value.

### Adversarial content streams (hand-built, on a scratch page)

| Case | code_count | glyph_count | Mismatch | MuPDF warned |
| --- | --- | --- | --- | --- |
| Unknown operator between two shows | 21 | 21 | no | yes |
| Nested `BT` without matching `ET` | 10 | 10 | no | no |
| Stray extra `Q` before content | 12 | 12 | no | no |
| Well-formed inline image (control) | 23 | 23 | no | no |
| Inline image whose declared size exceeds its data | 23 | 12 | **yes** | yes |
| **Clip-only text (`7 Tr`)** | 13 | **0** | **yes** | **no** |
| **Fill+stroke text (`2 Tr`)** | 6 | **12** | **yes** | **no** |

The last two rows are new in this revision, added because review asked
for a demonstrated case where the witness catches something a
warnings-only check would miss entirely -- both do, with zero MuPDF
warning text to fall back on. For comparison, `3 Tr` (ordinary invisible
text, as used by real OCR text layers under a scanned image) reconciles
exactly (6 codes, 6 glyphs) -- this is specific to modes 2, 6, and 7, not
"any invisible text."

### Real-corpus reconciliation, corrected

| Pass | Reconciliation rate | Mismatches without a warning |
| --- | --- | --- |
| Naive whole-page | 93.6% (8,637/9,231 pages) | 482 |
| **Per-decoding-unit** (the shape REDESIGN §4 actually specifies) | **71.2%** (5,868/8,237 pages) | **1,925** |

The first version reported 92.7% for the per-decoding-unit pass. That
number was produced by the bugged script described above and does not
describe the mechanism REDESIGN §4 specifies. **71.2% is the corrected
number.** A breakdown of the 1,925 warning-free mismatches by the page's
font encodings (`eval/spikes/s1b_consumption_witness.py`'s
`mismatch_font_encodings` output):

```
MacRomanEncoding (mixed with an unlabelled font): 1,252
MacRomanEncoding alone:                             277
Identity-H alone:                                   225
Identity-H mixed with MacRomanEncoding:              31
WinAnsiEncoding alone:                               12
Identity-H mixed with WinAnsiEncoding:               11
(remaining combinations, each < 5):                 < 10 combined
```

Manual inspection of sample MacRoman mismatches found small, near-exact
discrepancies (e.g. 354 codes vs. 356 glyphs) on ordinary text using no
unusual render mode -- **not** the Tr-mode issue demonstrated above, and
**not root-caused** by this pass. The leading hypothesis
(`doc.get_char_widths`'s glyph-presence signal diverging from MuPDF's
actual rendering-time glyph resolution for some subset fonts) is
plausible but unconfirmed.

**Decision** (docs/adr/0008): the Tr-mode handling (correct for 2/6,
leave 7 as a real mismatch signal) should ship in Phase 4a unconditionally
-- it is cheap, deterministic, and demonstrated. The broader byte-count
witness is proposed as advisory pending root-cause of the MacRoman/
Identity-H residual, given its real ~29% mismatch rate on per-unit
content -- the owner should confirm this trade-off rather than accept
either extreme by default.
