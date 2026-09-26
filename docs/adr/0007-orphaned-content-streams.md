# 0007. Orphaned content streams

Status: accepted

## Context

REDESIGN.md §4 says a content stream no revision references "has no
context: its strings are searched as they are (as today) and it is also
decoded under a fallback (the union of document fonts), so a match is
still found -- but its status stays `FLAGGED`," explicitly calling this
"stricter than today" and noting: "Phase 1 measures how often it fires;
an ADR accepts the cost or defines a narrower sound rule." This is that
measurement and that ADR.

The stakes are asymmetric with docs/adr/0005's: unlike a false-hard
pattern match, an orphaned stream staying `FLAGGED` forever (never
`DECODED`) does not claim a leak was found -- it only prevents exit `0`.
The question is purely affordability: how often does this rule alone
stand between a clean file and certification.

## Decision

**Accept the cost. Ship "a content-stream-shaped unit no revision
references is always `FLAGGED`" exactly as REDESIGN §4 already
specifies -- no narrower rule.** A narrower rule was considered (e.g.
only flag an orphaned stream that also contains a validated pattern or
value match, rather than every content-sniffing orphan) and rejected:
it would reintroduce exactly the "guessing a kind never reduces
coverage" problem Principle 3 exists to prevent -- a heuristic that
looks harmless from its own bytes is precisely what an orphaned
leftover from a redaction tool that skipped garbage collection looks
like (§4's own motivating example; K2 in §8).

The measured rate (below) is small enough, and concentrated enough in a
handful of files, that no narrower rule is needed to keep this
affordable.

## Measurement

2,031 real files, current-revision reachability only (`eval/spikes/
inventory_lite.py`'s `orphaned_content_streams`, reusing
`verify._reachable_from_sources` and `verify._is_content_stream`
verbatim -- see that module's docstring for the current-revision-only
scoping limitation):

| | |
| --- | --- |
| Files with >= 1 orphaned content-like stream | 55 / 2,031 = **2.7%** |
| Total orphaned content-like streams found | 836 |
| Distribution | 2 files account for 356 each (712 of 836); median affected file has 1; 28 of 55 have exactly 1 |

None of these files have any redaction history (they are macOS
system/app resources), so none of the 836 streams are an actual leak --
they read as ordinary producer noise (unused alternate-layout Form
XObjects, print-driver artifacts). That is itself informative: on a
corpus with zero true positives for "orphaned leftover from a redaction
tool," the rule still only touches 2.7% of files, and even that 2.7% is
dominated by two outliers rather than spread evenly -- a sign this is
not a pervasive, unavoidable producer pattern that would make "always
`FLAGGED`" punishing in practice.

## Consequences

- 2.7% is a **ceiling** on this rule's contribution to the review rate
  on a non-redaction corpus; a real post-redaction corpus (Phase 0b's
  case library, the real-redactor tier) is the population this rule is
  actually aimed at and may show a different rate -- the plan's own
  `leftover.redacted-no-gc` story is exactly the true positive this
  measurement's corpus cannot contain.
- Because the rule never promotes to `DECODED`, it can only add to the
  *shadow* verdict until Phase 4a enables the content-stream unit kind,
  and then to the *enforced* verdict from Phase 4a on -- consistent with
  §6's phased-enforcement design; this ADR does not need its own
  enforcement gate beyond what Phase 4a already has.
- The current-revision-only reachability scoping (this measurement's
  known limitation, shared with today's `ORPHANED` label) means the true
  rate under REDESIGN's full "no revision references" wording -- walking
  every earlier revision's own trailer graph too -- could be modestly
  higher. Multi-revision files are a minority of most real corpora, and
  this pass did not have a multi-revision-heavy sample to size that gap;
  flagged as a follow-up measurement for Phase 3a rather than blocking
  this decision.

## Owner confirmation needed

None -- the measured rate is small and concentrated enough that
"accept the cost" is not a close call for this corpus. The follow-up
measurement noted above (rate on a real, multi-revision, post-redaction
corpus) is worth doing in Phase 0b/3a but does not change this ADR's
decision by itself.
