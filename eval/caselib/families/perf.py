"""Large performance files (docs/REDESIGN.md §5 / Phase 0b-2 / 0c): three
generated, deterministic-shape workloads representative of real documents
— a ~300-page scanned-style document, a ~500-page text document with an
embedded font (Word-export-like), and an image-heavy ~50 MB file.

These are slow on purpose (that is the point of a performance fixture) and
so are marked ``perf=True``: excluded from the default test run (opt in
with ``RUN_PERF=1``, see tests/test_case_library.py) and from the build
lock (caselib.lock.lockable) — see eval/README.md for how to run them."""

from __future__ import annotations

import os
from pathlib import Path

import fitz

from ..model import SSN, case, expect
from ..pdfkit import FILLER, embedded_font, png_of, save

SECRET = f"SSN {SSN}"

SCAN_PAGE_COUNT = 300
TEXT_PAGE_COUNT = 500
TEXT_SECRET_PAGE = 250          # buried in the middle, not the first/last page
IMAGE_HEAVY_PAGES = 10
IMAGE_SIDE = 1291                # ~5 MB/page raw RGB (1291*1291*3 ≈ 5.00 MB)


@case(
    "file.perf-scanned-300", truth="clean", features="live.pixels", writer="fitz",
    expected=expect(0), perf=True,
    story="A ~300-page scanned-style document: every page is an image of "
          "text, like a photocopier's or fax's output, exercising the OCR "
          "layer's per-page rendering and recognition cost at a realistic "
          "page count.",
)
def scanned_300(path: Path) -> None:
    # Ten distinct page images, cycled: still one real render+OCR pass per
    # page at scan time (that is what this fixture measures), without
    # paying for 300 distinct renders at build time.
    templates = [
        png_of(f"Page {i + 1} of a scanned report. {FILLER[i % len(FILLER)]}", width=850, height=110)
        for i in range(10)
    ]
    doc = fitz.open()
    for i in range(SCAN_PAGE_COUNT):
        page = doc.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(50, 50, 562, 110), stream=templates[i % len(templates)])
    save(doc, path)


@case(
    "file.perf-text-500", truth="leak", cells="live.font", writer="fitz",
    expected=expect(1, findings=(("SSN", "live"),), layers=(("SSN", "Text"),)),
    perf=True,
    story="A ~500-page text document in an embedded font, the way an "
          "exported Word document or a long report commonly arrives: "
          "exercises the Text layer's per-page cost at a realistic page "
          "count. One page in the middle keeps the SSN — everything else "
          "is filler.",
)
def text_500(path: Path) -> None:
    doc = fitz.open()
    for i in range(TEXT_PAGE_COUNT):
        page = doc.new_page()
        font = embedded_font(page)   # PyMuPDF dedupes the identical buffer
        lines = list(FILLER)
        if i == TEXT_SECRET_PAGE:
            lines.append(SECRET)
        for j, text in enumerate(lines):
            page.insert_text((72, 72 + 16 * j), text, fontname=font)
    save(doc, path)


@case(
    "file.perf-image-heavy", truth="clean", features="live.pixels", writer="fitz",
    expected=expect(0), perf=True,
    story="An image-heavy ~50 MB file: a handful of large, incompressible "
          "(pseudo-random) full-page images, exercising memory and "
          "throughput on a big-but-few-objects file rather than a "
          "many-small-objects one.",
)
def image_heavy(path: Path) -> None:
    doc = fitz.open()
    rng = os.urandom  # C-speed, no compressible structure to inflate the win
    for _ in range(IMAGE_HEAVY_PAGES):
        page = doc.new_page()
        samples = rng(IMAGE_SIDE * IMAGE_SIDE * 3)
        pix = fitz.Pixmap(fitz.csRGB, IMAGE_SIDE, IMAGE_SIDE, samples, False)
        page.insert_image(fitz.Rect(0, 0, 612, 792), pixmap=pix)
    save(doc, path)
