# 0012. Pattern-class precision: demotion, context rules, or both

Status: proposed (draft, 2026-10-15; options and a recommendation for the owner)

## Context

REDESIGN.md §1 names a matching problem: the built-in pattern classes
(`ssn`, `credit-card`, `email`, `us-phone`) have no context and raise
false **hard** findings on clean real files. Phase 5 (§7) must fix it,
choosing "context rules for pattern classes, or a demotion to review", and
its gate is "built-in pattern false hard < 1% on text-bearing real files".
[ADR 0005](0005-pattern-class-default-tier.md) kept the classes hard until
then and left that choice to Phase 5.

Owner decisions already taken (2026-10-15) bound the options:

- **Demote only.** A rule may turn a built-in class match from a hard
  finding into a manual-review warning (exit 1 → 2). It may never suppress
  a match (1 → 0): a demoted match still prevents certification.
- **Measure first.** Phase 5 PR 1 measured the baseline (below); the
  choice is made here, by the owner, after Lane B's first real-redactor
  numbers (U2 silent misses, U3 good-redaction exit-0 share).
- **Recall first.** Before any demotion ships, Phase 5 PR 2 measures what
  each option costs on leak cases (coordinator decision, 2026-10-14).

## Measurement

Phase 5 PR 1 (`eval/scorecard/pattern_baseline.py`; eval/spikes/RESULTS.md,
"Phase 5 baseline"), full CLI, every layer, rules = the four classes only,
2,073 local corpus files, commit 90669ef. Text-bearing stratum, 488
measured:

| Class | Hard files | Mostly |
| --- | --- | --- |
| `ssn` | 132 (27.0%) | runs inside hex ids (69 files) or alphanumeric ids (44), UUIDs (11); 3 files dashed like a real SSN |
| `us-phone` | 14 (2.9%) | bare 10-digit runs (12); 2 files punctuated like a real number |
| `credit-card` | 7 (1.4%) | number lists, bare runs; none grouped like a real card |
| `email` | 112 (23.0%) | real addresses: a true positive for "an address is here" (ADR 0005) |
| `ssn` or `us-phone` | **138 (28.3%)** | |

What each demotion would leave hard (`ssn` or `us-phone`, text-bearing; an
estimate from every match of the class, erring toward "still hard"):

| Demoted to review | Still hard |
| --- | --- |
| nothing (today) | 138 (28.3%) |
| a match inside a token (UUID, hex id, alphanumeric id) or a dated run | 25 (5.1%) |
| ... and a separated list of small numbers | 25 (5.1%) |
| ... and a bare digit run | 5 (1.0%) |
| the whole class | 0 |

The 5 left look exactly like the real thing: 3 files with a dashed
SSN-shaped value (Objects layer) and 2 with a punctuated phone number. No
option short of demoting real-looking values meets "< 1%": 5/488 is 1.02%.

**Recall** (Phase 5 PR 2a, `eval/scorecard/pattern_recall.py`): every
case-library leak case with a built-in class rule (109, of which 52 exit
1 today), scanned with the recorder, each option applied to what it saw.
A demotion never certifies a file (exit 2, not 0), but it moves a
confirmed leak to "please review":

| Option | Leak cases moved from exit 1 to exit 2 |
| --- | --- |
| A | 0 |
| B | 1: `layout.form-boxes-pattern-only`, an SSN typed one digit per form box, which the Text layer reads as a bare `123456789` |
| C | 9: every pattern-only leak (`page.visible-pattern-rule`, the five `pattern-only` layout rows, `document.trailer-key-pattern-rule`, the two sideways scans) |

The lower and upper bounds agree for every option. The case library does
not yet hold an SSN written against a label with the space lost
("SSN123456789"), which A would also demote; it should, before PR 3.

## Options

Per class. "Shape" is the classification PR 1 measured
(`eval/scorecard/match_shapes.py`); PR 2 would move the rule into
`redaction_verifier/matching/` as pure code.

**A. Context rule, narrow.** Demote a match that sits inside a token
(touching letters, part of a UUID or hex id) or inside a dated run (a PDF
`D:` date, a timestamp). Keep standalone matches hard, bare or formatted.
`ssn`/`us-phone`: 28.3% → 5.1%. Gate not met. Moves no case-library
leak.

**B. Context rule, wide.** A, plus demote any match not grouped as the
class's real values are (bare digit runs, lists of small numbers). Keep
only matches written like a real SSN (`3-2-4`), phone number (`3-3-4`,
optional `1`/parentheses) or card number (groups of four) hard.
`ssn`/`us-phone`: 28.3% → 1.0% (5 files). Gate missed by one file in
488. Moves one case-library leak: an SSN in form digit boxes, read as
nine bare digits. Tax and benefit forms, among the owner's document
types, are written that way.

**C. Demote the class.** Every match of the class is review. 28.3% → 0.
Moves all 9 pattern-only leaks to review; they relabel 1 → 2 (ADR
0005's list, plus the trailer and sideways-scan cases).

**E. B with a digit-box exception.** B, but a bare run read from single
digits in evenly spaced boxes stays hard. This needs the glyph geometry
`views.text.visual_text_glyphs` now provides (#120), and has not been
measured: neither how many clean corpus files hold such runs, nor whether
OCR and the other layers can tell boxes apart.

**D. Mix per class.** For example B for `ssn` and `us-phone`, A for
`credit-card` (whose validator already rejects most noise: 1.4%, none
real-looking), and `email` unchanged.

**Gate options** (the owner's, independent of A–D):

- **G1. Keep "< 1%", counting every hard file.** Only C (or C for `ssn`)
  meets it today.
- **G2. "≤ 1%".** B's 5/488 is 1.02%, so even this only passes if the
  rate is rounded to one decimal. Not recommended: it moves the bar to
  fit a number.
- **G3. Count only matches that are not value-shaped.** A clean file
  holding a dashed `123-45-6789` is treated like an email address: a true
  positive for "a value of this class is written here", which the rules
  file's author opted into by enabling the class. Under G3, B scores 0 of
  488, and A scores between 20 and 25 (4.1–5.1%; PR 1's counts do not say
  how many of the 5 value-shaped files also hold a bare run). This is the reading ADR 0005 already applied
  to `email`.
- **`email`** is excluded from the gate under ADR 0005's reasoning in
  every option above; the owner may confirm or reverse that here.

## Recommendation

**In two steps, with `email` unchanged:**

1. **Now: A for every digit class** (`ssn`, `us-phone`, `credit-card`).
   It moves no case-library leak and removes 82% of today's false hards
   (138 → 25 files, 28.3% → 5.1%). It does not meet the gate, and the
   owner should hear that plainly; it is the most that can be taken
   without a measured recall cost.
2. **Then: E for `ssn` and `us-phone`**, once the digit-box exception is
   built and measured on both the corpus and the case library, aiming at
   B's 1.0% without B's form-box loss, **with gate G3**: the 5 files left
   hold values written exactly like an SSN or a phone number, the shape no
   context rule can tell from a secret, and G3 counts them the way ADR
   0005 counts an email address.

Why not B now: its one recall loss is the owner's own document type. Why
not C: it gives up every pattern-only leak (9 cases) to reach a number. G2
moves the bar to fit the number; G3 instead says what the remaining files
are.

Conditions: Lane B's U3 number does not show good redactions held at exit
2 by these shapes; if it does, the owner may weigh C for `ssn` again. Any
case relabelled (none under A) goes under `weaker: true` in
`eval/accepted_diffs.yaml` with this ADR as the owner decision and a
CHANGELOG line.

If the owner wants the gate as written (G1) met now, only C for `ssn`
does it (with B for `us-phone` and `credit-card`: 2 files, 0.4%), at the
cost of all 9 pattern-only SSN leaks moving to review.

## Consequences

- Verdicts: some pattern-only findings move from exit 1 to exit 2. Never to 0.
- PR 2: the recall measurement (2a, done above), then the chosen option
  as a pure tier policy in `matching/` with tests (2b). No call-site
  change.
- PR 3 (after #100 merges and this ADR is accepted): the policy wired
  into `verify.py`'s pattern call sites; accepted diffs marked
  `weaker: true`; case relabels; docs in sync; the baseline re-run to
  confirm the gate.
- Later (after Lane C's #120): geometry-based split matching, which is
  stricter and moves `match.pattern-split`.

## Owner decision

Pending.
