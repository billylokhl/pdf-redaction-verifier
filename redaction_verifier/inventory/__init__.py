"""redaction_verifier.inventory — our own account of every byte of a PDF.

docs/REDESIGN.md §4 ("Inventory"), built in Phase 3a in shadow mode (§6):
nothing the CLI ships imports this package yet. It runs in the child
process (§4, "Process boundary"), so it must never import the parts that
hold secrets -- rules, matching, report, views -- nor verify
(tests/test_import_boundaries.py enforces both). It never raises on
input bytes: every anomaly is a Flag.
"""

from __future__ import annotations

from .tiling import check_tiling, tile
from .types import CONTESTED, Contested, Region, Tiling

__all__ = ["CONTESTED", "Contested", "Region", "Tiling", "check_tiling", "tile"]
