# Phase 3b (Ledger + verdict) plan

This is the PR sequence for REDESIGN §7's row 3b, as [phase3a-plan.md](phase3a-plan.md) was
for 3a.

- **History:** drafted 2026-10-15 (Pacific); revised the same day after review rounds 1 and 2
  of #179 and the owner's and coordinator's decisions (§7).
- **Decided:** Q1-Q13 (§7). Nothing is pending.
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
  - **Stage frames:** under Q11's decided design, decoded document bytes, for matching, as
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
  - `Decryption.views` (REDESIGN §4, "Encryption"; ADR 0001's 3a-9a-1 note).

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
      `STRING_TAGGED`, `STRING_UNTAGGED`, `STRING_BINARY`, `READER_VIEW`. `unit` is a unit
      index, or `FILE` for a file-level stage. `STORED` is always `StageRef(FILE, STORED)`:
      one stage over `[0, size)`.
    - `CheckRef`, with a closed `Check`: `CHILD_COMPLETE`, `FILE_IDENTITY`, `TILING`,
      `PAGE_COUNT`, `OBJECT_SET`, `INVENTORY_FILE_FLAGS`, `PARSER_AGREEMENT` and `RAW_MATCH`.
  - **Records:**
    - `Obligation(id, status, reason, witness)`.
    - `Witness`: byte-consumption spans in a stage's decoded coordinates, and the stage length
      they must tile. Glyph-count fields are reserved for 4a.
    - `DecodeResult`, without evidence or children.
    - `Finding(rule index, tier, stage ref)`: no text, since the parent masks at report time.
      Raw-match findings are always review tier in 3b (§4, "Matching").
  - **Enums:**
    - `Label` and `StructuralKind`;
    - `PendingReason` (`NO_DECODER`, `NOT_IN_PHASE`);
    - the closed `UNREADABLE` reasons (`HINT_PAYLOAD`);
    - the closed `FAILED` reasons (`NOT_REPORTED`);
    - the `FLAGGED` reason `REVIEW_MATCH`, for a `RAW_MATCH` with a review-tier match.
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

- **Deliverable:** `redaction_verifier/protocol.py`, child-side and pure.
  - **The exact frame schema** for §3's frame types, the child's encoder, and the parent's
    strict, incremental validator. The validator checks frames as they arrive and spools stage
    bytes.
  - **Every numeric bound** of §3's B1-B5 and B3.
  - The new `Limits` fields: `max_messages`, `max_stage_bytes` and `max_stage_total`.
  - The validator returns a `ChildReport`, or a closed `ProtocolError` that makes
    `CHILD_COMPLETE` `FAILED`.
- **Review:** coordinator decision, 2026-10-15. 3b-2 is reviewed as a property test against
  the real inventory and the corpus, not against figures written in this plan.
- **Files:** `protocol.py`; `budget.py` (`max_messages`, `max_stage_bytes`,
  `max_stage_total`); `tests/test_protocol.py`; the fixtures below, under `tests/`; and the
  guard entry (`json`, `struct`).
- **Depends on:** 3b-1.
- **Acceptance criteria:**
  - **B1, verbatim, a hard criterion (and 3b-11's too):** "a file that legacy exits 0 on must
    never become FAILED from flag volume". `manyflags.py` is a fixture.
  - **B2:** `offset0.py` is a fixture. The offset-0 entry is sent with no unit, and
    `OBJECT_SET` agrees with `qpdf --show-xref`. Entries with a mismatched number or
    generation, and entries sharing an offset, are tested too.
  - **B4:** `contested.py` is a fixture, and every contested region it yields validates.
  - **`bigint.py` is a fixture.** Integers in frames go up to about 70 digits, as the
    inventory's flag parameters can, plus -1 (a trailer as a flag's source).
  - **Per-unit flags:** a nested unit's flags carry their 3 parameters.
  - **Encoding:** the compact JSON encoding (separators, key order) is pinned by a test. The
    `hello` frame carries `sha256`.
  - **Frame shapes:**
    - `label` frames are one per body and label;
    - `result` frames are defined for units with no label, and for `XREF_TABLE`'s two parts;
    - `witness` frames are one per claim for stored units (B5).
  - **Bounds:**
    - each frame type's bound and byte size is derived from the real inventory's worst cases
      and proved with worst-case values;
    - `decoded` arrives before units and claims, and `D` is bounded by `max_inflated_bytes +
      size`;
    - the fixed absolute structural cap and the parent's memory bound are derived from
      `sandbox.Limits.memory_bytes`;
    - a property test over the case library, the corpus and the fuzz set shows that no correct
      child report reaches any bound.
  - **Exhaustion:** the child counts its detail before it emits the core. A report exhausted
    at `max_messages` still validates, with its core whole and `BUDGET_EXHAUSTED` among its
    flags.
  - **Stages:**
    - truncation travels in the stage header;
    - binary strings have their own stage;
    - no match can span two strings: one frame per string, or a separator the normaliser can't
      bridge (this PR decides which);
    - the expected stages are derived from the units.
  - **Rejected input:** no proper prefix validates, cut at any byte. Duplicates, reordering,
    unknown types, frames after `end`, out-of-enum names and non-integers are rejected. A
    single-byte mutation is rejected, or yields counts and a hash that still match what
    arrived.
  - `STREAM_SLACK` is excluded from tiling validation.
- **Gate:** the suite, mypy strict, and the acceptance criteria above.
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
    ADR 0011's R6 (a)).
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
  - **Spooling:** the reader spools stage bytes to an unlinked temporary file, opened and then
    unlinked at once inside the parent's private `0700` directory. The bytes then die with the
    parent's descriptor, even on a `SIGKILL` (Principle 5).
  - **Wall clock:** the child's wall-clock limit counts from legacy's end, not the child's start,
    since a niced child may get little CPU while legacy runs. The CPU rlimit still bounds it
    from its start.
    - After legacy ends, the parent waits at most `post_legacy_wait_s` for the child. That
      defaults to 20 s, a dated refinement of Q3 (§7), and 3b-9's measurement may revise it.
    - Then it cancels the child (outcome `killed_wall_clock`).
    - **The bound:** the CLI's added time after legacy is at most `post_legacy_wait_s` plus the
      parent's own post-legacy work: the qpdf anchors (`--show-npages`, `--show-xref`, unless
      they ran during legacy), the SHA-256 of the file, validating the spool, and the matcher's
      pass. Each is linear in the file and its stages, and §6 gate 5 measures each. The CLI
      therefore stays inside the scorecard's 60 s per file whenever legacy plus that bound
      does, and gate 5 checks it.
    - **Consequence:** a case whose legacy run takes more than about 40 s can now time out
      against the scorecard's 60 s, if its child is still running when legacy ends. §6 gate 1
      reports the child's time after legacy's end, per platform and with OCR off, so such
      cases are named before the hook lands.
  - **An `input_fd` variant:** the child gets the parent's already-open descriptor. Both sides
    read with `os.pread` and never `read`/`seek`, because they share one open file
    description and its offset.
  - **Parent death:** a lifeline pipe whose write end only the parent holds. The child's
    preamble watches its read end and `_exit`s on EOF. Beside it:
    - on Linux, `PR_SET_PDEATHSIG` (`SIGKILL`). It fires when the *thread* that spawned the
      child exits, not the process. So `start()` always hands the spawn to one long-lived
      spawner thread that `sandbox` owns, whatever thread calls it. The test calls `start()`
      from a short-lived thread that then exits, and checks the child survives until the parent
      ends. The preamble also re-checks `getppid()` after setting
      it, since the parent may already be gone;
    - on macOS, a `kqueue` `EVFILT_PROC`/`NOTE_EXIT` watch on the parent's pid. After
      registering it, the preamble re-checks `getppid()`: a parent that died before the
      registration is caught there.
  - **What the lifeline covers:**
    - the parent killed by `SIGKILL` (the scorecard runner's timeout kill);
    - the parent's process group killed (`bombrun`'s), which `start_new_session=True` keeps
      from reaching the child directly;
    - Ctrl-C, which the parent's handler turns into `cancel()`, then re-raises the
      `KeyboardInterrupt` so legacy's own interrupt path runs unchanged.
  - **The private `0700` directory** is removed by the parent on every normal path. If the
    parent dies, the child removes it when the lifeline fires, before it exits, so neither
    spooled nor child-side files survive (Principle 5).
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
  - fault injection: a child that sleeps past `post_legacy_wait_s` is cancelled within the
    bound, and the CLI's added time stays within it;
  - spooled bytes leave no file behind after a parent `SIGKILL`;
  - no rlimit or watchdog regression.
- **The bomb harness:** `bombrun` runs the whole CLI under the outer caps, but the CLI's own
  child is in a session of its own. So the watchdog changes: `sandbox`'s footprint and kill
  cover the run's whole process tree (descendants by parent pid, sampled each poll), not only
  its process group. That counts the inner child against the outer memory cap and kills it with
  the CLI. A bomb test shows an inner child over the outer cap is counted and killed. (Bounding
  the inner `Limits` by the outer caps instead would need a CLI argument; not chosen.)
  - **Cost:** on macOS, finding descendants means a full process listing every poll (50 ms).
    3b-5 measures that overhead and may sample the tree less often than the footprint.
  - **Not covered:** a double-forked escapee re-parented away from the tree is not found. It
    is left to the lifeline, and to the OS sandbox in 3d.
- **Files:** also `eval/caselib/bombrun.py` if its own polling needs the same change.
- **Gate:** the suite on both platforms; the bomb harness still green.
- **Verdict path:** none (unwired).

**3b-6. String and stage extraction** (Lane A, child-side)

- **Deliverable:** `decoders/stages.py`. For each unit it yields the decoded stages the raw
  matcher needs, each as (unit, `Stage`, bytes):
  - `XREF_FIELDS`: each cross-reference stream's decoded field data, free entries included;
  - `OBJSTM_PAYLOAD`: each object stream's decoded payload, so members are matched over their
    whole span;
  - **String stages:** every string token of every object and object-stream member, taken from
    the **decrypted** values (9b-1's), not the stored bytes. They are split into separate
    stages, and kept apart so that no match can span two strings (§3):
    - `STRING_TAGGED` and `STRING_UNTAGGED`: each text string decoded with its language tags
      kept, and with them stripped (`strings.py` already returns both). They are sent as UTF-8
      and flagged as text, so the parent matches them as text, the way legacy matches decoded
      strings: decoded with UTF-8, then normalised. Not as latin-1, which would miss a non-ASCII
      value whose UTF-8 bytes latin-1 reads as other characters;
    - `STRING_BINARY`: each non-text string's bytes as written, matched as bytes;
  - `READER_VIEW`: each `Decryption.views` entry, as a stream of that object (Lane A2's
    9b obligation).

  Each stage is charged to the `Budget`, and each is capped by `Limits.max_stage_bytes` (Q12,
  added to `budget.py` by 3b-2). Past the cap, the child sends the stage cut at the cap, marks
  it truncated in the stage header (B1) and raises a `STAGE_TRUNCATED` flag. That stage's
  `RAW_MATCH` is `FLAGGED`. It is shadow-only and never a child failure. The total stage bytes
  are capped the same way (`max_stage_total`): stages past it are not sent, each is flagged
  (its header marks it), and none fails the child.
- **Files:** `decoders/stages.py`, `ledger.py` (`STAGE_TRUNCATED`, EMITTERS),
  `tests/test_stages.py`, and the guard entry.
- **Depends on:** 3b-2 (the caps) and 3b-3. Its `READER_VIEW` part depends on 9b-1, and lands
  as a follow-up if 9b-1 is later.
- **Tests:** a planted value reaches a stage in each of these shapes:
  - a free entry;
  - an object-stream member;
  - a hex string in UTF-16BE;
  - a language-tagged string, both forms;
  - a non-ASCII value in a UTF-16 string, matched as text;
  - a string in an encrypted object, from its decrypted value;
  - a views entry.

  Also: either cap gives a flag, not a failure.
- **Gate:** the suite and mypy strict.
- **Verdict path:** none.

**3b-7. The child entry** (Lane A2)

- **Deliverable:** `redaction_verifier/child.py`, run by `sandbox.start` under `python -I`. In
  order:
  1. `pread`s the descriptor, hashing as it goes;
  2. runs `crypt.decrypt` and `build_inventory` with the decryptions, per 3a-9b-1's interface;
  3. runs the registry and the stage extraction;
  4. counts its detail first, then sends the core whole, and charges detail messages to
     `Limits.max_messages` (§3, B1; 3b-2 sets the bounds). `max_units` is unchanged:
     SYNTHESIZED units are charged to it as today, tiling units are not. Past `max_messages`,
     the child drops the remaining detail and reports `BUDGET_EXHAUSTED`, still in a complete
     report;
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

- **Deliverable:** `redaction_verifier/rawmatch.py`, parent-side, under Q11's decided design,
  (iii)+M (§3, §7).
  - **The matcher:** the legacy Binary layer's own calls: `RollingScanner` over
    `normalize_string(bytes.decode("latin-1"))` for binary stages, and over the UTF-8 decode for
    the text-flagged string stages.
  - **Tiers:** legacy's. Every raw match is **review tier**, as `REVIEW_BINARY` with
    `NOISY_SOURCE` is today (`verify.py`'s Binary layer), and never hard.
  - **Rules:** exact-value rules only (`SecretMatcher`), as legacy's Binary layer runs.
    - ADR 0012's policy function, `matching.policy.informational_rules`, classifies class
      rules. The raw matcher runs none, so it does not call that function.
    - #160 flips the policy's default partway through this sequence. That changes nothing
      here, and 3b-8's tests pin rawmatch's output under both defaults.
    - Raw pattern and class matching (ADR 0003's review tier over image bytes) is 4b's.

  It runs over:
  - `STORED`: every byte `[0, size)` of the parent's own descriptor, `pread` in chunks. That
    covers whole object spans with comments inside them, claimed comment lines, dead
    object-stream bytes, slack and unindexed ranges. A secret across a chunk edge is found;
  - each stage the child sent.

  The STORED pass itself needs no child report. A hit is attributed to the region containing it
  when the report is valid, and to the file otherwise. The output is ledger `Finding`s and the
  `RAW_MATCH` obligations' statuses.
- **Files:** `rawmatch.py`, `tests/test_rawmatch.py`, and the guard (parent-side: never imported
  by the child).
- **Depends on:** 3b-2 (stage frames), 3b-4, and 3b-6 for real stages. It may land before
  3b-6 by testing against synthetic stage frames, with 3b-6's own tests adding the real ones.
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
  - findings carry no text, and every one is review tier;
  - a match leaves that stage's `RAW_MATCH` `FLAGGED` (review);
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
    - **"The target exists"** means `is_file()`, and then an `fstat` of the opened
      descriptor showing `S_ISREG`. A FIFO, a device or a file swapped in after the check gets
      no child: the run records `not_run`.
    - **Early exits:** on every early exit after the child starts (rules invalid, a password,
      an unreadable PDF, a crash), the parent cancels the child and reaps it before returning,
      so no child outlives the run.
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
  - the rules-invalid path, and each other early exit, leave no child process behind;
  - a FIFO target gets no child;
  - Ctrl-C cancels the child and re-raises;
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
  - **Documentation of the backstops (B3):** the CHANGELOG and README must say that a file too
    large for the child's backstops (wall clock, CPU, memory) now exits 2 by design, where
    legacy alone might have exited 0.
- **Files:**
  - `verify.py`;
  - `README.md`;
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
  - a cancelled child under `--fail-fast` after a confirmed leak stays 1;
  - **B1's hard criterion, verbatim:** "a file that legacy exits 0 on must never become FAILED
    from flag volume". `manyflags.py` exits 0, as legacy does.
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
  - an ADR 0010 note on item 5 as built:
    - `python -I <file>` through `sandbox.py` rather than `-m`;
    - a parent-side memory watchdog rather than a memory rlimit (macOS ignores it);
    - the matcher's split by stage, (iii)+M (Q11). From 4a, a stage's `RAW_MATCH` never
      discharges on the child's word alone;
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

**Scope of this section** (coordinator decision, 2026-10-15, owner informed). This section
states the protocol's requirements and its frame types, with enough detail to fix their
meaning. The exact frame schema and every numeric bound belong to 3b-2. That PR is reviewed as
a property test against the real inventory and the corpus, and its acceptance criteria are
listed in 3b-2.

**Framing.**

- Each frame is a 4-byte big-endian length, then a payload.
- **Structural frames:** a canonical JSON object each, of a closed type: compact separators,
  no whitespace, keys in a fixed order (pinned by 3b-2).
- **Stage frames:** a structural header, then a binary frame of exactly the declared length.
- The order is strict. An unknown key, a missing key, a duplicate or a reordering is a protocol
  error.
- **Every frame is fixed-shape.** No frame holds a list that grows with the file, its
  revisions or its units. The one list allowed is a region's owners, which the inventory caps at
  16. Anything with more items becomes one frame per item.

**Frame types, in arrival order.**

- **Core:** `hello` through `flag` and `end`. A report always contains its core, whole,
  exhausted or not.
- **Detail:** `label`, `result`, `witness` and `stage`. Detail is budgeted.

1. **`hello`:** the protocol version, and the size and SHA-256 of the bytes the child read.
2. **`decoded`:** each container's decoded length (each object stream's).
   - It comes before any unit or claim, so the parent can bound what follows on arrival
     (B3).
3. **`unit`**, one per unit, and **`claim`**, one per claimed span.
   - A unit carries its kind, object, generation, revision, and its container (or none).
   - A claim is one span the unit claimed in `tile()`; a unit may have several.
   - Claims are in the container's coordinates: raw for `STREAM_SLACK`, decoded for
     object-stream members and headers.
   - A SYNTHESIZED unit carries its (body, revision) and no claim.
4. **`region`:** the tiling as `inventory/types.py` holds it.
   - It carries its container (or the file), its span and kind (a `UnitKind`, `CONTESTED`,
     `WHITESPACE` or `UNINDEXED`), up to 16 owners and the true claimant count.
5. **`xref`:** each in-use entry of the current revision.
   - It carries the object number, generation and type, and the unit it lands on **or none**
     (B2).
6. **`page`:** each page of the current revision's canonical list, for `PAGE_COUNT`.
7. **`flag`:** each flag, attached to a unit or to the file, with its own bound per source
   (B1).
8. **`label`:** one frame per (stream body, label), from `Inventory.body_labels`.
   - The labels are per body across revisions, so one frame per body and label is all the
     verdict needs.
   - A STRUCTURAL label carries its record's kind.
9. **`result`:** one frame per (unit, label), the registry's status and reason.
   - A unit with no label gets one result with the label "none".
   - A unit with several parts gets one result per part, named by a closed `Part`. Today the
     only such unit is `XREF_TABLE`: its entries (`DECODED`) and its trailer dictionary
     (`UNEXAMINED`).
10. **`witness`:** one frame per witness span, attached to its result (B5).
11. **`stage`:** a header (unit or file, `Stage`, a text flag, the length sent, and the
    original length with a truncated flag), then the bytes (B1, Q11).
12. **`end`:** the completion sentinel, which must come last.
    - It carries the count of each frame type, the stage bytes sent, and the `Budget`'s
      counters with whether the budget is exhausted.

**What the parent validates:**

- every count in `end` equals what arrived;
- the `hello` size and SHA-256 equal its own `pread` of the same descriptor;
- top-level regions tile `[0, size)` (`check_tiling`), and every `WHITESPACE` region's bytes
  are PDF whitespace (the predicate moved to `ledger.py`);
- each container's regions tile its decoded length;
- `STREAM_SLACK` is excluded from tiling validation, since it is nested in its object's raw
  span and is not a container's tiling;
- every owner index is a declared unit;
- region containment, under B4;
- `xref` entries, under B2;
- every bound of B3, checked as frames arrive;
- `Child` ended ok: exit 0, not killed, not cancelled, the private directory removed.

**The requirements.**

**B1. Flag volume.** Every flag the enforced and shadow rules need can be sent, each source
with its own derived bound:

- per-unit flags: up to `max_flags_per_object` (64) per unit, plus `FLAGS_TRUNCATED`;
- the file's flag list: up to `max_flags_per_file`, plus `FLAGS_TRUNCATED`;
- flags the child adds itself: `STAGE_TRUNCATED`, `BUDGET_EXHAUSTED` from `max_messages`,
  and crypt's flags.

How the flags must behave:

- **Truncation can't be hidden.** A stage's truncation travels in its own stage header, so a
  capped flag list can't hide it.
- **Exhaustion is known in time.** The child counts its detail before it emits the core, so a
  `BUDGET_EXHAUSTED` caused by `max_messages` is in the core's flags.
- **Hard acceptance criterion, for 3b-2 and 3b-11:** "a file that legacy exits 0 on must never
  become FAILED from flag volume". `manyflags.py` (10,050 objects, each with an integer outside
  32 bits, on which legacy passes) is a fixture.

**B2. In-use xref entries without a body.** These are representable:

- owner decision 7's offset-0 entries (129 corpus files);
- entries whose number or generation doesn't match the body at their offset;
- several entries sharing one offset.

Each is sent with no unit, or with the unit it shares. Its number and generation are checked
against a unit only when it names one. `OBJECT_SET` compares the full in-use set, body-less
entries included, against `qpdf --show-xref`, which lists offset-0 entries too. `offset0.py`
is a fixture.

**B3. The parent's bounds.**

- **Per type, on arrival:** the parent checks each frame type's bound as frames arrive. `decoded`
  comes first, so units and claims are bounded by `size + D` before they arrive.
- **`D`:** at most `max_inflated_bytes + size`, since an unfiltered object stream is not
  charged to the inflated-bytes budget.
- **Absolute caps:** there is a fixed absolute structural byte cap, and a bound on the parent's
  memory. Both are tied to `sandbox.Limits.memory_bytes`: a child that fits in its memory
  backstop cannot produce more frames than the cap admits. 3b-2 derives the ratio from the
  inventory's memory per unit, and proves it on the corpus.
- **Disk and memory during legacy:** the parent's disk use (the spool: the structural cap plus
  `max_stage_total`) and its memory are bounded while legacy runs, not only afterwards.
- **What happens past a bound:** a child past any of these is `FAILED`. A correct child can't
  reach them.
- **Backstops by design.** A file too large for the child's backstops (wall clock, CPU,
  memory) **can** be moved to exit 2. The child is killed, and from 3b-11 that fails
  `CHILD_COMPLETE`. 3b-11's CHANGELOG and README must say so.

**B4. Contested regions.**

- A single-owner region must lie within one of its owner's claims.
- A `CONTESTED` region must intersect each listed owner's claims; it need not lie inside any
  one of them. The inventory's sweep merges adjacent contested bytes with different claimant
  sets into one region.

`contested.py` is a fixture.

**B5. Stored-unit witnesses.**

- A stored unit's witness coordinates are its claims concatenated in order, with one witness
  span per claim.
- That covers units whose claims are not one span: `HEADER` (the header line plus the
  binary-marker comment) and `XREF_EPILOGUE`.
- The witness balances when the spans tile the concatenated length.

**Content past a budget is flagged, never `FAILED`.** That covers detail past `max_messages`,
which becomes `BUDGET_EXHAUSTED`, and stages past `max_stage_bytes` or `max_stage_total`, which
are truncated and marked in their header. All of it is shadow-only.

**Stages: what the parent expects, and how strings are kept apart.**

- **Expected stages come from the units** (REDESIGN §4: the parent holds the list of expected
  obligations):
  - `XREF_FIELDS` for each cross-reference stream unit;
  - `OBJSTM_PAYLOAD` for each object-stream unit;
  - a string stage for each object and object-stream member, which may be empty;
  - `READER_VIEW` for each entry the core's `Decryption` facts announce.
- **A stage that never arrives** leaves its `RAW_MATCH` `UNEXAMINED`.
- **Binary strings have their own stage,** `STRING_BINARY`, matched as bytes. Text strings are
  matched as text.
- **No match can span two strings:** each string is its own unit of matching, and 3b-2
  decides between one frame per string and a separator the normaliser can't bridge.

**Failure semantics.** Any of these makes `CHILD_COMPLETE` `FAILED`, with a closed outcome:

- a protocol error, including any bound of B3;
- a kill (`killed_wall_clock`, `killed_memory`, `stray`);
- `cancelled` (Ctrl-C, `--fail-fast` after a confirmed leak, or an early error exit);
- `reader_failed`;
- `exit_nonzero`;
- `truncated`;
- a missing sentinel.

Every obligation the child would have discharged is then `FAILED` (`NOT_REPORTED`). There is no
partial credit: a valid-looking prefix is discarded whole. The STORED raw pass still runs, since
it needs only the parent's bytes. Its matches are review tier (§4), and neither verdict takes
an exit 1 from a raw match in 3b.

**Limits.**

- The work budget is primary: exhaustion is a flag inside a complete report.
- The wall clock, CPU and memory backstops are `sandbox.Limits`' defaults until 3d (Q3). The
  exception is the wall clock, which counts from legacy's end with `post_legacy_wait_s` (3b-5,
  Q3's refinement).
- A backstop hit is a failure, and so can move a large file to exit 2 from 3b-11 (B3).

**Concurrency** (Q2 decided "always, concurrently"; this approach approved by the coordinator,
2026-10-15).

- **The child:** it starts once the target exists and runs at `nice` +10.
- **The parent during legacy:** its reader thread only copies frames into the unlinked spool
  file in its private `0700` directory (Principle 5), bounded by the caps. It does no
  validation or matching, holds the GIL for no CPU-heavy work, and so adds nothing to legacy's
  deadline-bound steps.
  - Those steps are the 120 s subprocess timeout (`SUBPROCESS_TIMEOUT_S`) and the 20 s qpdf
    check deadline (`QPDF_CHECK_DEADLINE_S`).
- **After legacy finishes:** the parent validates, matches and computes the verdict.
- **The residual risk:** a niced child starved under the scorecard's one-worker-per-core load
  could reach `post_legacy_wait_s`, and become a child failure. The CLI's own added time is
  bounded (3b-5). §6 gate 5 measures both directly. If it fails, the options go to the owner
  (§7, "Possible new question").

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
     - **the hint stream:** STRUCTURAL, `UNREADABLE`, so flagged, and never ORPHANED (ADR
       0011's R6 (a), staged);
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
- **Tier:** every raw-match finding is **review tier**, as legacy's Binary layer makes every
  raw match (`REVIEW_BINARY`, `NOISY_SOURCE`). A raw match is never hard in 3b.
- **`RAW_MATCH`'s status:**
  - after a review-tier match, `FLAGGED`, with reason `REVIEW_MATCH`, a closed reason added
    in 3b-1;
  - after a scan with no match, discharged (`DECODED`, with a witness covering the stage);
  - over a truncated stage, `FLAGGED` (`STAGE_TRUNCATED`);
  - for a stage named but never sent, `UNEXAMINED`.
- **No exit 1 from a raw match.** In 3b nothing gives the shadow verdict an exit 1. Hard
  findings need decoded evidence, which is 4a's. There is no hard raw tier in 3b (Q13,
  decided); it is to be revisited at 4a/4d if evidence shows a need.

**Exits.**

- **Shadow:** 1 on a confirmed hard finding (none can arise in 3b); else 2 if anything is
  undischarged; else 0. Before 4a every file has `UNEXAMINED` dictionaries and views, so a
  shadow 0 before 4a is a bug, and §6 gates on it.
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

- On all three sets, on Linux and macOS: 0 child failures and 0 timeouts. The sets are the
  corpus, the case library, and the fuzz set (`scorecard inventory fuzz`, 2,000 files, seed 0).
  The bomb cases are excluded; they are judged by `bombrun`.
- A re-encrypted corpus sample: the corpus has no encrypted file, so 3b-9 re-encrypts a
  sample with pikepdf (each standard handler) and runs the child on it. That covers
  `crypt.py` in the child on real layouts, with the same 0-failure gate.
- `CHILD_COMPLETE`, `FILE_IDENTITY` and `TILING` hold on **every** file, flagged or not.
  Contested files' regions tile like any other. These are exactly what 3b-11 enforces.
- The qpdf anchors (`PAGE_COUNT`, `OBJECT_SET`) must hold on every inventory-unflagged file;
  mismatches on flagged files are counted by reason, not gated.
- The maxima of core frames, detail frames (against `max_messages`), structural bytes, stage
  bytes, units, regions and child wall time are reported against each cap.
- The child's time after legacy's end is reported per platform and with OCR off, against
  `post_legacy_wait_s`. Cases whose legacy run exceeds about 40 s are named (3b-5).

**2. The ledger reproduces 3a-12** (3b-9).

- **Pinned inputs:**
  - the corpus manifest;
  - 3a-12's per-file record of today's exit, measured at b8aa29c and kept in its local
    per-file output, keyed by SHA-256. If that output was not kept, `unit_budget.py` is re-run
    at b8aa29c. Today's CLI is not re-run, because #167 has since changed the exit-2 set;
  - 3a-12's per-file XMP image bodies (`xmp_flag_bodies`).
- **9b should have no effect on the corpus.** The inventory gate's corpus runs found 0
  encrypted files (RESULTS.md, "The corpus": "0 encrypted files"). So 9b's decryption should
  change no corpus label. The gate checks it rather than assuming it: 0 files whose labels
  differ between the inventory at 3c49bce and the run's commit, and any that differ are
  root-caused.
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
  - shadow exit 1 on 0 files: no 3b path yields one (§4).
- Case-library exit-1 cases show shadow 2; that is expected until 4a. Reported, not gated.

**4. The matcher against legacy's raw search** (3b-8, 3b-9).

- **In scope:** legacy matches whose bytes lie in the stored file or in one of 3b's stages
  (xref field data, object-stream payloads, string tokens, `Decryption.views`).
- **Gate:** for every such legacy Binary-layer review match (`REVIEW_BINARY`) on the case
  library, the shadow matcher has a review match in the same stage. Every legacy hard finding
  from the Objects layer whose value lies in a dictionary or object-stream member's string
  token also has a shadow review match in a string stage. Strings inside content streams are
  excluded: they are 4a's.
- Any difference in scope is root-caused.
- **Out of 3b's scope, and listed as 4a's:**
  - Binary-layer matches in Flate-decoded streams that 3b does not stage. Legacy scans
    `qpdf --qdf` output, which inflates every stream, content and image streams included; 3b
    stages only xref and object-stream payloads.
  - The Objects layer's fitz-decoded content.
  - Gate 4 lists them by case, and does not gate them.
- **Planted values** in every span §1 lists are found (3b-8's tests).

**5. Shipped output identical, under load** (3b-10, 3b-11, 3b-12).

- The scorecard diff shows 0 unlisted and 0 new accepted entries:
  - on Linux, on every PR;
  - on macOS+OCR, on the branch (#147).
- **Repeat runs under load:** three runs of the full case-library scorecard at CI's default
  parallelism, plus one corpus run, with:
  - 0 exit drift;
  - 0 child failures;
  - 0 scorecard timeouts;
  - the CLI's added time after legacy within 3b-5's bound (`post_legacy_wait_s` plus the
    matcher's pass), with its maximum reported.
- **Runtime:** the added wall clock per case and on the corpus (p50, p95, max), reported to the
  owner (Q2):
  - p95 must stay within REDESIGN §9's 2x;
  - an added p95 above 25% goes to the coordinator.

## 7. Decisions and open questions

**Decided** (recorded 2026-10-15, Pacific):

- **Q1, decided by the owner on 2026-10-15 and confirmed as an explicit amendment to REDESIGN
  §6.** In 3b the enforced verdict counts the child-integrity checks only (`CHILD_COMPLETE`,
  `FILE_IDENTITY`, `TILING`). The qpdf anchors and every other ledger finding are recorded,
  not enforced.
  - This departs from REDESIGN §6's "tool, view, rules-scope and cross-check obligations plus
    units of enforced kinds". REDESIGN §6 now says so, dated.
  - Legacy keeps enforcing its own tool and scope warnings.
- **Q2, decided by the coordinator on 2026-10-15:** the child always runs, concurrently with
  the legacy scan. 3b-10 reports the added wall clock on the corpus (p50, p95).
  - The approach in §3 "Concurrency" was approved by the coordinator on 2026-10-15: a child at
    nice +10, the parent only spooling during legacy, and gate 5's zero drift under load.
  - If gate 5 fails, the question goes back to the owner.
- **Q3, decided by the coordinator on 2026-10-15:** `sandbox.Limits`' defaults until 3d.
  - **Refined 2026-10-15 (#179 review rounds 2-3, coordinator):** the child's wall clock
    counts from legacy's end. The parent waits at most `post_legacy_wait_s` (20 s) and then
    cancels the child. The CPU and memory limits are unchanged (3b-5).
- **Q4, decided by the coordinator on 2026-10-15, and superseded for stage bytes by the owner's
  Q11 the same day.** Structural messages carry no document bytes or text. Decoded stage bytes
  cross for matching only (Q11). 4a's evidence channel still gets its own review.
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
  - **`Decryption.views`:** discharged in 3b as the `READER_VIEW` stage, since Q11's design
    covers it naturally.
- **Q9, decided by the coordinator on 2026-10-15:** Lane A writes the guard entries, and Lane A2
  reviews them.
- **Q10, decided by the owner on 2026-10-15:** the views move behind the ledger and into the
  child in 4a, and the string/name token decoder is built in 4a. REDESIGN's 4a row says so.
- **Lane A2's obligation, recorded 2026-10-15** (ADR 0001's 3a-9a-1 note): the matcher scans
  each `Decryption.views` entry as a stream of its object, with that object's storage class.
  - That entry is the catalog's `/Metadata` under `/EncryptMetadata false` today.
  - The backstop is not live until something consumes `views`.
  - The coordinator has asked Lane A2 for 3b-5's `input_fd` and protocol-descriptor API.

**Q11, decided by the owner on 2026-10-15: (iii)+M, split by stage, with the 4a mitigation.**
The matcher needs the secrets, which only the parent holds. No rules value may reach the child
(ADR 0010), and the parent must not parse the hostile file (REDESIGN §4). The options were:

- **(i) The parent reads and decodes.** The child sends only spans and closed transforms, as
  integers. The parent `pread`s its own descriptor and runs today's matcher on those spans.
  - **Advantage: every matched byte is derived by the parent from the file itself.** A buggy
    or compromised child can misreport spans, but it cannot hand the parent clean bytes in
    place of the file's own. Every stage is bound to the file.
  - **Cost:** the parent re-decodes hostile bytes in process: capped Flate
    (`inventory/flate.py`) and string decoding (`inventory/strings.py`). That is pure Python
    plus zlib, budget-charged, with no MuPDF. But it departs from REDESIGN §4's "the child
    touches the PDF", the CLI would load two inventory modules, and the import guard changes.
- **(ii) The child matches against salted digests.** Rejected.
  - SSN-sized secrets (10^9 candidates) brute-force in seconds, and before 3d nothing stops a
    compromised child exfiltrating them.
  - Pattern rules can't be digested.
  - A function of every rules value reaches the child, against ADR 0010's canary.
- **(iii) Split by stage.**
  - The parent scans all of `[0, size)` from its own descriptor, needing nothing from the child.
  - The child sends the decoded stages as capped, length-prefixed binary frames: xref field
    data, object-stream payloads, string tokens and `Decryption.views`.
  - The parent only matches. Document bytes flow child to parent only, which REDESIGN §4's
    design allows ("evidence back"). Secrets never reach the child, and the parent parses
    nothing.
  - **Its weakness, the trade-off against (i): nothing binds a stage to the file.** A buggy or
    compromised child could send clean stages, and so discharge those stages' `RAW_MATCH`. The
    `STORED` pass is unaffected: it reads the parent's own bytes.

**The decision.** The owner took (iii), with mitigation M:

- **3b:** keep the split by stage. It costs nothing here, because `RAW_MATCH` is shadow-only in
  3b, so no enforced verdict rests on a stage.
- **From 4a on (M):** a stage's `RAW_MATCH` never discharges on the child's word alone. It needs
  a second derivation: either the parent re-inflates the stage in a throwaway process, or it is
  cross-checked against MuPDF's own decode. The 4a plan chooses between them. REDESIGN's 4a row
  records this.
- **What it forces:**
  - binary-safe framing and stage frames (§3, 3b-2);
  - sandbox spooling (3b-5);
  - Q4 superseded for stage bytes;
  - an ADR 0010 note in 3b-13, recording (iii) and M. No ADR amendment is needed: ADR 0010's
    canary is untouched, and REDESIGN §4 already allows evidence back.
- **`Decryption.views`** is discharged in 3b as the `READER_VIEW` stage, subject to M from 4a.

**Q12, decided by the coordinator on 2026-10-15:**

- The caps are the corpus maxima with 10x headroom, measured and reported by 3b-6 (stage bytes)
  and 3b-9 (everything else).
- Until measured: `max_stage_bytes` 256 MiB, spooled to disk. `max_stage_total` starts at the
  same.
- An over-cap stage is flagged (fail-closed), never failed (§3, "Content past a budget is
  flagged").
- The detail-message budget, `Limits.max_messages`, is sized the same way (3b-9 measures it;
  1,000,000 until then). `max_units` keeps ADR 0006's meaning and headroom.
- The structural byte cap and the parent's memory bound are not corpus multiples. They are
  derived by 3b-2 from `sandbox.Limits.memory_bytes`, so a child within its memory backstop
  cannot reach them (§3, B3).

**Q13, decided by the coordinator on 2026-10-15 (owner informed): no hard raw tier in 3b.**

- Every raw match is review tier, matching legacy: the Binary layer is always review tier, and
  hard findings come from its structural pass, which the ledger gains with 4a's decoders.
- To be revisited at 4a/4d if evidence shows a need.

**Possible new question:** if §6 gate 5 shows exit drift or timeouts under load, the concurrency
options go to the owner:

- a capped child pool;
- running the child after legacy instead of beside it;
- a scorecard worker-count change.

## 8. PR count and estimate

**Count:** 13 PRs, plus this plan; a 14th if review splits the scorecard column out of 3b-10.

- Lane A has 11: 3b-1 to 3b-13, except 3b-5 and 3b-7. Lane A2 has 2: 3b-5 and 3b-7.
- **Up from 10 in the first draft:** the raw matcher (3b-6 stages, 3b-8 matcher; owner Q8 (b))
  and the sandbox lifetime API, split out of the child entry (3b-5).

**Pace:**

- Lane A merged five 3a-7 PRs on 2026-10-15, with 2-3 review rounds each.
- Each `verify.py` PR (3b-10, 3b-11, 3b-12) adds a 58-62 minute macOS+OCR run, and waits its turn
  behind #160, which needs owner sign-offs (35 cases).
- Lane A2's work is serial. It holds #178, then 9b-1, then 9b-2, then 3b-5, then 3b-7. That
  queue, not Lane A, sets the child's date.

**Estimate:** 15-23 working days elapsed, with the upper end likely. The lower end assumes A2's
queue and the `verify.py` queue both move at the 2026-10-15 pace, and that no gate needs a
second root-causing round.

The estimate is also conditional on two things outside 3b:

- **#160 clearing the `verify.py` queue.** It waits on 35 owner sign-offs, and 3b-10 cannot
  start before it.
- **3a-14's final gate,** which 3b-9 waits on.

A delay in either moves the end date day for day.

| Work | Estimate |
| --- | --- |
| 3b-1 to 3b-4, pure | 3-4 days |
| 3b-6 and 3b-8 | 3-4 days, partly parallel |
| Lane A2: 9b-1, 9b-2, 3b-5, 3b-7 in order | 4-7 days from #178's merge |
| 3b-9, with its corpus gates and root-causing | 2-3 days |
| The three `verify.py` PRs | 1-2 days each, at the queue's head |
| Close-out | Half a day |

**Critical path:** #178 → 9b-1 → 9b-2 → 3b-5 → 3b-7 → 3b-9 → 3b-10 → 3b-11 → 3b-12 → 3b-13.

- 3b-5 needs no 3b PR and could start now, but it sits in Lane A2's serial queue unless A2
  takes it ahead of 9b.
- 3b-9 also waits on 3a-14's final gate.
- 3b-10 also waits on #160's place in the `verify.py` queue.
- Off the path: 3b-1 to 3b-4, 3b-6 and 3b-8.

**Owner input still needed:** none. Q1-Q13 are decided. A new concurrency question arises only
if §6 gate 5 fails.

**REDESIGN's 3b row** is updated by 3b-13's close-out for the matcher and the protocol as
built. Until then it points here.

3d, and through it 4b, starts from 3b-9's runner.
