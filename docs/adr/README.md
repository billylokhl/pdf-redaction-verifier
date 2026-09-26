# Architecture decision records

These record the Phase 1 ("Decisions") questions from
[docs/REDESIGN.md](../REDESIGN.md) §7: the seven named ADR topics
(0001-0007) plus one more (0008), for spike S1b's own findings -- the
Phase 1 row assigns S1b as a measurement rather than a named ADR
question, but what it found turned out to need a decision of its own
(see 0008's Context). Per the plan, an ADR is required only for the
Phase 1 questions and for any later change to compatibility or exit
semantics (§7's closing note); this index is expected to grow under that
second rule, not just at Phase 1.

**Status** is `proposed` when the ADR lays out options and a
recommendation for the repository owner to confirm, rather than settling
the question itself -- this pass found that most of these seven
questions turn on a judgement call (a new dependency, a real severity
change, a stress-test that was not run, a review-rate cost this corpus
cannot size) that is the owner's to make, not the reviewing pass's.
`accepted` is reserved for a question this pass's own measurement
settles without a material trade-off left open.

| # | Title | Status | Decision / recommendation (one line) |
| --- | --- | --- | --- |
| [0001](0001-encryption-and-decryption-cross-check.md) | Encryption and the decryption cross-check | proposed | Decrypt per object; cross-check against MuPDF/pikepdf wherever a key path is independently readable, **FLAGGED** wherever it is not (superseded revisions with their own `/Encrypt`, dead bodies outside the xref); recommend hand-rolled key derivation on a crypto-primitives library, since pikepdf only exposes the current revision. |
| [0002](0002-benign-parser-warning-categories.md) | Benign parser-warning categories | proposed | Only two categories survive review as benign, each verified per instance (not assumed from qpdf's wording): a wrong xref offset only when no object body exists at all; a duplicated key only when both values are identical. Refined flag rate: 2.86% of all files, **7.2% of text-bearing files** (was mismeasured at 0.69% before this review). |
| [0003](0003-not-applicable-reasons.md) | The `NOT_APPLICABLE` closed list | accepted | Ship exactly the four reasons REDESIGN §4 already names; explicitly, `NOT_APPLICABLE` never exempts a unit's decoded bytes from the raw matcher -- it excuses a decoder from re-parsing them, not the search. |
| [0004](0004-image-ocr-envelope.md) | Image-OCR envelope | proposed | Geometry and a completed OCR pass are not enough for `DECODED` -- REDESIGN §4 requires a *recall-validated* envelope, which Phase 4b has not measured yet; until then image-OCR evidence is `FLAGGED`, never `DECODED`. Bounds and known misses (isoluminant colours, post-downsample small glyphs) listed for the owner. |
| [0005](0005-pattern-class-default-tier.md) | Pattern-class default tier | proposed | Recommend built-ins **stay hard** until Phase 5 ships its own fix -- demoting now contradicts REDESIGN's "never less strict" rule and Phase 2's differential gate, and would need every affected case relabelled first. Measured 45% (not 10.8%) of the *text-bearing* stratum Phase 5 actually gates on. |
| [0006](0006-recursion-and-decode-budget.md) | Recursion and decode budget | proposed | Depth ≤ 25 (content/container boundaries only -- reference-graph depth is a separate, undecided problem), units sized against a measured corpus max of 32,971 objects, OCR pixels capped **per page**, not cumulatively (a cumulative cap conflicts with §5's required ~300-page scan fixture). Needs a stress spike. |
| [0007](0007-orphaned-content-streams.md) | Orphaned content streams | proposed | Mechanism unchanged (ship "always `FLAGGED`"), but the measured cost is **11.4% of text-bearing files** (55/484), not 2.7% of all files as first reported, and is a lower bound (excludes paint-only orphans, K5, and bodies outside the xref entirely) -- the owner should reconfirm "accept the cost" against the corrected number. |
| [0008](0008-consumption-witness-granularity.md) | Consumption-witness granularity (spike S1b) | proposed | Text render mode (`Tr` 2/6/7, not 3) must be handled unconditionally in Phase 4a -- two verified, warning-free cases (clip-only text, fill+stroke double-count) show `get_texttrace()` alone would let a redactor's mode-hidden text reach `DECODED`. The broader byte-count witness's real reconciliation rate, after fixing a font-tracking bug in the spike itself, is **71.2%** on real content (not the 92.7% first reported); recommend shipping it advisory pending root-cause of that residual, with the Tr-mode fix carrying the hard-gate safety property on its own. |

## Template

```markdown
# NNNN. Title

Status: proposed | accepted | superseded by NNNN

## Context

What REDESIGN.md question this resolves, and why it was open.

## Decision

The rule (or, if `proposed`, the options and a recommendation) stated so
a decoder or the verdict function -- or the owner, deciding -- can act on
it without re-deriving the reasoning.

## Measurement

What was run, on what corpus, and the numbers. Link the spike script.

## Consequences

What this changes, what it does not, and what could overturn it.

## Owner confirmation needed

Delete this section if none. Required whenever Status is `proposed`.
```

## Measurements

Every number cited here and in the ADRs comes from
[`eval/spikes/`](../../eval/spikes/) run against a local, non-personal
corpus of 2,031 PDFs (484 of them text-bearing) discovered under
`/System/Library`, `/Library`, and `/Applications` on the author's
machine (see [`eval/spikes/RESULTS.md`](../../eval/spikes/RESULTS.md)
for the full run, both denominators, and reproduction instructions).
The corpus is a live directory listing and file counts drift by a few
files between runs (see `eval/spikes/README.md`'s "Known limitations");
expect a different corpus to move these numbers further, though not
their order of magnitude or the direction of each recommendation.
