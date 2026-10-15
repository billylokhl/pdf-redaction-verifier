"""redaction_verifier.visible_map.render — drawing the map, and checking it.

The map is a new document: nothing is copied from the original. Its only
font is Helvetica (base 14, never embedded), which PyMuPDF writes in
Latin-1: typographic dashes, quotes and the ellipsis become their ASCII
forms, and any other character outside Latin-1 is written as \\u{XXXX}.
Text this module writes itself (headings, notes, page numbers, counts,
codes) is drawn in its own colours (INK, ALERT, the box colours); every
string that came from the plan's variable inputs is drawn in VAR, a
colour no fixed text uses, so the self-check can tell them apart in the
saved file.

The self-check has two steps:

1. Checker.clean checks each variable string whole, before and after
   escaping, against every rule; a string that hits is replaced by a
   neutral form, which is checked in turn.
2. check_saved reopens the saved bytes and scans all page text (per
   line, knowing each character's colour), the document information,
   XMP and every decompressed stream (knowing which string tokens are
   fixed text). A hit is tolerated only where every character of it is
   tool-generated; any other hit aborts the write.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Sequence

import pymupdf as fitz

from redaction_verifier.matching import PatternRule, SecretMatcher, _fold_for_patterns
from redaction_verifier.matching import match_patterns, normalize_string
from redaction_verifier.model import Secret
from redaction_verifier.visible_map import markers
from redaction_verifier.visible_map.latin import latin as latin
from redaction_verifier.visible_map.plan import (
    DASHED,
    DOTTED,
    HATCHED,
    SECTIONS,
    SOLID,
    Box,
    MapPlan,
    page_ranges,
)

INK = (0.0, 0.0, 0.0)
VAR = (0.1, 0.2, 0.55)
VAR_HEX = 0x1A338C          # VAR as MuPDF reads it back
ALERT = (0.75, 0.05, 0.05)
WHITE = (1.0, 1.0, 1.0)
STYLE_COLORS = {SOLID: (0.85, 0.1, 0.1), HATCHED: (0.85, 0.45, 0.0),
                DASHED: (0.1, 0.35, 0.8), DOTTED: (0.45, 0.15, 0.6)}
STYLE_DASHES = {SOLID: None, HATCHED: None, DASHED: "[4 2] 0", DOTTED: "[1 2] 0"}
LEGEND = (
    (SOLID, "on the page: selectable text, or an annotation the page draws"),
    (HATCHED, "on the page in viewers that redraw form fields"),
    (DASHED, "on click, hover or scroll, in the comments panel, or when opened"),
    (DOTTED, "when printed"),
)
META = {"title": "Visible map (masked)", "author": "", "subject": "", "keywords": "",
        "creator": "pdf-redaction-verifier", "producer": "pdf-redaction-verifier"}
FONT = "helv"
SIZE = 9.0
HEADING = 10.5
LEAD = 11.5
MARGIN = 36.0
SUMMARY_SIZE = (612.0, 792.0)
MIN_MAP_SIDE = 144.0
BAND = 72.0
VAR_CAP = 80          # characters of one variable string drawn


class SelfCheckFailed(Exception):
    """The saved map holds a hit outside the tool's own text."""


def cap(text: str) -> str:
    """*text* as Writer.line draws a variable string: cut to VAR_CAP."""
    return text if len(text) <= VAR_CAP else text[:VAR_CAP - 1] + "…"


class Checker:
    """Step 1 of the self-check, and the rules step 2 matches with."""

    def __init__(self) -> None:
        self.use([], [])

    def use(self, secrets: Iterable[Secret], patterns: Iterable[PatternRule]) -> None:
        self.secrets = list(secrets)
        self.matcher = SecretMatcher(self.secrets)
        self.patterns = list(patterns)

    def hits(self, text: str) -> bool:
        for form in (text, latin(text)):
            if self.matcher.search(normalize_string(form)):
                return True
            if match_patterns(form, self.patterns):
                return True
            if match_patterns(form, self.patterns, collapse_separators=True):
                return True
        return False

    def clean(self, text: str, *neutral: str) -> str:
        """The first of *text* and its *neutral* forms that no rule
        matches, whole or as drawn (cut to VAR_CAP), before or after
        escaping ("?" as a last resort, which step 2 still checks)."""
        for candidate in (text, *neutral):
            if not self.hits(candidate) and not self.hits(cap(candidate)):
                return candidate
        return "?"


# ── Text layout ────────────────────────────────────────────────────────
Segment = tuple[str, bool]          # (text, variable)


class Writer:
    """Writes the map's pages, recording every fixed string it draws."""

    def __init__(self) -> None:
        self.doc = fitz.open()
        self.fixed: set[str] = set()
        self.variables = 0          # variable strings drawn
        self.page: fitz.Page | None = None
        self.shape: Any = None
        self.y = 0.0
        self.width = 0.0
        self.height = 0.0

    def new_page(self, width: float, height: float) -> fitz.Page:
        self.commit()
        self.page = self.doc.new_page(width=width, height=height)
        self.shape = self.page.new_shape()
        self.width, self.height = width, height
        self.y = MARGIN
        return self.page

    def commit(self) -> None:
        if self.shape is not None:
            self.shape.commit()
            self.shape = None

    def text(self, x: float, y: float, text: str, *, variable: bool, size: float = SIZE,
             color: tuple[float, float, float] = INK) -> float:
        """Draw one segment with its baseline at *y*; return its width."""
        shown = latin(text)
        if not shown:
            return 0.0
        if variable:
            self.variables += 1
        else:
            self.fixed.add(shown)
        self.shape.insert_text((x, y), shown, fontname=FONT, fontsize=size,
                               color=VAR if variable else color)
        return float(fitz.get_text_length(shown, fontname=FONT, fontsize=size))

    def width_of(self, text: str, size: float = SIZE) -> float:
        return float(fitz.get_text_length(latin(text), fontname=FONT, fontsize=size))

    def line(self, segments: Sequence[Segment], *, size: float = SIZE,
             color: tuple[float, float, float] = INK, indent: float = 0.0,
             on_new_page: Callable[[], None] | None = None) -> None:
        """Flow *segments* from the left margin, wrapping between words
        of fixed text and before (never inside) a variable string."""
        left, right = MARGIN + indent, self.width - MARGIN
        words: list[Segment] = []
        for text, variable in segments:
            if variable:
                words.append((cap(text), True))
            else:
                words.extend((w, False) for w in re.split(r"(?<= )", text) if w)
        x = left
        lead = size * LEAD / SIZE
        self._room(on_new_page)
        for text, variable in words:
            w = self.width_of(text, size)
            if x > left and x + w > right:
                self.y += lead
                self._room(on_new_page)
                x = left + 12
                text = text.lstrip() if not variable else text
                w = self.width_of(text, size)
            x += self.text(x, self.y, text, variable=variable, size=size, color=color)
        self.y += lead

    def _room(self, on_new_page: Callable[[], None] | None) -> None:
        # A line fits when its baseline is above the bottom margin.
        if self.y > self.height - MARGIN:
            self.new_page(self.width, self.height)
            if on_new_page is not None:
                on_new_page()


# ── The summary ────────────────────────────────────────────────────────
def _short(sha: str) -> str:
    return f"{sha[:4]}…{sha[-3:]}" if len(sha) > 8 else sha


def summary(w: Writer, plan: MapPlan) -> None:
    w.new_page(*SUMMARY_SIZE)
    name = plan.target_name         # already cut to plan.NAME_CAP, then checked
    w.line([("VISIBLE MAP (masked)", False)], size=14)
    w.line([("Target ", False), (name, True),
            (f"   sha256 {_short(plan.target_sha256)}   v{plan.version}   exit "
             f"{plan.exit_code}", False)])
    w.y += 4
    w.line([(plan.headline, False)], size=12,
           color=ALERT if plan.row <= 4 or plan.row == 8 else INK)
    if plan.flags.I:
        _banner(w, plan)
    pages = plan.mapped_pages
    omitted = plan.page_count - len(pages)
    styles: dict[str, int] = {}
    for b in plan.boxes:
        styles[b.style] = styles.get(b.style, 0) + 1
    w.y += 4
    if plan.boxes:
        w.line([(f"Boxes: {len(plan.boxes)} on {page_ranges(pages)}"
                 f" ({omitted} page{'s' if omitted != 1 else ''} without boxes omitted)",
                 False)])
    else:
        w.line([(f"Boxes: none ({plan.page_count} page{'s' if plan.page_count != 1 else ''} "
                 "without boxes omitted)", False)])
    for style, what in LEGEND:
        if style in styles:
            boxes = [b for b in plan.boxes if b.style == style]
            w.line([(f"{_style_name(style)} boxes ({what}): {len(boxes)}, "
                     f"{page_ranges([b.page for b in boxes])}", False)], indent=12)
    for section, (heading, _flag) in SECTIONS.items():
        items = [i for i in plan.listed if i.section == section]
        if section == "not_visible":
            if plan.not_visible:
                parts = ", ".join(f"{k} {v}" for k, v in sorted(plan.not_visible.items()))
                total = sum(plan.not_visible.values())
                w.y += 3
                w.line([(f"{heading}: {total} ({parts})", False)], size=HEADING)
            continue
        if not items:
            continue
        w.y += 3
        w.line([(f"{heading}:", False)], size=HEADING)
        for item in items:
            where = f"p.{item.page} " if item.page is not None else ""
            note = f" -- {item.note}" if item.note else ""
            segments: list[Segment] = [(where, False), (item.label, True), (note, False)]
            if item.detail:
                segments += [(", " if item.note else " -- ", False), (item.detail, True)]
            w.line(segments, indent=12)
    if plan.codes:
        w.y += 3
        w.line([("Warning codes:", False)], size=HEADING)
        by_code: dict[str, set[int]] = {}
        for code, page in plan.codes:
            pages_of = by_code.setdefault(code, set())
            if page is not None:
                pages_of.add(page)
        w.line([("; ".join(f"{c} {page_ranges(sorted(p))}" if p else c
                           for c, p in sorted(by_code.items())), False)], indent=12)
    if plan.legend:
        w.y += 3
        w.line([("Legacy rule names are not shown; rules are numbered:", False)], size=HEADING)
        for entry in plan.legend:
            w.line([(entry, False)], indent=12)
    _legend(w)


def _style_name(style: str) -> str:
    return {SOLID: "Solid", HATCHED: "Hatched", DASHED: "Dashed", DOTTED: "Dotted"}[style]


def _banner(w: Writer, plan: MapPlan) -> None:
    w.y += 4
    top = w.y - 10
    reasons = "; ".join(plan.reasons) if plan.reasons else "items could not be placed"
    lines = _wrap_fixed(w, f"INCOMPLETE: {reasons}", w.width - 2 * MARGIN - 12, SIZE)
    height = len(lines) * LEAD + 6
    w.shape.draw_rect(fitz.Rect(MARGIN - 4, top, w.width - MARGIN + 4, top + height))
    w.shape.finish(color=ALERT, fill=ALERT, width=0)
    for text in lines:
        w.text(MARGIN + 2, w.y, text, variable=False, color=WHITE)
        w.y += LEAD
    w.y += 6


def _wrap_fixed(w: Writer, text: str, width: float, size: float) -> list[str]:
    lines, current = [], ""
    for word in text.split(" "):
        trial = f"{current} {word}" if current else word
        if current and w.width_of(trial, size) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    return lines + ([current] if current else [])


def _legend(w: Writer) -> None:
    w.y += 6
    w.line([("Legend", False)], size=HEADING)
    for style, what in LEGEND:
        w._room(None)
        sample = fitz.Rect(MARGIN + 12, w.y - 8, MARGIN + 40, w.y + 1)
        _draw_box(w.shape, sample, style)
        w.text(MARGIN + 48, w.y, f"{_style_name(style)}: {what}", variable=False)
        w.y += LEAD + 2
    w.line([("[n] numbers a box; its page lists what each number is. Boxes are drawn on a "
             "blank page of the original's displayed size. No character of an exact value is "
             "shown: a pattern match shows at most its last characters, and none that are part "
             "of an exact value.", False)])


# ── The page maps ──────────────────────────────────────────────────────
def _draw_box(shape: Any, rect: fitz.Rect, style: str) -> None:
    color = STYLE_COLORS[style]
    shape.draw_rect(rect)
    shape.finish(color=color, width=1.2, dashes=STYLE_DASHES[style])
    if style == HATCHED:
        step = 4.0
        c = rect.x0 - rect.height
        while c < rect.x1:
            # The line x - y = c - rect.y0 clipped to the rectangle.
            x0, y0 = max(rect.x0, c), rect.y0 + max(0.0, rect.x0 - c)
            x1 = min(rect.x1, c + rect.height)
            y1 = rect.y0 + (x1 - c)
            if x1 > x0:
                shape.draw_line((x0, y0), (x1, y1))
            c += step
        shape.finish(color=color, width=0.5)


def map_pages(w: Writer, plan: MapPlan) -> None:
    for page_no in plan.mapped_pages:
        boxes = [b for b in plan.boxes if b.page == page_no]
        width, height = plan.page_sizes[page_no]
        small = width < MIN_MAP_SIDE or height < MIN_MAP_SIDE
        page_w = max(width, 3 * MIN_MAP_SIDE / 2) if small else width
        page_h = height + BAND if small else height
        w.new_page(page_w, page_h)
        w.shape.draw_rect(fitz.Rect(0, 0, width, height))
        w.shape.finish(color=(0.75, 0.75, 0.75), width=0.5)
        w.text(4, 10, f"p.{page_no}", variable=False, size=7, color=(0.4, 0.4, 0.4))
        tags: list[fitz.Rect] = []
        for n, box in enumerate(boxes, start=1):
            _draw_box(w.shape, box.rect, box.style)
            tag = f"[{n}]"
            width = w.width_of(tag, 7)
            tx = min(max(box.rect.x0, 2.0), page_w - width - 2)
            ty = box.rect.y0 - 2 if box.rect.y0 > 10 else box.rect.y1 + 8
            # Boxes on the same text (one per rule) would stack their numbers.
            while any(fitz.Rect(tx, ty - 7, tx + width, ty).intersects(t) for t in tags):
                tx += width + 2
            tags.append(fitz.Rect(tx, ty - 7, tx + width, ty))
            w.text(tx, ty, tag, variable=False, size=7, color=STYLE_COLORS[box.style])
        _number_list(w, page_no, boxes, page_w, page_h)


def _entry(n: int, box: Box) -> list[Segment]:
    extra = ", approx." if box.approx else ""
    return [(f"[{n}] ", False), (box.label, True), (f", {box.how}{extra}", False)]


def _number_list(w: Writer, page_no: int, boxes: list[Box], page_w: float,
                 page_h: float) -> None:
    """The numbered list: at the page foot when it fits below every box,
    else on overflow pages of its own."""
    rows = len(boxes)
    first = page_h - MARGIN - (rows - 1) * LEAD     # the first entry's baseline
    lowest = max(b.rect.y1 for b in boxes)
    fits = all(w.width_of("".join(t for t, _ in _entry(n, b))) < page_w - 2 * MARGIN
               for n, b in enumerate(boxes, start=1))
    if fits and lowest + 8 < first - LEAD:
        w.y = first
    else:
        w.new_page(*SUMMARY_SIZE)
        w.line([(f"p.{page_no}: what each box is", False)], size=HEADING)

    def again() -> None:
        w.line([(f"p.{page_no}: what each box is (continued)", False)], size=HEADING)

    for n, box in enumerate(boxes, start=1):
        w.line(_entry(n, box), on_new_page=again)


def render(plan: MapPlan) -> tuple[bytes, Writer]:
    """The map's bytes, markers included, and the writer that drew them
    (whose record of fixed text check_saved needs)."""
    w = Writer()
    summary(w, plan)
    map_pages(w, plan)
    w.commit()
    w.doc.set_metadata(dict(META))
    data = w.doc.tobytes(garbage=4, deflate=True)
    return markers.insert_markers(data), w


# ── Step 2: the saved bytes ────────────────────────────────────────────
def _tainted_hit(checker: Checker, chars: str, tainted: Sequence[bool]) -> bool:
    """A rule matches *chars* somewhere that touches a tainted character
    (or anywhere, when positions cannot be mapped and any is tainted)."""
    if not any(tainted):
        return False
    norm: list[str] = []
    owners: list[int] = []
    for k, ch in enumerate(chars):
        piece = normalize_string(ch)
        norm.append(piece)
        owners.extend([k] * len(piece))
    text = "".join(norm)
    exact = text == normalize_string(chars)
    for secret in checker.secrets:
        start = text.find(secret.normalized)
        while start >= 0:
            if not exact or any(tainted[owners[i]] for i in range(start, start + len(secret.normalized))):
                return True
            start = text.find(secret.normalized, start + 1)
    folded_parts = [_fold_for_patterns(ch) for ch in chars]
    folded = "".join(folded_parts)
    exact = folded == _fold_for_patterns(chars)
    fowners = [k for k, part in enumerate(folded_parts) for _ in part]
    for rule in checker.patterns:
        pos = 0
        while pos <= len(folded):
            m = rule.regex.search(folded, pos)
            if m is None:
                break
            if not m.group(0):
                pos = m.end() + 1
                continue
            if rule.validator is None or rule.validator(m.group(0)):
                if not exact or any(tainted[fowners[i]] for i in range(m.start(), m.end())):
                    return True
                pos = m.end()
            else:
                pos = m.start() + 1
    return False


_STRING_TOKEN = re.compile(rb"<(?!<)[0-9A-Fa-f\s]*>|\((?:\\.|[^\\()])*\)", re.S)


_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _decode_token(token: bytes) -> str:
    """A PDF string token's bytes, as Latin-1: hex strings, and literal
    strings with their escapes (\\n, \\ddd octal, an escaped line end
    as a continuation, any other escaped character as itself)."""
    if token.startswith(b"<"):
        hexdigits = re.sub(rb"\s", b"", token[1:-1])
        if len(hexdigits) % 2:
            hexdigits += b"0"
        return bytes.fromhex(hexdigits.decode()).decode("latin-1")

    def escape(m: re.Match[bytes]) -> bytes:
        esc = m.group(1)
        if esc[:1] in b"01234567":
            return bytes([int(esc, 8) & 0xFF])
        if esc in (b"\r\n", b"\r", b"\n"):
            return b""
        return _ESCAPES.get(esc, esc)

    body = re.sub(rb"\\([0-7]{1,3}|\r\n|.)", escape, token[1:-1], flags=re.S)
    return body.decode("latin-1")


def check_saved(data: bytes, checker: Checker, writer: Writer) -> None:
    """Step 2. Raise SelfCheckFailed on any hit not wholly inside the
    tool's own text."""
    fixed = writer.fixed
    seen_variable = False
    doc = fitz.open("pdf", data)
    if doc.is_repaired:
        raise SelfCheckFailed("the map does not reopen cleanly")
    head, tail = data[:1024], data[-len(markers.MARKER_LINE):]
    if markers.markers_in(head, tail) != (True, True):
        raise SelfCheckFailed("markers missing")
    for index in range(doc.page_count):
        page = doc[index]
        raw = page.get_text("rawdict", clip=fitz.INFINITE_RECT(),
                            flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_MEDIABOX_CLIP)
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                chars, tainted = [], []
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        chars.append(char.get("c", ""))
                        tainted.append(span.get("color") == VAR_HEX)
                seen_variable = seen_variable or any(tainted)
                if _tainted_hit(checker, "".join(chars), tainted):
                    raise SelfCheckFailed(f"page {page.number + 1} text")
    if writer.variables and not seen_variable:
        # Provenance is read back from the colour: if no variable text
        # reads back as VAR, every hit would pass as fixed text.
        raise SelfCheckFailed("variable text not found in the saved map")
    for key, value in (doc.metadata or {}).items():
        if value and value not in META.values() and checker.hits(str(value)) and key not in (
                "format", "encryption"):
            raise SelfCheckFailed("document information")
    xmp = doc.get_xml_metadata()
    if xmp and checker.hits(xmp):
        raise SelfCheckFailed("XMP")
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        body = doc.xref_stream(xref) or b""
        chars, tainted = [], []
        pos = 0
        for m in _STRING_TOKEN.finditer(body):
            other = body[pos:m.start()].decode("latin-1")
            chars.append(other)
            tainted.extend([False] * len(other))
            text = _decode_token(m.group(0))
            chars.append(text)
            tainted.extend([text not in fixed] * len(text))
            pos = m.end()
        rest = body[pos:].decode("latin-1")
        chars.append(rest)
        tainted.extend([False] * len(rest))
        if _tainted_hit(checker, "".join(chars), tainted):
            raise SelfCheckFailed(f"stream {xref}")


# ── The whole step ─────────────────────────────────────────────────────
def build_map(data: bytes | memoryview, target_sha256: str,
              header: dict[str, Any]) -> tuple[bytes, str, MapPlan]:
    """The checked map for the document *data* from *header* (see
    plan.make_plan), its headline (fixed text and warning codes only)
    and its plan. Every label goes through a Checker of the header's
    rules (step 1) and the saved bytes are checked (step 2). Raises on
    any failure, SelfCheckFailed included."""
    from redaction_verifier.visible_map import plan as plan_module

    checker = Checker()
    rules = plan_module.Rules(header["rules"], checker.clean)
    checker.use(rules.secrets.values(), rules.patterns.values())
    the_plan = plan_module.make_plan(data, target_sha256, header, rules)
    pdf, writer = render(the_plan)
    check_saved(pdf, checker, writer)
    return pdf, the_plan.headline, the_plan
