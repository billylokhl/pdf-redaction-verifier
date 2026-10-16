"""Make the OCR self-check PDF: a fabricated SSN that exists only as pixels.

The page carries one image and no text layer, so only the OCR check
(Apple Vision) can find the number. Fabricated data only.

    python examples/make_ocr_check.py [OUT_DIR]

writes OUT_DIR/ocr-check.pdf and OUT_DIR/ocr-check-secrets.json
(OUT_DIR defaults to this script's folder). Pass --clean to write
ocr-clean.pdf instead: the same kind of page without the number.
"""

import json
import sys
from pathlib import Path

import pymupdf

SECRET = {"name": "Test SSN (fabricated)", "value": "123-45-6789"}


def image_only_pdf(lines, out):
    # 1. Draw the text on a scratch page and photograph it at 200 dpi.
    scratch = pymupdf.open()
    page = scratch.new_page(width=612, height=792)
    y = 120
    for line in lines:
        page.insert_text((72, y), line, fontsize=20, fontname="helv")
        y += 40
    png = page.get_pixmap(dpi=200).tobytes("png")
    scratch.close()
    # 2. Put only that picture on a fresh page: no text layer at all.
    doc = pymupdf.open()
    doc.new_page(width=612, height=792).insert_image(
        pymupdf.Rect(0, 0, 612, 792), stream=png)
    doc.set_metadata({})
    doc.save(out, garbage=4, deflate=True, no_new_id=True)
    doc.close()


def main(argv):
    clean = "--clean" in argv
    args = [a for a in argv if a != "--clean"]
    out_dir = Path(args[0]) if args else Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        image_only_pdf(["Sample letter", "Nothing to hide on this page."],
                       str(out_dir / "ocr-clean.pdf"))
        return
    image_only_pdf(["Sample letter", f"Applicant SSN: {SECRET['value']}"],
                   str(out_dir / "ocr-check.pdf"))
    (out_dir / "ocr-check-secrets.json").write_text(
        json.dumps([SECRET], indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
