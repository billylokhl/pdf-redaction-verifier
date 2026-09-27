# 0008. Consumption-witness granularity

Status: proposed

## Context

REDESIGN.md §4 states the consumption witness as: "our own content
tokenizer counts the character codes in every text-show operand (using
each font's code length) and this must equal the glyph count in the text
trace. Any MuPDF warning while interpreting the stream also means not
`DECODED`." Two of the plan's own core principles depend on this witness
actually gating, not merely informing: §2 says a unit's obligation "is
discharged only when its decoder **consumed** it completely — proved by
a witness"; Principle 2 says "`DECODED` is accepted only when the
decoder's witness balances." **Advisory-only is fail-open with respect
to both of these** -- it lets `DECODED` happen without the witness
actually balancing, which is precisely what §2 and Principle 2 rule out.
That is masked during the transition period by worst-of shipping (legacy
stays at least as strict), but it becomes a real, un-masked exit-`0`
path the moment Phase 6 retires legacy. This ADR is not one of REDESIGN's
seven named Phase 1 questions, but what spike S1b found needed a
decision of its own.

**This is the third version of this ADR**, after two rounds of review.
The first version reported 92.7% reconciliation on real content and
recommended advisory. The second round found that number came from a bug
in the measuring script (see docs/adr's sibling note in
`eval/spikes/RESULTS.md`) and, once fixed, the honest reconciliation
rate was only 71.2% -- so this ADR's second version recommended advisory
again, this time because a hard gate at that cost looked unaffordable,
while still calling out that advisory is fail-open. **A third round
root-caused essentially all of that 71.2%'s shortfall** (below) down to
a real reconciliation rate of ~98.7%, at which point a hard gate is
affordable and the fail-open problem should not be accepted at all.

## The root cause of the second version's 71.2% figure

Re-running this spike's own committed output against real content found
five distinct causes, four of them bugs in the measuring script itself
(never in `verify.py`, which this pass does not touch), one a real,
narrow modelling gap:

1. **The glyph-exclusion rule itself was unsound and is now removed.**
   `doc.get_char_widths` resolves a code through the font's own cmap as
   if it were a Unicode code point -- it ignores `/Encoding`,
   `/Differences`, and `/CIDToGIDMap` entirely, and returns an empty
   table for a font it cannot load this way. For 690 real pages that
   fabricated "this font has no glyphs at all," excluding every code
   shown and producing a false 0-equals-0 match that hid the real
   question. The premise behind the exclusion was also false:
   `get_texttrace()` DOES emit a char entry (Unicode replacement
   character, glyph id 0) for a code with no glyph --
   `(A\x01\x02B\x7f\x81)` shown in Helvetica traces 6 chars, not 2, in a
   direct check. **The exclusion is removed entirely.**
2. **This spike's own literal-string decoder had the same two bugs
   `verify._decode_pdf_string` has** (kept a backslash-end-of-line
   continuation as a literal newline; never normalised a raw CRLF inside
   a literal to a single LF) -- both spec violations (PDF 32000-1
   §7.3.4.2). Fixed **locally in this spike script**
   (`_decode_literal_fixed`), not in `verify.py`.
3. **Annotation *and form-widget* appearance text is included in a
   whole-page `get_texttrace()` call** -- MuPDF's page interpreter runs
   both. `page.annots()` does not enumerate widgets (PyMuPDF surfaces
   those separately via `page.widgets()`), so deleting only `annots()`
   looked sufficient until a filled-in form field's value showed up as
   an unexplained mismatch. Both are now deleted from the in-memory page
   before tracing (this spike never writes the file).
4. **A ToUnicode continuation entry** (glyph id -1 -- a second or further
   Unicode character folded into one glyph, e.g. a ligature's expansion)
   was being counted as an extra glyph. Excluded now.
5. **A fill-then-stroke render mode (`Tr` 2/6) draws, and
   `get_texttrace()` reports, the same glyph twice -- as two separate
   spans, not two entries in one span.** De-duplicated by (glyph id,
   origin) across the whole page's trace, confirmed against real
   content: `(SAMPLE)` shown once under `2 Tr` produces two 6-char spans
   with identical (glyph, origin) pairs.
6. **A few mixed-width CJK CMaps** (e.g. `90msp-RKSJ-H`) are not 1- or
   2-byte fixed-width and are not modelled by this script's simple
   code-length table. A page using one is now skipped, not silently
   miscounted, and reported separately from pages skipped for drawing a
   Form XObject.

## Decision

**Recommend a hard gate: a witness mismatch means `FLAGGED`, never
`DECODED`, unconditionally -- no advisory mode.** Per the fail-closed
rule this whole review is built around, and per REDESIGN §2/Principle 2
directly (Context, above), this is the only design that does not create
a new exit-`0` path once legacy retires. The corrected measurement
(below) shows the cost of doing this now is small.

**Alternative, if the owner is not comfortable committing to
enforcement from a Phase 1 spike's numbers: ship the hard gate in shadow
mode first (recorded, not shipped in the worst-of verdict -- REDESIGN
§6's existing shadow/enforced split already has a mechanism for exactly
this), and move it into the enforced verdict once Phase 4a's own
implementation confirms the corpus rate.** This is not the same as
"advisory" -- shadow mode still computes the gate as a hard flag, it
only defers *shipping* that flag's effect on the exit code, which
Phase 6 already requires resolving before legacy retires. Advisory (the
first two versions' choice) has no such resolution point and was wrong
to recommend.

Three verified, warning-free adversarial cases (`eval/spikes/
s1b_consumption_witness.py`, `ADVERSARIAL_CASES`) demonstrate what a
gate closes:

- **Clip-only text (`Tr 7`)**: drawn, zero texttrace glyphs, `get_text()`
  still returns it.
- **An inline image whose declared-length pixel data literally spells a
  second, complete text-show operation** (`... EI (PHANTOM) Tj ...`,
  still inside the image's own declared byte range): MuPDF correctly
  reads it as opaque pixels; this spike's own naive `EI`-search inline-
  image skip (the same technique `verify.py`'s current tokenizer uses)
  re-parses the tail as real content and finds a phantom show operation
  that was never drawn -- the safe direction (over-counting, not a
  miss), but proof that a naive image-length skip is unsound and REDESIGN
  §4's declared-length-aware design is required, not optional, for
  Phase 4a.
- **Text inside a switched-off optional-content group**: absent from
  both `get_texttrace()` and `get_text()`, present in the raw stream --
  REDESIGN §8's K6 shape, reproduced directly.

(`Tr 3`, ordinary invisible text as used by real OCR text layers, and
`Tr 2` fill+stroke, now de-duplicated, both reconcile exactly -- included
in `ADVERSARIAL_CASES` as the comparison. This is not "any unusual
render mode misbehaves.")

**Correcting an overclaim from the first version**: it said spike S1b
"confirmed" the mechanism requires a scratch page holding one resolved
(stream, context) unit in isolation, as REDESIGN §4 describes. What S1b
actually measured is a **whole-page** `page.read_contents()` vs. a
**whole-page** `get_texttrace()`, with pages that draw through a Form
XObject skipped (994 of 9,231 -- entirely unmeasured, not counted either
way) rather than resolved and measured as their own unit. This is
consistent with, but does not confirm, REDESIGN's actual per-unit
design; a real per-unit measurement (one resolved stream on its own
scratch page, per REDESIGN §4) has still not been built. Phase 4a's own
implementation is where that gets built and confirmed for real.

## Measurement

`eval/spikes/s1b_consumption_witness.py`, corrected per the six root
causes above, same corpus:

| Pass | Reconciliation rate (text pages) | In-scope files | Files with any mismatch |
| --- | --- | --- | --- |
| Naive whole-page | 96.1% (2,417/2,515) | 436 | 18 (4.1%) |
| **Per-decoding-unit** (skips Form-XObject and mixed-width-CMap pages) | **98.7%** (2,235/2,264) | 326 | **9 (2.8%)** |

("Text pages" excludes a page where both code_count and glyph_count are
zero -- most corpus pages show no text at all and would otherwise pad
the rate with a meaningless 0-equals-0 "match": 5,963 of 8,227 measured
pages in the per-decoding-unit pass.)

**A small residual remains and is not fully root-caused**: the 9
mismatching in-scope files show small (1-3 code), not-render-mode-related
discrepancies on otherwise ordinary MacRoman-encoded text with no
control characters and no Form XObjects -- the same general shape as an
earlier, larger residual, at roughly 1/40th the size. This is honestly
reported as unresolved, not swept in with the fixes above; it is small
enough that a hard gate's cost (flagging roughly 3 files in 100 that
would otherwise certify) is a reasonable trade rather than a blocking
concern.

## Consequences

- The hard-gate recommendation applies to the whole witness (render-mode
  handling and the general byte-count check together) -- there is no
  longer a reason to split them, now that the general check's real cost
  is known to be small.
- A ~2.8% in-scope-file mismatch rate is a real, if small, addition to
  the review rate Phase 4a's own gate ("K3-K6... closed; every per-case
  change is stricter and listed; review-rate change within what
  [ADR 0007] accepted") must account for.
- If the owner picks the shadow-mode alternative, Phase 3b's existing
  shadow/enforced verdict machinery is the right place to wire it, not a
  new mechanism -- REDESIGN §6 already describes exactly this kind of
  staged enforcement for unit kinds generally.

## Owner confirmation needed

Hard gate now (recommended -- REDESIGN §2/Principle 2 require it, and
the corrected cost is ~2.8% of in-scope files) vs. hard gate in shadow
mode first, enforced once Phase 4a's own implementation reconfirms the
rate on a per-unit (not whole-page) basis.
