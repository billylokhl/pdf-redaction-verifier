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
  with a test showing MuPDF and qpdf agree. Audit the existing leniencies in 3a-3b: a lone CR
  after `stream` (flagged since 3a-3b), unknown escapes, octal overflow, '#00' in names, a
  header found after skipped whitespace/comments, text_string's replacement characters.
- 3a-3b (new, before 3a-4): tests/test_inventory_differential.py, a Hypothesis fuzzer that
  reads generated objects with our parser, MuPDF and qpdf and fails on any unflagged
  disagreement; grows to value level (object set, stream extents, string and name values)
  and becomes the 3a-6 gate below. Plus a file-wide work budget (bytes lexed incl. rescans,
  tokens; cap = k x file size -> BUDGET_EXHAUSTED) and n-vs-8n timing tests.
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
  benign; '#00' in a name, unknown escapes and octal overflow are allowed unflagged (as in
  MuPDF); a text string's language tags are kept beside the stripped text for 3b to match both.
- 3a-4 Capped Flate (+predictors, AFTER_STREAM_END, UNSUPPORTED_FILTER) + xref chain
  (classic/stream/hybrid/linearized, /Prev cycles, offset mismatch vs slack, unchained
  sections, header offset) + Revisions with prefix-cut ends; differential vs legacy
  _earlier_revisions. ~750.
- 3a-5 build_inventory (claims: header, xref, epilogues, indexed objects; dead-body scan of
  gaps only; ObjStm tiling; bodies_by_number) + `python -m redaction_verifier.inventory FILE`
  secret-free JSON summary; Hypothesis pdf_files() strategy; every case tiles. ~650.
- 3a-6 `python -m scorecard inventory` corpus gate harness (aggregates only, per-file
  subprocess + timeout); RESULTS.md section. THE 3a GATE: tiling on 100% of corpus files, 0
  crashes, 0 timeouts.
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
