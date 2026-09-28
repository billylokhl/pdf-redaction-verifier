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
card numbers, UUID digits as SSNs). Phase 1 measured 23.1% for the `ssn`/
`us-phone` classes alone (the actual false hards -- a validated `email`
match is a true positive, not a miscolored digit run, so it is excluded
from this number) on the text-bearing stratum of a 2,031-file real corpus
(484 text-bearing files) — still well above the cited range; see
[ADR 0005](adr/0005-pattern-class-default-tier.md) (accepted): built-in
classes **stay hard** through the Move and until Phase 5 ships its own
fix, since demoting them earlier would contradict
this plan's own "never less strict" rule and Phase 2's differential
gate.

What already works and is kept: fail-closed on tool failure (qpdf
failure, missing OCR, a repaired earlier revision → exit `2`), two-tier findings, the xref-chain revision walk, the matching
engine and rules formats.

### The implementation problems

- One module mixing parsing, decoding, matching, policy and I/O.
- Tuned magic numbers, each a bypass (0.8 readable-code ratio, 8×32 px
  image gate, fewer than 3 content operators, 16 MB, 50 revisions). The
  new design removes the 8×32 image gate outright: an image of any size
  is enlarged and OCR'd, and `FLAGGED` until a recall bound covers it,
  and an image nothing in the document uses is always `FLAGGED`
  ([ADR 0004](adr/0004-image-ocr-envelope.md); K21).
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
  and are flagged. Our decrypted values are cross-checked against a
  second, independent decryption wherever one is available (MuPDF's for
  every object with an xref entry, including an orphaned one, and for a
  superseded revision through the same prefix cut today's
  `scan_earlier_revisions` already reopens, which carries that
  revision's own `/Encrypt`); any key path no second decryption reaches
  — a body outside every xref — is flagged, not `DECODED` on one
  decryption's word alone. Details in
  [ADR 0001](adr/0001-encryption-and-decryption-cross-check.md)
  (accepted): **pikepdf** decrypts each revision's prefix cut, and its
  decrypted-but-unfiltered stream bytes are cross-checked against
  MuPDF's `xref_stream_raw`; pikepdf is pinned when Phase 3a adds it.
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
- **Consumption witness**: MuPDF's interpreter silently skips or
  reshapes some input rather than raising, so our own content tokenizer
  counts the character codes in every text-show operand (using each
  font's code length) and this must equal the glyph count in the text
  trace; a mismatch means `FLAGGED`, unconditionally
  ([ADR 0008](adr/0008-consumption-witness-granularity.md), accepted:
  a hard gate from Phase 4a, no shadow-mode period). A MuPDF warning
  while interpreting the stream also means not `DECODED`, except under
  [ADR 0009](adr/0009-benign-interpretation-warnings.md)'s guarded rule
  (accepted): a warned unit is excused only when its own witness balances
  with a non-zero count, no warning on it is a filter/decode error, an
  image-decoder warning is vouched for only by that image's own witness
  and only if it is on the reviewed-harmless allowlist (an image-decoder
  error, a warning that suggests lost data or an unrecognised one always
  flags the image; owner decision C, ADR 0004), the warning belongs to
  the unit the witness measured (annotations and widgets are vouched for
  by their own unit), and warnings are collected
  with the interpreter run first on a freshly opened document. A warning
  the implementation does not recognise keeps its unit flagged, and the
  rate is re-measured on every PyMuPDF/MuPDF update.

  Count rules (from spike S1b, accepted with ADR 0008): the count needs
  two adjustments to reconcile on real content: a ToUnicode continuation entry is not a glyph, and on a
  unit that sets text render mode 2 or 6 (fill+stroke) MuPDF reports
  each glyph twice, as two separate spans (on any other unit a repeated
  glyph is a real second draw and must be counted). With those, and
  after fixing several bugs in the measuring spike itself, every
  measured text page reconciled (2,345 of 2,345; 856 pages that draw a
  form XObject were not measured). `Tr 7` (clip-only) text and text in
  a switched-off optional-content group (§8's K6) show codes with no
  texttrace glyphs and no MuPDF warning; today's tool finds both through
  its Objects layer, so the witness matters for this design's `DECODED`
  discharge, not as a gap in today's tool. The raw warning rule would
  flag 45.7% of text-bearing files (PyMuPDF 1.28.2); ADR 0009's guarded
  rule flags 7.6%.
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
  file" is the right advice. Phase 1 measures how often it fires: 11.4%
  of the text-bearing stratum of a 2,031-file real corpus (55/484,
  a lower bound — it does not count a paint-only orphan, the K5 shape,
  or a body outside the xref entirely — and a gross cost: today's tool
  already exits `2` on an orphan it cannot decode, and that overlap was
  not measured; Phase 3a measures it); see
  [ADR 0007](adr/0007-orphaned-content-streams.md) (accepted): "always
  `FLAGGED`", no narrower rule.
- **Deduplication** key: hash(decrypted stream) + hash(resolved
  dependency closure: fonts, resources, colour spaces, masks). Evidence
  keeps every `UnitRef` that shared it.

### Decoder registry

| Unit kind | Decoder | Witness / cannot-finish |
| --- | --- | --- |
| Filtered stream (any) | Explicit filter-chain stage | Bytes after a filter's end marker become a `RESIDUE` child (raw-searched, flagged if non-whitespace); unknown filter → `UNREADABLE` |
| Content stream, form XObject, annotation appearance, Type3 glyph proc | Scratch page, per context (above) | Font witness; unrendered paint → flagged |
| String / name / key in a dictionary | PDF token decoding | Whole object tokenised |
| Image (incl. each SMask as its own unit; a `/SMask` or `/Mask` is used exactly when its image is used, and a mask shared by more than one image is used only if all of its owning images are -- any single unused owner makes it unused and `FLAGGED`, fail-closed against hiding content in a mask shared with a harmless used image, owner decisions 2026-09-27) | Normalise (stencil + `/Decode`, CMYK/Indexed/ICC → grey, JBIG2/JPX), OCR the image; composited masks/strips OCR'd as rendered | Geometry (envelope: colour space/filter, at most 10,000 px per side, ≤35 Mpx, compatible mask) narrows what is attempted. There is no size excusal (owner decision A, 2026-09-27): an image under today's 8×32 `_text_sized` floor is enlarged and OCR'd like any other, and the envelope's lower bound is whatever Phase 4b validates with enlargement; this closes a single small image. An unused image is always `FLAGGED`, whatever its size or OCR result (owner decision D, like [ADR 0007](adr/0007-orphaned-content-streams.md)'s orphaned content streams). "Used" is the owner's definition: drawn by the current revision (not only an earlier one); by content that actually runs when a page is shown — the page's content streams and the forms, annotation appearances and patterns those draw, not only a switched-off layer, a hidden annotation, a non-current appearance state (`/AS` "off") or a form nothing draws; and with some of it landing on the page after the crop box and clipping (not entirely off-page, clipped to nothing, zero-size or fully transparent). Any page counts; when use cannot be established, the image is unused. So orphaned images, images a page lists but never draws, images drawn only in an earlier revision, hidden layers, hidden annotations or undrawn forms, page thumbnails (`/Thumb`) and `/Alternates` images, and images drawn entirely off-page or clipped away are all `FLAGGED`. A render cut into 7 px strips cannot be read strip by strip, and nothing reassembles unused strips, so D, not the missing size excusal, closes them. Images drawn visibly but covered by something painted over them are outside the definition, so used: strips under a box, drawn apart or partly clipped are a known miss of per-image OCR — the render does not reassemble them either, today's tool exits `0` on them, and D does not apply; how Phase 4b handles them is an open question for the owner. Every filter-chain stage of the image's bytes (encoded and decoded) is raw-searched (value rules, and pattern classes at review tier). A decode error, any warning that suggests lost data (truncation, a corrupt or premature end of a codestream, a zlib or flate error) or any unrecognised warning means `FLAGGED`; a warning reviewed as harmless (e.g. JPX `numcomps doesn't match color_space`, openjpeg `misplaced cmap box`) is on a reviewed allowlist and only the image's own witness may vouch for it, from Phase 4b (owner decision C). An image that decodes to more than declared — any image data beyond the main decoded picture: an EXIF or other thumbnail, an extra JPEG 2000 codestream, an extra JBIG2 page, decoded samples beyond `/Width`×`/Height`×components×bits per component, a codec frame larger than declared — means `FLAGGED` (owner decision B). `DECODED` requires a recall bound Phase 4b has not measured yet, so until then image-OCR evidence is `FLAGGED` for every image, whatever its size — [ADR 0004](adr/0004-image-ocr-envelope.md) (accepted; the pixel bounds are re-checked against Apple Vision's real limits in Phase 4b) |
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
[ADR 0003](adr/0003-not-applicable-reasons.md) (accepted) adopts exactly
these four, rejects three other candidates considered, and adopts the
governing rule this list depends on, a requirement on every decoder:
`NOT_APPLICABLE` excuses a decoder from re-parsing bytes a structural
parse already accounted for — it never excuses those bytes from the raw
matcher. The matcher runs over every filter-chain stage, including the
encoded bytes an image codec consumes, not only decoded samples (a JPEG
comment segment, a JPEG 2000 metadata box or a JBIG2 extension segment
never reaches the pixels). Pattern classes, not only value rules, run
over the same image bytes at review tier.

**Budget**: one object for the whole run including decoding recursion —
depth, bytes inflated, units, OCR pixels. Exhaustion → that unit
`FLAGGED` (an ordinary status, not a special override — this already
blocks exit `0` under the ledger model).
[ADR 0006](adr/0006-recursion-and-decode-budget.md) (accepted, as
placeholders) sets decoding-recursion depth ≤ 25 (reference-graph recursion — `/Kids`
chains, `/Parent` loops — is a separate, still-open problem for Phase
3a/3d), units sized against a measured corpus maximum of 32,971 *objects*
(a rough proxy: it undercounts objects compressed in an `/ObjStm`, and a
unit is not the same thing as an object — Phase 3a must re-derive this),
≤ 2 GiB inflated bytes, and OCR pixels capped at 200 Mpx per page **plus**
a whole-run cap derived from that (200 Mpx × the anchored page count, not
an independent flat number — a flat cumulative cap conflicts with §5's
own ~300-page-scan corpus requirement) — none of this from a dedicated
stress spike; the numbers are placeholders, re-derived in Phase 3a.

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
lint, which fires on 12% of real Acrobat files. This spike measures "any
qpdf/MuPDF warning" as a proxy for the real check (the inventory's own
object-set/page-tree comparison, which does not exist until Phase 3a) —
Phase 3c must re-measure against the real comparison before judging this
gate, not assume the proxy's rate transfers unchanged. Measured raw flag
rate with the proxy, on the text-bearing stratum of a 2,031-file real
corpus: 32.4% — two spec-adjacent but not spec-mandated categories
account for most of it (a wrong/zero xref offset that is benign *only
when no object body exists anywhere in the file for that number* --
nothing was actually lost; a duplicated dictionary key with identical
values, where no reader's resolution choice could differ).
[ADR 0002](adr/0002-benign-parser-warning-categories.md) (accepted)
verifies each instance of those two categories against the file's own
bytes (not assumed from qpdf's wording) and drops two other categories
the first version of that ADR wrongly called benign; the resulting
combined flag rate is **7.2% of text-bearing files** (2.9% of all
files) — well above the original ~1.5% estimate, and REDESIGN §4's own
"Ambiguity detection" bullet already treats duplicate keys and bad xref
offsets as flag-worthy at the byte-tiling level, which this measurement
does not contradict: ADR 0002 narrows only the *parser-agreement*
cross-check's judgment of when such an instance is additional
corroborating evidence versus provably inert.

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
would show there anyway, or — for a layout-splitting `match.*` cell —
when page 1's own extracted text holds the value only across lines, only
in part, only on later pages, or nowhere), the pinned fabricated secret, the mistake and
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
  into the package; `verify.py` re-exports them so its own remaining
  code (`main`/`run_cli` and the legacy scanning layers, until they too
  move or retire) and any external user can keep reaching them
  unchanged. Phase 2 step 3 (#29) decoupled the test suite from that
  re-export — every test now imports a moved name from the
  `redaction_verifier` submodule that defines it, not via `verify.X` —
  so the re-export block's only remaining reason to exist is the CLI
  entry and external callers, not the tests; it retires along with them
  once `verify.py` shrinks to a ~5-line CLI entry (the README and
  `pyproject` use `verify.py`) in Phase 6. A ruff banned-import rule
  stops new internal imports.
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
| **0b. Case library** | PR 0b-1: schema (truth, secret cells vs features, pinned known gaps, storage-qualified expectations), cell ids checked against COVERAGE.md glyph by glyph, the 85 end-to-end cases (labels corrected, portable fonts), K1–K9 pinned, a raw-bytes writer, the real-redactor tier, a build lock. PR 0b-2a: the carriers × producer-layouts grid (replacing `corpus_builders`) and the page-text grid (the page-layer corpus's 27 document layouts; a pattern-only rule set pins `match.pattern-split`); the judge checks storage exactly per expected rule. Then: a non-fitz case for every claimed cell (a may-only-shrink list; `rawpdf` gains incremental updates and xref/object streams); real-tool files with a provenance sidecar each (tool, version, settings, sha256, scrubbed metadata) and a privacy scrub test (raw bytes, decompressed streams, every decoded PDF string token — including UTF-16 and hex strings — metadata and XMP, a reasoned allowlist for exceptions whose entries are exact literals — kind and finding text compared with `==`, each required to match a finding the case actually produces) — done; the blind red-team slot (`caselib/redteam/<round>/`, labels frozen on commit via a hash in `round.json` checked against a recorded `adjudications.log` entry, an origin="redteam" invariant against the loader's own registry, required attestation fields plus a README per round, a `new.*` cell placeholder gated by `cells.NEW_CELL_ALLOWLIST`'s tracking issue numbers, first-run score kept as hold-out) — done, with a worked (non-blind) example round exercising the machinery in CI; gallery fields (`mistake`, `recovery`) required on leak cases, `model.GALLERY_FIELDS_PENDING` as the may-only-shrink allowlist — done, filled for all leak cases as of this PR; `eval/check_ratchets.py` diffing every may-only-shrink allowlist and the red-team anchor against the merge-base with `main` (or `HEAD~1` on a direct push), since a single commit editing a list and its own same-commit consistency check together is otherwise undetected — done (single-commit case; see §8's "To check in Phase 0b" for the deferred multi-commit gap); cases for the remaining `UNDOCUMENTED_GAPS`; a case with a value hidden in an image's raw sample bytes rather than rendered as glyphs and one with a value in a JPEG comment segment (in the encoded bytes, never the decoded pixels), to pin [ADR 0003](adr/0003-not-applicable-reasons.md)'s rule that `NOT_APPLICABLE`/`DECODED` never exempts a unit's bytes, at any filter-chain stage, from the raw matcher — done (`eval/caselib/families/image_bytes.py`: `leftover.hidden-sample-bytes`/`-pattern-rule`, `page.hidden-sample-bytes`/`-pattern-rule`, `page.jpeg-comment-ssn`, plus the EXIF-thumbnail, extra-rows and strips cases §7's 3a row and §8 schedule alongside them — `page.jpeg-exif-thumbnail` (K39), `page.image-extra-rows` (K37), `leftover.ssn-strips`, `page.ssn-strips-joined`, `page.ssn-strips-under-box` (K38), `page.ssn-strips-unused-resource`; each measured directly against today's `verify.py`, not assumed from the ADRs' own tables); the large performance files. | Every ✓/⚑ cell has a caught leak case; every ✗ cell a pinned case or an allowlist entry |
| **0c. Scorecard** | Runner, differential against `eval-ref-0`, `accepted_diffs.yaml`, real-corpus manifest with strata and large files, CI tiers. Normalised key: exit, `error.code`, findings as (rule, tier, storage), review warnings as (rule, review, storage, adjacency), other warnings as (code, layer, tool). Additive report fields it needs: a per-layer status block (ran / unavailable / crashed), so Linux runs can recompute an OCR-excluded verdict, and `target_sha256`. Real-corpus results commit only the normalised key — never samples, messages or file names. The reference records storage class only; the new path adds the carrier kind (page content, annotation, Info dict …). One runner for pytest and the scorecard: the CLI as a subprocess per case with a timeout, judged on the process's exit code. CI tiers: Linux runs every case not needing OCR; macOS (pinned image) runs `requires: ocr` cases plus a clean false-alarm subset, and a scheduled full-environment run covers the grids (their OCR-free labels must keep holding with OCR); sharding and xdist; results cached by (PDF hash, rules hash, tree hash). Case labels hold verdicts and storage; exact legacy warning codes live in the `eval-ref-0` baseline. The judge checks finding storage exactly per expected rule; extend the same to warnings (a review warning filed under the wrong storage class). | Baseline committed; CI fails on unlisted diffs |
| **0d. Hygiene** | ruff, mypy (non-strict), coverage report; hash-locked deps; Actions pinned by SHA; Dependabot; subprocess argv (`--`, `-config ''`, absolute paths, minimal env); `CHANGELOG.md`; the `--json` report records qpdf, exiftool, OS and Vision versions and the rules file's provenance (SHA-256, format, rule counts). Independent of 0a–0c. | CI green |
| **0e. Gallery** | Generated from the case library. | — |
| **1. Decisions** ✅ done — all nine ADRs accepted by the owner on 2026-09-27 | ADRs: [encryption](adr/0001-encryption-and-decryption-cross-check.md); [benign parser-warning categories](adr/0002-benign-parser-warning-categories.md); [`NOT_APPLICABLE` list](adr/0003-not-applicable-reasons.md); [image-OCR envelope method](adr/0004-image-ocr-envelope.md); [pattern-class default tier](adr/0005-pattern-class-default-tier.md); [recursion budget](adr/0006-recursion-and-decode-budget.md); [orphaned content streams](adr/0007-orphaned-content-streams.md). Spike S1b: [consumption witness](adr/0008-consumption-witness-granularity.md). Found while measuring 0008: [benign interpretation warnings](adr/0009-benign-interpretation-warnings.md). Four further owner decisions on images the same day, recorded in ADRs 0003/0004 (and 0007, 0009): A, no size excusal; B, in the owner's words, "any image data in the file beyond the main decoded picture (EXIF/other thumbnails, extra JPEG 2000 codestreams, extra JBIG2 pages) counts as 'decodes to more than declared', so the image is flagged"; C, image-decoder errors and data-loss warnings always flag, reviewed-harmless warnings are vouched for only by the image's own witness from 4b, unrecognised ones flag; D, images nothing uses are always `FLAGGED`, with "used" defined by the owner (drawn by the current revision, by content that runs when a page is shown, some of it landing on the page; fail-closed). Drawn strips under a box are an open question before 4b. Measured on a 2,031-file real corpus (484 text-bearing), both denominators: parser-agreement flag rate 32.4% raw / 7.2% refined (text-bearing); pattern-class false-hard rate 23.1% for `ssn`/`us-phone` alone (text-bearing; the 45.5% four-class union overstates it -- `email` is a true positive); orphaned-content-stream rate 11.4% (text-bearing); unindexed non-whitespace byte rate 3.1e-6 by bytes; consumption-witness reconciliation 100.0% of measured text pages (2,345/2,345; 856 form-drawing pages unmeasured), after root-causing bugs in the measuring spike itself; interpretation-warning rate 11.2%/45.7% (all/text-bearing, against the pinned PyMuPDF 1.28.2 -- down from 32.2%/53.5% on 1.27.2.3, about 3x on all files but about 1.2x on text-bearing files) -- the largest single Phase 1 finding, not sized before this pass; 7.6% of text-bearing files under ADR 0009's accepted guarded rule (with the annotation guard). Full numbers: [`eval/spikes/RESULTS.md`](../eval/spikes/RESULTS.md). | ADRs approved — met 2026-09-27 |
| **2. Move** ✅ done — step 1: model, matching, rules (#24); step 2: report, views (#27); step 3: tests decoupled from `verify.py`'s re-export (#29) | Move pure parts into the package; port behavioural tests to the case library or CLI; rewrite mutation tests against new module paths. Step 1 (#24): `redaction_verifier/model.py` (`VerifyError`, `Secret`, `Finding`, `Warn`/`WarnList`, `ScanReport` and the warning-code/storage/layer/adjacency constants); `redaction_verifier/matching/` (`values.py` — normalizer, `SecretMatcher`, `RollingScanner`; `validators.py` — the Luhn/SSA/card/email/NANP validators; `patterns.py` — `PatternRule`, `BUILTIN_PATTERN_CLASSES`, `match_patterns`, `PatternScanner`); `redaction_verifier/rules/` (the redactor-config mapping tables, the shared rule builders, `RuleSet`, `load_rules` and the JSON/YAML loaders). Step 2 (#27): `redaction_verifier/views/` (`text.py` — the layout-aware visual text extractor: `_reconstruct`, `_join_cluster`, `extract_visual_text`, `TEXT_GENUINE_READINGS` and the line-clustering tolerance constants; `ocr.py` — the Apple Vision OCR bridge: `_vision_recognize_batch`, `extract_ocr_text`, `OCR_DPI`, and the one source of truth for `_OCR_IMPORTS_OK`); `redaction_verifier/report/` (`text.py` — `_sanitize_report_text`, `print_report`; `json.py` — `build_json_report`, `write_json_report`, `JSON_SCHEMA_VERSION`). `verify.py` re-exports every name used outside the package (`from redaction_verifier.X import Y as Y`; three implementation-detail constants private to a single matching submodule are not re-exported, since nothing outside it ever reaches them) so existing imports, `verify.X` references and the CLI are unchanged; a ruff `flake8-tidy-imports` banned-api rule (`TID251`, scoped to exclude `verify.py`/tests/eval) stops the package from importing back, and the package import is guarded the same fail-closed way as the PyMuPDF import (missing package → `[ERROR]` + exit 2, never a traceback and exit 1). The views package's `import fitz` is plain (no guard of its own): a failure propagates out of `redaction_verifier` and is caught by the same guard; the Vision/Foundation import inside `views/ocr.py` keeps its ordinary-failure degrade (`except Exception: _OCR_IMPORTS_OK = False`) but no longer has its own dedicated fatal-message guard for a BaseException there, since the package must never import verify to call `_fatal_import` itself — that case is now reported as the generic "cannot import redaction_verifier" instead of a Vision-specific message (test_cli.py's import-guard tests were extended accordingly, plus a dedicated case pinning that an ordinary Vision `ImportError` still degrades rather than exits fatally). `build_json_report` no longer owns the tool version (`verify.__version__` stays in `verify.py`, the single source pyproject.toml's dynamic version reads); its caller passes it in as `tool_version`. Object/revision scanning, hidden-object handling, qpdf/exiftool and `main`/`run_cli` stay in `verify.py` for later steps. Step 3 (#29): every test and eval module that used a moved name through `verify.X` (`tests/*.py`, plus `eval/gallery/build.py`, `eval/caselib/run.py` and `eval/spikes/pattern_class_false_hard.py`) now imports it directly from the `redaction_verifier` submodule that defines it — `ScanReport`/`Secret`/`Warn`/`WarnList`/`WARNING_CODES`/`WARNING_FIELDS`/`LAYERS`/`STORAGE_CLASSES`/`VerifyError` from `.model`; `SecretMatcher`/`PatternRule`/`normalize_string`/`BUILTIN_PATTERN_CLASSES`/`match_patterns`/`mask`/the validators/`_fold_for_patterns` from `.matching`; `RuleSet`/`load_rules`/`ENTITY_TYPE_TO_CLASS`/`_make_class_rule` from `.rules`; `extract_visual_text`/`TEXT_GENUINE_READINGS`/`_OCR_IMPORTS_OK` from `.views`; `JSON_SCHEMA_VERSION`/`build_json_report` from `.report` — with no change to what any test does or asserts. `verify.py`'s own re-export block is untouched (compatibility for the CLI and external users; it retires only at Phase 6, see §6) and its remaining `verify.X` monkeypatches (`print_report`, plus the legacy-only `scan_pdf_objects`, `check_hidden_layers`, `_looks_binary`, `_strip_pdf_strings`, `_is_opaque_stream`, `_pdf_string_spans`, ...) are unchanged, since each is patched on the module its own caller looks it up on (`print_report`'s caller — `main`/`run_cli` — still lives in `verify.py`, so patching `verify.print_report` is still correct, not a leftover). `tests/test_import_boundaries.py` is a new regression guard: it parses `verify.py`'s own re-export block with `ast` to compute the "moved names" set (so it can never go stale), then parses every `tests/*.py` file the same way and fails on any `verify.<moved name>` attribute access — proven to catch a real regression and to have zero false positives on names `verify.py` defines itself. `tests/test_mutation.py`'s four guard-necessity mutations (`_looks_binary`, `_strip_pdf_strings`, `_is_opaque_stream`, `_pdf_string_spans`) all target the legacy object-scanning layer that stays in `verify.py` until Phase 6, so none needed retargeting to a new module path — only their fixture-construction calls moved to direct imports; all four still fail as designed when their mutation is applied (still "kill" the bug), confirmed by re-running them. No behavioural tests were restructured or moved into the case library/CLI: every remaining `verify.X` reference in the suite names something that still lives in `verify.py`. | Per-case differential identical — met: scorecard diff unchanged (386 compared, 10 changed, 10 accepted, 0 unlisted, 0 crashed); test count did not drop (1,027 → 1,029, +2 for the new import-boundary guard) |
| **3a. Inventory** — in progress: 3a-1 landed (#32); PR sequence and owner decisions in [phase3a-plan.md](phase3a-plan.md) | Own parser, byte tiling, ambiguity detection, reference graph, encryption; Hypothesis property tests (ranges tile the file exactly). Shadow mode. Fix, in the new tokenizer, the two PDF-32000 §7.3.4.2 line-ending bugs Phase 1 found in `verify._decode_pdf_string` (a backslash-end-of-line continuation is kept as a literal newline instead of contributing nothing; a raw CRLF inside a literal is not normalised to a single LF) -- today's tool appears to fail closed on every case tried despite this (normalisation strips the stray newline before matching), but the string decoder is on the exit-0 audit surface (Principle 2) and the bug is real. From the Phase 1 decisions: add and pin `pikepdf` for decryption, confirm its decrypted-but-unfiltered read matches the `xref_stream_raw` stage, and add a fixture whose earlier revision carries a different `/Encrypt` ([ADR 0001](adr/0001-encryption-and-decryption-cross-check.md)); add, before Phase 4b, the hidden-image case (a 10×10 image whose samples spell the SSN), the JPEG-comment case (a JPEG whose comment segment holds the SSN, present in the encoded `DCTDecode` bytes but not the decoded pixels), the EXIF-thumbnail case (a small JPEG whose EXIF thumbnail shows the SSN; the image must be `FLAGGED`) and next to it the extra-rows case (a referenced 200×20 DeviceGray image with a blank declared frame plus 20 extra rows holding an SSN render; the image must be `FLAGGED`; today's tool exits `0`, see §8's note below the K-table) ([ADR 0003](adr/0003-not-applicable-reasons.md), [ADR 0004](adr/0004-image-ocr-envelope.md) decision B), and the pixel-text-under-the-floor case, a leftover image under 8×32 holding pixel-drawn text (caselib's existing `leftover.small-image`, K21, a 7 px tall SSN render, already expects `LEFTOVER_IMAGE`; add the strips variant K21 describes, two leftover 90×7 strips of one render, which pins decision D, and next to it a strips-under-a-box case, both strips drawn with a black box painted over them, which today's tool exits `0` on) ([ADR 0004](adr/0004-image-ocr-envelope.md)) — done (`eval/caselib/families/image_bytes.py`: `leftover.hidden-sample-bytes`, `leftover.hidden-sample-bytes-pattern-rule`, `page.hidden-sample-bytes`, `page.hidden-sample-bytes-pattern-rule`, `page.jpeg-comment-ssn`, `page.jpeg-exif-thumbnail` (K39), `page.image-extra-rows` (K37), `leftover.ssn-strips`, `page.ssn-strips-joined`, `page.ssn-strips-under-box` (K38), `page.ssn-strips-unused-resource`; each built and run against today's `verify.py` rather than assumed from the ADRs' own tables — most are today's silent misses (`known_gap`); `page.hidden-sample-bytes`, `page.jpeg-comment-ssn` and `page.ssn-strips-joined` are controls the tool already gets right, pinned alongside the gaps for contrast); re-derive the unit budget ([ADR 0006](adr/0006-recursion-and-decode-budget.md)); measure the overlap of "always `FLAGGED`" orphans with today's exit `2` ([ADR 0007](adr/0007-orphaned-content-streams.md)), and the unmeasured cost of decision D under the owner's definition of "used": every unused image flagged, including images listed but never drawn, drawn only in an earlier revision, a hidden layer or annotation, page thumbnails and `/Alternates` images, and off-page or clipped-away images ([ADR 0004](adr/0004-image-ocr-envelope.md) decision D). 3a-1 (#32), in shadow mode (nothing the CLI ships imports it): the ledger types in `ledger.py`, re-exported from `model.py` (`Status`; `NAReason`, exactly ADR 0003's four, pinned by a test; a closed `FlagReason`; `UnitKind`; frozen `UnitRef`/`Span`/`Flag`/`Unit` -- a `Flag` carries only named plain integers, never document bytes); `budget.py` (`Limits` with ADR 0006's placeholders, a flat 20 Gpx ceiling on the derived OCR run cap and named parse limits, and a `Budget` whose `charge_*` methods return False on exhaustion, never raise, and report each exhausted counter once as a `BUDGET_EXHAUSTED` flag -- the unit cap is re-derived from this phase's measurements with headroom, owner decision 2026-09-27; hitting a cap means the file cannot exit 0); `inventory/` with `tile()`, an elementary-interval sweep that partitions every byte into owned, `CONTESTED`, `WHITESPACE` or `UNINDEXED` (flagged) regions -- a run of contested bytes is one region with one flag carrying the true claimant count and at most `max_contested_owners` (16) owners listed, so n claims give O(n) regions and flags however they overlap -- clips/flags claims past the end, refuses and flags (`CLAIM_INVALID`) nested refs, gap-kind claims and top-level claims of the nested-only kinds (`OBJSTM_MEMBER`, `STREAM_SLACK`) before hashing them, and flags a unit's partial self-overlap (`SELF_OVERLAP`); and `check_tiling()`, the pure geometric check the parent will re-run in 3b (which must also re-check gap labels against the raw bytes). Hypothesis is pinned in the `test` extra with a derandomized `ci` profile (the default under `CI`); property tests check `tile()` byte by byte against an oracle, claim-order independence, and that `check_tiling()` accepts every tiling and rejects single perturbations. `tests/test_import_boundaries.py` now also guards the process boundary: `import verify` loads neither `redaction_verifier.inventory` nor `pikepdf`, importing the inventory and budget loads no rules, matching, report, views, `verify` or `fitz` module, and no child-side module (`inventory/`, `budget.py`, `ledger.py`) imports those, imports importlib, runpy, pkgutil, builtins, imp, zipimport or the import system's internals, reaches `sys.modules`/`sys.meta_path`, names a banned module in a string, or mentions `__import__`, exec, eval, compile, `__builtins__` or `__loader__` at all (by name, attribute or string, so an alias or a getattr is caught; deliberate obfuscation is left to review). mypy runs strict on `redaction_verifier.inventory`, `.budget` and `.ledger`. | Tiling holds on every corpus file; no crashes |
| **3b. Ledger + verdict** | Obligations, parent anchors, child protocol and completion sentinel, witnesses, single verdict function, shadow and enforced verdicts, worst-of shipping, `--explain`. | Shipped exit identical to reference on every case; shadow verdict reported; a truncated or empty child report exits `2` |
| **3c. Parser agreement** | As §4. Close the two check gaps [ADR 0002](adr/0002-benign-parser-warning-categories.md) accepted for now: a benign-offset body inside an `/ObjStm`, and duplicate keys compared on the first token only. | Flag rate as measured in Phase 1 |
| **3d. Sandbox** | Limits, watchdog, private temp dir around the child from 3b. Prerequisite for 4b–4c. | Bomb/hang cases exit `2` |
| **4a. Content decoder** | Scratch-page decoding with contexts, font witness, every token, render pass. Enforce for content kinds. The consumption witness is a hard gate, enforced from this phase ([ADR 0008](adr/0008-consumption-witness-granularity.md)); a warned unit is excused only under [ADR 0009](adr/0009-benign-interpretation-warnings.md)'s guarded rule, with unrecognised warnings failing closed; forms, measured as their own units here, are what the Phase 1 spike could not measure. The witness de-duplicates a glyph drawn twice by fill-then-stroke keyed on (font, glyph, origin) and only for text drawn under `Tr` 2 or 6 — the spike keyed on (glyph, origin) across every span of a page that sets either mode ([ADR 0008](adr/0008-consumption-witness-granularity.md), causes 5 and 7). | K3–K6 and the switched-off-layer / hidden-annotation / unused-resource cells closed; every per-case change is stricter and listed; review-rate change within the rates measured in ADR [0007](adr/0007-orphaned-content-streams.md) (11.4% of text-bearing files) and ADR [0009](adr/0009-benign-interpretation-warnings.md) (7.6%); a larger rate goes back to the owner |
| **4b. Image decoder** | Normalisation, OCR, validated envelope. Image OCR stays `FLAGGED`-only until this phase measures a recall bound, and that bound must cover images under 8×32, enlarged (there is no size excusal); re-check the 35 Mpx / 10,000 px bounds against Apple Vision's real limits; pattern classes run over raw image bytes (every filter-chain stage, encoded and decoded) at review tier; the reviewed allowlist of harmless image-decoder warnings, each vouched for only by the image's own witness (decision C); unused images always `FLAGGED` (decision D, with the owner's definition of "used") ([ADR 0003](adr/0003-not-applicable-reasons.md), [ADR 0004](adr/0004-image-ocr-envelope.md)). **Open question for the owner before this phase** ([ADR 0004](adr/0004-image-ocr-envelope.md)): how drawn strips that are covered, drawn apart or clipped are discharged — for example, whether the recall bound must include banded images, or whether each content stream's images are also OCR'd composited as that stream draws them, without whatever is painted over them. | Leftover-image cells closed inside envelope; recall measured; K21, including the strips variant, exits `2`; the owner has answered the open question on drawn strips under a box, and the strips-under-a-box case exits `2` (or carries the answer as its label) |
| **4c. Containers** | Recursive PDFs, zip/Office, encoded-run unwrapping; global budget. | Container cells closed |
| **4d. Filters + residue** | Filter-chain stage, `RESIDUE` children. | K1, K2 closed |
| **5. Matching precision** | Context rules for pattern classes, or a demotion to review -- per [ADR 0005](adr/0005-pattern-class-default-tier.md) (accepted) built-ins stay hard until this phase, which chooses between the two; geometry-based matching for values split by columns or page furniture. | False hard < 1% on text-bearing real files |
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
| K14 | Pixels drawn entirely outside the page's media box: never rendered, so OCR never sees them. In the new design the image is unused under owner decision D's definition of "used" ([ADR 0004](adr/0004-image-ocr-envelope.md)), so `FLAGGED` |
| K15 | Font-coded text in a switched-off optional-content layer: the layer is never rendered, and the Objects layer's literal scan does not apply a font's map |
| K16 | A hidden annotation's appearance drawing font-coded text |
| K17 | A hidden annotation's appearance drawing pixels. In the new design the image is unused under decision D, so `FLAGGED` |
| K18 | A form XObject in a page's resources, never drawn, showing font-coded text |
| K19 | A form XObject in a page's resources, never drawn, showing pixels. In the new design the image is unused under decision D, so `FLAGGED` |
| K20 | An orphaned stream shows a plain-looking string; a font mapping those exact (ordinary) codes to other glyphs would render it as the secret, but nothing flags plain-looking codes as undecodable and the literal characters do not match |
| K21 | An orphaned image just under the leftover-image size gate (7 px tall; still fully readable — OCR reads it back exactly once upscaled 10x). The same gap covers a render cut into strips, each under the gate, not yet a case of its own — see §8's "Strips of one render that the page never shows joined" for the full picture, including the drawn-strips-under-a-box variant that stays an open question before Phase 4b. The new design closes the single image by removing the size excusal and the never-drawn strips by flagging every unused image ([ADR 0004](adr/0004-image-ocr-envelope.md), owner decisions A and D) |
| K22 | The secret as a base64 thumbnail image inside the document's own (live) XMP metadata packet |
| K23 | Same, inside an orphaned/superseded XMP packet |
| K24 | The secret as a page's own `/Thumb` preview image: referenced (live), but never rendered or OCR'd. In the new design a `/Thumb` image is unused under decision D, so `FLAGGED` |
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
| K37 | A referenced, drawn 200×20 DeviceGray image whose declared frame renders blank, but whose stream holds 20 more undeclared rows with an SSN render: MuPDF decodes only the declared `Width`×`Height`×components×bits-per-component byte range, so the extra rows are never drawn, never rendered, and never OCR'd, and MuPDF raises no warning that more data followed |
| K38 | The strips variant of K21, drawn under a box: a 90×14 SSN render split into two 90×7 images, drawn stacked on the page, with a black box painted over both afterwards — OCR only ever sees the rendered, composited page (the box), not what is under it, the same gap as K12 but for a render stored as two image objects rather than one |
| K39 | A small JPEG's own EXIF (APP1) thumbnail renders the SSN: a second, fully decodable picture the main image's declared frame never mentions. Only the main picture is ever drawn or OCR'd, and unlike a raw sample byte or a JPEG comment segment, the thumbnail's render is itself DCT/Huffman-compressed pixel data, not literal text bytes, so no raw byte sweep finds it either |

K12-K36 are pinned in `eval/caselib/families/gaps.py` (the remaining
`UNDOCUMENTED_GAPS`, now empty); K37, K38 and K39 are pinned, alongside
the rest of ADR 0003/0004's scheduled image cases, in
`eval/caselib/families/image_bytes.py`. One more case in `gaps.py` is the
opposite problem — a **false alarm**, not a silent miss: a clean document whose
only planted content is a live (referenced), correctly sized image
XObject whose byte ramp (0..255, repeated) happens to contain the ASCII
codes for '0'-'9' in order — the literal digits `0123456789` — purely as
a byproduct of the ramp itself. The Binary (qpdf) layer's raw byte sweep
cannot distinguish that coincidence from a real leak inside binary data,
so it warns (exit `2`) on a genuinely clean file
(`false-alarm.binary-value-collision`).

Two more silent misses used to have no K-number, because every K-number
must have a case and these did not; Phase 3a added cases for both
(`eval/caselib/families/image_bytes.py`), so they are now K37 and K38
above:

- **Image data beyond the declared frame** (K37). A referenced, drawn
  200×20 DeviceGray image whose declared frame is blank, but whose
  stream holds 20 more rows with an SSN render, exits `0`
  (`page.image-extra-rows`; measured, not reproduced from memory), and
  MuPDF gives no warning: the undeclared rows are never drawn, so
  neither the render nor OCR sees them, and the digits are pixels, not
  bytes. Under [ADR 0004](adr/0004-image-ocr-envelope.md)'s decision B
  the new design flags it.
- **Strips of one render that the page never shows joined.** A 90×14
  SSN render split into two 90×7 images exits `0` when both strips are
  drawn under a black box (K38, `page.ssn-strips-under-box`), when both
  are unreferenced (the strips variant of K21 itself,
  `leftover.ssn-strips`), or when both are listed in `/Resources` but
  never drawn (`page.ssn-strips-unused-resource`, the `unused-resource.pixels`
  cell K19 already names via a different case, so this one carries no
  K-number of its own); drawn side by side and uncovered, the rendered
  page's OCR reads it, exit `1` (`page.ssn-strips-joined`, a control, not
  a gap) — all measured against today's `verify.py`. Decision D flags
  the never-drawn strips (unused); the covered drawn strips are used, so
  they are a known miss of per-image OCR and an open question for the
  owner before Phase 4b (ADR 0004).

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

To check in Phase 3b (non-blocking notes from 3a-1's review):

- `tile()` drops an empty claim without a flag when it lies inside the
  file, and a claim that starts at or past the end is flagged
  `CLAIM_OUT_OF_RANGE` but plays no part in choosing an owner's label.
  Neither can hide bytes (an empty claim covers none), but 3b should
  decide whether a unit reporting an empty span is itself an anomaly.
- `check_tiling()` is geometry only; the parent anchor must also re-check
  every `WHITESPACE`/`UNINDEXED` region against the raw bytes (its
  docstring says so).
- `Limits` is configuration and is not validated: an infinite or
  fractional limit would make `Budget` misreport or raise. Validate it
  if limits ever become user-settable.
- The child-side import guard (`tests/test_import_boundaries.py`) bans
  the string constants `"exec"`, `"eval"` and `"compile"` and the
  attributes `.eval`/`.exec`/`.modules`; harmless code using those
  words will trip it. Loosen it case by case, never by dropping a check.

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
