# 0009. Benign interpretation warnings

Status: proposed

## Context

REDESIGN.md §4's consumption-witness rule says plainly: "Any MuPDF
warning while interpreting the stream also means not `DECODED`." Phase 1
measured docs/adr/0002's parse-level (qpdf/MuPDF-*open*) warnings, but
never measured the *other* half of that sentence: a warning MuPDF's page
**interpreter** emits while actually running a page's content stream
(`page.get_texttrace()`), which is a different signal fired by different
code for a different reason -- a malformed piece of content mid-stream,
not a structural parse problem with the file as a whole. This ADR and
its measurement are new in this pass; there is no first version to
correct.

## Decision

**This ADR does not decide the rate is acceptable -- it sizes it and
proposes a review rule, since the affordability call belongs to the
owner (see Owner confirmation).** Recommend: an interpretation-warning
category is excused from the "not `DECODED`" rule for a specific unit
only when docs/adr/0008's byte-count consumption witness *also* balances
for that same unit -- i.e. the warning fired, but nothing was actually
lost or misread as a result, exactly the same evidentiary bar ADR 0002
holds a parse-level warning to. A category is never excused blanket,
by name alone, regardless of what happened in the unit it fired on --
that would repeat ADR 0002's first-version mistake of trusting a
message's own reassuring wording. This is a *rule for evaluating*
categories, not a pre-approved list; no category is excused by this ADR
itself.

## Measurement

`eval/spikes/interpretation_warnings.py`: `fitz.TOOLS.mupdf_warnings()`
collected around `page.get_texttrace()`, categorised by a normalised
(numbers stripped) warning line, over every page of the 2,031-file
corpus (484 text-bearing):

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 interpretation warning | 32.2% (653/2,031) | 53.5% (259/484) |
| Pages with >= 1 interpretation warning | 16.5% (1,520/9,231) | 16.7% (1,091/6,543) |

Top categories by files affected (all files; aggregate category labels
only, per this directory's privacy rule -- font names like
"HelveticaNeue" or "SFProText-Semibold" are generic system font names,
not personal or file-identifying data):

- `invalid marked content and clip nesting` -- 425 files
- `FT_Get_Advance(<font>,<n>): invalid glyph index` (several distinct
  embedded-font subsets) -- 10-36 files each
- `JPX numcomps (<n>) doesn't match color_space (<n>)` -- 33 files
- `openjpeg warning: Found a misplaced 'cmap' box outside jp2h box` -- 30
  files
- `ignoring zlib error: incorrect data check` -- 5 files

**This is the real review-rate cost of REDESIGN §4's rule as written**:
32.2% of all files (53.5% of text-bearing files) would newly fail to
reach `DECODED` for at least one unit, before any of the value the
consumption witness itself provides is even considered. Whether any of
these categories are "benign" under the rule above (byte-count witness
balances anyway) was not measured in this pass -- that cross-check
requires running docs/adr/0008's witness on exactly the same units that
warned, which this spike does not yet do.

## Consequences

- This is a materially larger review-rate contributor than either
  docs/adr/0002's parser-agreement warnings (7.2% text-bearing) or
  docs/adr/0007's orphaned streams (11.4% text-bearing) -- the single
  largest Phase 1 cost found in this review, and one REDESIGN §4 already
  commits to ("any MuPDF warning... means not `DECODED`") without having
  sized it before this pass.
- `invalid marked content and clip nesting` alone (425 files, ~21% of the
  whole corpus) dominates the all-files count; per-glyph `FT_Get_Advance`
  warnings against specific embedded font subsets dominate the
  text-bearing count. Both look, on their face, like producer-side
  content-stream imperfections (marked-content/clip nesting depth,
  malformed subset font tables) rather than evidence of tampering, but
  "looks benign" is exactly the assumption ADR 0002's first version made
  and had to retract -- hence the per-unit verification rule recommended
  above rather than a name-based allowlist.

## Owner confirmation needed

- Whether a review-rate contribution this large (32.2% all files, 53.5%
  text-bearing) from interpretation warnings alone is affordable as
  REDESIGN §4 states the rule, or whether Phase 3c/4a needs the
  per-unit, witness-cross-checked benign rule proposed above (or a
  narrower one) before this becomes a real gate.
- Commission the cross-check measurement this ADR did not run: for each
  warned unit, does docs/adr/0008's byte-count witness also balance? That
  number, not the raw warning rate above, is what the proposed rule
  actually needs to be evaluated.
