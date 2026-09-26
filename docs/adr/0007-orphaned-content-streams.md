# 0007. Orphaned content streams

Status: proposed

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

**11.4%, not 2.7%, is the number that describes this rule's real cost**:
every affected file happens to be text-bearing (an orphan that sniffs as
content requires a shown text object, so this is expected), so diluting
the rate across the corpus's ~76% text-free majority understated the
cost on the population where it actually bites by roughly 4x.

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
   object; see docs/adr/0003's unindexed-byte measurement, which found
   none matching this exact shape in this corpus, but did not
   specifically search for it either).

The "two outliers" framing (2 files at 356 each) is kept as a real,
useful observation about *distribution* (median 1, most affected files
have very few), but it does not make the corrected 11.4% rate a
"ceiling" -- it describes the shape of what this narrower measurement
found, not a bound on what the broader phenomenon (including 1 and 2
above) would show.

## Consequences

- 11.4% (text-bearing) is a real, larger cost than the first version
  reported, and per Phase 4a's own gate ("review-rate change within what
  the orphan ADR accepted") this is now the number Phase 4a's actual
  review-rate change gets checked against -- confirm this before Phase
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

## Owner confirmation needed

Whether an 11.4% (text-bearing) review-rate contribution -- known to be
a lower bound -- is still affordable to accept without a narrower rule,
now that it is roughly 4x the number the first version of this ADR
reported.
