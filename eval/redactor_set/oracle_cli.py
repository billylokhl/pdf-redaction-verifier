"""The ``oracle`` and ``adjudicate`` subcommands (python -m redactor_set)."""

from __future__ import annotations

import hashlib
import json
import shutil
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


def stored_whole(row: oracle.InstanceResult, encoding: str) -> bool:
    """Whether the channels that read stored content found *row* whole:
    the raw and object scans (R1, R2) for text, the extracted-image OCR
    for pixels. They alone see an orphaned stream or image in m1 and m2,
    so calibration must prove them, not just the render (which wins the
    class in m0)."""
    whole = [e for e in row.evidence if e.how == "whole" and e.counts]
    if encoding == "pixels":
        return any(e.channel == "R2" and e.role == "image" for e in whole)
    return any(e.channel in ("R1", "R2") for e in whole)


def run_oracle(root: Path, set_name: str, methods: list[str], only: list[str]) -> int:
    """Run the oracle on every output; results go to
    ``oracle/<set>/<method>/<stem>.json`` and the renders and crops it
    read to ``oracle/<set>/<method>/<stem>/`` for the adjudicator. m0 is
    a calibration: every planted instance must be found whole in the
    unredacted input -- and by the channels that read stored content
    (``stored_whole``) -- or the command exits 1."""
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
            work = out_dir / stem
            if work.exists():
                shutil.rmtree(work)
            result = oracle.run(path, truth, output=str(path.relative_to(root)),
                                sha256=sha, method=method, work=work)
            (out_dir / f"{stem}.json").write_text(oracle.dumps(result))
            weak = [i for i in result.instances if i.index >= 0 and
                    i.cls not in ("recoverable-visible", "recoverable-hidden")]
            if method == "m0":
                weak += [i for i in result.instances if i.index >= 0 and i not in weak
                         and not stored_whole(i, truth.instances[i.index].encoding)]
            if method == "m0" and weak:
                bad_calibration.append((stem, [f"{i.secret}/{i.loc}={i.cls}" for i in weak]))
            print(f"{set_name} {method} {stem}: {result.truth}")
    for stem, names in bad_calibration:
        print(f"CALIBRATION: {stem}: not found whole in the unredacted input: "
              f"{', '.join(names)}")
    return 1 if bad_calibration else 0


def run_adjudicate(root: Path, set_name: str, action: str, reason: str,
                   seeds: list[int] | None = None) -> int:
    adj_dir = root / "adjudication" / set_name
    if action == "draft":
        made = 0
        for method in ("m1", "m2"):
            for path in sorted((root / "oracle" / set_name / method).glob("*.json")):
                target = adj_dir / method / path.name
                if target.exists():
                    continue        # never overwrite an adjudicator's work
                truth = json.loads((root / set_name / f"{path.stem}.truth.json").read_text())
                if seeds is not None and truth.get("seed") not in seeds:
                    continue
                result = oracle.FileResult.from_dict(json.loads(path.read_text()))
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                rec = adjudicate.draft(result, truth["instances"], sha, truth["producer"],
                                       path.stem)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
                made += 1
        print(f"{made} draft record(s) written under {adj_dir}")
        return 0
    if action == "lock":
        try:
            path, digest = adjudicate.lock(root, set_name, reason=reason, seeds=seeds)
        except ValueError as exc:
            print(f"not locked:\n{exc}")
            return 1
        print(f"locked: {path}\nlock sha256: {digest}")
        return 0
    problems, lock_sha = adjudicate.verify(root, set_name)
    for p in problems:
        print(p)
    if lock_sha:
        print(f"lock sha256: {lock_sha}")
    return 1 if problems else 0
