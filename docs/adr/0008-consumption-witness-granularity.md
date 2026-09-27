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

**This is the fourth version of this ADR**, after three rounds of
review. The first version reported 92.7% reconciliation on real content
and recommended advisory. The second round found that number came from a
bug in the measuring script (see `eval/spikes/RESULTS.md`) and, once
fixed, the honest reconciliation rate was only 71.2% -- so this ADR's
second version recommended advisory again, this time because a hard gate
at that cost looked unaffordable, while still calling out that advisory
is fail-open. The third round root-caused most of that shortfall, to
98.7% (2,235/2,264 text pages; 9 of 326 in-scope files mismatching), and
reported a residual it could not explain. **This round explained the
residual: all 9 files were artefacts of the spike itself** (causes 7 and
8 below). On the corrected script every measured text page reconciles;
what remains is the pages the spike cannot yet measure (Measurement,
below).

## The root causes of the earlier figures

Re-running this spike's own output against real content found eight
distinct causes, seven of them bugs in the measuring script itself
(never in `verify.py`, which this pass does not touch), one a real,
narrow modelling gap (6):

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
   origin) across the page's trace, confirmed against real content:
   `(SAMPLE)` shown once under `2 Tr` produces two 6-char spans with
   identical (glyph, origin) pairs. (Cause 7 narrows where this applies.)
6. **A few mixed-width CJK CMaps** (e.g. `90msp-RKSJ-H`) are not 1- or
   2-byte fixed-width and are not modelled by this script's simple
   code-length table. A page using one is skipped, not silently
   miscounted, and reported separately from pages skipped for drawing a
   Form XObject.
7. **The de-duplication in 5 was applied to every page, and removed real
   draws.** On a page that never sets `2 Tr` or `6 Tr`, a repeated
   (glyph, origin) pair is a real second draw -- a run of zero-width
   glyphs at one position, or the same text painted twice at one spot --
   which the code count also counts. Removing it manufactured a
   mismatch; this caused 8 of the 9 mismatching files in the previous
   version. De-duplication is now confined to pages that set render mode
   2 or 6, and a hand-built control (the same text painted twice at one
   spot under `0 Tr`) reconciles at 10 codes = 10 glyphs.
8. **The Form XObject skip was a regex that both missed and over-matched.**
   It required whitespace before the XObject name, so it missed
   `0 TL/Fm0 Do` -- the ninth file, a page drawing a form the spike
   compared as if it did not -- and it skipped any page drawing *any*
   XObject, images included. Each `Do` operand is now resolved in the
   page's resources, and only a draw of a real `/Subtype /Form` XObject
   skips the page, so image-only pages are now measured.

## Decision

**Recommend a hard gate: a witness mismatch means `FLAGGED`, never
`DECODED`, unconditionally -- no advisory mode.** Per the fail-closed
rule this whole review is built around, and per REDESIGN §2/Principle 2
directly (Context, above), this is the only design that does not create
a new exit-`0` path once legacy retires. The corrected measurement
(below) finds no unexplained mismatch among the pages it can measure.

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

**What the gate is for, stated precisely.** Three hand-built cases
(`eval/spikes/s1b_consumption_witness.py`, `ADVERSARIAL_CASES`) produce
a witness mismatch with no MuPDF warning at all. **None of them is a
false exit `0` under today's tool**, and one is not a leak at all:

- **Clip-only text (`Tr 7`)**: drawn, zero texttrace glyphs. Today's
  `verify.py` exits `1` on it (checked directly with a value rule: the
  Objects layer finds the string, and so does the Text layer, since
  `get_text()` still returns clip text).
- **Text inside a switched-off optional-content group** (REDESIGN §8's
  K6 shape): absent from both `get_texttrace()` and `get_text()`, present
  in the raw stream. Today's `verify.py` exits `1` on it (the Objects
  layer finds the string).
- **An inline image whose declared-length pixel data literally spells a
  second, complete text-show operation** (`... EI (PHANTOM) Tj ...`,
  still inside the image's own declared byte range): MuPDF correctly
  reads it as opaque pixels; this spike's own naive `EI`-search
  inline-image skip (the same technique `verify.py`'s current tokenizer
  uses) re-parses the tail as content and counts a show operation that
  was never drawn. **This is the spike's tokenizer over-counting -- a
  false flag, not a leak shape the gate closes.** What it does show is
  that a naive image-length skip is unsound, so REDESIGN §4's
  declared-length-aware tokenizer is required for Phase 4a.

The gate matters for the rewrite's `DECODED` discharge (REDESIGN §2: a
unit is discharged only when its decoder's witness proves it consumed
the unit), not for today's tool: once a content unit can be `DECODED`,
the `Tr 7` and switched-off-layer shapes are exactly what a decoder
that trusted `get_texttrace()` alone would discharge while text it never
reported sits in the stream.

(`Tr 3`, ordinary invisible text as used by real OCR text layers, `Tr 2`
fill+stroke, de-duplicated, and the same text painted twice under
`0 Tr`, not de-duplicated, all reconcile exactly -- included in
`ADVERSARIAL_CASES` as the comparison. This is not "any unusual render
mode misbehaves.")

**Correcting an overclaim from the first version**: it said spike S1b
"confirmed" the mechanism requires a scratch page holding one resolved
(stream, context) unit in isolation, as REDESIGN §4 describes. What S1b
actually measures is a **whole-page** `page.read_contents()` vs. a
**whole-page** `get_texttrace()`, with pages that draw a Form XObject
skipped (856 of 9,231 -- entirely unmeasured, not counted either way)
rather than resolved and measured as their own unit. This is consistent
with, but does not confirm, REDESIGN's actual per-unit design; a real
per-unit measurement (one resolved stream on its own scratch page, per
REDESIGN §4) has still not been built. Phase 4a's own implementation is
where that gets built and confirmed for real.

## Measurement

`eval/spikes/s1b_consumption_witness.py`, corrected per the eight root
causes above, same corpus (PyMuPDF 1.28.2):

| Pass | Reconciliation rate (text pages) | In-scope files | Files with any mismatch |
| --- | --- | --- | --- |
| Naive whole-page | 97.3% (2,448/2,515) | 436 | 7 (1.6%) |
| **Per-decoding-unit** (skips Form-XObject and mixed-width-CMap pages) | **100.0%** (2,345/2,345) | 399 | **0** |

("Text pages" excludes a page where both code_count and glyph_count are
zero -- most corpus pages show no text at all and would otherwise pad
the rate with a meaningless 0-equals-0 "match": 6,018 of the 8,363
measured pages in the per-decoding-unit pass.)

**No unexplained mismatch remains among measured pages.** The previous
version's residual (9 files) was causes 7 and 8, both spike artefacts.

**What is not measured**, stated plainly -- of the corpus's 9,231 pages:

- **856 pages, in 206 files, draw a Form XObject** and are skipped
  entirely. Their text is inside the form, not the page's own content
  stream; REDESIGN §4 measures each form as its own unit, which this
  spike does not build. Their reconciliation rate is unknown.
- **12 pages, in 9 files, use an unmodelled mixed-width CJK CMap** and
  are skipped.
- No `Do` operand failed to resolve to either a form or an image (0
  pages).

**Caveat: the witness compares counts, not content.** A tokenizer error
that adds codes and an interpreter behaviour that adds glyphs could in
principle cancel on the same page and balance. Not observed here (no
case or corpus page showed it), but a balanced count is evidence that
the stream was consumed, not proof that every code was read correctly.

## Consequences

- The hard-gate recommendation applies to the whole witness (render-mode
  handling and the general byte-count check together) -- there is no
  reason to split them.
- On the pages this spike can measure, the gate's review-rate cost is 0
  unexplained mismatches (0 of 399 in-scope files). The open cost is the
  unmeasured pages above: 856 form-drawing pages (206 files) and 12 CJK
  pages (9 files). Phase 4a must measure forms as their own units; any
  it cannot measure are `FLAGGED` under this gate, and that rate is what
  Phase 4a's own gate ("K3-K6... closed; every per-case change is
  stricter and listed; review-rate change within what [ADR 0007]
  accepted") must account for.
- If the owner picks the shadow-mode alternative, Phase 3b's existing
  shadow/enforced verdict machinery is the right place to wire it, not a
  new mechanism -- REDESIGN §6 already describes exactly this kind of
  staged enforcement for unit kinds generally.

## Owner confirmation needed

Hard gate now (recommended -- REDESIGN §2/Principle 2 require it; on
the corrected spike, 0 unexplained mismatches among 2,345 measured text
pages in 399 files, with 856 form-drawing pages in 206 files and 12 CJK
pages in 9 files unmeasured) vs. hard gate in shadow mode first,
enforced once Phase 4a's own implementation measures forms as their own
units and reconfirms the rate on a per-unit (not whole-page) basis.
