# PDF Redaction Verifier

Forensic CLI tool that detects sensitive strings (secrets) inside a PDF across
four independent layers: layout-aware DOM text, OCR (Apple Vision), metadata
(exiftool), and decompressed binary streams (qpdf).

## Requirements

- Python 3.10+, macOS (Apple Silicon)
- `pip install pymupdf`
- `uv pip install pyobjc-framework-Vision pyobjc-framework-Quartz`
- CLI tools: `exiftool`, `qpdf` (`brew install exiftool qpdf`)

## Usage

```
python verify.py --target document.pdf --secrets secrets.json
```

Pass `--fail-fast` to stop at the first confirmed finding instead of
producing a complete forensic report.

Exit codes: `0` certified clean, `1` secret detected, `2` operational
error, incomplete scan, or a raw-stream match needing manual review — a
`2` must never be treated as a clean result.
