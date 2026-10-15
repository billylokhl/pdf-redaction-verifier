"""redaction_verifier.visible_map.sink — what the scan hands the map.

The scan calls `offer` at two places (verify.scan_page_layer and
verify.scan_hidden_objects), and whoever runs the scan offers the page
count once, all only when a sink exists: without one nothing here is evaluated. `offer` copies
what it is given into plain lists of rule names, page numbers and
locator strings — never document text, never a matched sample — inside
its own guard, so a broken or hostile sink can cost the map its
completeness (`failed`) but can neither raise into the scan nor mutate
what the scan keeps using.

Standard library only: verify.py imports this module on every run.
"""

from __future__ import annotations

import contextlib
from typing import Any

# The per-page truth sets scan_page_layer offers, in its own names and
# order: values hard on a line, values hard across the seam with the
# previous page, pattern rules hard on a line, pattern rules only in fused
# text, values only in joined lines or reconstructions, values crossing a
# page break any other way.
PAGE_FIELDS = ("hard", "seam", "pattern_hard", "pattern_soft", "warned", "crossing")


def _plain(value: Any) -> Any:
    """A deep, plain copy: sets and dicts become sorted lists of their
    (string) members or keys — rule names, never a dict's values, which
    for pattern hits are matched samples."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (set, frozenset, dict)):
        names = list(value)
        if not all(isinstance(n, str) for n in names):
            raise TypeError("truth sets hold rule names only")
        return sorted(names)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    raise TypeError(f"cannot hand a {type(value).__name__} to the map")


class MapSink:
    """Collects the scan's truth for the map, O(pages x hits) in memory:
    a page with no hit costs one integer in `scanned`."""

    def __init__(self) -> None:
        self.page_count: int | None = None
        self.scanned: dict[str, list[int]] = {}
        self.truth: dict[str, dict[int, dict[str, list[str]]]] = {}
        self.hidden: list[dict[str, Any]] = []
        self.failed = False

    def take(self, kind: str, refs: list[Any]) -> None:
        if kind == "document":
            (self.page_count,) = refs
        elif kind == "page":
            layer, page_no, *sets = refs
            if len(sets) != len(PAGE_FIELDS):
                raise ValueError("page truth has the wrong shape")
            self.scanned.setdefault(layer, []).append(page_no)
            if any(sets):
                self.truth.setdefault(layer, {})[page_no] = dict(zip(PAGE_FIELDS, sets))
        elif kind == "hidden":
            page_index, xref, item_kind, location, hard, hits = refs
            self.hidden.append({
                "page": None if page_index is None else page_index + 1,
                "xref": xref, "kind": item_kind, "location": location,
                "hard": hard, "rules": hits,
            })
        else:
            raise ValueError(f"unknown offer {kind!r}")

    def to_header(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "scanned": self.scanned,
            # JSON object keys are strings; the planner turns them back.
            "truth": {layer: {str(p): t for p, t in pages.items()}
                      for layer, pages in self.truth.items()},
            "hidden": self.hidden,
            "failed": self.failed,
        }


def offer(sink: Any, kind: str, *refs: Any) -> None:
    """Hand *sink* a plain copy of *refs*. Never raises (an Exception
    only: a Ctrl-C still stops the scan as it always has); a failure marks
    the sink failed, which the map reports as incomplete."""
    try:
        sink.take(kind, [_plain(r) for r in refs])
    except Exception:
        with contextlib.suppress(Exception):
            sink.failed = True
