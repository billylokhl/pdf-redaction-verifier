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

`corpus.py` never looks under `$HOME` or any user data directory. Every
script here reports **aggregate counts and rates only** — file paths,
filenames, or file contents are never printed, logged, or written to a
committed file. `RESULTS.md` in this directory holds only the aggregate
numbers cited in the ADRs, with the corpus size and discovery command so
the measurement is reproducible on another machine.

## Files

| File | Purpose |
| --- | --- |
| `corpus.py` | Discovers the local PDF corpus (paths held in memory only). Run standalone to print just a count. |
| `s1b_consumption_witness.py` | Spike S1b: extends `verify._scan_content` to also track the active font (for code-unit length) and compares its code count against `page.get_texttrace()`'s glyph count — first on hand-built adversarial content streams (malformed operator, truncated inline image, unbalanced `BT`/`q`), then as a reconciliation-rate sweep over every page of the real corpus. |
| `inventory_lite.py` | A minimal, spike-quality byte tiler (header / object / xref+trailer / `%%EOF` / whitespace) and an orphaned-stream classifier, reusing `verify.py`'s xref-chain regexes (`_STARTXREF_RE`, `_OBJ_HEADER_RE`, `_PREV_RE`, `_xref_section`, `_dict_end`) and `_reachable_from_sources`. Not the real Phase 3a inventory — just enough to measure the three corpus rates below. |
| `measure_corpus.py` | Runs the three Phase 1 corpus measurements and prints an aggregate JSON summary: unindexed non-whitespace byte rate, orphaned-content-stream rate, and parser-agreement flag rate (qpdf `--object-streams=disable` + MuPDF repair/warnings, explicitly *not* `qpdf --check`'s linearization lint). |
| `pattern_class_false_hard.py` | Runs `verify.py`'s own built-in pattern classes (imported, not reimplemented) against the real corpus's plain-text page extractions, behind `docs/adr/0005`. |

## Running

From the repository root, with `qpdf` on `PATH` (`brew install qpdf`) for
the parser-agreement measurement:

```bash
python3 eval/spikes/s1b_consumption_witness.py
python3 eval/spikes/measure_corpus.py
python3 eval/spikes/pattern_class_false_hard.py
```

Both scripts add the repo root to `sys.path` themselves so `import
verify` resolves without `PYTHONPATH`. Each prints a JSON object to
stdout; nothing is written to disk. The numbers in `RESULTS.md` are a
snapshot from one run on the author's machine — expect different (but
similarly small) numbers on another machine's corpus.

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
