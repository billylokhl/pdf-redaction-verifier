# 0005. Pattern-class default tier

Status: proposed

## Context

REDESIGN.md §1 states built-in pattern classes "raise false **hard**
findings on 7.5-13% of clean real-world files (dates as card numbers,
UUID digits as SSNs)" and lists this as a design problem. §5's Phase 5
row names the eventual fix as either "context rules for pattern classes
**or** demote built-ins to review," gated on "false hard < 1% on
text-bearing real files." The Phase 1 row asks for a *default tier*
decision for the time between Phase 2 (the Move) and Phase 5, not for
Phase 5's own fix to be decided or pre-empted here.

**The first version of this ADR got the scope of that decision wrong.**
It shipped "demote built-in classes to review, starting at Phase 2" as
an accepted decision. That directly contradicts:

- REDESIGN's own transition invariant: "the tool never becomes less
  strict than it is today" (§1) and the Move/Worst-of-verdict section's
  restatement of it (§6).
- Phase 2's own gate: "**Per-case differential identical**" -- demoting
  a rule's tier changes the normalised key (`(rule, tier, storage)`) for
  every case whose only hard finding is a bare pattern-class match, which
  is not "identical."
- Phase 5's own ownership of exactly this choice ("demote built-ins to
  review" is one of *Phase 5's* two named options, not Phase 1's to
  spend early).

This version reverses that: it treats the tier question as the owner's
call, proposes options, and recommends the one that does not violate the
transition invariant.

## Decision

**Recommended default: built-in pattern classes (`ssn`, `credit-card`,
`email`, `us-phone`) stay at today's hard-finding tier (single-line/
single-literal match = hard) through the Move and until Phase 5 actually
ships its own fix.** This keeps Phase 2's differential gate meaningful
(no case's expected tier needs to change just to move code around) and
does not pre-empt Phase 5's own decision between its two named options.

**Cases this would affect if demoted instead** (so the owner can weigh
the size of the change): `eval/caselib/families/live.py`'s
`page.visible-pattern-rule` case (its only expected finding is
`("Any SSN", "live")`); every `page_text_labels.py` grid entry whose
expected findings list is exactly `(("Any SSN", "live"),)` (the
`pattern-only` layout variants: `single-line`, `rotated`, `form-boxes`,
`overlapping-values`, `overlapping-split-short-tail` at minimum); and
`families/layout.py`'s SSN-class cases. All of these currently expect
exit `1`; demoting the class would need every one relabelled to expect
`2` (or a mixed hard/review finding set) *before* Phase 2's differential
gate could pass -- this is Phase 2 work, not a Phase 1 ADR's to do.

**Options for the owner:**

1. **Keep hard until Phase 5 (recommended).** No case relabelling, no
   transition-invariant conflict, no early severity change. Phase 5
   resolves the false-hard rate with context rules, a demotion, or both,
   on its own gate ("false hard < 1% on text-bearing real files").
2. **Demote only `ssn` and `us-phone`.** `credit-card`'s Luhn+date-guard
   validator measured **zero** false positives on this corpus (see
   Measurement) -- demoting it has no supporting evidence and would
   weaken a rule that appears to already work. `email` is a different
   failure mode entirely: a validated match is a **true positive for
   "there is an email address here"** -- it is not a pattern
   miscoloring unrelated digits, the way an SSN-shaped date is. Whether
   an incidental email address is *sensitive* is a judgment call for the
   rules file's author, not evidence the `email` class itself is
   imprecise. If the owner wants a narrower demotion than option 1,
   `ssn` and `us-phone` are the two classes this pass's data actually
   supports demoting.
3. **Demote all four now, accept the Phase 2 relabelling cost.** Not
   recommended: it spends Phase 5's decision early, on data that Phase
   5's own gate (text-bearing false-hard rate) is the designed place to
   evaluate it against.

## Measurement

Ran the four built-in classes against every page's plain-text extraction
over the 2,031-file corpus (`eval/spikes/pattern_class_false_hard.py`),
now also stratified by the text-bearing subset (484 files) Phase 5's own
gate is defined on:

| Class | All files (2,031) | Text-bearing (484) |
| --- | --- | --- |
| `ssn` | 5.4% | 22.7% |
| `credit-card` | 0.0% | 0.0% |
| `email` | 5.5% | 22.9% |
| `us-phone` | 0.1% | 0.6% |
| **Any of the four (union)** | **10.8%** | **45.5%** |

**The number that matters for this decision is 45.5%, not 10.8%.** A
file with no extractable text cannot produce a pattern-class false hard
via this path at all, so diluting the rate across the ~76% text-free
majority of a system/app corpus understates the real exposure on exactly
the population Phase 5's gate targets. 45.5% is far above the plan's
originally cited 7.5-13% -- worth flagging to the owner as a possible
sign that Phase 5's work is more urgent, or more involved, than the
plan's original estimate assumed.

This measurement's own scope is a lower bound, stated plainly: it scans
page **text** only, not Metadata (XMP/Info) or Objects (string literals,
decoded streams), both of which today's tool also runs pattern classes
over. The real false-hard rate (all layers) is at least this large, not
capped by it.

## Consequences

- No verdict changes as a result of this ADR: the recommended default
  (option 1) is "change nothing yet."
- The case-list above is the concrete input Phase 2's implementers need
  if the owner instead picks option 2 or 3 -- relabel those cases'
  expected findings/tiers before the Move, or Phase 2's differential
  gate will (correctly) fail on them.
- This ADR does not claim any effect on downstream CI gates beyond
  Phase 2's differential -- the first version's discussion of scorecard
  behavior under demotion is removed as unsupported speculation.

## Owner confirmation needed

Which of the three options to ship: keep hard until Phase 5
(recommended), demote only `ssn`/`us-phone` now, or demote all four now
and accept the Phase 2 case-relabelling cost.
