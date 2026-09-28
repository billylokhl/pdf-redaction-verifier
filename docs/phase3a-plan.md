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
  parse each distinct offset once (xref entries can share one), and bound flags per file.
  Measured: 8,946 objects in the 402 case-library files parse with no flag, and every stream
  read at an xref offset matches MuPDF's xref_stream_raw byte for byte on that corpus. Known
  difference: whitespace other than one end-of-line between a stream's /Length and
  `endstream` is data to MuPDF, not to us or qpdf; whitespace holds nothing either way.
  References are not range-checked here (a huge or dangling number is 3a-7's to flag), and
  comments inside an object are skipped, so 3b must match the whole object span's bytes.
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
