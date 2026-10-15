"""Generate the real-redactor set's inputs (templates or probes) into the
local, gitignored root, with a truth manifest per document and a
preflight that refuses any input carrying something it must not."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import pwd
import random
import re
import shutil
import socket
import tempfile
from importlib import metadata
from pathlib import Path

import pikepdf

from . import locate, persona, producers
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


def private_needles() -> list[str]:
    """What must never reach a document, lower-cased: the login name, the
    home path, the user's full name and each part of it (the account's
    gecos field), the hostname and its first label, and ``file://`` (a
    local file URL, as Chrome's default header prints)."""
    out = {getpass.getuser(), str(Path.home()), "file://"}
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0]
    except (KeyError, AttributeError):
        gecos = ""
    out.add(gecos)
    out.update(gecos.split())
    host = socket.gethostname()
    out.update({host, host.split(".")[0]})
    return sorted({n.lower() for n in out if len(n) >= 3})


def _text_blobs(pdf: pikepdf.Pdf, raw: bytes, path: Path) -> list[bytes]:
    """Lower-cased: the raw file's objects, every decoded stream except font
    programs and images (binary, where a short needle matches by chance;
    a secret there is check (2)'s concern), and each page's extracted
    text -- drawn text in glyph codes (Chrome's header and footer) is
    readable only there."""
    font_or_image = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
            for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                if key in obj:
                    font_or_image.add(obj[key].objgen)
    # The raw file without its stream bodies (decoded below): compressed
    # bytes would match a short needle by chance.
    blobs = [re.sub(rb"stream\r?\n.*?endstream", b"stream endstream", raw, flags=re.S).lower()]
    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream) or obj.objgen in font_or_image \
                or obj.get("/Subtype") == "/Image":
            continue
        try:
            blobs.append(obj.read_bytes().lower())
        except pikepdf.PdfError:
            pass
    for lines in locate.page_lines(path):
        blobs.append("\n".join(line.text for line in lines).lower().encode("utf-8"))
    return blobs


def preflight(path: Path, truth: DocTruth) -> None:
    """Refuse an input that (1) carries anything private_needles() names,
    in any case, raw, decoded or as extracted page text, (2) holds a
    secret's bytes inside a font program or image samples (a coincidence
    the oracle would report as a leak), or (3) has a trailer /ID whose
    hex text, as the file writes it, a config pattern matches (as a
    verifier pattern rule could)."""
    raw = path.read_bytes()
    needles = private_needles()
    encoded = [n.encode() for n in needles] + [n.encode("utf-16-be") for n in needles]
    with pikepdf.open(path) as pdf:
        for blob in _text_blobs(pdf, raw, path):
            if any(n in blob for n in encoded):
                raise PreflightError(f"{truth.doc}: carries this machine's login name, user "
                                     "name, home path, hostname or a file URL")
        secrets = {f.encode() for f in persona.written_forms()}
        for inst in truth.instances:
            secrets.update(bytes.fromhex(h) for h in inst.fingerprints)
        for kind, data in _font_and_image_streams(pdf):
            for s in secrets:
                if len(s) >= 6 and s in data:
                    raise PreflightError(f"{truth.doc}: a secret's bytes occur in a {kind} stream")
        ids = [m.group(1).decode() for m in re.finditer(rb"<([0-9A-Fa-f\s]+)>", _trailer_ids(raw))]
        parsed = pdf.trailer.get("/ID")
        if isinstance(parsed, pikepdf.Array):
            ids += [bytes(part).hex() for part in parsed]
        for text in ids:
            text = re.sub(r"\s", "", text)
            if any(re.search(p, t, re.IGNORECASE) for p in persona.PATTERNS
                   for t in (text, text.upper(), text.lower())):
                raise PreflightError(f"{truth.doc}: trailer /ID hex matches a config pattern")


def _trailer_ids(raw: bytes) -> bytes:
    """The bytes of every ``/ID [...]`` array written in the file (a
    classic trailer or a cross-reference stream's dictionary)."""
    return b" ".join(m.group(0) for m in re.finditer(rb"/ID\s*\[[^\]]*\]", raw))


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
    """Build each document into *out_dir* (``<id>-s<seed>.pdf`` and
    ``<id>-s<seed>.truth.json``) and return its manifest rows, each with
    the producer versions it was made with."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made_with = versions()
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
            stem = f"{doc_id}-s{seed}"
            pdf_out = out_dir / f"{stem}.pdf"
            shutil.copyfile(built.pdf, pdf_out)
            (out_dir / f"{stem}.truth.json").write_text(truth.to_json())
            rows.append({"doc": doc_id, "seed": seed, "file": pdf_out.name,
                         "sha256": sha256(pdf_out), "producer": truth.producer,
                         "pages": truth.pages, "instances": len(truth.instances),
                         "versions": made_with})
            print(f"{stem}: {truth.pages} page(s), {len(truth.instances)} instance(s)")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return rows


def write_manifest(out_dir: Path, rows: list[dict[str, object]], kind: str) -> None:
    """Merge *rows* into ``manifest.json``, one row per (document, seed)."""
    path = out_dir / "manifest.json"
    old = json.loads(path.read_text())["documents"] if path.exists() else []
    merged = {r["file"]: r for r in old}
    merged.update({r["file"]: r for r in rows})
    path.write_text(json.dumps({"kind": kind, "documents": [merged[k] for k in sorted(merged)]},
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
