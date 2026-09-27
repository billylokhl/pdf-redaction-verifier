# Architecture decision records

These record the Phase 1 ("Decisions") questions from
[docs/REDESIGN.md](../REDESIGN.md) §7: the seven named ADR topics
(0001-0007) plus two more (0008, 0009) found while measuring them -- the
Phase 1 row assigns spike S1b as a measurement rather than a named ADR
question, but what it found turned out to need a decision of its own
(see 0008's Context), and measuring 0008 surfaced a second, related
question (0009, interpretation warnings) that REDESIGN §4 already
commits to a rule for without having sized it. Per the plan, an ADR is
required only for the Phase 1 questions and for any later change to
compatibility or exit semantics (§7's closing note); this index is
expected to grow under that second rule, not just at Phase 1.

**Status.** All nine were written as `proposed` -- options and a
recommendation for the repository owner -- because each turned on a
judgement call (a new dependency, a severity change, a stress test not
yet run, a review-rate cost) that is the owner's to make. The owner
approved every recommendation on 2026-09-27, so all nine are now
`accepted`, and the same day decided one further question the approval
left open (how a sub-floor image is discharged, recorded in 0003 and
0004); each ADR's "Owner decision" section records exactly what was
decided.

| # | Title | Status | Decision (one line) |
| --- | --- | --- | --- |
| [0001](0001-encryption-and-decryption-cross-check.md) | Encryption and the decryption cross-check | accepted | Decrypt each revision's prefix cut with **pikepdf**, cross-checked against MuPDF's `xref_stream_raw`; anything no second decryption covers (dead bodies outside every xref) is `FLAGGED`. pikepdf is pinned when Phase 3a adds it; 3a also adds a fixture whose earlier revision has a different `/Encrypt` and confirms pikepdf's unfiltered read. |
| [0002](0002-benign-parser-warning-categories.md) | Benign parser-warning categories | accepted | Two categories are benign, each verified per instance: a wrong xref offset only when no object body exists; a duplicated key only when both values are identical. Flag rate 2.9% of all files, **7.2% of text-bearing files**. Two known check gaps (ObjStm bodies; first-token key comparison) accepted until Phase 3c closes them. |
| [0003](0003-not-applicable-reasons.md) | The `NOT_APPLICABLE` closed list | accepted | Exactly REDESIGN §4's four reasons, under one rule for every decoder: **no unit's bytes are ever exempt from the raw matcher**, at any filter-chain stage (including the encoded bytes an image codec consumes). Pattern classes run over raw image bytes at review tier. A sub-floor image is `NOT_APPLICABLE` without OCR only once all its bytes are searched and its decoded size is under the floor. The hidden-image and JPEG-comment cases are added in 0b/3a before Phase 4b. |
| [0004](0004-image-ocr-envelope.md) | Image-OCR envelope | accepted | Image OCR is `FLAGGED`-only, never `DECODED`, until Phase 4b measures recall. Today's 8×32 `_text_sized` excusal stays (235 images in 27 files, 5.6% of text-bearing): a sub-floor image is `NOT_APPLICABLE` without OCR once every filter-chain stage of its bytes is raw-searched and its actual decoded size (not just the declared one) is under the floor. The 35 Mpx cap stands (two 38.3 Mpx images exceed it), re-checked against Apple Vision in 4b. |
| [0005](0005-pattern-class-default-tier.md) | Pattern-class default tier | accepted | Built-in pattern classes **stay hard** until Phase 5 ships its fix (context rules or a demotion, Phase 5's choice). `ssn`/`us-phone` false-hard rate: **23.1% of text-bearing files** (the four-class union of 45.5% wrongly includes `email`, a true positive). |
| [0006](0006-recursion-and-decode-budget.md) | Recursion and decode budget | accepted | Placeholders: depth ≤ 25, 200,000 units (re-derived in 3a), 2 GiB inflated, OCR 200 Mpx per page plus a whole-run cap derived from page count. Exhaustion → `FLAGGED`, never exit 0. Reference-graph recursion stays open for 3a/3d. |
| [0007](0007-orphaned-content-streams.md) | Orphaned content streams | accepted | Always `FLAGGED`. Gross cost **11.4% of text-bearing files** (55/484), a lower bound; the overlap with today's exit `2` is measured in 3a. |
| [0008](0008-consumption-witness-granularity.md) | Consumption-witness granularity (spike S1b) | accepted | **Hard gate**: a witness mismatch → `FLAGGED`, unconditionally, from Phase 4a (no shadow-mode period). Corrected spike: **100.0% (2,345/2,345) of measured text pages, 0 of 399 files**; unmeasured: 856 form-drawing pages (206 files), 12 CJK-CMap pages (9 files). |
| [0009](0009-benign-interpretation-warnings.md) | Benign interpretation warnings | accepted | Replaces "any interpreter warning → not `DECODED`" (45.7% of text-bearing files) with the **guarded rule, including the annotation guard**: excused only when the unit's own witness balances non-zero, never for filter/decode or image-decoder warnings, interpreter run first on a fresh document; unrecognised warnings fail closed; re-measured on every PyMuPDF/MuPDF update. Cost **7.6% of text-bearing files** (37/484). |

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

## Owner decision (YYYY-MM-DD)

What the owner decided, and when, as the accepted ADRs record it. While
Status is `proposed`, this section lists the open questions the owner
must decide instead, and is retitled once they are decided.
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
their order of magnitude or the direction of each decision.
