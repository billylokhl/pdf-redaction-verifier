"""Builders for the robustness corpus.

Two axes the hand-written regression tests did not cover:

* **Producer layouts.** Every earlier fixture used ``fitz``'s default
  ``save()``, which writes a classic cross-reference table. Real
  producers (Acrobat, Word, Ghostscript, Chrome, most scanners) emit
  PDF 1.5+ *object streams* behind an *xref stream*, and linearized or
  incrementally-updated files. The ObjStm false-ORPHANED bug shipped
  green precisely because nothing here resembled that input.

* **Carrier surfaces.** A secret can hide in a content-stream literal, a
  dictionary string, /Info metadata, an annotation, a form field, an
  object packed inside an ObjStm, or an object orphaned by a redaction.
  Each is a distinct path through the scanner.

Nothing binary is committed: every document is generated. Real vendor
files can still be exercised without committing them — drop a ``*.pdf``
plus a ``*.pdf.secrets.json`` sidecar into ``tests/corpus_pdfs/`` and
``test_corpus`` will pick them up (see ``iter_real_corpus``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import fitz

# The canonical example SSN (same value the rest of the suite uses); never
# a real person's data.
SSN = "123-45-6789"

REAL_CORPUS_DIR = Path(__file__).parent / "corpus_pdfs"


# ── Producer layouts ───────────────────────────────────────────────────────
# Each saver takes an open fitz doc and a destination path and writes it in
# one on-disk layout. The point is the container shape, not the content, so
# every layout carries the same planted secret.

def save_classic(doc: fitz.Document, path: Path) -> None:
    """Classic cross-reference table (older producers, fitz default)."""
    doc.save(str(path))


def save_object_streams(doc: fitz.Document, path: Path) -> None:
    """Object streams + xref stream: the PDF 1.5+ default of Acrobat,
    Ghostscript, Chrome print-to-PDF and most modern producers."""
    doc.save(str(path), deflate=True, use_objstms=1)


def save_incremental(doc: fitz.Document, path: Path) -> None:
    """A base save followed by an incremental update, so the file has a
    multi-section xref chained through /Prev — reachability must resolve
    across the appended section, not just the newest one."""
    doc.save(str(path), deflate=True)
    followup = fitz.open(str(path))
    followup.set_metadata({**followup.metadata, "keywords": "revised"})
    followup.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    followup.close()


def save_garbage_collected(doc: fitz.Document, path: Path) -> None:
    """Full rewrite with garbage collection + object streams — what a
    correct redactor should emit (no unreferenced leftovers)."""
    doc.save(str(path), deflate=True, garbage=4, use_objstms=1)


PRODUCER_LAYOUTS: dict[str, Callable[[fitz.Document, Path], None]] = {
    "classic": save_classic,
    "objstm": save_object_streams,
    "incremental": save_incremental,
    "gc-objstm": save_garbage_collected,
}


def _new_doc_with_secret_in_info() -> fitz.Document:
    """A one-page doc whose SSN lives in an /Info dictionary string — a
    dict-resident secret, the surface that gets packed into an ObjStm."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Cover page. Nothing visible here.")
    doc.set_metadata({"subject": f"case reference {SSN}", "title": "Application"})
    return doc


def build_layout(layout: str, path: Path) -> None:
    """Write the same dict-resident secret in the named producer layout."""
    doc = _new_doc_with_secret_in_info()
    PRODUCER_LAYOUTS[layout](doc, path)
    doc.close()


def build_clean_layout(layout: str, path: Path) -> None:
    """A clean document in the named layout — for false-positive checks
    (a modern object-stream file must not be accused of orphans)."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "This page is entirely clean.")
    PRODUCER_LAYOUTS[layout](doc, path)
    doc.close()


# ── Carrier surfaces ───────────────────────────────────────────────────────
# Each planter puts the SSN on ONE surface and returns whether the carrier
# is expected to be reported as ORPHANED (i.e. unreferenced leftover).

def plant_content_stream(path: Path) -> bool:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "cover")
    doc.update_stream(
        page.get_contents()[0],
        f"BT /F1 12 Tf 72 200 Td (SSN {SSN}) Tj ET".encode())
    doc.save(str(path))
    doc.close()
    return False


def plant_dict_string(path: Path) -> bool:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "cover")
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /Type /Custom /Note (SSN {SSN}) >>")
    # Reference it from the catalog so it is NOT an orphan.
    doc.xref_set_key(doc.pdf_catalog(), "MyRef", f"{xref} 0 R")
    doc.save(str(path))
    doc.close()
    return False


def plant_info_metadata(path: Path) -> bool:
    doc = _new_doc_with_secret_in_info()
    doc.save(str(path))
    doc.close()
    return False


def plant_annotation(path: Path) -> bool:
    doc = fitz.open()
    doc.new_page().add_freetext_annot(fitz.Rect(50, 50, 320, 90), f"SSN {SSN}")
    doc.save(str(path))
    doc.close()
    return False


def plant_form_field(path: Path) -> bool:
    doc = fitz.open()
    page = doc.new_page()
    widget = fitz.Widget()
    widget.field_name = "ssn"
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.field_value = f"SSN {SSN}"
    widget.rect = fitz.Rect(50, 50, 320, 90)
    page.add_widget(widget)
    doc.save(str(path))
    doc.close()
    return False


def plant_objstm_packed(path: Path) -> bool:
    """A secret in a dict object that the writer packs INTO an /ObjStm
    container (an annotation, which fitz packs; /Info is kept out). This
    is the surface behind the ObjStm false-ORPHANED bug: the packed
    object is reachable and must not be labelled orphaned."""
    doc = fitz.open()
    doc.new_page().add_freetext_annot(fitz.Rect(50, 50, 320, 90), f"SSN {SSN}")
    doc.save(str(path), deflate=True, use_objstms=1)
    doc.close()
    return False


def plant_orphaned_content(path: Path) -> bool:
    """The founding failure mode: a content stream present in the bytes
    but referenced by nothing — a redaction that dropped a box over the
    text and left the object behind."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "clean visible text")
    orphan = doc.get_new_xref()          # referenced by nothing
    doc.update_object(orphan, "<< /Length 40 >>")
    doc.update_stream(
        orphan, f"BT /F1 12 Tf 72 200 Td (SSN {SSN}) Tj ET".encode())
    doc.save(str(path))
    doc.close()
    return True


# name -> (planter, layer that must catch it, expect_orphan)
# "Objects" surfaces are checked directly against scan_pdf_objects (fast,
# no external tools); "full" surfaces need the CLI (OCR/metadata tools).
CARRIER_SURFACES: dict[str, tuple[Callable[[Path], bool], str]] = {
    "content-stream": (plant_content_stream, "Objects"),
    "dict-string": (plant_dict_string, "Objects"),
    "annotation": (plant_annotation, "Objects"),
    "form-field": (plant_form_field, "Objects"),
    "objstm-packed": (plant_objstm_packed, "Objects"),
    "orphaned-content": (plant_orphaned_content, "Objects"),
}


# ── Raw-bytes layouts fitz will not emit ────────────────────────────────────
# Constructed as literal PDF so the exact structural trap is reproduced.

def write_indirect_filter_image(path: Path) -> None:
    """An image whose /Filter is an indirect reference (kind 'xref').

    The classifier must resolve the reference and treat the object as
    binary; a naive check parses the JPEG bytes as text.
    """
    path.write_bytes(
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
        b"/Resources<<>>>>endobj\n"
        b"4 0 obj/DCTDecode endobj\n"
        b"5 0 obj<</Type/XObject/Subtype/Image/Width 1/Height 1"
        b"/Filter 4 0 R/Length 4>>stream\n\xff\xd8\xff\xe0\nendstream endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF"
    )


def iter_real_corpus():
    """Yield (path, expected_secret_names) for any real vendor PDFs the
    user has dropped into tests/corpus_pdfs/ with a .secrets.json sidecar.

    This is how real Acrobat/Word/scanner output gets exercised without
    committing binaries to the repo. Empty when the directory is absent.
    """
    if not REAL_CORPUS_DIR.is_dir():
        return
    for pdf in sorted(REAL_CORPUS_DIR.glob("*.pdf")):
        sidecar = pdf.with_suffix(".pdf.secrets.json")
        expected = json.loads(sidecar.read_text()) if sidecar.exists() else []
        yield pdf, expected
