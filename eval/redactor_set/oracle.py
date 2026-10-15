"""The independent oracle: which planted secrets each redacted output
still holds, and how (docs: eval/README.md, "The real-redactor set").

It shares no code with the verifier (tests/test_redactor_set_oracle.py
checks the imports). Three channels, on different engines, so no class
rests on one engine the verifier also uses:

- **R1** (``oracle_raw``, no PDF library): every stream body in the raw
  bytes decoded and searched, with text operands joined per text block.
- **R2** (pikepdf, libqpdf): every object, reachable from the trailer or
  not, so a hit gets a storage class (live or orphaned); strings, XMP,
  attachments, outlines, annotations and field values, the Info
  dictionary (also an unreferenced one), images and thumbnails.
- **V** (poppler ``pdftoppm`` at 300 dpi, then tesseract): what a reader
  sees -- the whole page and each planted box, plus every extracted image
  and thumbnail, OCR'd. A box filled with one colour is a redaction box.

pdfminer.six (R3) reads the live text of each page, as a second text
reader; MuPDF is never used.

``classify`` turns the channels' evidence into one class per planted
instance and per (file, secret): ``recoverable-visible`` (V reads it
whole), ``recoverable-hidden`` (stored, found whole by R1 or R2 outside a
font program or image, or read by V from an undrawn image), ``partial``
(only a piece of four or more characters survives anywhere; owner
decision: a leak) or ``removed``. The adjudicator (adjudicate.py) checks
and freezes these before the verifier ever runs on the set.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import oracle_raw
from .truth import DocTruth, Instance

RENDER_DPI = 300
CLASSES = ("recoverable-visible", "recoverable-hidden", "partial", "removed")
SEVERITY = {c: i for i, c in enumerate(reversed(CLASSES))}   # removed 0 ... visible 3


@dataclass
class Evidence:
    channel: str            # "R1", "R2", "R3", "V"
    where: str              # human-readable: object, store, page and box
    how: str                # "whole" or "partial:<piece>"
    storage: str = ""       # "live", "orphaned" or "" when unknown
    counts: bool = True     # False for hits that never count alone (font, image bytes)


@dataclass
class InstanceResult:
    index: int              # position in the truth manifest
    secret: str
    loc: str
    cls: str
    evidence: list[Evidence] = field(default_factory=list)
    uncertain: bool = False   # an OCR near miss: the adjudicator decides


@dataclass
class FileResult:
    output: str             # path relative to the local root
    sha256: str
    doc: str
    method: str
    instances: list[InstanceResult]
    secrets: dict[str, str]  # secret id -> worst class over its instances
    truth: str               # "leak" or "clean"
    channel_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Matching ───────────────────────────────────────────────────────────

_OCR_MAP = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5",
                          "s": "5", "B": "8"})


def norm(text: str) -> str:
    """OCR-tolerant normal form: case and spacing folded, look-alike
    letters mapped to digits, punctuation other than @ . / - dropped."""
    text = re.sub(r"[^\w@./-]", "", text).translate(_OCR_MAP)
    return text.lower()


def _distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, stopping once it exceeds *limit*."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def ocr_match(value: str, text: str) -> str | None:
    """"whole", "near" or None: is *value* in OCR *text*? Values of nine
    or more normalised characters allow one edit; shorter must match
    exactly. A distance of two or three is a near miss for the
    adjudicator, never a match."""
    v, t = norm(value), norm(text)
    if not v:
        return None
    if v in t:
        return "whole"
    best = min((_distance(v, t[i:i + len(v) + k], 3) for k in (-1, 0, 1)
                for i in range(0, max(1, len(t) - len(v) + 2))), default=99)
    if len(v) >= 9 and best <= 1:
        return "whole"
    return "near" if best <= 3 else None


def ocr_partial(value: str, text: str) -> str | None:
    """A partial-residue piece of *value* in OCR text (oracle_raw.residue)."""
    return oracle_raw.residue(text, value)


# ── R2: pikepdf ────────────────────────────────────────────────────────

def _reachable(pdf: Any) -> set[tuple[int, int]]:
    import pikepdf

    seen: set[tuple[int, int]] = set()
    stack: list[Any] = [pdf.trailer]
    while stack:
        obj = stack.pop()
        if isinstance(obj, pikepdf.Object) and obj.is_indirect:
            if obj.objgen in seen:
                continue
            seen.add(obj.objgen)
        if isinstance(obj, pikepdf.Stream):
            obj = obj.stream_dict
        if isinstance(obj, pikepdf.Dictionary):
            stack.extend(obj[k] for k in obj.keys())
        elif isinstance(obj, pikepdf.Array):
            stack.extend(list(obj))
    return seen


def _strings(obj: Any, depth: int = 0) -> list[tuple[str, str]]:
    """(key, decoded text) for every string directly in *obj*."""
    import pikepdf

    out: list[tuple[str, str]] = []

    def walk(o: Any, key: str, d: int) -> None:
        if d > 12:
            return
        if isinstance(o, pikepdf.String):
            out.append((key, str(o)))
        elif isinstance(o, pikepdf.Array):
            for item in o:
                if not (isinstance(item, pikepdf.Object) and item.is_indirect):
                    walk(item, key, d + 1)
        elif isinstance(o, pikepdf.Dictionary):
            for k in o.keys():
                item = o[k]
                if not (isinstance(item, pikepdf.Object) and item.is_indirect):
                    walk(item, k, d + 1)

    walk(obj, "", depth)
    return out


def r2_scan(path: Path, prints: dict[str, list[str]]) -> tuple[list[tuple[str, Evidence]],
                                                               list[Any]]:
    """Whole and partial hits of each form in every object (strings in
    dictionaries and arrays, decoded stream data other than fonts and
    images), each with its storage class; plus every image and
    thumbnail, decoded to a PIL image, for V."""
    import pikepdf

    forms = list(prints)
    hits: list[tuple[str, Evidence]] = []
    images: list[Any] = []
    with pikepdf.open(path) as pdf:
        live = _reachable(pdf)
        font_files: set[tuple[int, int]] = set()
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
                for k in ("/FontFile", "/FontFile2", "/FontFile3"):
                    if k in obj:
                        font_files.add(obj[k].objgen)
        for obj in pdf.objects:
            storage = "live" if obj.objgen in live else "orphaned"
            label = f"object {obj.objgen[0]}"
            holder = obj.stream_dict if isinstance(obj, pikepdf.Stream) else obj
            for key, text in _strings(holder):
                for form in forms:
                    if form in text:
                        hits.append((form, Evidence("R2", f"{label} {key}".strip(), "whole",
                                                    storage)))
                    elif (piece := _text_partial(form, text)):
                        hits.append((form, Evidence("R2", f"{label} {key}".strip(),
                                                    f"partial:{piece}", storage)))
            if not isinstance(obj, pikepdf.Stream):
                continue
            if obj.get("/Subtype") == "/Image" or "/Width" in obj and "/Height" in obj:
                img = _to_image(obj)
                if img is not None:
                    images.append((f"{label} image ({storage})", storage, img))
                continue
            if obj.objgen in font_files:
                continue
            try:
                data = obj.read_bytes()
            except pikepdf.PdfError:
                continue
            kind = ("xmp" if obj.get("/Type") == "/Metadata" else
                    "embedded" if obj.get("/Type") == "/EmbeddedFile" else "stream")
            blocks = oracle_raw.text_blocks(data) if kind == "stream" else []
            for form in forms:
                for how, needle in oracle_raw.text_forms(form):
                    if how != "hex" and needle in data:
                        hits.append((form, Evidence("R2", f"{label} {kind}", "whole", storage)))
                        break
                else:
                    codes = [bytes.fromhex(p) for p in prints[form] if p]
                    if any(c in b for c in codes for b in blocks):
                        hits.append((form, Evidence("R2", f"{label} {kind}", "whole", storage)))
    return hits, images


def _text_partial(form: str, text: str) -> str | None:
    return None if form in text else oracle_raw.residue(text, form)


def _to_image(stream: Any) -> Any:
    """A decodable image or thumbnail as a PIL image, else None."""
    try:
        import pikepdf
        from PIL import Image

        if stream.get("/Subtype") == "/Image":
            return pikepdf.PdfImage(stream).as_pil_image()
        w, h = int(stream.Width), int(stream.Height)
        data = stream.read_bytes()
        mode = "RGB" if len(data) >= w * h * 3 else "L"
        return Image.frombytes(mode, (w, h), data[:w * h * (3 if mode == "RGB" else 1)])
    except Exception:          # an image this channel cannot decode is noted, not fatal
        return None


# ── R3: pdfminer ───────────────────────────────────────────────────────

def r3_pages(path: Path) -> list[str]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    pages = []
    for layout in extract_pages(str(path)):
        pages.append("\n".join(el.get_text() for el in layout
                               if isinstance(el, LTTextContainer)))
    return pages


# ── V: render and OCR ──────────────────────────────────────────────────

def _tesseract(png: Path, psm: int = 6) -> str:
    tess = shutil.which("tesseract")
    if not tess:
        raise RuntimeError("tesseract not found on PATH")
    out = subprocess.run([tess, str(png), "-", "--psm", str(psm)], capture_output=True,
                         text=True, timeout=180)
    return out.stdout


def render(path: Path, out_dir: Path) -> list[Path]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError("pdftoppm (poppler) not found on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([pdftoppm, "-r", str(RENDER_DPI), "-png", str(path), str(out_dir / "p")],
                   check=True, capture_output=True, timeout=600)
    return sorted(out_dir.glob("p-*.png"), key=lambda p: int(p.stem.split("-")[1]))


def uniform(img: Any) -> bool:
    """A crop that is one colour (a redaction box) or blank paper."""
    lo, hi = img.convert("L").getextrema()
    return hi - lo < 24


def _crop(png: Path, box: list[float], pad: float = 10.0) -> Any:
    from PIL import Image

    k = RENDER_DPI / 72
    with Image.open(png) as im:
        x0, y0 = max(0, (box[0] - pad) * k), max(0, (box[1] - pad) * k)
        x1, y1 = min(im.width, (box[2] + pad) * k), min(im.height, (box[3] + pad) * k)
        return im.crop((int(x0), int(y0), int(x1), int(y1))).copy()


def _ocr_image(img: Any, work: Path, name: str, scale: int = 1) -> str:
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale))
    p = work / f"{name}.png"
    img.save(p)
    return _tesseract(p)


# ── Classification ─────────────────────────────────────────────────────

def _forms_for(inst: Instance) -> list[str]:
    return [inst.written, *inst.parts] if inst.parts else [inst.written]


def classify_instance(inst: Instance, ev: list[Evidence], *, near: bool) -> tuple[str, bool]:
    counting = [e for e in ev if e.counts]
    if any(e.channel == "V" and e.how == "whole" for e in counting):
        return "recoverable-visible", near
    if any(e.how == "whole" for e in counting):
        return "recoverable-hidden", near
    if any(e.how.startswith("partial") for e in counting):
        return "partial", near
    return "removed", near


def worst(classes: list[str]) -> str:
    return max(classes, key=lambda c: SEVERITY[c]) if classes else "removed"


# Which R1 body kinds and R2 stores can hold an instance of each
# encoding: evidence is attributed to an instance only from a compatible
# place, so a name in the Info dictionary is not counted for the same
# name drawn on a page. Two instances of one secret in the same kind of
# place (a header on every page) cannot be told apart this way; the
# per-secret class is what the measurement uses.
_COMPATIBLE: dict[str, tuple[set[str], set[str]]] = {
    "page-text": ({"content"}, {"stream"}),
    "string": ({"raw", "string", "other", "content"}, {"string", "stream"}),
    "xmp": ({"xmp"}, {"xmp"}),
    "embedded": ({"embedded", "raw", "string"}, {"embedded", "string"}),
    "pixels": (set(), {"image"}),
}


def _r2_kind(e: Evidence) -> str:
    where = e.where
    if where.endswith(" xmp"):
        return "xmp"
    if where.endswith(" embedded"):
        return "embedded"
    if where.endswith(" stream"):
        return "stream"
    if " image" in where:
        return "image"
    return "string"


def run(path: Path, truth: DocTruth, *, output: str, sha256: str, method: str,
        work: Path | None = None) -> FileResult:
    """Every channel on one output file, then the classification."""
    work = work or Path(tempfile.mkdtemp(prefix="rs-oracle-"))
    notes: list[str] = []
    raw = path.read_bytes()
    prints: dict[str, list[str]] = {}
    for inst in truth.instances:
        for form in _forms_for(inst):
            prints.setdefault(form, [])
            prints[form] += [p for p in inst.fingerprints if p not in prints[form]]
    r1 = oracle_raw.scan(raw, prints)
    if r1.undecodable:
        notes.append(f"R1: {r1.undecodable} of {r1.bodies} stream bodies not decodable "
                     "(image codecs or unknown filters)")
    r2_hits, images = r2_scan(path, prints)
    pages_text = r3_pages(path)
    pngs = render(path, work / "render")
    page_ocr = [_tesseract(p) for p in pngs]
    image_ocr = [(where, storage, _ocr_image(img, work, f"img{i}", scale=4 if img.width < 400 else 1))
                 for i, (where, storage, img) in enumerate(images)]

    results: list[InstanceResult] = []
    for idx, inst in enumerate(truth.instances):
        ev: list[Evidence] = []
        near = False
        forms = _forms_for(inst)
        r1_kinds, r2_kinds = _COMPATIBLE[inst.encoding]
        for h in r1.hits:
            if h.form in forms and h.kind in r1_kinds:
                ev.append(Evidence("R1", f"{h.kind} obj {h.obj}",
                                   h.how if h.how.startswith("partial") else "whole",
                                   counts=h.kind not in oracle_raw.NOT_RECOVERABLE_ALONE))
        for form, e in r2_hits:
            if form in forms and _r2_kind(e) in r2_kinds:
                ev.append(e)
        if inst.encoding == "page-text":
            pages = {inst.page} | {int(b[0]) for b in inst.part_boxes}
            for page_no in sorted(p for p in pages if p is not None and p < len(pages_text)):
                if all(f in pages_text[page_no] for f in forms[1:] or forms):
                    ev.append(Evidence("R3", f"page {page_no + 1} live text", "whole", "live"))
        # V: the planted box (or each part's) on its own page; for drawn
        # instances also their page's whole render.
        boxes = ([(int(b[0]), list(b[1:])) for b in inst.part_boxes] if inst.part_boxes
                 else [(inst.page, inst.bbox)] if inst.page is not None and inst.bbox else [])
        for page_no, box in boxes:
            if page_no >= len(pngs):
                continue
            crop = _crop(pngs[page_no], box)
            text = _ocr_image(crop, work, f"crop{idx}-{page_no}", scale=2)
            for form in (inst.parts or forms):
                m = ocr_match(form, text)
                if m == "whole":
                    ev.append(Evidence("V", f"page {page_no + 1} box", "whole", "live"))
                elif m == "near":
                    near = True
                elif (piece := ocr_partial(form, text)) and not uniform(crop):
                    ev.append(Evidence("V", f"page {page_no + 1} box", f"partial:{piece}",
                                       "live"))
        if inst.page is not None and inst.page < len(page_ocr) and inst.encoding in (
                "page-text", "pixels") and not inst.parts:
            if ocr_match(inst.written, page_ocr[inst.page]) == "whole" and any(
                    e.channel == "V" for e in ev):
                ev.append(Evidence("V", f"page {inst.page + 1} render", "whole", "live"))
        if inst.encoding == "pixels":
            for where, storage, text in image_ocr:
                m = ocr_match(inst.written, text)
                if m == "whole":
                    # Pixels stored in an image: hidden unless the page shows
                    # them (the page crop above says so).
                    ev.append(Evidence("R2", where, "whole", storage))
                elif m == "near":
                    near = True
        cls, uncertain = classify_instance(inst, ev, near=near)
        results.append(InstanceResult(idx, inst.secret, inst.loc, cls, ev, uncertain))

    per_secret: dict[str, list[str]] = {}
    for r in results:
        per_secret.setdefault(r.secret, []).append(r.cls)
    secrets = {sid: worst(cl) for sid, cl in sorted(per_secret.items())}
    truth_label = "leak" if any(c != "removed" for c in secrets.values()) else "clean"
    return FileResult(output, sha256, truth.doc, method, results, secrets, truth_label, notes)


def dumps(result: FileResult) -> str:
    import json

    return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"

