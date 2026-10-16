"""redaction_verifier.visible_map.locate — where on a page a finding is.

Text. The map must box exactly what the scan matched, so it reads each
page through the scan's own reading code: views.text.visual_text_glyphs
gives extract_visual_text's readings with, per line, the glyphs it was
read from (and each glyph's box and whether it is visible). A match in a
line then names its glyphs, and the box is their union. Nothing here
rebuilds a reading.

Matches are found with the scan's own matchers and tiers: a value is
searched per normalized line of the genuine readings (0-6), as the hard
tier does, across the seam with the previous page's last line in the
seam readings (0-4) as SecretMatcher.crossing does, and a pattern per
raw line of the genuine readings with no separator collapsing. The
readings overlap, so one occurrence (one set of glyphs) is kept once.
Readings 0-2 are visible text, 3 is text outside the visible area, and
4-6 read across the visible edge: a hit there is boxed where its glyphs
are visible, and listed as outside the crop where they are not.

Frames. PyMuPDF reports glyphs, and takes get_text clips, in an
unrotated frame whose origin is the top-left of the page's VISIBLE area
(CropBox clipped to MediaBox), at every rotation; its rotation_matrix is
built from the unclipped CropBox, so where a rotated page's CropBox
reaches past its MediaBox that matrix is offset from the glyph frame.
The map converts glyph-frame rectangles with display_matrix(), built
from the displayed size alone. Annotation rectangles are a different
matter: Annot.rect lives in the rotation_matrix frame, and
Annot.rect * rotation_matrix is the right displayed rectangle (as is a
link's "from"); to clip page text under one, the map takes the
displayed box back through display_matrix(), never derotation_matrix.

Annotations. A box is the annotation's rectangle in displayed
coordinates; a link's "from" already is.

Every box is verified independently of how it was computed: the glyphs
whose centres lie under a text box (through the page's derotation), read
in the scan's geometric orders, must contain the match; an annotation
box must agree with the annotation's raw /Rect mapped through the page's
own CropBox. A box that fails is dropped and its occurrence listed as
"could not be positioned".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import pymupdf as fitz

from redaction_verifier.matching import PatternRule, _fold_for_patterns, normalize_string
from redaction_verifier.model import Secret
from redaction_verifier.views import (
    TEXT_GENUINE_READINGS,
    TEXT_SEAM_READINGS,
    _reconstruct,
    visual_text_glyphs,
)

PAD = 1.0                    # points added around a text box
MIN_SIDE = 4.0               # the smallest box drawn, per side
_MAX_COORD = 1e6             # a rectangle beyond this is not placeable

Glyph = tuple[float, float, float, float, str]

# PyMuPDF's widget types and the password field flag (PDF Ff bit 14);
# its bundled stubs do not declare the PDF_WIDGET_TYPE_* constants.
TEXT = getattr(fitz, "PDF_WIDGET_TYPE_TEXT", 7)
CHECKBOX = getattr(fitz, "PDF_WIDGET_TYPE_CHECKBOX", 2)
COMBOBOX = getattr(fitz, "PDF_WIDGET_TYPE_COMBOBOX", 3)
LISTBOX = getattr(fitz, "PDF_WIDGET_TYPE_LISTBOX", 4)
RADIOBUTTON = getattr(fitz, "PDF_WIDGET_TYPE_RADIOBUTTON", 5)
PASSWORD = 1 << 13


@dataclass
class PageText:
    """One page's genuine readings (views.text.visual_text_glyphs, the
    first TEXT_GENUINE_READINGS), each a list of lines, each line the
    glyph indices it was read from; and per glyph its character, box (in
    the glyph frame) and whether it is visible."""

    lines: list[list[list[int]]]
    chars: Sequence[str]
    boxes: Sequence[tuple[float, float, float, float]]
    visible: Sequence[bool]

    def raw(self, glyphs: Sequence[int]) -> str:
        return "".join(self.chars[g] for g in glyphs)


def page_text(page: fitz.Page) -> PageText | None:
    """The page's genuine readings with their glyphs, from the scan's own
    reading code (views.text.visual_text_glyphs); None when it cannot tag
    the page's glyphs."""
    tagged = visual_text_glyphs(page)
    if tagged is None:
        return None
    return PageText([[list(line) for line in lines]
                     for lines in tagged.readings[:TEXT_GENUINE_READINGS]],
                    tagged.chars, tagged.boxes, tagged.visible)


def _owners(raw: str, fold: Callable[[str], str]) -> tuple[str, list[int]] | None:
    """*fold*(raw) with, per output character, the raw character it came
    from — or None when folding character by character does not give
    the same text as folding the whole (then positions are unknown)."""
    out: list[str] = []
    owners: list[int] = []
    for k, ch in enumerate(raw):
        piece = fold(ch)
        out.append(piece)
        owners.extend([k] * len(piece))
    text = "".join(out)
    return (text, owners) if text == fold(raw) else None


@dataclass
class TextHit:
    """One located occurrence of a rule on a page: the glyphs it covers
    (all of the line's when *approx*)."""

    page: int
    rule: str
    reading: int
    glyphs: list[int]
    approx: bool = False
    part: bool = False
    sample: str = ""        # the matched raw text, for patterns (masked when shown)
    needle: str = ""        # what the verifying clip must contain, normalized


def _distinct(hits: list[TextHit]) -> list[TextHit]:
    """One hit per set of glyphs: the readings overlap (reading 4 repeats
    the visible lines of reading 0 and carries them across the edge;
    5-6 repeat 1-2), so the same occurrence is often read twice. The
    first reading that reads it keeps it."""
    seen: set[frozenset[int]] = set()
    out = []
    for hit in hits:
        key = frozenset(hit.glyphs)
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def value_hits(pt: PageText, page_no: int, secret: Secret) -> list[TextHit]:
    """Every occurrence of a value on one line of a genuine reading."""
    hits: list[TextHit] = []
    for reading, lines in enumerate(pt.lines):
        for glyphs in lines:
            raw = pt.raw(glyphs)
            if secret.normalized not in normalize_string(raw):
                continue
            mapped = _owners(raw, normalize_string)
            if mapped is None:
                hits.append(TextHit(page_no, secret.name, reading, list(glyphs), approx=True,
                                    needle=secret.normalized))
                continue
            text, owners = mapped
            start = text.find(secret.normalized)
            while start >= 0:
                end = start + len(secret.normalized)
                hits.append(TextHit(page_no, secret.name, reading,
                                    list(glyphs[owners[start]:owners[end - 1] + 1]),
                                    needle=secret.normalized))
                start = text.find(secret.normalized, start + 1)
    return _distinct(hits)


def pattern_hits(pt: PageText, page_no: int, rule: PatternRule) -> list[TextHit]:
    """Every validated match of a pattern on one raw line of a genuine
    reading — match_patterns' own search, continued past the first hit.
    A line whose fold changes its length is boxed whole, marked approx."""
    hits: list[TextHit] = []
    for reading, lines in enumerate(pt.lines):
        for glyphs in lines:
            raw = pt.raw(glyphs)
            folded = _fold_for_patterns(raw)
            mapped = _owners(raw, _fold_for_patterns)
            pos = 0
            while pos <= len(folded):
                m = rule.regex.search(folded, pos)
                if m is None:
                    break
                matched = m.group(0)
                if not matched:
                    pos = m.end() + 1
                    continue
                if rule.validator is not None and not rule.validator(matched):
                    pos = m.start() + 1
                    continue
                needle = needle_of(matched)
                if mapped is None:
                    hits.append(TextHit(page_no, rule.name, reading, list(glyphs), approx=True,
                                        sample=matched, needle=needle))
                else:
                    owners = mapped[1]
                    hits.append(TextHit(page_no, rule.name, reading,
                                        list(glyphs[owners[m.start()]:owners[m.end() - 1] + 1]),
                                        sample=matched, needle=needle))
                pos = m.end()
    return _distinct(hits)


def seam_hits(prev: PageText, cur: PageText, page_no: int, secret: Secret) -> list[TextHit]:
    """A value running from the previous page's last line onto this
    page's first line, in a genuine reading: one part-hit on each page."""
    hits: list[TextHit] = []
    needle = secret.normalized
    for reading in range(min(len(prev.lines), len(cur.lines), TEXT_SEAM_READINGS)):
        left_lines = [g for g in prev.lines[reading] if normalize_string(prev.raw(g))]
        right_lines = [g for g in cur.lines[reading] if normalize_string(cur.raw(g))]
        if not (left_lines and right_lines):
            continue
        left = _owners(prev.raw(left_lines[-1]), normalize_string)
        right = _owners(cur.raw(right_lines[0]), normalize_string)
        if left is None or right is None:
            continue
        joined, cut = left[0] + right[0], len(left[0])
        start = joined.find(needle, max(0, cut - len(needle) + 1))
        if not 0 <= start < cut < start + len(needle):
            continue
        end = start + len(needle)
        lg, rg = left_lines[-1], right_lines[0]
        hits.append(TextHit(page_no - 1, secret.name, reading,
                            list(lg[left[1][start]:left[1][cut - 1] + 1]), part=True,
                            needle=joined[start:cut]))
        hits.append(TextHit(page_no, secret.name, reading,
                            list(rg[right[1][0]:right[1][end - cut - 1] + 1]), part=True,
                            needle=joined[cut:end]))
        break
    return hits


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", "", _fold_for_patterns(text))


def needle_of(raw: str) -> str:
    """What a verifying clip must contain for the raw text *raw*."""
    return normalize_string(raw) or _squeeze(raw)


@dataclass
class Split:
    """A hit's glyphs, split at the visible edge: the visible ones (and
    what a box over them must read), and whether any lie off the page."""

    visible: list[int]
    needle: str
    off_page: bool


def split_visible(pt: PageText, hit: TextHit) -> Split | None:
    """*hit* split at the visible edge, or None when its visible part
    cannot be told (a hit boxed as a whole line, approx, that runs off
    the page). A hit wholly on the page keeps its own needle; a hit
    partly off it gets its visible glyphs' own text as the needle (empty
    when they read as nothing, say a lone separator)."""
    visible = [g for g in hit.glyphs if pt.visible[g]]
    if len(visible) == len(hit.glyphs):
        return Split(visible, hit.needle, False)
    if not visible:
        return Split([], "", True)
    if hit.approx:
        return None
    return Split(visible, needle_of(pt.raw(visible)), True)


# ── Boxes ──────────────────────────────────────────────────────────────
def _finite(rect: fitz.Rect) -> bool:
    return (all(math.isfinite(v) and abs(v) < _MAX_COORD
                for v in (rect.x0, rect.y0, rect.x1, rect.y1))
            and rect.x1 > rect.x0 and rect.y1 > rect.y0)


def min_size(rect: fitz.Rect) -> fitz.Rect:
    """*rect* grown about its centre to at least MIN_SIDE per side."""
    r = fitz.Rect(rect)
    if r.width < MIN_SIDE:
        cx = (r.x0 + r.x1) / 2
        r.x0, r.x1 = cx - MIN_SIDE / 2, cx + MIN_SIDE / 2
    if r.height < MIN_SIDE:
        cy = (r.y0 + r.y1) / 2
        r.y0, r.y1 = cy - MIN_SIDE / 2, cy + MIN_SIDE / 2
    return r


def display_matrix(page: fitz.Page) -> fitz.Matrix:
    """Glyph frame (unrotated, origin at the visible area's top-left) to
    displayed coordinates, from the page's displayed size and rotation
    only."""
    r, rotation = page.rect, page.rotation % 360
    if rotation == 90:
        return fitz.Matrix(0, 1, -1, 0, r.width, 0)
    if rotation == 180:
        return fitz.Matrix(-1, 0, 0, -1, r.width, r.height)
    if rotation == 270:
        return fitz.Matrix(0, -1, 1, 0, 0, r.height)
    return fitz.Matrix(1, 0, 0, 1, 0, 0)


def to_glyph_frame(page: fitz.Page, box: fitz.Rect) -> fitz.Rect:
    """A displayed rectangle in the glyph frame (where get_text clips)."""
    rect = fitz.Rect(box) * ~display_matrix(page)
    rect.normalize()
    return rect


def text_box(page: fitz.Page, pt: PageText, glyphs: Sequence[int]) -> fitz.Rect | None:
    """The displayed box around *glyphs*: their union plus PAD, at least
    MIN_SIDE a side."""
    union = fitz.Rect(pt.boxes[glyphs[0]])
    for g in glyphs[1:]:
        union |= fitz.Rect(pt.boxes[g])
    if not _finite(union):
        return None
    padded = fitz.Rect(union.x0 - PAD, union.y0 - PAD, union.x1 + PAD, union.y1 + PAD)
    shown = padded * display_matrix(page)
    shown.normalize()
    return min_size(shown)


def verify_text_box(page: fitz.Page, pt: PageText, box: fitz.Rect, needle: str) -> bool:
    """The page's glyphs under *box* (displayed coordinates; glyph boxes
    are in the glyph frame, hence to_glyph_frame), read in the scan's own
    geometric orders — not the content stream's, which may draw a value
    out of order — contain *needle*. Independent of how the box was
    found: a box shifted off its glyphs fails."""
    if not needle:
        return False
    clip = to_glyph_frame(page, box)
    inside: list[Glyph] = []
    for c, (x0, y0, x1, y1) in zip(pt.chars, pt.boxes):
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if clip.x0 <= cx <= clip.x1 and clip.y0 <= cy <= clip.y1:
            inside.append((cx, cy, x1 - x0, y1 - y0, c))
    if not inside:
        return False
    across = _reconstruct(inside, cluster_axis=1)
    down = _reconstruct(inside, cluster_axis=0)
    up = "\n".join(line[::-1] for line in down.split("\n"))
    for text in (across, down, up):
        for line in text.split("\n"):
            if needle in normalize_string(line) or needle in _squeeze(line):
                return True
    return False


def visible_area(page: fitz.Page) -> fitz.Rect:
    """The visible area in PDF space: the CropBox (Page.cropbox is
    measured from the MediaBox's top) clipped to the MediaBox."""
    media, crop = page.mediabox, page.cropbox
    return fitz.Rect(crop.x0, media.y1 - crop.y1, crop.x1, media.y1 - crop.y0) & media


def _glyph_ctm(page: fitz.Page) -> fitz.Matrix:
    """PDF user space to the glyph frame (origin at the visible area's
    top-left), from the page's own boxes: neither Page.transformation_
    matrix nor rotation_matrix is right where a rotated page's CropBox
    reaches past its MediaBox."""
    area = visible_area(page)
    return fitz.Matrix(1, 0, 0, -1, -area.x0, area.y1)


def raw_rect(doc: fitz.Document, xref: int) -> fitz.Rect | None:
    kind, value = doc.xref_get_key(xref, "Rect")
    if kind != "array":
        return None
    try:
        nums = [float(v) for v in value.strip("[]").split()]
    except ValueError:
        return None
    if len(nums) != 4:
        return None
    return fitz.Rect(min(nums[0], nums[2]), min(nums[1], nums[3]),
                     max(nums[0], nums[2]), max(nums[1], nums[3]))


def annot_box(page: fitz.Page, rect: Any, *, displayed: bool = False) -> fitz.Rect | None:
    """The displayed box for an annotation rectangle (unrotated unless
    *displayed*), or None when it is missing, infinite, degenerate or
    entirely off the page."""
    if rect is None or not _finite(fitz.Rect(rect)):
        return None
    box = fitz.Rect(rect) if displayed else fitz.Rect(rect) * page.rotation_matrix
    if not box.intersects(page.rect):
        return None
    return box


def verify_annot_box(page: fitz.Page, doc: fitz.Document, xref: int, core: fitz.Rect) -> bool:
    """*core* (the box before MIN_SIDE, from MuPDF's own annotation or
    link rectangle) agrees with the annotation's raw /Rect taken through
    the page's boxes into the glyph frame and displayed: at least 90%
    overlap both ways."""
    raw = raw_rect(doc, xref)
    if raw is None:
        return False
    expect = raw * _glyph_ctm(page) * display_matrix(page)
    expect.normalize()
    common = fitz.Rect(core) & expect
    if common.is_empty:
        return False
    area = common.width * common.height
    return (area >= 0.9 * core.width * core.height and area >= 0.9 * expect.width * expect.height)


@dataclass
class AnnotFacts:
    """What the classification rules read about one annotation, widget
    or link. Gathered once, so the rules themselves are pure."""

    is_widget: bool = False
    is_link: bool = False
    subtype: str = ""
    flags: int = 0           # the annotation's /F
    oc_kind: str = ""        # "", "OCG" or "OCMD" (or "?" when unreadable)
    oc_on: bool | None = None
    oc_may_print: bool = False
    popup_open: bool = False
    own_open: bool = False
    field_type: int | None = None
    field_flags: int = 0
    opt_plain: bool | None = None
    rect: Any = None
    displayed: bool = False


def _key(doc: fitz.Document, xref: int, key: str) -> tuple[str, str]:
    try:
        kind, value = doc.xref_get_key(xref, key)
        return str(kind), str(value)
    except Exception:
        return "null", "null"


def _ref(value: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s+\d+\s+R\s*", value)
    return int(m.group(1)) if m else None


def layer_rules(doc: fitz.Document) -> int:
    """How many objects hold a visibility expression (an OCMD, or any
    dictionary with /VE): content behind one is shown or hidden by a rule
    the map does not evaluate."""
    count = 0
    rule = re.compile(r"/Type\s*/OCMD\b|/VE\b")
    for xref in range(1, doc.xref_length()):
        try:
            source = doc.xref_object(xref, compressed=True)
        except Exception:
            continue
        if rule.search(source):
            count += 1
    return count


def outside_crop(page: fitz.Page) -> bool:
    """The page's MediaBox reaches past its CropBox on any side: content
    can sit where no viewer shows it until the page is uncropped. The
    CropBox is taken back to PDF space (Page.cropbox is measured from the
    MediaBox's top) and clipped to the MediaBox, so a CropBox that is
    shifted rather than smaller counts too."""
    media, shown = page.mediabox, visible_area(page)
    if shown.is_empty:
        return True
    return any(abs(a - b) > 0.5 for a, b in zip(
        (shown.x0, shown.y0, shown.x1, shown.y1), (media.x0, media.y0, media.x1, media.y1)))


def switched_off(doc: fitz.Document) -> set[int]:
    """The optional-content groups that are off: MuPDF's own evaluation
    (Document.get_ocgs), and — because get_ocgs reports a group with a
    /Usage dictionary as on even when the default configuration turns it
    off and MuPDF hides its content — every group the default
    configuration's /OFF array or /BaseState /OFF turns off as well."""
    ocgs = doc.get_ocgs()
    off = {x for x, ocg in ocgs.items() if not ocg.get("on")}
    cat = doc.pdf_catalog()

    def refs(key: str) -> set[int]:
        kind, value = _key(doc, cat, key)
        return {int(n) for n in re.findall(r"(\d+)\s+\d+\s+R", value)} if kind == "array" else set()

    off |= refs("OCProperties/D/OFF") & set(ocgs)
    if _key(doc, cat, "OCProperties/D/BaseState")[1] == "/OFF":
        off |= set(ocgs) - refs("OCProperties/D/ON")
    return off


def oc_facts(doc: fitz.Document, xref: int, facts: AnnotFacts, off: set[int]) -> None:
    """Row 1 and row 5's inputs: is the item gated by a group (and is it
    off, and does it print) or by a visibility expression?"""
    kind, value = _key(doc, xref, "OC")
    if kind == "null":
        return
    oc = _ref(value) if kind == "xref" else None
    if oc is None:
        facts.oc_kind = "?"
        return
    otype = _key(doc, oc, "Type")[1]
    if otype == "/OCMD":
        facts.oc_kind = "OCMD"
        return
    try:
        known = oc in doc.get_ocgs()
    except Exception:
        known = False
    if otype != "/OCG" or not known:
        facts.oc_kind = "?"
        return
    facts.oc_kind = "OCG"
    facts.oc_on = oc not in off
    facts.oc_may_print = _key(doc, oc, "Usage/Print/PrintState")[1] == "/ON"


class PageItems:
    """One page's widgets and annotations, indexed by xref once (the page
    object must outlive them: PyMuPDF ties an annotation to it)."""

    def __init__(self, page: fitz.Page) -> None:
        self.page = page
        self.widgets = {w.xref: w for w in page.widgets()}
        self.annots = {a.xref: a for a in page.annots()}


def annotation_facts(doc: fitz.Document, items: PageItems, xref: int,
                     off: set[int]) -> AnnotFacts | None:
    """Facts for the annotation or widget *xref* on the page, or None if
    the page does not list it."""
    facts = AnnotFacts()
    widget = items.widgets.get(xref)
    if widget is not None:
        facts.is_widget = True
        facts.subtype = "Widget"
        facts.rect = widget.rect
        facts.field_type = widget.field_type
        facts.field_flags = int(widget.field_flags or 0)
        if widget.field_type == LISTBOX:
            kind, value = _key(doc, xref, "Opt")
            facts.opt_plain = kind == "array" and "[" not in value.strip()[1:]
    else:
        annot = items.annots.get(xref)
        if annot is None:
            return None
        facts.subtype = str(annot.type[1])
        facts.rect = annot.rect
    try:
        facts.flags = int(_key(doc, xref, "F")[1])
    except ValueError:
        facts.flags = 0
    oc_facts(doc, xref, facts, off)
    kind, value = _key(doc, xref, "Popup")
    popup = _ref(value) if kind == "xref" else None
    if popup is not None:
        facts.popup_open = _key(doc, popup, "Open")[1] == "true"
    facts.own_open = facts.subtype == "Text" and _key(doc, xref, "Open")[1] == "true"
    return facts


def link_facts(doc: fitz.Document, page: fitz.Page, xref: int | None, index: int | None,
               off: set[int]) -> AnnotFacts | None:
    try:
        links = page.get_links()
    except Exception:
        return None
    link = None
    if index is not None and 0 <= index < len(links):
        link = links[index]
    if link is None or (xref is not None and link.get("xref") != xref):
        link = next((ln for ln in links if xref is not None and ln.get("xref") == xref), None)
    if link is None:
        return None
    facts = AnnotFacts(is_link=True, subtype="Link", rect=link.get("from"), displayed=True)
    lx = link.get("xref")
    if isinstance(lx, int) and lx > 0:
        try:
            facts.flags = int(_key(doc, lx, "F")[1])
        except ValueError:
            facts.flags = 0
        oc_facts(doc, lx, facts, off)
    return facts
