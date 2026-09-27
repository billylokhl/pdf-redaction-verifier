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
corpus (484 text-bearing).

**This number moved by 3x between two runs of this same pass, from a
dependency bump alone -- itself a finding, not just noise.** The first
measurement (PyMuPDF 1.27.2.3) found 32.2% of all files / 53.5% of
text-bearing files affected, dominated by `invalid marked content and
clip nesting` (425 files). Rebasing onto `main` picked up a dependency
bump to PyMuPDF 1.28.2 (a newer bundled MuPDF), and re-running the exact
same script found:

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 interpretation warning | **11.2% (227/2,031)** (was 32.2%) | **45.7% (221/484)** (was 53.5%) |
| Pages with >= 1 interpretation warning | 5.0% (459/9,219) | 6.9% (453/6,543) |

`invalid marked content and clip nesting` **no longer appears in the top
categories at all** -- the newer MuPDF release evidently stopped emitting
it (or stopped hitting the condition) for every one of the 425 files
that produced it before. The remaining categories (`FT_Get_Advance`
per-font-subset warnings, `JPX numcomps`/`openjpeg` JPEG2000 warnings)
are stable across both versions.

Top categories by files affected on the current (1.28.2) measurement --
aggregate category labels only, per this directory's privacy rule; font
names like "HelveticaNeue" or "SFProText-Semibold" are generic system
font names, not personal or file-identifying data:

- `FT_Get_Advance(<font>,<n>): invalid glyph index` (several distinct
  embedded-font subsets) -- 4-36 files each
- `JPX numcomps (<n>) doesn't match color_space (<n>)` -- 33 files
- `openjpeg warning: Found a misplaced 'cmap' box outside jp2h box` -- 30
  files

**This is the real review-rate cost of REDESIGN §4's rule as written,
as of the currently pinned PyMuPDF version**: 11.2% of all files (45.7%
of text-bearing files) would newly fail to reach `DECODED` for at least
one unit. The 3x swing between two point releases is itself the
strongest argument in this ADR for the per-unit, witness-cross-checked
rule over a name-based allowlist: a category that vanishes on a routine
dependency bump was never a stable signal about document content in the
first place, and a rule keyed to its name (benign or not) would silently
change behaviour on the next MuPDF upgrade with no code change of our
own. Whether any category is "benign" under the proposed rule (byte-count
witness balances anyway) was not measured in this pass -- that
cross-check requires running docs/adr/0008's witness on exactly the same
units that warned, which this spike does not yet do.

## Consequences

- At 45.7% of text-bearing files, this is still a materially larger
  review-rate contributor than either docs/adr/0002's parser-agreement
  warnings (7.2% text-bearing) or docs/adr/0007's orphaned streams
  (11.4% text-bearing) -- the single largest Phase 1 cost found in this
  review, and one REDESIGN §4 already commits to ("any MuPDF warning...
  means not `DECODED`") without having sized it before this pass.
- The remaining categories (per-glyph `FT_Get_Advance` warnings against
  specific embedded font subsets; `JPX numcomps`/`openjpeg` JPEG2000
  warnings) look, on their face, like producer-side imperfections
  (malformed subset font tables, non-conforming JPX streams) rather than
  evidence of tampering, but "looks benign" is exactly the assumption
  ADR 0002's first version made and had to retract -- hence the per-unit
  verification rule recommended above rather than a name-based allowlist.
- The version-sensitivity finding itself has a consequence beyond this
  ADR: any Phase 1/3c measurement that counts *by warning category name*
  should be re-verified whenever a MuPDF/PyMuPDF version bump lands, not
  assumed stable -- docs/adr/0002's qpdf-based categories are a
  different, external tool and were not affected by this bump, but a
  future qpdf upgrade could plausibly do the same thing to those.

## Owner confirmation needed

- Whether a review-rate contribution this large (11.2% all files, 45.7%
  text-bearing, as currently measured against the pinned PyMuPDF 1.28.2)
  from interpretation warnings alone is affordable as REDESIGN §4 states
  the rule, or whether Phase 3c/4a needs the per-unit, witness-cross-
  checked benign rule proposed above (or a narrower one) before this
  becomes a real gate.
- Commission the cross-check measurement this ADR did not run: for each
  warned unit, does docs/adr/0008's byte-count witness also balance? That
  number, not the raw warning rate above, is what the proposed rule
  actually needs to be evaluated.
- Given the 3x swing measured between two PyMuPDF point releases,
  consider whether this measurement should be re-run as part of routine
  dependency-bump review (e.g. Dependabot PRs touching PyMuPDF), not just
  once at Phase 1.
