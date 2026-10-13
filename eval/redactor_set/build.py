"""Shared machinery for templates and probes: HTML page scaffolding,
scans, fillable forms, and turning what a builder planted into a truth
manifest (with the checks that it planted exactly that)."""

from __future__ import annotations

import html
import random
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
    """Locate every declared page-text value, check nothing undeclared
    was drawn, and build the truth manifest."""
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
                None, parts=list(split.parts), fingerprints=prints,
                note="part page and box: " + "; ".join(",".join(f"{v:g}" for v in b) for b in boxes)))
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
    if not instances and not built.clean_control:
        raise PlantError(f"{doc_id}: no secret planted")
    return DocTruth(doc_id, seed, built.producer, n_pages, built.title, instances,
                    clean_control=built.clean_control)


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
