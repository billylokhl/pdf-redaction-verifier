"""One-location probe documents (plan §1.0): each holds a secret in
exactly one place, so running the redactor on it shows what the tool
does to that location alone. Their results replace the plan's "best
knowledge" column before the full set is trusted."""

from __future__ import annotations

import random
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import post, producers
from .build import Built, Field, SplitSpec, TextSpec, chrome, esc, form_pdf, scan_pages
from .persona import BY_ID
from .truth import Instance

V = {k: s.value for k, s in BY_ID.items()}
FILLER = "<p>This page is a probe. It carries one fabricated value in one place.</p>"


def _page(work: Path, name: str, body: str, *, title: str = "Probe") -> Path:
    return chrome(work, name, title, [f"<h1>{esc(title)}</h1>{FILLER}{body}"])


def t1(work: Path, rng: random.Random) -> Built:
    pdf = _page(work, "t1", f"<p>SSN {V['SSN1']}</p>")
    return Built(pdf, "chrome", "Probe", text=[TextSpec("SSN1", V["SSN1"], "T1", 1)])


def t2(work: Path, rng: random.Random) -> Built:
    pdf = _page(work, "t2", "<p>Spouse SSN 666382914</p>")
    return Built(pdf, "chrome", "Probe", text=[TextSpec("SSN2", "666382914", "T2", 1)])


def t3a(work: Path, rng: random.Random) -> Built:
    body = ("<p>Our records list the account holder as Jordan Q.<br>"
            "Testperson for this file.</p>")
    pdf = _page(work, "t3a", body)
    return Built(pdf, "chrome", "Probe",
                 split=[SplitSpec("NAME1", V["NAME1"], "T3a", ["Jordan Q.", "Testperson"])])


def t3b(work: Path, rng: random.Random) -> Built:
    body = ("<table style='table-layout:fixed'><tr><td style='width:50%'><p>Owner</p>"
            "<p>Beneficiary: Avery R.</p></td><td><p>Samplename, spouse</p><p>Since 2019</p>"
            "</td></tr></table>")
    pdf = _page(work, "t3b", body)
    return Built(pdf, "chrome", "Probe",
                 split=[SplitSpec("NAME2", V["NAME2"], "T3b", ["Avery R.", "Samplename"])])


def t3c(work: Path, rng: random.Random) -> Built:
    pages = [f"<h1>Probe</h1>{FILLER}<p style='margin-top:560pt'>Account holder: Jordan Q.</p>",
             "<p>Testperson, continued from the previous page.</p>"]
    pdf = chrome(work, "t3c", "Probe", pages)
    return Built(pdf, "chrome", "Probe",
                 split=[SplitSpec("NAME1", V["NAME1"], "T3c", ["Jordan Q.", "Testperson"])])


def _form(work: Path, name: str, field: Field) -> Built:
    out = work / f"{name}.pdf"
    inst = form_pdf(out, title="Probe", heading=["Probe form (fabricated)"],
                    static=[(0, 40, 200, "This page is a probe.", 9)], fields=[field], n_pages=1)
    return Built(out, "reportlab", "Probe", other=inst)


def f1(work: Path, rng: random.Random) -> Built:
    return _form(work, "f1", Field(0, "ssn", "Social security number", 40, 90, 200,
                                   value=V["SSN1"], secret="SSN1"))


def f2(work: Path, rng: random.Random) -> Built:
    return _form(work, "f2", Field(0, "ssn", "SSN, one character per box", 40, 90, 242, h=20,
                                   kind="comb", value=V["SSN1"], secret="SSN1", loc="F2"))


def _annotated(work: Path, name: str, kind: str, box: list[float], text: str,
               inst: Instance, *, body: str = "", uri: str | None = None) -> Built:
    src = _page(work, f"{name}-src", body or "<p>Contact us</p>")
    pdf = post.edit(src)
    post.add_annotation(pdf, 0, kind, box, text, uri=uri)
    out = work / f"{name}.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", "Probe", other=[inst])


def a1(work: Path, rng: random.Random) -> Built:
    box = [380.0, 300.0, 560.0, 320.0]
    return _annotated(work, "a1", "FreeText", box, f"Acct {V['ACCT']}",
                      Instance("ACCT", V["ACCT"], "A1", "string", 0, box, note="FreeText"))


def a1x(work: Path, rng: random.Random) -> Built:
    # Over the page's own (redacted) SSN line; the FreeText says something else.
    from . import locate
    src_body = f"<p>SSN {V['SSN1']}</p>"
    tmp = _page(work, "a1x-locate", src_body)
    box = locate.find(locate.page_lines(tmp), V["SSN1"])[0][1]
    built = _annotated(work, "a1x", "FreeText", [box[0], box[1], box[0] + 180, box[3]],
                       f"Acct {V['ACCT']}",
                       Instance("ACCT", V["ACCT"], "A1x", "string", 0, box,
                                note="FreeText over redacted text"), body=src_body)
    built.text.append(TextSpec("SSN1", V["SSN1"], "T1", 1))
    return built


def a2(work: Path, rng: random.Random) -> Built:
    box = [540.0, 200.0, 560.0, 220.0]
    return _annotated(work, "a2", "Text", box, f"Spouse SSN {V['SSN2']}",
                      Instance("SSN2", V["SSN2"], "A2", "string", 0, box, note="sticky note"))


def a3(work: Path, rng: random.Random) -> Built:
    from . import locate
    body = f"<p>Holder {esc(V['NAME1'])}</p>"
    tmp = _page(work, "a3-locate", body)
    box = locate.find(locate.page_lines(tmp), V["NAME1"])[0][1]
    built = _annotated(work, "a3", "Highlight", box, f"Check DOB {V['DOB']}",
                       Instance("DOB", V["DOB"], "A3", "string", 0, box,
                                note="highlight /Contents"), body=body)
    built.text.append(TextSpec("NAME1", V["NAME1"], "T1", 1))
    return built


def a4(work: Path, rng: random.Random) -> Built:
    from . import locate
    tmp = _page(work, "a4-locate", "<p>Contact us</p>")
    box = locate.find(locate.page_lines(tmp), "Contact us")[0][1]
    return _annotated(work, "a4", "Link", box, "mailto",
                      Instance("EMAIL", V["EMAIL"], "A4", "string", 0, box, note="link URI"),
                      uri=f"mailto:{V['EMAIL']}")


def _meta(work: Path, name: str, fields: dict[str, str], inst: list[Instance],
          xmp: bool = False) -> Built:
    src = _page(work, f"{name}-src", "<p>Nothing secret on the page.</p>")
    pdf = post.edit(src)
    post.set_info(pdf, fields)
    if xmp:
        post.set_xmp(pdf, creator=V["NAME1"], title="Probe", keywords=V["ACCT"])
    out = work / f"{name}.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", fields.get("Title", "Probe"), other=inst)


def m1(work: Path, rng: random.Random) -> Built:
    return _meta(work, "m1", {"Title": "Probe", "Author": V["NAME1"]},
                 [Instance("NAME1", V["NAME1"], "M1", "string", note="/Author")])


def m2(work: Path, rng: random.Random) -> Built:
    return _meta(work, "m2", {"Title": "Probe", "TaxpayerSSN": V["SSN1"]},
                 [Instance("SSN1", V["SSN1"], "M2", "string", note="/TaxpayerSSN")])


def m3(work: Path, rng: random.Random) -> Built:
    return _meta(work, "m3", {"Title": "Probe"},
                 [Instance("NAME1", V["NAME1"], "M3", "xmp", note="dc:creator"),
                  Instance("ACCT", V["ACCT"], "M3", "xmp", note="pdf:Keywords")], xmp=True)


def o1(work: Path, rng: random.Random) -> Built:
    src = _page(work, "o1-src", "<p>Nothing secret on the page.</p>")
    pdf = post.edit(src)
    post.add_outline(pdf, [(f"{V['NAME1']} - return", 0)])
    out = work / "o1.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", "Probe",
                 other=[Instance("NAME1", V["NAME1"], "O1", "string", note="outline title")])


def e1(work: Path, rng: random.Random) -> Built:
    src = _page(work, "e1-src", "<p>Nothing secret on the page.</p>")
    pdf = post.edit(src)
    post.attach(pdf, "data.csv", f"ssn\n{V['SSN1']}\n".encode(), "Import data")
    out = work / "e1.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", "Probe",
                 other=[Instance("SSN1", V["SSN1"], "E1", "embedded", note="CSV")])


def _scan(work: Path, name: str, rng: random.Random, loc: str) -> Built:
    src = _page(work, f"{name}-src", f"<p style='font-size:14pt'>SSN {V['SSN1']}</p>")
    jpgs, inst = scan_pages(src, rng, work, [TextSpec("SSN1", V["SSN1"], loc, 1)], loc)
    out = work / f"{name}.pdf"
    if loc == "S1":
        producers.image_pages_pdf(jpgs, out, title="Probe")
        return Built(out, "reportlab-scan", "Probe", other=inst)
    raw = work / f"{name}-tess.pdf"
    producers.tesseract_pdf(jpgs, raw)
    pdf = post.edit(raw)
    post.set_info(pdf, {"Title": "Probe"})
    post.save(pdf, out)
    pdf.close()
    return Built(out, "tesseract", "Probe", other=inst, ocr_text_layer=True)


def s1(work: Path, rng: random.Random) -> Built:
    return _scan(work, "s1", rng, "S1")


def s2(work: Path, rng: random.Random) -> Built:
    return _scan(work, "s2", rng, "S2")


def i1(work: Path, rng: random.Random) -> Built:
    from .templates import d07_check_images
    return d07_check_images(work, rng)


def p1(work: Path, rng: random.Random) -> Built:
    src = _page(work, "p1-src", f"<p style='font-size:22pt'>SSN {V['SSN1']}</p>")
    thumbs = producers.rasterise(src, 50, Path(tempfile.mkdtemp(prefix="rs-thumb-", dir=work)))
    pdf = post.edit(src)
    post.add_thumbnail(pdf, 0, thumbs[0])
    out = work / "p1.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", "Probe",
                 text=[TextSpec("SSN1", V["SSN1"], "T1", 1)],
                 other=[Instance("SSN1", V["SSN1"], "P1", "pixels", 0, note="50 dpi thumbnail")])


def q1(work: Path, rng: random.Random) -> Built:
    raw = work / "q1-quartz.pdf"
    producers.quartz_text_pdf(f"Probe letter\n\nSSN {V['SSN1']}\n", raw)
    pdf = post.edit(raw)
    post.set_info(pdf, {"Title": "Probe"})
    out = work / "q1.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "quartz+pikepdf", "Probe", text=[TextSpec("SSN1", V["SSN1"], "Q1", 1)])


PROBES: dict[str, Callable[[Path, random.Random], Built]] = {
    "T1": t1, "T2": t2, "T3a": t3a, "T3b": t3b, "T3c": t3c, "F1": f1, "F2": f2,
    "A1": a1, "A1x": a1x, "A2": a2, "A3": a3, "A4": a4, "M1": m1, "M2": m2, "M3": m3,
    "O1": o1, "E1": e1, "S1": s1, "S2": s2, "I1": i1, "P1": p1, "Q1": q1,
}
