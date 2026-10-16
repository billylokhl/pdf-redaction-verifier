# Evaluation

The case library that measures the verifier — see
[docs/REDESIGN.md](../docs/REDESIGN.md) §5. Feasibility probes and real-corpus
measurements behind the redesign's Phase 1 ADRs live separately in
[`spikes/`](spikes/README.md): throwaway scripts, not run in CI and not
part of the case library.

## The case library (`caselib/`)

Each case is a small script that writes one PDF with fabricated data,
plus the truth about it, what a correct verifier reports, and the story
of how the redaction failed:

```python
@case("leftover.redacted-no-gc", truth="leak", cells="orphaned.plain",
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      story="The SSN was redacted on the page, but the file was saved without "
            "garbage collection, so the original content stream is still inside.",
      mistake="Saving without garbage collection.",
      recovery="Decompress the file, find the leftover stream, decode its hex strings.")
def redacted_no_gc(path):
    ...
```

- **`id`** is `<family>.<slug>`, the family saying where the content is
  (`page`, `layout`, `document`, `attachment`, `leftover`, `revision`,
  `file`). Ids never change — baselines and gallery links key on them.
- **`truth`**: `leak` (a secret is in the file) or `clean`.
- **`cells`** (leaks only): where the planted secret is — ids from
  `caselib/cells.py`, which mirrors [COVERAGE.md](../COVERAGE.md) cell by
  cell. **`features`**: other cells the document exercises (all a clean
  case has).
- **`expected`**: the correct verdict — the exit code, hard findings as
  `(rule, storage)`, warnings as `(code, storage)`, and optionally
  `layers` as `(rule, layer)` when the cell is about one layer's reading.
  For a rule the case lists, where it is found is exact: the same rule
  found in a storage class not listed (a live object also called
  ORPHANED) fails the case. Any coverage warning ("could not read") not
  listed fails it too.
- **`known_gap`**: `KnownGap(cell, today=expect(...))` when today's tool
  gets it wrong. The case is judged against `today`, so a fix, a partial
  fix or a broken generator fails the test until the label is updated.
- **`older_macos`** (OCR cases only): `OlderMacOS(27, today)` when
  Vision on a macOS older than the owner's 27 reads the case's page
  differently; there it replaces `known_gap` (`today`, or `None` when
  `expected` holds there). `before` must be 27, so it never changes a
  label on the owner's macOS.
- **`writer`**: `fitz` (PyMuPDF — also what the verifier reads with),
  `raw` (hand-assembled bytes, `caselib/rawpdf.py`), `qpdf`, `pikepdf`
  (libqpdf in-process: the encrypted family, `families/encrypted.py`), or
  `file` (committed real-tool output under `caselib/real/`,
  `origin="redactor"`).
- **`bomb`** (`Bomb(wall_clock_s, memory_bytes, today=None)`): a bomb or
  hang case (`families/bombs.py`, Phase 3d). It is never scanned in the
  test process -- `test_case_library`, the gallery, the scorecard and its
  differential skip it -- and `tests/test_bomb_cases.py` judges it out of
  process through `redaction_verifier.sandbox` (`caselib/bombrun.py`),
  its whole process group killed at its caps -- on macOS, on Linux's
  Python 3.13 CI leg, or with `RUN_BOMBS=1` (once per platform per CI run;
  nothing in it depends on the Python version). `today` pins how today's
  tool fails to answer, where it does not (`"hang"`, `"memory"`,
  `"error:CODE"`, exit 2), or, as an `Expect`, the exact answer it gives
  when it finds the value but files it under the wrong storage (COVERAGE's
  footnote ¹⁵); the test fails once that changes, as a closed known gap
  does. Each bomb's built bytes are checked against `cases.lock.json`
  there too. The cells are COVERAGE.md's `hostile.*` ("Hostile input").
- **`rules`** default to `SSN` = 123-45-6789 and `Code` = BLUEHERON.
  **`requires`** (`ocr`, `qpdf`, `exiftool`) skips a case where a tool it
  needs to be judged is missing — or, with `no-ocr`, where OCR is present
  (for a text-layer gap that OCR happens to cover); other cases are judged with only the
  missing tools' "not available" warnings left out (none on a full
  environment, `REQUIRE_FULL_ENV=1`).

Tests (`tests/test_case_library.py`) also require: every ✓ and ⚑ cell has
a leak case that is caught, reported in that cell's storage class; every
✗ cell has a pinned known-gap case unless listed in `UNDOCUMENTED_GAPS`
(which may only shrink); and `cells.py` matches COVERAGE.md glyph for
glyph.

**qpdf's exit 3** (`families/qpdf_warnings.py`, `requires: qpdf`): since
the owner decision of 2026-10-15 an exit 3 is judged warning by warning
(COVERAGE.md footnote 16). Six leak cases are layouts where qpdf's
warning is the only signal of hidden content and must stay `2`
(`unindexed.plain.qpdf-warned`); seven clean cases are the harmless
layouts real files carry and now exit `0` (weaker `accepted_diffs.yaml`
entries, since `eval-ref-0` exits `2` on them). Three of those entries
are scoped `env: ocr`: their qpdf warnings depend on qpdf's version, and
only the weekly macOS job runs qpdf 12.4 (Linux `scorecard-diff` installs
Ubuntu's 11.9, where the two sides agree).

**Grids** (`grid=`, `params=`) are parameterised families: `carriers`
(six carrier surfaces × four producer layouts) and `page-text` (27 page
layouts × three rule sets × with or without the SSN pattern rule, plus the
pattern rule alone; labels and known gaps in
`families/page_text_labels.py`). Their tests carry the `grid` marker;
CI runs them on Linux, where OCR is absent — their labels hold with and
without OCR.

### The raw writer (`caselib/rawpdf.py`)

Every case above can be written with PyMuPDF, but PyMuPDF is also the
library the verifier reads with — a corpus it alone wrote could not show
a place the two disagree (docs/REDESIGN.md §5's same-author-bias guard).
`rawpdf.py` assembles a PDF's bytes by hand instead, object by object:

- **`build`** — a classic file: objects in number order, one classic
  cross-reference table, a trailer.
- **`incremental_update(pdf, objects)`** — appends one more incremental
  save to an already-built file: the given objects, a classic
  cross-reference section listing exactly those numbers, and a trailer
  whose `/Prev` chains back to `pdf`'s own last `startxref` — the same
  chain the verifier's earlier-revision walk follows, so this is what
  makes a raw `superseded.*` case possible.
- **`build_objstm`** — a PDF 1.5 file instead: the non-stream objects
  packed into one `/ObjStm`, and the cross-reference itself written as an
  `/XRef` stream rather than a table.
- **`build(..., trailer=...)`** with **`caselib/rawcrypt.py`** — an
  encrypted file by hand: `Handler` is the standard security handler,
  RC4 only (revisions 2-4, the empty user password; revision 4 through a
  crypt filter, optionally with `/EncryptMetadata false`), written with
  the stdlib alone, so the ledger's tests need no pikepdf for an
  encrypted file. It encrypts each string and stream for its object
  (`string`, `stream_data`), and can use a key the file's `/U` does not
  authenticate. `flip_last_block` and `replace_stream_data` make an AES
  file pikepdf wrote decrypt differently in the two readers (an
  inconsistent padding), with no key: Phase 3a-9's cross-check fixture.

`caselib.NONFITZ_PENDING` (`cells.py`) lists claimed (✓/⚑) cells that
still have no *caught leak case from a non-fitz writer* — a leak, no
`known_gap`, `writer != "fitz"` — tracking the same same-author-bias
guard case by case. It may only shrink; `tests/test_case_library.py`
checks both directions (nothing claimed and uncovered is missing from
it, and nothing in it is already covered).

### Real-tool files and their provenance (`caselib/real/`)

The only binaries in the library: committed output of a redaction tool,
because a generated corpus can only show a failure someone thought to
build. All data in them is fabricated (`families/redactors.py`'s
docstring). The tool itself is never named — only described generically
("a PyMuPDF-based redaction tool") — since this is a public repository.

Every `real/<name>.pdf` has a sidecar, `real/<name>.json`: its sha256
(re-checked against the file on disk, never trusted from the sidecar
alone), a declared per-file size cap, the tool's generic description,
version and run date, the exact settings and values used, which caselib
case (if any) generated its pre-redaction input, an explicit
"fabricated data" statement, the list of metadata fields scrubbed
(empty when the tool's own output already had none to scrub), and a
summary of the label (`families/redactors.py`'s `case()` call) it backs.
`families/redactors.py` cross-checks its own cases against their
sidecars at import time (`_sidecar`); `tests/test_case_library.py`'s
`TestProvenance` re-derives the same facts independently, so a sidecar
can't drift from either the file or the registered case without a test
failing.

Six of the files are measured outputs of the real-redactor set (below),
written by `python -m redactor_set subset`: the exact bytes the oracle
classed and the adjudication locked, never regenerated or shrunk, with
the set's persona as their rules (`PERSONA_RULES`). Their sidecars add
the tool's method (as-is, or re-saved with garbage collection), every
Info-like dictionary and XMP value in the file, live or orphaned
(`info_fields`), and an `oracle` block: the locked adjudication, per
planted instance its written form, fingerprints, class, cell, storage,
expected exit and the channels that found it, with the lock's sha256.
`tests/test_caselib_redactor_set.py` checks each case's label against
that block (the expected exit, and a required finding per recoverable
instance a correct verifier reports, except an owner-accepted listed
gap), re-runs the oracle's raw scanner and object walk on the committed
bytes, allows only persona values and known producer strings in the
metadata, and keeps file URLs and home paths out of the page text.

| File | Method | Truth | Shows |
| --- | --- | --- | --- |
| `redactor-form-fields.pdf` | re-saved | leak | form fields found and counted as redacted, but a widget is not removed: each still shows its value |
| `redactor-annotations.pdf` | re-saved | leak | a sticky note, a highlight's comment and a link outside any redaction keep their values |
| `redactor-scan-skipped.pdf` | re-saved | leak | a page with no text layer is skipped: the scan shows everything |
| `redactor-info-orphan.pdf` | as-is | leak | the nulled Info dictionary (a custom key too), the unlinked XMP and the old page stream stay as orphans |
| `redactor-statement-gc.pdf` | re-saved | clean | a correctly redacted statement |
| `redactor-w2-gc.pdf` | re-saved | clean | a correctly redacted form |

### Privacy scrub

Every case whose bytes are committed as-is (`writer="file"` — today
`real/*.pdf` and the red-team round's PDFs) is scanned by
`TestPrivacyScrub`, in its raw bytes, every stream PyMuPDF can
decompress, every PDF string token (literal `(...)` or hex `<...>`)
those contain — decoded, including a hex string's packed bytes and a
UTF-16BE/LE literal's actual text, since a leak hiding behind either is
invisible to a plain ASCII scan of the surrounding bytes — the
document's metadata and XMP, and any embedded file's name or
description. It looks for home-directory paths (`/Users/…`, `/home/…`,
`C:\Users\…`), email addresses outside `example.com` / `example.org` /
`*.test`, hostname-shaped strings (`*.local`, `*.lan`, `*.corp`, …), and
XMP document/instance ids that look machine-generated (a real UUID in
`xmpMM:DocumentID` or `xmpMM:InstanceID`).

A genuine exception is a `Case.privacy_allowlist` entry of exactly
`{"kind": ..., "text": ..., "reason": ...}`: `"kind"` one of
`model.PRIVACY_KINDS`, `"text"` the finding's text exactly as the scrub
reports it, and a `"reason"`. A home-path finding stops at the username
(`/Users/jsmith`, never the full path), so a home-path entry excuses
every path under that one home directory — all of `/Users/jsmith/...` —
not a single file. An entry excuses a
finding only when both its kind and its text are equal (`==`) to the
finding's: it is a literal, never a pattern, so it can excuse nothing but
the one finding it names, and an `"email"` entry never excuses the same
text reported as a `"hostname"`. `model.Case` rejects any other shape at
construction, and `TestPrivacyScrub` also requires every entry to match
a finding its case actually produces, so a dead entry — or a regex
written where the literal belongs — fails. (Entries used to be
`re.fullmatch` patterns screened for breadth against fixed probe
strings; every screen tried was defeatable — `/Users/[^/]+`, `.+\.home`,
`[^@]+@acme\.com`, a conditional group — and no case used the
allowlist, so it is literal-only now.)

### The real-redactor set (`redactor_set/`, local)

The committed real-tool files above are three, made by hand. The
real-redactor set measures a redaction tool's real output at scale,
against secrets whose every location is known: fabricated documents of
the kinds people redact, run through the tool, then judged by an oracle
that shares no code with the verifier and adjudicated per file before the
verifier ever sees them, so the verifier's silent misses and its exit `0`
rate on correctly redacted files can be counted (`scorecard
redactor-set run`). It is local: everything it
writes lives under the gitignored `eval/scorecard/redactor_set_local/`
(the generator refuses any other `--root` unless given
`--allow-outside`), and only aggregate counts are ever published. A
small subset is committed under `caselib/real/` with the usual sidecars
(above).

```bash
uv sync --extra test --extra redactor-set     # + reportlab, Pillow
PYTHONPATH=eval:. python -m redactor_set generate        # 20 documents -> inputs/<id>-s<seed>.pdf
PYTHONPATH=eval:. python -m redactor_set probes          # 22 probes   -> probes/
REDACTOR_CMD=... REDACTOR_PYTHON=... \
  PYTHONPATH=eval:. python -m redactor_set run --set inputs   # or --set probes
PYTHONPATH=eval:. python -m redactor_set oracle --set inputs  # m0, m1, m2 -> oracle/
PYTHONPATH=eval:. python -m redactor_set adjudicate draft --set inputs
PYTHONPATH=eval:. python -m redactor_set adjudicate lock --set inputs    # then: verify
PYTHONPATH=eval:. python -m scorecard redactor-set run [--seed 0] [--json OUT]
PYTHONPATH=eval:. python -m redactor_set subset --set inputs --pick NAME=STEM:METHOD:CASE_ID
```

- **Documents** (`templates.py`): 20 templates — tax forms (a W-2, a
  1099-INT, a fillable and a printed 1040-style return, a state return),
  statements (checking, with check images, card, a two-column brokerage
  one), scans (image-only, searchable with an OCR text layer, and a mixed
  packet), a three-page fillable application, a macOS "Save as PDF"
  letter, and the parts a page-text redactor might not reach:
  annotations, document Info and XMP, bookmarks, an attachment, page
  thumbnails, values in another format, and a clean control. Five
  producers write them — Chrome (`--no-pdf-header-footer`, a throwaway
  profile), reportlab, tesseract, macOS `cupsfilter` (Quartz: a simple
  font, literal strings) and pikepdf — and none is PyMuPDF, the library
  the verifier reads with. A producer's own `/Producer` string (a Chrome
  milestone, a macOS build) is kept; the committed subset's scrub must
  cover it. Needs Chrome (`CHROME` to override its path), poppler,
  tesseract and macOS.
- **Secrets** (`persona.py`): one fabricated household, every value
  unissuable (SSN area 666, neither SSN sequential, EIN prefix 00, an
  unassigned routing prefix, the card networks' test number,
  example.com), no two sharing a run of six digits. One fabricated config
  with a real config's structure (exact values, patterns, entity types;
  LLM detection off), written to `config/redact.yaml`, drives every
  document and is also the verifier's `--secrets`.
- **Truth** (`truth.py`, `<id>-s<seed>.truth.json`): every place a
  secret was written — its location code (live text, a variant format, a
  split value, a form field, each annotation kind, Info, XMP, an outline,
  an attachment, scan pixels, an OCR text layer, an image, a thumbnail),
  page and box (points, top-left origin; per part, for a split value),
  and, for drawn text, its fingerprints (`locate.py`): the codes the
  page's own fonts would use to draw it, one sequence per font that can
  draw every character, found through each font's ToUnicode map. Drawn
  text is often shown a glyph or a word at a time, so a fingerprint
  appears in a content stream only once a text block's string operands
  are joined (kerning numbers dropped); pdfminer.six gives positions.
- **Planting checks** (`build.py`): generation fails unless the document
  holds exactly what its builder declared — every written form's page
  text occurrences (pdfminer.six) are the declared ones; every secret
  stored as text anywhere else (a string object, XMP, an attachment, an
  annotation or field appearance, any other decodable stream) is
  accounted for by a declared instance of that kind, and every declared
  one is really stored; and neither page text nor a string, XMP or
  attachment store holds, outside a declared value, a piece of a secret
  four or more characters long — a run of a number's digits, or a whole
  word (four or more letters) of a name or address; machine-written
  dates and identifiers are exempt — which an oracle could not tell from
  a redaction that left part of a value behind (partial residue counts
  as a leak). Filler amounts and dates are redrawn until
  they hold none. Pixels (scans, images, thumbnails) are declared, not
  checked; an OCR text layer's own readings are recorded as instances.
- **Preflight** (`generate.py`): a document is refused if it carries,
  in any case, this machine's login name, the user's full name or any
  part of it, the home path, the hostname, or a `file://` URL — in its
  raw objects, its decoded streams, its decoded string objects (hex,
  escaped or UTF-16), or its extracted page text (text drawn in glyph
  codes, such as a browser's header, is readable only there) —
  or a secret's bytes inside a font program or image samples, or a
  trailer `/ID` whose text the config's patterns match (a hex string's
  digits as written, a literal string's own text).
- **Probes** (`probes.py`): one location per document, to see what the
  tool does to each place alone before the full set is trusted.
- **Runner** (`run.py`): method 1 is the tool exactly as it is run day
  to day, `$REDACTOR_CMD <file> --config <yaml>`; method 2 re-saves
  method 1's output with garbage collection (`garbage=4, deflate=True`)
  in the tool's own Python, `$REDACTOR_PYTHON`. The tool is named
  nowhere in the repo, only by these environment variables. A run that
  times out is recorded as such (its whole process group ended), and an earlier run's outputs are
  removed first, so they can never stand in for a failed one
  (`work/<set>/run.json`).

- **Partial residue** (owner decision, 2026-10-14): a surviving piece of
  a secret counts as a leak. For digits and identifiers, any run of four
  or more of a value's digits counts (separators inside one token
  dropped: `123-45` holds `1234`); for letters, only a whole word of four
  or more letters from a secret (`Testperson`, `Fictional`) — a
  four-letter fragment inside another word (`tion` in `compensation`) is
  not one. The generator keeps filler free of such pieces and the oracle
  looks for them by this one rule (`persona.residue_in`,
  `oracle_raw.residue`; a test checks the two agree).
- **Oracle** (`oracle_raw.py`, `oracle.py`): for each output file and
  each planted instance, which channels still find the value, on three
  engines so no class rests on one the verifier also uses. **R1**
  (`oracle_raw`, standard library only): every `stream … endstream`
  body in the raw bytes decoded (Flate, ASCIIHex, ASCII85) and searched
  for the written text (ASCII, escaped and hex strings, UTF-16, and
  UTF-16 as a hex string) and, in content streams, for the instance's
  fingerprints or its UTF-16 in each text block's joined operands; a
  body is marked superseded when a later body has its object number and
  unindexed after the final `%%EOF`; a hit in a font
  program or image never counts alone. **R2** (pikepdf): every object,
  live (reachable from the trailer) or orphaned, each string with the
  role of the dictionary holding it (Info, annotation, field, outline,
  file spec, link action) and each decoded stream with its role (page
  content, appearance, XMP, attachment); every image and thumbnail is
  extracted for OCR (one this channel cannot decode is noted on the
  result, never dropped silently). **V** (poppler `pdftoppm` at 300 dpi, then
  tesseract — not the verifier's Vision): each planted box cropped and
  read, each page read whole, each extracted image read; plus
  pdfminer.six's live text (R3). Evidence counts for an instance only
  from a place that kind of instance is stored (a name in the Info
  dictionary is not counted for the same name drawn on a page).
  Evidence no planted instance can claim — the tool copied a value into
  an annotation or metadata, a scan gained a text layer, an earlier
  revision kept it — is kept as an **unattributed** row for its secret:
  it makes the file a leak, and the adjudicator must place it. A
  page-level reading (pdfminer's live text, the whole-page OCR) is
  accounted for only by an instance of that secret that took that same
  reading on that page: a scan or a form field on the page does not
  account for a text layer the tool added over it. Each row
  is **recoverable-visible** (V reads it whole), **recoverable-hidden**
  (stored whole, outside fonts and images), **partial** (a piece, or one
  part of a split value) or **removed**. OCR allows one edit on values
  of nine or more characters; a farther miss (two or three edits, or one
  on a shorter value) is flagged *uncertain*, never a match, as is a
  planted box that still holds ink no reading explains (a box of one
  colour — a redaction box, or blank paper — does not). Machine-written
  dates are never searched for partial residue; a secret's piece inside
  another secret's whole value still counts (a name in an email address
  is a name). Method `m0` (the
  unredacted inputs) is a calibration: every planted instance must be
  found whole, and by the channels that read stored content (R1 or R2
  for text, the extracted-image OCR for pixels; they alone see an
  orphaned stream or image later), or `oracle` exits 1. Renders and crops are kept under
  `oracle/<set>/<method>/<stem>/` for the adjudicator.
- **Adjudication** (`adjudicate.py`): `draft` turns each oracle result
  into a record per file — per row its class, where its evidence is
  stored (live, orphaned, superseded, unindexed; for pixels, drawn or
  orphaned), the COVERAGE.md cell that puts it in and the exit a correct
  verifier gives there, taken from the case library's own case for that
  cell (`EXPECTED`; rows with no case of their own are marked
  provisional, known gaps marked as such, and a test checks every case
  id, cell and exit against the library). Pixels a page draws map to
  `live.pixels`, which the library expects to be a hard finding through
  OCR (exit `1`). Rows the adjudicator must clear are flagged: uncertain
  OCR or ink, a channel disagreement, an unattributed row, a storage the
  oracle could not tell. The adjudicator (an agent viewing the kept
  renders and crops, the owner spot-checking) names themself and clears
  each flag — `confirmed` with a note, or an `override` with a reason
  (and, for a recoverable row, a cell and expected exit; never exit `0`,
  which would score a silent miss as correct). `lock` refuses an
  unfinished record, a record whose output file or oracle result has
  changed, a row edited by hand rather than overridden, and a set that
  is not exactly one record, `<method>/<stem>.json`, per `m1` and `m2`
  output in the run's `run.json` (an output the run did not make is
  reported, never skipped); `--seed` limits the set to those seeds'
  documents and the lock keeps it (`draft --seed` drafts only those);
  it recomputes each file's truth and expected exit (the worst of its rows,
  in the order `0 < 2 < 1`) and writes a sha256 per record to
  `adjudication.lock.json`. A locked record that changes, disappears or
  is added, a change of seeds, or a lock file gone while records are
  already finalised needs a reason, which the lock keeps. `verify` re-checks everything
  and prints the lock's own sha256: the lock lives in the gitignored
  root, so the measurement reports that hash beside its counts. Records
  are locked before the verifier runs on the set.

- **Measurement** (`scorecard redactor-set run`, `scorecard/redactor_set.py`):
  refuses to start unless the adjudication lock verifies, then runs the
  verifier's CLI on every locked `m1` and `m2` output with the set's own
  YAML config as `--secrets`, and counts, per method:
  - per file (`compute_metrics`), with truth and expected exit from the
    lock: silent misses (a leak that exits `0`), downgrades (expected
    `1`, got `2`), false hard;
  - per instance (U2): every row the adjudication left recoverable,
    partial residue included, must be found by the verifier. Its report
    has one entry per (layer, rule, location), so one occurrence can
    give several (the Text and OCR layers on one page; an exact rule and
    a pattern both matching). Entries are therefore grouped into
    **occurrences**: a place (storage, and the page, else the object)
    and, there, the secret whose exact rule fired, else "a pattern
    match". A pattern entry where another secret's exact rule fired
    belongs to that secret, never to a different secret the pattern
    happens to fit. An occurrence names an instance when one of the
    instance's rules fired (its secret's `exact_values[i]`, or a pattern
    matching the string as written), in a storage class it can sit in
    (`live`; `orphaned` or `unreferenced` for an orphaned object;
    `superseded`) and, for drawn text and pixels, on its page (a value
    split across pages: on any of them, or by a match the report joins
    across the break and places on no page). Each
    occurrence vouches for at most one instance. Content with no page
    (an annotation, a field, metadata) must be found in its own object
    when the oracle names it, and entries that name neither page nor
    object (the Hidden and Metadata layers' locations) count as one
    occurrence per storage, and only where no layer placed that secret
    in an object; an occurrence on a page vouches for such content only
    on the page it sits on (the largest one-to-one
    matching decides); an instance with candidates the matching cannot
    cover is **ambiguous**, not a hit. A hard finding is a **hit**; a
    warning alone is a hit where review (exit `2`) is expected and a
    **downgrade** where a finding is. A file that exits non-zero for
    another secret does not excuse an instance.
  - **flagged** (owner decision, 2026-10-15): in a ⚑ cell (found but
    unreadable) the verifier reports a place, by design with no rule.
    Its place warning counts as caught, reported apart from hits, only
    when its code fits the cell's column (`LEFTOVER_UNDECODABLE_TEXT`
    for font, `LEFTOVER_IMAGE` for pixels, `LEFTOVER_CONTAINER`), the
    instance expects exit `2`, and the instance's object, from the
    oracle's evidence, is in the warning's object list. A ⚑ cell whose
    warning names a page instead (coordinator decision, 2026-10-15,
    extending the owner's rule): `thumbnail.pixels` is caught only by
    `PAGE_THUMBNAIL`, with exit `2` expected, naming the instance's exact
    page -- another page, or none, is a miss. Two blind spots,
    both on the safe side (a miss, never a false catch): the verifier
    lists at most eight objects and then "(+N more)", and
    `LEFTOVER_CONTAINER` lists payloads, not objects, so a container
    instance is never flagged. Read such misses as limits of the
    warning's text, not necessarily of the verifier.
  - **listed gaps** (owner decision, 2026-10-15): a missed name written
    in another form than the configured one (word order, case) and a
    missed piece of a name (a whole word of it, in an email address,
    say) are listed gaps, counted apart and not blocking U2; the README
    tells users to configure every written form of a name. Other
    misses, and known-gap cells (counted by cell), still need the
    owner's acceptance.
  - split values: the verifier keeps a value split across lines,
    columns or a page break at review (exit `2`); where the owner's
    residue rule makes the visible piece a leak (exit `1` expected),
    that is a downgrade, reported, not gated, and the case library
    keeps its labels (owner decision, 2026-10-15).
  - U3: of the `m2` outputs whose truth is clean, the clean control
    apart, the share that exits `0`, with the warning codes behind every
    exit `2`. The owner's targets (2026-10-14): U3 100% -- one review
    flag on a correctly redacted file blocks milestone U -- and false
    hard 0; every run reports PASS or FAIL against both.

  Only counts, with the lock's sha256 and the seeds measured, reach
  stdout and `--json` (a rules file whose exact values are not the
  persona's is refused); per-file rows go to `results/<date>.json` in the
  local root. A run in which any output gets no verdict (a crash, a
  timeout, an error report) or was scanned without OCR exits `1`, writes
  no `--json`, counts those outputs' instances as unmeasured, and is not
  recorded; a complete run appends the lock's sha256 to
  `results/lock-history.json`, and a lock that differs from the last one
  measured (relabelled after the verifier ran) needs
  `--lock-change-reason`, which the history keeps. `--seed` restricts it
  to one seed's documents. The owner's first run is 20 templates at seed
  0 (owner decision, 2026-10-12): a second seed generated locally is out
  of scope, and its drafts are kept outside `adjudication/`, unlocked
  and unmeasured. The persona's first SSN was the repo's sequential
  123-45-6789 until 2026-10-15: it matched a form's "Line 1" ... "Line 9"
  labels read across lines and a font's bytes, sending correctly
  redacted outputs to review for reasons no real SSN would, so the set
  was regenerated with 666-51-4073 (owner decision).

Nothing in `redactor_set/` imports the verifier or PyMuPDF
(`tests/test_redactor_set_generator.py` and
`tests/test_redactor_set_oracle.py` check it).

### The blind red-team slot (`caselib/redteam/`)

Every other case is written by someone who has read `verify.py`, which
is exactly the bias this corpus needs correcting for. `redteam/<round>/`
holds rounds built by someone working only from `COVERAGE.md` and the
threat model — see [`redteam/README.md`](caselib/redteam/README.md) for
the full protocol: how a round is authored (and what it must attest —
author, date, the `COVERAGE.md` commit it was given, a no-code-access
statement, a `README.md`; `families/redteam.py`'s `check_attestation`
enforces this at load time), why its `labels.json` is frozen (a sha256
in `round.json`, changeable only through a recorded `adjudications.log`
entry), and how a round may report a storage place COVERAGE.md has no
row for yet through the `new.<slug>` placeholder namespace
(`cells.NEW_CELL_ALLOWLIST`, redteam-only). Red-team cases load through
the ordinary family mechanism (`families/redteam.py`) and land in the
same `REGISTRY` with `origin="redteam"` — an invariant
(`TestRedTeam.test_origin_matches_the_loader`) checks that the set of
ids claiming that origin equals exactly the ids every round's
`labels.json` lists **on disk**, recomputed independently there rather
than read back from the loader's own (mutable, in-process)
`REGISTERED_IDS` bookkeeping — since nothing else stops an ordinary
family from setting `origin="redteam"` on its own, and trusting
`REGISTERED_IDS` alone would only catch a bypass the loader's own code
happened to also get wrong the same way.

`round.json`'s `labels_sha256` can be checked for internal consistency
by a test that only sees one commit — but nothing stops that same
commit from moving `labels_sha256` **and** `initial_labels_sha256`
together, since the test would just compare the new file against
itself. See "The ratchet check" below for how that's actually caught —
including the confirmed bypass of also renaming the round's directory so
a naive comparison-by-path never notices the old anchor is gone.

### Gallery fields ratchet

A leak case is shown in a gallery of how redaction fails, so it should
carry `mistake` (what caused it) and `recovery` (how it's found by
hand). `model.GALLERY_FIELDS_PENDING` is a may-only-shrink allowlist —
the same shape as `UNDOCUMENTED_GAPS` — for the leak cases that don't
carry both yet; `tests/test_case_library.py`'s `TestGalleryFields`
checks it is exact (nothing missing is left off it, and nothing on it is
actually filled in already).

### The ratchet check (`eval/check_ratchets.py`)

`UNDOCUMENTED_GAPS`, `NONFITZ_PENDING` and `GALLERY_FIELDS_PENDING` may
only shrink; a red-team round's `initial_labels_sha256` may never move.
Each is checked for internal consistency by
`tests/test_case_library.py` — but that suite only ever sees one
checkout, so nothing there stops a single commit from editing one of
these *and* loosening its own check to match, in lockstep. Catching that
needs a second, different commit to compare against:

`eval/check_ratchets.py` diffs every ratchet against the merge-base with
`main` (a pull request) or `HEAD~1` (a direct push to `main`, which
only catches a single-commit rewrite — see the script's docstring for
what that does and doesn't cover) and fails if anything grew, if an
`initial_labels_sha256` moved, if a ratchet the base's own registry
lists is no longer checked (`check_registry`, below), or if the base
can't be read well enough to tell. It is CI's `ratchets` job
(`.github/workflows/tests.yml`, needs `fetch-depth: 0` for the history to
diff against) and can also be run locally:

```bash
python eval/check_ratchets.py                 # resolves the base itself
python eval/check_ratchets.py --base <ref>     # or name one explicitly
```

The check reads each ratchet statically from its module's source,
never by importing it, so it must be sure the literal it reads is the
value Python ends up binding. It fails closed ("cannot verify") unless a
ratchet name has **exactly one** possible writer anywhere in its module,
and that writer is a top-level `NAME = frozenset({...})` (or annotated)
literal assignment. Everything in the module is walked, not just its top
level, and anything that could bind the name counts as a writer: a
store or delete in any block (`if`/`try`/`with`/`for`/`while`, a class
body, a function), tuple or starred unpacking, `for`/`with ... as`/
walrus targets, augmented assignment, `global NAME`, `def`/`class NAME`,
an import binding it (including an alias), `except ... as NAME`, a
`match` capture, a parameter or keyword argument named it
(`globals().update(NAME=...)`), any star import, and any string or bytes
constant containing it other than a docstring (`globals()["NAME"]`,
`vars()`/`locals()` subscripts, `setattr(sys.modules[__name__], "NAME",
...)`, `exec("NAME = ...")`). Rebinding `frozenset` itself also fails.
The string rule is deliberately broad: even an error message or comment
string that mentions the name outside a docstring counts, and the
failure names each writer and why it counts ("a string constant
mentioning UNDOCUMENTED_GAPS at line 12", "def UNDOCUMENTED_GAPS at line
40", ...), so a harmless one is quick to spot and reword.
Consumers import the sets through the `caselib` package, so
`eval/caselib/__init__.py` is checked too (`check_reexports`): it may
bind a ratchet name only by re-exporting it unchanged
(`from .cells import UNDOCUMENTED_GAPS`), nothing else.

The tests that use the sets (`test_every_gap_has_a_pinned_case`,
`test_claimed_cells_have_non_fitz_evidence`, `TestGalleryFields`) don't
read an imported global: they call `_verified(NAME)`, which parses the
literal from the defining file with the same `_frozenset_literal` — the
value this check verified — so nothing that rebinds a global at runtime
changes what they compare against. Separately,
`test_ratchet_sets_in_effect_equal_their_literal` asserts that
`caselib.<NAME>` and the defining module's attribute (after every family
has loaded) equal that literal, which catches a rebind done while caselib
imports (a name built at runtime, `exec` of a computed string) for any
other importer. It cannot catch code that rebinds them later.

The base's own list of ratchets counts too (`check_registry`): the
`RATCHET_SETS` and `RATCHET_REEXPORTS` in the base's copy of
`eval/check_ratchets.py` — read from the base ref, not the running
script — must all still be checked. Deleting an entry, or renaming or
moving a set along with a matching entry, would otherwise pass ("nothing
to shrink from") and let the set grow unseen. A ratchet the base lists
must also be readable in the base, so a set that is missing there was
renamed or moved, not introduced. An unreadable base registry fails, and
so does a base that has ratchet files but no copy of the checker at all
(only a base that predates the ratchets entirely has nothing to compare).
**Retiring or renaming a ratchet is unsupported by design**: an emptied
ratchet stays, with its test, for good — it costs nothing and guards the
"no exceptions" state. Adding a new ratchet is always fine.

**Threat model.** The ratchets guard against accidental or unreviewed
growth: a set grown by any ordinary edit, a rebind in some block or
form the author didn't think of, a rename, move or removal of a ratchet,
or a re-export that changes it. They do not defend against deliberately
malicious code in a pull request: edits to this checker's own logic, or
runtime code planted to mutate state the tests read (monkeypatching
`_frozenset_literal`, say). Both are visible in the diff, and adversarial
review of every pull request is mandatory on this repository; that is
what those rely on.

Its comparison logic (`run_all_checks` and friends) takes an abstract
"tree" (`read(path)`, `glob(pattern)`), so `tests/test_check_ratchets.py`
exercises it against fake in-memory trees — no git, no filesystem —
independently of `GitTree`'s subprocess calls.

Two properties a confirmation review specifically probed and confirmed
were missing, both now closed:

- **A round is matched by identity, not by path.** `check_redteam_anchors`
  reads round.json's own `"round"` field, not the directory it lives in.
  `git mv`-ing a round's directory changes nothing by itself; `git
  mv`-ing it *and* forging a fresh `initial_labels_sha256` at the new
  path is still caught, because the old anchor is looked up by identity
  and found regardless of where the file now lives. A round whose
  identity disappears between the two trees entirely — deleted, or its
  `"round"` field itself changed, indistinguishable from delete-and-
  recreate — is always a failure: a round's history may never simply
  vanish.
- **An unresolvable base fails closed in CI.** If `eval/check_ratchets.py`
  can't work out what to diff against (`origin/main` unreachable, no
  merge-base, no `HEAD~1`), that used to print a message and exit `0` —
  silently skipping the entire check. Now `is_ci_context` (`GITHUB_ACTIONS
  =true`, or `GITHUB_BASE_REF` / `GITHUB_EVENT_NAME` set at all) decides:
  inside anything that looks like CI, no base to compare against is a
  hard failure; only a genuinely local invocation with none of those
  variables set may skip cleanly.

## Running

```bash
pytest tests/test_case_library.py
```

Build and judge cases outside pytest, keeping the PDFs and reports:

```bash
PYTHONPATH=eval:. python -m caselib.run /tmp/cases              # all
PYTHONPATH=eval:. python -m caselib.run /tmp/cases page.visible  # some
```

The case library is also how `--visible-map` proves it changes no
verdict: `tests/test_visible_map_cli.py` runs one case from every
(family, row, truth) cell with the flag off and on and compares exit
code, stdout and the `--json` report, and `tests/test_visible_map.py`
does the same in-process with and without a sink at the scan's hooks.
`RUN_MAP_IDENTITY=1` runs every buildable case instead (grids included;
`perf` cases stay excluded):

```bash
RUN_MAP_IDENTITY=1 pytest tests/test_visible_map_cli.py tests/test_visible_map.py -k case_library
```

`tests/test_views_text_readings.py` also builds every case (`perf` ones
included) and checks, page by page, that `views.text.visual_text_glyphs`
reads exactly what `extract_visual_text` reads.

## The build lock

`caselib/cases.lock.json` holds each case's PDF hash, so a change in what
a generator writes (an edit, a PyMuPDF upgrade) shows up in review. Builds
are reproducible: no new file IDs, UTC, date values pinned, and payloads
that must be compressed precomputed (compressor output varies between
zlib versions). After changing a case, regenerate it:

```bash
PYTHONPATH=eval:. python -m caselib.lock
```

Perf cases (below) are excluded (`caselib.lock.lockable`): they are large
and slow on purpose, and nothing reviews their bytes line by line the way
it does an ordinary case's.

## The scorecard (`scorecard/`)

The scorecard (docs/REDESIGN.md §5, Phase 0c) is the case library run a
different way: instead of judging each case against its label in-process
(`caselib.run`, above, used by `tests/test_case_library.py`), it runs the
verifier's real CLI as a subprocess per file, with a timeout — the same
runner for pytest's speed and the scorecard's realism, just pointed at
two different git refs.

**Differential**: compare the working tree (the candidate) against the
pinned `eval-ref-0` tag (the reference, checked out into a disposable
worktree — never the live code) on a normalised key: exit, `error.code`,
findings as (rule, tier, storage), review warnings as (rule, "review",
storage, adjacency), other warnings as (code, layer, tool) — each as a
*multiset*, so losing one of two otherwise-identical findings (the same
rule in two separate orphaned streams) is a real, visible difference.
The reference runs with a `qpdf` shim first on its `PATH` that adds
`--deterministic-id` (`scorecard.differential.deterministic_qpdf_env`):
`eval-ref-0` predates that flag, so its qpdf output carried a trailer
`/ID` seeded by the current second, which the case library's `SHORT`
rule (`45-67` → `4567`) matched about once in 2,000 scans — a warning
that came and went with the clock. The shim removes only that input; the
candidate passes the flag itself. The candidate also blanks the `/ID`
elements qpdf generates before its Binary sweep (CHANGELOG, "Binary
layer: the `/ID` qpdf generates is not scanned"), which the reference
cannot: a case whose generated `/ID` spelled a value rule would be a
stable "warning removed" difference, listed with that reason (and as
weaker only when it moves the case's exit to 0: weaker is judged by exit
code).
Exact legacy warning codes and messages are deliberately not part of the
key. `OCR_UNAVAILABLE`/`TOOL_MISSING` warnings are dropped and the exit
recomputed without them first (mirroring `caselib.run.judge`), so the key
is comparable across environments — a case that goes from a clean exit
`0` to a flagged `2` compares the same on a full-environment run and on
Linux, where an unrelated `OCR_UNAVAILABLE` would otherwise already sit
on both sides. `REQUIRE_FULL_ENV=1` disables this at the orchestration
layer (`scorecard.differential.normalization_have`) — a missing tool
there is a bug, not noise to drop; `keys.is_environmental` itself never
reads that variable, so it stays a pure function of its arguments.

Two guards keep this recomputation from ever manufacturing a verdict
that hides a real bug: an **error** report (`report["error"]` set, e.g.
`PDF_PASSWORD`) is never touched, since the crash escape hatch doesn't
follow the findings/warnings formula at all; and the *unfiltered* report
must already be **self-consistent** with its own findings/warnings
(`1` if findings, else `2` if any warning, else `0`) before anything is
recomputed — a fail-open bug reporting `0` despite a real warning stays
`0`, a real, visible difference, rather than being "fixed" by
recomputing from the same warnings the report itself failed to act on.
The process's own exit code is checked against the report's claimed
`exit_code` too (`CliResult.crashed`, mirroring `judge`'s "report says
exit X, process Y"); a mismatch is a crash, not a report to normalise.

```bash
PYTHONPATH=eval:. python -m scorecard diff                      # every case
PYTHONPATH=eval:. python -m scorecard diff page.visible          # some
PYTHONPATH=eval:. python -m scorecard diff --json /tmp/diff.json --verbose
```

Exits non-zero if any case crashed or timed out, changed in a way not
listed in `eval/accepted_diffs.yaml`, an accepted entry is now stale (see
below), or zero cases were compared at all (every id given was unknown,
or nothing in the library matches this environment — a silent 0/0 would
otherwise look identical to a clean pass). The default "every case"
selection also skips any case marked `perf` (a large, multi-minute
generator such as a 300-page scan) unless you name it explicitly — it
would very likely time out at `--timeout` (default 60s) otherwise, which
reads as a crash rather than a deliberate skip.

`--timeout` is wall-clock, so it measures a file's own runtime only while
each scan has a core and its memory to itself: `--workers` defaults to
one per usable core (the CPU-affinity mask, where Python exposes it), and
no more than one per GiB of physical RAM (a scan peaks at 0.3–0.5 GB).
Neither a container's CPU quota nor its memory limit is seen: pass
`--workers` there. Do not raise it past that — extra workers do not finish
sooner, they only stretch each file's wall time toward the timeout. On
the weekly macOS runner (3 cores, 7 GB, OCR on the CPU at about 1–2 s a
page), 12 workers turned four-page cases that take about 9 s alone into
45–68 s, sent the VM into swap, timed eight cases out, and made a pass
over the library take 11.4 minutes instead of 6.7.

A case that is slow on purpose, but is not a `perf` file, carries its own
limit, `scan_timeout` (seconds, `caselib/model.py`): the scorecard's
`diff` and `metrics` give that case the larger of it and `--timeout`.
Only `page.dense-sideways-scan` (K43) has one, 300 s, and a test lists
the cases that do, so adding one is a reviewed change. A page full of
vertical lines gets the most OCR work the readings allow (#70's upright
re-reads plus #91's turned and unstacked readings): 78 Vision calls, about 8 s alone on Apple silicon (5.5 s of it Vision) but 151 s on the
weekly runner, where the reference takes 10 s. It timed out at 60 s
there (#147). It stays a regular case rather than `perf`, so pytest,
including `full-macos` on every PR, still judges it.

**`eval/accepted_diffs.yaml`**: every intended per-case change between
the reference and the candidate — case id, the reference's normalised
key ("old"), the candidate's ("new"), a reason, the COVERAGE.md cell, and
the PR. A difference the file does not list exactly (both sides) fails
the differential. When a fix or a regression changes a case's key,
`scorecard diff --json` prints both keys for the case; copy them into a
new entry (or update the existing one, if this is a further change to a
case already listed) with a reason for the change, and review it in the
PR like any other test change.

Two rules keep the file honest:

- **Never less strict, silently.** This project's severity order is
  `0 < 2 < 1` (clean, then cannot-certify, then confirmed leak). An
  entry that moves the *other* way — `1`→`2`, `1`→`0`, or `2`→`0` — must
  say so with `weaker: true`; loading the file without that flag on such
  an entry is an error. `scorecard diff` prints every matched weaker
  entry in its own **WEAKER** section so it cannot be missed in review.
- **No stale entries.** An entry that no case in a full run actually
  produced is itself a gate failure — either the case id no longer
  exists in the case library, or it does exist, ran this time, and still
  didn't produce that change. An entry for a case that exists but was
  skipped *this run* only because the environment can't run it
  (`requires: ocr` on Linux, say) is not flagged: it may be exactly
  right on the environment that does run it.

**Environment scope (`env`).** The same change can normalise to
different keys with and without OCR: on the weekly macOS run, Vision
adds its own review warnings to both sides of a case the Text layer
changed (#147). An entry may therefore say `env: ocr` or `env: no-ocr`,
the case library's `requires` vocabulary; any other value, or `env:` with
no value, is refused when the file loads, and an entry without the field
applies in every environment. Which one a run is in is
`caselib.run.available()`: `ocr` when the Vision bridge imports.

- An entry out of scope for this run neither matches a difference nor
  counts as stale here. It is checked where it is in scope: `no-ocr`
  entries by `scorecard-diff` on every PR, `ocr` entries only by the
  weekly job, so record `ocr` keys from that job's JSON artifact (on
  macOS 27, `eval-ref-0` fails every OCR page, so a local run there does
  not give the reference's keys).
- An entry for a case id the library no longer has is stale in every
  environment, in scope or not.
- An entry scoped to an environment takes the place of the same case's
  *unscoped* entries in that environment; they still match, and are still
  checked for staleness, everywhere else. This is how an existing entry is
  narrowed without editing it, since the file is add-only (docs/REDESIGN.md
  §7a): append a scoped entry for the environment where the old one does
  not hold. PR #68's eight `layout.two-column-wrap`,
  `layout.raw-font-coded-two-column-wrap` and `layout.repeated-line-wraps-*`
  entries were recorded without OCR and are left as they are; the `env: ocr`
  entries for the same cases appended in #147 replace them with OCR.
- A case whose entries are scoped to both environments cannot also have
  an unscoped one (it would be in scope nowhere, so never checked); the
  file is refused. A test on the committed file also refuses an entry
  scoped to an environment its case can never run in (`env: ocr` on a
  `requires: no-ocr` case), and an unscoped entry replaced in the only
  environment its case runs in.

Scoping only ever takes entries out of a run, never adds one, so it
cannot accept a difference the file would have refused without it. The
other rules apply to a scoped entry unchanged: an exact old/new match,
and `weaker: true` with an owner decision for any loosening.

**Metrics**: the same case library, run through the candidate CLI alone
and judged against case labels — no reference needed:

```bash
PYTHONPATH=eval:. python -m scorecard metrics --json /tmp/metrics.json
```

Reports silent miss (leak case, exit 0), downgrade (expected 1, got 2),
false hard (clean case with a hard finding), review rate (clean case,
exit 2), crashes, timeouts, and runtime p50/p95 — all against `expected`
labels, so a documented `known_gap` still counts as a miss here (that is
the point: it tracks how many gaps are left, not just whether they are
labelled).

A case with no verdict — the CLI crashed (bad exit code, no or invalid
report, or a report whose exit disagrees with the process's), timed out,
or reported an `error` saying it could not complete the scan — is either
left out of every rate above or counted with a verdict it never earned. `metrics`
therefore exits 1 when any case has no verdict (each listed on stderr),
or when zero cases ran, and still prints the table and writes `--json`.
A known gap or any other labelled miss does not fail it — those are what
it measures.

**Reference result caching**: `--cache-dir DIR` skips the reference
worktree and subprocess runs entirely for any case whose result is
already cached under today's key: the reference commit, a hash of
`caselib/cases.lock.json`, a hash of every `eval/scorecard/*.py` file
(so the cache invalidates itself the moment the comparison logic
changes, not just when someone remembers to bump a version), and this
machine's qpdf/exiftool versions, OCR availability, the PyMuPDF version,
and whether `REQUIRE_FULL_ENV` is set (it changes what `have` gets
passed to `normalize`, so it changes the stored keys, not just this
machine's raw capability) — a cache entry from a different environment
or mode is never reused. On top of that per-run key, each cached case
also carries its own `input_hash` — `sha256(pdf bytes + json(rules,
sort_keys=True))` — so changing that one case's rules (or a generator
producing different bytes the build lock didn't catch) invalidates just
that entry instead of silently serving a reference result for different
input. A crashed or timed-out reference run is never written to the
cache (and never trusted back out of it), so one flaky run doesn't
poison every run after it. CI passes a persistent cache directory
(`actions/cache`) so most PRs never re-run the reference at all.

### The real-world corpus (local only)

Clean-side metrics (false hard, review rate) on real files, stratified
by whether they carry a text layer, have more than one revision, and
producer family. Nothing here is committed: the manifest and results
live under `eval/scorecard/real_corpus/` (gitignored) — every `--manifest`
and `--out` path is refused outside it unless you pass `--allow-outside`
(for, say, an external drive; you are then responsible for never
committing or sharing that path). Entries are keyed by SHA-256, with
paths relative to a root directory you configure — that root itself is
never recorded anywhere, but **the manifest's own `path` field is a
relative file name from it**, so the manifest is exactly as sensitive as
a directory listing of your corpus and must never be committed, emailed,
or pasted anywhere. Only the coarse producer *family* (e.g. `"acrobat"`,
`"office"`) is stored — the raw `/Producer` string is read, classified,
and discarded on the spot, since real producer apps sometimes embed a
username, hostname, or email address in it.

`corpus build` records every regular file under the root whose name ends
in `.pdf` in any case (`.PDF`, `.Pdf`, ...); a symbolic link to a file
counts as that file. It prints how many entries it left out, by kind and
never by name: files without a `.pdf` extension, entries named `.pdf`
that are not regular files (a broken link, say), symbolic links to
directories (not followed, so a link cycle cannot loop the walk),
directories it could not list, and `.pdf` files it could not read (any
I/O error, a permission error say: such a file cannot be hashed, so it
is left out, and the error's message, which names the file, is never
printed). A `--root` that does not exist, is not a directory, or cannot
be accessed or listed is refused, and so is a root with no readable PDF:
the build never replaces a manifest with an empty one, and leaves the
existing one as it was. The manifest is written to a fresh temporary
file beside it (mode 0600, or the existing manifest's mode) and renamed
into place. It is replaced, not rewritten (a second hard link to it
keeps the old content), so a failed write leaves the old manifest and no
temporary file; a symbolic link at `--manifest` is written through. A
write-protected manifest, or one in a directory that cannot be written,
is refused and left as it was.

A file MuPDF cannot read is recorded, not fatal: `corpus build` used to
abort on the first password-protected or corrupt file, so the exemption
below could never be reached. Each entry now carries `probe`: `"ok"`,
`"password"` (opens but needs a password: the CLI's `PDF_PASSWORD`),
`"unreadable"` (MuPDF cannot open it: the CLI's `PDF_UNREADABLE`) or
`"text-unreadable"` (opens, but reading a page's text fails; the CLI has
no matching error code and scans it normally). Such a file's
`text_bearing` is `null`, unknown, and its producer family `"unknown"`,
since neither was read; multi-revision is still measured from the raw
bytes. `corpus run` counts a `"password"` or `"unreadable"` file only in
`all` (its exit 2 is a review verdict, as below), in `text-unknown`, and
in `probe:password` or `probe:unreadable` — never in a measured stratum
(text-bearing, text-free, the revision strata or a producer family). The
CLI's error report on it has no findings, so there it would only add an
unscanned file to the false-hard denominator: as text-bearing, of the
stratum the redesign's targets are measured on. `all` does include them,
so its false-hard denominator counts files nothing scanned: read false
hard on a measured stratum. A `"text-unreadable"` file is scanned, but
no text stratum can hold it, so it is its own counted group (owner
decision, 2026-10-12): `all`, `text-unknown` and `probe:text-unreadable`
only, and the run passes. `probe:text-unreadable` is always in the
table and `--json`, even at 0, and the run always prints the count.

That placement is decided by the build-time probe, so `corpus run` exits
1 when the scan does not bear the probe out, listing each such file by
SHA-256 prefix (`probe` lines): a `"password"` or `"unreadable"` file the
CLI did not report as `PDF_PASSWORD` / `PDF_UNREADABLE` (a PyMuPDF
upgrade can make a file readable, and its findings would otherwise
vanish from every measured stratum), and an `"ok"` or
`"text-unreadable"` file the CLI could not open. Rebuild the manifest
for them. The build
prints only how many there were, never which, and never keeps the
exception's message (it can name the path). A manifest written before
the field existed loads with `probe` `"ok"`; one written now does not
load in older code (it rejects the new field), so rebuild it after
switching back. An unknown `probe` value, a `text_bearing` that is not
`true`, `false` or `null`, or a pair that disagrees (`text_bearing` is
known exactly when the probe is `"ok"`), is refused, not made a stratum.

```bash
# Build the manifest from a directory of real PDFs (never committed):
PYTHONPATH=eval:. python -m scorecard corpus build --root ~/corpus

# Report files the manifest expects that are missing or have changed:
PYTHONPATH=eval:. python -m scorecard corpus check --root ~/corpus

# Scan every manifest file, report clean-side metrics per stratum:
PYTHONPATH=eval:. python -m scorecard corpus run --root ~/corpus \
    --secrets secrets.json --json /tmp/corpus.json
```

`corpus run` exits 1 when a manifest file is missing, changed or vanishes
mid-run (it scans only the rest), when any scan has no verdict as above,
when a scan contradicts the build-time probe (above), or when nothing
was scanned — each would leave the clean-side rates
describing something other than the manifest. The per-file lines name a
file by its SHA-256 prefix; the `MISSING`/`CHANGED` lines before them give its
manifest path, since that is what you fix. One exception (owner's
decision): a file the CLI cannot open — `PDF_PASSWORD` (encrypted) or
`PDF_UNREADABLE` (corrupt) — gets exit 2, which is the tool's real,
fail-closed answer for that file, so it counts as a review verdict and is
only noted on stderr. It still prints the table and writes `--json`.

`corpus run` also writes a **plaintext copy of `--secrets`** into `--out`
per file scanned (`<sha256-prefix>.rules.json` — the same scratch file the
CLI itself reads), so whatever values `--secrets` names live there too,
not just in the file you pointed at. `--out` defaults under
`eval/scorecard/real_corpus/scan-workdir` (gitignored, guarded the same
way as `--manifest`) for exactly this reason.

**Use a non-sequential value in `--secrets`.** The case library's SSN,
`123-45-6789`, is deliberately easy to read, but its digits are in order,
and real files hold digit tables: a hex string that decodes to a character
table beginning `0123456789`, or a run of one-character strings `1`, `2`,
... `9` (for example, a font's character-code table), which the Objects layer reads back
to back so that a value split across strings is still caught. Both contain
`123456789`, so a corpus run with the case library's value reports false
hard findings that a real value never would (eval/spikes/RESULTS.md,
"Value false hards with a sequential test value"). A value such as
`666-51-4073` does not collide.

### Pattern-class false-hard baseline (Phase 5, measurement only)

Phase 5's gate is "built-in pattern false hard < 1% on text-bearing real
files" (docs/REDESIGN.md §7). ADR 0005's 23.1% scanned page text only;
`scorecard.pattern_baseline` measures the whole CLI, every layer:

```bash
PYTHONPATH=eval:. python -m scorecard.pattern_baseline \
    --root eval/scorecard/real_corpus/links --workers 4 --timeout 300 \
    --json /tmp/pattern-baseline.json
```

Each manifest file is scanned with `builtin_classes.rules.json` — the
four built-in classes (`ssn`, `credit-card`, `email`, `us-phone`) and no
value — so on this corpus, which holds no planted secret, every hard
finding is a false hard (an `email` match is a true positive for "an
address is here", ADR 0005, and is reported but kept out of the
digit-class unions). The CLI is run through `scorecard/pattern_recorder.py`:
`verify.py` itself, same arguments, exit status and report, with
`match_patterns` — the one function every pattern match in the tool goes
through — wrapped. For every call that could yield a hard finding (no
`collapse_separators`; a collapsed call is the review tier) the wrapper
classifies **every** validated match at its exact span into a **shape**
(`scorecard/match_shapes.py`), not only the first one the tool reports:
`formatted` (separators grouping the digits as the class's real values
are — `3-2-4`, `3-3-4`, fours — decided first, so "SSN123-45-6789" stays
`formatted`), `uuid`, `hex-id`, `date-or-time` (only with context: a PDF
`D:` date, a `YYYYMMDDhhmm` timestamp, epoch milliseconds — never epoch
seconds, which are also every phone number in area codes 201–220),
`alnum-id`, `number-list` (separators, grouped otherwise), `bare-digits`
or `email-address`. A rule is classified as the built-in class whose
regex it is, whatever its name (the case library's "Any SSN" is `ssn`);
a custom pattern fits no class-specific shape. Where the evidence is thin
it errs toward the shapes no option below demotes. The record file holds only shapes and, per hit,
a key salted with a per-run secret that is never written (the sample in
it masked exactly as the report masks it: with the scan's exact values,
and fully in the OCR layer), so neither the
matched text nor the masked sample can be recovered from it. Each hard
finding in the `--json` report is joined to its hit's shape through that
key (several shapes on one key: the one kept longest below); a finding
with no record counts as `unjoined`, which should stay 0. A wrapper
failure can lose a record, never change a hit: `tests/test_pattern_baseline.py`
scans every case-library case that has a pattern rule with and without
the recorder installed and requires an identical report, and checks the
recorder's own CLI against `verify.py` on a file with findings, a clean
file and one it cannot open.

Output, to stdout and `--json`, is counts only, per stratum (all,
text-bearing, text-free, text-unknown): per class, files with a hard
finding (and their rate), by layer, by shape and by layer and shape, and
files with a review warning for the class; the unions `ssn|us-phone`,
`ssn|us-phone|credit-card` and `any built-in`; exit codes; and
unmeasured files by reason (a crash, a timeout or a CLI error such as an
encrypted file — never folded into a rate's denominator).

For each class and union, `residual_hard_files` **estimates** the files
that would still be hard if the matches of some shapes were demoted to
review — demoting the embedded shapes (`uuid`, `hex-id`, `alnum-id`) and
`date-or-time`; then also `number-list`; then also `bare-digits`, so only
`formatted` stays hard. Nothing is demoted. A file counts if it has a hard
finding of the class today and any match of that class the recorder saw
has a kept shape (or a finding of it has no known shape; an email address
is never demoted). The estimate errs toward keeping files hard — a
kept-shape match seen only in a call whose result is a review warning
still counts — with one blind spot the other way: once every rule has
hit, a layer's rolling scanner stops feeding the matcher, so a later
match there is never seen. Scratch reports, rules and record files go
under `real_corpus/pattern-baseline-workdir`. It changes no verdict.
Results: eval/spikes/RESULTS.md, "Phase 5 baseline".

**Recall cost** (`scorecard.pattern_recall`, Phase 5 PR 2a): the other
side of the same choice. Every case-library case with a built-in class
rule is scanned in-process with the recorder installed, and each of ADR
0012's options is applied to what it saw:

```bash
PYTHONPATH=eval:. python -m scorecard.pattern_recall [--json OUT]
```

For ADR 0012's demote-only options (per class, a set of shapes kept hard)
it prints, per option, the leak cases that exit 1 today and would move to
exit 2, as two bounds: `moved_at_least` (every class finding demoted
whichever of its recorded shapes is used, none unknown, and no other match
of the class in a hard-capable call kept) and `moved_at_most` (each finding
possibly demoted, an unknown shape counting as demoted); and the same upper
bound as if the cases' value rules were absent. For the owner's policy 1
(a class match no longer affects the exit code) it recomputes every case's
exit from its report without the class findings and class review warnings
and lists every case whose exit changes; a leak case reaching 0 needs the
owner's sign-off. It applies policy 1 to every case label too (expected,
known gap, older macOS; scanned or not): a label lists warning codes, not
their rule, so a review warning is the class's only when the case's rules
are all classes, and a label it cannot judge is settled by this scan's
report when that report satisfies the label exactly (`caselib.run.judge`;
listed as `settled_by_scan`), or reported as undetermined. It also prints the cases measured per class (a class with
none has no measured recall), how many exit-1 cases a value rule holds, the
cases expected at exit 1 that exit otherwise today (not judged), and any
finding of unknown or ambiguous shape. It exits 1 if a finding's shape is
unknown, a warning names a class rule without carrying it in `rule`, or a
label stays undetermined.
Case ids are fabricated and printed. Results: eval/spikes/RESULTS.md, "Phase 5 recall".

`scorecard.pattern_policy1_corpus` recomputes the baseline run's per-file
reports under the owner's policy 1 (ADR 0012: built-in class matches no
longer affect the exit code) and prints, per stratum, the exit codes today
and under policy 1 and the moves between them; counts only, from the
local reports, scanning nothing.

### The inventory agreement gate (Phase 3a-6, 3a-6b)

ADR 0010's gate for the inventory (`redaction_verifier.inventory`, in
shadow mode): on every file, `build_inventory` runs in a child process
with a 60 s timeout, then the reader differential
(`eval/scorecard/inventory.py`, the same oracle the tests use) compares
the inventory against MuPDF and qpdf -- the qpdf CLI for the object map
and `--check`, and libqpdf in-process through pikepdf for every value
(ADR 0010's amendment) -- in a second child with its own
timeout (default 900 s, reported separately; each qpdf call inside it
may use the whole budget, so a huge but valid file is not cut short). **The gate passes only
with zero unflagged disagreements** (a file that does not tile counts as
one), zero crashes, zero timeouts, and every unflagged file actually
verified: an oracle that crashes or times out on an unflagged file, or
two children that flag the same file differently, fail it too. Flagged
files, UNINDEXED/CONTESTED regions and flag rates are reported, never
gated (ADR 0010: measure before enforcing). Exit status: 0 pass, 1 fail,
2 refused (a drifted or empty manifest, no qpdf on PATH, or a qpdf CLI
whose major.minor version is not pikepdf's libqpdf's: their messages and
limits differ across releases, so the allowlists hold for one). A `--json`
file left from an earlier run is deleted first, so a stale aggregate
never passes for a fresh one.

```bash
# The real corpus: the manifest is verified first -- any file missing or
# changed since `corpus build` and the run is refused (exit 2, counts only;
# `corpus check` lists the files). Minutes on ~2,000 files with --workers 8
# (keep N below your core count, or the 60 s timeout measures contention).
# The corpus is the macOS system and application PDFs eval/spikes/corpus.py
# finds (never your home folder); `corpus build` wants one root, so stage
# hashed symlinks inside the gitignored real_corpus/ first -- names are
# hashed so no system path or file name reaches the manifest:
PYTHONPATH=eval:. python -c 'import hashlib, os, pathlib; from spikes import corpus
d = pathlib.Path("eval/scorecard/real_corpus/links"); d.mkdir(parents=True, exist_ok=True)
for p in corpus.discover():
    t = d / (hashlib.sha256(str(p).encode()).hexdigest()[:16] + ".pdf")
    if os.access(p, os.R_OK) and not t.exists(): os.symlink(p, t)'
PYTHONPATH=eval:. python -m scorecard corpus build --root eval/scorecard/real_corpus/links
PYTHONPATH=eval:. python -m scorecard inventory run --root eval/scorecard/real_corpus/links \
    --json /tmp/inventory-gate.json --workers 8

# The same gate over the case library (every case this machine can build)
# and over generated and mutated files (seeded, reproducible):
PYTHONPATH=eval:. python -m scorecard inventory caselib --json /tmp/caselib-gate.json
PYTHONPATH=eval:. python -m scorecard inventory fuzz --count 2000 --seed 0 \
    --json /tmp/fuzz-gate.json
```

What each part of the output means:

- `gate`: `passed`, and the gating counts: `unflagged_disagree` (by
  failed check in `unflagged_disagree_by_check`: `object_set` (qpdf
  could not open a revision at all, "qpdf exit 2", counts here too),
  `stream_data`, `object_value`, `member_value`, `trailer_value`,
  `libqpdf_warning`, `dead_bodies`, `qpdf_check` (a WARNING or ERROR
  line outside the written allowlist below), `mupdf_warning`,
  `needs_password`, `encryption`, `page_list` and `page_attribute`
  (since 3a-7b: each revision's pages in order, and each page's
  inherited `/Resources`, `/MediaBox`, `/CropBox`, `/Rotate` as written,
  against MuPDF's own inheritance and a second libqpdf instance that
  pushes them to the pages, owner decision 9; and the page as MuPDF
  shows it -- media box, crop box, rotation, rectangle -- against what
  those attributes say it is), `name_tree` (since 3a-7c: each
  revision's `/EmbeddedFiles` entries, keys as text and values as
  written, against MuPDF's `pdf_load_name_tree` and libqpdf's name-tree
  helper; an encrypted revision's keys are not compared until 3a-9, and
  its non-empty name trees are flagged), `annotation_list` and
  `annotation` (since 3a-7d, `eval/scorecard/annotations.py`, owner
  decision 11: each page's annotations as MuPDF lists them --
  `pdf_first_annot` then `pdf_first_widget`, Link and Popup in neither
  list -- against ours as a multiset by object number, by `/Annots`
  position for one written directly; then each annotation synthesized or
  not against MuPDF's drawing of it, read by the 3a-7d spike's method: a
  copy with rotation, `/F` and `/OC` cleared against the same copy with
  every annotation that has `/AP` replaced by a stand-in `/Square` drawn
  from it, each annotation's trace and 4x pixels compared, NOTHING
  agreeing with either reading; not exact below 4x. MuPDF's warnings while
  it loads and draws those two copies are the gate's own and are
  discarded; the file's own -- its pages loaded, its appearances
  synthesized -- are gated as before), `reachable` (since 3a-7e-1,
  coordinator decision Q5: each revision's reachable objects, ours
  exactly against what MuPDF keeps writing it with `tobytes(garbage=1)`
  and libqpdf keeps writing it in QDF mode, read from its `%% Original
  object ID` comments; the references a writer rewrites though its
  reader reads them -- a stream's indirect `/Length`, the trailer's
  `/Encrypt` and `/ID`, in qpdf a stream's `/DecodeParms` without
  `/Filter` (libqpdf reads it as written; its writer drops it, no filter
  using it) -- are
  added back as each reader reads them, and one whose target holds
  references of its own fails closed, `templates` naming the reader and
  "not exact"; ADR 0011's notes of 2026-10-15, 3a-7e-1),
  `oracle_error`, `tiling`),
  `crashes`, `timeouts`, `unverified`, `inconsistent`. A file that needs
  a user password cannot be certified, so it is **flagged**, not a
  disagreement and not exempt (owner decision, 2026-10-12, #48 item 3):
  it no longer fails the gate. It is reported under the reason
  `needs_password` in `flags`, or under the inventory's own reasons when
  it has any, so `files_by_reason.needs_password` counts only files with
  no other flag. The oracle stops at the first revision that needs a
  password, so nothing after it is compared; the file fails the gate
  only if it does not tile (a disagreement, listed under
  `needs_password` here) or the two children flag it differently
  (`inconsistent`). The oracle reports `needs_password` only when our
  trailer also reads that revision encrypted; a file MuPDF wants a
  password for that our trailer reads as plain is an `encryption`
  disagreement, which gates.
- `unflagged_agree`, and `unflagged_agree_encrypted`: encrypted files
  whose object sets agree but whose bytes and values are not compared
  until 3a-8/9. A revision is exempted only when our trailer, MuPDF and
  qpdf all read it encrypted; any split (an `/Encrypt null` both readers
  ignore, say) is an `encryption` disagreement. `encrypted_files` counts
  files MuPDF reads encrypted, `encrypt_entry_files` those whose
  trailers carry an `/Encrypt` entry.
- `qpdf_check_errors_on_agree`: agreeing files where `qpdf --check`
  printed an `ERROR` line (exit 2) the allowlists cover (page-tree
  semantics until 3a-7b, which flags them; now only page content
  syntax). ERROR lines are gated like WARNING lines since 3a-6b.
- `flagged`, `flag_rate`, and `flags`: files per flag reason, the top ten
  reasons, and files with exactly one reason. A flagged file is never
  compared with the readers: the inventory said the readers may disagree,
  and a flagged file can never exit 0 once the inventory is wired in.
- `regions`: files, regions and bytes left UNINDEXED or CONTESTED in the
  file's own tiling (object streams' decoded tilings are not included).
- `timing`: percentiles of the build child's wall clock (interpreter
  start included), the oracle's, and the work units charged per byte;
  since 3a-7e-1 also `reachable_seconds`, the `reachable` check's share
  of the oracle's time on agreeing files (#52 item 4).
- `pending_decisions`: counts for the owner's open decisions --
  incremental updates to linearized files (#44 item 1: a linearized file
  with more than one revision; `/L` off the file's length is reported
  apart, since trailing bytes alone do that), comment lines
  claimed after the header or an intermediate `%%EOF` (#46 item 1: files,
  lines, the longest line in bytes, lines with non-printable bytes or an
  `N G obj`), streams whose indirect `/Length` lives in an object stream,
  `REVISION_AMBIGUOUS` flags whose value is equal in every revision, and
  dead object streams (#46 item 4). Decided 2026-10-03 (owner decision 7,
  docs/phase3a-plan.md): updated linearized files are canonical (unless
  their `/L` names the updated file's whole length), odd
  comment lines are flagged `COMMENT_LINE`, the rest stay flagged; the
  counts are still reported, to watch them. `labels` (since 3a-7e-1,
  ADR 0011 item 5; costs for the owner, R6 and R7): over the unflagged
  files' current revisions, files and streams with each `STRUCTURAL`
  kind, files whose hint stream sits under a stale `/L` (an updated
  linearized file: coordinator decision Q8), files with a revision
  without labels, and percentiles of `graph_work`, the work the
  reference graph's stages charged (ADR 0011 item 7). `labels.drawing`
  (since 3a-7e-2; `scorecard.inventory.drawing_measures`), each as files
  with any and the total over the unflagged files:
  - the current revision's live streams, `DRAWABLE` and `ORPHANED`
    (`streams_current`, `drawable_current`, `orphaned_current`);
  - per body across revisions: stream bodies, drawable, structural, both,
    orphaned (`bodies_*`);
  - orphans for the verdict that the current revision does not call
    orphaned (`orphaned_older_only`), and the converse
    (`orphaned_current_labelled_older`);
  - the current revision's orphans by class (`orphan_class_*`): the use
    kind of the path that first reaches each from the trailer (thumbnail,
    piece_info, appearance, properties, ...), `unreachable_source`,
    `unreferenced`, `objstm` (home to no live entry), and the verdict's
    `dead_body` and `older_body`; and the same classes over the verdict's
    orphans only, per body (`orphan_body_class_*`, since 3a-7e-3: an
    orphan of the current revision labelled in an older one is left out);
  - `/Metadata` streams orphaned on an annotation or an ICC profile
    (`orphan_metadata_*`, for 3a-7e-3's briefing);
  - the closure gap, measured and never gated: streams MuPDF lists on the
    current revision's pages that are not `DRAWABLE` (`closure_gap`, and by
    listing `closure_gap_contents`, `_images` (with soft masks),
    `_xobjects` and `_annotations` (`pdf_annot_ap`)), out of
    `mupdf_listed`. Any gap goes to an ADR 0011 note, never into the
    lists.

  `labels_cost` (since 3a-7e-3; `scorecard.inventory.cost_measures`;
  the owner's cost briefing, eval/spikes/RESULTS.md), over the unflagged
  files:
  - `exit_2_files`: files that would exit `2` once labels gate (3b's
    verdict; 4a's decoders), each counted once, and `exit_2_by_class`
    (`orphan` per body, `hint_stream`, `xmp_image` -- an image or
    binary --, `javascript`, `synthesized`, `output_intent`) and
    `exit_2_only_class` (files with one class only);
    `exit_2_files_marked_today`: those that already exit `2` today for a
    page thumbnail (`page_thumb_legacy`: #133's `PAGE_THUMBNAIL`, or its
    check failing, read without importing verify.py) -- a lower bound on
    the overlap with today's exit `2`, which 3a-12 measures in full
    (`eval/spikes/unit_budget.py`; RESULTS.md, "Phase 3a-12");
    `exit_2_files_but_thumbnails_marked_today` leaves out only the files
    whose sole class is page-thumbnail orphans so marked;
  - `orphan_files_current` beside the per-body count, and
    `orphan_body_classes` (files and streams per class);
  - `xmp`: the XMP packets labelled `STRUCTURAL`, per body across
    revisions, holding an image (`xmp_image_bodies`; `xmp_image_current`
    in the current revision) or binary nothing accounts for
    (`xmp_binary_*`), either (`xmp_flag_bodies`, the `xmp_image` class),
    or that the scan does not decode -- a filter other than Flate, or a
    run that does not decode (`xmp_undecoded_bodies`). The scan is eval's,
    not the inventory's: every XML numeric character reference
    (`&#xA;` and the like) is removed, then every base64, hex, ASCII85 or
    quoted-printable run of at least 48 decoded bytes is looked at for an image
    signature (JPEG, PNG, GIF, TIFF, BMP, JPEG 2000, JBIG2, WebP), else
    for bytes that are not text (ADR 0011 item 5's XMP bullet; R7);
  - `graph_work` against `graph_sum`, item 7's own sum over revisions of
    live objects and their edges (every revision, one whose results are
    reused included): percentiles of each, the sum of the file with the
    largest work, and work per unit of sum.

  `timing.reachable_share` (since 3a-7e-3): the `reachable` check's
  share of each agreeing file's oracle time, and over the run
  (`reachable_share_total`).
- `config`: provenance -- the git commit and whether the tree had local
  changes, the qpdf CLI, libqpdf, pikepdf, PyMuPDF, MuPDF and Python
  versions, the platform
  (OS, release, architecture), the start time (UTC) and the run's
  options.
- `compared`: how many streams, object-stream members and values (every
  live object, member and trailer, per revision) were compared. A value
  is compared with libqpdf exactly (kinds, string bytes, integers and
  reals as written, references by number and generation) and with MuPDF
  allowing a real one 32-bit float step from ours (MuPDF holds reals in
  32 bits); references are compared as written -- since 3a-7a the
  inventory flags any that does not resolve (`REF_DANGLING`,
  `REF_GENERATION`), so none reaches the comparison. `annotations` (since
  3a-7d): annotations listed and classified against MuPDF, per revision.
  `reachable_revisions` (since 3a-7e-1): revisions whose reachable set
  both readers confirmed.
  Any libqpdf warning while reading is a disagreement unless allowlisted.

The written allowlist of qpdf messages (`eval/scorecard/inventory.py`),
applied to `--check` lines and to libqpdf's warnings alike: a content
stream's own syntax (Phase 4's), as in 3a-6 -- a substring of a line, not
anchored (issue #46 tracks narrowing it; the page-tree substrings went
with 3a-7b, which flags any tree off the canonical shape, as did the
retyped-catalog exemption); and since 3a-6b, entries that each match one whole line on
the exact path, each with a reader-agreement test in
`tests/test_inventory_values.py`: linearization hint-table lint
(`LINEARIZATION_LINT`, only on `--check` lines of a file qpdf calls
linearized; 15 messages of qpdf's linearization checker:
readers do not read objects through hint tables), Flate data cut short
in a stream the inventory does not decode (`TRUNCATED_FLATE`, at that
stream's data start; content decoding is Phase 4's) -- the entry for a
reference to object 0 (`OBJECT_ZERO_REFERENCE`) went with 3a-7a, which
flags it `REF_DANGLING`; since 3a-6c, qpdf's warning on an in-use entry
at offset 0 (`OFFSET_ZERO`, tested in `tests/test_inventory_decisions.py`),
which the inventory reads as no object -- the oracle drops such an entry
from qpdf's map only after MuPDF and libqpdf have both read it as null.
And one whole-line entry on qpdf's own line, which carries no path
(issue #88): a page content stream's syntax error in a token starting at
byte 0, which qpdf prints with no offset, `page object N G stream N G
(content): ...` (one stream or an array of them; `CONTENT_AT_ZERO`) --
content syntax is Phase 4's, as for the `(content, offset N)` form.
Since 3a-7c, qpdf's warning on an empty name- or number-tree root
(`name/number tree node has neither non-empty /Names nor /Kids`, or
`/Nums`; `EMPTY_TREE_ROOT`, whole line on the exact path): both readers
read such a root as empty (owner decision R5), and the inventory flags
any empty node below a root, so on an unflagged file the line is a
root's; reader-agreement tests for `/EmbeddedFiles`, `/Dests` and
`/PageLabels` in `tests/test_inventory_trees.py`.

**What to share: the `--json` aggregate only.** It holds counts, rates
and timings -- no file name, path, SHA-256, document bytes or reader
message -- and so does stdout. Per-file detail goes to `--detail`
(default `eval/scorecard/real_corpus/inventory-gate-SOURCE.jsonl`, SOURCE
being `corpus`, `caselib` or `fuzz`, so one run never overwrites
another's; gitignored and guarded like `--manifest`): one line per file keyed by
SHA-256, with its verdict, flag reasons, the failed check and where
(revision, object number), timings, the measurements, and reader
messages only as scrubbed templates (numbers, names, strings and quoted
text replaced; a child's failure only by its exception class). It is as sensitive as
the manifest -- never commit or paste it; use it to find a file on your
own machine (`sha256sum`) when a number needs explaining.

### Where each gate runs

- **Linux CI, every PR**: the non-OCR case library and the differential
  against `eval-ref-0` (`.github/workflows/tests.yml`, job
  `scorecard-diff`) — cases needing OCR are skipped automatically (no
  Vision bridge on Linux), matching `requires: ocr`/`no-ocr` filtering.
- **Scheduled, weekly, macOS**: the full case library including grids,
  with OCR (`REQUIRE_FULL_ENV=1`), so the grids' OCR-free labels are
  checked to still hold with OCR present
  (`.github/workflows/scorecard-weekly.yml`). A run recently took 58-62
  minutes (pytest ~22, scorecard diff ~23, metrics ~12; about 35 when first
  measured, about 50 on 2026-10-12); the job's limit is 120. Each branch has its own queue, so
  a branch can dispatch it as its pre-merge macOS+OCR check (#147).
- Both macOS jobs — this one and `full-macos` in `tests.yml`, which runs
  the suite on every PR — use `macos-latest` (macOS 26 as of this
  writing), so a newer macOS is covered only by local runs. Run the case
  library locally after an OS upgrade: on macOS 27 that is how an OCR
  failure on every page was found, and then Vision reading a
  one-character-per-line stack as one line, which turned four
  `layout.vertical-stack-*` pattern-rule cases from manual review into a hard
  finding on a misread number (both fixed; see the CHANGELOG). `full-macos`
  skips the grids, so only the weekly job runs those cases with OCR. Two
  sides of that fix are also pinned outside the grids. `full-macos` runs
  both on every PR, but only the second is a real guard there:
  - `page.digit-column`, clean: a lone digit column must not give a hard
    finding. It runs everywhere, OCR or not, but only macOS 27 misreads
    the column, so only a local macOS 27 run catches a regression there;
    on CI's macOS 26 it passes even with the code before the fix.
  - `page.scan-image-sideways-90` and `-270`, `requires: ocr`: an SSN
    that exists only as a turned image must stay a hard OCR finding. CI
    guards this side on every PR.

  Separately, two OCR cases on scanned pages, `layout.scanned-vertical-stack`
  (K42, fixed) and the known gap `page.dense-sideways-scan` (K43)
  (`families/ocr_misses.py`), pin what Vision on macOS 27 returns. They
  are not grid cases, so `full-macos` runs them on macOS 26 too, where
  Vision reads both pages differently: K42 still exits `0` there and
  K43 exits `1`. Each case says so in its `older_macos` label
  (`OlderMacOS(27, today)` in `caselib/model.py`), which replaces its
  `known_gap` on a macOS older than 27: `today` is the wrong answer pinned
  there, or `None` when the case's `expected` holds there. So CI guards
  what macOS 26 gives and a local macOS 27 run what the owner gets; a
  change on either side fails until the label is updated. The gallery
  does not read `older_macos`: built from a macOS 26 scorecard, it would
  show K42 as a miss with no known gap.
- **Local**: the real-world corpus (above) — never in CI, since the
  files never leave your machine.

## Performance files (`marked perf=True`)

Three large, generated files representative of real workloads
(`families/perf.py`), for measuring runtime rather than correctness:

- `file.perf-scanned-300` — a ~300-page scanned-style document (every page
  an image of text), for the OCR layer's per-page cost.
- `file.perf-text-500` — a ~500-page text document in an embedded font
  (Word-export-like), for the Text layer's per-page cost.
- `file.perf-image-heavy` — an image-heavy ~50 MB file (a handful of
  large, incompressible images), for throughput and memory on a
  big-but-few-objects file.

These are synthetic runtime proxies checked into this repository, not the
real-world corpus files docs/REDESIGN.md §5 asks for in Phase 0c (a local,
SHA-256-keyed, non-fabricated manifest of representative vendor files) —
they exercise the same per-page/per-object cost shapes without needing
any real document.

They are excluded from the default test run — `pytest` skips anything
marked `perf` unless `RUN_PERF=1` is set:

```bash
RUN_PERF=1 pytest tests/test_case_library.py -k perf -v
```

or, outside pytest, to keep the PDF and time the scan directly:

```bash
PYTHONPATH=eval:. python -m caselib.run /tmp/perf file.perf-scanned-300
```

## Gallery (`gallery/`)

A static, self-contained HTML page generated from the case library
(docs/REDESIGN.md's Phase 0e): "what a reader sees, the recovered secret,
the tool's verdict." **It never gates anything** — it is not run by
`pytest`, not part of `ci-ok`, and has no verdict of its own.

```bash
PYTHONPATH=eval:. python -m gallery build --out /tmp/gallery
PYTHONPATH=eval:. python -m gallery build --out /tmp/gallery --results /tmp/diff.json
open /tmp/gallery/index.html
```

For every leak case (grouped by family, then by COVERAGE.md cell) it
shows the id, the story, the **mistake** (what the redactor or a tool did
wrong) and the **recovery** (how a person actually gets the secret back
out), a page-1 PNG rendered fresh at build time (never committed —
"what a reader sees"), the pinned secret(s) planted in it (fabricated:
the canonical example SSN, 123-45-6789), and today's verdict. Clean and
false-alarm cases get their own section. Each render may carry a short
caption saying where the pinned value is relative to page 1, derived
from the case's primary cell rather than a hand list:

- A row that never reaches the rendered page (off-page, a switched-off
  layer, an orphaned or superseded object, metadata, an attachment, a
  script, private data, the trailer, unindexed bytes, ...): "Nothing visible here: the
  secret is elsewhere in the file".
- "live" (drawn on a page): left uncaptioned by design; the value may be
  covered (a box over it, say) — the caption is about where the value
  is, not whether a reader can make it out.
- "match" (a layout-splitting limit): the row alone can't say, so it is
  decided from the pages' own extracted text, rule by rule, with letters
  and digits compared and everything else (separators, spaces, line
  breaks) ignored. The whole value on one line of page 1 gets no
  caption; the whole value on page 1 but only reading across lines
  (`match.line-wrap` and friends, a two-column wrap) gets "The value is
  on this page, split across lines"; at least 3 characters of it on
  page 1 with the rest on a later page (`match.page-break` and friends)
  gets "Only part of the value is on page 1; the rest is on a later
  page"; none of it on page 1 but all of it further on gets "Not on page
  1: the value appears later in the document"; and in no page's text at all
  gets "Nothing visible here" (e.g. `match.extreme-coordinates`, K35,
  which draws its text where PyMuPDF's extraction never returns it, so
  the page-1 PNG is blank). A rule with no literal value (a built-in
  class like `ssn`) is judged by where its own regex finds a match its
  validator accepts, one line at a time or across joined lines — never
  over the whole page with every separator stripped, which would fuse
  digits from unrelated lines. With several rules, the one page 1 shows
  most of decides. `tests/test_gallery.py` pins the answer for every
  `match.*` leak case against a hand-checked table
  (`tests/gallery_visibility.json`).

Captions report where the value is in the page's text, not whether it is
legible: a value under a box, drawn white on white, or in invisible
text render mode 3 is on page 1 all the same, so it gets no caption.

**The miss marker** — the gallery's most important one — is "MISSES IT
TODAY" on any leak case whose *shown* verdict has exit `0`: not the
pinned label directly, but whatever `gallery.verdicts.verdict_for`
actually displays (measured, when `--results` covers the case; the
label otherwise). A leak case with **no** `known_gap` at all showing exit
`0` is the dangerous direction — a real, undocumented miss — and gets a
distinct "NEW MISS (not a known gap)" badge instead, so it's never
confused with an already-tracked gap. When a measured result disagrees
with a pinned known gap in the *reassuring* direction (the label says
exit `0`, this run's measured verdict caught it anyway), the page says so
in a note rather than showing a stale badge — the gap may already be
closed, or this run's environment/version differs from the one the
label was pinned against. A crashed measured run is never a miss —
specifically, a crashed **candidate** run: a `scorecard diff --json`
row's own `"crashed"` field is true when *either* side crashed
(`differential.CaseDiff.crashed`), so `gallery.verdicts.load_results`
keys off the row's `"candidate"` key instead (`None` exactly when the
candidate itself crashed) — a reference-only crash no longer hides a
real candidate result (including a real miss) behind "crashed". The
candidate's result is shown as usual, with a note that the reference run
crashed on this case, so there is nothing to compare it against.

Each case links to its COVERAGE.md cell and, where its known gap is one
of docs/REDESIGN.md §8's numbered gaps, to its K-number, via
`gallery.knumbers.CASE_TO_K` — an explicit, hand-verified table (an
earlier version derived this by fuzzy-matching each case's story against
§8's table; a review found that untested and non-deterministic, so it's
now a plain dict, checked by `tests/test_gallery.py` against
`documented_k_numbers` — parsed straight from §8 — so the two can never
silently drift apart). COVERAGE.md's cell ids don't appear verbatim
anywhere in COVERAGE.md's own prose (confirmed: none of the case
library's ~70 cell descriptions are literal substrings of it either), so
the link points at the file as a whole rather than a GitHub text-fragment
anchor (`#:~:text=...`) — that mechanism does work on GitHub's rendered
markdown (verified against a live example), there's just no reliably
derivable search string to hand it per cell.

**The verdict shown**: without `--results`, it's the case's own label
(`expected`, or `known_gap.today` for a known gap) — accurate as of the
last green test run, but not measured by the gallery build itself, and
labelled as such. With `--results`, a `scorecard diff --json` report
(eval/README.md's "The scorecard", above) — the gallery reuses that
runner rather than scanning cases itself; a case the report doesn't
cover (skipped this environment) still falls back to its label.

Perf cases and any case whose `requires` this machine can't meet are
skipped, the same as `caselib.run`. Nothing is written outside `--out`;
PDFs are built into a temporary directory. CI builds it on Linux (no
OCR, so gap labels are shown, never measured — running the scorecard
first isn't worth the extra time for a page that doesn't gate anything)
and uploads it as a `continue-on-error` artifact, outside `ci-ok`'s
`needs:`.
