# Evaluation

The case library that measures the verifier — see
[docs/REDESIGN.md](../docs/REDESIGN.md) §5.

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
- **`writer`**: `fitz` (PyMuPDF — also what the verifier reads with),
  `raw` (hand-assembled bytes, `caselib/rawpdf.py`), `qpdf`, or `file`
  (committed real-tool output under `caselib/real/`, `origin="redactor"`).
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

**Grids** (`grid=`, `params=`) are parameterised families: `carriers`
(six carrier surfaces × four producer layouts) and `page-text` (27 page
layouts × three rule sets × with or without the SSN pattern rule, plus the
pattern rule alone; labels and known gaps in
`families/page_text_labels.py`). Their tests carry the `grid` marker;
CI runs them on Linux, where OCR is absent — their labels hold with and
without OCR.

## Running

```bash
pytest tests/test_case_library.py
```

Build and judge cases outside pytest, keeping the PDFs and reports:

```bash
PYTHONPATH=eval:. python -m caselib.run /tmp/cases              # all
PYTHONPATH=eval:. python -m caselib.run /tmp/cases page.visible  # some
```

## The build lock

`caselib/cases.lock.json` holds each case's PDF hash, so a change in what
a generator writes (an edit, a PyMuPDF upgrade) shows up in review. Builds
are reproducible: no new file IDs, UTC, date values pinned, and payloads
that must be compressed precomputed (compressor output varies between
zlib versions). After changing a case, regenerate it:

```bash
PYTHONPATH=eval:. python -m caselib.lock
```
