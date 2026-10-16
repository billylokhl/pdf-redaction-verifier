# Phase 3b (Ledger + verdict) plan

This is the PR sequence for REDESIGN §7's row 3b, as [phase3a-plan.md](phase3a-plan.md) was
for 3a.

- **History:** drafted 2026-10-15 (Pacific); revised the same day after review round 1 of
  #179 and the owner's and coordinator's decisions (§7).
- **Pending:** one question (Q11, the matcher's design) is open for the owner, and Q12 for the
  coordinator.
- **Verdict changes:** nothing here changes a verdict.

Read with:

- **REDESIGN:** §2 (obligations, parent anchors); §3 (principles 1, 2, 4, 5, 6, 8 and 11);
  §4 (process boundary, core types, decoder registry); §6 ("Shadow mode", as amended for 3b
  on 2026-10-15); §7a.
- **ADRs:**
  - ADR 0001, with its 3a-9a-1 note: `Decryption.views`;
  - ADR 0003, including its governing rule and the deferred free-entry bytes;
  - ADR 0006's amendment: `Limits.max_units` 500,000;
  - ADR 0007's note;
  - ADR 0008;
  - ADR 0010, item 5;
  - ADR 0011, item 5, and R1-R7.
- **RESULTS.md:** "Phase 3a-7e" and "Phase 3a-12".

**Lanes, stated here because the registry is local-only:**

- **Lane A owns the core:** `verdict.py`, `decoders/`, `rawmatch.py`, the inventory's build
  and types, `eval/scorecard/inventory.py`, and the `verify.py` hook.
- **Lane A2 owns:**
  - `crypt.py`;
  - `sandbox.py`;
  - the child entry;
  - the bomb cases;
  - the edits to `tests/test_import_boundaries.py`.
- **Lane F (Phase 5) shares files:** `verify.py`, `model.py` and `report/*`.
- **The `verify.py` queue:** one open PR at a time on `verify.py` (REDESIGN §7a). Today's open
  one is #160, Phase 5's "PR 3b", which waits on owner sign-offs. #167 has merged.

## Ground rules for every 3b PR

- **Shipped exit unchanged, except in 3b-11.** Only 3b-11 may change a shipped exit, and only
  to exit 2 on a run whose child-integrity checks fail (Q1, REDESIGN §6 as amended). Any such
  change needs at least a minor version bump (Principle 11).
  - Every case in the case library and every corpus file keeps its exit.
  - The scorecard stays at its accepted list, with 0 unlisted and 0 crashed.
  - CHANGELOG reads "Verdict changes: none", except in 3b-11.
- **The #147 rule.** These PRs touch `verify.py`, `report/*` or `model.py`: 3b-1 (re-export
  lines only), 3b-10, 3b-11 and 3b-12. Each needs a passing macOS+OCR scorecard run on its
  branch before merge (`gh workflow run scorecard-weekly.yml --ref <branch>`; about 58-62
  minutes), recorded in the PR body. The Linux every-PR differential must also pass.
- **The parent holds all the rules.**
  - It holds the rules file and its values.
  - It holds every rule that turns statuses into an exit: the ORPHANED mapping, STRUCTURAL's
    whole-parse condition, the witness checks, the enforced set, worst-of and matching.
  - The child reports facts and never decides an obligation's final status.
  - No rules value reaches the child (ADR 0010 item 5; the canary test).
- **What crosses the pipe.**
  - **Structural messages:** only integers and closed enum names. Spans and object numbers are
    document-derived integers.
  - **Stage frames:** under Q11's recommended design, decoded document bytes, for matching, as
    stage frames (§3). Never secrets: nothing flows from parent to child but the descriptor and
    code paths.
- **Code standards.**
  - New modules are mypy strict, Python 3.10 floor.
  - `verdict.py` is at 100% branch coverage from its first PR.
  - Every closed enum is pinned by a test.
  - Every new `FlagReason` has an EMITTERS entry.
- **Import-guard entries.** Lane A writes them in its own PRs, and Lane A2 reviews them (Q9).
  `protocol.py` is child-side and may import `json` and `struct` beyond the allowlist.
  `child.py` is an entry point, shaped like `inventory/__main__.py`'s exemption:
  - it may import `os`, `sys`, `hashlib`, `select` and, through `crypt.py`, `pikepdf` and
    `pymupdf`;
  - nothing may import it;
  - every other check applies.

  The parent-side modules (`verdict`, `anchors`, `shadow`, `rawmatch`) never import the
  inventory, `crypt`, `pikepdf`, `decoders` or `child`.
- **Process.**
  - One problem per PR; about 600 changed code lines triggers a split decision.
  - Docs stay in sync: README, DESIGN, COVERAGE, eval/README, REDESIGN and CHANGELOG.
  - Each PR gets an independent adversarial review (Opus: all of this is on the exit-0 audit
    surface), ending with the Review table.

## 1. Scope

REDESIGN's row 3b covers:

- obligations and parent anchors;
- the child protocol and its completion sentinel;
- witnesses;
- the single verdict function, with shadow and enforced verdicts;
- worst-of shipping;
- `--explain`.

It also fixes two verdict rules:

- every stream labelled `ORPHANED` (per body, `Inventory.body_labels`) is `FLAGGED` whatever its
  decoder reports, with a test where a `DECODED` orphan still yields `FLAGGED`;
- a stream both `STRUCTURAL` and `DRAWABLE` is drawable.

Beyond the row:

- **The minimal child.** ADR 0010 item 5 moved it here. REDESIGN §4 (#176) runs `crypt.py` in
  it.
- **The matcher.** The owner's re-decision of Q8 (2026-10-15) adds the ledger's own raw matcher
  over the spans earlier plans assign to 3b:
  - whole object spans (phase3a-plan, 3a-3: "3b must match the whole object span's bytes");
  - both forms of a language-tagged text string (3a-2/3a-3: "for 3b to match both");
  - dead object streams' bytes (3a-5: "3b/Phase 4's to scan as raw bytes");
  - owner decision 7's claimed comment bytes;
  - xref-stream free-entry bytes (ADR 0003, "Deferred");
  - `Decryption.views` (§4).

The row's gate, as amended:

- the shipped exit is identical to the reference on every case;
- the shadow verdict is reported;
- a truncated or empty child report exits `2`. That holds in the enforced verdict, from 3b-11.

**Not 3b:**

- **3c, parser agreement.** 3b creates the `PARSER_AGREEMENT` obligation, `UNEXAMINED`; 3c
  discharges it. The qpdf anchors join the enforced set no earlier than 3c.
- **3d, the sandbox.** The OS sandbox, watchdog tuning and the bomb and hang gate belong to
  3d. 3b uses `sandbox.py` with `os_sandbox=False`, extended in 3b-5.
- **4a** (owner decision Q10, 2026-10-15; REDESIGN's 4a row):
  - the views move behind the ledger and into the child;
  - the string/name/key token decoder of the registry;
  - content streams, fonts and synthesized appearances;
  - the consumption witness, enforced (ADR 0008).

  Until then, page-view obligations and dictionary tokens stay `UNEXAMINED`. The raw matcher
  covers their bytes, but a raw match is not a decode.
- **4b-4d.** Images, decision D's "used" and the strips-under-a-box question are 4b's (owner
  decision 2026-10-15: they must be caught; Lane G measures). Containers are 4c's, and the
  general filter chain and `RESIDUE` are 4d's. No unit kind is enforced in 3b.
- **The hint-table decoder.** RESULTS.md "Owner choice", option (a), is staged: its spike runs
  in parallel, and hint streams stay flagged meanwhile (owner, 2026-10-15). Its six PRs are
  outside this count, and its registry hookup lands with its decoder in 3c (§4, rule 6).
- **Enforcing 3a-12's classes.** The 707 label-class files and the inventory's 153 flagged files
  stay shadow-only until each kind's decoder phase, and all of them until Phase 6.

## 2. The PR sequence

Thirteen PRs, plus this plan: Lane A has 11, Lane A2 has 2. "Verdict path" means the PR
touches code the shipped CLI runs.

**3b-1. Obligations, witnesses and labels: the types** (Lane A)

- **Deliverable:** additions to `ledger.py`, append-only.
  - **Reference types:**
    - `SynthesizedRef(body, revision)`. It is not a `UnitKind`, since it claims no bytes.
    - `ViewRef(page, reading)`, where the reading is TEXT or OCR.
    - `ToolRef`, with a closed `Tool`.
    - `RuleScopeRef`.
    - `StageRef(unit, stage)`, with a closed `Stage`: `STORED`, `XREF_FIELDS`, `OBJSTM_PAYLOAD`,
      `STRING_TAGGED`, `STRING_UNTAGGED`, `READER_VIEW`.
    - `CheckRef`, with a closed `Check`: `CHILD_COMPLETE`, `FILE_IDENTITY`, `TILING`,
      `PAGE_COUNT`, `OBJECT_SET`, `INVENTORY_FILE_FLAGS`, `PARSER_AGREEMENT` and `RAW_MATCH`.
  - **Records:**
    - `Obligation(id, status, reason, witness)`.
    - `Witness`: byte-consumption spans in a stage's decoded coordinates, and the stage length
      they must tile. Glyph-count fields are reserved for 4a.
    - `DecodeResult`, without evidence or children.
    - `Finding(rule index, tier, stage ref)`: no text, since the parent masks at report time.
  - **Enums:**
    - `Label` and `StructuralKind`;
    - `PendingReason` (`NO_DECODER`, `NOT_IN_PHASE`);
    - the closed `UNREADABLE` reasons (`HINT_PAYLOAD`);
    - the closed `FAILED` reasons (`NOT_REPORTED`).
  - `check_tiling` and the PDF-whitespace predicate move here from `inventory/tiling.py`, which
    re-exports them. The parent then re-runs both without importing the inventory package. The
    file is Lane A's and outside Lane A2's 9b claim.
  - `model.py` re-exports the new names.
- **Files:**
  - `redaction_verifier/ledger.py`, append-only; Lane A2's #178 appends `DECRYPTION_*`, so this
    PR rebases on it;
  - `inventory/tiling.py`, the re-export;
  - `model.py`, re-export lines;
  - `tests/test_ledger_types.py`;
  - the guard entries.
- **Depends on:** main; it starts now (Q7).
- **Tests:**
  - closed enums pinned, frozen records;
  - everything that crosses the pipe is plain integers or enum members;
  - a `Witness` balances only when its spans tile `[0, length)`;
  - an empty span is invalid, which settles 3a-1's note: it is a protocol error;
  - `check_tiling` is unchanged behind the re-export.
- **Gate:** the suite, mypy strict, the guard, and #147 (for `model.py`).
- **Verdict path:** re-export lines only.

**3b-2. The protocol** (Lane A)

- **Deliverable:** `redaction_verifier/protocol.py`, child-side and pure: the length-prefixed
  frame format, the message schema of §3, the child's encoder, and the parent's strict validator.
  - The validator is incremental: it validates frames as they arrive and spools stage bytes.
  - It returns a `ChildReport`, or a closed `ProtocolError` that makes `CHILD_COMPLETE`
    `FAILED`.
  - If Q11 goes to option (i), the stage frames are dropped from this PR.
- **Files:** `protocol.py`, `tests/test_protocol.py`, and the guard entry (`json`, `struct`).
- **Depends on:** 3b-1.
- **Tests (Hypothesis):**
  - round-trip;
  - no proper prefix validates, cut at any byte;
  - a single-byte mutation is rejected, or still yields counts and a hash that match what
    arrived;
  - duplicates, reordering, unknown types, frames after `end`, and counts past their bound are
    rejected;
  - a non-integer where an integer belongs is rejected, and so is a name outside its enum;
  - a region list that does not tile is rejected;
  - a stage frame larger than its declared length is rejected.
- **Gate:** the suite and mypy strict.
- **Verdict path:** none.

**3b-3. The registry and the structural decoders** (Lane A)

- **Deliverable:** `decoders/__init__.py`, the registry, and `decoders/structural.py`. They run
  child-side.
  - **Cross-reference and object streams:** each stream's payload result is `DECODED`, with a
    witness, when the inventory read it whole and unflagged.
    - The xref field data is `NOT_APPLICABLE`, reason 1.
    - The object-stream header table is `NOT_APPLICABLE`, reason 2.
    - The members are their own units.
  - **What a stream's OBJECT unit ends with:** the payload's result, worst-of'd with its
    dictionary's. A dictionary's result is `UNEXAMINED` (`NO_DECODER`) until 4a's token decoder.
    So in 3b, **an xref-stream or object-stream OBJECT unit ends `UNEXAMINED` (`NO_DECODER`)**,
    with its payload discharged.
  - **`HEADER` and `XREF_EPILOGUE`:** `DECODED` by the inventory's canonical parse, with a
    witness over the span.
  - **`XREF_TABLE`:** its entries are `DECODED`, its trailer dictionary `UNEXAMINED`.
  - **Hint streams:** no decoder entry; listed status `UNREADABLE` (`HINT_PAYLOAD`, ADR 0003,
    R6 (a)).
  - **XMP, JavaScript, output intents, embedded files, every DRAWABLE kind and SYNTHESIZED:**
    no entry, so `UNEXAMINED` (`NO_DECODER`).
- **Files:** `decoders/`, `tests/test_decoders_structural.py`, and the guard entries. It reads
  `inventory/types.py` and `labels.py` and edits no inventory file.
- **Depends on:** 3b-1.
- **Tests:**
  - every entry is pinned;
  - **the hint stream has no entry and is `UNREADABLE`.** The hint-table hookup must flip this
    pinned test explicitly;
  - a structural decoder whose witness does not cover its whole stream says so; 3b-4 then drops
    the label;
  - work is charged to the `Budget`.
- **Gate:** the suite and mypy strict.
- **Verdict path:** none.

**3b-4. The verdict function** (Lane A)

- **Deliverable:** `verdict.py`, parent-side and pure.
  - `build_ledger(anchors, report | failure, findings) -> Ledger`.
  - `verdict(ledger, enforced) -> Verdict`, giving the shadow exit, the enforced exit, and the
    undischarged obligations grouped by (kind, status, reason) with counts and sample provenance.
  - `worst_of`.
  - `Limits` is validated at construction (3a-1's note), since the parent uses its values as
    protocol bounds.
- **Files:** `verdict.py`, `budget.py` (validation), `tests/test_verdict.py`, and the guard
  entry.
- **Depends on:** 3b-1, 3b-2 and 3b-3.
- **Tests:**
  - every rule of §4;
  - properties:
    - the shadow exit is never 0 while anything is undischarged;
    - worsening any obligation never lowers either exit;
    - replacing a valid report with any failure never lowers either exit;
    - `worst_of` is commutative, associative and monotone;
  - an exhaustive `match` on `Status`;
  - 100% branch coverage.
- **Gate:** the suite, mypy strict, and the coverage target.
- **Verdict path:** none.

**3b-5. The sandbox's lifetime and descriptor API** (Lane A2)

- **Deliverable:** `sandbox.py` grows from a blocking `run()` into a handle:
  - **`start()` returns a `Child`:**
    - `cancel()`;
    - `wait(timeout)`;
    - its watchdog (wall clock, memory footprint) runs in its own thread, not the caller's;
    - an incremental reader of a dedicated protocol descriptor, with a byte cap, feeding 3b-2's
      validator;
    - stdout stays diagnostics, with its existing cap.
  - **An `input_fd` variant:** the child gets the parent's already-open descriptor. Both sides
    read with `os.pread` and never `read`/`seek`, because they share one open file
    description and its offset.
  - **Parent death:** a lifeline pipe whose write end only the parent holds. The child's
    preamble watches its read end and `_exit`s on EOF. Beside it:
    - on Linux, `PR_SET_PDEATHSIG` (`SIGKILL`);
    - on macOS, a `kqueue` `EVFILT_PROC`/`NOTE_EXIT` watch on the parent's pid.
  - **What the lifeline covers:**
    - the parent killed by `SIGKILL` (the scorecard runner's timeout kill);
    - the parent's process group killed (`bombrun`'s), which `start_new_session=True` keeps
      from reaching the child directly;
    - Ctrl-C, which the parent's handler turns into `cancel()`.
  - **Priority:** the child runs at a lower priority (`os.nice`, +10) (blocker 4, §6 gate 5).
  - **Package lookup:** the preamble prepends the package root the parent passes (a code path,
    never a rules path), so `python -I` finds the package from a checkout.
- **Files:** `sandbox.py`, `tests/test_sandbox.py`.
- **Depends on:** nothing from 3b; it can start now.
- **Tests:**
  - the child dies when the parent is `SIGKILL`ed, on Linux and macOS, with no process outliving
    it;
  - cancel kills the group;
  - Ctrl-C;
  - the descriptor's offset is never moved;
  - the reader cap gives a closed `truncated` outcome;
  - no rlimit or watchdog regression.
- **Gate:** the suite on both platforms; the bomb harness still green.
- **Verdict path:** none (unwired).

**3b-6. String and stage extraction** (Lane A, child-side)

- **Deliverable:** `decoders/stages.py`. For each unit it yields the decoded stages the raw
  matcher needs, each as (unit, `Stage`, bytes):
  - `XREF_FIELDS`: each cross-reference stream's decoded field data, free entries included;
  - `OBJSTM_PAYLOAD`: each object stream's decoded payload, so members are matched over their
    whole span;
  - `STRING_TAGGED` and `STRING_UNTAGGED`: each text string's decoding with its language tags
    kept and with them stripped (`strings.py` already returns both), in UTF-8;
  - `READER_VIEW`: each `Decryption.views` entry, as a stream of that object (Lane A2's
    9b obligation).

  Each stage is charged to the `Budget`. Each is capped by `Limits.max_stage_bytes` (Q12); past
  the cap, a `STAGE_TRUNCATED` flag is raised, and that stage's `RAW_MATCH` is `FLAGGED`.
- **Files:** `decoders/stages.py`, `ledger.py` (`STAGE_TRUNCATED`, EMITTERS),
  `tests/test_stages.py`, and the guard entry.
- **Depends on:** 3b-3. Its `READER_VIEW` part depends on 9b-1, and lands as a follow-up if 9b-1
  is later.
- **Tests:** a planted value reaches a stage in each of these shapes:
  - a free entry;
  - an object-stream member;
  - a hex string in UTF-16BE;
  - a language-tagged string, both forms;
  - a views entry.

  Also: the cap gives a flag, not a failure.
- **Gate:** the suite and mypy strict.
- **Verdict path:** none.

**3b-7. The child entry** (Lane A2)

- **Deliverable:** `redaction_verifier/child.py`, run by `sandbox.start` under `python -I`. In
  order:
  1. `pread`s the descriptor, hashing as it goes;
  2. runs `crypt.decrypt` and `build_inventory` with the decryptions, per 3a-9b-1's interface;
  3. runs the registry and the stage extraction;
  4. charges each reported unit and region to `max_units`. Tiling units are not charged today;
     past the cap the child reports `BUDGET_EXHAUSTED`, still in a complete report;
  5. writes the protocol to its dedicated descriptor. fd 1 points at `/dev/null` before
     `pymupdf` or `pikepdf` load.
- **Files:** `child.py`, `tests/test_child.py`, and the guard (entry-point exemption).
- **Depends on:** 3b-2, 3b-3, 3b-5, 3b-6 and 3a-9b-1; never a stub interface.
- **Tests:**
  - ADR 0010 item 5's canary: a unique rules value reaches none of the child's argv,
    environment, descriptors or working directory;
  - every case-library file and encrypted fixture validates;
  - library output on fd 1 cannot corrupt the protocol;
  - an installed package and a checkout both work;
  - a raising child sends no sentinel.
- **Gate:** a case-library sweep on Linux and macOS: every report validates, with 0 failures and
  0 timeouts.
- **Verdict path:** none (unwired).

**3b-8. The parent's raw matcher** (Lane A)

- **Deliverable:** `redaction_verifier/rawmatch.py`, parent-side. Q11's recommended design is
  option (iii), §3. Today's raw matcher (`RollingScanner` and `PatternScanner` over
  `normalize_string(bytes.decode("latin-1"))`, the legacy raw layer's exact calls and tiers,
  with ADR 0012's informational policy as legacy applies it) runs over:
  - `STORED`: every byte `[0, size)` of the parent's own descriptor, `pread` in chunks. That
    covers whole object spans with comments inside them, claimed comment lines, dead
    object-stream bytes, slack and unindexed ranges. A secret across a chunk edge is found;
  - each stage the child sent.

  The STORED pass itself needs no child report. A hit is attributed to the region containing it
  when the report is valid, and to the file otherwise. The output is ledger `Finding`s and the
  `RAW_MATCH` obligations' statuses.
- **Files:** `rawmatch.py`, `tests/test_rawmatch.py`, and the guard (parent-side: never imported
  by the child).
- **Depends on:** 3b-2 (stage frames) and 3b-4.
- **Tests:** a value planted in each of these is found:
  - a whole object span;
  - an object comment;
  - a claimed comment line;
  - a dead object stream's stored bytes;
  - a free entry;
  - an object-stream member;
  - both language-tag forms;
  - a views entry;
  - a chunk edge.

  Further:
  - findings carry no text;
  - a stage that never arrived leaves its `RAW_MATCH` `UNEXAMINED`;
  - a truncated stage is `FLAGGED`.
- **Gate:** the suite and mypy strict, plus a case-library table of the shadow matcher against
  today's raw layers (§6 gate 4).
- **Verdict path:** none.

**3b-9. Parent anchors, the shadow runner, and the measurement gate** (Lane A)

- **Deliverable:**
  - **`anchors.py`:**
    - the file size and SHA-256 by `pread` on the descriptor the child will get;
    - qpdf in its own process (`--show-npages`, `--show-xref`), with an absolute path, `--` and
      a minimal environment;
    - the tool and rules-scope facts.
  - **`shadow.py`:**
    - starts the child (3b-5, 3b-7);
    - feeds the validator;
    - runs the STORED matcher pass and then the stage passes;
    - checks the anchors and builds the verdict (3b-4);
    - never raises.
  - **`python -m redaction_verifier.shadow FILE --rules R`:** counts only.
  - **`eval/scorecard/shadow.py`:** the corpus, case-library and fuzz harness of §6.
- **Files:** `anchors.py`, `shadow.py`, `eval/scorecard/shadow.py`, tests, a "Phase 3b" section
  in RESULTS.md, and eval/README.
- **Depends on:** 3b-4, 3b-7, 3b-8, and 3a-14's final gate.
- **Tests:** each of these yields `FAILED`:
  - a kill: wall clock, memory or stray;
  - a cancel;
  - a truncated, empty or garbled report;
  - a hash mismatch;
  - a region tiling mismatch;
  - a `WHITESPACE` region with another byte;
  - qpdf missing or failing (its `CheckRef`).
- **Gate:** §6 gates 1-4.
- **Verdict path:** none.

**3b-10. The CLI hook, shadow only** (Lane A)

- **Deliverable:**
  - **The hook:** `verify.py` starts the shadow run when the target exists, before the rules
    load (Q2), and does its own CPU work only after legacy has finished (§3, "Concurrency").
  - **JSON:** `--json` gains `experimental.shadow` (§5), `undischarged` included: this PR owns
    that field. `JSON_SCHEMA_VERSION` stays 2.
  - **Scorecard:** a `shadow_exit` column in `metrics` and the corpus aggregate, outside the
    key.
  - **Import guard:** `test_cli_never_loads_the_inventory_or_pikepdf` changes. The CLI now
    loads `sandbox`, `budget`, `ledger`, `protocol`, `verdict`, `anchors`, `shadow` and
    `rawmatch`, and must still never load the inventory, `crypt`, `pikepdf`, `decoders` or
    `child`.
- **Files:**
  - `verify.py` (the hook);
  - `report/json.py` (an additive keyword);
  - `eval/scorecard/metrics.py`, `corpus.py`, and a test in `keys.py`'s suite;
  - `tests/test_json.py`;
  - `tests/test_import_boundaries.py` (Lane A2's file, Q9).
- **Depends on:** 3b-9, and the `verify.py` queue (behind #160).
- **Tests:**
  - every byte outside `experimental` is identical with and without the hook, on every
    case-library file;
  - `experimental.shadow` is present on every path;
  - deterministic JSON.
- **Gate:** §6 gate 5; the bomb cases re-judged (H2: the CLI now spawns a child, and no bomb
  may hang it or leave a process behind).
- **Verdict path:** yes, with the exit identical.

**3b-11. Worst-of: enforce the child-integrity checks** (Lane A)

- **Deliverable:**
  - **Enforced set:** `CHILD_COMPLETE`, `FILE_IDENTITY`, `TILING` (Q1; REDESIGN §6 as amended).
  - **Shipped exit:** `worst_of(legacy, enforced)`.
  - **New warning:** `LEDGER_INCOMPLETE` (Q6), in layer "Ledger", whenever the enforced exit
    raises the shipped one, so the report stays self-consistent for the scorecard.
  - **Version:** at least a minor version bump.
- **Files:**
  - `verify.py`;
  - `model.py` (`WARNING_CODES`: `LEDGER_INCOMPLETE` as coverage; `LAYERS`: "Ledger");
  - `report/text.py` and `report/json.py` (the layer's rendering);
  - `tests/test_shadow_enforced.py`;
  - `CHANGELOG` (Verdict changes) and the version.
- **Depends on:** 3b-10 and §6 gate 5 holding.
- **Tests:**
  - a truncated, empty or garbled report exits 2 with the warning, and so do a killed child and
    a hash mismatch;
  - legacy 1 stays 1;
  - legacy 2 is unchanged;
  - a cancelled child under `--fail-fast` after a confirmed leak stays 1.
- **Gate:**
  - the scorecard diff is identical on both platforms;
  - 0 child failures on the corpus, the case library and fuzz;
  - the bomb pins (#168) are re-judged, each moving pin stricter only and named.
- **Verdict path:** yes, stricter only.

**3b-12. `--explain`** (Lane A)

- **Deliverable:** `--explain` prints the enforced verdict's undischarged obligations, then the
  shadow's under their own heading.
  - **Order:** by status severity (`FAILED`, `UNREADABLE`, `FLAGGED`, `UNEXAMINED`), then count
    descending, then kind and reason.
  - **Cap:** at most 50 groups each, then "N more groups", with at most 5 provenance samples
    per group (lowest revision, object, span).
  - **Content:** integers and closed names only, never text.
  - **JSON:** it reads the `undischarged` field 3b-10 added and adds none.
- **Files:** `verify.py` (the flag), `report/text.py`, `tests/test_explain.py`.
- **Depends on:** 3b-11 and the `verify.py` queue.
- **Tests:**
  - deterministic;
  - canary values absent;
  - every reason renders;
  - the exit is identical with and without the flag.
- **Gate:** #147.
- **Verdict path:** yes, with the exit identical.

**3b-13. Close-out** (Lane A)

- **Deliverable:**
  - the §6 measurements re-run on main;
  - the REDESIGN 3b row;
  - an ADR 0010 note on item 5 as built: `python -I <file>` through `sandbox.py` rather than
    `-m`, and a parent-side memory watchdog rather than a memory rlimit (macOS ignores it);
  - an ADR 0011 item-5 note on the verdict's mapping as built;
  - CHANGELOG.
- **Files:** docs only.
- **Depends on:** 3b-12.

**Order:**

1. **Now, in parallel:** 3b-1 and 3b-5.
2. **After 3b-1:** 3b-2 and 3b-3 together.
3. **After both:** 3b-4. 3b-6 follows 3b-3.
4. **The child:** 3b-7 joins 3b-2, 3b-3, 3b-5, 3b-6 and 9b-1.
5. **The matcher:** 3b-8 follows 3b-4.
6. **Serial from here:** 3b-9 (also needing 3a-14), then 3b-10, 3b-11, 3b-12 and 3b-13.

## 3. The child protocol

**Directions.**

- **Parent to child:**
  - the input as one read-only descriptor, read with `pread` only;
  - the protocol descriptor's write end;
  - the lifeline's read end;
  - the package root (a code path);
  - the protocol version.

  Never the rules path, rule values, the target's path or an environment. The canary test
  checks this.
- **Child to parent:** on the protocol descriptor, structural frames and stage frames. The
  parent never prints the child's stderr or stdout. It records only a closed outcome.

**Framing.** Each frame is a 4-byte big-endian length and then a payload.

- **Structural frames:** JSON payloads, one object each, of closed types.
- **Stage frames:** a JSON header (unit index, `Stage`, length), and then a binary frame of
  exactly that length.

The order is strict. An unknown key, a missing key, a duplicate or a reordering is a protocol
error.

1. **`hello`:** `{"v": 1, "size", "sha256"}`, about the bytes the child read.
2. **`unit`:** one per unit.
   - `{"i", "kind", "object", "generation", "revision", "within", "claim": [start, end]}`.
   - `within` is a container unit's index, or null.
   - The `claim` is in the container's coordinates: raw for `STREAM_SLACK`, decoded for
     object-stream members and headers.
   - SYNTHESIZED units are `{"i", "synth": [body, revision]}`.
3. **`decoded`:** `{"unit", "length"}`, the decoded length of each container whose nested
   units are tiled (each object stream).
4. **`region`:** the tiling as `inventory/types.py` holds it.
   - `{"container": unit | null, "span", "kind", "owners": [unit indices], "claimants"}`.
   - `kind` is a `UnitKind`, `CONTESTED`, `WHITESPACE` or `UNINDEXED`.
   - A contested region lists at most `max_contested_owners` owners and its true claimant
     count, so one unit may own several regions.
   - Top-level regions tile `[0, size)`; each container's regions tile `[0, decoded length)`.
5. **`flag`:** as `ledger.Flag` serializes, attached to a unit index or to the file.
6. **`page`:** the current revision's canonical page list, `{"index", "object",
   "generation"}`, for `PAGE_COUNT`.
7. **`xref`:** the current revision's in-use entries.
   - `{"object", "generation", "type": "offset" | "compressed", "unit"}`, where `unit` is
     the OBJECT or OBJSTM_MEMBER unit it lands on. Free entries are not units and are not
     listed.
   - This is for `OBJECT_SET`, against `qpdf --show-xref`.
8. **`label`:** per stream body, its labels across revisions, each STRUCTURAL record with its
   kind.
9. **`result`:** per (unit, label), the registry's status, reason and witness.
10. **`stage`:** stage frames, under Q11 (iii).
11. **`end`:** the completion sentinel, which must come last. It carries:
    - the count of each message type;
    - the stage bytes sent;
    - the `Budget` counters, and whether the budget is exhausted.

**What the parent validates:**

- every count in `end` equals what arrived;
- `size` and `sha256` equal its own `pread` of the same descriptor;
- top-level regions tile `[0, size)` (`check_tiling`), and every `WHITESPACE` region's bytes
  are PDF whitespace (the predicate moved to `ledger.py`);
- each container's regions tile its decoded length;
- every owner index is a declared unit;
- an owned region lies within its owner's claim;
- every `xref` entry names a declared unit of matching number and generation;
- units and regions are at most `max_units` (the child charges both);
- flags are at most `max_flags_per_file` plus one `FLAGS_TRUNCATED`;
- stage bytes are at most `max_stage_bytes` (Q12);
- structural frames are at most 128 MiB;
- `Child` ended ok: exit 0, not killed, not cancelled, the private directory removed.

On sizes: the corpus's largest file has 32,976 tiling units, and about 44,000 with views and
drawable streams. At roughly 150 bytes per unit or region frame, that is about 10-15 MB. At
the full 500,000 units the structural frames could pass 128 MiB. **Past any cap the run is
`FAILED`, explicitly.** From 3b-11 that exits 2, so §6 measures the corpus maximum against
each cap.

**Failure semantics.** Any of these makes `CHILD_COMPLETE` `FAILED`, with a closed outcome:

- a protocol error;
- a kill (`killed_wall_clock`, `killed_memory`, `stray`);
- `cancelled` (Ctrl-C, or `--fail-fast` after a confirmed leak);
- `reader_failed`;
- `exit_nonzero`;
- `truncated`;
- a missing sentinel.

Every obligation the child would have discharged is then `FAILED` (`NOT_REPORTED`). There is no
partial credit: a valid-looking prefix is discarded whole. The STORED raw pass still runs, since
it needs only the parent's bytes. A confirmed match from it makes the shadow verdict exit 1. The
enforced verdict never takes an exit from raw matches.

**Limits.**

- The work budget is primary: exhaustion is a flag inside a complete report.
- The wall clock, CPU and memory backstops are `sandbox.Limits`' defaults until 3d (Q3).
- A backstop hit is a failure.

**Concurrency** (Q2 decided "always, concurrently"; blocker 4's fix keeps it).

- **The child:** it starts once the target exists and runs at `nice` +10.
- **The parent during legacy:** its reader thread only copies frames into its private `0700`
  directory (Principle 5), bounded by the caps. It does no validation or matching, holds the
  GIL for no CPU-heavy work, and so adds nothing to legacy's deadline-bound steps.
  - Those steps are the 120 s subprocess timeout (`SUBPROCESS_TIMEOUT_S`) and the 20 s qpdf
    check deadline (`QPDF_CHECK_DEADLINE_S`).
- **After legacy finishes:** the parent validates, matches and computes the verdict.
- **The residual risk:** a niced child starved under the scorecard's one-worker-per-core load
  could reach its wall clock, or push the CLI past the scorecard's 60 s per-file timeout. §6
  gate 5 measures this directly. If it fails, the remaining options go to the owner as a new
  question:
  - a capped child pool;
  - running the child after legacy instead of beside it;
  - a scorecard worker-count change.

**Identity.** `FILE_IDENTITY` binds the parent's descriptor to the child's read only. Legacy's
layers open the path separately, which this check does not bind; legacy is frozen.

## 4. The verdict computation

**Obligations,** all starting `UNEXAMINED`:

- each reported unit and SYNTHESIZED unit;
- each page view per reading, anchored from qpdf's page count. They stay `NOT_IN_PHASE` until
  4a (Q10);
- each tool and rules scope;
- each `Check`;
- a `RAW_MATCH` per `StageRef`: STORED for the file, plus each stage the child names in its
  registry results. A stage named but never sent stays `UNEXAMINED`.

**Unit status:** the worst result per unit, `UNEXAMINED` (`NO_DECODER`) with none. The parent
then applies, in order:

1. **Flags.** Any inventory or crypt flag on a unit makes it `FLAGGED`. File-level flags make
   `INVENTORY_FILE_FLAGS` `FLAGGED`.
2. **Witnesses (Principle 2, ADR 0008).**
   - `DECODED` counts only when the witness's spans tile the stage length; otherwise
     `FLAGGED`.
   - `NOT_APPLICABLE` counts only with ADR 0003's reason for that kind.
   - Glyph counts are enforced from 4a.
3. **ORPHANED is `FLAGGED`** (R3, ADR 0007, decision D by walk), whatever its decoders
   report. A test pins that a `DECODED` orphan is `FLAGGED`.
4. **STRUCTURAL's whole-parse condition** (ADR 0011 item 5). A record is a candidate.
   - **Kinds with a registry decoder** (cross-reference and object streams): the label holds
     only if the witness covers the whole decoded stream. Otherwise it drops; a body with no
     other label becomes `ORPHANED`, and rule 3 applies. This is 3a-7e's promised test.
   - **Kinds with no decoder** keep the label and their listed status:
     - **the hint stream:** STRUCTURAL, `UNREADABLE`, so flagged, and never ORPHANED (R6 (a),
       staged);
     - **XMP, JavaScript, output intents and embedded files:** STRUCTURAL, `UNEXAMINED`.
5. **STRUCTURAL and DRAWABLE together means drawable.** Every label's decoder runs, the worst
   wins, and no label discharges alone. In 3b the drawable side has no decoder, so such a
   body stays `UNEXAMINED`; a test pins that a structural result alone never discharges it.
6. **Hint streams stay flagged until their decoder lands.** Clearing one takes:
   - the hint-table decoder: every bit read, checked against the layout;
   - its registry entry, with the pinned test flipped;
   - an ADR 0003 amendment at that list's bar.

**Matching.**

- **Where findings come from:** the parent's raw matcher (§2, 3b-8).
- **What a finding does:** a hard-tier finding makes the shadow exit 1; a review-tier one leaves
  that `RAW_MATCH` `FLAGGED`.
- **What discharges a `RAW_MATCH`:** being scanned with no finding.
- **Decoded content is not covered yet.** Decoded text from content streams and dictionaries
  is 4a's evidence. Until then, a raw match is the only source of exit 1 in the shadow verdict.

**Exits.**

- **Shadow:** 1 on a confirmed hard finding; else 2 if anything is undischarged; else 0.
  Before 4a every file has `UNEXAMINED` dictionaries and views, so a shadow 0 before 4a is a
  bug, and §6 gates on it.
- **Enforced:** the same function over Q1's set.
- **Shipped:** legacy alone until 3b-11; from then, `worst_of(legacy, enforced)`.

**Determinism.** The ledger is sorted, and the verdict is pure in (anchors, report, findings).

## 5. Shadow output

`--json` gains, additively (3b-10):

```json
"experimental": {
  "shadow": {
    "version": 1,
    "status": "complete | child_failed | not_run",
    "outcome": "ok | <closed outcome> | null",
    "exit_code": 2,
    "enforced_exit_code": 0,
    "obligations": {"total": 0, "discharged": 0},
    "findings": {"hard": 0, "review": 0},
    "undischarged": [{"kind": "...", "status": "...", "reason": "...", "count": 0}],
    "labels": {"drawable": 0, "structural": {"<kind>": 0}, "orphaned": 0, "synthesized": 0}
  }
}
```

- **Shipped fields are untouched.** `exit_code` and `verdict` are the shipped ones; the shadow
  exit never enters them.
- **Every field is always present.** On early error exits (target not found, rules invalid, a
  password) `shadow` is an object with `"status": "not_run"` and every other field null.
- **No timings, file names, text or rule names.** That keeps the output byte-identical across
  runs and secret-free. Findings are counts here; masked findings appear only if a later phase
  ships them.
- **Stability.** `JSON_SCHEMA_VERSION` stays 2, and README marks `experimental` outside the
  stability promise.
- **The scorecard.** Its `shadow_exit` column is outside the normalised key. A test pins that a
  change confined to `experimental` makes no diff.

## 6. Measurement gates

All gates run over the SHA-pinned corpus (2,073 files, 489 text-bearing), the case library and
fuzz. Results go in RESULTS.md, "Phase 3b".

**1. The enforced checks hold on every file** (3b-9).

- On all three sets, on Linux and macOS: 0 child failures and 0 timeouts.
- `CHILD_COMPLETE`, `FILE_IDENTITY` and `TILING` hold on **every** file, flagged or not.
  Contested files' regions tile like any other. These are exactly what 3b-11 enforces.
- The qpdf anchors (`PAGE_COUNT`, `OBJECT_SET`) must hold on every inventory-unflagged file;
  mismatches on flagged files are counted by reason, not gated.
- The maxima of structural bytes, stage bytes, units, regions and child wall time are
  reported against each cap.

**2. The ledger reproduces 3a-12** (3b-9).

- **Pinned inputs:**
  - the corpus manifest;
  - 3a-12's per-file record of today's exit, measured at b8aa29c and kept in its local
    per-file output, keyed by SHA-256. If that output was not kept, `unit_budget.py` is re-run
    at b8aa29c. Today's CLI is not re-run, because #167 has since changed the exit-2 set;
  - 3a-12's per-file XMP image bodies (`xmp_flag_bodies`).
- **9b has no effect:** no corpus file is encrypted, so 9b's decryption changes no corpus
  label. The gate checks that: 0 files whose labels differ between the inventory at 3c49bce
  and the run's commit.
- **The mapping from ledger groups to 3a-12's classes,** by manifest entry:

  | 3a-12 class | Ledger group |
  | --- | --- |
  | Orphans | Files with a body made `FLAGGED` by rule 3 |
  | Hint streams | Files with a STRUCTURAL hint record (`UNREADABLE`) |
  | XMP images | 3a-12's image bodies, each of which must lie in the ledger's STRUCTURAL-XMP `UNEXAMINED` group |
  | JavaScript | STRUCTURAL-JavaScript records |
  | Synthesized | Files with a `SynthesizedRef` |
  | Output intents | STRUCTURAL output-intent records |
  | Inventory flags | Any `FLAGGED` from rule 1 |

  The ledger cannot tell an image-holding XMP packet from another until there is an XMP
  decoder. So 3a-12's XMP class is joined in: its image bodies must be a subset of the ledger's
  XMP group, and that group must equal the inventory's STRUCTURAL XMP records.
- **"Reproduces" means equal file sets, not just equal counts:**
  - over the 1,920 unflagged files: orphans 242, hint streams 400, XMP images 516, JavaScript
    86, synthesized 130, output intents 0, any of them 707;
  - over all 2,073 files: with the inventory's flags, 860.
- **The rates, from the pinned exits:**

  | Population | Classes | Today's qpdf exit 3 | qpdf exit 3 removed |
  | --- | --- | --- | --- |
  | All files | 707 | 49.2% | 41.3% |
  | All files | 860 | 52.2% | 46.0% |
  | Text-bearing | 707 | 80.6% | 47.6% |
  | Text-bearing | 860 | 82.2% | 56.2% |

  Per distinct content, text-bearing: 25.9%, 49.5% and 60.0%. The same rates are also
  reported with current main's exits, as information.
- Any difference is root-caused before merge: a ledger bug is fixed, a spike error documented.

**3. Shadow against today** (3b-9; re-run at 3b-10 and 3b-13).

- A per-file table of the shadow exit against the shipped one.
- **Gate:**
  - shadow exit 0 on 0 files;
  - shadow exit 1 on 0 corpus files under 3a-12's fabricated value rule (the corpus is clean).
- Case-library exit-1 cases that the shadow leaves at 2 are listed: they are matches outside
  the raw stages, which 4a gains. Reported, not gated.

**4. The matcher against legacy's raw layers** (3b-8, 3b-9).

- Every case-library case in which today's raw layers (Binary, Objects) report a hard finding
  in raw storage also gets a hard finding from the shadow matcher.
- Any case where they differ is root-caused.
- Planted values in every span §1 lists are found (3b-8's tests).

**5. Shipped output identical, under load** (3b-10, 3b-11, 3b-12).

- The scorecard diff shows 0 unlisted and 0 new accepted entries:
  - on Linux, on every PR;
  - on macOS+OCR, on the branch (#147).
- **Repeat runs under load:** three runs of the full case-library scorecard at CI's default
  parallelism, plus one corpus run, with:
  - 0 exit drift;
  - 0 child failures;
  - 0 scorecard timeouts.
- **Runtime:** the added wall clock per case and on the corpus (p50, p95, max), reported to the
  owner (Q2):
  - p95 must stay within REDESIGN §9's 2x;
  - an added p95 above 25% goes to the coordinator.

## 7. Decisions and open questions

**Decided** (recorded 2026-10-15, Pacific):

- **Q1, decided by the owner on 2026-10-15 and confirmed as an explicit amendment to REDESIGN
  6.** In 3b the enforced verdict counts the child-integrity checks only (`CHILD_COMPLETE`,
  `FILE_IDENTITY`, `TILING`). The qpdf anchors and every other ledger finding are recorded,
  not enforced.
  - This departs from 6's "tool, view, rules-scope and cross-check obligations plus units of
    enforced kinds". 6 now says so, dated.
  - Legacy keeps enforcing its own tool and scope warnings.
- **Q2, decided by the coordinator on 2026-10-15:** the child always runs, concurrently with
  the legacy scan. 3b-10 reports the added wall clock on the corpus (p50, p95). Blocker 4's
  measures, in §3 "Concurrency", keep this decision.
- **Q3, decided by the coordinator on 2026-10-15:** `sandbox.Limits`' defaults until 3d.
- **Q4, decided by the coordinator on 2026-10-15:** no decoded text crosses the pipe in 3b.
  - **Q11's recommendation would change this decision**, and it is put to the owner there.
  - If the owner takes (iii), decoded stage bytes cross for matching only.
  - 4a's evidence channel still gets its own review.
- **Q5, decided by the coordinator on 2026-10-15:** `--explain` ships in 3b.
- **Q6, decided by the coordinator on 2026-10-15:** the new warning is `LEDGER_INCOMPLETE`.
  The closed reasons are as named in 3b-1 and §3.
- **Q7, decided by the coordinator on 2026-10-15:** 3b-1 to 3b-4 start now, without waiting for
  3a-14.
- **Q8, decided by the owner on 2026-10-15, then re-decided the same day to (b).** The ledger's
  raw matcher is built in 3b.
  - **First decision:** a file-level `RAW_MATCH`, `UNEXAMINED` in 3b. That premise was false:
    earlier plans give 3b this matching (phase3a-plan, 3a-2/3a-3, 3a-5, and owner decision 7;
    ADR 0003's deferred free-entry bytes).
  - **Re-decision:** build the matcher in 3b, covering those spans (3b-6, 3b-8).
  - **`Decryption.views`:** discharged in 3b as the `READER_VIEW` stage, since the design
    covers it naturally. If Q11 goes to (i), it falls back to 4a.
- **Q9, decided by the coordinator on 2026-10-15:** Lane A writes the guard entries, and Lane A2
  reviews them.
- **Q10, decided by the owner on 2026-10-15:** the views move behind the ledger and into the
  child in 4a, and the string/name token decoder is built in 4a. REDESIGN's 4a row says so.
- **Lane A2's obligation, recorded 2026-10-15** (ADR 0001's 3a-9a-1 note): the matcher scans
  each `Decryption.views` entry as a stream of its object, with that object's storage class.
  - That entry is the catalog's `/Metadata` under `/EncryptMetadata false` today.
  - The backstop is not live until something consumes `views`.
  - The coordinator has asked Lane A2 for 3b-5's `input_fd` and protocol-descriptor API.

**Q11 (owner): where the raw matcher runs.** It needs the secrets, which only the parent holds.
No rules value may reach the child (ADR 0010), and the parent must not parse the hostile file
(§4).

- **(i) The parent reads and decodes.** The child sends only spans and closed transforms, as
  integers. The parent `pread`s its own descriptor and runs today's matcher:
  - the stored bytes need nothing more: this is the legacy raw search's trust level;
  - the decoded stages make the parent re-decode in process, on the child's spans. That means
    capped Flate for xref-stream and object-stream payloads (`inventory/flate.py`), and string
    unescaping with UTF-16/PDFDocEncoding and tag stripping (`inventory/strings.py`).
  - **Cost:** the parent then inflates and decodes hostile bytes. The code is pure Python plus
    zlib, budget-charged, with no MuPDF. But it departs from 4's "the child touches the PDF",
    the CLI would load two inventory modules, and the import guard changes.
  - **Protocol:** no stage frames, and Q4 stands.
- **(ii) The child matches against salted digests** of normalized secrets.
  - **Leakage:** SSNs and phone numbers are low-entropy. 10^9 candidates brute-force a salted
    digest in seconds, so a compromised child recovers every such secret.
  - **No OS sandbox until 3d:** nothing stops a compromised child from exfiltrating what it
    recovers.
  - **Coverage:** rolling matching means one digest per window length per byte, which is
    feasible. But pattern rules and built-in classes are regular expressions and cannot be
    digested, and review-tier logic (separators, joined lines) cannot run blind.
  - **ADR 0010:** it fails the canary in spirit, since a function of every rules value reaches
    the child.
  - **Rejected.**
- **(iii) Split by stage: the parent scans stored bytes, and the child sends decoded stages.**
  - **STORED:** the parent scans all `[0, size)` of its own descriptor, needing nothing from
    the child. It covers whole object spans, comments, dead object streams' stored bytes,
    slack and unindexed ranges, at legacy's trust level.
  - **Decoded stages:** the child, which already decodes them, sends their bytes as
    length-prefixed stage frames. Those are xref field data with free entries, object-stream
    payloads, both forms of each text string, and `Decryption.views`. The parent only matches.
  - **Under §4,** document bytes flow child to parent only. That direction is §4's design: the
    child "streams obligations and evidence back", and matching stays in the parent. Secrets
    still never reach the child, and the parent still parses nothing. Stages are capped (Q12),
    and a stage past its cap is flagged, not failed.
  - **What it forces:**
    - a protocol change: binary-safe framing and stage frames, already written into §3 and
      3b-2;
    - Q4 reversed for stage bytes;
    - sandbox work: an incremental reader of the protocol descriptor and spooling to the
      parent's private directory (3b-5);
    - **no ADR change.** ADR 0010's canary is untouched, and §4 already allows evidence back.
      3b-13's ADR 0010 note records it.

*Recommendation: (iii).* It keeps every decode in the child and every secret in the parent. It
needs no new trust in the parent beyond legacy's raw read, and it discharges `Decryption.views`
in 3b. (i) is the fallback if the owner prefers no document-derived bytes on the pipe before
4a. Under (i), the stage frames leave 3b-2, 3b-6 shrinks to transforms, 3b-8 imports the two
decoders parent-side, `READER_VIEW` moves to 4a, and the estimate is unchanged.

**Q12 (coordinator, before 3b-6): `Limits.max_stage_bytes` and the 128 MiB structural cap.**
*Recommendation:* set both from 3b-9's corpus maxima with 10x headroom, as `max_units` was set.
Until then, 256 MiB for stage bytes, spooled to disk, not held in memory.

**Possible new question:** if §6 gate 5 shows exit drift or timeouts under load, the
concurrency options of §3 go to the owner as a new question. Q2's "always, concurrently" is not
changed here.

## 8. PR count and estimate

**Count:** 13 PRs, plus this plan; a 14th if review splits the scorecard column out of 3b-10.

- Lane A has 11: 3b-1 to 3b-13 except 3b-5 and 3b-7. Lane A2 has 2: 3b-5 and 3b-7.
- **Up from 10 in the first draft:** the raw matcher (3b-6 stages, 3b-8 matcher, owner Q8 (b))
  and the sandbox lifetime API split out of the child entry (3b-5, blocker 3).

**Pace:**

- Lane A merged five 3a-7 PRs on 2026-10-15, with 2-3 review rounds each.
- Each `verify.py` PR (3b-10, 3b-11, 3b-12) adds a 58-62 minute macOS+OCR run and waits its
  turn behind #160, which needs owner sign-offs (35 cases).

**Estimate:** about 15-23 working days elapsed, if no decision blocks. That is the round-1
review's 12-18, plus about 3-5 for the matcher.

| Work | Estimate |
| --- | --- |
| The four pure PRs, 3b-1 to 3b-4 | 3-4 days |
| 3b-6 and 3b-8 | 3-4 days, partly parallel |
| 3b-5 (Lane A2) | 1-2 days, from now |
| 3b-7 (Lane A2) | 1-2 days, after 9b-1 |
| 3b-9, with its corpus gates and root-causing | 2-3 days |
| The three `verify.py` PRs | 1-2 days each, at the queue's head |
| Close-out | Half a day |

**Critical path:** #178 → 3a-9b-1 → 3b-7 → 3b-9 → 3b-10 → 3b-11 → 3b-12 → 3b-13.

- 3b-9 also waits on 3a-14's final gate.
- 3b-10 also waits on #160's place in the `verify.py` queue.
- Off the path: 3b-1 to 3b-4, 3b-5, 3b-6 and 3b-8.

**Owner input still needed:** Q11 before 3b-2 pins the stage frames. Q1, Q8 and Q10 are
decided.

3d, and through it 4b, starts from 3b-9's runner.
