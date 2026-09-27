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

**Status** is `proposed` when the ADR lays out options and a
recommendation for the repository owner to confirm, rather than settling
the question itself -- this pass found that all nine of these questions
turn on a judgement call (a new dependency, a real severity
change, a stress-test that was not run, a review-rate cost this corpus
cannot size) that is the owner's to make, not the reviewing pass's.
`accepted` is reserved for a question this pass's own measurement
settles without a material trade-off left open.

| # | Title | Status | Decision / recommendation (one line) |
| --- | --- | --- | --- |
| [0001](0001-encryption-and-decryption-cross-check.md) | Encryption and the decryption cross-check | proposed | Decrypt per object and per revision (today's tool already reaches superseded revisions of an encrypted file by reopening a prefix cut, which carries that revision's own `/Encrypt`); cross-check against MuPDF, **FLAGGED** wherever no second decryption exists (dead bodies outside every xref). Recommend pikepdf on each revision's prefix cut, cross-checked against MuPDF, over hand-rolled key derivation on a crypto library -- dead bodies stay flagged either way, so hand-rolled crypto only turns an exit `2` into a `1`. Owner picks the option and the library to pin. |
| [0002](0002-benign-parser-warning-categories.md) | Benign parser-warning categories | proposed | Only two categories survive review as benign, each verified per instance (not assumed from qpdf's wording): a wrong xref offset only when no object body exists at all; a duplicated key only when both values are identical. Combined refined flag rate: 2.9% of all files, **7.2% of text-bearing files** (was mismeasured at 0.69% before this review). |
| [0003](0003-not-applicable-reasons.md) | The `NOT_APPLICABLE` closed list | proposed (depends on 0004) | Ship exactly the four reasons REDESIGN §4 already names, under one governing rule for every decoder: no unit's bytes are ever exempt from the raw matcher. Reason 4 additionally requires 0004's recall bound, so it can't be reached until Phase 4b measures one. Today's tool flags a value hidden in a drawn image's samples only through qpdf's raw byte sweep (`REVIEW_BINARY`), and passes a small unreferenced one under its 8×32 floor. Recommend pattern classes (not just value rules) run over decoded image samples at manual-review tier, and scheduling the hidden-image case in Phase 0b/3a. |
| [0004](0004-image-ocr-envelope.md) | Image-OCR envelope | proposed | Main question: image OCR stays `FLAGGED`-only, never `DECODED`, until Phase 4b measures recall (REDESIGN §4 requires a *recall-validated* envelope). Today's 8×32 `_text_sized` rule excuses 299 images in 51 files (2.5% of all files) -- 235 in 27 (5.6% of text-bearing); recommend keeping that excusal with samples still raw-searched (0003), not flagging every one. Two real 38.3 Mpx images (one text-bearing file) exceed the 35 Mpx cap. |
| [0005](0005-pattern-class-default-tier.md) | Pattern-class default tier | proposed | Recommend built-ins **stay hard** until Phase 5 ships its own fix -- demoting now contradicts REDESIGN's "never less strict" rule and Phase 2's differential gate, and would need every affected case relabelled first. Measured **23.1%** of the *text-bearing* stratum for `ssn`/`us-phone` alone (the actual false-hard classes -- `email` is a true positive, so the four-class union of 45.5% overstates it). |
| [0006](0006-recursion-and-decode-budget.md) | Recursion and decode budget | proposed | Depth ≤ 25 (content/container boundaries only -- reference-graph depth is a separate, undecided problem), units sized against a measured corpus max of 32,971 *objects* (a rough proxy: ObjStm members uncounted, units ≠ objects), OCR pixels capped at 200 Mpx per page **plus** a whole-run cap derived from the anchored page count (not an independent flat number). Needs a stress spike. |
| [0007](0007-orphaned-content-streams.md) | Orphaned content streams | proposed | Mechanism unchanged (ship "always `FLAGGED`"). Measured gross cost: **11.4% of text-bearing files** (55/484) -- the same 55 files first reported as 2.7% of all files, now on the text-bearing denominator; a lower bound (excludes paint-only orphans, K5, and bodies outside the xref), and gross, not added: today's tool already exits `2` on undecodable orphans, overlap unmeasured. |
| [0008](0008-consumption-witness-granularity.md) | Consumption-witness granularity (spike S1b) | proposed | **Recommend a hard gate**: mismatch → `FLAGGED`, unconditionally -- advisory is fail-open with respect to REDESIGN §2/Principle 2 and only masked by worst-of shipping until Phase 6. After eight spike-side root causes, every measured text page reconciles: **100.0% (2,345/2,345), 0 of 399 files**; unmeasured: 856 form-drawing pages (206 files) and 12 CJK-CMap pages (9 files). Alternative: shadow mode first, enforced once Phase 4a reconfirms. |
| [0009](0009-benign-interpretation-warnings.md) | Benign interpretation warnings | proposed | REDESIGN §4's "any MuPDF warning while interpreting → not `DECODED`" costs **11.2% of all files, 45.7% of text-bearing files** (PyMuPDF 1.28.2; 32.2%/53.5% on 1.27.2.3 -- about 3x on all files, about 1.2x on text-bearing). Recommend excusing a warned unit only when its own witness balances, with three exit-`0` guards (never a filter/decode warning, never 0 = 0, never an image-decoder warning on a text count), interpreter run first on a fresh document: measured **34 of 221 warned text-bearing files still flagged (7.0% of 484)**. |

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
