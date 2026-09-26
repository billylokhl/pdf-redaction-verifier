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
| `s1b_consumption_witness.py` | Spike S1b: extends `verify._scan_content` to also track the active font (for code-unit length, keeping numeric operands on the stack so `Tf` is recognised) and, per font, excludes codes with no real glyph (`doc.get_char_widths`, not a byte-value guess) before comparing its code count against `page.get_texttrace()`'s glyph count — first on seven hand-built adversarial content streams, then as a reconciliation-rate sweep over every page of the real corpus, both a naive whole-page pass and the per-decoding-unit pass REDESIGN §4 actually specifies, with a breakdown of any residual mismatch by font encoding. |
| `inventory_lite.py` | A minimal, spike-quality byte tiler (header / object / xref+trailer / `%%EOF` / whitespace) and an orphaned-stream classifier, reusing `verify.py`'s xref-chain regexes (`_STARTXREF_RE`, `_OBJ_HEADER_RE`, `_PREV_RE`, `_xref_section`, `_dict_end`) and `_reachable_from_sources`. Not the real Phase 3a inventory — just enough to measure the three corpus rates below. |
| `measure_corpus.py` | Runs the three Phase 1 corpus measurements (each on both denominators — all files and text-bearing) and prints an aggregate JSON summary: unindexed non-whitespace byte rate, orphaned-content-stream rate, parser-agreement flag rate (qpdf `--object-streams=disable`, resolved via `shutil.which` + MuPDF repair/warnings, explicitly *not* `qpdf --check`'s linearization lint, with only two verified-per-instance benign categories excluded from the refined rate), and the per-file object-count distribution. |
| `pattern_class_false_hard.py` | Runs `verify.py`'s own built-in pattern classes (imported, not reimplemented) against the real corpus's plain-text page extractions, on both denominators, behind `docs/adr/0005`. Scans page text only — not Metadata or Objects, which today's tool also runs pattern classes over. |

## Running

From the repository root, with `qpdf` on `PATH` (`brew install qpdf`) for
the parser-agreement measurement:

```bash
python3 eval/spikes/s1b_consumption_witness.py
python3 eval/spikes/measure_corpus.py
python3 eval/spikes/pattern_class_false_hard.py
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
  (`Identity-H`/`Identity-V` → 2, otherwise → 1). Real predefined CJK
  CMaps with mixed-width codespaces are not modelled; none appeared in
  the local corpus.
- Its per-font "does this code have a real glyph" check
  (`doc.get_char_widths`, glyph id 0 = no glyph) does not fully explain
  the residual mismatch found on real content (see
  `docs/adr/0008-consumption-witness-granularity.md` and this
  directory's `RESULTS.md`) — a real, uninvestigated gap in this spike's
  own measurement, not a settled finding.
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
