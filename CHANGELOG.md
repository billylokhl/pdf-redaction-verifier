# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added
- `--json` report output format for machine-readable scan results
- `--version` flag to display tool version
- Tokenization of leftover content streams (compact inline images, long/compactly written text)

### Verdict changes
The following changes can move a file from exit 0 (clean) to exit 2 (uncertifiable):
- Files with compactly written content streams (no spaces between operators)
- Files with long inline images or text in leftover streams without BT operators
- Files with font-before-BT patterns in leftover streams (now properly tokenized)

These files previously exited 0 but now exit 2 due to improved leftover stream analysis. The scanner cannot certify these files as fully clean because it cannot reliably extract the content.

## [0.1.0] - Initial release

### Added
- Initial release of pdf-redaction-verifier
- Multi-layer PDF scanning: text, OCR (macOS), metadata, objects, binary streams, hidden objects
- Support for exact value and regex-based secret matching
- Configurable rules via JSON and YAML formats
- Exit codes: 0 (clean), 1 (secret detected), 2 (operational error / uncertifiable)
- Cross-platform support (macOS with full environment, Linux with partial)
