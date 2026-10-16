"""How CI splits the full-environment macOS suite into four parallel shards.

.github/workflows/tests.yml runs `full-macos` as a four-leg matrix, each leg
calling `python tests/ci_shards.py <a|b|c|d>` to get its pytest selection:

- shards "a", "b" and "c": exactly the files in their explicit lists;
- shard "d": everything under tests/ EXCEPT those lists, via --ignore.

So a new test file lands in shard d by default and can never go unrun; only
the three lists are maintained by hand, to keep the legs balanced. The CI
runner is 3-6x slower than a laptop on the OCR-heavy files, so the lists
follow CI timings (PR #186, 2026-10-16: each shard about 11-16 minutes of
tests; the case library alone is about 11-15 and bounds how small a shard can
get).
tests/test_ci_shards.py asserts that the shards' collections are disjoint
and together equal the unsplit collection.
"""

from __future__ import annotations

import sys

SHARDS: dict[str, tuple[str, ...]] = {
    "a": (
        "tests/test_visible_map_cli.py",
        "tests/test_inventory_stage.py",
        "tests/test_inventory_pages.py",
        "tests/test_inventory_graph.py",
        "tests/test_visible_map.py",
    ),
    "b": ("tests/test_case_library.py",),
    "c": (
        "tests/test_pattern_baseline.py",
        "tests/test_bomb_cases.py",
        "tests/test_policy1_cases.py",
    ),
}
DEFAULT_SHARD = "d"
ALL_LISTED: tuple[str, ...] = tuple(f for files in SHARDS.values() for f in files)


def pytest_args(shard: str) -> list[str]:
    if shard in SHARDS:
        return list(SHARDS[shard])
    if shard == DEFAULT_SHARD:
        return ["tests/", *(f"--ignore={f}" for f in ALL_LISTED)]
    raise ValueError(f"unknown shard {shard!r} (expected one of a, b, c, d)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: ci_shards.py <a|b|c|d>")
    print(" ".join(pytest_args(sys.argv[1])))
