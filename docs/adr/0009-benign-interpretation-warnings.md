# 0009. Benign interpretation warnings

Status: accepted (owner approval, 2026-09-27)

## Context

REDESIGN.md §4's consumption-witness rule says plainly: "Any MuPDF
warning while interpreting the stream also means not `DECODED`." Phase 1
measured docs/adr/0002's parse-level (qpdf/MuPDF-*open*) warnings, but
never measured the *other* half of that sentence: a warning MuPDF's page
**interpreter** emits while actually running a page's content stream
(`page.get_texttrace()`), which is a different signal fired by different
code for a different reason -- a malformed piece of content mid-stream,
not a structural parse problem with the file as a whole.

**Correction to the previous version of this ADR.** It (a) described the
effect of a PyMuPDF bump as a "3x swing" everywhere, which holds for all
files only -- on the text-bearing files this ADR's cost is about, the
rate moved from 53.5% to 45.7%, about 1.2x; (b) proposed excusing a
warned unit whenever the byte-count witness balances, which has an
exit-`0` hole (Decision, below); and (c) did not measure the rule it
proposed. This version measures it, with the hole closed.

## Decision

**The guarded rule below replaces REDESIGN §4's raw rule ("any warning
means not `DECODED`"), with all five points, including guard 4
(decided, owner approval 2026-09-27).** The raw rule would flag 45.7% of
text-bearing files; the guarded rule flags 7.6% (Measurement).

**Why "the witness balances" is not enough on its own.** docs/adr/0008's
witness compares our count of the codes a content stream shows against
the glyphs MuPDF reports -- and both sides read MuPDF's own *decoded*
bytes. If a filter fails part-way (a truncated Flate stream, say), the
text lost with it is missing from both sides equally, and the witness
still balances. On this corpus, a rule that excused every balanced
warned page would excuse 7 pages carrying filter/decode warnings, all 7
at 0 codes = 0 glyphs -- the stream decoded to nothing that shows text,
which is balance only in the most useless sense.

**A warned unit is excused from the "not `DECODED`" rule only when all
of the following hold:**

1. **The unit's own consumption witness (docs/adr/0008) balances with a
   non-zero count.** 0 = 0 is never balance.
2. **No warning on the unit is a filter or decode error** (Flate/zlib,
   "premature end of data", and the like). These are never excused: the
   witness cannot see what a failed filter dropped.
3. **An image-decoder warning (JPEG 2000, JPEG, JBIG2) is vouched for
   only by that image's own witness** (Phase 4b, docs/adr/0004), never
   by a text-code count on the page that draws it. Until Phase 4b, a
   unit carrying one stays flagged.
4. **The warning belongs to the unit the witness measured.** Measured
   with pages as units, the witness compares the page's own content
   stream (annotations and widgets removed), while the warnings were
   collected with them present; a warning from an annotation's appearance
   stream must be vouched for by that annotation's own unit.
5. **Warnings are collected by running the interpreter first, on a
   freshly opened document.** MuPDF emits some warnings only the first
   time it loads a resource, so an earlier pass on the same document
   (`get_text()`, for instance) silently hides them (Measurement,
   below).

A category is never excused blanket, by name alone, regardless of what
happened in the unit it fired on -- that would repeat ADR 0002's
first-version mistake of trusting a message's own reassuring wording.
This is a *rule for evaluating* units, not a pre-approved list; no
category is excused by this ADR itself.

**Unrecognised warnings fail closed.** Guard 2 is implemented the
fail-closed way round: a warning family is eligible for the witness test
only once it has been reviewed and recorded as a text-interpretation
warning; any warning the implementation does not recognise (including a
filter warning renamed by a MuPDF update) keeps its unit flagged.

**Re-measured on every PyMuPDF/MuPDF update.** The rate moved materially
between two point releases (Measurement), so this measurement is re-run
whenever a PyMuPDF or MuPDF version bump lands, as part of reviewing that
bump.

## Measurement

`eval/spikes/interpretation_warnings.py`: `fitz.TOOLS.mupdf_warnings()`
collected around `page.get_texttrace()`, categorised by a normalised
(numbers stripped) warning line, over every page of the 2,031-file
corpus (484 text-bearing), `get_texttrace()` first on a freshly opened
document.

**The rate moved on a dependency bump.** The first measurement
(PyMuPDF 1.27.2.3) found 32.2% of all files (653/2,031) and 53.5% of
text-bearing files (259/484) affected, dominated by `invalid marked
content and clip nesting` (425 files, only 37 of them text-bearing).
Rebasing onto `main` picked up a dependency bump to PyMuPDF 1.28.2 (a
newer bundled MuPDF), and the same script now finds:

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 interpretation warning | **11.2% (227/2,031)** (was 32.2%) | **45.7% (221/484)** (was 53.5%) |
| Pages with >= 1 interpretation warning | 5.0% (459/9,231) | 6.9% (453/6,543) |

`invalid marked content and clip nesting` no longer appears at all. The
swing is about 3x on all files (32.2% to 11.2%) but about 1.2x on
text-bearing files (259 to 221 files), because the category that
vanished was almost entirely in text-free files.

What the 221 warned text-bearing files carry (aggregate category labels
only, per this directory's privacy rule; font names like "HelveticaNeue"
are generic system font names, not personal or file-identifying data):

- **One font-warning family covers 186 of the 221**:
  `FT_Get_Advance(<font>,<n>): invalid glyph index`, one category per
  embedded font subset (4-36 files each).
- `JPX numcomps (<n>) doesn't match color_space (<n>)` (JPEG 2000) -- 33
  files
- `openjpeg warning: Found a misplaced 'cmap' box outside jp2h box`
  (JPEG 2000) -- 30 files
- `bogus font (...) ascent/descent values` -- 2 files
- `premature end of data in flate filter` -- 1 file
- `... repeated <n> times...` appears in 121 files, but it is MuPDF's
  own repeat suppression (more copies of the warning before it), not a
  category.

**Call order changes the rate.** Running `get_text()` over the document
before `get_texttrace()` drops the warned files to 187 of all files
(9.2%) and 186 text-bearing (38.4%), and the JPEG 2000 categories vanish
entirely, because MuPDF emits some warnings only on the first load of a
resource. The rule must use the order that surfaces them, which is the
order measured here (point 5 above).

**The rule, measured** (same script; pages as units;
`s1b_consumption_witness.witness(unit_only=True)` as the per-unit
witness):

| Text-bearing | Warned files still flagged | Warned pages excused |
| --- | --- | --- |
| Raw rule (any warning → not `DECODED`) | 221 of 484 (45.7%) | 0 of 453 |
| Witness balances, no guards | 3 of 221 (1.4%) | 436 of 453 |
| **Witness balances, guards 1-3** | **34 of 221 (15.4%) -- 7.0% of the 484 text-bearing files** | **403 of 453** |
| **Guards 1-4 (annotated pages not excused) -- the decided rule** | **37 of 221 (16.7%) -- 7.6% of 484** | **395 of 453** |

- Without the guards, the rule would excuse 31 pages carrying JPEG 2000
  warnings (their images are not witnessed by a text count) and 2
  carrying filter warnings, both at 0 = 0 (7 such pages across all
  files, all at 0 = 0).
- Of the 50 warned text-bearing pages that stay flagged under guards
  1-3: **46 are unmeasured XObjects** -- 31 carry an image-decoder
  warning (no JPEG 2000 warning is excused) and 15 draw a Form
  XObject the spike does not measure -- plus 2 that use an unmodelled
  CJK CMap and 2 that carry a filter warning.
- 41 warned pages carry annotations or widgets; guards 1-3 excuse 8 of
  them on the page-content witness alone, which guard 4 would not.
- All files, guards 1-3: 39 of the 227 warned files stay flagged
  (17.2%), 1.9% of all 2,031.

## Consequences

- Raw, at 45.7% of text-bearing files, this is a materially larger
  review-rate contributor than docs/adr/0002's parser-agreement warnings
  (7.2% text-bearing) or docs/adr/0007's orphaned streams (11.4%
  text-bearing). Under the guarded rule it falls to 7.0-7.6% of
  text-bearing files, most of it images (Phase 4b) and forms (Phase 4a)
  that the spike does not witness yet.
- The guards key partly on warning text, which this ADR shows is
  version-sensitive: a renamed filter warning would slip past guard 2 if
  it were implemented as "excusable unless recognised as a filter
  error". It is therefore implemented the other way round (a family is
  excusable only once reviewed as a text-interpretation warning), which
  is fail-closed on renames; that variant's cost on this corpus was not
  measured and is measured when Phase 4a implements it.
- Any Phase 1/3c measurement that counts *by warning category name*
  should be re-verified whenever a MuPDF/PyMuPDF version bump lands, not
  assumed stable -- docs/adr/0002's qpdf-based categories are a
  different, external tool and were not affected by this bump, but a
  future qpdf upgrade could plausibly do the same thing to those.

## Owner decision (2026-09-27)

Owner decision: "approve all recommendations."

- **The guarded rule, with all five points, including guard 4**
  (annotation and widget warnings are vouched for only by their own
  unit), replaces the raw rule. Measured cost: 37 of 221 warned
  text-bearing files still flagged, 7.6% of all 484 text-bearing files
  (raw rule: 45.7%).
- **The three exit-`0` guards are requirements, not tuning**: never
  excuse a filter/decode warning, never accept 0 = 0 as balance, and
  vouch for an image-decoder warning only with the image's own witness.
- **Unrecognised warning names fail closed** (guard 2 implemented as an
  allow-after-review list, not a deny list).
- **Call order**: the interpreter runs first on a freshly opened
  document.
- **Re-measure on every PyMuPDF/MuPDF update**, as part of reviewing the
  bump.
