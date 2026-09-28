"""Render a case's first page to a PNG, at build time, into ``--out`` —
never committed (docs/REDESIGN.md's Gallery section: "what a reader
sees")."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

#: Modest: this is illustrative, not diagnostic — small files, fast builds.
DPI = 100


def render_page1(pdf_path: Path, out_png: Path) -> bool:
    """Render *pdf_path*'s first page to *out_png*. Returns False (and
    writes nothing) for a PDF with no pages or one PyMuPDF can't open —
    a build-time render failure must not take the whole gallery down."""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return False
    try:
        if doc.page_count < 1:
            return False
        pix = doc[0].get_pixmap(dpi=DPI)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_png))
        return True
    except Exception:
        return False
    finally:
        doc.close()
