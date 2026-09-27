"""redaction_verifier.report — human and JSON reports.

Split into modules per docs/REDESIGN.md §4 ("report/ human, JSON [with
schema_version, experimental], --explain"): text.py (the console report:
_sanitize_report_text, print_report), json.py (the machine-readable
`--json` report: build_json_report, write_json_report,
JSON_SCHEMA_VERSION). This module re-exports everything so callers can do
`from redaction_verifier.report import X` without knowing which submodule
defines it.
"""

from __future__ import annotations

from redaction_verifier.report.json import (
    JSON_SCHEMA_VERSION,
    build_json_report,
    write_json_report,
)
from redaction_verifier.report.text import _sanitize_report_text, print_report

__all__ = [
    "JSON_SCHEMA_VERSION",
    "_sanitize_report_text",
    "build_json_report",
    "print_report",
    "write_json_report",
]
