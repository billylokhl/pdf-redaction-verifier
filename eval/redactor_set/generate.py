"""Generate the real-redactor set's inputs (templates or probes) into the
local, gitignored root, with a truth manifest per document and a
preflight that refuses any input carrying something it must not."""

from __future__ import annotations

import getpass
import hashlib
import json
import random
import re
import shutil
import tempfile
from importlib import metadata
from pathlib import Path

import pikepdf

from . import persona, producers
from .build import Built, finalize
from .truth import DocTruth

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ROOT = REPO_ROOT / "eval" / "scorecard" / "redactor_set_local"


class PreflightError(RuntimeError):
    pass


def local_root(path: Path, *, allow_outside: bool = False) -> Path:
    """*path*, resolved, refused outside the gitignored local root unless
    the caller explicitly allows it (then never commit or share it)."""
    resolved = path.resolve()
    if not allow_outside and resolved != LOCAL_ROOT and LOCAL_ROOT not in resolved.parents:
        raise SystemExit(f"refusing {path}: outside {LOCAL_ROOT.relative_to(REPO_ROOT)} "
                         "(pass --allow-outside to override)")
    return resolved


def _font_and_image_streams(pdf: pikepdf.Pdf) -> list[tuple[str, bytes]]:
    font_files: set[tuple[int, int]] = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
            for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                if key in obj:
                    font_files.add(obj[key].objgen)
    found = []
    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream):
            continue
        kind = ("font" if obj.objgen in font_files
                else "image" if obj.get("/Subtype") == "/Image" else None)
        if kind is None:
            continue
        try:
            data = obj.read_bytes()
        except pikepdf.PdfError:
            data = obj.read_raw_bytes()
        found.append((kind, data))
    return found


def _all_decoded(pdf: pikepdf.Pdf, raw: bytes) -> list[bytes]:
    blobs = [raw]
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            try:
                blobs.append(obj.read_bytes())
            except pikepdf.PdfError:
                pass
    return blobs


def preflight(path: Path, truth: DocTruth) -> None:
    """Refuse an input that (1) carries this machine's login name or home
    path anywhere, raw or decoded, (2) holds a secret's bytes inside a
    font program or image samples (a coincidence the oracle would
    report as a leak), or (3) has a trailer /ID whose hex text
    the config's own patterns would match (as a verifier pattern rule could)."""
    raw = path.read_bytes()
    private = [p for p in {getpass.getuser(), str(Path.home())} if len(p) >= 3]
    needles = [p.encode() for p in private] + [p.encode("utf-16-be") for p in private]
    with pikepdf.open(path) as pdf:
        for blob in _all_decoded(pdf, raw):
            for needle in needles:
                if needle in blob:
                    raise PreflightError(f"{truth.doc}: carries this machine's login name or "
                                         "home path")
        secrets = {f.encode() for f in persona.written_forms()}
        for inst in truth.instances:
            secrets.update(bytes.fromhex(h) for h in inst.fingerprints)
        for kind, data in _font_and_image_streams(pdf):
            for s in secrets:
                if len(s) >= 6 and s in data:
                    raise PreflightError(f"{truth.doc}: a secret's bytes occur in a {kind} stream")
        ids = pdf.trailer.get("/ID")
        if isinstance(ids, pikepdf.Array):
            for part in ids:
                hexed = bytes(part).hex()
                if any(re.search(p, h, re.IGNORECASE) for p in persona.PATTERNS
                       for h in (hexed, hexed.upper())):
                    raise PreflightError(f"{truth.doc}: trailer /ID hex matches a config pattern")


def versions() -> dict[str, str]:
    out = {"chrome": producers.chrome_version(), "tesseract": producers.tesseract_version(),
           "poppler": producers.poppler_version()}
    for dist in ("reportlab", "pillow", "pdfminer.six", "pikepdf"):
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist] = "unavailable"
    return out


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(builders: dict[str, object], out_dir: Path, *, seed: int,
             only: list[str] | None = None) -> list[dict[str, object]]:
    """Build each document into *out_dir* (``<id>.pdf`` and
    ``<id>.truth.json``) and return its manifest rows."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for doc_id, build in builders.items():
        if only and doc_id not in only:
            continue
        rng = random.Random(f"{doc_id}:{seed}")
        work = Path(tempfile.mkdtemp(prefix=f"rs-{doc_id}-"))
        try:
            built: Built = build(work, rng)  # type: ignore[operator]
            truth = finalize(doc_id, seed, built)
            preflight(built.pdf, truth)
            pdf_out = out_dir / f"{doc_id}.pdf"
            shutil.copyfile(built.pdf, pdf_out)
            (out_dir / f"{doc_id}.truth.json").write_text(truth.to_json())
            rows.append({"doc": doc_id, "file": pdf_out.name, "sha256": sha256(pdf_out),
                         "seed": seed, "producer": truth.producer, "pages": truth.pages,
                         "instances": len(truth.instances)})
            print(f"{doc_id}: {truth.pages} page(s), {len(truth.instances)} instance(s)")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return rows


def write_manifest(out_dir: Path, rows: list[dict[str, object]], kind: str) -> None:
    path = out_dir / "manifest.json"
    old = json.loads(path.read_text())["documents"] if path.exists() else []
    merged = {r["doc"]: r for r in old}
    merged.update({r["doc"]: r for r in rows})
    path.write_text(json.dumps({"kind": kind, "versions": versions(),
                                "documents": [merged[k] for k in sorted(merged)]},
                               indent=2) + "\n")


def write_config(root: Path) -> Path:
    """The fabricated redactor config, written once; a different file
    already there is refused rather than silently replaced."""
    path = root / "config" / "redact.yaml"
    text = persona.redactor_config_yaml()
    if path.exists():
        if path.read_text() != text:
            raise SystemExit(f"{path} differs from the persona's config: move it away first")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
