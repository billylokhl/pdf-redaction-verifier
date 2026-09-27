# 0006. Recursion and decode budget

Status: accepted (owner approval, 2026-09-27)

## Context

REDESIGN.md §4 says the decoding worklist needs "**Budget**: one object
for the whole run including recursion -- depth, bytes inflated, units,
OCR pixels. Exhaustion -> flagged," and Principle 4 requires hostile-input
handling to be resource-bounded and work-based. Neither the plan nor
`verify.py` fixes the actual numbers. `verify.py` today has one
comparable cap, `MAX_ATTACHMENT_BYTES = 16 << 20` (16 MiB, per
attachment, not cumulative) and `MAX_EARLIER_REVISIONS = 50`.

**Corrections to the first version of this ADR.** It (a) cited an
object-count example ("largest 258... all files under 1,300") that was
never true of the full corpus -- 258 came from an ad hoc 40-file sample
checked interactively and never re-measured against all 2,031 files;
(b) described exhaustion as something that "does not fail closed to exit
`2` for the whole run," which is confusingly stated -- there is no
special exit-`2`-override behavior here at all, budget exhaustion is
just an ordinary `FLAGGED` status like any other, which (per Principle 1)
already prevents that file from exiting `0`; (c) set a single cumulative
500 Mpx OCR-pixel cap without checking it against §5's own corpus
requirement of "a ~300-page scan," which a cumulative cap this size
cannot accommodate; (d) did not address recursion through the
**reference graph** (page-tree `/Kids` chains, `/Parent` back-references)
separately from recursion through **decoding** (nested forms, Type3,
containers) -- these are different code paths with different failure
modes.

## Decision

**Exhaustion, restated plainly:** when any bound below is hit, the unit
being processed (and anything still queued behind it) becomes `FLAGGED`.
Because an undischarged/`FLAGGED` obligation already blocks exit `0`
under the ledger model (Principle 1), that file's exit is `>= 2` (or
stays `1` if a hard finding was already confirmed elsewhere in the same
file) -- there is no separate "budget exhaustion" exit-code rule to
specify; it falls out of the existing verdict function.

**Depth <= 25** for decoding recursion only (a form `Do`, a Type3 glyph
invocation, a recursive-PDF unwrap, a zip/Office entry) -- **this bound
does not cover reference-graph traversal** (walking `/Kids` to enumerate
pages, resolving inherited resources up a `/Parent` chain). That is a
separate, unresolved problem: a naive recursive graph walk can hit
Python's own `RecursionError` on a deeply nested or cyclic graph (a
`/Parent` loop, in particular, does not terminate at all without cycle
detection) before this depth counter is ever consulted. **Deferred to
Phase 3a (the reference-graph implementation must be iterative or
explicitly cycle-checked, not naive recursion) and Phase 3d (the sandbox
must still catch a `RecursionError` that escapes and treat it the same
as any other child failure -- unreported obligations `FAILED`)** -- not
resolved by this ADR.

**Units <= 200,000 decoded per run**, sized against this pass's corpus
(corrected numbers -- `eval/spikes/measure_corpus.py`'s
`object_count_distribution`, from `inventory_lite.tile()` over all 2,031
files): median 13 objects, 95th percentile 100, **maximum 32,971**
(two files near 2,063 objects, then a long tail down to the median).
**This number is a rough proxy, not a units count, in two ways review
found:** (a) `inventory_lite.tile()` counts top-level `N G obj` headers
found by scanning raw bytes -- an object compressed inside an `/ObjStm`
(PDF 1.5+ object streams) has no such header at all and is not counted,
so this undercounts real object totals for any file using them (the same
blind spot as docs/adr/0002's ObjStm gap); (b) REDESIGN's "unit" is not
"object" -- a unit is created for every object-stream member, every
content-stream child a decoder uncovers, every unindexed byte range, so
the true units-per-run count for a given file is at least its object
count, likely well above it. 200,000 leaves headroom over the largest
*object* count this pass saw (not the "two orders of magnitude over 258"
the first version claimed from an unrepresentative sample), but that
headroom is against the wrong quantity. **Decided: treat 200,000 as a
placeholder Phase 3a must re-derive** once it can enumerate real units
(including ObjStm members and decoder-discovered children), not a number
to carry forward as measured.

**Inflated bytes <= 2 GiB cumulative**, matching the general order of
magnitude of the existing per-attachment cap (16 MiB) scaled up for a
whole-run budget. Not independently re-measured against a real large
file in this pass.

**OCR pixels: a per-page cap of 200 Mpx, plus a whole-run cap DERIVED
from it (200 Mpx x the anchored page count), not an independent flat
number.** The first version's flat 500 Mpx cumulative cap fails REDESIGN
§5's own corpus requirement outright: a representative "~300-page scan"
at 300 DPI on Letter-size pages is about 8.4 Mpx/page (2550x3300), so a
legitimate 300-page scanned document needs roughly 2,520 Mpx of stored-
image OCR if most pages are a single full-page scanned image -- five
times the flat cap.

**A per-page cap alone is not enough either -- review correctly pointed
out it contradicts §4's "one Budget for the whole run" framing and
Principle 6 ("Limits are work-based... "; a per-page-only cap has no
whole-run ceiling at all, which is exactly the kind of unbounded-total
resource use Principle 4's hostile-input handling exists to prevent.**
The corrected design keeps both: 200 Mpx per page (comfortably above one
full-page scan at 300 DPI), and a whole-run total capped at 200 Mpx
multiplied by the page count the parent already anchors independently
(qpdf's reported page count, per §4's own anchoring design) -- not a
second, independently-tunable flat number, which is exactly what would
recreate the ~300-page-scan conflict. A file that claims many pages to
inflate this budget is already bounded by the *other* budgets here (byte
count, unit count) and by the page-count anchor itself. Plus
docs/adr/0004's own per-image 35 Mpx cap. **Page-view OCR (rendering a
page and OCRing it -- the existing view obligation, unchanged since
today's tool) is explicitly out of scope for this budget** -- it is a
different obligation kind (§4: "one per view... each page's OCR") with
its own existing, uncapped-by-this-ADR behavior; this budget governs
only Phase 4b's new per-stored-image decoding.

## Measurement

Spike S1b (docs/adr/0008, `eval/spikes/RESULTS.md`'s S1b section) is the only Phase
1 evidence bearing on per-unit cost, and only indirectly: every case it
checked ran in a single pass over one content stream plus one
`get_texttrace()` call, cheap enough that depth and unit-count, not
per-unit CPU time, look like the binding constraints in practice. This
pass did not build a decompression-bomb or deeply-nested-forms fixture
to find where these bounds actually bite, nor did it re-verify the 2 GiB
byte cap or exercise reference-graph recursion at all.

## Consequences

- One shared `Budget` for decoding recursion (forms, Type3, containers);
  reference-graph recursion is explicitly a separate, still-open problem
  for Phase 3a/3d, not silently assumed to be covered by the same
  counter.
- The per-page OCR cap, plus a whole-run cap derived from it (not
  independent), means a document's total stored-image OCR bill scales
  with its page count the way page-view OCR already does today, rather
  than hitting an arbitrary whole-file ceiling that penalizes long
  legitimate documents more than short suspicious ones, while still
  giving REDESIGN's "one Budget for the whole run" framing an actual
  whole-run number to point to.
- These are named, single-purpose numbers per Principle 9 and each needs
  its own pinning test once Phase 3d builds the sandbox around them; the
  Phase 3d gate ("Bomb/hang cases exit `2`") is a different, harsher
  outcome than ordinary budget exhaustion and should stay a separate
  test path.

## Owner decision (2026-09-27)

Owner decision: "approve all recommendations."

- **Accept the four numeric bounds as placeholders** (depth 25, units
  200,000, inflated bytes 2 GiB, OCR 200 Mpx/page) -- sized by analogy
  and corpus headroom, not a dedicated adversarial stress spike, and
  acceptable to ship as-is on that basis. No stress spike is commissioned
  now.
- **Re-derive units in Phase 3a** once it can enumerate real units
  (including ObjStm members and decoder-discovered children) -- 200,000
  is a placeholder, not a number to carry forward as measured.
- **Reference-graph recursion stays open for Phase 3a/3d** (cycle
  detection, `RecursionError` handling) rather than being partially
  addressed here.
