"""Spike for 3a-7d (docs/phase3a-plan.md): how can the gate tell, per
annotation, whether MuPDF draws the stored appearance or one it
synthesizes? Result (2026-10-12, MuPDF 1.28.2): no object-level signal works -- neither
`pdf_annot_ap` (before or after drawing, or after pdf_update_annot), nor
the annotation's /AP in MuPDF's in-memory document, nor
pdf_annot_needs_resynthesis tells a synthesized appearance from the stored
one (column ap-object-changed: always 0). What works is comparing drawings:
MuPDF's trace (fz_new_trace_device + pdf_run_annot) of the annotation
against the trace of a stand-in -- a /Square with the same /Rect, /AP,
/AS and /F, which MuPDF draws from /AP whenever /AP /N names a stream --
in a separate copy of the document. Three readings: NOTHING (no mark),
STORED (equal traces), SYNTHESIZED (otherwise). On every fixture below
MuPDF's reading agrees with ADR 0011 item 5's rules; pixels confirm it
(the stored box is drawn exactly when the reading is STORED). Fabricated
fixtures only.

    PYTHONPATH=eval:. python eval/spikes/annotation_appearance.py
"""

from __future__ import annotations

import pymupdf
from scorecard.pdfgen import Writer

M = pymupdf.mupdf
RED = b"1 0 0 rg 0 0 50 20 re f"


def build(annot: bytes, extra: dict[int, bytes], acroform: bytes = b"",
          streams: dict[int, bytes] | None = None) -> bytes:
    w = Writer()
    t: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    t[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R%s >>" % acroform), 0)
    t[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    t[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                        b" /Resources << >> /Annots [4 0 R] >>"), 0)
    t[4] = (1, w.obj(4, annot), 0)
    for n, body in extra.items():
        t[n] = (1, w.obj(n, body), 0)
    for n, data in (streams or {5: RED}).items():
        t[n] = (1, w.stream(n, b" /Subtype /Form /BBox [0 0 50 20] /Resources << >>", data), 0)
    w.epilogue(w.table(t, b"/Size %d /Root 1 0 R" % (max(t) + 1)))
    return bytes(w.out)


RECT = b"/Rect [10 10 60 30]"
CASES = {
    "square-with-ap": (build(b"<< /Type /Annot /Subtype /Square %s /AP << /N 5 0 R >> >>" % RECT, {}), False),
    "square-no-ap": (build(b"<< /Type /Annot /Subtype /Square %s /C [0 0 1] >>" % RECT, {}), True),
    "square-ap-dict-no-as": (build(b"<< /Type /Annot /Subtype /Square %s /C [0 0 1] /AP << /N << /On 5 0 R >> >> >>" % RECT, {}), True),
    "square-ap-dict-as": (build(b"<< /Type /Annot /Subtype /Square %s /AS /On /AP << /N << /On 5 0 R >> >> >>" % RECT, {}), False),
    "sig-unsigned": (build(b"<< /Type /Annot /Subtype /Widget /FT /Sig /T (s) %s /AP << /N 5 0 R >> >>" % RECT,
                           {}, b" /AcroForm << /Fields [4 0 R] >>"), True),
    "sig-signed": (build(b"<< /Type /Annot /Subtype /Widget /FT /Sig /T (s) /V 6 0 R %s /AP << /N 5 0 R >> >>" % RECT,
                         {6: b"<< /Type /Sig /Filter /Adobe.PPKLite >>"}, b" /AcroForm << /Fields [4 0 R] >>"), False),
    "widget-text-ap": (build(b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (t) /V (SSN 123-45-6789) %s /AP << /N 5 0 R >> /DA (/Helv 9 Tf 0 g) >>" % RECT,
                             {}, b" /AcroForm << /Fields [4 0 R] >>"), False),
    "widget-need-appearances": (build(b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (t) /V (SSN 123-45-6789) %s /AP << /N 5 0 R >> /DA (/Helv 9 Tf 0 g) >>" % RECT,
                                      {}, b" /AcroForm << /Fields [4 0 R] /NeedAppearances true >>"), True),
    "widget-no-ap": (build(b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (t) /V (SSN 123-45-6789) %s /DA (/Helv 9 Tf 0 g) >>" % RECT,
                           {}, b" /AcroForm << /Fields [4 0 R] >>"), True),
    "link": (build(b"<< /Type /Annot /Subtype /Link %s /Border [0 0 0] >>" % RECT, {}), False),
    "sig-unsigned-hidden": (build(b"<< /Type /Annot /Subtype /Widget /FT /Sig /T (s) /F 2 %s /AP << /N 5 0 R >> >>" % RECT,
                                  {}, b" /AcroForm << /Fields [4 0 R] >>"), True),
    "freetext-no-ap": (build(b"<< /Type /Annot /Subtype /FreeText %s /Contents (SSN 123-45-6789) /DA (/Helv 9 Tf 0 g) >>" % RECT, {}), True),
    "square-ap-as-missing-entry": (build(b"<< /Type /Annot /Subtype /Square %s /C [0 0 1] /AS /Off /AP << /N << /On 5 0 R >> >> >>" % RECT, {}), True),
}


def ap_n(obj: object) -> tuple[int, int] | None:
    """The /AP /N in effect: its object number, following /AS."""
    ap = M.pdf_dict_get(obj, M.pdf_new_name("AP"))
    n = M.pdf_dict_get(ap, M.pdf_new_name("N"))
    if M.pdf_is_dict(n) and not M.pdf_is_stream(n):
        n = M.pdf_dict_get(n, M.pdf_dict_get(obj, M.pdf_new_name("AS")))
    return (M.pdf_to_num(n), M.pdf_to_gen(n)) if M.pdf_is_indirect(n) else None


def annotations(page: pymupdf.Page) -> list[object]:
    found = []
    pp = page._pdf_page() if hasattr(page, "_pdf_page") else M.pdf_page_from_fz_page(page.this)
    for first, step in ((M.pdf_first_annot, M.pdf_next_annot), (M.pdf_first_widget, M.pdf_next_widget)):
        a = first(pp)
        while a.m_internal:
            found.append(a)
            a = step(a)
    return found


def trace(annot: object) -> bytes:
    """MuPDF's drawing of *annot*, as its trace device writes it."""
    buf = M.FzBuffer(1024)
    out = M.FzOutput(buf)
    dev = M.fz_new_trace_device(out)
    M.pdf_run_annot(annot, dev, M.FzMatrix(), M.FzCookie())
    M.fz_close_device(dev)
    M.fz_close_output(out)
    return bytes(M.fz_buffer_extract(buf))


STAND_IN_KEYS = ("Rect", "AP", "AS", "F")
# Trace-device elements that put marks on the page (or clip what does).
DRAWING_OPS = (b"<fill_", b"<stroke_", b"<clip_", b"text", b"<fill_image", b"<fill_shade")


def drawn_as_stored(data: bytes, index: int) -> bool | None:
    """Does MuPDF draw annotation *index* of page 1 as its stored /AP? Its
    trace against a stand-in's: a /Square with the same /Rect, /AP, /AS
    and /F -- which MuPDF draws from /AP whenever /AP /N names a stream
    (control: square-with-ap) -- in a separate copy of the document.
    None when the annotation has no stored stream to compare."""
    real_doc = pymupdf.open(stream=data, filetype="pdf")
    real_page = real_doc[0]  # kept alive: an annotation is bound to its page
    real = annotations(real_page)[index]
    obj = M.pdf_annot_obj(real)
    if ap_n(obj) is None or not M.pdf_is_indirect(obj):
        return None
    number = M.pdf_to_num(obj)
    copy = pymupdf.open(stream=data, filetype="pdf")
    pdf = pymupdf._as_pdf_document(copy)
    stand_in = M.pdf_new_dict(pdf, 8)
    M.pdf_dict_put(stand_in, M.pdf_new_name("Type"), M.pdf_new_name("Annot"))
    M.pdf_dict_put(stand_in, M.pdf_new_name("Subtype"), M.pdf_new_name("Square"))
    original = M.pdf_load_object(pdf, number)
    for key in STAND_IN_KEYS:
        value = M.pdf_dict_get(original, M.pdf_new_name(key))
        if value.m_internal:
            M.pdf_dict_put(stand_in, M.pdf_new_name(key), value)
    M.pdf_update_object(pdf, number, stand_in)
    copy_page = copy[0]
    for annot in annotations(copy_page):
        if M.pdf_to_num(M.pdf_annot_obj(annot)) == number:
            return trace(real) == trace(annot)
    return None


def mupdf_reading(data: bytes, index: int) -> str:
    """What MuPDF does with annotation *index*: NOTHING (draws nothing --
    hidden, a link, an empty synthesis), STORED (draws exactly what its
    stored /AP draws), or SYNTHESIZED (draws something else). The gate
    compares the inventory's SYNTHESIZED classification with this:
    SYNTHESIZED matches SYNTHESIZED or NOTHING, not-synthesized matches
    STORED or NOTHING -- what nothing shows cannot differ."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc[0]  # kept alive: an annotation is bound to its page
    drawn = trace(annotations(page)[index])
    if not any(op in drawn for op in DRAWING_OPS):
        return "NOTHING"
    stored = drawn_as_stored(data, index)
    return "STORED" if stored else "SYNTHESIZED"


def main() -> None:
    for name, (data, expected) in CASES.items():
        doc = pymupdf.open(stream=data, filetype="pdf")
        stored = {}
        pdf = pymupdf._as_pdf_document(doc)
        obj = M.pdf_load_object(pdf, 4)
        stored = ap_n(obj)
        page = doc[0]
        page.get_pixmap()  # draw: MuPDF synthesizes what it synthesizes now
        found = annotations(page)
        after = [ap_n(M.pdf_annot_obj(a)) for a in found]
        drawn_ap = after[0] if after else None
        synthesized = drawn_ap != stored
        reading = mupdf_reading(data, 0) if found else "NOTHING"
        agrees = reading == "NOTHING" or (reading == "SYNTHESIZED") == expected
        print(f"{name:28} ap-object-changed={synthesized:1}  mupdf={reading:11}"
              f"  adr-item-5-synthesized={expected!s:5}  {'agrees' if agrees else 'DISAGREES'}")


if __name__ == "__main__":
    main()
