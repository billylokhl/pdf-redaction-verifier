# 0010. Readers are the authority; agreement is the gate

Status: accepted (owner approval, 2026-09-28)

## Context

Phase 3a's first three PRs (#32, #34, #36) went through repeated review
rounds. Across the seven rounds that did not clear, the must-fix findings
fell into three classes, none of which review can close for good:

1. **Reader disagreement** accepted without a flag: a UTF-16LE text
   string decoded as NUL-separated letters where MuPDF and qpdf show
   text; `endstreamendobj`, where MuPDF ends the stream and qpdf reads
   a longer one; whitespace and NUL bytes after an exact `/Length`,
   which MuPDF reads as stream data -- an image made of them renders
   text -- while we and qpdf read nothing.
2. **Complexity**: three quadratic-time bugs (the tiling sweep,
   duplicate-key comparison, a whole-file parse without end bounds).
3. **The static child-side import guard**, a list of forbidden dynamic
   routes that every round extended (builtins, imp, timeit/code/pickle,
   typing's string evaluators, modules reachable as attributes).

A one-time design review (2026-09-28) judged the architecture sound --
the evidence ledger, fail-closed verdict, byte tiling and parent anchors
-- but found one design flaw behind class 1 and a method gap behind all
three:

- §4 "Ambiguity detection" named *our tokenizer* the authority. For a
  leak checker the authority is what readers display. The planned
  "parser agreement" check (§4, Phase 3c) compares only the in-use
  object set and page tree, and only in 3c: at that granularity it
  would have caught none of the class-1 bugs. Tiling proves every byte
  is attributed; it does not prove the attribution matches a reader's.
- Each class needs an automated oracle, not another review: a
  differential against the real readers (class 1), a work budget
  (class 2), and a process boundary that makes the static guard
  unnecessary (class 3).

## Decision

Owner approval, 2026-09-28, of every recommendation:

1. **Readers are the authority.** The inventory's parser certifies that
   the readers agree; it does not decide the reading. Every lenient
   branch in the parser either flags its input, or is on a written
   allowlist where each entry has a test showing MuPDF and qpdf read it
   identically. REDESIGN §4 is rewritten accordingly.
2. **Agreement is the 3a gate.** The differential harness started in
   3a-3b grows to value level and becomes 3a-6's gate: per object,
   compare the object set per revision, raw stream extents and
   dictionary string and name values against MuPDF and qpdf (`--json`
   v2 with raw stream data, one process per file), over the 2,031-file
   corpus, the case library and fuzz output. **Gate: zero unflagged
   disagreements**, flagged ones reported by reason, plus 0 crashes and
   0 timeouts. ADR 0001's byte-level stream cross-check extends to
   unencrypted files.
3. **Linear time is enforced by the budget, not by review.** A
   file-wide work counter (bytes lexed, rescans included, and tokens),
   capped at a multiple of the file size, reports `BUDGET_EXHAUSTED`;
   CI times adversarial inputs at n and 8n. A future quadratic bug
   shows up as a flag in the corpus numbers, not as a hang.
4. **3a-4 is canonical, not a repair emulator.** A closed set of xref
   chain shapes; every entry must land exactly on a matching `N G obj`;
   each revision's object map must equal MuPDF's and qpdf's without
   repair warnings; anything else flags the whole file. No emulation
   of readers' repair logic.
5. **A minimal child process moves into 3b** (from 3d): `python -I -m`
   the child, an empty environment, the PDF passed as an open file
   descriptor and never the rules path, CPU/memory/core rlimits, a
   wall-clock kill, a typed protocol the parent validates, and a canary
   test that no rules value reaches the child's argv, environment or
   descriptors. Secrets are data: a child that never receives them
   cannot leak them, whatever it imports. The OS-level sandbox (no
   network, no filesystem) stays in 3d.
6. **Rules before code**: the canonical-form rules for the reference
   graph (an edge kind we do not model makes its target an orphan,
   `FLAGGED`; where readers' resource inheritance differs, decode under
   each and flag), decryption (a closed set of V/R/CFM/StmF/StrF/EFF
   combinations; two decryptors must agree) and the decoders (a filter
   stage is `DECODED` only if our output equals MuPDF's and its input
   is consumed exactly) are written as ADR notes before those PRs.
7. **Review method.** Every review finding records the automated check
   that would have caught it, and that check is added. Parser PRs stay
   small. REDESIGN §7's cells are to be reduced to invariants, each
   with its test, with history moved to the CHANGELOG.

Also decided the same day:

- **Review triage.** A review finding blocks a merge only when it could
  let a leak exit 0, could hide or misattribute bytes without a flag,
  shows the plan is flawed, or is a major implementation problem (a
  crash on input, superlinear time on plausible input). Uncommon cases
  and minor hardening are filed as GitHub issues and followed up later.
- **The static import guard is frozen** as a mistake-catcher
  (tests/test_import_boundaries.py). Routes past it are filed against
  the child process (item 5), not fixed in the guard.
- **Measure before enforcing.** Items 1, 2 and 4 will raise the
  `FLAGGED` (exit 2) rate on real files; exit semantics do not change.
  The rate is measured with item 2's harness and brought to the owner
  before any gate or default is enforced on it.

## Consequences

- About a week of work before 3a-4 (items 1-3), expected to be repaid
  by fewer review rounds; 3a-4 itself gets smaller (item 4).
- 3c's "parser agreement" becomes a re-run of 3a's agreement harness
  against the full decoded view, not the first comparison with readers.
- Churn still expected later, most costly first: the content decoder's
  dependence on MuPDF's version (ADR 0008/0009 re-measurement on every
  PyMuPDF upgrade); 4b's "used" geometry (decision D); font-program
  span parsing (prefer fontTools, under the budget, in the child); 4d
  filter end-of-data rules; 3c's flag rate (7.2% measured by a proxy).
