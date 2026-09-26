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

| # | Title | Decision (one line) |
| --- | --- | --- |
| [0001](0001-encryption-and-decryption-cross-check.md) | Encryption and the decryption cross-check | Decrypt per object with each revision's own `/Encrypt`; treat a wrong key as detected by cross-checking our plaintext against MuPDF's wherever MuPDF can also read the object, not by validating the key up front. |
| [0002](0002-benign-parser-warning-categories.md) | Benign parser-warning categories | Four qpdf parse-level warning categories (self-corrected wrong xref offset, duplicated dictionary key, missing `endobj` before a valid next token, a specific benign inline-image note) do not count toward the parser-agreement flag; everything else does. |
| [0003](0003-not-applicable-reasons.md) | The `NOT_APPLICABLE` closed list | Ship exactly the four reasons REDESIGN §4 already names; no additional reason is justified by this pass, and the list stays closed pending a future ADR. |
| [0004](0004-image-ocr-envelope.md) | Image-OCR envelope | `DECODED` requires: a bitmap-normalisable colour space/filter, both dimensions within `[8, 10000]` px, total pixels `<= 35,000,000`, and (for a soft-masked image) a mask whose dimensions match or cleanly downscale; anything else is flagged, never skipped. |
| [0005](0005-pattern-class-default-tier.md) | Pattern-class default tier | Demote built-in pattern classes (not custom rules) to review-only by default, ahead of Phase 5 -- measured 10.8% of real corpus files trip a false hard finding under today's rule, matching REDESIGN §1's cited range. Needs owner sign-off: this changes real exit codes starting at Phase 2. |
| [0006](0006-recursion-and-decode-budget.md) | Recursion and decode budget | One shared `Budget` with depth `<= 25`, `<= 200,000` decoded units, and `<= 2 GiB` inflated bytes for the whole run; exhaustion flags rather than fails closed to `2`. |
| [0007](0007-orphaned-content-streams.md) | Orphaned content streams | Accept the cost: measured at 2.7% of real corpus files (836 streams over 55 files, heavily skewed by two outliers). Ship "always `FLAGGED`" as REDESIGN §4 already specifies; no narrower rule is needed. |
| [0008](0008-consumption-witness-granularity.md) | Consumption-witness granularity (spike S1b) | The witness only works when computed per decoding unit on its own scratch page (confirmed), and must exclude glyph-less control codes from the expected count; ship it as advisory evidence, not a hard `DECODED` gate, until a ~7% residual CJK mismatch is root-caused. |

## Template

```markdown
# NNNN. Title

Status: accepted | superseded by NNNN

## Context

What REDESIGN.md question this resolves, and why it was open.

## Decision

The rule, stated so a decoder or the verdict function can implement it
without re-deriving the reasoning.

## Measurement

What was run, on what corpus, and the numbers. Link the spike script.

## Consequences

What this changes, what it does not, and what could overturn it.

## Owner confirmation needed

Delete this section if none.
```

## Measurements

Every number cited here and in the ADRs comes from
[`eval/spikes/`](../../eval/spikes/) run against a local, non-personal
corpus of 2,031 PDFs discovered under `/System/Library`, `/Library`, and
`/Applications` on the author's machine (see
[`eval/spikes/RESULTS.md`](../../eval/spikes/RESULTS.md) for the full
run and reproduction instructions). Expect different corpora to move
these numbers, though not their order of magnitude or the direction of
each decision.
