# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added
- `--json` report output format for machine-readable scan results
- `--version` flag to display tool version
- Tokenization of leftover content streams (compact inline images, long/compactly written text)
- Case library (`eval/caselib`) with parameter grids and raw-writer cases for robust testing

### Changed
**Security**: exiftool and qpdf now run with absolute paths, exiftool with `-config ""` disabled, minimal environment variables, and a private working directory. Tool exit warnings now include a `returncode` field. The unquoted-YAML-value warning in configuration parsing now reports only the YAML type rather than the converted value.

### Verdict changes
The following changes can move a file's exit code:
- **Exit 0 → 2**: Files with compactly written content streams (no spaces between operators), long inline images or text in leftover streams without BT operators, or font-before-BT patterns in leftover streams (now properly tokenized). The scanner cannot certify these files as fully clean because it cannot reliably extract the content.
- **Exit 1 → 2**: An unexpected internal error in the tool now exits 2 (uncertifiable) unless a secret had already been found, in which case it stays 1 (leak). The JSON report includes `error.code` as `INTERNAL_ERROR` when applicable.

## [0.1.0] - Initial release

### Added
- Initial release of pdf-redaction-verifier
- Multi-layer PDF scanning: text, OCR (macOS), metadata, objects, binary streams, hidden objects
- Support for exact value and regex-based secret matching
- Configurable rules via JSON and YAML formats
- Exit codes: 0 (clean), 1 (secret detected), 2 (operational error / uncertifiable)
- Cross-platform support (macOS with full environment, Linux with partial)
