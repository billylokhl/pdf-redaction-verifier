# Phase 1 spikes

Feasibility probes and corpus measurements behind the ADRs in
[`docs/adr/`](../../docs/adr/). See
[docs/REDESIGN.md](../../docs/REDESIGN.md) §4 and §7 (Phase 1) for the
questions these answer.

These are throwaway scripts, not library code: no test suite, not
imported by `verify.py` or `eval/caselib`, not run in CI. They import a
few private helpers straight out of `verify.py` (`_scan_content`,
`_reachable_from_sources`, the xref-chain regexes) — read-only reuse of
the tokenizer the plan already names, never a modification of it.

## Privacy

The corpus measurements run against a **local, non-personal PDF corpus**
discovered from macOS system and application locations only:

```
/System/Library
/Library
/Applications
```

`corpus.py`'s `discover()` refuses (raises) any root that is, or sits
under, `$HOME` — never looks under the user's home directory or any
other personal data location. Every
script here reports **aggregate counts and rates only** — file paths,
filenames, or file contents are never printed, logged, or written to a
committed file. `RESULTS.md` in this directory holds only the aggregate
numbers cited in the ADRs, with the corpus size and discovery command so
the measurement is reproducible on another machine.

## Files

| File | Purpose |
| --- | --- |
| `corpus.py` | Discovers the local PDF corpus (paths held in memory only) and classifies a file as text-bearing (`is_text_bearing`, >=1 page with non-empty `get_text()`) — the shared stratification every other script uses. Run standalone to print counts for both. |
| `s1b_consumption_witness.py` | Spike S1b: extends `verify._scan_content` to also track the active font (for code-unit length, keeping numeric operands on the stack so `Tf` is recognised) and decodes literal strings with a locally-fixed decoder (line-continuation/CRLF bugs `verify._decode_pdf_string` also has), then compares its code count against `page.get_texttrace()`'s glyph count (excluding ToUnicode continuation entries and de-duplicating fill+stroke double-draws, with annotations and form widgets deleted before tracing) — first on ten hand-built content streams, then as a reconciliation-rate sweep over every page of the real corpus, reported on the text-page and in-scope-file denominators (most pages show no text at all and would otherwise pad the rate). |
| `inventory_lite.py` | A minimal, spike-quality byte tiler (header / object / xref+trailer / `%%EOF` / whitespace) and an orphaned-stream classifier, reusing `verify.py`'s xref-chain regexes (`_STARTXREF_RE`, `_OBJ_HEADER_RE`, `_PREV_RE`, `_xref_section`, `_dict_end`) and `_reachable_from_sources`. Not the real Phase 3a inventory — just enough to measure the three corpus rates below. |
| `measure_corpus.py` | Runs the three Phase 1 corpus measurements (each on both denominators — all files and text-bearing) and prints an aggregate JSON summary: unindexed non-whitespace byte rate, orphaned-content-stream rate, parser-agreement flag rate (qpdf `--object-streams=disable`, resolved via `shutil.which` + MuPDF repair/warnings, explicitly *not* `qpdf --check`'s linearization lint, with only two verified-per-instance benign categories excluded from the refined rate), and the per-file object-count distribution. |
| `pattern_class_false_hard.py` | Runs `verify.py`'s own built-in pattern classes (imported, not reimplemented) against the real corpus's plain-text page extractions, on both denominators, behind `docs/adr/0005`. Scans page text only — not Metadata or Objects, which today's tool also runs pattern classes over. |
| `interpretation_warnings.py` | Behind `docs/adr/0009`: how often MuPDF's page *interpreter* itself warns while running a page's content (`page.get_texttrace()`), categorised by a normalised warning text, on both denominators — the real cost of REDESIGN §4's "any MuPDF warning → not `DECODED`" rule. |
| `image_envelope_stats.py` | Behind `docs/adr/0004`: how many stored images fall under its 8px envelope floor, via `doc.xref_get_key` on every object (no decompression), on both denominators. |

## Running

From the repository root, with `qpdf` on `PATH` (`brew install qpdf`) for
the parser-agreement measurement:

```bash
python3 eval/spikes/s1b_consumption_witness.py
python3 eval/spikes/measure_corpus.py
python3 eval/spikes/pattern_class_false_hard.py
python3 eval/spikes/interpretation_warnings.py
python3 eval/spikes/image_envelope_stats.py
```

Every script adds the repo root to `sys.path` itself so `import verify`
resolves without `PYTHONPATH`. Each prints a JSON object to stdout;
nothing is written to disk. The numbers in `RESULTS.md` are a snapshot
from one run on the author's machine — expect a different (not
necessarily smaller: several rates measured much larger than first
assumed, see `RESULTS.md`) set of numbers on another machine's corpus.

## Known limitations (spike quality, not production semantics)

- `inventory_lite.py` tiles only the **current revision's** xref chain
  (following `/Prev`, plus `/XRefStm` for hybrid files) — it does not
  additionally walk each still-older revision's own trailer graph. An
  object referenced only by a revision older than the second-to-last is
  therefore classified by current-revision reachability alone, same as
  `verify.py`'s existing `ORPHANED` label today.
- Object spans are recovered by scanning for `N G obj` headers and
  matching `/Length` (falling back to a literal `endstream` search when
  `/Length` is an indirect reference or absent); a coincidental `endobj`
  inside binary stream data can truncate a span. At corpus scale this is
  noise, not bias, and is exactly the kind of ambiguity the real Phase 3a
  parser is designed to detect and flag rather than silently resolve.
- The consumption-witness spike infers each font's code length (1 vs 2
  bytes) from `page.get_fonts()`'s reported `/Encoding` name
  (`Identity-H`/`Identity-V` and the fixed-2-byte `Uni*-UCS2/UTF16-H/V`
  family → 2, a recognised simple-font encoding → 1). **Real predefined
  CJK CMaps with mixed-width codespaces do appear in the local corpus**
  (`90msp-RKSJ-H`, ~10 pages) -- an earlier version of this note claimed
  none did. A page using one is skipped (`_SkipPage("mixed_width_cmap")`)
  rather than silently miscounted; it is not modelled.
- An earlier version of the spike excluded, per font, codes that
  `doc.get_char_widths` reported as having no glyph. That check was
  itself unsound (`get_char_widths` resolves a code through the font's
  cmap as a Unicode code point, ignoring `/Encoding`, `/Differences` and
  `/CIDToGIDMap`) and produced a misleadingly clean reconciliation rate
  on real content by coincidence. **It has been removed entirely** --
  see `docs/adr/0008-consumption-witness-granularity.md`'s root-cause
  section and this directory's `RESULTS.md` for what actually explained
  the residual mismatch (a decoding bug in this script's own literal-
  string handling, annotation/widget text, ToUnicode continuation
  entries, and fill+stroke double-counting -- all fixed).
- **The corpus is a live directory listing, not a fixed reference set.**
  `corpus.discover()` re-walks `/System/Library`, `/Library`, and
  `/Applications` on every run; OS/app updates between runs can add,
  remove, or change files, so exact counts (corpus size, text-bearing
  count, per-category counts) drift by a handful between runs on the
  *same* machine, and will differ more on another machine entirely. Every
  number in `RESULTS.md` and the ADRs is quoted from one specific,
  reproducible run, not a number this corpus is guaranteed to reproduce
  exactly — rerun the scripts for a fresh, current count rather than
  assuming the committed numbers still hold bit-for-bit.
