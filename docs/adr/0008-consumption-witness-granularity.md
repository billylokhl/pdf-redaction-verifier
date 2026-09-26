# 0008. Consumption-witness granularity and exclusions

Status: accepted, ships as advisory pending owner confirmation

## Context

REDESIGN.md §4 states the consumption witness as: "our own content
tokenizer counts the character codes in every text-show operand (using
each font's code length) and this must equal the glyph count in the text
trace. Any MuPDF warning while interpreting the stream also means not
`DECODED`. Feasibility is part of spike S1." This is not one of the
seven questions the Phase 1 row lists as needing an ADR, but the row
also assigns Phase 1 spike S1b specifically to test this claim, and
what S1b found changes how the mechanism must be implemented -- enough
that it belongs in this set rather than only in a spike script's
comments.

## Decision

**Adopt the witness, at the granularity REDESIGN §4 already specifies
for the mechanism as a whole (§4, "Decoding content streams"): one
resolved (stream, context) unit attached to its own scratch page, never
a whole assembled page compared against a whole-page texttrace.** This
was already the plan's stated mechanism; S1b's contribution is
confirming it is load-bearing, not optional, and adding one refinement
the plan's wording did not spell out:

**A code that a simple font's encoding maps to no glyph (a C0 control
code other than tab) must be excluded from the witness's expected glyph
count**, not counted as a code that should have produced a shown glyph.
`get_texttrace()` does not report an entry for it; a witness that
expects one will falsely refuse `DECODED` on ordinary content.

**Ship this as advisory in Phase 4a, not as a hard `DECODED` gate, until
the residual gap below is root-caused.** The witness still runs and its
result is recorded as evidence either way; what changes is whether a
mismatch alone can hold a unit out of `DECODED` on real content Phase 4a
has not yet proven safe to gate on.

## Measurement

Full detail and the adversarial-case table are in `eval/spikes/
RESULTS.md` §a; summary:

- On five hand-built adversarial content streams attached to a scratch
  page (unknown operator, unbalanced `BT`, stray `Q`, a well-formed
  inline image, and one with a truncated inline image), the witness
  reconciled exactly on the four well-formed cases and caught the one
  built to break MuPDF's inline-image parser (23 codes sent, 12 glyphs
  shown) -- the exact failure mode REDESIGN §4 cites as motivation.
- A **naive whole-page** comparison over 9,231 real corpus pages
  reconciles only 88.9% of the time. Diagnosing the gap found two causes,
  neither a malformed-content signal: (1) a Form XObject's glyphs appear
  in the page's texttrace but its bytes are not in `page.
  read_contents()` at all -- confirmation that the per-unit mechanism
  above is required, not optional; (2) glyph-less C0 control codes
  embedded in otherwise normal strings (this ADR's exclusion rule).
- Applying both fixes (isolating pages with no Form XObject, excluding
  C0 codes) raises reconciliation to **92.7%** (7,633 / 8,233 pages).
  The remaining **7.3%** is concentrated in Identity-H (CJK) content and
  was not root-caused in this pass -- the leading candidate is
  `get_texttrace()`'s `chars` count reflecting ToUnicode-mapped output
  characters for some multi-byte CMaps rather than one entry per drawn
  glyph. This pass checked one sample page for the most obvious tell
  (duplicate-origin `chars` entries, which a multi-character expansion
  would produce) and did not find it, so the mechanism remains
  unconfirmed, not ruled out.

## Consequences

- Phase 4a's decoder registry entry for content streams, forms,
  annotation appearances, and Type3 glyph procedures should implement
  the witness with the exclusion rule above from the start -- shipping
  the naive count-every-code version would misfire on a meaningful
  share of real CJK/Identity-H content (see Owner confirmation).
- Advisory-only means a mismatch is recorded (evidence, and in the
  shadow verdict once Phase 3b exists) but a unit is not held out of
  `DECODED` on the witness's word alone until Phase 4a's own work
  resolves the residual 7.3% -- otherwise Phase 4a would ship a
  same-order-of-magnitude false-negative-for-DECODED rate to the one
  this pass just spent docs/adr/0005 trying to bring *down*.
- MuPDF-warning-based rejection (the other half of REDESIGN §4's
  sentence: "any MuPDF warning... also means not `DECODED`") is
  unaffected by this ADR and should ship as originally specified --
  every adversarial mismatch in this pass's testing also produced a
  MuPDF warning, so warning-based rejection alone already covers the
  cases found so far; the witness is corroborating defense-in-depth
  that does not depend on MuPDF's warning wording staying stable across
  versions, not the sole detection path.

## Owner confirmation needed

- Confirm shipping the witness as advisory-only in Phase 4a (rather than
  delaying Phase 4a's `DECODED` enforcement until the CJK residual is
  fully root-caused, or shipping it as a hard gate and accepting some
  false non-`DECODED` results on real CJK content).
