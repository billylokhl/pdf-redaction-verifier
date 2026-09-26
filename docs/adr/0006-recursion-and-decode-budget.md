# 0006. Recursion and decode budget

Status: accepted, one item needs owner confirmation

## Context

REDESIGN.md §4 says the decoding worklist needs "**Budget**: one object
for the whole run including recursion -- depth, bytes inflated, units,
OCR pixels. Exhaustion -> flagged," and Principle 4 requires hostile-input
handling to be resource-bounded and work-based rather than wall-clock
based. Neither the plan nor `verify.py` fixes the actual numbers.
`verify.py` today has exactly one comparable cap, `MAX_ATTACHMENT_BYTES
= 16 << 20` (16 MiB, applied per-attachment, not cumulatively) and
`MAX_EARLIER_REVISIONS = 50` -- both flagged in §1 as "tuned magic
numbers, each a bypass," which this ADR is meant to replace with named,
reasoned bounds rather than repeat.

Recursion specifically matters for: nested Form XObjects (a form drawing
another form), Type3 glyph procedures (each glyph is its own content
stream, itself capable of drawing more content), recursive/embedded PDFs
(Phase 4c), and container formats (zip/Office) that can nest arbitrarily.
Spike S1b (docs/adr/0006 for the mechanism it validates and where it
does not yet) is the closest empirical link this pass has to how
expensive one decoding unit actually is, which bears on how many units a
depth/count budget can affordably allow.

## Decision

One shared `Budget` object for the whole run, decremented as work
happens (Principle 4's "work-based" limits), with these named bounds:

- **Depth <= 25.** Deep enough for any legitimate nesting this pass
  found reason to expect (a form inside a form inside an annotation
  appearance is 3; even a deliberately obfuscating producer chaining ten
  forms is comfortably inside 25) and shallow enough that a
  denial-of-service via self-referential or mutually-nested forms cannot
  make meaningful progress before the budget trips. Depth counts once
  per content-stream-or-container boundary crossed (a form `Do`, a
  Type3 glyph invocation, a recursive-PDF unwrap, a zip/Office entry),
  not per PDF object.
- **Units <= 200,000 decoded per run.** Sized against this pass's
  corpus: the single largest object count seen while building
  `inventory_lite.py`'s tiler was in the low hundreds per file (largest
  observed: 258 objects, `eval/spikes/RESULTS.md`'s files are all under
  1,300 objects going by the same tiler's `object_count` field); 200,000
  leaves roughly two orders of magnitude of headroom for a legitimately
  large multi-thousand-page document while still bounding a
  decompression-bomb-style object-stream fan-out.
- **Inflated bytes <= 2 GiB cumulative.** Matches the general order of
  magnitude of the existing per-attachment cap (16 MiB) scaled up for a
  whole-run budget covering many attachments/streams rather than one,
  while remaining far below what would risk the parent process's own
  memory watchdog (§4's sandbox, Phase 3d) tripping from the child's
  legitimate work rather than a bomb.
- **OCR pixels**: governed by docs/adr/0004's per-image envelope
  (35 Mpx per image) plus a **cumulative** run cap of 500 Mpx, so a
  document with many images at or near the per-image cap cannot turn
  into an unbounded total OCR bill even though no single image exceeds
  its own envelope.

**Exhaustion flags, it does not fail closed to exit `2` for the whole
run.** The unit that tripped the budget (and every unit still queued
behind it) becomes `FLAGGED`; units already `DECODED` keep that status.
This follows Principle 1's letter (an unhandled status cannot produce
`0`; `FLAGGED` cannot either) without turning one oversized attachment
into a whole-file `2` the way a wall-clock kill (Principle 4's "any kill
-> the child's unreported obligations are `FAILED`") already would for a
more serious failure. Budget exhaustion is an expected, boring outcome
for large-but-legitimate files; a kill is not.

## Measurement

Spike S1b (docs/adr/0008, `eval/spikes/RESULTS.md` §a) is the only Phase
1 evidence that bears on per-unit cost, and only indirectly: every
adversarial case and every real-corpus page it checked reconciled (or
was diagnosed as a known confound) using a single pass over one content
stream's tokens plus one `get_texttrace()` call -- cheap enough that
depth and unit-count, not per-unit CPU time, look like the binding
constraints in practice. This pass did not build a decompression-bomb or
deeply-nested-forms fixture to empirically find where these bounds
actually bite -- the numbers above are sized by analogy to the existing
attachment cap and this pass's corpus object counts (`eval/spikes/
inventory_lite.py`'s tiler), not by a dedicated stress spike.

## Consequences

- Every recursive decoder (forms, Type3, containers, recursive PDFs)
  shares one counter; a document that is legitimately deep in one
  dimension (many attachments) and shallow in another (no nested forms)
  is not penalized by a per-kind budget that would otherwise need its
  own separate tuning.
- Because exhaustion flags rather than fails the whole run, a
  budget-exhausted file cannot silently reach exit `0` -- `FLAGGED`
  units block certification the same way any other undischarged
  obligation does (Principle 1).
- These are named, single-purpose numbers per Principle 9 and each needs
  its own pinning test once Phase 3d builds the sandbox around them
  (the gate that phase already names: "Bomb/hang cases exit `2`" is a
  different, harsher outcome than budget exhaustion and should stay a
  separate test path from these three caps).

## Owner confirmation needed

- The three numeric bounds (depth 25, units 200,000, inflated bytes
  2 GiB, OCR 500 Mpx cumulative) are sized by analogy and corpus
  headroom, not by a dedicated adversarial stress spike (decompression
  bomb, deeply-nested self-referential forms). Confirm these are
  acceptable to ship as-is, or commission that stress spike before
  Phase 3d wires the sandbox around them.
