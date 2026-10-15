"""How much of a large page render does Apple Vision read? (#128)

The measurement behind ``redaction_verifier.views.ocr.MAX_OCR_RENDER_SIDE``.
For each page size (the longest side in points, portrait, 0.77 as wide,
like letter) and print size, a page holds one block of 10 lines of a
fabricated SSN at the top-left corner or at the centre. It is rendered
the way OCR renders a page (300 dpi, grayscale) and read with Vision
(both passes). Prints the number of the 20 lines (10 per pass) whose
SSN Vision read, per block. ``--distinct`` numbers each line, so no two
lines are the same.

macOS only (the PyObjC Vision bridge). Fabricated data only.

    python eval/spikes/ocr_render_size.py 792,1008,1224,1440,2400 6,8,10,24
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from redaction_verifier.views import ocr  # noqa: E402

SSN = "123-45-6789"   # fabricated


def lines_read(long_pt: int, font: float, where: str, distinct: bool) -> tuple[int, int]:
    """(SSN lines Vision read over both passes, the render's longest side)."""
    width = round(long_pt * 0.77)
    doc = fitz.open()
    page = doc.new_page(width=width, height=long_pt)
    x0, y0 = (20, 40) if where == "corner" else (width / 2 - font * 6, long_pt / 2 - font * 6)
    for i in range(10):
        text = f"Ref {i} SSN {SSN} acct {9000 + i * 37}" if distinct else f"SSN {SSN} is here"
        page.insert_text((x0, y0 + i * font * 1.25), text, fontsize=font)
    pix = page.get_pixmap(dpi=ocr.OCR_DPI, colorspace=fitz.csGRAY)
    on, off = ocr._vision_recognize_batch(pix.tobytes("png"))
    doc.close()
    return (sum(SSN in o.text.replace(" ", "") for o in on + off), max(pix.width, pix.height))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("long_pt", help="comma-separated longest page sides, in points")
    parser.add_argument("fonts", help="comma-separated print sizes, in points")
    parser.add_argument("--distinct", action="store_true", help="number each line")
    args = parser.parse_args()
    fonts = [float(f) for f in args.fonts.split(",")]
    print("long_pt long_px " + " ".join(f"{f:g}pt-corner {f:g}pt-centre" for f in fonts))
    for long_pt in (int(v) for v in args.long_pt.split(",")):
        cells, long_px = [], 0
        for font in fonts:
            for where in ("corner", "centre"):
                read, long_px = lines_read(long_pt, font, where, args.distinct)
                cells.append(read)
        print(long_pt, long_px, " ".join(f"{c:>5}" for c in cells), flush=True)


if __name__ == "__main__":
    main()
