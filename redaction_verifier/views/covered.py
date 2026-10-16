"""Images painted over on the page (K12, K38; ADR 0004's drawn strips
under a box, the legacy hotfix of 2026-10-15).

OCR reads the rendered page, so an image with an opaque box drawn over
it reads as the box: the pixels are still in the file and nothing reads
them. Two checks, both on the page as drawn:

- `covered_images`: every image drawn on the page (Page.get_bboxlog, in
  drawing order) with the largest fraction of its visible area that one
  opaque fill drawn after it covers (Page.get_drawings, matched to the
  log by its sequence number). An image covered at least
  COVERED_FRACTION is flagged (IMAGE_COVERED, exit 2): what lies under
  the fill was never read.
- `images_only_page`: the page rebuilt from its images alone, each
  decoded and placed at its drawn rectangle, with nothing painted over
  them, so OCR reads what the box hides; strips drawn apart in one
  render (K38) rejoin as they were drawn. A listed value read there is
  a finding (exit 1). A page is rebuilt only when it has a covered
  image.

Measured before the owner's decision (Lane G spike, 2026-10-15): on the
case library the 25% threshold flags exactly K12 and K38 and no clean
case; on the 2,073-file local corpus it flags 10 files (1 text-bearing),
and images-only OCR of their 60 pages took 5.8 s and read no listed
value.

Limits, all on the side of reading less, never of passing a covered
image: only one area's coverage counts (several small boxes adding up
are not summed); a fill's bounding rectangle stands for its area (a
diagonal shape counts as its box); an image painted over by another
image, by text or by a stroke (however wide) is not a cover; inline
images and images over MAX_IMAGE_PIXELS are not rebuilt (still flagged).
Coverage is measured in the page's unrotated space, where the draw log
reports, and against any clip path overlapping the image as well as the
whole image, so a box over the visible window of a clipped image counts.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf as fitz

# The fraction of an image's area one opaque fill must cover to flag it
# (owner decision, 2026-10-15).
COVERED_FRACTION = 0.25
# A fill at least this opaque hides what is under it.
OPAQUE = 0.99
# An image larger than this is not decoded for the rebuilt page (ADR
# 0004's per-image cap); its page is still flagged.
MAX_IMAGE_PIXELS = 35_000_000


@dataclass(frozen=True)
class CoveredImage:
    bbox: fitz.Rect
    fraction: float


def _area(rect: fitz.Rect) -> float:
    return max(0.0, rect.width) * max(0.0, rect.height)


def unrotated_rect(page: fitz.Page) -> fitz.Rect:
    """*page*'s rectangle before its /Rotate: the space get_bboxlog,
    get_drawings and get_image_info report in."""
    return fitz.Rect(0, 0, page.cropbox.width, page.cropbox.height)


def _finite(rect: fitz.Rect) -> bool:
    """A real painted area: not empty, not MuPDF's infinite bbox of a
    degenerate path (`10 10 m f` paints nothing)."""
    return (not rect.is_empty and not rect.is_infinite
            and all(abs(v) < 1e9 for v in (rect.x0, rect.y0, rect.x1, rect.y1)))


def _covers(page: fitz.Page) -> list[tuple[int, fitz.Rect]]:
    """Every opaque area painted on *page*, with its index in the
    page's draw log: each filled path (`fill-path`) and shading
    (`fill-shade`). A fill counts unless get_drawings shows it see-through;
    the two are paired in order by rectangle, since get_drawings skips
    paths the log keeps, so its sequence numbers are not log indices. A
    fill with no exact partner counts as opaque."""
    see = [(fitz.Rect(d["rect"]), d.get("fill_opacity"))
           for d in sorted(page.get_drawings(), key=lambda d: d["seqno"])
           if d.get("fill") is not None]
    out: list[tuple[int, fitz.Rect]] = []
    k = 0
    for i, (kind, bbox) in enumerate(page.get_bboxlog()):
        if kind not in ("fill-path", "fill-shade"):
            continue
        rect = fitz.Rect(bbox)
        if not _finite(rect):
            continue
        opacity = None
        if kind == "fill-path" and k < len(see) and _same(see[k][0], rect):
            opacity = see[k][1]
            k += 1
        if opacity is None or opacity >= OPAQUE:
            out.append((i, rect))
    return out


def _same(a: fitz.Rect, b: fitz.Rect) -> bool:
    return all(abs(x - y) < 0.01 for x, y in ((a.x0, b.x0), (a.y0, b.y0), (a.x1, b.x1), (a.y1, b.y1)))


def covered_images(page: fitz.Page) -> list[CoveredImage]:
    """Every image drawn on *page* (inside the page) with the largest
    fraction of it that one opaque area painted after it covers, in
    drawing order. The fraction is of the image's area and, where a clip
    path overlaps it, of the part of it inside that clip too (the log
    gives an image's unclipped rectangle), whichever is larger."""
    bounds = unrotated_rect(page)
    covers = _covers(page)
    clips = [fitz.Rect(d["scissor"]) for d in page.get_drawings(extended=True)
             if d.get("type") == "clip" and d.get("scissor") is not None]
    out: list[CoveredImage] = []
    for i, (kind, bbox) in enumerate(page.get_bboxlog()):
        if kind != "fill-image":
            continue
        image = fitz.Rect(bbox) & bounds
        if _area(image) <= 0:
            continue
        windows = [image] + [w for w in (image & c for c in clips if _finite(c)) if _area(w) > 0]
        best = 0.0
        for seqno, cover in covers:
            if seqno > i:
                for window in windows:
                    best = max(best, _area(window & cover) / _area(window))
        out.append(CoveredImage(image, best))
    return out


def is_covered(page: fitz.Page) -> bool:
    return any(c.fraction >= COVERED_FRACTION for c in covered_images(page))


def _quarter_turn(transform: tuple[float, ...]) -> int:
    """The rotation, in degrees, nearest the image's placement."""
    a, b = transform[0], transform[1]
    if abs(b) > abs(a):   # the image's x axis runs vertically
        return 90 if b > 0 else 270
    return 180 if a < 0 else 0


def images_only_page(page: fitz.Page) -> fitz.Document:
    """A one-page document, the size of *page*, holding only *page*'s
    images, each at its drawn rectangle turned by the nearest quarter
    turn. An image that cannot be decoded, is inline or is over
    MAX_IMAGE_PIXELS is left out. The caller closes it."""
    doc = page.parent
    bounds = unrotated_rect(page)
    out = fitz.open()
    try:
        new = out.new_page(width=bounds.width, height=bounds.height)
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref") or 0
            rect = fitz.Rect(info["bbox"]) & bounds
            if xref <= 0 or rect.is_empty:
                continue
            if info.get("width", 0) * info.get("height", 0) > MAX_IMAGE_PIXELS:
                continue
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                new.insert_image(rect, pixmap=pix, rotate=_quarter_turn(info["transform"]),
                                 keep_proportion=False)
            except Exception:
                continue
        new.set_rotation(page.rotation)   # read upright as the page is shown
    except BaseException:
        out.close()
        raise
    return out
