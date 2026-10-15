"""Where planted text landed, and which bytes encode it.

Two jobs, both done without MuPDF (the redactor's and the verifier's
library):

- ``page_lines`` reads each page's text lines with pdfminer.six, with a
  box per character, so ``find`` can give a planted value's page and
  bounding box (top-left-origin points, truth.py's frame).
- ``fingerprints`` gives the bytes a page's own fonts use to draw a
  string: through each font's ToUnicode CMap (Chrome's Identity-H CID
  fonts) or, for a simple font with a standard encoding, the string's
  own ASCII bytes. The oracle searches decompressed streams for these,
  since text drawn in glyph codes never appears in a file as ASCII.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pikepdf


@dataclass(frozen=True)
class Line:
    text: str
    boxes: tuple[tuple[float, float, float, float], ...]   # one per character of text


def page_lines(pdf: Path) -> list[list[Line]]:
    """Every page's text lines (pdfminer), each character with its box."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine

    pages: list[list[Line]] = []
    for layout in extract_pages(str(pdf), laparams=LAParams()):
        height = layout.height
        lines: list[Line] = []

        def walk(obj: object) -> None:
            if isinstance(obj, LTTextLine):
                text: list[str] = []
                boxes: list[tuple[float, float, float, float]] = []
                for ch in obj:
                    t = ch.get_text()
                    if isinstance(ch, LTChar):
                        box = (ch.x0, height - ch.y1, ch.x1, height - ch.y0)
                    elif text:              # an inserted space: borrow the previous box
                        box = boxes[-1]
                    else:
                        continue
                    for c in t:             # a ligature's text is several characters
                        text.append(c)
                        boxes.append(box)
                joined = "".join(text).rstrip("\n")
                lines.append(Line(joined, tuple(boxes[:len(joined)])))
            elif isinstance(obj, LTTextContainer) or hasattr(obj, "__iter__"):
                for child in obj:
                    walk(child)

        walk(layout)
        pages.append(lines)
    return pages


def find(pages: list[list[Line]], needle: str) -> list[tuple[int, list[float]]]:
    """(page, bbox) for every occurrence of *needle* inside one line."""
    hits = []
    for page_no, lines in enumerate(pages):
        for line in lines:
            for m in re.finditer(re.escape(needle), line.text):
                boxes = line.boxes[m.start():m.end()]
                hits.append((page_no, [round(min(b[0] for b in boxes), 2),
                                       round(min(b[1] for b in boxes), 2),
                                       round(max(b[2] for b in boxes), 2),
                                       round(max(b[3] for b in boxes), 2)]))
    return hits


def image_boxes(pdf: Path) -> list[list[list[float]]]:
    """Every page's placed images (pdfminer), top-left-origin boxes, in
    reading order (top to bottom)."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTImage

    pages: list[list[list[float]]] = []
    for layout in extract_pages(str(pdf)):
        height = layout.height
        boxes: list[list[float]] = []

        def walk(obj: object) -> None:
            if isinstance(obj, LTImage):
                boxes.append([obj.x0, height - obj.y1, obj.x1, height - obj.y0])
            elif hasattr(obj, "__iter__"):
                for child in obj:
                    walk(child)

        walk(layout)
        pages.append(sorted(boxes, key=lambda b: (b[1], b[0])))
    return pages


# ── Fingerprints ───────────────────────────────────────────────────────

_TOKEN = re.compile(rb"<([0-9A-Fa-f\s]*)>|(\[)|(\])|([A-Za-z]+)")


def parse_tounicode(data: bytes) -> dict[str, bytes]:
    """A ToUnicode CMap -> {unicode text: code bytes} (first code wins)."""
    out: dict[str, bytes] = {}
    tokens: list[bytes | str] = []
    for m in _TOKEN.finditer(data):
        if m.group(1) is not None:
            tokens.append(bytes.fromhex(m.group(1).decode().replace(" ", "").replace("\n", "")))
        elif m.group(2):
            tokens.append("[")
        elif m.group(3):
            tokens.append("]")
        else:
            tokens.append(m.group(4).decode())

    def put(code: bytes, dst: bytes) -> None:
        try:
            text = dst.decode("utf-16-be")
        except UnicodeDecodeError:
            return
        out.setdefault(text, code)

    i, mode = 0, None
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("beginbfchar", "beginbfrange"):
            mode, i = tok, i + 1
            continue
        if tok in ("endbfchar", "endbfrange"):
            mode, i = None, i + 1
            continue
        if mode == "beginbfchar" and isinstance(tok, bytes) and i + 1 < len(tokens):
            dst = tokens[i + 1]
            if isinstance(dst, bytes):
                put(tok, dst)
            i += 2
            continue
        if mode == "beginbfrange" and isinstance(tok, bytes) and i + 2 < len(tokens):
            lo, hi, dst = tok, tokens[i + 1], tokens[i + 2]
            if not isinstance(hi, bytes):
                i += 1
                continue
            width = len(lo)
            lo_n, hi_n = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
            if isinstance(dst, bytes):
                base = int.from_bytes(dst, "big")
                for k in range(hi_n - lo_n + 1):
                    put((lo_n + k).to_bytes(width, "big"), (base + k).to_bytes(len(dst), "big"))
                i += 3
                continue
            if dst == "[":
                j, k = i + 3, 0
                while j < len(tokens) and tokens[j] != "]":
                    item = tokens[j]
                    if isinstance(item, bytes):
                        put((lo_n + k).to_bytes(width, "big"), item)
                    j, k = j + 1, k + 1
                i = j + 1
                continue
        i += 1
    return out


_SIMPLE_ENCODINGS = {"/WinAnsiEncoding", "/MacRomanEncoding", "/StandardEncoding"}


def _fonts(resources: pikepdf.Object | None, seen: set[tuple[int, int]]) -> list[pikepdf.Object]:
    found: list[pikepdf.Object] = []
    if not isinstance(resources, pikepdf.Dictionary):
        return found
    fonts = resources.get("/Font")
    if isinstance(fonts, pikepdf.Dictionary):
        found.extend(fonts[k] for k in fonts.keys())
    xobjects = resources.get("/XObject")
    if isinstance(xobjects, pikepdf.Dictionary):
        for k in xobjects.keys():
            xo = xobjects[k]
            og = xo.objgen
            if og in seen or xo.get("/Subtype") != "/Form":
                continue
            seen.add(og)
            found.extend(_fonts(xo.get("/Resources"), seen))
    return found


def font_encoders(pdf: pikepdf.Pdf, page_no: int) -> list[dict[str, bytes] | None]:
    """One encoder per font on the page: a ToUnicode reverse map, or
    None for a simple font with a standard encoding (ASCII as is)."""
    page = pdf.pages[page_no]
    encoders: list[dict[str, bytes] | None] = []
    for font in _fonts(page.obj.get("/Resources"), set()):
        tu = font.get("/ToUnicode")
        if isinstance(tu, pikepdf.Stream):
            encoders.append(parse_tounicode(tu.read_bytes()))
            continue
        enc = font.get("/Encoding")
        if font.get("/Subtype") in ("/Type1", "/TrueType") and (
                enc is None or str(enc) in _SIMPLE_ENCODINGS):
            encoders.append(None)
    return encoders


def fingerprints(pdf: pikepdf.Pdf, page_no: int, text: str) -> list[str]:
    """Hex of every distinct byte sequence a font on the page would use
    to draw *text* (only fonts that can draw all of it)."""
    out: list[str] = []
    for enc in font_encoders(pdf, page_no):
        if enc is None:
            if text.isascii():
                code = text.encode("ascii")
            else:
                continue
        else:
            try:
                code = b"".join(enc[c] for c in text)
            except KeyError:
                # Some producers place words apart instead of drawing a
                # space glyph: fall back to the text with spaces removed.
                try:
                    code = b"".join(enc[c] for c in text if c != " ")
                except KeyError:
                    continue
        if code.hex() not in out:
            out.append(code.hex())
    return out
