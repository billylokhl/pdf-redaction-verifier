# 0008. Consumption-witness granularity

Status: proposed

## Context

REDESIGN.md §4 states the consumption witness as: "our own content
tokenizer counts the character codes in every text-show operand (using
each font's code length) and this must equal the glyph count in the text
trace. Any MuPDF warning while interpreting the stream also means not
`DECODED`." Phase 1's spike S1b was assigned to test this. This ADR is
not one of REDESIGN's seven named Phase 1 questions, but what S1b found
needs a decision of its own.

**Correction to the first version of this ADR and its measurement.**
The spike script had a real bug: numeric content-stream operands were
never pushed onto its operand stack, so a `/F0 12 Tf` sequence never set
the tracked font (`operands[-2]` was never the font name — there was
only ever one operand, the name itself). Every font therefore silently
counted as width 1, including 2-byte Identity-H/V fonts. The first
version's crude "exclude any C0 control byte" correction then
**coincidentally cancelled part of this bug's effect**, because a 2-byte
CID code's high byte is often `0x00` (a C0 byte) for common BMP code
points -- excluding it by byte value happened to approximate the correct
divide-by-2 the Tf bug had skipped. The reported 92.7% reconciliation
rate was real output, but not evidence of what it was presented as.

Both bugs are fixed in this version: numeric operands are kept on the
stack (so `Tf` is recognised), and the "no glyph" exclusion is now
computed **per font**, from that font's own glyph table
(`doc.get_char_widths` -- glyph id 0 means no glyph, i.e. `.notdef`),
never from a byte's numeric value. The corpus was re-measured with the
fix; the new numbers are materially different and are what this
decision is actually based on.

## Decision

**Two separable findings, two separable actions:**

**1. Text render mode must be accounted for -- this part is not a close
call and needs no further measurement to justify.** Two hand-built,
verified, **warning-free** cases (`eval/spikes/s1b_consumption_witness.py`,
`ADVERSARIAL_CASES`) show `get_texttrace()` diverging from what was
actually shown, with no MuPDF warning to catch either one:

- **`Tr 7` (add-to-clip-path, i.e. invisible-and-not-even-for-OCR):**
  13 codes shown, **0** texttrace glyphs, no warning. `page.get_text()`
  still returns the text. This is the shape of a genuinely hidden
  leftover: a decoder relying on `get_texttrace()` glyph presence alone
  would see nothing here and could still reach `DECODED` via other
  content in the same unit, silently carrying Tr-7 text through
  un-flagged.
- **`Tr 2` / `Tr 6` (fill-then-stroke):** 6 codes shown, **12** texttrace
  glyphs (exactly double), no warning -- `get_texttrace()` reports each
  glyph twice, once per paint operation.

  (For comparison: `Tr 3`, ordinary invisible text as used by real OCR
  text layers under a scanned image, reconciles exactly -- 6 codes, 6
  glyphs, confirmed. This is not a blanket "any invisible text mode
  misbehaves" finding; it is specific to modes 2, 6, and 7.)

  **Decision:** Phase 4a's real witness must treat these deterministically,
  not as generic "mismatch -> flag" noise: modes 2/6 should be corrected
  for (halve the expected count, or de-duplicate by glyph origin, before
  comparing) so that ordinary bold/outlined text does not spuriously
  flag; mode 7 should **not** be corrected for -- the mismatch it
  produces is exactly the signal that matters, and "fixing" the
  expectation to tolerate it would silently remove the one thing this
  spike was built to catch. This requires no further measurement and no
  owner decision -- it is a specification detail for whoever implements
  Phase 4a, independent of the broader question below.

**2. The general byte-count consumption witness, once (1) is corrected
for, is *proposed* as a hard `DECODED` gate -- but the real, corrected
reconciliation rate is far lower than first reported, and the owner
should see the honest number before this ships.** Per the fail-closed
rule this whole review is built around ("any state the design can't
positively vouch for must be `FLAGGED`"), a byte-count mismatch should
in principle always mean `FLAGGED`, never advisory-only. The first
version of this ADR shipped "advisory" specifically because 92.7%
reconciliation made a hard gate look nearly free. **That number was
wrong** (see Correction above). The honest, corrected number is much
higher-cost -- see Measurement.

## Measurement

`eval/spikes/s1b_consumption_witness.py`, corrected script, same 2,031-
file corpus:

| Pass | Reconciliation rate | Mismatches without a warning |
| --- | --- | --- |
| Naive whole-page (informative only -- wrong unit, see below) | 93.6% (8,637/9,231 pages) | 482 |
| **Per-decoding-unit** (skips pages with Form XObjects; excludes, per font, codes with no real glyph) | **71.2%** (5,868/8,237 pages) | **1,925** |

The per-decoding-unit pass is the one REDESIGN §4 actually specifies
(one resolved stream on its own scratch page); the naive whole-page pass
is kept only to document why a whole-page comparison is the wrong shape
(a Form XObject's glyphs are not in `page.read_contents()` at all).

**71.2%, not 92.7%, is the real number.** A breakdown of the 1,925
warning-free mismatches by the page's font encodings:

```
MacRomanEncoding (mixed with an unlabelled font): 1,252
MacRomanEncoding alone:                             277
Identity-H alone:                                   225
Identity-H mixed with MacRomanEncoding:              31
WinAnsiEncoding alone:                               12
Identity-H mixed with WinAnsiEncoding:               11
(other, small counts):                              < 10 combined
```

Manually inspecting sample mismatches in the largest (MacRoman) bucket
found small, near-exact discrepancies -- e.g. 354 codes counted against
356 glyphs shown, on ordinary address-block text with no unusual render
mode, no control characters, and no Form XObjects. **This residual is
not the render-mode issue in Decision (1)** (these samples use no
non-default `Tr` at all) **and is not root-caused by this pass.** The
leading hypothesis -- `doc.get_char_widths`'s own glyph-presence signal
diverging from MuPDF's actual rendering-time glyph resolution for some
subset/embedded simple fonts -- is plausible but unconfirmed; it was not
possible to verify within this review's time budget.

## Consequences

- Decision (1) (Tr-mode handling) should be implemented in Phase 4a
  regardless of how Decision (2) resolves -- it is cheap, deterministic,
  and closes a real, demonstrated silent-miss risk.
- Decision (2), if the owner picks "hard gate now": roughly 29% of
  text-bearing per-unit content would be held out of `DECODED` purely by
  witness noise, not real malformed content, until the MacRoman/Identity-H
  residual above is root-caused -- a large, currently-unbounded
  review-rate cost that was not visible under the first version's
  (incorrect) 92.7% figure.
- Decision (2), if the owner picks "advisory until root-caused": the
  Tr-7-shaped risk is fully covered anyway by Decision (1)'s
  unconditional fix, so the remaining exposure from shipping the general
  witness as advisory is limited to *other, not-yet-found* silent-skip
  shapes beyond the ones this pass's adversarial cases covered --
  smaller than the first version implied advisory mode carried, since
  the specific concrete case this review raised (Tr 7) is independently
  closed either way.

## Owner confirmation needed

Whether to ship the general byte-count witness as a hard `DECODED` gate
now (fail-closed default, ~29% measured review-rate cost on text-bearing
per-unit content pending root-cause) or advisory-only pending root-cause
of the MacRoman/Identity-H residual (lower cost now, relies on Decision
(1) alone to close the specific silent-miss shape this pass demonstrated).
Recommend: advisory *specifically for the general byte-count check* with
Decision (1)'s Tr-mode handling shipped as a hard, unconditional part of
the font/render witness regardless -- but this trades off differently
depending on how much the owner weighs "fail closed always" against a
~29% unexplained review-rate cost, so it is presented as a
recommendation, not a settled decision.
