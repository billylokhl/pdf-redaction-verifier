#!/usr/bin/env python3
"""Discover a local, non-personal PDF corpus for the Phase 1 measurements.

Only macOS system and application locations are scanned. This module
NEVER looks under the user's home directory, and callers must never
print, log, or persist the individual paths it returns — only aggregate
counts and rates about the files they name. See eval/spikes/README.md.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Deliberately not $HOME or anything under it. These are OS- and
# vendor-shipped PDFs (help books, sample documents, licenses, printer
# test pages, template content) — never a user's own documents.
ROOTS: tuple[str, ...] = ("/System/Library", "/Library", "/Applications")


def discover(roots: tuple[str, ...] = ROOTS, limit: int | None = None) -> list[Path]:
    """Every *.pdf under *roots*, case-insensitive. Paths are returned for
    this process's own use (opening the files); a caller must not print or
    persist them individually."""
    found: list[Path] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            proc = subprocess.run(
                ["find", root, "-iname", "*.pdf"],
                capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in proc.stdout.splitlines():
            if line:
                found.append(Path(line))
        if limit is not None and len(found) >= limit:
            break
    return found[:limit] if limit is not None else found


if __name__ == "__main__":
    # Standalone: report only a count, never the list itself.
    files = discover()
    by_root = {root: sum(1 for f in files if str(f).startswith(root)) for root in ROOTS}
    print(f"corpus size: {len(files)}")
    for root, n in by_root.items():
        print(f"  {root}: {n}")
