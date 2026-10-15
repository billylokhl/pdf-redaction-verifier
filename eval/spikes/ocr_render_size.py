"""How much of a large page render does Apple Vision read? (#128)

The measurement behind ``redaction_verifier.views.ocr.MAX_OCR_RENDER_SIDE``.
For each render size (the 300 dpi render's longest side in pixels) and
page shape (width / height: portrait 0.77, landscape 1.29, square 1,
narrow 0.4), and each print size, a page holds one block of 10 lines of
a fabricated SSN at the top-left corner, the centre or the bottom-right
corner. It is rendered the way OCR renders a page (300 dpi, grayscale)
and read with Vision (both passes). Prints, per block, how many of the
20 lines (10 per pass) Vision read the SSN on. ``--distinct`` numbers
each line, so no two lines are the same.

macOS only (the PyObjC Vision bridge). Fabricated data only.

    python eval/spikes/ocr_render_size.py 3300,3600,4200,5100 8,10 --shapes 0.77,1.29
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from redaction_verifier.views import ocr  # noqa: E402

SSN = "123-45-6789"   # fabricated
PLACES = ("corner", "centre", "far")


def lines_read(long_px: int, shape: float, font: float, where: str,
               distinct: bool) -> tuple[int, tuple[int, int]]:
    """(SSN lines Vision read over both passes, the render's size)."""
    long_pt = long_px * 72 / ocr.OCR_DPI
    width, height = ((long_pt, long_pt / shape) if shape >= 1 else (long_pt * shape, long_pt))
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    x0, y0 = {"corner": (20, 40), "centre": (width / 2 - font * 6, height / 2 - font * 6),
              "far": (width - font * 13, height - font * 13)}[where]
    for i in range(10):
        text = f"Ref {i} SSN {SSN} acct {9000 + i * 37}" if distinct else f"SSN {SSN} is here"
        page.insert_text((x0, y0 + i * font * 1.25), text, fontsize=font)
    pix = page.get_pixmap(dpi=ocr.OCR_DPI, colorspace=fitz.csGRAY)
    on, off = ocr._vision_recognize_batch(pix.tobytes("png"))
    doc.close()
    return sum(SSN in o.text.replace(" ", "") for o in on + off), (pix.width, pix.height)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("long_px", help="comma-separated render longest sides, in pixels")
    parser.add_argument("fonts", help="comma-separated print sizes, in points")
    parser.add_argument("--shapes", default="0.77,1.29,1,0.4",
                        help="comma-separated page width / height ratios")
    parser.add_argument("--distinct", action="store_true", help="number each line")
    args = parser.parse_args()
    fonts = [float(f) for f in args.fonts.split(",")]
    print("render_px font " + " ".join(PLACES) + "  (lines read of 20)")
    for long_px in (int(v) for v in args.long_px.split(",")):
        for shape in (float(s) for s in args.shapes.split(",")):
            for font in fonts:
                cells, size = [], (0, 0)
                for where in PLACES:
                    read, size = lines_read(long_px, shape, font, where, args.distinct)
                    cells.append(read)
                print(f"{size[0]}x{size[1]} {font:g}pt " + " ".join(f"{c:>3}" for c in cells),
                      flush=True)


if __name__ == "__main__":
    main()
