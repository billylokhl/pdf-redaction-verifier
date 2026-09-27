"""Carriers × producer layouts: the same SSN on each surface a PDF can
carry text on, saved in each on-disk layout real producers emit.

Every early fixture used PyMuPDF's default classic-xref save; real
producers (Acrobat, Word, Ghostscript, Chrome) write object streams
behind an xref stream, linearized or incrementally updated files. A bug
that labelled every object packed in an object stream ORPHANED shipped
green because nothing resembled that input. This grid keeps every
carrier honest in every layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import fitz

from ..model import SSN, case, expect
from ..pdfkit import save

# ── Producer layouts ────────────────────────────────────────────────────


def _classic(doc: fitz.Document, path: Path) -> None:
    save(doc, path)


def _object_streams(doc: fitz.Document, path: Path) -> None:
    save(doc, path, deflate=True, use_objstms=1)


def _incremental(doc: fitz.Document, path: Path) -> None:
    """A base save, then an incremental update: reachability must resolve
    across the appended section, not only the newest one."""
    save(doc, path, deflate=True)
    followup = fitz.open(str(path))
    followup.set_metadata({**followup.metadata, "keywords": "revised"})
    followup.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP,
                  no_new_id=True)
    followup.close()


def _garbage_collected(doc: fitz.Document, path: Path) -> None:
    """A full rewrite with garbage collection and object streams — what a
    correct redactor emits: no unreferenced leftovers."""
    save(doc, path, deflate=True, garbage=4, use_objstms=1)


LAYOUTS: dict[str, tuple[Callable[[fitz.Document, Path], None], str]] = {
    "classic": (_classic, "a classic cross-reference table"),
    "objstm": (_object_streams, "object streams behind an xref stream"),
    "incremental": (_incremental, "an incremental update over a base save"),
    "gc": (_garbage_collected, "a garbage-collected rewrite with object streams"),
}

# ── Carriers ────────────────────────────────────────────────────────────
# Each planter puts the SSN on one surface of a fresh document.


def _content_stream(doc: fitz.Document) -> None:
    page = doc.new_page(); page.insert_text((72, 72), "cover")
    doc.update_stream(page.get_contents()[0],
                      f"BT /F1 12 Tf 72 200 Td (SSN {SSN}) Tj ET".encode())


def _dict_string(doc: fitz.Document) -> None:
    doc.new_page().insert_text((72, 72), "cover")
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /Type /Custom /Note (SSN {SSN}) >>")
    doc.xref_set_key(doc.pdf_catalog(), "MyRef", f"{xref} 0 R")   # referenced: live


def _info(doc: fitz.Document) -> None:
    doc.new_page().insert_text((72, 72), "Cover page. Nothing visible here.")
    doc.set_metadata({"subject": f"case reference {SSN}", "title": "Application",
                      "creationDate": "", "modDate": ""})


def _annotation(doc: fitz.Document) -> None:
    doc.new_page().add_freetext_annot(fitz.Rect(50, 50, 320, 90), f"SSN {SSN}")


def _form_field(doc: fitz.Document) -> None:
    widget = fitz.Widget()
    widget.field_name = "ssn"; widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.field_value = f"SSN {SSN}"; widget.rect = fitz.Rect(50, 50, 320, 90)
    doc.new_page().add_widget(widget)


def _orphaned_content(doc: fitz.Document) -> None:
    """A content stream referenced by nothing — a redaction that left the
    original object behind."""
    doc.new_page().insert_text((72, 72), "clean visible text")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<< >>")
    doc.update_stream(xref, f"BT /F1 12 Tf 72 200 Td (SSN {SSN}) Tj ET".encode())


def _clean(doc: fitz.Document) -> None:
    doc.new_page().insert_text((72, 72), "This page is entirely clean.")


# carrier -> (planter, cell, storage, description)
CARRIERS: dict[str, tuple[Callable[[fitz.Document], None], str, str, str]] = {
    "content-stream": (_content_stream, "live.plain", "live", "a page's content stream"),
    "dict-string": (_dict_string, "live.plain", "live",
                    "a string in a dictionary the catalog references"),
    "info": (_info, "metadata.plain", "live", "the Info dictionary's subject"),
    "annotation": (_annotation, "annot-fields.plain", "live", "a free-text annotation"),
    "form-field": (_form_field, "annot-fields.plain", "live", "a form field's value"),
    "orphaned-content": (_orphaned_content, "orphaned.plain", "orphaned",
                         "a content stream nothing references"),
}

# carrier -> (mistake, recovery), independent of the on-disk layout.
MISTAKE: dict[str, str] = {
    "content-stream": "Nobody redacted it.",
    "dict-string": "A tool left a reference value in a dictionary the catalog reaches.",
    "info": "Redacting the page but not the document properties.",
    "annotation": "Leaving a free-text annotation with the value in its /Contents.",
    "form-field": "Redacting the page but not the form field's stored value.",
    "orphaned-content": "Saving without garbage collection.",
}
RECOVERY: dict[str, str] = {
    "content-stream": "Read the page.",
    "dict-string": "Walk the catalog's dictionaries and read the string.",
    "info": "File > Properties in any viewer.",
    "annotation": "Open the annotation and read its contents.",
    "form-field": "Open the form in any PDF editor and inspect the field's value.",
    "orphaned-content": "Decompress the file and read the leftover content stream.",
}


def _builder(plant, layout):
    def build(path: Path) -> None:
        doc = fitz.open(); plant(doc); layout(doc, path)
    return build


for carrier, (plant, cell, storage, where) in CARRIERS.items():
    for name, (layout, how) in LAYOUTS.items():
        # Garbage collection removes what nothing references: the orphan
        # is gone, so that file is clean.
        cleaned = carrier == "orphaned-content" and name == "gc"
        common = dict(grid="carriers", params=(("carrier", carrier), ("layout", name)),
                      writer="fitz")
        if cleaned:
            case("leftover.orphan-removed-by-gc", truth="clean", features=cell,
                 expected=expect(0), story=f"The SSN in {where}, saved as {how}: the "
                 "rewrite drops the unreferenced object, so nothing is left.",
                 **common)(_builder(plant, layout))  # type: ignore[arg-type]
            continue
        family = {"orphaned": "leftover"}.get(storage,
                                              "page" if carrier == "content-stream" else "document")
        found = (("SSN", storage),)
        if carrier == "info" and name == "incremental":
            # The update rewrites the Info dictionary (keywords), so its
            # earlier version, still holding the SSN, is superseded too.
            found += (("SSN", "superseded"),)  # type: ignore[assignment]
        case(f"{family}.{carrier}-{name}",
             truth="leak", cells=cell,
             expected=expect(1, findings=found, layers=(("SSN", "Objects"),)),
             story=f"The SSN in {where}, saved as {how}.",
             mistake=MISTAKE[carrier], recovery=RECOVERY[carrier],
             **common)(_builder(plant, layout))  # type: ignore[arg-type]

for name, (layout, how) in LAYOUTS.items():
    case(f"document.clean-{name}", truth="clean", features="live.plain", expected=expect(0),
         story=f"A clean page saved as {how}: nothing may be called orphaned.",
         grid="carriers", params=(("carrier", "clean"), ("layout", name)),
         )(_builder(_clean, layout))
