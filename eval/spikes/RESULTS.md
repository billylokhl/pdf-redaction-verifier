# Phase 1 measurement results

One run, on the author's machine, 2026-09-26. macOS (Darwin 25.6.0),
PyMuPDF 1.27.2.3, qpdf 12.4.1. Corpus: 2,031 PDFs discovered by
`corpus.py` under `/System/Library`, `/Library`, and `/Applications`
(no user data). Reproduce with:

```bash
python3 eval/spikes/measure_corpus.py
python3 eval/spikes/s1b_consumption_witness.py
python3 eval/spikes/pattern_class_false_hard.py
```

Only aggregate numbers appear below — no file paths, filenames, or
document contents, per `eval/spikes/README.md`.

## (b) Orphaned-content-stream rate -- docs/adr/0007

```json
{
  "files_measured": 2031,
  "files_with_orphaned_content_stream": 55,
  "file_rate": 0.0271,
  "total_orphaned_content_streams": 836
}
```

2.7% of files carry at least one object that is (a) unreachable from the
current revision's trailer (a trustworthy walk) and (b) sniffs as
content (`verify._is_content_stream`: at least one shown text object).
The 836-stream total is heavily skewed: two files account for 356 each
(712 of 836); the median affected file has exactly 1, and 28 of the 55
have exactly 1. Nothing in the corpus suggests these are redaction
leftovers -- system/app PDFs have no redaction history -- they read as
ordinary producer noise (unused alternate-layout Form XObjects, print
optimisation). See docs/adr/0007 for what this implies for "always
`FLAGGED`".

## (c) Parser-agreement flag rate -- docs/adr/0002

```json
{
  "files_measured": 2031,
  "qpdf_raw_flag_rate": 0.0896,
  "qpdf_refined_flag_rate": 0.0039,
  "mupdf_flag_rate": 0.0034,
  "combined_raw_flag_rate": 0.0901,
  "combined_refined_flag_rate": 0.0069
}
```

Raw qpdf warnings (`qpdf <file> --object-streams=disable /dev/null`,
never `qpdf --check`) fire on 9.0% of files -- well above REDESIGN §4's
~1.5% estimate. Categorised by files affected (182 raw-flagged files):

| Category | Files | Benign? |
| --- | --- | --- |
| Wrong xref offset, self-corrected by scanning ("a common error handled correctly by qpdf and most other applications") | 146 | Yes |
| Missing/misplaced `endobj` before a token that is unambiguously the next object or xref section | 26 | Yes (see docs/adr/0002 for the boundary case this doesn't cover) |
| Duplicated dictionary key (last occurrence wins, per spec) | 21 | Yes |
| "input stream is complete but output may still be valid" (small single-object files) | ~34 | Yes |
| Numeric literal overflowed qpdf's parser; object treated as null | 1 | No -- data was discarded |
| Genuinely reconstructed cross-reference table | 1 | No |

(Categories overlap per file, so the column does not sum to 182.)
Excluding the benign categories drops the qpdf-only flag rate to 0.39%;
combined with MuPDF's independent 0.34% (`is_repaired` or any warning
during open), the final parser-agreement flag rate is **0.69%** (14 of
2,031 files, only one flagged by both tools) -- consistent with the
plan's original estimate once the benign categories are named. See
docs/adr/0002.

## (d) Unindexed non-whitespace byte rate -- docs/adr/0003, 0007

```json
{
  "files_measured": 2031,
  "total_bytes": 126842256,
  "total_unindexed_non_ws_bytes": 399,
  "byte_rate": 3.1e-6,
  "files_with_any_unindexed_non_ws_byte": 8,
  "file_rate": 0.0039
}
```

Byte-level rate: 399 unindexed non-whitespace bytes out of 126.8 MB
(3.1 bytes per 10 million). File-level rate: 8 of 2,031 files (0.39%)
have at least one such byte, in three observed shapes:

1. **6 files**: `%BeginExifToolUpdate ... %EndExifToolUpdate <n>` comment
   markers -- ExifTool's own PDF metadata-update bookkeeping, sitting
   outside the object graph by design.
2. **1 file**: a linearized PDF repeats a `%PDF-x.y` / binary-marker
   comment pair mid-file, at the point where the main body resumes after
   the first-page section's own `startxref`/`%%EOF` -- a real, if
   unusual, producer convention this spike's tiler does not special-case
   beyond the file's true header.
3. **1 file**: a small hand-built test PDF (676 bytes) whose own
   `startxref` value does not point at its `xref` keyword -- the file's
   cross-reference table is genuinely unreachable via its own declared
   chain. `inventory_lite.py` deliberately does not fall back to a raw
   `xref` scan here, matching REDESIGN §4's "an unknown form is a flag"
   rule for the chain grammar -- this is a real ambiguity, not a tiler
   bug (confirmed: `qpdf --check` also has to repair this file).

Limitations of the tiler itself are in `eval/spikes/README.md`.

## Pattern-class false-hard rate -- docs/adr/0005

```json
{
  "files_measured": 2031,
  "file_rate": {"ssn": 0.0542, "credit-card": 0.0, "email": 0.0547, "us-phone": 0.0015},
  "union_file_rate": 0.1083
}
```

None of these 2,031 files are redaction targets, so every validated
`ssn`/`us-phone` match is necessarily a false positive, and every
`credit-card` non-match is a true negative. 10.8% of files trip at least
one built-in class -- inside REDESIGN §1's already-cited 7.5-13% range.
`credit-card`'s Luhn+date-guard validator had zero false positives here,
noticeably stronger than the other three. See docs/adr/0005.

## (a) S1b: the consumption witness -- docs/adr/0008

### Adversarial content streams (hand-built, on a scratch page)

| Case | code_count | glyph_count | Witness caught it | MuPDF warned |
| --- | --- | --- | --- | --- |
| Unknown operator between two shows | 21 | 21 | matches (no mismatch) | yes |
| Nested `BT` without matching `ET` | 10 | 10 | matches | no |
| Stray extra `Q` before content | 12 | 12 | matches | no |
| Well-formed inline image (control) | 23 | 23 | matches | no |
| Inline image whose declared size exceeds its data | 23 | 12 | **mismatch -- caught** | yes |

The one case built to actually break MuPDF's inline-image parser (REDESIGN
§4's motivating claim) is the one case the witness disagrees on: MuPDF's
texttrace silently drops "AFTER IMAGE" (12 glyphs shown instead of the
23 codes our tokenizer says were sent), exactly the failure mode the
witness exists to catch. In every adversarial case tried here, MuPDF
also emitted a warning -- this spike did not find a case where the
witness catches something a warning check would miss, but the warning
text is not a stable contract across MuPDF versions the way a byte count
is, so the witness remains a defense that does not depend on parsing
prose.

### Real-corpus reconciliation

A **naive whole-page** comparison (our tokenizer against `page.
get_texttrace()` for the whole page) reconciles on only 88.9% of 9,231
pages (807 mismatches with no MuPDF warning). Investigating the gap
found two confounds, neither of which is a malformed-content signal:

1. **Wrong unit.** `page.read_contents()` is only the page's own content
   stream; a `Form XObject` invoked with `Do` draws glyphs the page's
   texttrace includes but whose bytes are not in that buffer at all
   (`code_count = 0` on some pages with 30+ glyphs shown). This is not a
   bug to fix -- it is confirmation that REDESIGN §4's mechanism (attach
   *one resolved stream* to a scratch page, never diff a whole rendered
   page) is the right shape for the check, not an optional refinement of
   it.
2. **Glyph-less codes.** A C0 control byte (commonly a stray `\r` some
   producer's line-wrapping leaves inside an otherwise normal literal
   string) has no glyph in WinAnsi/MacRoman/StandardEncoding, so MuPDF's
   texttrace omits it while a naive tokenizer still counts it as a code
   sent. Excluding C0 codes (except tab) from the expected count and
   skipping pages that invoke a Form XObject (isolating something closer
   to REDESIGN's actual per-unit comparison) raises reconciliation to
   92.7% (7,633 of 8,233 checked pages).

The remaining 7.3% gap is concentrated in Identity-H (CJK) content and
was not fully root-caused in this pass -- see docs/adr/0006's owner-decision
note. It is not evidence against the mechanism (the isolated adversarial
tests above still reconcile exactly wherever content is well-formed),
but it does mean the witness is not yet safe to enforce as a hard
`DECODED` gate without further Phase 4a work to identify the remaining
source (candidates: `get_texttrace()`'s `chars` count reflecting
ToUnicode-mapped output characters rather than drawn glyphs 1:1 for some
multi-byte CMaps). See docs/adr/0008 for the resulting decision (ship as
advisory, not a hard gate, until this is root-caused).
