"""redaction_verifier — the package verify.py's pure parts are moving into.

See docs/REDESIGN.md §4 ("Package layout") and §6 ("Transition"): reusable
pure parts move here first ("Move, don't wrap"); verify.py re-exports them
so existing imports and the CLI keep working unchanged, until it shrinks to
a thin CLI entry point in a later phase.

This package must never import verify (one-way dependency, enforced by a
ruff banned-import rule in pyproject.toml).
"""

from __future__ import annotations
