"""Page text layouts × rule sets: how the reading of a page's text joins
(or fails to join) a value split by layout — across lines, across page
breaks, in form boxes, one character per line, in table rows, rotated.

Each layout is built with three rule sets and with or without the
built-in SSN pattern rule. The label for every member is in LABELS
(page_text_labels.py): the verdict and what must be reported, reviewed
case by case — a split the tool reads on a single line or at a page
seam is a hard finding; one it only sees by joining lines or reading
columns is manual review (DESIGN.md, two-tier model).
"""

from __future__ import annotations

from pathlib import Path

import fitz

from ..model import KnownGap, case
from ..pdfkit import save
from .page_text_labels import GAP_REQUIRES, GAPS, LABELS

SSN = "123-45-6789"
CARD = "4111 1111 1111 1111"
NAME = "Jonathan Quincy"

RULE_SETS: dict[str, tuple[tuple[str, str], ...]] = {
    "pattern-only": (),                     # the SSN pattern rule alone
    "ssn": (("SSN", SSN),),
    "multi": (("SSN", SSN), ("CARD", CARD), ("NAME", NAME)),
    "overlapping": (("LONG", "123-45-6789-0001"), ("SSN", SSN), ("SHORT", "45-67")),
}

# layout -> (description, pages); each page is None (blank) or a list of items:
#   ("lines", [text, ...], y0=100)  lines 30 pt apart
#   ("at", x, y, text, rotate)      one run at a point
#   ("boxes", x, y, chars, gap)     one character per form box
#   ("vstack", x, y, chars)         one character per line
#   ("table", rows)                 cells 160 pt apart, rows 20 pt apart
LAYOUTS: dict[str, tuple[str, list]] = {
    "single-line": ("the SSN on one line",
                    [[("lines", ["hello", f"SSN {SSN} end"])]]),
    "split-lines": ("the SSN wrapped across two lines",
                    [[("lines", ["total 123-45-", "6789 units"])]]),
    "split-three-lines": ("the SSN over three lines",
                          [[("lines", ["a 123", "45", "6789 b"])]]),
    "split-pages": ("the SSN split at a page break, last line to first line",
                    [[("lines", ["intro", "SSN 123-45-"])], [("lines", ["6789 more", "x"])]]),
    "split-pages-needs-earlier-lines": (
        "a page-break split that needs more than the last line before the break",
        [[("lines", ["ref 123", "45-"])], [("lines", ["6789 end"])]]),
    "split-pages-needs-later-lines": (
        "a page-break split that needs more than the first line after the break",
        [[("lines", ["x", "SSN 123"])], [("lines", ["45", "6789 end"])]]),
    "split-over-blank-page": ("the SSN split around a blank page",
                              [[("lines", ["SSN 123-45-"])], None, [("lines", ["6789 t"])]]),
    "split-three-pages": ("the SSN over three pages, a short middle one",
                          [[("lines", ["intro", "SSN 123-"])], [("lines", ["45-"])],
                           [("lines", ["6789 end"])]]),
    "split-four-pages": ("the SSN's digits over four pages",
                         [[("lines", ["id 123"])], [("lines", ["45"])], [("lines", ["67"])],
                          [("lines", ["89 end"])]]),
    "rotated": ("the SSN rotated 90°", [[("at", 300, 500, f"SSN {SSN}", 90)]]),
    "rotated-split-pages": ("the SSN rotated 90° and split at a page break",
                            [[("at", 300, 500, "SSN 123-45-", 90)], [("at", 300, 500, "6789 x", 90)]]),
    "rotated270-split-pages": (
        "the SSN rotated 270° and split at a page break, among body text",
        [[("lines", ["body text here", "more body"]), ("at", 500, 300, "123-45-", 270)],
         [("at", 50, 300, "6789", 270), ("lines", ["page two body"])]]),
    "form-boxes": ("the SSN's digits in form boxes", [[("boxes", 72, 200, "123456789", 25)]]),
    "form-boxes-split-pages": (
        "form-box digits split at a page break",
        [[("lines", ["x"]), ("boxes", 72, 700, "12345", 25)],
         [("boxes", 72, 72, "6789", 25), ("lines", ["y"], 200)]]),
    "vertical-stack": ("the SSN's digits one per line", [[("vstack", 100, 100, "123456789")]]),
    "vertical-stack-split-pages": ("one digit per line, split at a page break",
                                   [[("vstack", 100, 650, "12345")], [("vstack", 100, 72, "6789")]]),
    "table-rows": ("the SSN split across table rows",
                   [[("table", [["Name", "ID"], ["Bob", "123-45-"], ["6789", "Alice"]])]]),
    "table-rows-split-pages": (
        "the SSN split across table rows on two pages",
        [[("table", [["Name", "ID"], ["Bob", "123-45-"]])], [("table", [["6789", "Alice"], ["x", "y"]])]]),
    "several-values": ("a card number, a name and a split SSN over three pages",
                       [[("lines", [f"card {CARD}", "Jonathan", "Quincy said"])],
                        [("lines", ["ssn 123-45-"])], [("lines", ["6789"])]]),
    "overlapping-values": ("an account number that contains the SSN",
                           [[("lines", ["acct 123-45-6789-0001"])]]),
    "overlapping-split-pages": ("the account number split at a page break",
                                [[("lines", ["x", "acct 123-45-"])], [("lines", ["6789-0001"])]]),
    "overlapping-split-short-tail": (
        "the account number split with only its suffix after the break",
        [[("lines", ["x", "acct 123-45-6789-"])], [("lines", ["0001 y"])]]),
    "blank-pages": ("two blank pages", [None, None]),
    "repeated-line-wraps": ("the SSN wrapped across lines on two pages",
                            [[("lines", ["a 123-45-", "6789"])], [("lines", ["b 123-45-", "6789"])]]),
    "two-page-breaks": ("two SSNs, each split at a different page break",
                        [[("lines", ["SSN 123-45-"])], [("lines", ["6789", "second 123-"])],
                         [("lines", ["45-6789"])]]),
    "name-split-pages": ("a name split at a page break",
                         [[("lines", ["Dear Jonathan"])], [("lines", ["Quincy, hello"])]]),
    "name-split-three-pages": ("a name split over three pages",
                               [[("lines", ["Dear Jona"])], [("lines", ["than"])],
                                [("lines", ["Quincy, hello"])]]),
}


def _build(pages: list):
    def build(path: Path) -> None:
        doc = fitz.open()
        for spec in pages:
            page = doc.new_page()
            for item in spec or []:
                kind = item[0]
                if kind == "lines":
                    y0 = item[2] if len(item) > 2 else 100
                    for i, text in enumerate(item[1]):
                        page.insert_text((72, y0 + 30 * i), text)
                elif kind == "at":
                    _, x, y, text, rotate = item
                    page.insert_text((x, y), text, rotate=rotate)
                elif kind == "boxes":
                    _, x, y, chars, gap = item
                    for j, char in enumerate(chars):
                        page.insert_text((x + gap * j, y), char)
                elif kind == "vstack":
                    _, x, y, chars = item
                    for j, char in enumerate(chars):
                        page.insert_text((x, y + 14 * j), char)
                elif kind == "table":
                    for r, row in enumerate(item[1]):
                        for c, cell in enumerate(row):
                            page.insert_text((72 + 160 * c, 100 + 20 * r), cell)
        save(doc, path)
    return build


CELL = {"single-line": "live.plain", "rotated": "live.plain", "overlapping-values": "live.plain",
        "form-boxes": "live.plain", "several-values": "match.page-break", "blank-pages": None}

# Which rules' values each layout actually contains. The short value
# "45-67" lies inside every SSN, so it is present wherever the SSN is.
_NAME_ONLY = {"name-split-pages", "name-split-three-pages"}


def _present(layout: str, rule: str) -> bool:
    if layout == "blank-pages":
        return False
    if rule == "NAME":
        return layout in _NAME_ONLY or layout == "several-values"
    if rule == "CARD":
        return layout == "several-values"
    if rule == "LONG":
        return layout.startswith("overlapping")
    return layout not in _NAME_ONLY          # SSN, SHORT, the SSN pattern


def _cell(layout: str) -> str:
    if layout in CELL:
        return CELL[layout]
    if "pages" in layout or "page-break" in layout:
        return "match.page-break"
    return "match.line-wrap"


for layout, (description, pages) in LAYOUTS.items():
    for rule_set, values in RULE_SETS.items():
        for pattern in ("", "pattern"):
            if rule_set == "pattern-only" and not pattern:
                continue
            key = (layout, rule_set, pattern)
            label = LABELS[key]
            rules = tuple({"name": n, "value": v} for n, v in values)
            if pattern:
                rules += ({"name": "Any SSN", "class": "ssn"},)
            cell = _cell(layout)
            names = [name for name, _ in values] + (["Any SSN"] if pattern else [])
            leak = cell is not None and any(_present(layout, name) for name in names)
            gap = GAPS.get(key)
            slug = f"{rule_set}{'-pattern' if pattern and rule_set != 'pattern-only' else ''}"
            case(f"layout.{layout}-{slug}",
                 truth="leak" if leak else "clean",
                 cells=(cell,) if leak else (),
                 features=() if leak or cell is None else (cell,),
                 expected=label, rules=rules,
                 known_gap=KnownGap(gap[0], gap[1]) if gap else None,
                 requires=GAP_REQUIRES.get(key, ()),
                 story=f"{description[0].upper()}{description[1:]}; rules: {rule_set}"
                       f"{' plus the SSN pattern' if pattern else ''}.",
                 grid="page-text",
                 params=(("layout", layout), ("rules", rule_set), ("pattern", pattern or "none")),
                 )(_build(pages))
