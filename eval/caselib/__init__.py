"""The case library: generated PDFs, each with the verdict a correct
verifier gives and the story of how the redaction failed.

    from caselib import REGISTRY, load
    load()                       # import every family, filling REGISTRY

See docs/REDESIGN.md §5 and eval/README.md.
"""

from __future__ import annotations

import importlib
import pkgutil

from .cells import CELLS, NEW_CELL_ALLOWLIST, NONFITZ_PENDING, UNDOCUMENTED_GAPS, Cell
from .model import (CODE, DEFAULT_RULES, GALLERY_FIELDS_PENDING, PRIVACY_KINDS, REGISTRY, SSN,
                    Case, Expect, KnownGap, case, expect)

__all__ = ["CELLS", "GALLERY_FIELDS_PENDING", "NEW_CELL_ALLOWLIST", "NONFITZ_PENDING",
           "PRIVACY_KINDS", "UNDOCUMENTED_GAPS", "Cell", "CODE", "DEFAULT_RULES",
           "REGISTRY", "SSN", "Case", "Expect", "KnownGap", "case", "expect", "load"]


def load() -> dict[str, Case]:
    """Import every module under caselib.families (idempotent)."""
    from . import families
    for module in pkgutil.iter_modules(families.__path__):
        importlib.import_module(f"{families.__name__}.{module.name}")
    return REGISTRY
