# 0005. Pattern-class default tier

Status: accepted, needs owner confirmation before it ships

## Context

REDESIGN.md §1 states built-in pattern classes "raise false **hard**
findings on 7.5-13% of clean real-world files (dates as card numbers,
UUID digits as SSNs)" and lists this as one of the design problems
motivating the rewrite. §5's Phase 5 row names the eventual fix as
either "context rules for pattern classes **or** demote built-ins to
review," gated on "false hard < 1% on text-bearing real files" -- but
Phase 5 (matching precision, geometry-aware rules) is several phases
away from Phase 1. The Phase 1 row asks for a *default tier* decision --
what built-in pattern classes do in the meantime, from the Move (Phase
2) onward, not what Phase 5's eventual proper fix looks like.

Today's tool applies one two-tier rule to every kind of rule (value,
custom pattern, and built-in class alike): a single-line/single-literal
match is hard; anything only visible after joining lines, columns, or
pages is review. That rule does not distinguish "the operator wrote this
specific regex for their own data" from "this is one of four generic
built-in classes with no knowledge of the document's context."

## Decision

**Built-in pattern classes (`ssn`, `credit-card`, `email`, `us-phone`)
default to the review tier unconditionally** -- a class match is never a
hard finding on its own, regardless of line/literal adjacency, until
Phase 5's context rules land and can re-promote a specific,
context-confirmed match. Custom `pattern` rules (operator-authored
regexes) and `value` rules (known secrets) are **not** affected by this
ADR and keep today's adjacency-based two-tier behavior unchanged: an
operator who wrote a specific regex for their own document chose that
regex's precision themselves, which is a different trust relationship
than a generic four-class roster ships with by default.

This is the "demote built-ins to review" branch of Phase 5's own named
options, adopted early rather than left as a hard finding for however
long Phase 5 takes to reach -- see Measurement for why waiting was not
judged acceptable.

## Measurement

Ran the four built-in classes (`BUILTIN_PATTERN_CLASSES`, unmodified
regexes and validators) against every page's plain-text extraction
(`page.get_text("text")`, per line, matching how `scan_page_layer`
already evaluates the hard tier) over the same 2,031-file local corpus:

| Class | Files with >=1 validated match | File rate |
| --- | --- | --- |
| `ssn` | 110 | 5.4% |
| `credit-card` | 0 | 0.0% |
| `email` | 111 | 5.5% |
| `us-phone` | 3 | 0.1% |
| **Any of the four (union)** | **220** | **10.8%** |

None of these files are redaction targets; every SSN-shaped and
phone-shaped match is necessarily a false positive (dates, part numbers,
and other nine/ten-digit runs that happen to pass the SSA-area and NANP
validators). `credit-card`'s Luhn+date-guard validator is, empirically,
much stronger than the other three -- zero false positives here is a
point in favor of Phase 5 eventually being able to re-promote validated
classes individually rather than treating all four alike, but that
refinement is explicitly out of this ADR's scope.

The 10.8% union rate lands squarely inside REDESIGN §1's already-cited
7.5-13% range -- this measurement corroborates the plan's existing
estimate rather than revising it, but it is the first time that estimate
has been checked against a real, reproducible, non-personal corpus
(previous citation had no attached measurement).

## Consequences

- A file whose only findings are built-in-class matches moves from exit
  `1` to exit `2` under the new path. Per Principle 11 this is a
  versioned verdict change and, in the scorecard's own terms (§5), a
  **downgrade** (expected `1`, actual `2`) on every case that plants a
  secret matched only by a bare pattern class and not by a value rule.
  The case library (Phase 0b) must re-label any such case's `expected`
  tier before Phase 2 lands this, or the differential gate will
  (correctly) fail on it.
- Exit `2` still blocks a release the same way exit `1` does in every
  CI gate this tool is aware of ("cannot certify" is not "certified
  clean") -- the practical security posture is unchanged; what changes
  is no longer *overclaiming* certainty a bare digit-pattern match never
  had.
- This is explicitly an interim default. Phase 5 is expected to
  supersede this ADR (re-promoting specific, context-confirmed matches
  back to hard) rather than amend it in place.

## Owner confirmation needed

This changes real exit codes for real inputs starting at the Move
(Phase 2), not just inside the new ledger path -- unlike this pass's
other ADRs, it is a live severity change, not a forward-looking design
note. Confirm before Phase 2: is a 5-11 percentage-point false-hard rate
on clean files (measured here, consistent with §1's existing estimate)
enough to demote built-in classes to review *now*, rather than carrying
today's hard-finding behavior forward under the worst-of rule until
Phase 5 actually ships the precision work?
