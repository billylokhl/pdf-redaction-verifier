# Evaluation

The case library that measures the verifier — see
[docs/REDESIGN.md](../docs/REDESIGN.md) §5.

## The case library (`caselib/`)

Each case is a small script that writes one PDF with fabricated data,
plus what a correct verifier says about it and the story of how the
redaction failed:

```python
@case("leak.orphan.redacted-no-gc", cells="orphaned.plain",
      expected=expect(1, findings=(("SSN", "orphaned"),)),
      story="The SSN was redacted on the page, but the file was saved without "
            "garbage collection, so the original content stream is still inside.",
      mistake="Saving without garbage collection.",
      recovery="Decompress the file (mutool clean -d) and search the raw bytes.")
def redacted_no_gc(path):
    ...
```

- **`expected`** is the *correct* verdict: the exit code, plus findings
  as `(rule, storage)` pairs and warning codes that must appear.
- **`known_gap`** names the cell when today's tool gets the case wrong.
  The test is a strict xfail: closing the gap fails it until the label is
  removed.
- **`cells`** are ids from `caselib/cells.py`, which mirrors
  [COVERAGE.md](../COVERAGE.md). Every ✓ and ⚑ cell must have a case that
  plants a secret there and expects the tool to catch it.
- **`writer`**: `fitz` (PyMuPDF — also what the verifier reads with),
  `raw` (hand-assembled bytes, `caselib/rawpdf.py`), `qpdf`, or
  `redactor` (real redaction-tool output, committed under `caselib/real/`
  with its provenance in `families/redactors.py`).
- **`rules`** default to two values, `SSN` = 123-45-6789 and
  `Code` = BLUEHERON. **`requires`** lists `ocr`, `qpdf`, `exiftool`;
  a case is skipped where they are missing. Without OCR or the tools, a
  case is judged with their "not available" warnings left out.

Cases are grouped by id: `clean.*` (nothing to find), `leak.*` (a secret
the tool should catch or flag), `gap.*` (known gaps), `redactor.*` (real
tool output).

## Running

```bash
pytest tests/test_case_library.py
```

Build and judge cases outside pytest, keeping the PDFs and reports:

```bash
PYTHONPATH=eval:. python -m caselib.run /tmp/cases              # all
PYTHONPATH=eval:. python -m caselib.run /tmp/cases leak.visible  # some
```

## The build lock

`caselib/cases.lock.json` holds each case's PDF hash, so a change in what
a generator writes (an edit, a PyMuPDF upgrade) shows up in review. Builds
are made reproducible: no new file IDs, pinned dates, and Python-side
compression in stored blocks. After changing a case, regenerate it:

```bash
PYTHONPATH=eval:. python -m caselib.lock
```
