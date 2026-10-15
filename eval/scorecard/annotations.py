"""The annotation part of the inventory's agreement gate (3a-7d, ADR 0011
items 5 and 8; the method from eval/spikes/annotation_appearance.py,
owner decision 11, P1 = (a)).

Per revision, MuPDF's annotation list -- pdf_first_annot then
pdf_first_widget on every page -- against ours, page by page as a
multiset by object number (by /Annots position for an annotation written
directly; Link and Popup are in neither: MuPDF lists and draws neither);
then each annotation's classification against MuPDF's drawing of it.

No object-level signal tells a synthesized appearance from the stored
one in MuPDF 1.28 (pdf_annot_ap, before or after drawing, and
needs_resynthesis all name the stored one), so the gate compares
drawings, batched per revision: copy A is the revision with every
page's /Rotate removed and every annotation's /F and /OC cleared (so
rotation, NoRotate, hidden, NoView and optional content hide nothing);
copy B is copy A with every annotation that has /AP replaced by a
stand-in /Square with the same /Rect, /AP and /AS as written, which
MuPDF draws from /AP whenever /AP /N names a stream. Each annotation is
drawn alone in both (pdf_run_annot into a trace device, and at 4x into
pixels) and read as NOTHING (no mark: no path filled or stroked, no text
element of any kind, no image or shading -- a path clip alone marks
nothing), STORED (trace and pixels equal) or SYNTHESIZED (otherwise; any
annotation without /AP that marks). Ours SYNTHESIZED matches SYNTHESIZED
or NOTHING; ours not synthesized matches STORED or NOTHING. The copies
pair annotations by a private key holding (page, /Annots position) --
a widget's stand-in moves from MuPDF's widget list to its annotation
list.

Not exact: two drawings equal in trace and in 4x pixels read as the
same, so content differing only below that resolution -- an image's
samples, a glyph program -- is not told apart (owner decision 11).
MuPDF's warnings while it loads and draws the two copies are not the
file's (the copies are ours: unhidden, unrotated, stand-ins; drawing
runs appearance content, Phase 4's): they are discarded. Every warning
the file itself gives -- its pages loaded, its appearances synthesized
-- is gated as before, and any warning pending when this starts is a
disagreement here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

import pymupdf

from redaction_verifier.inventory.annotations import Annotation

M = pymupdf.mupdf

# Trace elements that mark the page or hold text: paths filled or
# stroked, any text element (clip-mode and invisible text are
# extractable), images and shadings. A path clip alone marks nothing.
MARKS = (b"<fill_path", b"<stroke_path", b"_text", b"<fill_image", b"<fill_shade")
# The private key each annotation carries in both copies: [page index].
TAG = "RedactionVerifierAnnotationKey"

NOTHING, STORED, SYNTHESIZED = "NOTHING", "STORED", "SYNTHESIZED"

# An annotation's identity in a page's list: its object number, or for
# one written directly in /Annots its position there.
Key = tuple[str, int]


def _name(key: str) -> Any:
    return M.pdf_new_name(key)


def listed(page: pymupdf.Page) -> list[Any]:
    """MuPDF's annotations, then its widgets: what it draws."""
    found = []
    pp = M.pdf_page_from_fz_page(page.this)
    for first, step in ((M.pdf_first_annot, M.pdf_next_annot),
                        (M.pdf_first_widget, M.pdf_next_widget)):
        annot = first(pp)
        while annot.m_internal:
            found.append(annot)
            annot = step(annot)
    return found


def trace(annot: Any) -> bytes:
    buf = M.FzBuffer(1024)
    out = M.FzOutput(buf)
    dev = M.fz_new_trace_device(out)
    M.pdf_run_annot(annot, dev, M.FzMatrix(), M.FzCookie())
    M.fz_close_device(dev)
    M.fz_close_output(out)
    return bytes(M.fz_buffer_extract(buf))


def pixels(annot: Any) -> bytes:
    """The annotation drawn alone, 4x, as raw RGB samples."""
    display = M.pdf_new_display_list_from_annot(annot)
    pix = M.fz_new_pixmap_from_display_list(display, M.fz_scale(4, 4), M.fz_device_rgb(), 0)
    return bytes(pymupdf.Pixmap(pix).samples)


def page_keys(doc: pymupdf.Document, number: int, page: pymupdf.Page) -> list[Key]:
    """MuPDF's list for page *number* of *doc* (loaded: *page*): each
    annotation's object number, or ("direct", its /Annots position) --
    found by identity in the page's /Annots array."""
    pdf = pymupdf._as_pdf_document(doc)
    annots = M.pdf_dict_get(M.pdf_lookup_page_obj(pdf, number), _name("Annots"))
    positions = {M.pdf_array_get(annots, i).m_internal_value(): i
                 for i in range(M.pdf_array_len(annots))}
    keys: list[Key] = []
    for annot in listed(page):
        obj = M.pdf_annot_obj(annot)
        if M.pdf_is_indirect(obj):
            keys.append(("object", M.pdf_to_num(obj)))
        else:
            keys.append(("direct", positions.get(obj.m_internal_value(), -1)))
    return keys


def our_key(annotation: Annotation) -> Key:
    if annotation.number is not None:
        return ("object", annotation.number)
    return ("direct", annotation.index)


def _copies(blob: bytes) -> tuple[pymupdf.Document, pymupdf.Document, set[tuple[int, int]]]:
    """Copy A (/Rotate, /F and /OC cleared) and copy B (copy A with each
    annotation that has /AP replaced by a stand-in), once per revision.
    Each /Annots dictionary is tagged with its first (page, position);
    and the tags of those with /AP as written (MuPDF writes the appearance
    it synthesizes into an annotation written directly in /Annots, so its
    own object cannot tell)."""
    pair = []
    with_ap: set[tuple[int, int]] = set()
    for stand_in in (False, True):
        doc = pymupdf.open(stream=blob, filetype="pdf")
        pdf = pymupdf._as_pdf_document(doc)
        for number in range(doc.page_count):
            page_obj = M.pdf_lookup_page_obj(pdf, number)
            M.pdf_dict_del(page_obj, _name("Rotate"))
            annots = M.pdf_dict_get(page_obj, _name("Annots"))
            for i in range(M.pdf_array_len(annots)):
                annot = M.pdf_array_get(annots, i)
                if not M.pdf_is_dict(annot):
                    continue
                M.pdf_dict_del(annot, _name("F"))
                M.pdf_dict_del(annot, _name("OC"))
                if not M.pdf_dict_get(annot, _name(TAG)).m_internal:
                    tag = M.pdf_new_array(pdf, 2)
                    M.pdf_array_push(tag, M.pdf_new_int(number))
                    M.pdf_array_push(tag, M.pdf_new_int(i))
                    M.pdf_dict_put(annot, _name(TAG), tag)
                ap = M.pdf_dict_get(annot, _name("AP"))
                if not ap.m_internal or M.pdf_is_null(ap):
                    continue
                with_ap.add(_tag_of(M.pdf_dict_get(annot, _name(TAG))))
                if not stand_in:
                    continue
                square = M.pdf_new_dict(pdf, 6)
                M.pdf_dict_put(square, _name("Type"), _name("Annot"))
                M.pdf_dict_put(square, _name("Subtype"), _name("Square"))
                for key in ("Rect", "AP", "AS", TAG):
                    value = M.pdf_dict_get(annot, _name(key))  # as written: references kept
                    if value.m_internal and not M.pdf_is_null(value):
                        M.pdf_dict_put(square, _name(key), value)
                if M.pdf_is_indirect(annot):
                    M.pdf_update_object(pdf, M.pdf_to_num(annot), square)
                else:
                    M.pdf_array_put(annots, i, square)
        pair.append(doc)
    return pair[0], pair[1], with_ap


def _tag_of(tag: Any) -> tuple[int, int]:
    return M.pdf_to_int(M.pdf_array_get(tag, 0)), M.pdf_to_int(M.pdf_array_get(tag, 1))


def _tag(annot: Any) -> tuple[int, int]:
    return _tag_of(M.pdf_dict_get(M.pdf_annot_obj(annot), _name(TAG)))


def reading(a: Any, b: Any, written_ap: bool) -> str:
    """One annotation: its drawing in copy A (*a*) against copy B's (*b*);
    *written_ap*: it has /AP as written."""
    drawn = trace(a)
    if not any(mark in drawn for mark in MARKS):
        return NOTHING
    if not written_ap:
        return SYNTHESIZED
    return STORED if drawn == trace(b) and pixels(a) == pixels(b) else SYNTHESIZED


def readings(blob: bytes) -> dict[tuple[int, int], str]:
    """MuPDF's reading of every annotation it lists in *blob*, by its
    (page, /Annots position) tag."""
    real, stand_ins, with_ap = _copies(blob)
    try:
        found: dict[tuple[int, int], str] = {}
        for number in range(real.page_count):
            page_a, page_b = real[number], stand_ins[number]  # kept alive: annotations bind to pages
            in_b = {_tag(b): b for b in listed(page_b)}
            for a in listed(page_a):
                tag = _tag(a)
                if tag not in found:
                    found[tag] = reading(a, in_b[tag], tag in with_ap)
        return found
    finally:
        real.close()
        stand_ins.close()


def compare_annotations(doc: pymupdf.Document, blob: bytes,
                        ours: tuple[Annotation, ...],
                        fail: Callable[[str, int | None], Exception]) -> int:
    """The revision's annotation list and classifications, ours against
    MuPDF's (*doc*: the revision, its pages loaded). Raises
    ``fail(what, object number)`` on the first disagreement; returns the
    number of annotations compared."""
    by_page: dict[int, list[Annotation]] = {}
    for annotation in ours:
        by_page.setdefault(annotation.page, []).append(annotation)
    for number in range(doc.page_count):
        page = doc[number]
        theirs = Counter(page_keys(doc, number, page))
        mine = Counter(our_key(a) for a in by_page.get(number, ()))
        if theirs != mine:
            differ = sorted((theirs - mine) + (mine - theirs))
            raise fail("list", differ[0][1] if differ and differ[0][0] == "object" else None)
    if not ours:
        return 0
    found = readings(blob)
    # Each annotation's reading, by the tag its first /Annots slot gave it
    # (an object listed twice is tagged once, where MuPDF's copy met it).
    first: dict[int, tuple[int, int]] = {}
    for annotation in ours:
        if annotation.number is not None:
            first.setdefault(annotation.number, (annotation.page, annotation.index))
    for annotation in ours:
        tag = (first[annotation.number] if annotation.number is not None
               else (annotation.page, annotation.index))
        seen = found.get(tag)
        if seen is None:
            raise fail("unread", annotation.number)
        if seen != NOTHING and (seen == SYNTHESIZED) != annotation.synthesized:
            raise fail("classification", annotation.number)
    return len(ours)
