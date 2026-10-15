"""Shared machinery for templates and probes: HTML page scaffolding,
scans, fillable forms, and turning what a builder planted into a truth
manifest (with the checks that it planted exactly that)."""

from __future__ import annotations

import html
import random
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

from . import locate, persona, producers
from .truth import DocTruth, Instance

PAGE_W, PAGE_H = 612.0, 792.0

CSS = """
@page { size: Letter; margin: 0.55in 0.6in; }
* { font-variant-ligatures: none; font-feature-settings: "liga" 0, "clig" 0; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #111; margin: 0; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
h1 { font-size: 15pt; margin: 0 0 6pt 0; }
h2 { font-size: 11.5pt; margin: 12pt 0 4pt 0; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 0.6pt solid #888; padding: 3pt 5pt; text-align: left; vertical-align: top; }
th { background: #eee; font-weight: bold; }
.num { text-align: right; }
.small { font-size: 8pt; color: #444; }
.box { border: 0.8pt solid #333; padding: 5pt 7pt; }
.label { font-size: 7pt; color: #444; display: block; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }
.grid > div { border: 0.6pt solid #555; padding: 4pt 6pt; min-height: 30pt; }
.envelope { margin: 40pt 0 26pt 30pt; line-height: 1.35; }
.runhead { border-bottom: 0.8pt solid #333; padding-bottom: 3pt; margin-bottom: 10pt; }
"""


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def html_doc(title: str, pages: list[str]) -> str:
    """A complete HTML document: a fabricated <title> always (Chrome
    otherwise uses the file name as the PDF's /Title)."""
    body = "".join(f'<div class="page">{p}</div>' for p in pages)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{esc(title)}</title>'
            f"<style>{CSS}</style></head><body>{body}</body></html>")


@dataclass
class TextSpec:
    """A value drawn as page text, located after generation."""
    secret: str
    written: str
    loc: str
    count: int                  # occurrences the builder drew, all pages
    note: str = ""


@dataclass
class SplitSpec:
    """A value drawn in pieces (wrapped, in two columns, across pages)."""
    secret: str
    written: str
    loc: str
    parts: list[str]


@dataclass
class Built:
    pdf: Path
    producer: str
    title: str
    text: list[TextSpec] = field(default_factory=list)
    split: list[SplitSpec] = field(default_factory=list)
    other: list[Instance] = field(default_factory=list)   # placed by the builder itself
    clean_control: bool = False
    # Pages carry an OCR engine's invisible text layer, whose content is
    # the engine's reading and so cannot be declared in advance: every
    # secret found there is recorded as its own S2 page-text instance.
    ocr_text_layer: bool = False


class PlantError(RuntimeError):
    """The document does not hold exactly what its builder declared."""


def _occurrences(pages: list[list[locate.Line]]) -> dict[str, int]:
    return {form: len(locate.find(pages, form)) for form in persona.written_forms()}


def finalize(doc_id: str, seed: int, built: Built) -> DocTruth:
    """Locate every declared page-text value and build the truth
    manifest, checking that the document's text holds exactly what was
    declared: page text (every occurrence of every written form), every
    other place text is stored (string objects, XMP, attachments,
    annotation and field appearances; see _store_check), and no
    residue-shaped piece of a secret outside a declared value, in page
    text (_residue_check) or a text store (_store_residue_check). Pixels
    are declared, not checked."""
    pages = locate.page_lines(built.pdf)
    instances: list[Instance] = []
    with pikepdf.open(built.pdf) as pdf:
        n_pages = len(pdf.pages)
        for spec in built.text:
            hits = locate.find(pages, spec.written)
            if len(hits) != spec.count:
                raise PlantError(f"{doc_id}: {spec.written!r} drawn {len(hits)} times, "
                                 f"declared {spec.count}")
            for page_no, bbox in hits:
                instances.append(Instance(
                    spec.secret, spec.written, spec.loc, "page-text", page_no, bbox,
                    fingerprints=locate.fingerprints(pdf, page_no, spec.written), note=spec.note))
        for split in built.split:
            if locate.find(pages, split.written):
                raise PlantError(f"{doc_id}: split value {split.written!r} is drawn whole")
            first_page, boxes, prints = None, [], []
            for part in split.parts:
                hits = locate.find(pages, part)
                if not hits:
                    raise PlantError(f"{doc_id}: part {part!r} of a split value not found")
                page_no, bbox = hits[0]
                first_page = page_no if first_page is None else first_page
                boxes.append([page_no, *bbox])
                prints.extend(locate.fingerprints(pdf, page_no, part))
            instances.append(Instance(
                split.secret, split.written, split.loc, "page-text", first_page,
                None, parts=list(split.parts), part_boxes=boxes, fingerprints=prints))
    # Nothing drawn that was not declared: every written form's page-text
    # occurrences are exactly the declared ones.
    declared: dict[str, int] = {}
    for spec in built.text:
        declared[spec.written] = declared.get(spec.written, 0) + spec.count
    for form, seen in _occurrences(pages).items():
        if seen == declared.get(form, 0):
            continue
        if built.ocr_text_layer and seen > declared.get(form, 0) and form not in declared:
            with pikepdf.open(built.pdf) as pdf:
                for page_no, bbox in locate.find(pages, form):
                    instances.append(Instance(
                        persona.written_forms()[form], form, "S2", "page-text", page_no, bbox,
                        fingerprints=locate.fingerprints(pdf, page_no, form),
                        note="the OCR engine's invisible text layer"))
            continue
        raise PlantError(f"{doc_id}: {form!r} appears {seen} times in page text, "
                         f"declared {declared.get(form, 0)}")
    instances.extend(built.other)
    instances.extend(_residue_check(doc_id, pages, built, instances))
    _store_check(doc_id, built.pdf, instances)
    _store_residue_check(doc_id, built.pdf)
    if not instances and not built.clean_control:
        raise PlantError(f"{doc_id}: no secret planted")
    return DocTruth(doc_id, seed, built.producer, n_pages, built.title, instances,
                    clean_control=built.clean_control)


def _residue_check(doc_id: str, pages: list[list[locate.Line]], built: Built,
                   instances: list[Instance]) -> list[Instance]:
    """Refuse page text holding a residue-shaped piece of a secret
    (persona.residue_in) outside a declared value: filler must not look
    like a partial leak. In an OCR engine's text layer such a piece is
    the engine's (mis)reading of a declared value, so it is recorded as
    a partial S2 instance instead."""
    blank = sorted({i.written for i in instances} | {p for i in instances for p in i.parts}
                   | set(persona.written_forms()), key=len, reverse=True)
    extra: list[Instance] = []
    for page_no, lines in enumerate(pages):
        for line in lines:
            text = line.text
            for value in blank:
                text = text.replace(value, " " * len(value))
            for piece in persona.residue_in(text):
                owner = next(sid for form, sid in persona.written_forms().items()
                             if piece.lower() in form.lower() or piece in re.sub(r"\D", "", form))
                if built.ocr_text_layer:
                    extra.append(Instance(owner, piece, "S2", "page-text", page_no, None,
                                          note="partial: the OCR text layer's reading"))
                    continue
                raise PlantError(f"{doc_id}: page {page_no + 1} text holds {piece!r}, a piece "
                                 f"of {owner} outside any declared value")
    return extra


def _page_stream_ids(pdf: pikepdf.Pdf) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """(page content and drawn form XObjects, annotation appearance streams
    and the forms they draw)."""
    def forms(resources: object, into: set[tuple[int, int]]) -> None:
        if not isinstance(resources, pikepdf.Dictionary):
            return
        xobjects = resources.get("/XObject")
        if isinstance(xobjects, pikepdf.Dictionary):
            for k in xobjects.keys():
                xo = xobjects[k]
                if isinstance(xo, pikepdf.Stream) and xo.get("/Subtype") == "/Form" \
                        and xo.objgen not in into:
                    into.add(xo.objgen)
                    forms(xo.get("/Resources"), into)

    page_ids: set[tuple[int, int]] = set()
    ap_ids: set[tuple[int, int]] = set()
    for page in pdf.pages:
        contents = page.obj.get("/Contents")
        for c in (contents if isinstance(contents, pikepdf.Array) else [contents]):
            if isinstance(c, pikepdf.Stream):
                page_ids.add(c.objgen)
        forms(page.obj.get("/Resources"), page_ids)
        for annot in page.obj.get("/Annots", pikepdf.Array()):
            ap = annot.get("/AP")
            if not isinstance(ap, pikepdf.Dictionary):
                continue
            for key in ap.keys():
                entry = ap[key]
                streams = [entry] if isinstance(entry, pikepdf.Stream) else (
                    [entry[k] for k in entry.keys()] if isinstance(entry, pikepdf.Dictionary)
                    else [])
                for st in streams:
                    if isinstance(st, pikepdf.Stream):
                        ap_ids.add(st.objgen)
                        forms(st.get("/Resources"), ap_ids)
    return page_ids, ap_ids


@dataclass(frozen=True)
class Store:
    """One place a document stores text outside page content."""
    kind: str            # "string", "xmp", "embedded", "appearance" or "other"
    key: str             # the dictionary key holding a string ("" for a stream)
    text: str            # decoded: a string's text, a stream's bytes as Latin-1
    data: bytes          # the string's or stream's bytes


def text_stores(pdf: pikepdf.Pdf) -> list[Store]:
    """Every string object (in every object, every stream's dictionary
    and the trailer, at any depth, decoded whatever its form -- hex,
    escaped, UTF-16) and every decodable stream that is not page content,
    a font program or an image."""
    page_ids, ap_ids = _page_stream_ids(pdf)
    font_files: set[tuple[int, int]] = set()
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/FontDescriptor":
            for k in ("/FontFile", "/FontFile2", "/FontFile3"):
                if k in obj:
                    font_files.add(obj[k].objgen)
    stores: list[Store] = []

    def walk(obj: object, key: str = "", depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(obj, pikepdf.String):
            stores.append(Store("string", key, str(obj), bytes(obj)))
        elif isinstance(obj, pikepdf.Array):
            for item in obj:
                if not (isinstance(item, pikepdf.Object) and item.is_indirect):
                    walk(item, key, depth + 1)
        elif isinstance(obj, pikepdf.Dictionary):
            for k in obj.keys():
                item = obj[k]
                if not (isinstance(item, pikepdf.Object) and item.is_indirect):
                    walk(item, k, depth + 1)

    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream):
            walk(obj)
            continue
        walk(obj.stream_dict)
        if obj.objgen in page_ids or obj.objgen in font_files or obj.get("/Subtype") == "/Image":
            continue
        kind = ("xmp" if obj.get("/Type") == "/Metadata"
                else "embedded" if obj.get("/Type") == "/EmbeddedFile"
                else "appearance" if obj.objgen in ap_ids else "other")
        try:
            data = obj.read_bytes()
        except pikepdf.PdfError:
            continue
        stores.append(Store(kind, "", data.decode("latin-1"), data))
    walk(pdf.trailer)
    return stores


def store_occurrences(path: Path) -> set[tuple[str, str]]:
    """(written form, store kind) for every secret form found in a text
    store (text_stores): a string object ("string"), an XMP stream
    ("xmp"), an embedded file ("embedded"), an annotation or field
    appearance ("appearance"), or any other decodable stream ("other").
    Font programs and images are the preflight's concern."""
    forms = list(persona.written_forms())
    found: set[tuple[str, str]] = set()
    with pikepdf.open(path) as pdf:
        for store in text_stores(pdf):
            for f in forms:
                if (f in store.text or f.encode() in store.data
                        or f.encode("utf-16-be") in store.data):
                    found.add((f, store.kind))
    return found


# Keys whose strings are machine-written numbers (dates, identifiers),
# where four digits of a secret occur by chance; and the same inside XMP.
_NUMERIC_KEYS = {"/CreationDate", "/ModDate", "/ID", "/M", "/CheckSum", "/Size",
                 "/Producer", "/Creator"}   # producers' own version strings
# Machine-written values wherever they sit: a tagged PDF's element ids
# (Chrome writes node00048 in /ID, /Headers and the /IDTree), and dates.
_MACHINE_VALUE = re.compile(r"node\d+|D:[\d+\-Z']+")
_XMP_NUMERIC = re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?|uuid:[0-9a-fA-F-]+")


def _store_residue_check(doc_id: str, path: Path) -> None:
    """Text stores outside page content hold no residue-shaped piece of a
    secret outside a written form (persona.residue_in), except in
    machine-written dates and identifiers."""
    forms = sorted(persona.written_forms(), key=len, reverse=True)
    with pikepdf.open(path) as pdf:
        stores = text_stores(pdf)
    for store in stores:
        if store.key in _NUMERIC_KEYS or store.kind in ("appearance", "other") or (
                store.kind == "string" and _MACHINE_VALUE.fullmatch(store.text)):
            continue        # appearances repeat their strings; "other" is refused whole
        text = store.text
        if store.kind == "xmp":
            text = _XMP_NUMERIC.sub(" ", text)
        for form in forms:
            text = text.replace(form, " ")
        pieces = persona.residue_in(text)
        if pieces:
            where = f"{store.kind} {store.key}".strip()
            raise PlantError(f"{doc_id}: {where} holds {pieces[0]!r}, a piece of a secret "
                             "outside any declared value")


# Which declared instances may account for a form found in each store.
_STORE_ACCOUNTS = {
    "string": lambda i: i.encoding in ("string", "embedded"),
    "xmp": lambda i: i.encoding == "xmp",
    "embedded": lambda i: i.encoding == "embedded",
    "appearance": lambda i: i.encoding == "string" and i.loc in ("F1", "F2", "A1", "A1x"),
}
# Where a declared instance of each encoding may be found.
_INSTANCE_STORES = {"string": {"string", "appearance"}, "xmp": {"xmp"},
                    "embedded": {"embedded", "string"}}


def _store_check(doc_id: str, path: Path, instances: list[Instance]) -> None:
    """Every secret stored outside page content is declared, and every
    declared non-page instance is really stored: the truth manifest is
    complete for text (an oracle trusts it to be)."""
    found = store_occurrences(path)
    for form, kind in sorted(found):
        accounts = _STORE_ACCOUNTS.get(kind)
        if accounts is None or not any(i.written == form and accounts(i) for i in instances):
            raise PlantError(f"{doc_id}: {form!r} is stored in a {kind} store no declared "
                             "instance accounts for")
    for inst in instances:
        stores = _INSTANCE_STORES.get(inst.encoding)
        if stores and not any((inst.written, k) in found for k in stores):
            raise PlantError(f"{doc_id}: declared {inst.secret} ({inst.loc}) is not stored "
                             "anywhere in the file")


# ── Chrome documents ───────────────────────────────────────────────────

def chrome(work: Path, name: str, title: str, pages: list[str]) -> Path:
    out = work / f"{name}.pdf"
    producers.chrome_pdf(html_doc(title, pages), out)
    return out


# ── Scans ──────────────────────────────────────────────────────────────

def scan_pages(src_pdf: Path, rng: random.Random, work: Path,
               specs: list[TextSpec], loc: str, *, dpi: int = producers.SCAN_DPI,
               page_offset: int = 0) -> tuple[list[Path], list[Instance]]:
    """Rasterise a Chrome-printed page set, degrade it like a scan, and
    map each declared value's box through the skew. Returns the JPEG
    page images and pixel-only instances (``loc`` S1 or S2)."""
    pages = locate.page_lines(src_pdf)
    raster_dir = Path(tempfile.mkdtemp(prefix="rs-raster-", dir=work))
    pngs = producers.rasterise(src_pdf, dpi, raster_dir)
    jpgs, skews = [], []
    for i, png in enumerate(pngs):
        jpg = work / f"{src_pdf.stem}-scan-{i}.jpg"
        skews.append(producers.degrade(png, jpg, rng))
        jpgs.append(jpg)
    shutil.rmtree(raster_dir, ignore_errors=True)
    instances = []
    for spec in specs:
        hits = locate.find(pages, spec.written)
        if len(hits) != spec.count:
            raise PlantError(f"scan source: {spec.written!r} drawn {len(hits)} times, "
                             f"declared {spec.count}")
        for page_no, bbox in hits:
            mapped = skews[page_no].map_box(bbox, dpi / 72)
            instances.append(Instance(spec.secret, spec.written, loc, "pixels",
                                      page_no + page_offset, [round(v, 2) for v in mapped],
                                      note=spec.note))
    for form, seen in _occurrences(pages).items():
        want = sum(s.count for s in specs if s.written == form)
        if seen != want:
            raise PlantError(f"scan source: {form!r} appears {seen} times, declared {want}")
    return jpgs, instances


# ── Fillable forms (reportlab) ─────────────────────────────────────────

@dataclass
class Field:
    page: int
    name: str
    label: str
    x: float          # top-left-origin points
    y: float
    w: float
    h: float = 16
    value: str = ""
    kind: str = "text"            # text | comb | checkbox | choice
    options: tuple[str, ...] = ()
    secret: str | None = None     # persona id, when value is a secret
    loc: str = "F1"


def form_pdf(out: Path, *, title: str, heading: list[str], static: list[tuple[int, float, float, str, int]],
             fields: list[Field], n_pages: int) -> list[Instance]:
    """A fillable form with real /AcroForm fields whose /V and /AP carry
    the values. *static* is (page, x, y, text, size) drawn as page text."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(out), pagesize=(PAGE_W, PAGE_H), invariant=1)
    c.setTitle(title)
    c.setAuthor("")
    c.setCreator("")
    form = c.acroForm
    instances: list[Instance] = []
    for page in range(n_pages):
        c.setFont("Helvetica-Bold", 13)
        c.drawString(40, PAGE_H - 44, heading[page] if page < len(heading) else heading[0])
        c.setFont("Helvetica", 7)
        c.drawRightString(PAGE_W - 40, PAGE_H - 44, f"Page {page + 1} of {n_pages}")
        for p, x, y, text, size in static:
            if p == page:
                c.setFont("Helvetica", size)
                c.drawString(x, PAGE_H - y, text)
        for f in fields:
            if f.page != page:
                continue
            c.setFont("Helvetica", 7)
            c.drawString(f.x, PAGE_H - f.y + 3, f.label)
            bottom = PAGE_H - f.y - f.h
            if f.kind in ("text", "comb"):
                extra = {"fieldFlags": "comb", "maxlen": len(f.value)} if f.kind == "comb" else {}
                form.textfield(name=f.name, value=f.value, x=f.x, y=bottom, width=f.w,
                               height=f.h, fontName="Helvetica", fontSize=10,
                               borderWidth=0.6, **extra)
            elif f.kind == "checkbox":
                form.checkbox(name=f.name, x=f.x, y=bottom, size=f.h, checked=f.value == "Yes",
                              buttonStyle="check", borderWidth=0.6)
            elif f.kind == "choice":
                form.choice(name=f.name, value=f.value, options=list(f.options), x=f.x,
                            y=bottom, width=f.w, height=f.h, fontName="Helvetica",
                            fontSize=10, fieldFlags="combo", borderWidth=0.6)
            if f.secret:
                instances.append(Instance(f.secret, f.value, f.loc, "string", page,
                                          [f.x, f.y, f.x + f.w, f.y + f.h],
                                          note=f"field {f.name}"))
        c.showPage()
    c.save()
    return instances
