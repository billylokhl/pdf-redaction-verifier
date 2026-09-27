# 0007. Orphaned content streams

Status: accepted (owner approval, 2026-09-27)

## Context

REDESIGN.md §4 says a content stream no revision references "has no
context: its strings are searched as they are (as today) and it is also
decoded under a fallback (the union of document fonts), so a match is
still found -- but its status stays `FLAGGED`," explicitly calling this
"stricter than today," and: "Phase 1 measures how often it fires; an ADR
accepts the cost or defines a narrower sound rule."

## Decision

**Ship "always `FLAGGED`" exactly as REDESIGN §4 specifies -- no narrower
rule.** A narrower rule (e.g. only flag an orphan that also contains a
validated pattern or value match) was considered and rejected: it would
reintroduce the "guessing a kind never reduces coverage" problem
Principle 3 exists to prevent, since an orphan that looks harmless from
its own bytes is exactly what a redaction tool's leftover looks like
(§4's own motivating example; K2 in §8).

This mechanism (always flag, no narrowing) is unchanged from the first
version of this ADR. **What changes here is the measured cost, and an
honest statement of what this measurement does and does not cover** --
the first version's framing ("2.7%... a ceiling... heavily skewed by two
outliers") understated both the rate and the measurement's own scope.

## Measurement, corrected

2,031 real files, 484 text-bearing (`eval/spikes/measure_corpus.py`,
`inv.orphaned_content_streams`, reusing `verify._reachable_from_sources`
and `verify._is_content_stream` verbatim):

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 orphaned content-like stream | 55 / 2,031 = 2.7% | **55 / 484 = 11.4%** |
| Total orphaned content-like streams | 836 | 836 |
| Median count among affected files | 1 | 1 |
| Files with exactly 1 | 28 | 28 |
| Top counts | 356, 356, 25, 13, 5 | (same -- all affected files are text-bearing) |

**11.4%, not 2.7%, is the number that describes this rule's real cost.**
All 55 affected files happen to be text-bearing (55 of 55). That is an
observation about this corpus, not something the measurement guarantees:
an orphan is an unreferenced stream, while "text-bearing" is judged on
the live pages' extracted text, so a file whose only text sits in an
orphan would be counted here without being text-bearing. Since every
affected file here is text-bearing, the text-bearing stratum is the
population the rule's cost falls on, and the 2.7% all-files rate
dilutes it across the corpus's ~76% text-free majority.

**This is the gross cost, not the added cost.** Today's tool already
scans orphaned streams (its Objects layer, `orphaned` and `unreferenced`
storage) and exits `2` for one it cannot decode, so some of these 55
files may already exit `2` today and would not be newly flagged by this
rule. That overlap was not measured in this pass; the added review-rate
cost is somewhere between 0 and 11.4% of text-bearing files.

**This measurement is a lower bound, not a "ceiling," on two counts the
first version did not state:**

1. It only counts an object as an orphan when it **shows text**
   (`verify._is_content_stream`: >=1 `BT...ET` block with a shown
   string). A **paint-only** orphan -- one that fills, strokes, or draws
   an image but shows no text (the K5 shape: text converted to outlines,
   in an orphaned stream) -- is invisible to this count entirely.
2. It only counts objects with a **current xref entry** at all. A "dead
   body" -- bytes that read as `N G obj ... endobj` but that the current
   xref table does not index -- is a different phenomenon this function
   cannot see (`eval/spikes/inventory_lite.py`'s byte tiler is the tool
   that would find those, as unindexed bytes that happen to parse as an
   object; see `eval/spikes/RESULTS.md`'s unindexed-byte section, whose
   three observed shapes include none matching this exact one in this
   corpus, though it did not specifically search for it either).

The "two outliers" framing (2 files at 356 each) is kept as a real,
useful observation about *distribution* (median 1, most affected files
have very few), but it does not make the corrected 11.4% rate a
"ceiling" -- it describes the shape of what this narrower measurement
found, not a bound on what the broader phenomenon (including 1 and 2
above) would show.

## Consequences

- 11.4% (text-bearing) is the same 55 files the first version reported
  as 2.7%, stated on the population the cost falls on; per Phase 4a's
  own gate ("review-rate change within the rates measured in ADR 0007
  (11.4%) and ADR 0009 (7.6%); a larger rate goes back to the owner")
  this is now the number Phase 4a's actual review-rate change gets
  checked against -- confirm this before Phase
  4a ships, not after.
- None of the 2,031 corpus files have any redaction history, so none of
  the 836 streams are an actual leak -- they read as ordinary producer
  noise. That observation is unchanged from the first version and still
  supports "accept the cost": a real post-redaction corpus (Phase 0b's
  case library, the real-redactor tier) is the population this rule is
  actually aimed at, and the plan's own `leftover.redacted-no-gc` story
  is exactly the true positive this measurement's corpus cannot contain.
- The K5 (paint-only orphan) and dead-body gaps above are candidates for
  a follow-up measurement in Phase 3a, once the byte tiler and a
  paint-detecting sniff both exist for real -- not blocking this
  decision, but the owner should know the true rate could be higher
  still.

## Owner decision (2026-09-27)

Owner decision: "approve all recommendations."

- **Accept "always `FLAGGED`" at the measured cost**: an 11.4%
  (text-bearing, 55/484) review-rate contribution -- the gross cost,
  known to be a lower bound (paint-only orphans and dead bodies are not
  counted), of which an unmeasured share already exits `2` today. The
  first version of this ADR reported the same 55 files as 2.7% of all
  2,031 files; the number did not grow, the denominator was corrected to
  the text-bearing stratum.
- **Measure the overlap with today's exit-`2` orphan handling in Phase
  3a**, to establish the added (not gross) review-rate cost.
