"""redaction_verifier.report.text — the human-readable console report.

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §4/§6 ("report/ human, JSON"; "Move, don't wrap").
Behaviour is byte-identical to the code this replaced; verify.py
re-exports every name here so existing imports, `verify.X` references and
the CLI keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

from redaction_verifier.matching import mask
from redaction_verifier.model import ScanReport


def _sanitize_report_text(text: str) -> str:
    """Strip control characters so PDF-derived bytes (ANSI escapes,
    newlines) cannot inject into or spoof the terminal report."""
    return "".join(ch if ch.isprintable() or ch == " " else "�" for ch in text)


def print_report(report: ScanReport, pdf_path: Path) -> None:
    """Render the final verdict banner and per-finding detail.

    This is the single choke point where pattern samples are masked and
    all document-derived text is sanitized.
    """
    bar = "=" * 70
    print(f"\n{bar}")
    if report.leaked:
        print(f"  [FAIL]  SENSITIVE DATA DETECTED IN: {pdf_path.name}")
        print(bar)
        for f in report.findings:
            line = f"  ✖ LAYER: {f.layer:<8} | RULE: {f.secret_name!r:<24} | {f.location}"
            if f.sample:
                line += f" — sample {mask(f.sample)}"
            print(_sanitize_report_text(line))
    else:
        print(f"  [PASS]  No target secrets detected in: {pdf_path.name}")
    print(bar)

    if report.degraded:
        print("\n  ⚠ ATTENTION — warnings were raised during the scan:")
        for w in report.warnings:
            print(_sanitize_report_text(f"    - {w}"))
        if not report.leaked:
            print("\n  A [PASS] with warnings is NOT a certified clean result "
                  "(exit code 2): resolve the warnings above.")
    print()
