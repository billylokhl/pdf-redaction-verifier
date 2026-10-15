"""Post-processing with pikepdf (libqpdf, not MuPDF): the parts of a
document a page-text redactor might not reach -- annotations, document
metadata, outlines, attachments and page thumbnails -- and joining
documents from different producers into one file.

Coordinates passed in are top-left-origin points (truth.py's frame);
this module flips them into PDF user space itself.
"""

from __future__ import annotations

import zlib
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, String


def _rect(page: pikepdf.Page, box: list[float]) -> Array:
    height = float(page.mediabox[3]) - float(page.mediabox[1])
    x0, y0, x1, y1 = box
    return Array([x0, height - y1, x1, height - y0])


def _helv_ap(pdf: pikepdf.Pdf, width: float, height: float, text: str, size: float = 9) -> Any:
    """A minimal appearance stream drawing *text* in Helvetica on a pale
    box, so every renderer shows the same thing."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = (f"q 1 1 0.85 rg 0 0 {width:.2f} {height:.2f} re f Q "
            f"BT /Helv {size} Tf 0 g 3 {height - size - 2:.2f} Td ({safe}) Tj ET").encode()
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                      Encoding=Name.WinAnsiEncoding)
    return pdf.make_stream(body, Type=Name.XObject, Subtype=Name.Form,
                           BBox=Array([0, 0, width, height]),
                           Resources=Dictionary(Font=Dictionary(Helv=font)))


def add_annotation(pdf: pikepdf.Pdf, page_no: int, kind: str, box: list[float],
                   text: str, *, uri: str | None = None) -> None:
    """Add a FreeText, Text (sticky note), Highlight or Link annotation."""
    page = pdf.pages[page_no]
    rect = _rect(page, box)
    common: dict[str, Any] = {"Type": Name.Annot, "Rect": rect, "F": 4}
    if kind == "FreeText":
        w, h = box[2] - box[0], box[3] - box[1]
        annot = Dictionary(**common, Subtype=Name.FreeText, Contents=String(text),
                           DA=String("/Helv 9 Tf 0 g"),
                           AP=Dictionary(N=_helv_ap(pdf, w, h, text)))
    elif kind == "Text":
        annot = Dictionary(**common, Subtype=Name.Text, Contents=String(text),
                           Name=Name.Comment, Open=False, C=Array([1, 0.8, 0]))
    elif kind == "Highlight":
        x0, y0, x1, y1 = (float(v) for v in rect)
        annot = Dictionary(**common, Subtype=Name.Highlight, Contents=String(text),
                           QuadPoints=Array([x0, y1, x1, y1, x0, y0, x1, y0]),
                           C=Array([1, 1, 0]), CA=0.4)
    elif kind == "Link":
        annot = Dictionary(**common, Subtype=Name.Link, Border=Array([0, 0, 0]),
                           A=Dictionary(S=Name.URI, URI=String(uri or text)))
    else:
        raise ValueError(f"unknown annotation kind {kind!r}")
    if "/Annots" not in page.obj:
        page.obj.Annots = Array()
    page.obj.Annots.append(pdf.make_indirect(annot))


def set_info(pdf: pikepdf.Pdf, fields: dict[str, str]) -> None:
    """Replace the document Info dictionary with exactly *fields* (keys
    without the slash), keeping the producer's own Producer/Creator."""
    keep = {k: pdf.docinfo[k] for k in ("/Producer", "/Creator") if k in pdf.docinfo}
    info = Dictionary({**keep, **{f"/{k}": String(v) for k, v in fields.items()}})
    pdf.trailer.Info = pdf.make_indirect(info)


def set_xmp(pdf: pikepdf.Pdf, *, creator: str, title: str, keywords: str) -> None:
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
        meta["dc:creator"] = [creator]
        meta["dc:title"] = title
        meta["pdf:Keywords"] = keywords


def add_outline(pdf: pikepdf.Pdf, titles: list[tuple[str, int]]) -> None:
    with pdf.open_outline() as outline:
        for title, page_no in titles:
            outline.root.append(pikepdf.OutlineItem(title, page_no))


def attach(pdf: pikepdf.Pdf, name: str, data: bytes, description: str) -> None:
    # Fixed dates, so the same input gives the same bytes.
    spec = pikepdf.AttachedFileSpec(pdf, data, description=description, filename=name,
                                    mime_type="text/csv", creation_date="D:20260101000000Z",
                                    mod_date="D:20260101000000Z")
    pdf.attachments[name] = spec


def add_thumbnail(pdf: pikepdf.Pdf, page_no: int, png: Path) -> None:
    """Attach a page thumbnail (/Thumb): an 8-bit grey image of the page,
    as Acrobat-style producers store it. Thumbnails are never redrawn by
    a viewer from the page, so a stale one keeps what the page showed."""
    from PIL import Image

    with Image.open(png) as im:
        grey = im.convert("L")
        w, h, raw = grey.width, grey.height, grey.tobytes()
    thumb = pdf.make_stream(zlib.compress(raw), Width=w, Height=h, BitsPerComponent=8,
                            ColorSpace=Name.DeviceGray, Filter=Name.FlateDecode)
    pdf.pages[page_no].obj.Thumb = thumb


def concatenate(parts: list[Path], out: Path) -> None:
    """Join PDFs page by page (the first part's document-level data wins)."""
    with pikepdf.open(parts[0]) as first:
        for extra in parts[1:]:
            with pikepdf.open(extra) as other:
                first.pages.extend(other.pages)
        save(first, out)


def edit(path: Path, out: Path | None = None) -> pikepdf.Pdf:
    """Open *path* for editing; the caller saves with :func:`save`."""
    return pikepdf.open(path, allow_overwriting_input=out is None)


def save(pdf: pikepdf.Pdf, out: Path) -> None:
    # A content-derived /ID: the same input bytes give the same output
    # bytes. Not qpdf's static /ID, whose hex digits (31415926535897...)
    # form a 16-digit run a card-number pattern rule would match.
    pdf.save(out, deterministic_id=True)
