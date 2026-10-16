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


def test_shard_a_files_exist():
    for f in ci_shards.SHARD_A:
        assert (ROOT / f).is_file(), f"{f} is in SHARD_A but does not exist"


def test_shards_partition_the_full_collection():
    full = _collect(["tests/"])
    a = _collect(ci_shards.pytest_args("a"))
    b = _collect(ci_shards.pytest_args("b"))
    assert a and b
    assert not (a & b), f"collected by both shards: {sorted(a & b)[:5]}"
    assert a | b == full, f"unrun in CI: {sorted(full - (a | b))[:5]}"
