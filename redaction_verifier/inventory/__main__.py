"""Dev entry point (3a-5): ``python -m redaction_verifier.inventory FILE``
prints a JSON summary of FILE's inventory -- counts by unit kind, region
kind and flag reason, the revision count, whether the tiling checks and
the work charged. Counts only: never a byte of the document, nor its
path. Not part of the shipped CLI (shadow mode, docs/phase3a-plan.md).

The one child-side module allowed json and sys (for argv and printing):
tests/test_import_boundaries.py's ENTRY_POINTS, and nothing child-side
may import this module.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from ..budget import Budget
from .build import build_inventory
from .types import Inventory


def summary(inventory: Inventory, work: int) -> dict[str, object]:
    """The inventory as counts, JSON-ready: integers, booleans and the
    closed enums' values."""
    regions = Counter(region.kind.value for region in inventory.tiling.regions)
    return {
        "size": inventory.size,
        "revisions": inventory.revisions,
        "tiles": inventory.tiles,
        "units": dict(Counter(unit.ref.kind.value for unit in inventory.units)),
        "regions": dict(regions),
        "flags": dict(Counter(flag.reason.value for flag in inventory.flags)),
        "object_streams": len(inventory.object_streams),
        "work": work,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m redaction_verifier.inventory FILE", file=sys.stderr)
        return 2
    try:
        with open(argv[0], "rb") as handle:
            data = handle.read()
    except OSError as error:
        print(f"cannot read the file: {error.strerror}", file=sys.stderr)
        return 2
    budget = Budget(file_size=len(data))
    inventory = build_inventory(data, budget=budget)
    print(json.dumps(summary(inventory, budget.work), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
