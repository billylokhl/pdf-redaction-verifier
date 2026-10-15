"""The ``oracle`` and ``adjudicate`` subcommands (python -m redactor_set)."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from . import adjudicate, oracle
from .truth import DocTruth

METHODS = {"m0": None, "m1": "m1", "m2": "m2"}


def _outputs(root: Path, set_name: str, method: str) -> list[tuple[str, Path]]:
    """(document stem, file) for one method: m0 is the input itself."""
    if method == "m0":
        return [(p.stem, p) for p in sorted((root / set_name).glob("*.pdf"))]
    run = json.loads((root / "work" / set_name / "run.json").read_text())
    out = []
    for stem, entry in sorted(run["documents"].items()):
        if entry.get(method):
            out.append((stem, root / entry[method]))
    return out


def run_oracle(root: Path, set_name: str, methods: list[str], only: list[str]) -> int:
    """Run the oracle on every output; m0 is a calibration (every planted
    instance must be found in the unredacted input) and fails the
    command when it does not hold."""
    bad_calibration: list[tuple[str, list[str]]] = []
    for method in methods:
        if method not in METHODS:
            raise SystemExit(f"unknown method {method!r}")
        out_dir = root / "oracle" / set_name / method
        out_dir.mkdir(parents=True, exist_ok=True)
        for stem, path in _outputs(root, set_name, method):
            if only and stem.split("-s")[0] not in only and stem not in only:
                continue
            truth = DocTruth.load(root / set_name / f"{stem}.truth.json")
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            with tempfile.TemporaryDirectory(prefix="rs-oracle-") as work:
                result = oracle.run(path, truth, output=str(path.relative_to(root)),
                                    sha256=sha, method=method, work=Path(work))
            (out_dir / f"{stem}.json").write_text(oracle.dumps(result))
            missed = [i for i in result.instances if i.cls == "removed"]
            if method == "m0" and missed:
                bad_calibration.append((stem, [f"{i.secret}/{i.loc}" for i in missed]))
            print(f"{set_name} {method} {stem}: {result.truth}")
    for stem, names in bad_calibration:
        print(f"CALIBRATION: {stem}: not found in the unredacted input: {', '.join(names)}")
    return 1 if bad_calibration else 0


def run_adjudicate(root: Path, set_name: str, action: str, reason: str) -> int:
    adj_dir = root / "adjudication" / set_name
    if action == "draft":
        made = 0
        for method in ("m1", "m2"):
            for path in sorted((root / "oracle" / set_name / method).glob("*.json")):
                target = adj_dir / method / path.name
                if target.exists():
                    continue        # never overwrite an adjudicator's work
                data = json.loads(path.read_text())
                result = oracle.FileResult(**{**data, "instances": [
                    oracle.InstanceResult(**{**i, "evidence": [oracle.Evidence(**e)
                                                               for e in i["evidence"]]})
                    for i in data["instances"]]})
                truth = json.loads((root / set_name / f"{path.stem}.truth.json").read_text())
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                rec = adjudicate.draft(result, truth["instances"], sha, truth["producer"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
                made += 1
        print(f"{made} draft record(s) written under {adj_dir}")
        return 0
    if action == "lock":
        try:
            path = adjudicate.lock(adj_dir, reason=reason)
        except ValueError as exc:
            print(f"not locked: {exc}")
            return 1
        print(f"locked: {path}")
        return 0
    problems = adjudicate.verify(adj_dir)
    for p in problems:
        print(p)
    return 1 if problems else 0
