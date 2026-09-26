# Redesign plan

Status: **draft, revision 3** — revised after four independent adversarial
reviews (soundness, feasibility, migration/evaluation, engineering) and a
confirmation pass.
Nothing here is implemented yet.

This plan replaces `verify.py` — one 3,000-line module — with a package
built around an explicit ledger of everything that must be checked
before a file can be certified, and uses the move to adopt the
engineering practices a release-gating security tool needs. It is staged
so the tool never becomes less strict than it is today.

## 1. Why

### What the tool is today

Six layers (Text, Objects, Metadata, Hidden, OCR, Binary) each extract
text their own way and feed one matcher (known values + pattern classes).
Exit `0` = certified clean, `1` = finding, `2` = cannot certify. Coverage
is documented by hand in `COVERAGE.md`.

### The design problem

Absence is a negative claim, but the tool is **enumerative**: it searches
the places it knows about and certifies whatever it did not find. Each
recent round of adversarial review found a new place (earlier revisions,
off-page text, orphaned XMP, leftover glyph-coded text, non-text
attachments), each patched as a special case. The review of this plan
found seven more that exit `0` today while holding the secret (§8,
cases K1–K7). Structural causes:

1. **No accounting.** Nothing checks that every part of the file was
   looked at, or that a layer which says "read" consumed everything it
   was given. Data after a compressed stream's end marker, plain text
   after the final `%%EOF`, or a text stream labelled as a font all pass.
2. **Layers are organised by extraction tool, not by storage.** Coverage
   is the accidental union of six techniques; `COVERAGE.md` describes it
   after the fact. Most gaps sit between layers.
3. **Decoding is tied to rendering.** Font-coded text is decoded only
   when drawn on a live page; pixels are read only by OCR of the
   rendered page. Stored-but-undrawn content — including text converted
   to outlines — is flagged or missed.
4. **Classification is taken on the file's word.** A stream's kind
   (font, image, page content) is decided from its own keys or by
   heuristics, and the chosen decoder is trusted to have read it.

A matching problem: built-in pattern classes have no context and raise
false **hard** findings on 7.5–13% of clean real-world files (dates as
card numbers, UUID digits as SSNs). Phase 1 measured 10.8% on a
2,031-file real corpus, confirming the range; see
[ADR 0005](adr/0005-pattern-class-default-tier.md), which demotes
built-in classes to review-only ahead of Phase 5's proper fix (owner
confirmation pending — this changes real exit codes starting at the
Move).

What already works and is kept: fail-closed on tool failure (qpdf
failure, missing OCR, a repaired earlier revision → exit `2`), two-tier findings, the xref-chain revision walk, the matching
engine and rules formats.

### The implementation problems

- One module mixing parsing, decoding, matching, policy and I/O.
- Tuned magic numbers, each a bypass (0.8 readable-code ratio, 8×32 px
  image gate, fewer than 3 content operators, 16 MB, 50 revisions).
- Objects are enumerated via `xref_length()`, which runs to the
  trailer's `/Size` and makes 1.4% of real files exit `2` for entries
  that do not exist.
- The evaluation harness that gated recent PRs lives in a temporary
  scratch directory, scrapes English warning text, and bypasses the CLI.
- Fail-closed is enforced by each layer remembering to warn.
- No type checking, linting, hash-locked dependencies or coverage in CI.

## 2. Goal, threat model, non-goals

**Goal.** Exit `0` means every **obligation** in the ledger was
discharged and nothing matched. Obligations start `UNEXAMINED`:

- one per *unit* of stored content (every object in every revision,
  every child a decoder uncovers, every unindexed byte range);
- one per *view* (each page's text readings, each page's OCR);
- one per *external tool* (qpdf, exiftool, OCR engine) and per rules
  scope (e.g. entity types the tool cannot verify);
- one per *cross-check* (parser agreement).

A unit's obligation is discharged only when its decoder **consumed** it
completely — proved by a witness the core checks (§4) — not merely when
its bytes were attributed to it.

Most unit obligations are discovered while scanning, so the parent
**anchors** the ledger before it starts the scanning child, from facts
it can establish independently:

- the file size — the child's reported unit spans must tile `[0, size)`
  exactly, checked in the parent;
- the page count as reported by qpdf (separate process) — one view
  obligation per page;
- the in-use object numbers of the current revision as reported by qpdf
  — each must appear as a unit;
- the tool and rules-scope obligations;
- a completion sentinel the child must send last, with counts of what
  it reported.

An empty or short but well-formed report therefore cannot yield exit
`0`.

**In scope.** Files from any producer, including malformed, repaired,
linearized, encrypted-with-owner-password and incrementally updated
files, where the sensitive data was *meant to be removed*. A file that
needs a user password the tool does not have exits `2`.

**Hostile input.** The PDF is untrusted input to the verifier. Crashes,
hangs and resource exhaustion are **defended** (exit `2`). Memory-
corruption exploits in native parsers (MuPDF, qpdf) are **mitigated**,
not defended: pinned versions, the parent's anchors (a forged result
must still tile the file and match qpdf's page tree and current-revision
object set), and an optional OS sandbox (no network, no writes).

**Non-goals** (stated in the README):

- Steganography: data in glyph spacing, image low-order bits, font
  outlines, or deliberately encrypted payloads — beyond flagging what
  cannot be read.
- Secrets not described by the rules file.
- Zeroing secrets in memory (Python cannot).
- Judging whether a box covers a glyph's pixels beyond what OCR of the
  rendered page reports.

## 3. Principles

In priority order.

1. **Fail closed by construction.** One function computes the exit code
   from the ledger. Every obligation starts `UNEXAMINED`; statuses are a
   closed enum; `NOT_APPLICABLE` requires a reason from a closed list
   (§4); an unhandled status cannot produce `0` (exhaustive `match`,
   mypy). A decoder that raises is `FAILED`, via one wrapper.
2. **Trust sits in witnesses, not in decoders' word.** `DECODED` is
   accepted only when the decoder's witness balances (bytes consumed,
   glyphs mapped). The **exit-0 audit surface** is listed explicitly:
   `model`, `verdict`, the parent's anchors and the parent/child
   protocol, the inventory tokenizer and byte tiling, reference-graph and
   resource resolution, decryption, the witness checks, the font-Unicode
   classifier, the normalizer and matcher, and each decoder's witness
   computation. Where our own code produces text a wrong answer would
   hide (decryption, resource resolution), it is also cross-checked
   against MuPDF for the objects both can see (§4).
3. **Classification is verified, never declared.** A unit's kind and any
   `NOT_APPLICABLE` reason must be confirmed by a successful structural
   parse (font program parses, image sample count matches). If a parse
   fails or the kind is uncertain, run every plausible decoder and take
   the union; guessing a kind never reduces coverage.
4. **Hostile input handling.** Parsing and decoding run in a child
   process (spawned, not forked; one per file) under: CPU-time rlimit,
   file-size rlimit, core dumps off, a parent-side wall-clock kill, and
   a parent-side memory watchdog polling the child's physical footprint
   (macOS ignores memory rlimits). Decompression we do ourselves is
   capped; MuPDF-internal decoding is bounded by the watchdog. Any kill
   → the child's unreported obligations are `FAILED`.
5. **Secrets stay secret.** Rule values and extracted document text never
   appear in logs, exception text, debug output or reports — only masked
   samples. Temporary files live in a private `0700` directory removed
   before exit.
6. **Deterministic.** Output is sorted and byte-identical across runs.
   Limits are work-based (bytes inflated, operators interpreted, pixels
   OCR'd); wall clock is only a backstop.
7. **Reproducible environment.** A hash-locked dependency file; GitHub
   Actions pinned by commit SHA; external tools resolved once to absolute
   paths, called with `--` before file arguments and a minimal
   environment (exiftool with `-config ''`); the report records tool,
   dependency, external-tool and OS versions and the rules file's
   SHA-256, and the tool refuses (exit `2`) below minimum versions.
8. **Provenance on everything.** Every finding, flag and warning carries
   a stable code and names its source: revision, object, byte span,
   decoder or view. `--explain` lists every undischarged obligation
   with provenance (never text).
9. **Every threshold is a named parameter** with its reason and a test
   pinning the trade-off.
10. **Generated coverage.** `COVERAGE.md`'s table is generated by joining
    the decoder registry (claims) with case-library results (evidence);
    a ✓ cell with no passing case fails CI.
11. **Verdict changes are versioned.** `CHANGELOG.md` has a mandatory
    "Verdict changes" section; any change that can move a file from `0`
    to `1`/`2` or `1` to `2` is at least a minor version bump.
12. **Compatibility.** Same CLI, exit codes and rules formats. A bare JSON
    array stays rules schema v1 forever; v2+ is `{"version": N, "rules":
    [...]}`. The redactor's YAML is another project's schema: unknown
    keys stay exit `2` and its hash is recorded.

## 4. Architecture

### Process boundary

- **Parent** (holds the secrets): CLI, rules, the list of expected
  obligations, matching, verdict, report.
- **Child** (touches the PDF): inventory, decoders, views. Streams
  obligations and evidence back over a pipe. A missing, truncated or
  malformed stream, or a non-zero child exit, makes every unreported
  obligation `FAILED`.
- **qpdf** runs as its own process for the independent parse.

Matching stays in the parent so the child never holds secrets.

### Package layout

```
redaction_verifier/
  cli.py          argv → Config → exit code
  rules/          schema, load, validate
  model.py        frozen types (below)
  inventory/      own xref/object parser: byte tiling, revisions, units,
                  per-revision reference graph and resource scopes
  decoders/       registry; one decoder per unit kind
  views/          page readings, rendered-page OCR
  fonts.py        Unicode-source classifier (pure)
  matching/       values, patterns, tiers (pure)
  verdict.py      ledger → Verdict (pure; only place exit codes are made)
  report/         human, JSON (with schema_version, experimental), --explain
  sandbox.py      child process, limits, watchdog, external-tool runner
```

### Core types (sketch)

```python
class Status(Enum): UNEXAMINED; DECODED; NOT_APPLICABLE; FLAGGED; UNREADABLE; FAILED

@dataclass(frozen=True)
class Obligation:
    id: UnitRef | ViewRef | ToolRef | RuleScopeRef | CheckRef
    status: Status
    reason: Reason | None          # closed enum; required unless DECODED
    witness: Witness | None

@dataclass(frozen=True)
class DecodeContext:
    revision: int
    referrer: UnitRef | None       # the object that draws/uses this unit
    resources: ResourceScope | None  # resolved, incl. inherited
    crypt: CryptHandler | None
    depth: int

@dataclass(frozen=True)
class DecodeResult:
    status: Status
    reason: Reason | None
    witness: Witness               # consumed ranges, glyphs shown/mapped
    evidence: tuple[Evidence, ...]
    children: tuple[Unit, ...]     # object-stream members, residue,
                                   # zip entries, nested PDFs, SMasks

Decoder = Callable[[Unit, DecodeContext, Budget], DecodeResult]

@dataclass(frozen=True)
class Evidence:
    source: UnitRef | ViewRef
    text: str
    adjacency: Adjacency           # GENUINE | JOINED_LINES | JOINED_LITERALS
                                   # | JOINED_PAGES | NOISY_SOURCE
    provenance: tuple[UnitRef, ...]
```

Tier = f(rule kind, `evidence.adjacency`) — today's two-tier model made
explicit.

### Inventory

Our own parser (~200 lines in the feasibility probe; it accounted every
byte of 1,224 real files in 1.6 s total). It replaces the current
hand-written xref code rather than adding a fourth parser, and objects
are never enumerated via `xref_length()`.

- **Chain grammar**: `startxref`, trailer `/Prev`, `/XRefStm` (hybrid
  files), linearized first-page sections (`startxref 0`, forward
  `/Prev`). An unknown form is a flag.
- **Byte tiling**: every byte belongs to exactly one of header (incl.
  the binary-marker comment), object, xref section/stream + trailer,
  `%%EOF`, or PDF whitespace. Anything else is an `UNINDEXED` unit — any
  non-whitespace byte is flagged; a range that parses as `obj…endobj`
  goes through the object decoders. Overlapping spans are a flag.
- **Ambiguity detection** (the tokenizer is the authority): duplicate
  dictionary keys, `/Length` disagreeing with `endstream` (fall back to
  scanning, record a flag-worthy note), xref offsets not landing on
  `N G obj`, the same object defined twice in one section.
- **Reference graph per revision**: which object draws or uses which,
  with resolved resources (inherited from the page tree or a parent
  form). This is what content streams need to decode (below).
- **Encryption**: per-object decryption using each revision's
  `/Encrypt`; unindexed ranges in an encrypted file cannot be decrypted
  and are flagged. Our decrypted strings and streams are compared with
  MuPDF's for every object MuPDF can reach; any mismatch is a flag (a
  wrong key yields garbage that still tokenises). Details in
  [ADR 0001](adr/0001-encryption-and-decryption-cross-check.md), which
  also proposes `pikepdf` for the crypto implementation itself
  (owner confirmation pending) rather than a hand-rolled one.
- **Resource resolution** is cross-checked the same way: for content
  drawn on current pages, the fonts we resolve must match the fonts
  MuPDF reports in its text trace.

### Decoding content streams

A content stream decodes correctly only with the fonts and resources in
scope where it is *used*, in *that* revision. Probes showed a wrong or
missing context gives silently wrong text (no error, no warning).

- The decoding unit is **(stream, context)**; the driver is a worklist:
  pop a unit, decode it under each context it is reached from, push its
  children.
- Mechanism (spike S1, confirmed): attach the stream to a scratch page
  with the resolved resources, optional-content switched on, the form
  BBox reset and a large MediaBox; extract with `get_texttrace()`.
- **Font witness**: `DECODED` only if no character is U+FFFD, every font
  resolves, and the pure `fonts.py` classifier confirms each font's
  Unicode source (ToUnicode covering the codes used, a standard encoding,
  or `/Differences` with standard glyph names). MuPDF gives no signal of
  its own.
- **Consumption witness**: MuPDF's interpreter silently skips malformed
  operators, unbalanced `BT`/`q` and bad inline images, so our own
  content tokenizer counts the character codes in every text-show
  operand (using each font's code length) and this must equal the glyph
  count in the text trace. Any MuPDF warning while interpreting the
  stream also means not `DECODED`. Feasibility is part of spike S1;
  spike S1b (Phase 1) confirmed the mechanism but found two refinements
  the witness needs — it must compare one resolved (stream, context)
  unit on its own scratch page, never a whole assembled page, and must
  exclude codes a simple font's encoding maps to no glyph (stray control
  bytes) from the expected count — and a residual ~7% mismatch on
  Identity-H (CJK) content that is not yet root-caused. See
  [ADR 0008](adr/0008-consumption-witness-granularity.md): the witness
  ships in Phase 4a as advisory evidence, not a hard `DECODED` gate,
  until that residual is understood.
- **Every token** is covered: strings outside text-show operators
  (`/ActualText`, marked-content property lists, `BX`/`EX` sections)
  are decoded and searched too.
- **Render pass**: a stream that paints (fills/strokes paths, draws
  images) is also rendered on its scratch page and OCR'd, so text
  converted to outlines is read. If it cannot be rendered in a resolved
  context, it is flagged, never `NOT_APPLICABLE`.
- A stream **no revision references** has no context: its strings are
  searched as they are (as today) and it is also decoded under a
  fallback (the union of document fonts), so a match is still found —
  but its status stays `FLAGGED`. A guessed context never counts as
  `DECODED`. This is stricter than today, where an orphaned stream of
  plain-looking strings passes; it is exactly the leftover a redactor
  that saves without garbage collection produces, and "clean up the
  file" is the right advice. Phase 1 measures how often it fires (2.7%
  of a 2,031-file real corpus, heavily skewed by two outlier files); see
  [ADR 0007](adr/0007-orphaned-content-streams.md), which accepts the
  cost and ships "always `FLAGGED`" with no narrower rule.
- **Deduplication** key: hash(decrypted stream) + hash(resolved
  dependency closure: fonts, resources, colour spaces, masks). Evidence
  keeps every `UnitRef` that shared it.

### Decoder registry

| Unit kind | Decoder | Witness / cannot-finish |
| --- | --- | --- |
| Filtered stream (any) | Explicit filter-chain stage | Bytes after a filter's end marker become a `RESIDUE` child (raw-searched, flagged if non-whitespace); unknown filter → `UNREADABLE` |
| Content stream, form XObject, annotation appearance, Type3 glyph proc | Scratch page, per context (above) | Font witness; unrendered paint → flagged |
| String / name / key in a dictionary | PDF token decoding | Whole object tokenised |
| Image (incl. each SMask as its own unit) | Normalise (stencil + `/Decode`, CMYK/Indexed/ICC → grey, JBIG2/JPX), OCR the image; composited masks/strips OCR'd as rendered | `DECODED` only inside a recall-validated envelope (format, size, mask type); outside it, or conversion failure → flagged; per-image cap 35 Mpx, dimensions in `[8, 10000]` px — [ADR 0004](adr/0004-image-ocr-envelope.md) (pixel bounds not independently re-verified against Apple Vision in Phase 1; owner confirmation pending) |
| Embedded file | By signature: text → detect and unwrap encoded runs (base64, quoted-printable, hex) then search; PDF → recursive verify (sub-ledger folded as worst status); zip/Office → unpack, per-paragraph text incl. deletions, comments, properties; else flagged | Shared global budget |
| XMP / Info | XML text / strings | — |
| Script stream | Raw text | — |
| Font program | Parse (sfnt / CFF / Type1); search name and metadata strings | Parsed tables must span the whole stream (padding aside); bytes outside them become a `RESIDUE` child; parse fails → unknown stream |
| Unknown / unverified stream | Every plausible decoder; raw text | Flagged unless a decoder fully consumes it |
| Unindexed range | Raw text, or object decoders if it parses | Any non-whitespace → flagged |

`NOT_APPLICABLE` reasons (closed list, each confirmed by parse):
xref-stream field data, object-stream header table, a font program
whose parsed tables span the stream and whose strings were searched,
image data fully consumed by the image decoder. Extended only by ADR;
[ADR 0003](adr/0003-not-applicable-reasons.md) confirms exactly these
four for Phase 1 and rejects three other candidates considered.

**Budget**: one object for the whole run including recursion — depth,
bytes inflated, units, OCR pixels. Exhaustion → flagged.
[ADR 0006](adr/0006-recursion-and-decode-budget.md) sets depth ≤ 25,
≤ 200,000 decoded units, ≤ 2 GiB inflated bytes, and a 500 Mpx
cumulative OCR cap — sized by analogy to today's per-attachment cap and
Phase 1's corpus object counts, not by a dedicated stress spike (owner
confirmation pending).

### Views

Kept for what storage cannot see: layout (values split across lines,
columns, page seams), glyphs drawn out of order, and wrong-but-present
Unicode maps (rendered-page OCR cross-checks them). Each page's view is
an obligation.

### Parser agreement

Compare the inventory's in-use object set and page tree against PyMuPDF
and against qpdf (separate process) for the current revision. "Repair"
means parse-level warnings (`qpdf <file> --object-streams=disable
/dev/null`, MuPDF warnings) — **not** `qpdf --check`'s linearization
lint, which fires on 12% of real Acrobat files. Measured raw flag rate
with this definition on a 2,031-file real corpus: 9.0% — six times the
original ~1.5% estimate, almost entirely two spec-defined, self-correcting
categories (a wrong xref offset qpdf recovers by scanning; a duplicated
dictionary key resolved by "last wins"). [ADR 0002](adr/0002-benign-parser-warning-categories.md)
names four such benign categories; excluding them brings the combined
qpdf+MuPDF flag rate to 0.69%, back in line with the original estimate.

## 5. Evaluation: the case library

The case library is the backbone of Phase 0 and serves three purposes:
test fixtures and scorecard corpus, the evidence behind `COVERAGE.md`,
and a gallery of how redaction fails.

### Cases

Each case is a generator (fake data only) plus metadata:

| Field | Meaning |
| --- | --- |
| `id`, `cells` | Stable IDs of the `COVERAGE.md` cells it exercises (cells get IDs) |
| `family`, parameters | Parameterised families (the 780 page-layer cases are one family) |
| `writer` | fitz, pikepdf/qpdf raw objects, hand-assembled bytes, reportlab, LibreOffice/Ghostscript, or a real redaction tool |
| `expected` | Correct verdict: exit, tier, rule |
| `known_gap` | Cell ID if today's tool is known to get it wrong — strict xfail: closing the gap fails the test until the label is updated |
| `story`, `mistake`, `recovery` | For the gallery: what went wrong, which tool or habit causes it, how the secret is recovered |
| `requires` | ocr / macos / fonts |
| `output_sha256` | Catches generator drift (fixed metadata dates and IDs) |

Guards against same-author bias:

- Each cell needs at least one case from a **non-fitz writer** (fitz is
  also the library the tool reads with) — done for every claimed (✓/⚑)
  cell; `caselib.NONFITZ_PENDING` tracks what is left and may only
  shrink.
- A committed **real-redactor tier**: small binaries with fake data and
  recorded provenance — a redactor POC, Acrobat Redact, a macOS
  Preview box, Word export with a shape over text.
- A **blind red-team slot**: cases written from `COVERAGE.md` and the
  threat model by someone (or an agent) who has not read the code.

Generators are portable: built-in or vendored OFL fonts, tools from
`PATH`, no absolute paths.

### Scorecard

Run only through the CLI's `--json` output, one subprocess per file,
with a timeout. Compared **per case** against a pinned reference (a git
tag, checked out in a worktree — never the live code) on the normalised
key {exit, set of (rule, tier, storage class), set of warning codes}.

| Metric | Definition | Measured on |
| --- | --- | --- |
| Silent miss | Leak case, actual exit `0` | Labelled cases only |
| Downgrade | Expected `1`, actual `2` | Labelled cases only |
| False hard | Clean case with a hard finding | Labelled clean cases + real corpus |
| Review rate | Clean case with exit `2` | Labelled clean cases + real corpus |
| Crash / timeout | As named | All |
| Runtime | Per file, p50 / p95 | All; budget p95 ≤ 2× baseline |

The real-world corpus is local-only, keyed by SHA-256 in a manifest
(paths relative to a configured root, no personal filenames), and
reports clean-side metrics by stratum: text-bearing, multi-revision,
producer family. It needs representative files — the current local set
is 76% text-free system resources — including a ~300-page scan, a
~500-page Word export and an image-heavy ~50 MB file.

Every intended per-case change is listed in `eval/accepted_diffs.yaml`
(case, old → new, reason, cell) and reviewed in its PR; any unlisted
difference fails the gate.

### Where each gate runs

- **Linux CI, every PR**: the non-OCR catalogue and the differential.
- **macOS CI (pinned image, e.g. `macos-15`)**: `requires: ocr` cases
  only; OS and Vision versions recorded in the baseline.
- **Local**: the real-world corpus. The PR commits
  `eval/results/<tree-hash>.json`; CI fails if the hash (computed
  excluding `eval/results/`) does not match the PR's code.
- Runtime is reported in CI but gated locally (runner timing is noisy).

### Gallery

Generated from case metadata: what a reader sees, the recovered secret,
the tool's verdict. Built at the end of Phase 0; it never gates.

**Status**: landed — `eval/gallery/` (`python -m gallery build --out DIR
[--results FILE]`): a static, dependency-free HTML page, grouped by
family and COVERAGE.md cell, with a page-1 render (built fresh, never
committed, with a derived caption when the cell's own row means nothing
would show there anyway), the pinned fabricated secret, the mistake and
recovery, and today's shown verdict per leak case — measured, from a
`scorecard diff --json` report, where one covers the case, the case's
own label otherwise, marked as such either way; a clear "MISSES IT
TODAY" marker on any case whose shown verdict exits `0`, a distinct "NEW
MISS" badge when that case has no pinned `known_gap` at all, and a note
when a measured result shows a pinned gap as already closed; clean and
false-alarm cases in their own section; each case linked to its
COVERAGE.md cell and, for the numbered gaps, its §8 K-number via an
explicit, hand-verified table checked against §8 by a test (an earlier
fuzzy, story-text-matching version proved untested and
non-deterministic). A non-gating CI job builds it and uploads it as an
artifact.

## 6. Transition

- **Move, don't wrap.** The reusable pure parts (normalizer, matcher,
  pattern classes and validators, rules loaders, report masking) move
  into the package; `verify.py` re-exports them until no test needs it,
  then shrinks to a ~5-line CLI entry (the README and `pyproject` use
  `verify.py`). A ruff banned-import rule stops new internal imports.
- **New path beside the old.** The ledger pipeline is built as a
  separate path, not as adapters over today's layers.
- **Worst-of verdict.** While both exist, the shipped exit is the more
  severe of legacy and new (ordering `0 < 2 < 1`). The tool never
  becomes less strict.
- **Shadow mode.** Two verdicts are computed from the ledger:
  - the *shadow* verdict counts every obligation. It is reported (a JSON
    field and a scorecard column) but never shipped or gated until
    Phase 6;
  - the *enforced* verdict counts only the tool, view, rules-scope and
    cross-check obligations plus units of **enforced kinds**. It is what
    enters the worst-of.

  No unit kind is enforced until its decoder lands (Phase 4), so
  accounting cannot flood exit `2` before decoders exist, and nothing
  needs adapters over the legacy layers. Views are the existing page
  readings and OCR, moved (not wrapped) in Phase 2.
- **Legacy retires** only when the new path alone is never less severe
  than legacy on any case, except those in `accepted_diffs.yaml`.
- **Legacy is otherwise frozen.** Known gaps found meanwhile become
  `known_gap` cases closed by the new path; a legacy hotfix is the
  owner's call per gap.

## 7. Plan

| Phase | Work | Gate |
| --- | --- | --- |
| **0a. Reference** | `--json` on today's `verify.py`: exit, findings (layer, masked rule, tier, storage class, object, revision, page, location) and warnings (stable code, kind, layer, storage class, rule, adjacency, tool, object, revision, page); `--version`; no verdict change. Tag it `eval-ref-0`. | Existing suite passes; JSON validated |
| **0b. Case library** | PR 0b-1: schema (truth, secret cells vs features, pinned known gaps, storage-qualified expectations), cell ids checked against COVERAGE.md glyph by glyph, the 85 end-to-end cases (labels corrected, portable fonts), K1–K9 pinned, a raw-bytes writer, the real-redactor tier, a build lock. PR 0b-2a: the carriers × producer-layouts grid (replacing `corpus_builders`) and the page-text grid (the page-layer corpus's 27 document layouts; a pattern-only rule set pins `match.pattern-split`); the judge checks storage exactly per expected rule. Then: a non-fitz case for every claimed cell (a may-only-shrink list; `rawpdf` gains incremental updates and xref/object streams); real-tool files with a provenance sidecar each (tool, version, settings, sha256, scrubbed metadata) and a privacy scrub test (raw bytes, decompressed streams, every decoded PDF string token — including UTF-16 and hex strings — metadata and XMP, a kind-and-pattern-exact reasoned allowlist for exceptions) — done; the blind red-team slot (`caselib/redteam/<round>/`, labels frozen on commit via a hash in `round.json` checked against a recorded `adjudications.log` entry, an origin="redteam" invariant against the loader's own registry, required attestation fields plus a README per round, a `new.*` cell placeholder gated by `cells.NEW_CELL_ALLOWLIST`'s tracking issue numbers, first-run score kept as hold-out) — done, with a worked (non-blind) example round exercising the machinery in CI; gallery fields (`mistake`, `recovery`) required on leak cases, `model.GALLERY_FIELDS_PENDING` as the may-only-shrink allowlist — done, filled for all leak cases as of this PR; `eval/check_ratchets.py` diffing every may-only-shrink allowlist and the red-team anchor against the merge-base with `main` (or `HEAD~1` on a direct push), since a single commit editing a list and its own same-commit consistency check together is otherwise undetected — done (single-commit case; see §8's "To check in Phase 0b" for the deferred multi-commit gap); cases for the remaining `UNDOCUMENTED_GAPS`; the large performance files. | Every ✓/⚑ cell has a caught leak case; every ✗ cell a pinned case or an allowlist entry |
| **0c. Scorecard** | Runner, differential against `eval-ref-0`, `accepted_diffs.yaml`, real-corpus manifest with strata and large files, CI tiers. Normalised key: exit, `error.code`, findings as (rule, tier, storage), review warnings as (rule, review, storage, adjacency), other warnings as (code, layer, tool). Additive report fields it needs: a per-layer status block (ran / unavailable / crashed), so Linux runs can recompute an OCR-excluded verdict, and `target_sha256`. Real-corpus results commit only the normalised key — never samples, messages or file names. The reference records storage class only; the new path adds the carrier kind (page content, annotation, Info dict …). One runner for pytest and the scorecard: the CLI as a subprocess per case with a timeout, judged on the process's exit code. CI tiers: Linux runs every case not needing OCR; macOS (pinned image) runs `requires: ocr` cases plus a clean false-alarm subset, and a scheduled full-environment run covers the grids (their OCR-free labels must keep holding with OCR); sharding and xdist; results cached by (PDF hash, rules hash, tree hash). Case labels hold verdicts and storage; exact legacy warning codes live in the `eval-ref-0` baseline. The judge checks finding storage exactly per expected rule; extend the same to warnings (a review warning filed under the wrong storage class). | Baseline committed; CI fails on unlisted diffs |
| **0d. Hygiene** | ruff, mypy (non-strict), coverage report; hash-locked deps; Actions pinned by SHA; Dependabot; subprocess argv (`--`, `-config ''`, absolute paths, minimal env); `CHANGELOG.md`; the `--json` report records qpdf, exiftool, OS and Vision versions and the rules file's provenance (SHA-256, format, rule counts). Independent of 0a–0c. | CI green |
| **0e. Gallery** | Generated from the case library. | — |
| **1. Decisions** ✅ | ADRs: [encryption](adr/0001-encryption-and-decryption-cross-check.md); [benign parser-warning categories](adr/0002-benign-parser-warning-categories.md); [`NOT_APPLICABLE` list](adr/0003-not-applicable-reasons.md); [image-OCR envelope method](adr/0004-image-ocr-envelope.md); [pattern-class default tier](adr/0005-pattern-class-default-tier.md); [recursion budget](adr/0006-recursion-and-decode-budget.md); [orphaned content streams](adr/0007-orphaned-content-streams.md). Spike S1b: [consumption witness](adr/0008-consumption-witness-granularity.md) (code count vs texttrace glyph count). Measured on a 2,031-file real corpus: parser-agreement flag rate 9.0% raw / 0.69% with benign categories excluded; unindexed non-whitespace byte rate 3.1e-6 by bytes, 0.39% of files; orphaned-content-stream rate 2.7% of files. Full numbers: [`eval/spikes/RESULTS.md`](../eval/spikes/RESULTS.md). | ADRs written; **five need owner confirmation** before their decisions ship: 0001 (adding `pikepdf` as a dependency), 0004 (the 35 Mpx / 10,000 px OCR envelope bounds, not independently re-verified), 0005 (demoting built-in pattern classes to review changes real exit codes from the Move onward), 0006 (budget numbers sized by analogy, not a stress spike), 0008 (shipping the witness as advisory rather than gating). |
| **2. Move** | Move pure parts into the package; port behavioural tests to the case library or CLI; rewrite mutation tests against new module paths. | Per-case differential identical |
| **3a. Inventory** | Own parser, byte tiling, ambiguity detection, reference graph, encryption; Hypothesis property tests (ranges tile the file exactly). Shadow mode. | Tiling holds on every corpus file; no crashes |
| **3b. Ledger + verdict** | Obligations, parent anchors, child protocol and completion sentinel, witnesses, single verdict function, shadow and enforced verdicts, worst-of shipping, `--explain`. | Shipped exit identical to reference on every case; shadow verdict reported; a truncated or empty child report exits `2` |
| **3c. Parser agreement** | As §4. | Flag rate as measured in Phase 1 |
| **3d. Sandbox** | Limits, watchdog, private temp dir around the child from 3b. Prerequisite for 4b–4c. | Bomb/hang cases exit `2` |
| **4a. Content decoder** | Scratch-page decoding with contexts, font witness, every token, render pass. Enforce for content kinds. Consumption witness ships as advisory evidence per [ADR 0008](adr/0008-consumption-witness-granularity.md), not a hard `DECODED` gate, until its ~7% CJK residual is root-caused. | K3–K6 and the switched-off-layer / hidden-annotation / unused-resource cells closed; every per-case change is stricter and listed; review-rate change within what [ADR 0007](adr/0007-orphaned-content-streams.md) accepted |
| **4b. Image decoder** | Normalisation, OCR, validated envelope. | Leftover-image cells closed inside envelope; recall measured |
| **4c. Containers** | Recursive PDFs, zip/Office, encoded-run unwrapping; global budget. | Container cells closed |
| **4d. Filters + residue** | Filter-chain stage, `RESIDUE` children. | K1, K2 closed |
| **5. Matching precision** | Context rules for pattern classes (built-ins already demoted to review by [ADR 0005](adr/0005-pattern-class-default-tier.md); this phase is where a specific, context-confirmed match gets re-promoted to hard) or refine the demotion; geometry-based matching for values split by columns or page furniture. | False hard < 1% on text-bearing real files |
| **6. Retire** | Legacy path removed; strict mypy on the core; 100% branch coverage on `model` and `verdict`. | New path alone passes every gate |

ADRs are required only for the Phase 1 questions and for any change to
compatibility or exit semantics.

**Phase 0c status**: landed — `eval/scorecard/` (runner, per-case
differential against `eval-ref-0` via a worktree, the normalised
*multiset* key above — same rule/tier/storage twice counts as two, not
one — with `OCR_UNAVAILABLE`/`TOOL_MISSING` dropped by exact, known tool
name (never an untrusted `/usr/bin/qpdf`-shaped or absent "tool" field
read as a match) and the exit recomputed before comparing, so the key
holds across environments; two guards on that recomputation so it can
never manufacture a verdict that hides a real bug — an error report's
exit is never touched, and the *unfiltered* report must already be
self-consistent with its own findings/warnings before anything is
recomputed, so a fail-open bug (a wrong exit `0` alongside a real
warning) shows up as a real difference instead of being "corrected"
away; the process's own exit code is checked against the report's own
claimed `exit_code` too (a mismatch is a crash, not a report to
normalise); `REQUIRE_FULL_ENV` is read only at the orchestration layer
(`normalization_have`), never inside the pure `is_environmental`/
`effective_exit` functions themselves, so their behaviour depends only
on their arguments; `eval/accepted_diffs.yaml` seeded from the real
reference-vs-main differences found by running it, with a `weaker: true`
requirement (checked both ways: required when the verdict does loosen,
rejected when it doesn't) on any entry that loosens the verdict (`1`→`2`,
`1`→`0`, `2`→`0`), and a stale-entry check on a full run that excludes a
case merely skipped this environment (`requires: ocr` on Linux) while
still catching one whose case id no longer exists at all; a `perf`-marked
case (a large, multi-minute generator) excluded from the default
selection unless named explicitly, so it can't silently time out a
default run; label-based metrics; the local-only real-corpus manifest —
producer *family* only, path-guarded to its own gitignored directory —
and stratified clean-side metrics; a reference result cache keyed by ref
commit, build-lock hash, the scorecard's own code hash, this machine's
tool versions/OCR availability, and whether `REQUIRE_FULL_ENV` changes
normalisation, with each cached case also carrying its own
`input_hash` (PDF bytes + rules) so changed rules invalidate just that
entry, and a crashed or timed-out run never saved or trusted back out).
The Linux every-PR differential job and the weekly full-environment
macOS job (`.github/workflows/tests.yml`,
`.github/workflows/scorecard-weekly.yml`), with `ci-ok` explicitly
failing (`if: always()` plus a `needs.*.result` check) rather than
relying on a skip reading as a pass; the judge's exact-storage check
extended to warnings, sorted by `str` rather than the tuple itself (a
code reported at both a string and a `None` storage otherwise crashes
the sort) (`eval/caselib/run.py`); `caselib.run.available()` probing OCR
directly rather than through whichever `verify.py` happens to be the
in-process import. Not yet done, left for a later PR: the additive
`--json` fields this phase calls for (a per-layer status block,
`target_sha256`), xdist/sharding, and the large (~300-page/~500-page/~50 MB)
performance files the real corpus wants.

## 8. Known gaps found by this review

All exit `0` on today's tool with the secret present (reproduced):

| ID | Case |
| --- | --- |
| K1 | Text-show operators after the zlib end marker in a **live** page's content stream (both parsers agree, qpdf reports no error) |
| K2 | Same, in an orphaned stream |
| K3 | Plain-text orphan stream carrying `/Length1` (taken for a font) |
| K4 | Plain-text orphan labelled as a 1×1 image |
| K5 | Text converted to outlines, in an orphaned stream |
| K6 | Same, in a switched-off optional-content layer |
| K7 | Plain text after the final `%%EOF` |
| K8 | Leftover font-coded text written without whitespace after `BT` (`BT/F1 11 Tf`, as `clean_contents` and redactor write it) — orphaned or superseded; found by the review of the case library; **fixed** in the current tool: leftover streams are tokenized as content (delimiters, NUL, long text objects, a font set before BT, compact inline images) |
| K9 | Font-coded text running across the page edge: the page reading splits it into on- and off-page parts |
| K10 | Leftover glyph codes shown one character per `Tj`: no string is long enough to judge |
| K11 | An untyped leftover text with three stand-alone words that are content operators (`n`, `m`, `q`) is taken for page content and never searched (pre-existing) |
| K12 | A scanned image with a box drawn over it on the same page: OCR only ever sees the rendered, composited page, never the image object itself |
| K13 | Font-coded text off the page in a real embedded font whose `/ToUnicode` map has been stripped: the glyphs are genuine (rendering the page with a widened media box and OCRing it reads the secret plainly), but no character-based reading, on or off the page, can turn the codes back into text without the map |
| K14 | Pixels drawn entirely outside the page's media box: never rendered, so OCR never sees them |
| K15 | Font-coded text in a switched-off optional-content layer: the layer is never rendered, and the Objects layer's literal scan does not apply a font's map |
| K16 | A hidden annotation's appearance drawing font-coded text |
| K17 | A hidden annotation's appearance drawing pixels |
| K18 | A form XObject in a page's resources, never drawn, showing font-coded text |
| K19 | A form XObject in a page's resources, never drawn, showing pixels |
| K20 | An orphaned stream shows a plain-looking string; a font mapping those exact (ordinary) codes to other glyphs would render it as the secret, but nothing flags plain-looking codes as undecodable and the literal characters do not match |
| K21 | An orphaned image just under the leftover-image size gate (7 px tall; still fully readable — OCR reads it back exactly once upscaled 10x) |
| K22 | The secret as a base64 thumbnail image inside the document's own (live) XMP metadata packet |
| K23 | Same, inside an orphaned/superseded XMP packet |
| K24 | The secret as a page's own `/Thumb` preview image: referenced (live), but never rendered or OCR'd |
| K25 | A zip attached as base64 text inside an `.eml`: no zip signature at the start, so it is read (and searched) as plain text, which the base64 does not literally contain |
| K26 | A PDF 2.0 `/AF` file matched only by a pattern rule: the Binary (qpdf) sweep — the only thing that reads such a file at all — is value secrets only |
| K27 | Same, a `/AF` file that is a scanned image: qpdf's raw sweep is a byte-text match, and compressed image bytes do not contain the value's literal digits |
| K28 | Same, a `/AF` file that is a zip container |
| K29 | A link's JavaScript action stores its code in a stream (`/JS 8 0 R`) rather than an inline string; a pattern rule never matches it because pattern rules are applied to PDF string-literal syntax, not raw stream bytes |
| K30 | An editor's private data (`/PieceInfo`) matched only by a pattern rule, for the same reason the Binary sweep is value-only |
| K31 | A `/Differences`-encoded font and its glyph codes for the secret, both appended after the file's final `%%EOF` — together exactly what a forensic reviewer could decode by hand, but entirely outside any indexed object |
| K32 | Raw pixel bytes for a scan of the secret, appended after the file's final `%%EOF` |
| K33 | A zip container appended after the file's final `%%EOF` |
| K34 | A value wrapped across two lines of a two-column page's left column, with an unrelated right-column line sitting between them: joining a page's lines top-to-bottom (not column by column) breaks the adjacency (judged only where OCR is absent — Apple Vision happens to read this specific layout column by column and gives the honest line-wrap review warning instead) |
| K35 | Text drawn at coordinates around 10⁹ points, split across two content-stream objects on the same baseline: PyMuPDF's extraction returns no glyphs that far out, and the Objects layer's per-object literal scan never sees either half whole (the same split at ordinary coordinates is read normally) |
| K36 | Font-coded text in a real embedded font, drawn starting at the exact same point as other page text: the two runs' glyphs share one baseline and interleave by x-position in both the Text layer's own reading and OCR's rendered pixels, so neither line comes out intact (a plain, non-embedded font at the same point is still caught by the Objects layer regardless of position, and a 20pt vertical offset — no more overlap — is read normally) |

K12-K36 are pinned in `eval/caselib/families/gaps.py` (the remaining
`UNDOCUMENTED_GAPS`, now empty). One more case there is the opposite
problem — a **false alarm**, not a silent miss: a clean document whose
only planted content is a live (referenced), correctly sized image
XObject whose byte ramp (0..255, repeated) happens to contain the ASCII
codes for '0'-'9' in order — the literal digits `0123456789` — purely as
a byproduct of the ramp itself. The Binary (qpdf) layer's raw byte sweep
cannot distinguish that coincidence from a real leak inside binary data,
so it warns (exit `2`) on a genuinely clean file
(`false-alarm.binary-value-collision`).

To check in Phase 0b:

- Font-coded (e.g. Identity-H) text in a drawn form whose BBox clips it
  away. The plain-text version is caught today (exit `1`), but the page
  text misses clipped glyphs, so a font-coded one may not be.
- `/XRefStm` in hybrid files is not followed by the revision walk.
- **Deferred**: `eval/check_ratchets.py` closes the single-commit version
  of the "may-only-shrink" ratchet weakness (a list and its own
  same-commit consistency check moving together) by diffing against the
  merge-base with `main`, or `HEAD~1` on a direct push. It does not close
  a *multi*-commit version: several commits, each individually diffed
  against its own `HEAD~1`, each moving a ratchet (or a red-team round's
  frozen anchor) a little, none of them triggering the check on its own,
  landing on `main` one push at a time rather than through a single pull
  request (where the whole set is checked against `main` in one shot).
  Closing that fully needs either disallowing direct pushes to `main`
  (branch protection, requiring PRs) or a check that walks every commit
  since the last tag/release rather than just `HEAD~1` — not implemented
  here; tracked as a follow-up.

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| A regression slips through | Pinned reference, per-case differential, worst-of verdict during transition |
| Review rate rises before decoders exist | Shadow mode; enforcement per unit kind |
| Decoders claim `DECODED` wrongly | Witnesses checked by the core; verified classification; envelopes for OCR |
| Runtime grows (render pass, stored-image OCR) | Measured: storage decoding is seconds per thousand files; image OCR ~1 s per scanned page — budget p95 ≤ 2×; caps are flags |
| Corpora unrepresentative | Non-fitz writers, real-redactor tier, blind red-team, representative large files |
| Scope creep | Each phase justified by named cells, K-cases or scorecard metrics |
| OCR is macOS-only | Unchanged; a portable OCR backend is out of scope |
