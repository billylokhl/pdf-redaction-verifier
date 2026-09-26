"""Loader for the blind red-team slot (eval/caselib/redteam/, see its
README for the protocol). Like every module under ``families/`` this is
imported by ``caselib.load()``; it walks each round directory, checks its
frozen label hash, and registers every case into the same ``REGISTRY``
with ``origin="redteam"``.

A round's ``labels.json`` is data, not code: each entry is a plain dict
matched to ``model.case()``'s keyword arguments, so a round can be
authored (and reviewed) without touching a line of Python. The two
supported ways to provide a case's bytes are a committed PDF
(``"pdf": "<name>.pdf"``, copied as-is — the same ``writer="file"``
families/redactors.py uses) or a builder script
(``"builder": "<module>:<function>"``, a module in the round's own
directory exposing a ``build(path: Path) -> None`` the way every other
family does).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from ..model import Expect, KnownGap, case, expect

REDTEAM = Path(__file__).resolve().parent.parent / "redteam"
ADJUDICATIONS = REDTEAM / "adjudications.log"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def round_dirs() -> list[Path]:
    """Every round directory (one that has both round.json and
    labels.json), sorted for a deterministic load order."""
    if not REDTEAM.is_dir():
        return []
    return sorted(
        d for d in REDTEAM.iterdir()
        if d.is_dir() and (d / "round.json").exists() and (d / "labels.json").exists()
    )


def read_adjudications() -> list[dict[str, Any]]:
    if not ADJUDICATIONS.exists():
        return []
    return [json.loads(line) for line in ADJUDICATIONS.read_text().splitlines() if line.strip()]


def check_label_lock(round_dir: Path) -> None:
    """Raise if a round's labels.json does not match its recorded hash,
    or was changed without a matching adjudications.log entry. See
    redteam/README.md's "Why labels are frozen"."""
    meta = json.loads((round_dir / "round.json").read_text())
    name = meta["round"]
    live = _sha256(round_dir / "labels.json")
    locked = meta["labels_sha256"]
    if live != locked:
        raise ValueError(
            f"redteam/{name}: labels.json (sha256 {live}) does not match round.json's "
            f"labels_sha256 ({locked}) — edit round.json's labels_sha256 to match, and "
            f"if the labels themselves changed, record why in adjudications.log.")
    initial = meta["initial_labels_sha256"]
    if locked != initial:
        adjudicated = any(a.get("round") == name and a.get("new_sha256") == locked
                          for a in read_adjudications())
        if not adjudicated:
            raise ValueError(
                f"redteam/{name}: labels_sha256 differs from initial_labels_sha256 with no "
                f"matching entry in redteam/adjudications.log — a label change needs a "
                f"recorded adjudication.")


def _load_builder(round_dir: Path, ref: str) -> Callable[[Path], None]:
    module_name, _, func_name = ref.partition(":")
    path = round_dir / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"redteam_{round_dir.name}_{module_name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, func_name or "build")


def _copy(src: Path):
    def build(path: Path) -> None:
        shutil.copyfile(src, path)
    return build


def _expect_from(data: dict[str, Any]) -> Expect:
    return expect(
        data["exit"],
        findings=tuple(tuple(f) for f in data.get("findings", [])),
        warnings=tuple(tuple(w) for w in data.get("warnings", [])),
        layers=tuple(tuple(layer) for layer in data.get("layers", [])),
    )


def register_round(round_dir: Path) -> None:
    check_label_lock(round_dir)
    entries = json.loads((round_dir / "labels.json").read_text())
    for entry in entries:
        rules = entry.get("rules")
        rules_tuple = tuple(rules) if rules else None
        known_gap = entry.get("known_gap")
        kwargs: dict[str, Any] = dict(
            truth=entry["truth"],
            cells=tuple(entry.get("cells", ())),
            features=tuple(entry.get("features", ())),
            expected=_expect_from(entry["expected"]),
            story=entry["story"],
            writer=entry.get("writer", "file"),
            origin="redteam",
            mistake=entry.get("mistake", ""),
            recovery=entry.get("recovery", ""),
            requires=tuple(entry.get("requires", ())),
            privacy_allowlist=tuple(entry.get("privacy_allowlist", ())),
        )
        if rules_tuple is not None:
            kwargs["rules"] = rules_tuple
        if known_gap:
            kwargs["known_gap"] = KnownGap(known_gap["cell"], _expect_from(known_gap["today"]))
        if entry.get("pdf"):
            build = _copy(round_dir / entry["pdf"])
        elif entry.get("builder"):
            build = _load_builder(round_dir, entry["builder"])
        else:
            raise ValueError(f"redteam/{round_dir.name}: case {entry['id']!r} names neither "
                              "a pdf nor a builder")
        case(entry["id"], **kwargs)(build)


for _round in round_dirs():
    register_round(_round)
