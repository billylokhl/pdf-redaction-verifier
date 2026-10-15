# 0012. Built-in pattern classes inform; listed secrets decide the verdict

Status: accepted (owner decision, 2026-10-15). No verdict changes until
the implementation PR ships it.

## Context

REDESIGN.md §1 names a matching problem: the built-in pattern classes
(`ssn`, `credit-card`, `email`, `us-phone`) have no context and raise false
**hard** findings on clean real files. Phase 5 (§7) must fix it; its gate
is "built-in pattern false hard < 1% on text-bearing real files".
[ADR 0005](0005-pattern-class-default-tier.md) kept the classes hard until
then.

Earlier the same day the owner had bounded Phase 5 to demotions only (a
class match could go from a hard finding to review, exit 1 → 2, never to
0), and this ADR's first draft weighed context rules within that bound
(Appendix). On 2026-10-15 the owner then gave a different direction,
relayed by the coordinator:

> If a match is not one of the secrets the user defined, why alert at
> all? The use is to confirm that the secrets the user listed are gone.

That is **policy 1**: a built-in class match that matches no listed value
no longer affects the exit code; it is still reported, as information
("SSN-shaped value on page 3"). Exit 0 then means that none of the listed
values, and none of the user's own `pattern` rules, was found in any layer
the tool read, and that nothing stopped it from reading a layer. It **supersedes the demote-only bound** for the built-in
classes and is a loosening that moves some cases to exit 0, so under
REDESIGN §6/§7a it needs the per-case owner sign-off listed below,
`weaker: true` entries in `eval/accepted_diffs.yaml`, and CHANGELOG lines
describing each layout.

## Decision

1. **Policy 1, as the owner stated it** (the default mode). A hard finding or a review
   warning whose rule is a built-in class becomes an informational notice:
   reported, never counted in the exit code. Findings of value rules, and
   every coverage warning, are unchanged.
2. **Scope** (each proposed with a recommendation; the owner adopted every
   recommendation):
   - **(a) User-written `pattern` rules stay verdict-affecting.**
     Adopted: yes. A user writes a regex for something they mean to
     find; it is a defined secret, not a guess the tool supplies. Only the
     four built-in classes become informational.
   - **(b) A strict mode keeps today's behaviour.** (Exit codes, tiers,
     findings and warnings as today; the `--json` report is
     `schema_version` 2 in both modes, with `notices` empty in strict
     mode.) Adopted: policy 1
     is the default, and `--strict-classes` (and a rules-file key,
     `"strict": true` on a class rule) restores today's tiers exactly, for
     users who rely on the classes as a net for secrets they did not
     list. No demotion option (Appendix) is built into strict mode now:
     strict means today's behaviour, unchanged, so it never loosens.
   - **(c) Wording.** Adopted: the human report prints class matches in
     their own section after the verdict, headed "Not counted in the
     verdict: values shaped like a built-in class", each line saying the
     value is not one of the listed secrets. The `--json` report moves
     them from `findings`/`warnings` to a new top-level `notices` array
     (same fields, `kind: "class-match"`) and bumps `schema_version` to 2,
     because a consumer reading `findings` would otherwise see a meaning
     change. README states plainly: exit 0 means none of the listed values
     (and none of the user's own `pattern` rules) was found in any layer
     read; it says nothing about other SSNs, card numbers, phone numbers
     or addresses in the file, which the notices show.
3. **The gate.** Under policy 1 the class-caused false-hard rate is 0 by
   construction, so the gate as written stops measuring anything in the
   default mode. Restated: "default mode: built-in classes cause no
   exit-code change on any file (checked on the corpus and the case
   library); strict mode: unchanged from today".
4. **The Appendix's options A, B, C and E are moot for the default mode**
   and are not built. They stay recorded in case strict mode later needs
   its noise reduced, which would be its own decision.

## Measurement

**Corpus** (Phase 5 PR 1, #134: the per-file reports of its run at commit
90669ef, recomputed under policy 1 by `eval/scorecard/pattern_policy1_corpus.py`;
rules were the four classes only, so under policy 1 each file's exit is set
by coverage warnings alone; counts only; eval/spikes/RESULTS.md, "Phase 5
baseline"):

| Text-bearing, 488 files | Exit 0 | Exit 1 | Exit 2 |
| --- | --- | --- | --- |
| today | 131 (26.8%) | 235 (48.2%) | 122 (25.0%) |
| policy 1 | 248 (50.8%) | 0 | 240 (49.2%) |

Moves: 111 files 1 → 0; 124 files 1 → 2 (they also carry a coverage
warning, so they leave the hard tier for review, not for 0); 6 files
2 → 0 (their only warning was a class review). All 2,072 measured files:
exit 0 from 1,579 to 1,785, exit 1 from 337 to 0. This covers all four
classes, `credit-card` (1.4% of text-bearing files today) and `email`
(23.0%) included. Policy 1 has no effect on U4 as defined (exact-value rules only); with
class rules enabled, it can only lower the review rate.

**Case library** (Phase 5 PR 2a, #138, `eval/scorecard/pattern_recall.py`):
every case with a built-in class rule scanned, and each case's exit
recomputed under policy 1; the per-case list is below. Every class rule in
the case library is `ssn`: no `us-phone`, `credit-card` or `email` leak
case exists, so nothing about those classes' recall is measured.

**Lane B's U3** (good redactions that exit 0): policy 1 removes the class
matches that hold a good redaction at exit 1 or 2, so U3 can only rise; by
how much depends on whether Lane B's runs include class rules, which Lane B
reports.

## Cases that need the owner's sign-off

Leak cases that reach exit 0 under policy 1 (#138's run): by the exit
they give today, or by their label (target) where a known gap's target was
set by the class alone; the 11 known gaps exit 0 today and still do, only
their target changes. A case reaching 0 is a pinned loosening: the planted
secret is caught only by a class rule.

Every one is an `ssn` case (the case library has no other class leak
case). 35 leak cases reach exit 0: 24 by the exit they give today, and 11
known gaps whose target (the label) is set by the class alone.

| Today | Cases | What they are |
| --- | --- | --- |
| exit 1 (9) | `page.visible-pattern-rule`, `layout.single-line-pattern-only`, `layout.rotated-pattern-only`, `layout.form-boxes-pattern-only`, `layout.overlapping-values-pattern-only`, `layout.overlapping-split-short-tail-pattern-only`, `document.trailer-key-pattern-rule`, `page.scan-image-sideways-90`, `page.scan-image-sideways-270` | an SSN on one line, rotated, in form digit boxes, overlapping other text, as a trailer key, or in a sideways scan, with the class rule as the only rule that catches it |
| exit 2 (14) | `layout.split-lines-pattern-only`, `layout.split-three-lines-pattern-only`, `layout.split-pages-pattern-only`, `layout.split-pages-needs-later-lines-pattern-only`, `layout.split-three-pages-pattern-only`, `layout.split-over-blank-page-pattern-only`, `layout.two-page-breaks-pattern-only`, `layout.overlapping-split-pages-pattern-only`, `layout.repeated-line-wraps-pattern-only`, `layout.several-values-pattern-only`, `layout.table-rows-pattern-only`, `layout.table-rows-split-pages-pattern-only`, `layout.vertical-stack-pattern-only`, `document.trailer-id-digits-pattern-rule` | an SSN split across lines, pages or table rows, read in a vertical stack, or as digits in the trailer `/ID`, whose only warnings are class reviews |
| known gap (1) | `page.dense-sideways-scan` (K43) | exit 2 today on macOS 27, 1 expected once K43 is fixed; 0 under policy 1 either way |
| known gaps, target exit 2 (11) | `document.af-pattern-rule`, `document.js-stream-pattern-rule`, `document.piece-info-pattern-rule`, `page.hidden-sample-bytes-pattern-rule` (REVIEW_BINARY); `layout.form-boxes-split-pages-pattern-only`, `layout.rotated270-split-pages-pattern-only`, `layout.split-four-pages-pattern-only`, `layout.split-pages-needs-earlier-lines-pattern-only`, `layout.vertical-stack-split-pages-pattern-only`, `layout.undashed-ssn-wrapped`, `layout.rotated-split-pages-pattern-only` (REVIEW_FUSED_PATTERN; needs `no-ocr`, so not scanned here: judged from its label) | exit 0 today (missed); their target, review from the class alone, becomes exit 0. In strict mode they keep their target and stay tracked as gaps |

The 31 cases with a value rule beside the class rule cannot be judged
from their labels (a label lists warning codes, not the rule that raised
them); this machine's scan settles them, and none changes. The lists
describe macOS 27 with every tool installed.

Two clean cases change as well, both improvements, but their labels
change: `document.generated-id-digits` (2 → 0; expected 0) and
`page.digit-column` (2 → 0; expected 2, the clean side of #74).

Treatment (adopted): keep each layout tested by running it in strict mode
with its current expectation, and add the default-mode expectation (the
new exit, with an informational notice). The case library's labels
for policy 1 then say "not a listed secret" rather than pinning a miss.

## Consequences

- Verdicts: files whose only findings or review warnings come from
  built-in classes move to exit 0 (or to 2 when a coverage warning
  remains). Each case-library leak case that moves is signed off by the
  owner, recorded `weaker: true`, and described in the CHANGELOG.
- `--json`: `schema_version` 2 with a `notices` array (scope c).
- A new flag `--strict-classes` and rules-file key (scope b).
- PR 2b: the policy as pure code in `redaction_verifier/matching/`, with
  tests; no call-site change. PR 3 (after #100 merges): wired into
  `verify.py` and the reports, relabels, docs in sync, and the corpus
  re-run to confirm no exit-code change from a class match in default
  mode.
- Coverage follow-up (owner, 2026-10-15): the implementation PR adds at
  least one `us-phone`, one `credit-card` and one `email` leak case, so
  strict mode has recall coverage for every class.
- ADR 0003 and the Phase 4b row run pattern classes over raw image bytes
  at review tier; in default mode that review becomes a notice too (strict
  mode keeps it).
- Geometry-based split matching (Phase 5's other item, now possible with
  #120's glyph geometry) is unaffected: it concerns listed values.

## Owner decision (2026-10-15)

All recommendations adopted, relayed by the coordinator:

1. **Policy 1 adopted** for the built-in classes and **confirmed to
   supersede the demote-only bound for them in the default mode**; the
   demote-only bound still binds everything else and strict mode.
2. **Scope (a):** user-written `pattern` rules stay verdict-affecting; only
   the four built-in classes become informational.
3. **Scope (b):** policy 1 is the default; `--strict-classes` and a
   rules-file key (`"strict": true` on a class rule) restore today's
   behaviour exactly, opt-in.
4. **Scope (c):** the wording as proposed: notices in their own report
   section, a JSON `notices` array with `kind: "class-match"` and
   `schema_version` 2, and the README stating what exit 0 covers.
5. **The gate** restated as in Decision 3.
6. **Per-case sign-off approved** for all 35 `ssn` leak cases that reach
   exit 0: the 24 by today's exit (9 from exit 1, 14 from exit 2, and K43's
   `page.dense-sideways-scan`), and, in a second sign-off the same day, the
   11 known gaps whose target is set by the class alone. The SSN is not a
   listed secret. In strict mode each keeps its current label and target. Each is
   recorded `weaker: true` with this sign-off and a CHANGELOG line naming
   its layout. The relabels of the clean cases `document.generated-id-digits`
   and `page.digit-column` are covered by the same decision.
7. **Case library:** strict-mode runs keep today's expectations; default
   mode expects exit 0 plus a notice.

**Owner decision 2026-10-15 (policy 1 explicitly framed as covering YAML
`entity_types`), recorded by the coordinator:** a redactor config's
`entity_types` (listed, or all of them when the key is omitted) are
built-in classes, so in the default mode their matches are notices like
any class's; `--strict-classes` makes them count. One rule for every rules
format: the same SSN-shaped value never gives exit 1 in one format and 0
in the other. A YAML config has no per-type `strict`.

## Coordinator decision (2026-10-15, owner informed)

Strict mode's "today's behaviour exactly" means exit codes, tiers, findings
and warnings as today. The `--json` report is `schema_version` 2 in both
modes, with `notices` empty in strict mode; there is no per-mode schema.

## Appendix: the demote-only options weighed first

Measured before the owner's direction (Phase 5 PR 1 and 2a; RESULTS,
"Phase 5 baseline" and "Phase 5 recall"). Not adopted; kept for the
record and for strict mode, should its noise ever need reducing (a
decision of its own).
Shapes are `eval/scorecard/match_shapes.py`'s. A keeps `formatted`,
`number-list` and `bare-digits` hard (a `formatted` match stays hard even
inside a token). B keeps only `formatted` (SSN 3-2-4, phone 3-3-4, card
groups of four or 4-6-5/4-6-4). C keeps nothing. E would be B plus a
digit-box exception, never built or measured.

| Text-bearing, 488 | Hard files left, `ssn`/`us-phone` | `ssn`/`us-phone`/`credit-card` | Case-library `ssn` leaks moved 1 → 2 |
| --- | --- | --- | --- |
| today | 138 (28.3%) | 139 (28.5%) | -- |
| A | 25 (5.1%) | 25 (5.1%) | 0 |
| B | 5 (1.0%) | 5 (1.0%) | 1 (`layout.form-boxes-pattern-only`, an SSN in form digit boxes) |
| C | 0 | 0 | 9 |

"Hard files left" is an estimate that errs toward keeping files hard, with
one blind spot the other way (a layer's rolling scanner stops once every
rule has hit). Demoted files go to exit 2, not 0, so no demotion option
raises the exit-0 share. A gate reading that counts only matches not
written like a real value ("G3") would score B at 0 by construction: it
changes what the gate counts rather than measuring less noise.
