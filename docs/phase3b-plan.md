# Phase 3b (Ledger + verdict) plan

Drafted 2026-10-15 (Pacific), for review by the coordinator and owner. This is the PR sequence
for REDESIGN §7's row 3b, as [phase3a-plan.md](phase3a-plan.md) was for 3a. The open
questions at the end need decisions before the PRs they name start. Nothing here changes a
verdict.

Read with: REDESIGN §2 (obligations, parent anchors), §3 (principles 1, 2, 4, 6, 8), §4
(process boundary, core types, decoder registry), §6 (shadow and enforced verdicts, worst-of)
and §7a; ADR 0003, ADR 0006 (amendment: `Limits.max_units` 500,000), ADR 0007 (note: the
overlap), ADR 0008, ADR 0010 item 5, ADR 0011 item 5 and R1-R7; RESULTS.md "Phase 3a-7e" and
"Phase 3a-12".

## Ground rules for every 3b PR

- **Shipped exit unchanged, except where a PR says otherwise.** Only 3b-8 may change a
  shipped exit, and only to exit 2 on a run whose child failed (Q1). Nothing else may change
  one. That covers every case in the case library and every corpus file.
  - The scorecard stays at its current accepted list: 0 unlisted, 0 crashed.
  - CHANGELOG: "Verdict changes: none", except for 3b-8.
- **The #147 rule.** Every PR that touches `verify.py`, `redaction_verifier/report/*` or
  `redaction_verifier/model.py` (3b-1's re-export, 3b-7, 3b-8, 3b-9) needs a passing
  macOS+OCR scorecard run on its branch before merge (`gh workflow run scorecard-weekly.yml
  --ref <branch>`), recorded in the PR body. The Linux every-PR differential must also pass.
- **The verdict rules live in the parent.** That means the rules file and secrets, plus every
  rule that turns statuses into an exit: the ORPHANED mapping, STRUCTURAL's whole-parse
  condition, witness checks, the enforced set and worst-of.
  - The child reports facts: units, flags, labels, decoder results and witnesses.
  - It never decides an obligation's final status.
- **Types and checking.** New modules are mypy strict, with Python 3.10 as the floor.
  - `verdict.py` targets 100% branch coverage now (REDESIGN Phase 6 requires it), so the
    target is never back-filled.
  - Every closed enum is pinned by a test, and every new `FlagReason` gets an EMITTERS entry.
- **Import guard.** Every new child-side module is added to the child-side import guard
  (`tests/test_import_boundaries.py`). Lane A2 owns that file's edits (LANES.md), so each PR
  that adds a module asks A2 for the entry or makes it with A2's review.
- **One problem per PR.** About 600 changed code lines triggers a split decision.
- **Docs in sync.** Each PR updates README, DESIGN, COVERAGE, eval/README, REDESIGN and
  CHANGELOG as applicable.
- **Review.** Each PR gets an independent adversarial review. Use Opus for this phase: all of
  it is on the exit-0 audit surface (Principle 2). The PR body ends with the Review table.
- **Nothing document-derived crosses the pipe in 3b.** No bytes, text or names cross. Every
  value is an integer or a closed enum's name (Q4).

## 1. Scope

REDESIGN's row 3b covers:

- obligations and parent anchors;
- the child protocol and its completion sentinel;
- witnesses;
- a single verdict function, with shadow and enforced verdicts;
- worst-of shipping;
- `--explain`.

The row also fixes these verdict rules:

- Every stream the reference graph labels `ORPHANED` is `FLAGGED`, whatever its decoder
  reports. This is per body across the revisions it lives in (`Inventory.body_labels`). A
  test pins that a `DECODED` orphan still yields `FLAGGED`.
- A stream that is both `STRUCTURAL` and `DRAWABLE` is drawable: every label's decoder runs,
  and the worst status wins.

ADR 0010 item 5 moves the minimal child process here from 3d: `python -I`, an empty
environment, the PDF as an open descriptor and never the rules path, rlimits, a wall-clock
kill, a typed protocol and a canary test. REDESIGN §4 (#176) runs `crypt.py` (3a-9) in that
child, beside the inventory and the decoders. The gate is:

- the shipped exit is identical to the reference on every case;
- the shadow verdict is reported;
- a truncated or empty child report exits `2`.

**Not 3b:**

- **3c, parser agreement:** the re-run of 3a's agreement harness against the full decoded view,
  and ADR 0002's two check gaps. 3b creates the parser-agreement obligation, `UNEXAMINED`,
  so nothing can be certified without it; 3c discharges it.
- **3d, the sandbox:** the OS sandbox, watchdog tuning, the private temporary directory as a
  gate, and the bomb and hang cases exiting `2`. 3b uses `redaction_verifier/sandbox.py` (#166)
  as merged, with `os_sandbox=False`.
- **4a-4d, decoders.** Content streams, fonts, the consumption witness as an enforced gate
  (ADR 0008), synthesized appearances and the string/name token decoder are 4a. Images,
  decision D's "used" and the strips-under-a-box question are 4b. Owner decision 2026-10-15:
  strips under a box must be caught, and Lane G measures. Containers are 4c; filter chains and
  `RESIDUE` are 4d.
  - 3b's registry holds only the structural kinds the inventory already decodes. Every other
    unit kind stays `UNEXAMINED`, with the reason `NO_DECODER`.
  - **No unit kind is enforced in 3b** (REDESIGN §6).
- **The hint-table decoder.** RESULTS.md "Owner choice", option (a), staged. Owner decision
  2026-10-15: the spike runs in parallel, and hint streams stay flagged meanwhile.
  - Its six PRs are outside 3b's count. Its "registry hookup in 3b" (step 3) lands once its
    decoder exists, in 3c.
  - 3b's registry is built so that the hookup is a single new entry plus flipping one pinned
    test (§4 below).
- **Enforcing anything measured in 3a-12.** The 707 label-class files and the inventory's 153
  flagged files stay shadow-only. They are enforced per kind, each from its decoder's phase,
  and all of them only when the shadow verdict ships (Phase 6).
- **Phase 5.** Lane F's "PR 3b" (#160) is Phase 5's third PR, not this phase.

## 2. The PR sequence

Ten PRs, plus this plan. Lane A owns the core and Lane A2 owns the child (DEPMAP). "Verdict
path" means the PR touches code the shipped CLI runs.

**3b-1. Obligations, witnesses and labels: the types** (Lane A)

- **Deliverable.** Additions to `ledger.py`, append-only:
  - **Reference types:**
    - `SynthesizedRef(body, revision)`: a SYNTHESIZED unit. Not a `UnitKind`, since it
      claims no bytes and `tile()` must keep rejecting it.
    - `ViewRef(page, reading)`: reading is TEXT or OCR.
    - `ToolRef`: a closed `Tool` enum (qpdf, exiftool, OCR).
    - `RuleScopeRef`.
    - `CheckRef`: a closed `Check` enum. Its members:
      - `CHILD_COMPLETE`;
      - `FILE_IDENTITY`;
      - `TILING`, which covers the gap bytes;
      - `PAGE_COUNT`;
      - `OBJECT_SET`;
      - `INVENTORY_FILE_FLAGS`;
      - `PARSER_AGREEMENT`, 3c's;
      - `RAW_MATCH` (Q8).
  - **Records:** `Obligation(id, status, reason, witness)` and `Witness`.
    - `Witness` holds the byte-consumption spans, in the decoded coordinates of the unit's
      stage, and the length they must tile.
    - Fields for ADR 0008's glyph counts are reserved, not populated, until 4a.
    - `DecodeResult` gets no evidence and no children in 3b (Q4).
  - **Labels:** a closed `Label` enum (`DRAWABLE`, `STRUCTURAL`, `ORPHANED`, `SYNTHESIZED`)
    and a closed `StructuralKind` matching `labels.py`'s records.
  - **Reasons:** a closed `PendingReason` for `UNEXAMINED` (`NO_DECODER`, `NOT_IN_PHASE`),
    and the closed reasons for `UNREADABLE` and `FAILED`, `NOT_REPORTED` among them (Q6 names
    them).
  - `model.py` re-exports the new names, additively.
- **Files:** `redaction_verifier/ledger.py` (append-only; Lane A2's #178 appends
  `DECRYPTION_*` to it, so rebase), `redaction_verifier/model.py` (re-export lines only),
  `tests/test_ledger_types.py` (EMITTERS, shared).
- **Depends on:** nothing beyond main. It can start before 3a-14 (Q7).
- **Tests:**
  - every enum is closed and pinned;
  - the records are frozen;
  - every field that can cross the pipe holds only plain integers or enum members, as `Flag`
    does;
  - `Witness` balances only when its spans tile `[0, length)` exactly;
  - an empty span is invalid. This settles 3a-1's open note: a unit reporting an empty span
    is a protocol error.
- **Gate:** the suite, mypy strict and the import guard.
- **Verdict path:** none. `model.py` gains re-export lines only, but the #147 run applies
  anyway because the PR touches `model.py`.

**3b-2. The child protocol** (Lane A)

- **Deliverable.** A new `redaction_verifier/protocol.py`, pure (the standard library's `json`
  plus `ledger`). It holds:
  - the message schema of §3;
  - the encoder the child uses;
  - the parent's strict validator.

  The validator returns a `ChildReport`, or a closed `ProtocolError` that makes
  `CHILD_COMPLETE` `FAILED`.
- **Files:** `redaction_verifier/protocol.py`, `tests/test_protocol.py`, and the guard entry
  (A2).
- **Depends on:** 3b-1.
- **Tests (Hypothesis):**
  - every report round-trips;
  - **no proper prefix of a valid stream validates**, whether cut at a line or at a byte;
  - a single-byte mutation either is rejected or validates to a report whose sentinel counts
    and hash still match what it carries, never to a report with obligations silently
    dropped;
  - duplicate, out-of-order and unknown messages are rejected, as are messages after the
    sentinel;
  - a count past its `Limits` bound is rejected, as is a non-integer where an integer belongs;
  - any string field outside its closed enum is rejected.
- **Gate:** the suite and mypy strict.
- **Verdict path:** none.

**3b-3. The decoder registry and the structural decoders** (Lane A)

- **Deliverable.** New modules `redaction_verifier/decoders/__init__.py` (the registry: a label
  or unit kind maps to a `Decoder`) and `decoders/structural.py`. They run in the child.
- **The 3b entries:**
  - **Cross-reference streams and object streams:** the inventory's own decoding.
    - `DECODED` with a witness when the inventory read the stream whole and unflagged.
    - Their field data and header table are `NOT_APPLICABLE` under ADR 0003 reasons 1 and 2.
  - **Hint streams:** `UNREADABLE` (ADR 0003, R6 (a)).
  - **XMP, JavaScript, output intents and embedded files:** no entry, so `UNEXAMINED`
    (`NO_DECODER`).
  - **Every DRAWABLE kind:** no entry until 4a/4b.
  - **SYNTHESIZED:** `UNEXAMINED` until 4a.
- **The `HEADER`, `XREF_TABLE` and `XREF_EPILOGUE` units:** `DECODED` by the inventory's
  canonical parse, with a witness over their span. Their bytes stay under `RAW_MATCH` (ADR
  0003's governing rule; owner decision 7: claimed comment bytes must be raw-searched).
- **Files:** `redaction_verifier/decoders/`, `tests/test_decoders_structural.py`, and the
  guard entry (A2). Read-only use of `inventory/types.py` and `labels.py`; no inventory file is
  edited.
- **Depends on:** 3b-1.
- **Tests:**
  - every registry entry is pinned;
  - **the hint stream is `UNREADABLE`**, a pinned test the hint-table hookup must flip
    explicitly;
  - a structural decoder whose witness does not cover the whole decoded stream reports so, and
    3b-4's test turns that body `ORPHANED`, as 3a-7e promised;
  - a body that is both structural and drawable gets both labels' results;
  - work is charged to the `Budget`.
- **Gate:** the suite and mypy strict.
- **Verdict path:** none.

**3b-4. The verdict function** (Lane A)

- **Deliverable.** A new `redaction_verifier/verdict.py`, pure. It holds:
  - `build_ledger(anchors, report | failure) -> Ledger`;
  - `verdict(ledger, enforced) -> Verdict`, with the shadow exit, the enforced exit and the
    undischarged obligations grouped by kind, status and reason;
  - `worst_of(a, b)`, ordered `0 < 2 < 1`.

  `check_tiling` moves from `inventory/tiling.py` to `ledger.py`, and `inventory/tiling.py`
  re-exports it. The parent can then re-run it without importing the inventory package,
  whose `__init__` loads `build_inventory`.

  Also in this PR, `Limits` is validated at construction (3a-1's open note), because the
  parent now uses its values as protocol bounds.
- **Files:** `redaction_verifier/verdict.py`, `ledger.py` (`check_tiling`, moved),
  `inventory/tiling.py` (the re-export, one line, in coordination with Lane A2's 3a-9b claims),
  `budget.py` (validation), `tests/test_verdict.py`, and the guard entry (A2).
- **Depends on:** 3b-1, 3b-2 (`ChildReport`) and 3b-3 (result shapes).
- **Tests:**
  - **every §4 rule;**
  - **properties:**
    - the shadow exit is never 0 while any obligation is undischarged;
    - worsening any obligation never lowers either exit;
    - replacing a valid report with any protocol failure never lowers either exit;
    - `worst_of` is commutative, associative and monotone;
  - an exhaustive `match` over `Status`;
  - 100% branch coverage.
- **Gate:** the suite, mypy strict and the coverage target.
- **Verdict path:** none.

**3b-5. The child entry** (Lane A2)

- **Deliverable.** A new `redaction_verifier/child.py`, run as `python -I` under
  `sandbox.run`. Its sequence:
  1. read the input descriptor and hash it;
  2. run `crypt.decrypt` per encrypted revision and `build_inventory` with the decryptions
     (3a-9b-1's interface);
  3. run the registry;
  4. write the protocol.

  It also owns:
  - **the protocol's own descriptor.** The protocol goes to a dedicated descriptor, not fd 1.
    fd 1 is pointed at `/dev/null` before `pymupdf` or `pikepdf` load, so library output can
    never corrupt the stream.
  - **package lookup under `-I`.** The child finds the package even when the CLI runs from a
    checkout (`python verify.py`). `-I` drops the script directory from `sys.path`, so the
    parent passes the package root, a code path and never a rules path, and the preamble
    prepends it.
  - **an `input_fd` variant of `sandbox.run`.** The child must read the same open file
    description the parent hashed, not reopen the path.
- **Files:** `redaction_verifier/child.py`, `redaction_verifier/sandbox.py` (the `input_fd`
  variant and the protocol descriptor), `tests/test_child.py`, and
  `tests/test_import_boundaries.py`. In the guard, `child.py` may import the inventory, crypt,
  decoders, protocol, ledger and budget, and never rules, matching, report, views or `verify`.
- **Depends on:** 3b-2, 3b-3 and 3a-9b-1 (Lane A2: `build_inventory` reading decryptions). It
  must not start on a stub interface.
- **Tests:**
  - **ADR 0010 item 5's canary:** a rules file with a unique value is run through the CLI path.
    The value must appear in none of the child's argv, environment, open descriptors or
    working directory.
  - the child's output validates on every case-library file and every encrypted fixture;
  - library output on fd 1 cannot corrupt the protocol;
  - an installed package and a checkout both work;
  - the rlimits apply;
  - a raising child yields a non-zero exit with no sentinel.
- **Gate:** the suite, plus a case-library sweep: every file's child report validates, with 0
  failures and 0 timeouts, on Linux and macOS.
- **Verdict path:** none. The module is new and unwired.

**3b-6. Parent anchors, the shadow runner and the measurement gate** (Lane A)

- **Deliverable.** New modules `redaction_verifier/anchors.py` and
  `redaction_verifier/shadow.py`, plus a new eval harness, `eval/scorecard/shadow.py`. None of
  it is wired into the CLI yet.
  - **`anchors.py`** establishes, from the parent's own facts:
    - the file size and SHA-256, from the descriptor it hands over;
    - the page count and the current revision's in-use object numbers, from qpdf in a
      separate process (`--show-npages`, `--show-xref`), using the existing subprocess
      conventions: absolute path, `--`, minimal environment;
    - the tool and rules-scope obligations.
  - **`shadow.py`** runs the child through `sandbox.run`, validates its output (3b-2), re-checks
    the tiling and every `WHITESPACE` region's bytes, builds the ledger and computes the
    verdict (3b-4). It never raises: any failure becomes `CHILD_COMPLETE` `FAILED`.
  - **The dev entry** `python -m redaction_verifier.shadow FILE` prints counts only, with no
    rules and no document bytes.
- **Files:** the three new modules, `tests/test_anchors.py`, `tests/test_shadow.py`, a "Phase
  3b" section in RESULTS.md, and eval/README.
- **Depends on:** 3b-4 and 3b-5.
- **Tests:**
  - **every failure path yields `FAILED`:** a kill for wall clock, memory or a stray process,
    a truncated report, an empty report, a garbled report, a hash mismatch, a tiling mismatch,
    a `WHITESPACE` region holding a non-whitespace byte, and qpdf missing or failing;
  - a page-count or object-set mismatch yields `FAILED` on that `CheckRef`.
- **Gate:** the measurement gates of §6, items 1-2.
- **Verdict path:** none.

**3b-7. The CLI hook, shadow only** (Lane A)

- **Deliverable:**
  - `verify.py` starts the shadow run concurrently with the legacy scan (Q2) and joins it
    before the report. The exit is unchanged.
  - `--json` gains `experimental.shadow` (§5). `JSON_SCHEMA_VERSION` stays 2: the field is
    additive, and it is documented as outside the schema's stability promise.
  - The scorecard gains a `shadow_exit` column in `metrics` and the corpus aggregate, outside
    the normalised key.
  - `keys.py` gains a test that `experimental` never enters the key.
- **Files:** `verify.py` (the hook only), `redaction_verifier/report/json.py` (one keyword
  argument, additive), `eval/scorecard/metrics.py`, `corpus.py`, `keys.py` (the test only),
  and `tests/test_json.py`.
- **Depends on:** 3b-6, and the `verify.py` queue: one open `verify.py` PR at a time, behind
  #167 and #160 today.
- **Tests:**
  - with the hook, the exit and every field outside `experimental` are byte-identical to
    without it, on every case-library file;
  - `experimental.shadow` is present on every path; it is `null`, with a status, on early
    error exits;
  - the JSON stays deterministic across runs, with no timings in it.
- **Gate:**
  - the scorecard diff is identical (Linux every-PR, and macOS+OCR on the branch, per #147);
  - the shipped exit is identical on every case;
  - the runtime overhead is measured (§6, item 4).
- **Verdict path:** yes. The hook must keep the shipped exit identical.

**3b-8. Worst-of: enforce the child-integrity checks** (Lane A)

- **Deliverable.**
  - **The enforced set** is the integrity `CheckRef`s: `CHILD_COMPLETE`, `FILE_IDENTITY` and
    `TILING` (Q1). No unit kind and no qpdf anchor joins it.
  - The shipped exit becomes `worst_of(legacy, enforced)`.
  - When the enforced exit raises the shipped one, the report carries a new stable warning
    (Q6), so the report stays self-consistent: the scorecard checks every exit against its
    report's own findings and warnings.
- **Files:** `verify.py`, `redaction_verifier/report/text.py` (the warning's line),
  `tests/test_shadow_enforced.py`, and CHANGELOG (Verdict changes).
- **Depends on:** 3b-7, and §6 gate 3 holding first.
- **Tests (the row's gate):**
  - a truncated, empty or garbled child report, or a killed child, exits 2;
  - legacy exit 1 with any child failure stays 1;
  - legacy exit 0 with a child failure becomes 2, with the warning;
  - legacy exit 2 is unchanged.
- **Gate:**
  - the scorecard diff stays identical on both platforms, with 0 child failures on the case
    library;
  - the corpus has 0 child failures;
  - the bomb cases (#168) are re-judged, and any `Bomb.today` pin that moves is recorded:
    stricter only, each moving pin named in the PR.
- **Verdict path:** yes. It is stricter only, and only where the child fails.

**3b-9. `--explain`** (Lane A)

- **Deliverable:**
  - `--explain` prints the enforced verdict's undischarged obligations, then a separately
    headed shadow section. Both are grouped by (kind, status, reason) with counts and integer
    provenance (revision, object, span), never text (Principle 8).
  - `--json`'s `experimental.shadow.undischarged` carries the same groups.
- **Files:** `verify.py` (the flag), `redaction_verifier/report/text.py`, and
  `tests/test_explain.py`.
- **Depends on:** 3b-8 and the `verify.py` queue.
- **Tests:**
  - the output is deterministic;
  - no document bytes or rule values appear, checked with the canary values;
  - every `PendingReason`, `FlagReason` and `Check` renders;
  - the exit is identical with and without `--explain`.
- **Gate:** the scorecard diff is identical (#147).
- **Verdict path:** yes, and the exit must stay identical.

**3b-10. Close-out** (Lane A)

- **Deliverable:** the final measurement (§6) re-run on main, the REDESIGN 3b row, ADR notes
  (ADR 0010 item 5: the protocol as built; ADR 0011 item 5: the verdict's mapping as built),
  and CHANGELOG.
- **Files:** docs and RESULTS.md only.
- **Depends on:** 3b-9.
- **Verdict path:** none.

Order and parallelism:

- 3b-1 comes first. 3b-2 and 3b-3 can then run in parallel, and 3b-4 joins them.
- 3b-5 (Lane A2) runs once 3b-2, 3b-3 and 3a-9b-1 have landed, in parallel with 3b-4.
- From 3b-6 on, the sequence is serial: 3b-6, 3b-7, 3b-8, 3b-9, 3b-10. Three of those PRs
  queue on `verify.py`.

## 3. The child protocol

**Direction and content.**

- **Parent to child:** the input as one open, read-only descriptor; the package root, a code
  path; and the protocol version. Nothing else crosses. The rules path, rule values, the
  target's path and the environment never reach the child (the canary test, 3b-5).
- **Child to parent:** facts only, on a dedicated descriptor. Every value is a non-negative
  integer or a closed enum's name. No document bytes, decoded text, names or messages cross
  in 3b. The parent never prints the child's stderr tail; it records only a closed failure
  outcome.
- **From 4a**, decoded text must reach the parent for matching, since matching stays in the
  parent and the child never holds secrets. That adds an `evidence` message under its own
  review (Q4). Until then, a 3b parent rejects an `evidence` message as a protocol error.

**Messages.** One JSON object per line, in this order. Each type is closed; unknown keys,
missing keys, duplicates and reordering are all protocol errors.

1. `hello`: `{"v": 1, "size": int, "sha256": hex}`, giving the protocol version and the size
   and SHA-256 of the bytes the child read from the descriptor.
2. `unit` (one per tiling unit; nested units follow their parent):
   - `{"kind", "span": [start, end], "within": unit index | null, "object", "generation",
     "revision"}`;
   - gaps (`WHITESPACE`, `UNINDEXED`) as `{"gap": kind, "span": [...]}`;
   - SYNTHESIZED units as `{"synth": [body, revision]}`.
3. `flag`, as `ledger.Flag` already serializes: the reason, an optional span, named integers.
   It is attached to a unit index, or to the file.
4. `label`: per stream body, its labels across revisions (`Inventory.body_labels`), with each
   STRUCTURAL record's kind.
5. `result`: per (unit, label), the registry's status, its reason and its witness.
6. `end`, the completion sentinel. It must come last and carries:
   - the count of every message type above;
   - the `Budget`'s counters;
   - whether the budget was exhausted.

**Validation, in the parent:**

- every count in `end` equals what arrived;
- `size` and `sha256` equal the parent's own;
- top-level unit and gap spans tile `[0, size)` exactly (`check_tiling`), and every
  `WHITESPACE` gap's bytes are PDF whitespace;
- nested spans tile their declared decoded lengths;
- units number at most `Limits.max_units`, and flags at most `max_flags_per_file` plus one
  `FLAGS_TRUNCATED`;
- every referenced unit index exists;
- the stream fits `sandbox.Limits.output_bytes` (64 MiB; 500,000 units at about 60 bytes each
  is about 30 MB);
- `sandbox.Result.ok` holds: exit 0, no kill, not truncated, the private directory removed.

**Failure semantics.** Any protocol error, a kill (wall clock, memory or stray), a reader
failure, a non-zero exit, truncated output, or a missing or short sentinel makes
`CHILD_COMPLETE` `FAILED`, with a closed outcome reason. Every obligation the child would
have discharged is then `FAILED` with the reason `NOT_REPORTED` (REDESIGN §4: "a missing,
truncated or malformed stream, or a non-zero child exit, makes every unreported obligation
`FAILED`").

**No partial credit.** If validation fails anywhere, the parent keeps none of the report. It
does not trust a prefix, even where the prefix's own messages look valid. In the shadow
verdict and, from 3b-8, in the enforced one, the run cannot exit 0.

**Timeout and limits.** The primary limit is the work budget (ADR 0010 item 3): exhaustion is
a `BUDGET_EXHAUSTED` flag inside a complete report, not a failure. The wall clock, CPU time
and memory are `sandbox.Limits` backstops (Q3); hitting one is a failure.

**Concurrency and cost.** The parent starts the child as soon as the target exists, before the
rules load, and joins it before the verdict. With `--fail-fast` and a confirmed leak, the
parent kills the child, since worst-of is already 1.

## 4. The verdict computation

**Obligations** (REDESIGN §2; types from 3b-1), all starting `UNEXAMINED`:

- one per tiling unit the child reports, and one per SYNTHESIZED unit;
- one per page view, per reading (Text, OCR), anchored from qpdf's page count;
- one per tool (qpdf, exiftool, OCR) and per rules scope;
- one per `Check`: `CHILD_COMPLETE`, `FILE_IDENTITY`, `TILING`, `PAGE_COUNT`, `OBJECT_SET`,
  `INVENTORY_FILE_FLAGS`, `PARSER_AGREEMENT` (3c's) and `RAW_MATCH` (Q8).

Each obligation has its own source of status:

- **Views:** `UNEXAMINED` (`NOT_IN_PHASE`) until the views run behind the ledger (4a). No
  adapter over the legacy layers (REDESIGN §6).
- **Tool and rules-scope obligations:** from the parent's own facts, which legacy also uses.
- **`PAGE_COUNT` and `OBJECT_SET`:** the anchors against the child's page list and current
  revision's units.

**Unit status.** Each unit's status is the worst status of every decoder result for it. A unit
with no result is `UNEXAMINED` (`NO_DECODER`). The parent then applies these rules, in order:

1. **Flags.** A unit carrying any inventory flag is `FLAGGED`, and so is anything a crypt flag
   attaches to (`DECRYPTION_MISMATCH`, `DECRYPTION_UNVERIFIED`). File-level flags make
   `INVENTORY_FILE_FLAGS` `FLAGGED`.
2. **Witnesses (Principle 2, ADR 0008).** A `DECODED` result counts only if its witness
   balances: its consumed spans tile the stage's length exactly. Otherwise the result is
   `FLAGGED`. A `NOT_APPLICABLE` result counts only with one of ADR 0003's four `NAReason`s,
   and only on the kind that reason names. Glyph-count balancing is reserved in the type and
   enforced from 4a.
3. **ORPHANED is `FLAGGED` (R3, ADR 0007, decision D by walk).** A stream body whose
   `body_labels` are `ORPHANED` is `FLAGGED` whatever its decoders report. A test pins that a
   `DECODED` orphan yields `FLAGGED`.
4. **STRUCTURAL's whole-parse condition (ADR 0011 item 5).** A STRUCTURAL record from the
   inventory is a candidate. It holds only if that kind's registry decoder reports a witness
   covering the whole decoded stream.
   - If it does not, the label drops. A body with no other label becomes `ORPHANED`, and rule
     3 applies. This is the test 3a-7e promised.
   - A kind with no decoder keeps its listed status: the hint stream is `UNREADABLE`, and XMP,
     JavaScript, output intents and embedded files are `UNEXAMINED`.
5. **STRUCTURAL and DRAWABLE together means drawable.** Every label's decoder runs, and the
   worst status wins. No label discharges a unit by itself. In 3b the drawable side has no
   decoder, so such a body stays `UNEXAMINED`. A test pins that the XMP or structural result
   alone never discharges it.
6. **Hint streams stay flagged until their decoder lands.** The registry maps the hint stream
   to `UNREADABLE`, so it is `FLAGGED` (ADR 0003, R6 (a); owner decision 2026-10-15: the spike
   runs in parallel). Clearing it takes a decoder that reads every bit and checks it against
   the layout (RESULTS, option (a)). That means a new registry entry, the pinned test flipped,
   and an ADR 0003 amendment for a new `NOT_APPLICABLE` reason, or a `DECODED` witness, at
   that list's bar.

**Exits.**

- **Shadow verdict:** over every obligation. Its exit is 1 if a confirmed match exists (none in
  3b: no evidence crosses), else 2 if any obligation is undischarged, else 0. Before 4a every
  file has `UNEXAMINED` objects, so the shadow exit is 2 everywhere. A shadow 0 before 4a is a
  bug, and §6 gates on it.
- **Enforced verdict:** the same function over the enforced set only: in 3b, the integrity
  checks of Q1.
- **Shipped exit:** `worst_of(legacy, enforced)` from 3b-8; legacy alone before that.

**Determinism and provenance.** The ledger is sorted, and the verdict is a pure function of
(anchors, report). The undischarged groups carry integer provenance only.

## 5. Shadow output

`--json` gains, additively:

```json
"experimental": {
  "shadow": {
    "version": 1,
    "status": "complete | child_failed | not_run",
    "outcome": "ok | <closed failure reason>",
    "exit_code": 2,
    "enforced_exit_code": 0,
    "obligations": {"total": 0, "discharged": 0},
    "undischarged": [{"kind": "...", "status": "...", "reason": "...", "count": 0}],
    "labels": {"drawable": 0, "structural": {"<kind>": 0}, "orphaned": 0, "synthesized": 0}
  }
}
```

- The shipped `exit_code` and `verdict` are computed as today: legacy alone in 3b-7, and
  worst-of with the enforced exit from 3b-8. The shadow exit never enters either.
- Every field is always present: `shadow` is `null`, with a status, on early error exits
  (target not found, rules invalid, a password).
- There are no timings, file names or text. That keeps the output byte-identical across runs
  (Principle 6) and secret-free.
- `JSON_SCHEMA_VERSION` stays 2. README marks `experimental` as outside the stability
  promise.
- The scorecard reads `experimental.shadow.exit_code` into a `shadow_exit` column (`metrics`,
  the corpus aggregate). It is outside the normalised key (`keys.py`), with a test that a
  change confined to `experimental` makes no diff. The differential's "the scorecard diff is
  identical" therefore holds for shipped output by construction, and the test proves it.
- The human report is unchanged in 3b-7. `--explain` (3b-9) is the only human view.

## 6. Measurement gates

All of these are measured on the SHA-pinned corpus (2,073 files, 489 text-bearing), the case
library and the fuzz set, and recorded in RESULTS.md "Phase 3b".

1. **Child and anchors (3b-6, before any wiring):**
   - 0 child failures and 0 timeouts on all three sets, on Linux and macOS;
   - every report validates;
   - on every inventory-unflagged file, `TILING`, `FILE_IDENTITY`, `PAGE_COUNT` and
     `OBJECT_SET` hold. On flagged files, mismatches are counted by reason, not gated;
   - the case-library run uses the scorecard's per-file timeout; the corpus run uses the 3a
     gate's 60 s, reported against Q3's limit.
2. **The ledger reproduces 3a-12 (3b-6).**
   - **Class counts:** over the unflagged corpus files, the undischarged groups must give
     exactly RESULTS.md's per-class file counts:

     | Class | Files |
     | --- | --- |
     | Orphans per body | 242 |
     | Hint streams | 400 |
     | XMP images | 516 |
     | JavaScript | 86 |
     | Synthesized | 130 |
     | Output intents | 0 |
     | Any of them | 707 |
     | Any of them, with the inventory's flagged files | 860 |

   - **The net review rate:** the per-class counts joined with today's exit-2 files must give
     3a-12's rates:

     | Population | Today's qpdf exit 3 | qpdf exit 3 removed |
     | --- | --- | --- |
     | All files | 49.2% | 41.3% |
     | All files, with the inventory's flags | 52.2% | 46.0% |
     | Text-bearing | 80.6% | 47.6% |
     | Text-bearing, with the inventory's flags | 82.2% | 56.2% |

   - **The 1,571-distinct-contents view** must also match: text-bearing 25.9%, 49.5% and
     60.0%.
   - Any deviation is root-caused before merge. A deviation in the ledger is a bug; one in
     the spike is documented. This is an independent check of the verdict's label mapping
     against 3a-12's spike, which computed the same classes another way.
3. **The shadow exit against today's exit (3b-6, and re-run at 3b-7 and 3b-10):**
   - a table of shadow against shipped exits per file;
   - **gate: shadow exit 0 on 0 files** (pre-4a);
   - exit-1 files show shadow 2 (no evidence crosses yet): reported, not gated, since the
     shadow is not shipped until Phase 6.
4. **Shipped output identical (3b-7, 3b-8 and 3b-9):**
   - the scorecard diff, Linux every-PR and macOS+OCR on the branch (#147), shows 0 unlisted
     and 0 new accepted entries;
   - the corpus run's shipped exits are unchanged file by file;
   - at 3b-8, child failures on all three sets are still 0, so worst-of changes nothing there;
   - **runtime** is the shipped CLI's wall time with the hook, against without, per case
     (p50, p95, max) and on the corpus. The gate is REDESIGN §9's p95 ≤ 2x. Anything above
     +25% p95 goes to the coordinator before merge.

## 7. Open questions

Each question below has options and a recommendation. "Coordinator" means a coordinator
decision with the owner informed. "Owner" means it changes what can exit 2 and goes to the
owner.

**Q1 (owner, before 3b-8): what does the enforced verdict hold in 3b?**

- (a) Only the child-integrity checks (`CHILD_COMPLETE`, `FILE_IDENTITY`, `TILING`). A run
  whose child fails or lies about the file's bytes exits 2; nothing else changes. This
  matches the row's gate ("a truncated or empty child report exits 2"). Expected cost: 0
  files, if §6 gate 1 holds.
- (b) Nothing. The child stays pure shadow until 3d, and the row's gate is met only in the
  shadow verdict.
- (c) Also the qpdf anchors (`PAGE_COUNT`, `OBJECT_SET`). These are environment-dependent:
  without qpdf the scorecard drops `TOOL_MISSING` and recomputes the exit, so an enforced
  anchor would make a cross-platform diff. They also overlap the inventory's 153 flagged
  files, which stay shadow-only.

*Recommendation: (a).* The anchors join with 3c, when parser agreement is enforced.

**Q2 (coordinator, before 3b-7): when does the child run?**

- (a) Always, concurrently with the legacy scan, so 3b-7 measures the cost 3b-8 will ship.
- (b) Only with `--json` or `--explain` until 3b-8.

*Recommendation: (a).*

**Q3 (coordinator, before 3b-6): the child's backstop limits.**

- (a) `sandbox.Limits`' defaults: 300 s wall clock, 600 s CPU, 4 GiB, 64 MiB of output.
- (b) The 3a gate's 60 s.

*Recommendation: (a) until 3d.* 3b-6 reports the corpus maximum against both. The work
budget is the real limit, and a tighter wall clock is 3d's call, with its watchdog.

**Q4 (coordinator, now): does decoded text cross the pipe in 3b?**

- (a) No. 3b has no decoder that yields evidence. The `evidence` message is specified when
  4a needs it, under its own review, and a 3b parent rejects one.
- (b) Define it now with no producer, which leaves a path untested until 4a.

*Recommendation: (a).*

**Q5 (coordinator, before 3b-9): `--explain` in 3b or 4a?**

- (a) In 3b, as REDESIGN's row lists it: grouped counts with integer provenance.
- (b) Defer to 4a, when enforced kinds make it useful to users.

*Recommendation: (a).* It is the only human view of the shadow ledger, and the row names
it.

**Q6 (coordinator, before 3b-1 and 3b-8): names.**

- The closed reasons for `UNEXAMINED` (`NO_DECODER`, `NOT_IN_PHASE`), for `UNREADABLE` (the
  hint stream's payload, ADR 0003), for `FAILED` (`NOT_REPORTED`) and for the child's failure
  outcomes (`killed_wall_clock`, `killed_memory`, `stray`, `reader_failed`, `exit_nonzero`,
  `truncated`, `protocol:<closed code>`).
- The shipped warning 3b-8 emits when the enforced verdict raises the exit. Recommended:
  `LEDGER_INCOMPLETE`, layer "Ledger", message "the independent structure check did not
  complete — NOT certified", with no detail beyond the closed outcome.

*Recommendation: as listed.*

**Q7 (coordinator, now): start before 3a closes?** DEPMAP puts 3b after 3a-14.

- (a) Start 3b-1 to 3b-4 now. They are new pure files that touch no inventory file except
  `tiling.py`'s one-line re-export, after 3a-9b. 3b-5 waits for 3a-9b-1, and 3b-6 for 3a-14's
  final gate.
- (b) Wait for 3a-14.

*Recommendation: (a).* It takes about 2-3 days off the critical path.

**Q8 (owner, before 3b-1): which phase builds the ledger's raw matcher?**

ADR 0003's governing rule needs every unit's bytes raw-matched at every filter-chain stage,
whatever the unit's status. Legacy's Binary layer does it today; the ledger has no matcher of
its own yet. The options:

- (a) A `RAW_MATCH` check obligation, `UNEXAMINED` in 3b, discharged by a parent-side raw
  matcher over the file's stored bytes in 4a and over every decoded stage in 4d. The parent
  holds both the secrets and the file's bytes.
- (b) A raw-match obligation per unit, discharged by the same matcher.
- (c) Build the stored-bytes matcher in 3b.

*Recommendation: (a) now.* Nothing can be certified without it, and (b) is the shape the 4a
plan may choose. (c) adds matcher work to a phase with no evidence yet.

**Q9 (coordinator): ownership of shared files.** The guard edits in 3b-2 to 3b-4 and 3b-5
fall in Lane A2's `test_import_boundaries.py`. Recommendation: Lane A writes each entry in its
own PR, and Lane A2 reviews it, rather than serializing through A2.

## 8. PR count and estimate

- **Count:** 10 PRs plus this plan. RESULTS.md estimated 7-9, before two splits:
  - the child entry moves to Lane A2 (3b-5);
  - the hook (3b-7) is split from enforcement (3b-8), so each `verify.py` PR has one problem
    and the #147 rule judges each alone.
- **Split:** Lane A has 9 PRs and Lane A2 has 1.
- **Pace basis:** Lane A merged five 3a-7 PRs on 2026-10-15, with reviews at 2-3 rounds each.
  Each `verify.py` PR adds a macOS+OCR scorecard run (about 35-60 minutes) and waits its turn
  in the `verify.py` queue: #167 and #160 are open today.

**Estimate by stage:**

| Stage | Estimate | Note |
| --- | --- | --- |
| 3b-1 to 3b-4, pure | 2-3 days | 3b-1 first; 3b-2 and 3b-3 in parallel; 3b-4 last |
| 3b-5, child (Lane A2) | 1-2 days after 3a-9b-1 | |
| 3b-6, anchors, runner and corpus gate | 1-2 days | Includes root-causing any 3a-12 deviation |
| 3b-7 to 3b-9, `verify.py` | About 1 day each once at the queue's head | 3 PRs |
| 3b-10, close-out | Half a day | |

**Total:** about 8-11 working days elapsed, if no owner decision blocks.

**Critical path:** 3b-1, 3b-2, 3b-4, 3b-6, 3b-7, 3b-8, 3b-9, 3b-10. 3b-5 joins from 3a-9b-1.
The owner's Q1 and Q8 are needed before 3b-8 and 3b-1 respectively. 3d, and through it 4b,
starts from 3b-6's runner.
