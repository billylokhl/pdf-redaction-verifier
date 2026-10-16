"""The CI macOS shards (tests/ci_shards.py) neither drop nor repeat a test."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests import ci_shards

ROOT = Path(__file__).resolve().parent.parent


def _collect(args: list[str]) -> set[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    ids = {line for line in out.stdout.splitlines() if "::" in line}
    assert ids, out.stdout[-2000:] + out.stderr[-2000:]
    return ids


def test_listed_files_exist_and_appear_once():
    assert len(set(ci_shards.ALL_LISTED)) == len(ci_shards.ALL_LISTED)
    for f in ci_shards.ALL_LISTED:
        assert (ROOT / f).is_file(), f"{f} is listed in a shard but does not exist"


def test_shards_partition_the_full_collection():
    full = _collect(["tests/"])
    shards = {s: _collect(ci_shards.pytest_args(s)) for s in "abcd"}
    seen: set[str] = set()
    for s, ids in shards.items():
        assert ids, f"shard {s} collects nothing"
        assert not (seen & ids), f"collected by two shards: {sorted(seen & ids)[:5]}"
        seen |= ids
    assert seen == full, f"unrun in CI: {sorted(full - seen)[:5]}"
