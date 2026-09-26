# 0002. Benign parser-warning categories

Status: accepted

## Context

REDESIGN.md §4 defines parser agreement as: compare the inventory's
in-use object set and page tree against PyMuPDF and against qpdf (a
separate process) for the current revision, where "repair" means
parse-level warnings (`qpdf <file> --object-streams=disable /dev/null`
plus MuPDF warnings) -- explicitly **not** `qpdf --check`'s
linearization lint, which the plan says fires on ~12% of real Acrobat
files for reasons unrelated to structural soundness. The plan cites a
measured flag rate of "~1.5%" for this definition but defers "benign
warning categories" to an ADR. This is that ADR.

## Decision

A qpdf parse-level warning does **not** count toward the parser-agreement
flag when the whole of its stderr output, after dropping qpdf's own
one-line summary ("operation succeeded with warnings..."), matches one
of these categories:

1. **Self-corrected wrong xref offset** -- qpdf's own message: "object
   has offset N - a common error handled correctly by qpdf and most
   other applications." The object was still found unambiguously by
   scanning for `N G obj`; nothing was lost.
2. **Duplicated dictionary key** -- "dictionary has duplicated key
   /Filter; last occurrence overrides earlier ones." The PDF spec's own
   resolution rule (last wins) is deterministic; qpdf and MuPDF apply
   it the same way.
3. **Missing/misplaced `endobj`** -- "expected endobj," where the object
   was still terminated unambiguously (the next token is the next
   object header or a xref section). See the caveat below: this is the
   one category with a real, if rare, failure mode.
4. **The single-object "still valid" note** -- "input stream is complete
   but output may still be valid," seen only on small single-object
   fragment PDFs in this corpus (see Measurement).

Everything else -- a numeric literal overflowing qpdf's parser and being
replaced with null (data loss), "file is damaged," an actual
cross-reference reconstruction, a bad indirect reference resolved as
null, or any warning text not matching one of the four patterns above --
**does** count as a repair-level disagreement and flags. `verify.py`
today has no such list; the new inventory's benign-set check is a small,
named, testable predicate (`_BENIGN_QPDF_PATTERNS` in
`eval/spikes/measure_corpus.py` is the Phase 1 proof; Phase 3c ports it,
with a regression test pinning each pattern against a hand-built
fixture).

**Caveat on category 3.** "Missing endobj, but the next token is
unambiguous" is exactly the shape of ambiguity REDESIGN §4's own
byte-tiler is built to detect (an omitted `endobj` could in principle
hide extra bytes between the true end of a stream and the next object).
Calling it benign here is a default, not a proof: it holds only because,
in the corpus, the next token in every such case was structurally
unambiguous. Phase 3a's real tiler should keep re-deriving this
per-object rather than trusting the category name, and downgrade a
specific instance out of the benign set the moment the following bytes
are *not* unambiguous.

## Measurement

2,031 real files (`eval/spikes/RESULTS.md`, full table there). Headline
numbers:

| | Flag rate |
| --- | --- |
| Raw qpdf (any warning) | 9.0% |
| Raw qpdf, benign categories excluded | 0.39% |
| MuPDF (`is_repaired` or any warning) | 0.34% |
| **Combined (either tool), benign excluded** | **0.69%** |

The raw rate (9.0%) is six times the plan's ~1.5% estimate; almost all
of the gap is category 1 (146 of 182 raw-flagged files) and category 2
(21 files) -- both self-correcting, spec-defined resolutions that every
mainstream PDF reader already applies identically. With those (and
categories 3-4) excluded, the combined rate lands at 0.69%, in the same
order of magnitude as the plan's original estimate and comfortably
affordable as a review-rate contributor.

## Consequences

- Without this ADR, shipping "any qpdf warning = repair" would have
  flagged 9% of real files for exit `2` -- an order of magnitude above
  what §5's scorecard gates ("false hard < 1%... review rate" targets)
  would tolerate. This ADR is what makes parser agreement affordable at
  all.
- The benign set is a **closed, named list** (Principle 9: every
  threshold named with its reason), not "ignore anything that looks
  routine" -- a new qpdf version emitting new wording for the same
  underlying condition needs a deliberate addition here, with its own
  corpus evidence, not a silent behavior change.
- Category 3's caveat means Phase 3a must implement the benign check as
  a per-object structural confirmation, not a stderr-text pattern match
  reused verbatim from this spike -- the spike's regex-on-stderr version
  is good enough to size the decision, not to ship.

## Owner confirmation needed

None -- the measured gap between raw and refined rates is large enough
that the direction of this decision is not close.
