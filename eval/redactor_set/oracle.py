"""The independent oracle: which planted secrets each redacted output
still holds, and how (docs: eval/README.md, "The real-redactor set").

It shares no code with the verifier (tests/test_redactor_set_oracle.py
checks the imports). Three channels, on different engines, so no class
rests on one engine the verifier also uses:

- **R1** (``oracle_raw``, no PDF library): every stream body in the raw
  bytes decoded and searched, with text operands joined per text block;
  a body is marked superseded (a later one has its object number) or
  unindexed (after the final ``%%EOF``).
- **R2** (pikepdf, libqpdf): every object, reachable from the trailer or
  not (live or orphaned), each string with the role of the dictionary
  holding it (Info, annotation, field, outline, file spec, link action),
  each decoded stream with its role (page content, appearance, XMP,
  attachment), and every image and thumbnail extracted for V.
- **V** (poppler ``pdftoppm`` at 300 dpi, then tesseract): each planted
  box cropped and read, each whole page read, and each extracted image
  read. A planted box that reads as one colour is a redaction box; one
  that holds ink no reading explains is left *uncertain*.

pdfminer.six (R3) reads each page's live text, a second text reader.
MuPDF is never used.

Evidence is **attributed** to a planted instance only from a place that
kind of instance can be stored (a name in the Info dictionary is not
counted for the same name drawn on a page). Evidence no instance can
claim -- the tool copied a value into an annotation or metadata, a
scan's page gained a text layer, an earlier revision kept it -- is kept
as an **unattributed** row for its secret: it still makes the file a
leak, and the adjudicator must place it.

Classes, per instance and per (file, secret), worst first:
``recoverable-visible`` (V reads it whole on the page),
``recoverable-hidden`` (stored whole outside fonts and images),
``partial`` (only a piece survives: owner decision 2026-10-14, a run of
four or more digits or a whole word of four or more letters) and
``removed``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import oracle_raw
from .truth import DocTruth, Instance

RENDER_DPI = 300
CLASSES = ("recoverable-visible", "recoverable-hidden", "partial", "removed")
SEVERITY = {c: i for i, c in enumerate(reversed(CLASSES))}   # removed 0 ... visible 3
STORAGES = ("live", "orphaned", "superseded", "unindexed", "")


@dataclass
class Evidence:
    channel: str            # "R1", "R2", "R3", "V"
    where: str              # human-readable: object, role, key, page, box
    how: str                # "whole" or "partial:<piece>"
    storage: str = ""       # STORAGES; "" when the channel cannot tell
    counts: bool = True     # False for hits that never count alone (font, image bytes)
    role: str = ""          # R2: info, annot, field, outline, filespec, uri, content,
                            # appearance, xmp, embedded, image, other; R1: its body kind
    form: str = ""          # the written form (or part) found
    page: int | None = None


@dataclass
class InstanceResult:
    index: int              # position in the truth manifest; negative: unattributed
    secret: str
    loc: str                # location code; "?" for an unattributed row
    cls: str
    evidence: list[Evidence] = field(default_factory=list)
    uncertain: bool = False   # an OCR near miss or unexplained ink: the adjudicator decides


@dataclass
class FileResult:
    output: str             # path relative to the local root
    sha256: str
    doc: str
    method: str
    instances: list[InstanceResult]
    secrets: dict[str, str]  # secret id -> worst class over its rows
    truth: str               # "leak" or "clean"
    channel_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileResult:
        rows = [InstanceResult(**{**i, "evidence": [Evidence(**e) for e in i["evidence"]]})
                for i in data["instances"]]
        return cls(**{**data, "instances": rows})


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
    or more normalised characters allow one edit; shorter ones must match
    exactly. A farther miss -- two or three edits, or one on a shorter
    value -- is "near": never a match, always the adjudicator's call."""
    v, t = norm(value), norm(text)
    if not v:
        return None
    if v in t:
        return "whole"
    limit = 3 if len(v) >= 6 else 1
    best = min((_distance(v, t[i:i + len(v) + k], limit) for k in (-1, 0, 1)
                for i in range(0, max(1, len(t) - len(v) + 2))), default=99)
    if len(v) >= 9 and best <= 1:
        return "whole"
    return "near" if best <= limit else None


def ocr_partial(value: str, text: str) -> str | None:
    """A partial-residue piece of *value* in OCR text (oracle_raw.residue)."""
    return oracle_raw.residue(text, value)


# ── R2: pikepdf ────────────────────────────────────────────────────────

_ANNOT_SUBTYPES = {"/Text", "/Link", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon",
                   "/PolyLine", "/Highlight", "/Underline", "/Squiggly", "/StrikeOut",
                   "/Stamp", "/Caret", "/Ink", "/Popup", "/FileAttachment", "/Sound",
                   "/Movie", "/Widget", "/Screen", "/Redact"}
_DATE_KEYS = {"/CreationDate", "/ModDate", "/M", "/ID"}


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


def _role(d: Any, info_ids: set[tuple[int, int]]) -> str:
    """What a dictionary holding a string is."""
    if d.is_indirect and d.objgen in info_ids:
        return "info"
    sub = str(d.get("/Subtype", ""))
    if "/FT" in d or sub == "/Widget":
        return "field"
    if str(d.get("/Type", "")) == "/Annot" or sub in _ANNOT_SUBTYPES:
        return "annot"
    if str(d.get("/S", "")) == "/URI":
        return "uri"
    if str(d.get("/Type", "")) == "/Filespec" or "/EF" in d:
        return "filespec"
    if "/Title" in d and ("/Parent" in d or "/Next" in d or "/Prev" in d):
        return "outline"
    if not d.is_indirect or "/Type" in d:
        return "other"
    # An indirect dictionary without /Type whose keys are all Info keys:
    # an Info dictionary, live or left behind.
    keys = set(d.keys())
    info_keys = {"/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer",
                 "/CreationDate", "/ModDate", "/Trapped"}
    return "info" if keys and (keys & info_keys) and not keys & {"/Kids", "/Parent"} else "other"


def _strings(obj: Any, role: str, info_ids: set[tuple[int, int]]) -> list[tuple[str, str, str]]:
    """(role, key, decoded text) for every string in *obj*, the role
    being the innermost dictionary's."""
    import pikepdf

    out: list[tuple[str, str, str]] = []

    def walk(o: Any, r: str, key: str, d: int) -> None:
        if d > 12:
            return
        if isinstance(o, pikepdf.String):
            out.append((r, key, str(o)))
        elif isinstance(o, pikepdf.Array):
            for item in o:
                if not (isinstance(item, pikepdf.Object) and item.is_indirect):
                    walk(item, r, key, d + 1)
        elif isinstance(o, pikepdf.Dictionary):
            inner = _role(o, info_ids) if d else r
            inner = r if inner == "other" else inner
            for k in o.keys():
                item = o[k]
                if not (isinstance(item, pikepdf.Object) and item.is_indirect):
                    walk(item, inner, k, d + 1)

    walk(obj, role, "", 0)
    return out


def r2_scan(path: Path, prints: dict[str, list[str]]) -> tuple[list[Evidence], list[Any]]:
    """Whole and partial hits of each form in every object -- strings
    (with their dictionary's role and key) and decoded streams (with
    their role) -- each with its storage class; plus every image and
    thumbnail, decoded to a PIL image, for V. Machine-written dates are
    never searched for residue."""
    import pikepdf

    hits: list[Evidence] = []
    images: list[Any] = []
    with pikepdf.open(path) as pdf:
        live = _reachable(pdf)
        info = pdf.trailer.get("/Info")
        info_ids = {info.objgen} if isinstance(info, pikepdf.Object) and info.is_indirect \
            else set()
        font_files: set[tuple[int, int]] = set()
        page_content: set[tuple[int, int]] = set()
        appearance: set[tuple[int, int]] = set()
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
                for k in ("/FontFile", "/FontFile2", "/FontFile3"):
                    if k in obj:
                        font_files.add(obj[k].objgen)
        for page in pdf.pages:
            c = page.obj.get("/Contents")
            for s in (c if isinstance(c, pikepdf.Array) else [c]):
                if isinstance(s, pikepdf.Stream):
                    page_content.add(s.objgen)
            for annot in page.obj.get("/Annots", pikepdf.Array()):
                ap = annot.get("/AP") if isinstance(annot, pikepdf.Dictionary) else None
                if isinstance(ap, pikepdf.Dictionary):
                    for k in ap.keys():
                        e = ap[k]
                        for st in ([e] if isinstance(e, pikepdf.Stream) else
                                   [e[j] for j in e.keys()] if isinstance(e, pikepdf.Dictionary)
                                   else []):
                            if isinstance(st, pikepdf.Stream):
                                appearance.add(st.objgen)
        for obj in pdf.objects:
            storage = "live" if obj.objgen in live else "orphaned"
            label = f"object {obj.objgen[0]}"
            holder = obj.stream_dict if isinstance(obj, pikepdf.Stream) else obj
            top_role = _role(obj, info_ids) if isinstance(obj, pikepdf.Dictionary) else "other"
            for role, key, text in _strings(holder, top_role, info_ids):
                for form in prints:
                    where = f"{label} {role} {key}".strip()
                    if form in text:
                        hits.append(Evidence("R2", where, "whole", storage, role=role,
                                             form=form))
                    elif key not in _DATE_KEYS and not text.startswith("D:") and \
                            (piece := oracle_raw.residue(text, form)):
                        hits.append(Evidence("R2", where, f"partial:{piece}", storage,
                                             role=role, form=form))
            if not isinstance(obj, pikepdf.Stream):
                continue
            if obj.get("/Subtype") == "/Image" or ("/Width" in obj and "/Height" in obj):
                img = _to_image(obj)
                if img is not None:
                    images.append((f"{label} image", storage, img))
                continue
            if obj.objgen in font_files:
                continue
            try:
                data = obj.read_bytes()
            except pikepdf.PdfError:
                continue
            role = ("xmp" if obj.get("/Type") == "/Metadata" else
                    "embedded" if obj.get("/Type") == "/EmbeddedFile" else
                    "appearance" if obj.objgen in appearance else
                    "content" if obj.objgen in page_content or
                    re.search(rb"\b(BT|Tj|TJ)\b", data[:65536]) else "other")
            blocks = oracle_raw.text_blocks(data) if role in ("content", "appearance") else []
            searchable = oracle_raw._without_dates(data) if role == "xmp" else data
            for form in prints:
                where = f"{label} {role}"
                plain = [n for h, n in oracle_raw.text_forms(form) if h != "hex"]
                codes = [bytes.fromhex(p) for p in prints[form] if p]
                if any(n in data for n in plain) or any(c in b for c in codes for b in blocks):
                    hits.append(Evidence("R2", where, "whole", storage, role=role, form=form))
                elif role in ("xmp", "embedded") and (
                        piece := oracle_raw.residue(searchable.decode("latin-1"), form)):
                    hits.append(Evidence("R2", where, f"partial:{piece}", storage, role=role,
                                         form=form))
    return hits, images


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
    for old in out_dir.glob("p-*.png"):
        old.unlink()
    subprocess.run([pdftoppm, "-r", str(RENDER_DPI), "-png", str(path), str(out_dir / "p")],
                   check=True, capture_output=True, timeout=600)
    return sorted(out_dir.glob("p-*.png"), key=lambda p: int(p.stem.split("-")[1]))


def uniform(img: Any) -> bool:
    """One colour: a redaction box, or blank paper."""
    lo, hi = img.convert("L").getextrema()
    return hi - lo < 24


def _crop(png: Path, box: list[float], pad: float = 0.0) -> Any:
    from PIL import Image

    k = RENDER_DPI / 72
    with Image.open(png) as im:
        x0, y0 = max(0, (box[0] - pad) * k), max(0, (box[1] - pad) * k)
        x1, y1 = min(im.width, (box[2] + pad) * k), min(im.height, (box[3] + pad) * k)
        return im.crop((int(x0), int(y0), max(int(x0) + 1, int(x1)),
                        max(int(y0) + 1, int(y1)))).copy()


def _ocr_image(img: Any, work: Path, name: str, scale: int = 1) -> str:
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale))
    p = work / f"{name}.png"
    img.save(p)
    return _tesseract(p)


# ── Attribution and classification ─────────────────────────────────────

def forms_of(inst: Instance) -> list[str]:
    return [inst.written, *inst.parts]


# R2 roles that can hold each string-instance location; R1 never
# attributes to a string instance (it cannot tell whose string it is).
STRING_ROLES: dict[str, set[str]] = {
    "F1": {"field", "annot", "appearance"}, "F2": {"field", "annot", "appearance"},
    "A1": {"annot", "appearance"}, "A1x": {"annot", "appearance"},
    "A2": {"annot"}, "A3": {"annot"}, "A4": {"uri", "annot"},
    "M1": {"info"}, "M2": {"info"}, "O1": {"outline"},
}


def _eligible(inst: Instance, e: Evidence) -> bool:
    """Can *e* be evidence for *inst*? (Page-level and box evidence is
    matched by page in attribute(); this covers R1 and R2.)"""
    if e.form not in forms_of(inst):
        return False
    if inst.encoding == "page-text":
        return (e.channel == "R1" and e.role == "content") or \
            (e.channel == "R2" and e.role == "content")
    if inst.encoding == "string":
        return e.channel == "R2" and e.role in STRING_ROLES.get(inst.loc, set())
    if inst.encoding == "xmp":
        return e.role == "xmp"
    if inst.encoding == "embedded":
        return e.role in ("embedded", "filespec")
    if inst.encoding == "pixels":
        return e.channel == "R2" and e.role == "image"
    return False


def _covered_by_r2(e: Evidence, r2: list[Evidence]) -> bool:
    """An R1 hit on a current body is the same evidence as R2's hit of
    that form (R2 reads every current object, and places it by role)."""
    return e.channel == "R1" and e.storage == "" and any(x.form == e.form for x in r2)


def classify(inst: Instance | None, ev: list[Evidence]) -> str:
    counting = [e for e in ev if e.counts]
    parts = inst.parts if inst is not None and inst.parts else []

    def whole(channels: tuple[str, ...]) -> bool:
        found = {e.form for e in counting if e.how == "whole" and e.channel in channels}
        if inst is not None and inst.written in found:
            return True
        return bool(parts) and all(p in found for p in parts) or (inst is None and bool(found))

    if whole(("V",)):
        return "recoverable-visible"
    if whole(("R1", "R2", "R3", "V")):
        return "recoverable-hidden"
    if counting:
        return "partial"        # a piece, or one part of a split value
    return "removed"


def worst(classes: list[str]) -> str:
    return max(classes, key=lambda c: SEVERITY[c]) if classes else "removed"


@dataclass
class Channels:
    r1: list[Evidence]
    r2: list[Evidence]
    r3: list[str]                       # each page's live text
    page_ocr: list[str]                 # each page's OCR text
    image_ocr: list[tuple[str, str, str]]   # (where, storage, OCR text)
    pngs: list[Path]
    notes: list[str]


def gather(path: Path, truth: DocTruth, work: Path, *, with_v: bool = True) -> Channels:
    prints: dict[str, list[str]] = {}
    for inst in truth.instances:
        for form in forms_of(inst):
            prints.setdefault(form, [])
            prints[form] += [p for p in inst.fingerprints if p not in prints[form]]
    raw = path.read_bytes()
    r1rep = oracle_raw.scan(raw, prints)
    notes = []
    if r1rep.undecodable:
        notes.append(f"R1: {r1rep.undecodable} of {r1rep.bodies} stream bodies not decodable "
                     "(image codecs or unknown filters)")
    r1 = [Evidence("R1", f"{h.kind} obj {h.obj} @{h.offset}",
                   h.how if h.how.startswith("partial") else "whole", h.storage,
                   counts=h.kind not in oracle_raw.NOT_RECOVERABLE_ALONE, role=h.kind,
                   form=h.form) for h in r1rep.hits]
    r2, images = r2_scan(path, prints)
    r3 = r3_pages(path)
    pngs: list[Path] = []
    page_ocr: list[str] = []
    image_ocr: list[tuple[str, str, str]] = []
    if with_v:
        pngs = render(path, work / "render")
        page_ocr = [_tesseract(p) for p in pngs]
        for i, (where, storage, img) in enumerate(images):
            image_ocr.append((where, storage, _ocr_image(
                img, work, f"image{i}", scale=4 if img.width < 400 else 1)))
    return Channels(r1, r2, r3, page_ocr, image_ocr, pngs, notes)


def attribute(truth: DocTruth, ch: Channels, work: Path) -> list[InstanceResult]:
    """Per instance its evidence and class; then one unattributed row per
    secret for every counting hit no instance can claim."""
    claimed: set[int] = set()
    rows: list[InstanceResult] = []
    pool = ch.r1 + ch.r2
    for idx, inst in enumerate(truth.instances):
        ev: list[Evidence] = []
        uncertain = False
        for e in pool:
            if _eligible(inst, e):
                ev.append(e)
                claimed.add(id(e))
        pages = ({int(b[0]) for b in inst.part_boxes} if inst.part_boxes
                 else {inst.page} if inst.page is not None else set())
        if inst.encoding == "page-text":
            for p in sorted(pages):
                if p < len(ch.r3):
                    for form in forms_of(inst):
                        if form in ch.r3[p]:
                            ev.append(Evidence("R3", f"page {p + 1} live text", "whole", "live",
                                               role="content", form=form, page=p))
        if inst.encoding == "pixels":
            for where, storage, text in ch.image_ocr:
                m = ocr_match(inst.written, text)
                if m == "whole":
                    ev.append(Evidence("R2", where, "whole", storage, role="image",
                                       form=inst.written))
                elif m == "near":
                    uncertain = True
        # V: each planted box (or each part's) on its own page.
        boxes = ([(int(b[0]), list(b[1:])) for b in inst.part_boxes] if inst.part_boxes
                 else [(inst.page, inst.bbox)] if inst.page is not None and inst.bbox else [])
        part_for_box = inst.parts if inst.part_boxes else [inst.written]
        for (p, box), form in zip(boxes, part_for_box):
            if p is None or p >= len(ch.pngs):
                continue
            tight = _crop(ch.pngs[p], box)
            text = _ocr_image(_crop(ch.pngs[p], box, pad=10.0), work, f"crop{idx}-{p}", scale=2)
            m = ocr_match(form, text)
            if m == "whole":
                ev.append(Evidence("V", f"page {p + 1} box", "whole", "live", form=form, page=p))
            else:
                if m == "near":
                    uncertain = True
                piece = ocr_partial(form, text)
                if piece and not uniform(tight):
                    ev.append(Evidence("V", f"page {p + 1} box", f"partial:{piece}", "live",
                                       form=form, page=p))
                elif not uniform(tight) and inst.encoding in ("page-text", "pixels") and \
                        not any(x.counts for x in ev):
                    # Ink in the planted box that no reading explains.
                    uncertain = True
        rows.append(InstanceResult(idx, inst.secret, inst.loc, classify(inst, ev), ev,
                                   uncertain))

    # Unattributed: counting hits no instance claimed (and not an R1
    # duplicate of something R2 saw), plus page-level text and OCR on a
    # page where no instance of that secret sits.
    by_form: dict[str, str] = {}
    for inst in truth.instances:
        for f in forms_of(inst):
            by_form.setdefault(f, inst.secret)
    pages_of: dict[str, set[int]] = {}
    for inst in truth.instances:
        ps = {int(b[0]) for b in inst.part_boxes} | ({inst.page} if inst.page is not None
                                                     else set())
        pages_of.setdefault(inst.secret, set()).update(ps)
    extra: dict[str, list[Evidence]] = {}
    for e in pool:
        if id(e) in claimed or not e.counts or _covered_by_r2(e, ch.r2):
            continue
        sid = by_form.get(e.form)
        if sid:
            extra.setdefault(sid, []).append(e)
    for p, text in enumerate(ch.r3):
        for form, sid in by_form.items():
            if form in text and p not in pages_of.get(sid, set()):
                extra.setdefault(sid, []).append(Evidence(
                    "R3", f"page {p + 1} live text", "whole", "live", role="content",
                    form=form, page=p))
    for p, text in enumerate(ch.page_ocr):
        for form, sid in by_form.items():
            if ocr_match(form, text) == "whole" and p not in pages_of.get(sid, set()):
                extra.setdefault(sid, []).append(Evidence(
                    "V", f"page {p + 1} render", "whole", "live", form=form, page=p))
    for n, (sid, ev) in enumerate(sorted(extra.items())):
        rows.append(InstanceResult(-1 - n, sid, "?", classify(None, ev), ev, True))
    return rows


def run(path: Path, truth: DocTruth, *, output: str, sha256: str, method: str,
        work: Path) -> FileResult:
    """Every channel on one output file, then attribution and classes.
    Renders and crops are kept in *work* for the adjudicator."""
    work.mkdir(parents=True, exist_ok=True)
    ch = gather(path, truth, work)
    rows = attribute(truth, ch, work)
    per_secret: dict[str, list[str]] = {}
    for r in rows:
        per_secret.setdefault(r.secret, []).append(r.cls)
    for inst in truth.instances:
        per_secret.setdefault(inst.secret, [])
    secrets = {sid: worst(cl) for sid, cl in sorted(per_secret.items())}
    truth_label = "leak" if any(c != "removed" for c in secrets.values()) else "clean"
    return FileResult(output, sha256, truth.doc, method, rows, secrets, truth_label, ch.notes)


def dumps(result: FileResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n"
