"""Spike for 3a-7d (docs/phase3a-plan.md): how can the gate tell, per
annotation, whether MuPDF draws the stored appearance or one it
synthesizes (ADR 0011 item 5)?

Result (2026-10-12, MuPDF 1.28.2):

- No object-level signal works: pdf_annot_ap (before drawing, and after
  drawing and pdf_update_annot) and pdf_annot_needs_resynthesis leave a
  synthesized appearance looking like the stored one (object_signals
  below prints them for every fixture).
- A drawing comparison works, batched per file: copy A is the document
  with every page's /Rotate removed and every annotation's /F and /OC
  cleared (so rotation, NoRotate, hidden, NoView and optional content do
  not hide the comparison); copy B is copy A with every annotation that
  has an /AP replaced by a stand-in -- a /Square with the same /Rect, /AP
  and /AS as written (references kept: MuPDF resolves them), which MuPDF
  draws from /AP whenever /AP /N names a stream. Per annotation, MuPDF's
  drawing in the two copies (pdf_run_annot) is compared by its trace
  (fz_new_trace_device: paths, glyphs and their text, image sizes and
  transforms) and by its pixels (rendered alone at 4x: the trace lacks
  image samples). Readings: NOTHING (no mark -- no path filled or
  stroked, no glyph, image or shading; a clip alone marks nothing),
  STORED (equal trace and pixels), SYNTHESIZED (otherwise, and any
  annotation without /AP that marks).
- Not exact: two drawings equal in trace and in 4x pixels are read as
  the same, so content that differs only below that resolution (an
  image's samples, a glyph program) is not told apart. ADR 0011 item 5
  already flags a synthesized unit whose rich text could draw an image.
- Link and Popup annotations are not listed by pdf_first_annot or
  pdf_first_widget (MuPDF draws neither); every other subtype is, a
  duplicated /Annots reference twice, annotations before widgets: the
  gate compares the lists as multisets by object number, without Link
  and Popup.

The fixtures cover ADR 0011 item 5's conditions; ``expected`` is the
ADR's classification (True: SYNTHESIZED). The gate matches ours
SYNTHESIZED to SYNTHESIZED or NOTHING, and ours not synthesized to STORED
or NOTHING. Fabricated fixtures only.

    PYTHONPATH=eval:. python eval/spikes/annotation_appearance.py
"""

from __future__ import annotations

import time

import pymupdf
from scorecard.pdfgen import Writer

M = pymupdf.mupdf
RED = b"1 0 0 rg 0 0 50 20 re f"
RECT = b"/Rect [10 10 60 30]"
FORM = b" /Subtype /Form /BBox [0 0 50 20] /Resources << >>"
FIELDS = b" /AcroForm << /Fields [4 0 R] >>"
NEED = b" /AcroForm << /Fields [4 0 R] /NeedAppearances true >>"


def build(annot: bytes, extra: dict[int, bytes] | None = None, acroform: bytes = b"",
          page: bytes = b"", direct: bool = False) -> bytes:
    """One page; annotation 4 (or *annot* written directly in /Annots);
    object 5 the stored red box; *extra* objects."""
    w = Writer()
    t: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    t[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R%s >>" % acroform), 0)
    t[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    annots = b"[%s]" % annot if direct else b"[4 0 R]"
    t[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                        b" /Resources << >> /Annots %s%s >>" % (annots, page)), 0)
    if not direct:
        t[4] = (1, w.obj(4, annot), 0)
    for n, body in (extra or {}).items():
        t[n] = (1, w.obj(n, body), 0)
    t[5] = (1, w.stream(5, FORM, RED), 0)
    w.epilogue(w.table(t, b"/Size %d /Root 1 0 R" % (max(t) + 1)))
    return bytes(w.out)


AP = b"/AP << /N 5 0 R >>"
TX = b"/Type /Annot /Subtype /Widget /FT /Tx /T (t) /V (SSN 123-45-6789) /DA (/Helv 9 Tf 0 g)"
SIG = b"/Type /Annot /Subtype /Widget /FT /Sig /T (s)"
SQ = b"/Type /Annot /Subtype /Square"
CASES: dict[str, tuple[bytes, bool]] = {
    # Stored /AP /N a stream, or a dictionary whose /AS (resolved) names an
    # entry: drawn as stored -- an entry that is not a stream draws nothing.
    "square-ap": (build(b"<< %s %s %s >>" % (SQ, RECT, AP)), False),
    "square-ap-state": (build(b"<< %s %s /AS /On /AP << /N << /On 5 0 R >> >> >>"
                              % (SQ, RECT)), False),
    "square-as-indirect": (build(b"<< %s %s /AS 6 0 R /AP << /N << /On 5 0 R >> >> >>"
                                 % (SQ, RECT), {6: b"/On"}), False),
    "square-as-not-a-stream": (build(b"<< %s %s /C [0 0 1] /AS /On /AP << /N << /On 3 >> >> >>"
                                     % (SQ, RECT)), False),
    "square-direct": (build(b"<< %s %s %s >>" % (SQ, RECT, AP), direct=True), False),
    "text-rotated-page": (build(b"<< /Type /Annot /Subtype /Text %s %s >>" % (RECT, AP),
                                page=b" /Rotate 90"), False),
    "square-hidden": (build(b"<< %s %s /F 2 %s >>" % (SQ, RECT, AP)), False),
    "square-noview": (build(b"<< %s %s /F 32 %s >>" % (SQ, RECT, AP)), False),
    "square-oc-off": (build(b"<< %s %s /OC 6 0 R %s >>" % (SQ, RECT, AP),
                            {6: b"<< /Type /OCG /Name (x) >>"},
                            b" /OCProperties << /OCGs [6 0 R] /D << /OFF [6 0 R] >> >>"), False),
    # No usable stored /AP: synthesized.
    "square-no-ap": (build(b"<< %s %s /C [0 0 1] >>" % (SQ, RECT)), True),
    "square-ap-empty": (build(b"<< %s %s /C [0 0 1] /AP << >> >>" % (SQ, RECT)), True),
    "square-ap-d-only": (build(b"<< %s %s /C [0 0 1] /AP << /D 5 0 R >> >>" % (SQ, RECT)), True),
    "square-ap-n-number": (build(b"<< %s %s /C [0 0 1] /AP << /N 7 >> >>" % (SQ, RECT)), True),
    "square-ap-state-no-as": (build(b"<< %s %s /C [0 0 1] /AP << /N << /On 5 0 R >> >> >>"
                                    % (SQ, RECT)), True),
    "square-as-missing-entry": (build(b"<< %s %s /C [0 0 1] /AS /Off"
                                      b" /AP << /N << /On 5 0 R >> >> >>" % (SQ, RECT)), True),
    "square-no-ap-hidden": (build(b"<< %s %s /F 2 /C [0 0 1] >>" % (SQ, RECT)), True),
    "freetext-no-ap": (build(b"<< /Type /Annot /Subtype /FreeText %s /Contents (SSN 123-45-6789)"
                             b" /DA (/Helv 9 Tf 0 g) >>" % RECT), True),
    "freetext-rc": (build(b"<< /Type /Annot /Subtype /FreeText %s /Contents (x)"
                          b" /DA (/Helv 9 Tf 0 g) /RC (<body><p>SSN 123-45-6789</p></body>) >>"
                          % RECT), True),
    # Widgets.
    "text-widget-ap": (build(b"<< %s %s %s >>" % (TX, RECT, AP), acroform=FIELDS), False),
    "text-widget-no-ap": (build(b"<< %s %s >>" % (TX, RECT), acroform=FIELDS), True),
    "text-widget-need": (build(b"<< %s %s %s >>" % (TX, RECT, AP), acroform=NEED), True),
    "text-widget-rv-need": (build(b"<< %s /RV (<body><p>SSN 123-45-6789</p></body>) %s %s >>"
                                  % (TX, RECT, AP), acroform=NEED), True),
    "text-widget-inherited-need": (build(b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R %s %s"
                                         b" /DA (/Helv 9 Tf 0 g) >>" % (RECT, AP),
                                         {6: b"<< /FT /Tx /T (t) /V (SSN 123-45-6789)"
                                             b" /Kids [4 0 R] >>"},
                                         b" /AcroForm << /Fields [6 0 R] /NeedAppearances true >>"),
                                   True),
    "checkbox-need": (build(b"<< /Type /Annot /Subtype /Widget /FT /Btn /T (c) /V /On /AS /On %s"
                            b" /AP << /N << /On 5 0 R >> >> >>" % RECT, acroform=NEED), True),
    "pushbutton-need": (build(b"<< /Type /Annot /Subtype /Widget /FT /Btn /Ff 65536 /T (b) %s %s"
                              b" /MK << /CA (Go) >> >>" % (RECT, AP), acroform=NEED), True),
    "combo-need": (build(b"<< /Type /Annot /Subtype /Widget /FT /Ch /Ff 131072 /T (c) /V (a)"
                         b" /Opt [(a) (b)] /DA (/Helv 9 Tf 0 g) %s %s >>" % (RECT, AP),
                         acroform=NEED), True),
    # Signatures: synthesized exactly when unsigned, whatever /AP says.
    "sig-unsigned": (build(b"<< %s %s %s >>" % (SIG, RECT, AP), acroform=FIELDS), True),
    "sig-signed": (build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                         {6: b"<< /Type /Sig /Filter /Adobe.PPKLite >>"}, FIELDS), False),
    "sig-doc-timestamp": (build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                                {6: b"<< /Type /DocTimeStamp /Filter /Adobe.PPKLite >>"}, FIELDS),
                          True),
    "sig-signed-inherited": (build(b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R %s %s >>"
                                   % (RECT, AP),
                                   {6: b"<< /FT /Sig /T (s) /V 7 0 R /Kids [4 0 R] >>",
                                    7: b"<< /Type /Sig /Filter /Adobe.PPKLite >>"},
                                   b" /AcroForm << /Fields [6 0 R] >>"), False),
    "sig-unsigned-inherited": (build(b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R %s %s >>"
                                     % (RECT, AP), {6: b"<< /FT /Sig /T (s) /Kids [4 0 R] >>"},
                                     b" /AcroForm << /Fields [6 0 R] >>"), True),
    "sig-unsigned-hidden": (build(b"<< %s /F 2 %s %s >>" % (SIG, RECT, AP), acroform=FIELDS),
                            True),
}
# Not listed by MuPDF (it draws neither): the gate leaves them out of both lists.
UNLISTED = {
    "link": build(b"<< /Type /Annot /Subtype /Link %s /Border [0 0 0] >>" % RECT),
    "popup": build(b"<< /Type /Annot /Subtype /Popup %s >>" % RECT),
}

# Trace elements that mark the page; a clip alone marks nothing.
MARKS = (b"<fill_path", b"<stroke_path", b"<fill_text", b"<stroke_text", b"<ignore_text",
         b"<fill_image", b"<fill_shade")


def _name(key: str) -> object:
    return M.pdf_new_name(key)


def listed(page: pymupdf.Page) -> list[object]:
    """MuPDF's annotations, then its widgets (pdf_first_annot and
    pdf_first_widget): what it draws."""
    found = []
    pp = M.pdf_page_from_fz_page(page.this)
    for first, step in ((M.pdf_first_annot, M.pdf_next_annot),
                        (M.pdf_first_widget, M.pdf_next_widget)):
        a = first(pp)
        while a.m_internal:
            found.append(a)
            a = step(a)
    return found


def trace(annot: object) -> bytes:
    buf = M.FzBuffer(1024)
    out = M.FzOutput(buf)
    dev = M.fz_new_trace_device(out)
    M.pdf_run_annot(annot, dev, M.FzMatrix(), M.FzCookie())
    M.fz_close_device(dev)
    M.fz_close_output(out)
    return bytes(M.fz_buffer_extract(buf))


def pixels(annot: object) -> bytes:
    """The annotation drawn alone, 4x, as raw RGB samples."""
    dl = M.pdf_new_display_list_from_annot(annot)
    pix = M.fz_new_pixmap_from_display_list(dl, M.fz_scale(4, 4), M.fz_device_rgb(), 0)
    return bytes(pymupdf.Pixmap(pix).samples)


def copies(data: bytes) -> tuple[pymupdf.Document, pymupdf.Document]:
    """Copy A (/Rotate, /F and /OC cleared) and copy B (copy A with each
    annotation that has /AP replaced by a stand-in), once per file."""
    pair = []
    for stand_in in (False, True):
        doc = pymupdf.open(stream=data, filetype="pdf")
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
                ap = M.pdf_dict_get(annot, _name("AP"))
                if not stand_in or not ap.m_internal or M.pdf_is_null(ap):
                    continue
                square = M.pdf_new_dict(pdf, 6)
                M.pdf_dict_put(square, _name("Type"), _name("Annot"))
                M.pdf_dict_put(square, _name("Subtype"), _name("Square"))
                for key in ("Rect", "AP", "AS"):
                    value = M.pdf_dict_get(annot, _name(key))  # as written: references kept
                    if value.m_internal and not M.pdf_is_null(value):
                        M.pdf_dict_put(square, _name(key), value)
                if M.pdf_is_indirect(annot):
                    M.pdf_update_object(pdf, M.pdf_to_num(annot), square)
                else:
                    M.pdf_array_put(annots, i, square)
        pair.append(doc)
    return pair[0], pair[1]


def readings(data: bytes) -> list[str]:
    """MuPDF's reading of each listed annotation on page 1, in its order."""
    real, stand_ins = copies(data)
    page_a, page_b = real[0], stand_ins[0]  # kept alive: annotations are bound to pages
    found = []
    for a, b in zip(listed(page_a), listed(page_b), strict=True):
        drawn = trace(a)
        if not any(mark in drawn for mark in MARKS):
            found.append("NOTHING")
            continue
        ap = M.pdf_dict_get(M.pdf_annot_obj(a), _name("AP"))
        if not ap.m_internal or M.pdf_is_null(ap):
            found.append("SYNTHESIZED")
            continue
        same = drawn == trace(b) and pixels(a) == pixels(b)
        found.append("STORED" if same else "SYNTHESIZED")
    return found


def object_signals(data: bytes) -> tuple[int, int, int]:
    """For the first listed annotation: pdf_annot_ap's object number
    before drawing, and after drawing and pdf_update_annot; and
    pdf_annot_needs_resynthesis -- none tells synthesized from stored."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    page = doc[0]
    annot = listed(page)[0]
    before = M.pdf_to_num(M.pdf_annot_ap(annot))
    page.get_pixmap()
    M.pdf_update_annot(annot)
    return before, M.pdf_to_num(M.pdf_annot_ap(annot)), M.pdf_annot_needs_resynthesis(annot)


def _many(n: int) -> bytes:
    annots = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    w = Writer()
    t: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    t[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), 0)
    t[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    t[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                        b" /Resources << >> /Annots [%s] >>" % annots), 0)
    t[5] = (1, w.stream(5, FORM, RED), 0)
    for i in range(n):
        t[10 + i] = (1, w.obj(10 + i, b"<< %s %s %s >>" % (SQ, RECT, AP)), 0)
    w.epilogue(w.table(t, b"/Size %d /Root 1 0 R" % (max(t) + 1)))
    return bytes(w.out)


def main() -> None:
    disagreements = 0
    for name, (data, expected) in CASES.items():
        reading = readings(data)[0]
        agrees = reading == "NOTHING" or (reading == "SYNTHESIZED") == expected
        disagreements += not agrees
        before, after, needs = object_signals(data)
        print(f"{name:27} mupdf={reading:11} adr-synthesized={expected!s:5}"
              f" {'agrees' if agrees else 'DISAGREES'}"
              f"{' (NOTHING: agrees with both)' if reading == 'NOTHING' else ''}"
              f"  [ap-before/after={before}/{after} needs-resynthesis={needs}]")
    for name, data in UNLISTED.items():
        doc = pymupdf.open(stream=data, filetype="pdf")
        page = doc[0]
        print(f"{name:27} listed by MuPDF: {len(listed(page))}")
    for n in (100, 400, 1600):  # two copies per file, whatever the count
        started = time.perf_counter()
        found = readings(_many(n))
        print(f"{n:5} annotations: {time.perf_counter() - started:.2f} s,"
              f" all STORED: {set(found) == {'STORED'}}")
    print("disagreements:", disagreements)


if __name__ == "__main__":
    main()
