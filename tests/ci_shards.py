"""How CI splits the full-environment macOS suite into two parallel shards.

.github/workflows/tests.yml runs `full-macos` as a two-leg matrix, each leg
calling `python tests/ci_shards.py <a|b>` to get its pytest selection:

- shard "a": exactly the files in SHARD_A (the slowest, OCR-heavy ones);
- shard "b": everything under tests/ EXCEPT SHARD_A, via --ignore.

So a new test file lands in shard b by default and can never go unrun; only
SHARD_A is maintained by hand, to keep the two legs balanced. Measured
2026-10-16 (local macOS, REQUIRE_FULL_ENV=1, -m "not grid", 804 s in all),
shard a is about 400 s and shard b about 400 s. tests/test_ci_shards.py
asserts that the shards' collections are disjoint and together equal the
unsplit collection.
"""

from __future__ import annotations

import sys

SHARD_A: tuple[str, ...] = (
    "tests/test_visible_map_cli.py",
    "tests/test_case_library.py",
    "tests/test_pattern_baseline.py",
    "tests/test_bomb_cases.py",
    "tests/test_policy1_cases.py",
)


def pytest_args(shard: str) -> list[str]:
    if shard == "a":
        return list(SHARD_A)
    if shard == "b":
        return ["tests/", *(f"--ignore={f}" for f in SHARD_A)]
    raise ValueError(f"unknown shard {shard!r} (expected 'a' or 'b')")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: ci_shards.py <a|b>")
    print(" ".join(pytest_args(sys.argv[1])))
