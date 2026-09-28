# Phase 3a (Inventory) plan

Agreed 2026-09-27 (owner decisions below). The PR sequence for REDESIGN §7's row 3a.

Ground rules for every 3a PR: shadow mode by construction — no 3a PR touches verify.py,
redaction_verifier/{report,views,matching,rules}/, legacy model types, accepted_diffs.yaml or
existing lock entries. Scorecard stays 10 accepted / 0 unlisted / 0 crashed. CHANGELOG
"Verdict changes: none". mypy strict for redaction_verifier.inventory.* and .budget. Python
3.10 floor (local _assert_never). Inventory never raises on input bytes: every anomaly is a
Flag with a closed FlagReason carrying only integers (no document bytes); iterative,
linear-time, Budget-charged parsing. Hypothesis pinned in the test extra; deterministic
`ci` profile. Isolation guard (3a-1): the CLI never imports redaction_verifier.inventory or
pikepdf; inventory never imports rules/matching/report/views/verify (secret-free child).

REVISED 2026-09-28 by ADR 0010 (owner decisions, all recommendations adopted):
- Readers are the authority. Every lenient parser branch flags, or is on a written allowlist
  with a test showing MuPDF and qpdf agree. 3a-3b audits the existing leniencies: a lone CR
  after `stream`, unknown escapes, octal overflow, '#00' in names, a
  header found after skipped whitespace/comments, text_string's replacement characters.
- 3a-3b (new, before 3a-4): tests/test_inventory_differential.py, a Hypothesis fuzzer that
  reads generated objects with our parser, MuPDF and qpdf and fails on any unflagged
  disagreement; grows to value level (object set, stream extents, string and name values)
  and becomes the 3a-6 gate below. Plus a file-wide work budget (bytes lexed incl. rescans,
  tokens; cap = k x file size -> BUDGET_EXHAUSTED) and n-vs-8n timing tests.
  Done (#39): stream framing and values (strings, names, numbers, nesting -- each reader's
  re-serialization parsed back and compared) with no unflagged disagreement in 20,000+
  examples each; the written leniency allowlist ACCEPTED_LENIENCIES, one reader-agreement
  test per entry; newly flagged because readers disagree: a lone CR after `stream`
  (STREAM_EOL) and '#00' in a name (INVALID_NAME_ESCAPE); TextString.lossless for decodes
  that replaced bytes; Limits.work_per_byte (64) / work_floor and Budget.charge_work, charged
  for every byte the parser or a scan passes over, rescans included, and refusing up front
  once spent (parse_indirect_at then returns None; parse_value_at a None value). Linearity
  is tested both ways: work counts (8n costs 8x n) and wall clock on adversarial offsets
  (duplicate offsets, offsets inside unterminated strings) -- work counting alone cannot see
  uncharged work. One ObjectParser per file (its endstream index is charged once). Deferred to
  3a-4/5: a header found after skipped whitespace is judged by the caller (an xref entry
  must land exactly on `N G obj`). Differential numbers so far cover unencrypted files.
- 3a-4 is canonical: a closed set of chain shapes; every entry on a matching `N G obj`; each
  revision's object map equals MuPDF's and qpdf's with no repair warnings; else the whole
  file flags. No repair emulation (the legacy _earlier_revisions differential stays a check).
- 3a-6 gate (replaces decision 3's "tiling on 100%" as the correctness measure): zero
  unflagged disagreements with MuPDF and qpdf on the 2,031-file corpus, the case library and
  fuzz output; flagged disagreements reported by reason; 0 crashes, 0 timeouts (>60 s).
- 3b gains the minimal child process (moved from 3d); 3d keeps the OS sandbox.
- ADR notes with canonical-form rules before 3a-7 (reference graph), 3a-8/9 (decryption) and
  the Phase 4 decoders.
- Review triage: a finding blocks only if it could let a leak exit 0, hides or misattributes
  bytes without a flag, shows a plan flaw, or is a major implementation problem (crash on
  input, superlinear on plausible input). Everything else becomes a GitHub issue. Each
  finding names the automated check that would have caught it, and that check is added.
- The static import guard is frozen as a mistake-catcher; routes past it are the child's job.
- Measure before enforcing: the flag rate under these rules goes to the owner before any gate
  or default depends on it.

Tracks: A parser core 1→7; B cases 10→11 (day one); C encryption 8 (after 1), 9 (after 5+8).

- 3a-1 Core types (Status, NAReason[exactly ADR 0003's 4], FlagReason, UnitKind, UnitRef,
  Span, Flag, Unit in model.py; inventory/types.py Region/Tiling; budget.py Limits/Budget),
  tiling.tile() elementary-interval sweep + check_tiling(); Hypothesis infra; guard. ~550.
- 3a-2 Lexer (ISO 32000 §7.2–7.3, spans, never raises) + strings.py (literal_bytes fixes the
  two _decode_pdf_string line-ending bugs; text_string = UTF-16BE/UTF-8 BOM else
  PDFDocEncoding, keep raw bytes). Don't touch legacy. ~650. Done: the lexer is where a
  string first goes unterminated, so UNTERMINATED lands here (3a-3 reuses it for objects),
  with INVALID_HEX_DIGIT, INVALID_NAME_ESCAPE and STRAY_DELIMITER; each flag rides on its
  token, at most two per token.
- 3a-3 Object parser (value types incl. duplicate dict keys; parse_indirect_at; flags
  DUPLICATE_KEY w/ full-value compare, LENGTH_MISMATCH, STREAM_SLACK, EXTRA_TOKENS,
  UNTERMINATED, NESTING_LIMIT, MISSING_ENDOBJ). ~700. Converting an INTEGER token must cap
  its digits first: Python's int() raises ValueError past 4,300 digits, and the lexer
  accepts a run of any length. The lexer charges no Budget and caps flags per token only (every
  stray ')' is one flag): 3a-3 charges per token (max_tokens_per_object) and must bound
  flags per object and per file. From 3a-2's review: a hex string with bad bytes decodes
  differently in MuPDF (ends the byte) and qpdf (rejects it), so INVALID_HEX_DIGIT is never
  benign; unknown escapes and octal overflow are allowed unflagged (as in MuPDF), and so is
  '#00' in a name (MuPDF's behaviour there not checked); a text string's language tags are
  kept beside the stripped text for 3b to match both.
  Done: inventory/objects.py. Values keep their spans; a token that fits nowhere is a
  flagged, decoded stray, never dropped; PdfDict.get refuses a key repeated with different
  values; numbers past Limits.max_number_digits (64) are flagged, not converted; tokens
  (max_tokens_per_object), nesting (max_container_depth) and flags (max_flags_per_object, 64,
  then one FLAGS_TRUNCATED) are capped per object. New flags: UNEXPECTED_TOKEN,
  MISSING_VALUE, NUMBER_OUT_OF_RANGE, TOKEN_LIMIT, STREAM_EOL, ENDSTREAM_JOINED (MuPDF ends a
  stream at `endstreamendobj`, qpdf reads one word and a longer stream), FLAGS_TRUNCATED
  beside the planned ones. An indirect /Length is resolved through a callback 3a-5 supplies. Every
  `endstream` is indexed once per parser, so streams without one stay linear. For 3a-5:
  parse each distinct offset once (xref entries can share one), bound each parse's `end` by
  the next known object offset (an unterminated string otherwise runs to the end of the file
  from every offset: quadratic), treat a clamped out-of-range offset or a `None` value as an
  anomaly, and bound flags per file.
  Measured: 8,946 objects in the 402 case-library files parse with no flag, and every stream
  read at an xref offset matches MuPDF's xref_stream_raw byte for byte on that corpus.
  Anything but one end-of-line between a stream's /Length and `endstream` -- whitespace
  included -- is STREAM_SLACK: MuPDF reads it as data (spaces and NULs are valid image
  samples, and an image can draw text with them), qpdf does not.
  References are not range-checked here (a huge or dangling number is 3a-7's to flag), and
  comments inside an object are skipped, so 3b must match the whole object span's bytes.
- 3a-4 split in two (ADR 0010: small parser PRs). 3a-4a (#41) done: inventory/flate.py,
  capped FlateDecode + PNG/TIFF predictors; only the canonical case (complete zlib stream,
  checksum good, input consumed exactly, whole rows, valid row types) is unflagged; otherwise
  output is kept and FLATE_ERROR / FLATE_TRUNCATED / AFTER_STREAM_END (with extra and
  non-whitespace counts) / BAD_DECODE_PARMS / PREDICTOR_ERROR flag it; output charged to the
  inflated-bytes budget in 1 MiB steps. Differential: unflagged decodes equal MuPDF's
  xref_stream and qpdf's --filtered-stream-data (3,000 fuzz examples over PNG and TIFF
  predictors, Colors 1-5, BitsPerComponent 1-16, whole and partial rows).
  zlib joins the child-side import allowlist. UNSUPPORTED_FILTER waits for 3a-4b, where a
  filter chain is first read. 3a-4b (#43) done: inventory/xref.py, the canonical chain --
  the file ends `startxref N %%EOF`; each offset lands exactly on an `xref` table (exact
  subsection headers and 20-byte entries, whitespace allowed only before `trailer`) or an
  /XRef stream (valid /Type, /W, /Index, /Size, decoded length exact, FlateDecode only via
  3a-4a); `%PDF-` at byte 0 (HEADER_OFFSET otherwise: readers read offsets relative to a
  late header); hybrid /XRefStm merged, any object both define flagged (readers disagree);
  /Prev followed without cycles; a forward
  /Prev only as a linearized file's first-page section (the newest section, the file's
  first object a /Linearized dictionary, the main section it points to without a /Prev of
  its own), whose pair is one revision; each revision ends after every older one; every
  in-use entry on its own `N G obj` and the whole object (through `endobj`, parsed once,
  its parser flags reported) inside its own revision's bytes, every compressed entry --
  inherited ones included, so a revision freeing an object stream its older members still
  live in is flagged -- on an object stream in use in that revision (whether that home
  really is an object stream holding the index is 3a-5's), object 0 never in use, no empty
  subsection;
  checked in one pass oldest to newest (linear however many revisions);
  each revision's newest trailer names a /Type /Catalog via /Root and has /Size = 1 +
  its highest object number. New flags: XREF_TAIL, XREF_NOT_FOUND, XREF_TABLE_MALFORMED,
  XREF_STREAM_MALFORMED, UNSUPPORTED_FILTER, XREF_CONFLICT, XREF_OFFSET_MISMATCH,
  PREV_CYCLE, MISSING_ROOT, XREF_SIZE_MISMATCH. Differential: every revision of 398 case-library
  files (672 revisions) equals qpdf's --show-xref object map without warnings and MuPDF
  opens it unrepaired; the other 4 files are the after-%%EOF leak cases, flagged
  XREF_TAIL. An exhaustive single-byte mutation sweep of canonical classic, stream and
  incremental files finds no unflagged disagreement (it found the /Root and /Size rules).
  The legacy _earlier_revisions agrees on every file but the linearized one, where it counts
  the main section as an extra revision; qpdf cannot open that prefix cut. Every differential
  also fails on any MuPDF warning. Not done here: an incremental update on a linearized file is
  flagged (fail closed; common for signed files -- measure the rate in 3a-6); a catalog or
  object inside an object stream is 3a-5's; unchained sections and slack between sections are
  3a-5's tiling. The original item, for reference --
- 3a-4 Capped Flate (+predictors, AFTER_STREAM_END, UNSUPPORTED_FILTER) + xref chain
  (classic/stream/hybrid/linearized, /Prev cycles, offset mismatch vs slack, unchained
  sections, header offset) + Revisions with prefix-cut ends; differential vs legacy
  _earlier_revisions. ~750.
- 3a-5 build_inventory (claims: header, xref, epilogues, indexed objects; dead-body scan of
  gaps only; ObjStm tiling; bodies_by_number) + `python -m redaction_verifier.inventory FILE`
  secret-free JSON summary; Hypothesis pdf_files() strategy; every case tiles. ~650.
  Done: inventory/build.py, `build_inventory(data, limits, *, budget)` -> `Inventory` (types.py:
  the tiling and `tiles`, units, flags, per-number entry histories, `body(n, revision)` and
  `bodies_by_number(revision)` resolved on demand -- nothing materialized per revision -- raw
  stream-data spans, and each object stream's `ObjectStream`: its decoded tiling and members by
  index). Claims: HEADER (the `%PDF-x.y` line and the comment lines after it -- the binary
  marker, MuPDF's "% Written by"), XREF_TABLE per classic section, XREF_EPILOGUE per revision
  (plus the comment lines MuPDF writes after an intermediate `%%EOF`; a linearized first-page
  section's `startxref 0` is claimed too), OBJECT per distinct in-use offset of every revision
  and per xref stream, parsed once and bounded by the next known start (objects, sections,
  epilogues); STREAM_SLACK nested in its object; DEAD_BODY for `N G obj` bodies found in the
  unclaimed gaps only (one linear pass per gap, resuming after each body; a header is looked
  for in a 64-byte window before each `obj` keyword, so digit runs are never rescanned); every
  in-use /Type /ObjStm decoded (Flate only, 3a-4a; else UNSUPPORTED_FILTER), its header table
  (OBJSTM_HEADER) and members (OBJSTM_MEMBER, bounded by the next member) tiled in decoded
  coordinates by `tile(..., within=)`. An indirect /Length resolves in every revision its
  object lives in; a target that changes while it lives is REVISION_AMBIGUOUS (fail closed,
  even for an equal value: comparing each would cost streams x revisions); a /Length inside an
  object stream is not resolved (the stream then flags LENGTH_MISMATCH). xref.py's own object
  checks now resolve an indirect /Length too (every such stream was flagged before). Comment
  lines after the header or an intermediate `%%EOF` are an allowlisted leniency (both readers
  skip them; test in test_inventory_build.py); a comment anywhere else stays UNINDEXED.
  New flags: XREF_EPILOGUE_MISMATCH, REVISION_AMBIGUOUS, OBJSTM_MALFORMED (/N, /First, a
  header not exactly 2N unsigned integers, a repeated number, offsets not increasing),
  OBJSTM_MEMBER_INVALID (no value, a stream, an object or xref stream; a bare `null` -- MuPDF
  takes it for a missing object and repairs the whole file -- or a lone `N G R`, which both
  readers read as the integer N; a token running across /First or a member's bound, which
  the readers read whole), OBJSTM_ENTRY_MISMATCH. A dead object stream (a DEAD_BODY) is
  not decoded: its compressed bytes are 3b/Phase 4's to scan as raw bytes.
  Flags are distinct and capped per file (Limits.max_flags_per_file, 10,000, then one
  FLAGS_TRUNCATED with the limit). The import guard's one exemption: the dev entry point
  `inventory/__main__.py` may import json and sys, nothing may import it, and every other check
  applies to it (tests/test_import_boundaries.py ENTRY_POINTS).
  Measured: all 402 case-library files inventory and tile, 0 crashes; 398 unflagged, the other
  4 the after-%%EOF leak cases (XREF_TAIL, their tail UNINDEXED). Differential
  (inventory_agrees): every revision of the 398 (672 revisions) agrees with qpdf and MuPDF --
  object set, each object's stream data vs MuPDF's xref_stream_raw (4,266 stream objects),
  each of the 85 object-stream members' values vs qpdf --show-object and MuPDF xref_object,
  dead bodies exactly the unlisted `N G obj` bodies, no qpdf --check structural warning, no
  MuPDF warning; the one encrypted file's bytes and values are not compared until 3a-8/9.
  pdf_files(): 1,000 junk-free generated files all agree (603 with object streams, 881 with
  updates, 785 with dead bodies, 345 hybrid); of 1,000 with junk in a gap, 512 agree and 488
  are flagged; mutation fuzzing (content streams, a probe object, an object stream's
  header table and probe member, gaps): 3,000 examples, 406 agree, 2,594 flagged, no unflagged
  disagreement. Linear: work at 8n within 1.1x of 8 x work at n; wall clock n/8 vs n on shared
  offsets, offsets inside an unterminated string, 20,000 fake headers in a gap (three shapes), an
  ObjStm with /N 10^9 over 50,000 pairs, 20,000 members, deeply nested members: ratios 3.3-9.2
  (12 on a 7 ms run), at most 31 work units per byte (limit 64).
  From issue #44, closed here: the budget is required on this path (`read_chain(...,
  *, budget)`, `build_inventory(..., *, budget)`; comment 1 item 1, and item 3); every compressed
  entry of every revision must find its member in an in-use /Type /ObjStm listing it at that
  index, rechecked whenever a revision replaces the home -- a non-stream home included
  (comment 1 item 4, comment 2 item 1); a compressed /Root must be a clean /Type /Catalog;
  a section inside an object's stream data is flagged (the object's parse ends at the section:
  comment 1 item 2) and a newer section inside an older trailer is CONTESTED (comment 2 item 2,
  the tiling half). Still open in #44: linearized-file updates (item 1, measure in 3a-6), the
  surviving xref mutants (item 2), PREV_CYCLE's reuse (item 4), local mypy on the test file
  (item 5), the `_PAGE_TREE_SEMANTICS` exemptions (comment 1 item 3; the inventory's
  differential adds two narrowly: a content stream's own syntax, and qpdf's catalog warning
  only when it retyped the root in its page-tree repair and still reads a catalog), the
  newer-section-offset invariant in xref.py (comment 2 item 2's cheap fix), `/Linearized`'s
  value (comment 2 item 3) and 3a-7's qpdf message (comment 2 item 4).
- 3a-6 `python -m scorecard inventory` corpus gate harness (aggregates only, per-file
  subprocess + timeout); RESULTS.md section. THE 3a GATE: tiling on 100% of corpus files, 0
  crashes, 0 timeouts.
  Done: one oracle -- eval/scorecard/inventory.py `compare()`, which the tests' `inventory_agrees`
  now wraps -- returns an Agreement: AGREES, FLAGGED with its reasons, or DISAGREES naming
  the failed check (tiling, object set, stream data, member value, dead bodies, qpdf --check,
  MuPDF warning or repair, a reader needing a password, ENCRYPTION -- a revision is exempted
  as encrypted only when our trailer, MuPDF and qpdf all read it so (PR #47 review: `/Encrypt
  null` had skipped every byte comparison) -- or ORACLE_ERROR: anything that stops
  the comparison, a reader timeout included, never agrees). Every object-stream member is
  compared with MuPDF (qpdf still for the first 8 per revision), each home decoded once; the
  generator moved to eval/scorecard/pdfgen.py. `python -m scorecard inventory run --root DIR`
  refuses a corpus that drifted from its SHA-pinned manifest (counts only), then per file runs
  the dev CLI (`build_inventory`) in a child with a 60 s timeout and the oracle in a second
  child (900 s, reported separately); `inventory caselib` and `inventory fuzz` run the same
  gate. It fails on an unflagged disagreement (or no tiling), a crash, a timeout, an unflagged
  file the oracle could not finish (UNVERIFIED), or the two children flagging differently;
  flag rates never fail it. stdout and --json: counts only; per-file detail keyed by SHA-256
  to a local-only file under real_corpus/ (one per source), reader messages as scrubbed
  templates, a child's failure only as its exception class. The aggregate's config records
  provenance: commit and dirty tree, qpdf/PyMuPDF/MuPDF/Python versions, platform, UTC start.
  Refused (exit 2) without qpdf; a stale --json is deleted first. qpdf --check ERROR lines
  (exit 2, page-tree semantics so far) are counted, not gated. Also
  measured: the pending decisions from #44 item 1 and #46 items 1 and 4 (below).
  The fuzz gate found one oracle bug, fixed: MuPDF's tight printer writes an empty name and
  the next token without a separator (`/ 2.5` prints `/2.5`) though MuPDF, qpdf and we all
  read two tokens; the member comparison now parses MuPDF's pretty print.
  Measured (eval/spikes/RESULTS.md, "Phase 3a-6"): case library (398 of 402 cases buildable on
  Linux) passes: 394 agree, 4 flagged (the after-%%EOF cases), 0 unflagged disagreements, 0
  crashes, 0 timeouts; 9,773 streams and 92 members compared. Fuzz (3,000, seed 0) passes: 740
  agree, 2,260 flagged, 0 unflagged disagreements. Pending decisions on the case library: no
  updated linearized file, no /Length in an object stream, no REVISION_AMBIGUOUS, no dead
  object stream; comment lines in 322 files (533 lines, the longest 25 bytes, none
  non-printable or `N G obj`-like). The 2,031-file corpus run is the owner's (local only).
- 3a-7 (from 3a-4b's differential) must flag a reference to a free or missing object (the
  chain accepts it -- readers' object maps agree -- but qpdf --check warns when it is used), and
  own the page-tree semantics the xref differential exempts.
- 3a-7 Reference graph per revision (iterative, cycles flagged, page-tree inheritance,
  ResourceScope for pages/forms/annots/patterns/Type3; edges by use-kind; reachable/orphaned).
- 3a-8 Pin pikepdf (runtime dep per ADR 0001), encrypted fixtures incl. earlier revision
  with a different /Encrypt; confirmation test pikepdf read_raw_bytes == fitz
  xref_stream_raw (decrypted, unfiltered). If it fails → reopen ADR 0001 (owner).
- 3a-9 crypt.py: per-revision decrypt via pikepdf, value-level cross-check vs MuPDF,
  DECRYPTION_MISMATCH / DECRYPTION_UNVERIFIED, /Perms mismatch → flag.
- 3a-10/11 Image cases (hidden samples, JPEG COM, EXIF thumb, extra rows, strips variants)
  — PR #30 (merged).
- 3a-12 Measure unit budget (ADR 0006 amendment draft) + orphan overlap (ADR 0007).
- 3a-13 Measure decision-D cost (documented upper-bound approximations).
- 3a-14 Close-out: final corpus gate, REDESIGN row, ADR notes, CHANGELOG.

Shadow output: not in the shipped CLI during 3a (no sandbox until 3d; parent holds secrets).
From 3b: `--json` gains additive "experimental": {"shadow": {...}}; exit computed without it;
scorecard adds a shadow_exit metrics column outside the normalised key.

OWNER DECISIONS (2026-09-27), all as recommended:
1. Unit cap: measure in 3a with generous headroom (ADR 0006 amendment), re-derive at 4c/4d;
   hitting the cap cannot exit 0 (FLAGGED; a confirmed hard finding still exits 1).
2. Decision-D cost: documented upper-bound estimate in 3a (unknown use = unused), exact in 4b.
3. 3a gate: SHA-pinned manifest of the 2,031 files; exact byte partition on every file;
   UNINDEXED/CONTESTED allowed but reported (not gated); >60 s per file = failure.
4. pikepdf: regular runtime dependency (per ADR 0001).
5. No CLI wiring in 3a (default accepted; shadow output via dev CLI + scorecard aggregate).
