"""The 20 document templates (plan §1.4), mirroring the owner's document
types: tax forms, bank and card statements, scans, multi-page fillable
forms, annotations and metadata. All names, numbers and institutions are
fabricated. In the garbage-collected output each leak template carries
one surviving-leak location kind, so a file-level verdict cannot hide a
location-level miss; the "clean" templates carry only page text in the
configured format plus metadata.

A template is ``build(work, rng) -> Built``; ``generate.py`` turns that
into a PDF and a truth manifest.
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import persona, post, producers
from .build import (Built, Field, SplitSpec, TextSpec, chrome, esc, form_pdf,
                    scan_pages)
from .persona import BY_ID
from .truth import Instance

V = {k: s.value for k, s in BY_ID.items()}
CITY = "Springfield, ZZ 00000"


def _safe(make: Callable[[], str]) -> str:
    """Draw filler until it holds no residue-shaped piece of a secret
    (persona.residue_in): a chance "4857" in an amount would read as an
    EIN left behind."""
    for _ in range(1000):
        text = make()
        if not persona.residue_in(text):
            return text
    raise RuntimeError("could not draw filler free of secret residue")


def _money(rng: random.Random, lo: float, hi: float) -> str:
    return _safe(lambda: f"{rng.uniform(lo, hi):,.2f}")


def _date(rng: random.Random) -> str:
    return _safe(lambda: f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}")


def _transactions(rng: random.Random, n: int, extra: list[str] | None = None) -> str:
    memos = ["GROCERY MARKET #0042", "CITY WATER UTILITY", "PAYROLL DEPOSIT", "COFFEE SHOP",
             "ONLINE TRANSFER", "PHARMACY", "FUEL STATION", "BOOKSTORE", "INSURANCE PREMIUM",
             "ATM WITHDRAWAL", "PARKING", "HARDWARE STORE"]
    rows = [(_date(rng), rng.choice(memos), _money(rng, 4, 900)) for _ in range(n)]
    for memo in extra or []:
        rows.insert(rng.randint(0, len(rows)), (_date(rng), memo, _money(rng, 20, 400)))
    body = "".join(f"<tr><td>{d}</td><td>{esc(m)}</td><td class='num'>{a}</td></tr>"
                   for d, m, a in rows)
    return f"<table><tr><th>Date</th><th>Description</th><th class='num'>Amount</th></tr>{body}</table>"


def _envelope(name: str = V["NAME1"]) -> str:
    return f"<div class='envelope'>{esc(name)}<br>{esc(V['ADDR'])}<br>{CITY}</div>"


# ── Tax forms ──────────────────────────────────────────────────────────

def d01_w2(work: Path, rng: random.Random) -> Built:
    title = "Wage and tax statement 2025"
    page = (
        "<h1>Form W-2 (fabricated sample) &mdash; Wage and Tax Statement 2025</h1>"
        "<div class='grid'>"
        f"<div><span class='label'>a Employee's social security number</span>{V['SSN1']}</div>"
        f"<div><span class='label'>b Employer identification number</span>{V['EIN']}</div>"
        "<div><span class='label'>c Employer's name and address</span>Anytown Payroll Services LLC"
        "<br>PO Box 0000, Anytown, ZZ 00000</div>"
        f"<div><span class='label'>1 Wages, tips, other compensation</span>{_money(rng, 40000, 90000)}</div>"
        f"<div><span class='label'>e Employee's name</span>{esc(V['NAME1'])}</div>"
        f"<div><span class='label'>2 Federal income tax withheld</span>{_money(rng, 3000, 9000)}</div>"
        f"<div><span class='label'>f Employee's address</span>{esc(V['ADDR'])}<br>{CITY}</div>"
        f"<div><span class='label'>3 Social security wages</span>{_money(rng, 40000, 90000)}</div>"
        "</div><p class='small'>Copy B &mdash; to be filed with the employee's federal tax return.</p>")
    return Built(chrome(work, "d01", title, [page]), "chrome", title,
                 text=[TextSpec("SSN1", V["SSN1"], "T1", 1), TextSpec("EIN", V["EIN"], "T1", 1),
                       TextSpec("NAME1", V["NAME1"], "T1", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1)])


def d02_1099int(work: Path, rng: random.Random) -> Built:
    title = "Interest income 2025"
    page = (
        "<h1>Form 1099-INT (fabricated sample) &mdash; Interest Income 2025</h1>"
        "<div class='grid'>"
        "<div><span class='label'>PAYER'S name</span>Anytown Federal Bank<br>PO Box 0000, "
        "Anytown, ZZ 00000</div>"
        f"<div><span class='label'>1 Interest income</span>{_money(rng, 10, 900)}</div>"
        f"<div><span class='label'>RECIPIENT'S TIN</span>{V['SSN1']}</div>"
        f"<div><span class='label'>4 Federal income tax withheld</span>0.00</div>"
        f"<div><span class='label'>RECIPIENT'S name</span>{esc(V['NAME1'])}</div>"
        f"<div><span class='label'>Account number</span>{V['ACCT']}</div>"
        f"<div><span class='label'>Street address</span>{esc(V['ADDR'])}</div>"
        f"<div><span class='label'>City</span>{CITY}</div></div>")
    return Built(chrome(work, "d02", title, [page]), "chrome", title,
                 text=[TextSpec(k, V[k], "T1", 1) for k in ("SSN1", "NAME1", "ACCT", "ADDR")])


def _return_fields(page: int, top: float) -> list[Field]:
    return [
        Field(page, f"name_{page}", "Your first name, initial and last name", 40, top, 300,
              value=V["NAME1"], secret="NAME1"),
        Field(page, f"ssn_{page}", "Your social security number", 380, top, 160,
              value=V["SSN1"], secret="SSN1"),
    ]


def d03_1040_fillable(work: Path, rng: random.Random) -> Built:
    title = "Individual income tax return 2025"
    fields = _return_fields(0, 90) + [
        Field(0, "spouse", "Spouse's first name, initial and last name", 40, 130, 300,
              value=V["NAME2"], secret="NAME2"),
        Field(0, "addr", "Home address (number and street)", 40, 170, 300,
              value=V["ADDR"], secret="ADDR"),
        Field(0, "city", "City, state and ZIP", 360, 170, 180, value=CITY),
        Field(0, "status", "Filing status", 40, 214, 200, kind="choice",
              value="Married filing jointly",
              options=("Single", "Married filing jointly", "Head of household")),
        Field(0, "wages", "1a Wages", 380, 260, 160, value=_money(rng, 40000, 90000)),
    ] + [
        Field(1, "name_1", "Name(s) shown on return", 40, 90, 300, value=V["NAME1"],
              secret="NAME1"),
        Field(1, "sign", "I declare under penalties of perjury (check)", 40, 140, 14,
              kind="checkbox", value="Yes"),
    ]
    out = work / "d03.pdf"
    instances = form_pdf(out, title=title,
                         heading=["Form 1040-style return (fabricated sample) 2025",
                                  "Form 1040-style return, page 2"],
                         static=[(0, 40, 300, "Income", 10), (1, 40, 200, "Sign here", 10)],
                         fields=fields, n_pages=2)
    return Built(out, "reportlab", title, other=instances)


def d04_1040_printed(work: Path, rng: random.Random) -> Built:
    title = "Individual income tax return 2025 (print)"
    head = f"<div class='runhead'>{esc(V['NAME1'])} &nbsp;&nbsp; {V['SSN1']}</div>"
    deps = (f"<table><tr><th>Spouse</th><th>SSN</th><th>Relationship</th></tr>"
            f"<tr><td>{esc(V['NAME2'])}</td><td>{V['SSN2']}</td><td>Spouse</td></tr></table>")
    pages = [
        head + "<h1>Form 1040-style return (fabricated sample) 2025</h1>"
        f"<p>Filing status: Married filing jointly</p>{deps}"
        f"<p>Home address: {esc(V['ADDR'])}, {CITY}</p>",
        head + "<h2>Income</h2>" + "".join(
            f"<p>Line {i}: {_money(rng, 0, 20000)}</p>" for i in range(1, 9)),
        head + "<h2>Tax and credits</h2>" + "".join(
            f"<p>Line {i}: {_money(rng, 0, 9000)}</p>" for i in range(16, 24)),
    ]
    return Built(chrome(work, "d04", title, pages), "chrome", title,
                 text=[TextSpec("NAME1", V["NAME1"], "T1", 3), TextSpec("SSN1", V["SSN1"], "T1", 3),
                       TextSpec("NAME2", V["NAME2"], "T1", 1), TextSpec("SSN2", V["SSN2"], "T1", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1)])


def d05_state(work: Path, rng: random.Random) -> Built:
    title = "State resident return 2025"
    pages = [
        "<h1>State resident income tax return (fabricated sample) 2025</h1>"
        f"<p>Taxpayer SSN: {V['SSN1']}</p><p>Filing status: joint</p>"
        + "".join(f"<p>Line {i}: {_money(rng, 0, 9000)}</p>" for i in range(1, 7)),
        "<h2>Spouse information</h2>"
        "<p>Spouse SSN as entered by the return software: 666382914</p>"
        "<p class='small'>Data line: ST25 R 666382914 J 0001</p>",
    ]
    return Built(chrome(work, "d05", title, pages), "chrome", title,
                 text=[TextSpec("SSN1", V["SSN1"], "T1", 1),
                       TextSpec("SSN2", "666382914", "T2", 2, note="undashed")])


# ── Statements ─────────────────────────────────────────────────────────

def _statement_head(rng: random.Random, page: int, pages: int) -> str:
    return (f"<div class='runhead'>Anytown Federal Bank &mdash; Account {V['ACCT']} "
            f"<span class='small'>&nbsp; page {page} of {pages}</span></div>")


def d06_checking(work: Path, rng: random.Random) -> Built:
    title = "Checking account statement"
    pages = [
        _statement_head(rng, 1, 3) + _envelope()
        + "<h2>Summary</h2>" + f"<p>Beginning balance {_money(rng, 900, 9000)}</p>"
        + _transactions(rng, 8, [f"ZELLE TO {V['NAME2']}"]),
        _statement_head(rng, 2, 3) + "<h2>Transactions (continued)</h2>" + _transactions(rng, 14),
        _statement_head(rng, 3, 3) + "<h2>Transactions (continued)</h2>" + _transactions(rng, 10),
    ]
    return Built(chrome(work, "d06", title, pages), "chrome", title,
                 text=[TextSpec("ACCT", V["ACCT"], "T1", 3), TextSpec("NAME1", V["NAME1"], "T1", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1),
                       TextSpec("NAME2", V["NAME2"], "T1", 1)])


def _check_image(rng: random.Random, number: int) -> tuple[bytes, list[tuple[str, tuple[int, int, int, int]]]]:
    """A cheque image (PNG bytes) and the pixel box of each secret on it."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (900, 380), 246)
    d = ImageDraw.Draw(img)
    font: Any
    small: Any
    try:
        font = ImageFont.truetype("Helvetica", 26)
        small = ImageFont.truetype("Helvetica", 20)
    except OSError:
        font = small = ImageFont.load_default(size=26)
    d.rectangle([4, 4, 895, 375], outline=90, width=3)
    boxes = []
    for secret, text, xy, f in (("NAME1", V["NAME1"], (30, 26), font),
                                ("ADDR", V["ADDR"], (30, 62), small)):
        d.text(xy, text, fill=20, font=f)
        boxes.append((secret, d.textbbox(xy, text, font=f)))
    d.text((640, 26), f"No. {number}", fill=20, font=small)
    d.text((30, 150), "PAY TO THE ORDER OF  City Water Utility", fill=20, font=small)
    d.text((620, 150), f"$ {_money(rng, 20, 300)}", fill=20, font=small)
    micr = f"C{V['ROUT']}C  A{V['ACCT']}A  {number}"
    d.text((60, 310), micr, fill=0, font=font)
    rout_x = 60 + d.textlength("C", font=font)
    acct_x = 60 + d.textlength(f"C{V['ROUT']}C  A", font=font)
    boxes.append(("ROUT", d.textbbox((rout_x, 310), V["ROUT"], font=font)))
    boxes.append(("ACCT", d.textbbox((acct_x, 310), V["ACCT"], font=font)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), [(s, (int(b[0]), int(b[1]), int(b[2]), int(b[3]))) for s, b in boxes]


def d07_check_images(work: Path, rng: random.Random) -> Built:
    import base64

    from . import locate

    title = "Checking statement with check images"
    imgs, drawn = [], []
    for i in range(2):
        png, boxes = _check_image(rng, 1041 + i)
        drawn.append(boxes)
        imgs.append(f"<p><img src='data:image/png;base64,{base64.b64encode(png).decode()}' "
                    "style='width:450pt; height:190pt'></p>")
    pages = [
        _statement_head(rng, 1, 2) + _envelope() + _transactions(rng, 8),
        "<h2>Check images</h2>" + "".join(imgs),
    ]
    pdf = chrome(work, "d07", title, pages)
    # Where Chrome placed each image (pdfminer), then each secret's box
    # inside it: the image is 900 x 380 px drawn at 450 x 190 pt.
    placed = locate.image_boxes(pdf)[1]
    if len(placed) != 2:
        raise RuntimeError(f"d07: expected 2 check images on page 2, found {len(placed)}")
    others = []
    for i, (box, secrets) in enumerate(zip(placed, drawn)):
        sx, sy = (box[2] - box[0]) / 900, (box[3] - box[1]) / 380
        for secret, (x0, y0, x1, y1) in secrets:
            others.append(Instance(secret, V[secret], "I1", "pixels", 1,
                                   [round(box[0] + x0 * sx, 2), round(box[1] + y0 * sy, 2),
                                    round(box[0] + x1 * sx, 2), round(box[1] + y1 * sy, 2)],
                                   note=f"check image {i + 1}"))
    return Built(pdf, "chrome", title,
                 text=[TextSpec("ACCT", V["ACCT"], "T1", 1), TextSpec("NAME1", V["NAME1"], "T1", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1)],
                 other=others)


def d08_card(work: Path, rng: random.Random) -> Built:
    title = "Credit card statement"
    pages = [
        "<div class='runhead'>Imaginary Card Services &mdash; Monthly statement</div>"
        + _envelope()
        + f"<p>Card number: {V['CARD']}</p>"
        + f"<p>New balance {_money(rng, 100, 3000)} &nbsp; Minimum payment {_money(rng, 25, 90)}</p>"
        + _transactions(rng, 10),
        "<h2>Transactions (continued)</h2>" + _transactions(rng, 12)
        + "<p class='small'>Questions? Call the number on the back of your card.</p>",
    ]
    return Built(chrome(work, "d08", title, pages), "chrome", title,
                 text=[TextSpec("CARD", V["CARD"], "T1", 1), TextSpec("NAME1", V["NAME1"], "T1", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1)])


def d09_brokerage_columns(work: Path, rng: random.Random) -> Built:
    """Two split values: one across two table columns (T3b), one across a
    page break (T3c)."""
    title = "Brokerage account statement"
    holdings = "".join(f"<tr><td>FUND {c}</td><td class='num'>{_money(rng, 100, 9000)}</td></tr>"
                       for c in "ABCDE")
    pages = [
        "<div class='runhead'>Placeholder Brokerage &mdash; Quarterly statement</div>"
        + f"<p>Mailing address on file: {esc(V['ADDR'])}, {CITY}</p>"
        + "<table style='table-layout:fixed'><tr>"
        "<td style='width:50%'><h2>Account owner</h2><p>Individual account</p>"
        "<p>Registered owner: Jordan Q.</p></td>"
        "<td><h2>Registration</h2><p>Testperson, individual</p>"
        "<p>Registered since 2019</p></td></tr></table>"
        + f"<h2>Holdings</h2><table>{holdings}</table>"
        + "<p style='margin-top:330pt'>Transfer-on-death beneficiary: Avery R.</p>",
        "<p>Samplename, spouse, 100% (continued from the previous page)</p>"
        "<h2>Activity</h2>" + _transactions(rng, 6),
    ]
    return Built(chrome(work, "d09", title, pages), "chrome", title,
                 text=[TextSpec("ADDR", V["ADDR"], "T1", 1)],
                 split=[SplitSpec("NAME1", V["NAME1"], "T3b", ["Jordan Q.", "Testperson"]),
                        SplitSpec("NAME2", V["NAME2"], "T3c", ["Avery R.", "Samplename"])])


# ── Scans ──────────────────────────────────────────────────────────────

def _letter_html(rng: random.Random) -> list[str]:
    return [
        "<p>Department of Imaginary Benefits<br>PO Box 0000, Anytown, ZZ 00000</p>"
        + _envelope()
        + f"<p>Re: {esc(V['NAME1'])}, SSN {V['SSN1']}</p>"
        "<p>Dear claimant,</p><p>We have received your application. Please keep this letter "
        "for your records. A decision will be mailed to the address above within 30 days.</p>"
        "<p>Sincerely,<br>Claims Office</p>"]


def d10_scan_letter(work: Path, rng: random.Random) -> Built:
    title = "Scanned letter"
    src = chrome(work, "d10-src", title, _letter_html(rng))
    jpgs, inst = scan_pages(src, rng, work, [
        TextSpec("NAME1", V["NAME1"], "S1", 2), TextSpec("SSN1", V["SSN1"], "S1", 1),
        TextSpec("ADDR", V["ADDR"], "S1", 1)], "S1")
    out = work / "d10.pdf"
    producers.image_pages_pdf(jpgs, out, title=title)
    return Built(out, "reportlab-scan", title, other=inst)


def _signed_form_html(rng: random.Random) -> list[str]:
    return [
        "<h1>Authorization form (fabricated sample)</h1>"
        f"<p>Name: {esc(V['NAME1'])}</p><p>SSN: {V['SSN1']}</p>"
        f"<p>Date of birth: {V['DOB']}</p>"
        "<p>I authorize the release of the records described below.</p>"
        "<p style='margin-top:60pt'>Signature: ______________________</p>",
        "<h2>Records requested</h2><p>Statements for calendar year 2025.</p>"
        f"<p>Requested by {esc(V['NAME1'])}</p>",
    ]


def d11_searchable_scan(work: Path, rng: random.Random) -> Built:
    title = "Signed authorization (scanned)"
    src = chrome(work, "d11-src", title, _signed_form_html(rng))
    jpgs, inst = scan_pages(src, rng, work, [
        TextSpec("NAME1", V["NAME1"], "S2", 2), TextSpec("SSN1", V["SSN1"], "S2", 1),
        TextSpec("DOB", V["DOB"], "S2", 1)], "S2")
    raw = work / "d11-tess.pdf"
    producers.tesseract_pdf(jpgs, raw)
    pdf = post.edit(raw)
    post.set_info(pdf, {"Title": title})
    out = work / "d11.pdf"
    post.save(pdf, out)
    pdf.close()
    # The text layer is tesseract's reading, so the declared page-text
    # count is not knowable in advance: the instances are the pixels.
    return Built(out, "tesseract", title, other=inst, ocr_text_layer=True)


def d12_mixed_pack(work: Path, rng: random.Random) -> Built:
    title = "Application packet"
    cover = chrome(work, "d12-cover", title, [
        "<h1>Application packet (fabricated sample)</h1>"
        f"<p>Applicant: {esc(V['NAME1'])}</p><p>Deposit account: {V['ACCT']}</p>"
        "<p>Enclosed: two scanned supporting documents.</p>"])
    scans_src = chrome(work, "d12-src", title, [
        f"<h2>Enclosure 1</h2><p>Proof of address</p>{_envelope()}",
        f"<h2>Enclosure 2</h2><p>Identity confirmation</p><p>SSN {V['SSN1']}</p>"])
    jpgs, inst = scan_pages(scans_src, rng, work, [
        TextSpec("NAME1", V["NAME1"], "S1", 1), TextSpec("ADDR", V["ADDR"], "S1", 1),
        TextSpec("SSN1", V["SSN1"], "S1", 1)], "S1", page_offset=1)
    scans = work / "d12-scans.pdf"
    producers.image_pages_pdf(jpgs, scans, title=title)
    out = work / "d12.pdf"
    post.concatenate([cover, scans], out)
    return Built(out, "chrome+reportlab-scan", title,
                 text=[TextSpec("NAME1", V["NAME1"], "T1", 1), TextSpec("ACCT", V["ACCT"], "T1", 1)],
                 other=inst)


# ── Fillable application ───────────────────────────────────────────────

def d13_benefits_form(work: Path, rng: random.Random) -> Built:
    title = "Benefits application"
    fields = [
        Field(0, "applicant", "1. Applicant full name", 40, 90, 320, value=V["NAME1"],
              secret="NAME1"),
        Field(0, "dob", "2. Date of birth", 400, 90, 140, value=V["DOB"], secret="DOB"),
        Field(0, "ssn", "3. Social security number", 40, 134, 200, value=V["SSN1"],
              secret="SSN1"),
        Field(0, "email", "4. Email", 260, 134, 280, value=V["EMAIL"], secret="EMAIL"),
        Field(0, "employed", "5. Currently employed", 40, 180, 14, kind="checkbox", value="Yes"),
        Field(1, "household", "6. Household size", 40, 90, 60, value="2"),
        Field(1, "program", "7. Program", 140, 90, 200, kind="choice", value="Housing",
              options=("Housing", "Childcare", "Energy")),
        Field(2, "ssn_confirm", "8. Re-enter SSN, one character per box", 40, 90, 242, h=20,
              kind="comb", value=V["SSN1"], secret="SSN1", loc="F2"),
    ]
    out = work / "d13.pdf"
    instances = form_pdf(out, title=title,
                         heading=["Benefits application (fabricated sample)",
                                  "Benefits application, household",
                                  "Benefits application, confirmation"],
                         static=[(1, 40, 140, "Attach proof of household size.", 9),
                                 (2, 40, 150, "Sign and date below.", 9)],
                         fields=fields, n_pages=3)
    return Built(out, "reportlab", title, other=instances)


# ── Quartz letter ──────────────────────────────────────────────────────

def d14_quartz_letter(work: Path, rng: random.Random) -> Built:
    title = "Employment verification"
    text = (
        "Anytown Payroll Services LLC\nPO Box 0000, Anytown, ZZ 00000\n\n"
        "EMPLOYMENT VERIFICATION\n\nTo whom it may concern,\n\n"
        f"This letter confirms that {V['NAME1']}\n"
        f"(SSN {V['SSN1']}) of {V['ADDR']}\n"
        f"has been employed with us since {_safe(lambda: str(rng.randint(2010, 2020)))}.\n\n"
        "Sincerely,\nHuman Resources\n")
    raw = work / "d14-quartz.pdf"
    producers.quartz_text_pdf(text, raw)
    pdf = post.edit(raw)
    post.set_info(pdf, {"Title": title})
    out = work / "d14.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "quartz+pikepdf", title,
                 text=[TextSpec(k, V[k], "T1", 1) for k in ("NAME1", "SSN1", "ADDR")])


# ── Annotations, metadata, outlines, attachments, thumbnails ───────────

def d15_annotated(work: Path, rng: random.Random) -> Built:
    title = "Annotated statement"
    src = chrome(work, "d15-src", title, [
        _statement_head(rng, 1, 2) + _envelope() + _transactions(rng, 7)
        + "<p style='margin-top:14pt'>Contact us</p>",
        _statement_head(rng, 2, 2) + _transactions(rng, 12)])
    pdf = post.edit(src)
    from . import locate
    lines = locate.page_lines(src)
    name_box = locate.find(lines, V["NAME1"])[0][1]
    contact = locate.find(lines, "Contact us")[0][1]
    a1 = [400.0, 60.0, 572.0, 80.0]
    a2 = [560.0, 200.0, 580.0, 220.0]
    post.add_annotation(pdf, 0, "FreeText", a1, f"Acct {V['ACCT']} - ask about fee")
    post.add_annotation(pdf, 0, "Text", a2, f"Spouse SSN {V['SSN2']}")
    post.add_annotation(pdf, 0, "Highlight", name_box, f"Check DOB {V['DOB']}")
    post.add_annotation(pdf, 0, "Link", contact, "mailto", uri=f"mailto:{V['EMAIL']}")
    out = work / "d15.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", title,
                 text=[TextSpec("ACCT", V["ACCT"], "T1", 2), TextSpec("NAME1", V["NAME1"], "T1", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1)],
                 other=[Instance("ACCT", V["ACCT"], "A1", "string", 0, a1, note="FreeText"),
                        Instance("SSN2", V["SSN2"], "A2", "string", 0, a2, note="sticky note"),
                        Instance("DOB", V["DOB"], "A3", "string", 0, name_box,
                                 note="highlight /Contents"),
                        Instance("EMAIL", V["EMAIL"], "A4", "string", 0, contact,
                                 note="link URI")])


def d16_metadata(work: Path, rng: random.Random) -> Built:
    title = f"{V['NAME1']} - 2025 tax summary"
    src = chrome(work, "d16-src", "Tax summary", [
        "<h1>Tax summary 2025 (fabricated sample)</h1>"
        f"<p>Prepared for SSN {V['SSN1']}</p>"
        + "".join(f"<p>Line {i}: {_money(rng, 0, 9000)}</p>" for i in range(1, 10))])
    pdf = post.edit(src)
    post.set_info(pdf, {"Title": title, "Author": V["NAME1"], "Subject": f"SSN {V['SSN1']}",
                        "Keywords": f"tax 2025 {V['ACCT']}", "TaxpayerSSN": V["SSN1"]})
    post.set_xmp(pdf, creator=V["NAME1"], title=title, keywords=f"tax 2025 {V['ACCT']}")
    out = work / "d16.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", title,
                 text=[TextSpec("SSN1", V["SSN1"], "T1", 1)],
                 other=[Instance("NAME1", V["NAME1"], "M1", "string", note="/Title"),
                        Instance("NAME1", V["NAME1"], "M1", "string", note="/Author"),
                        Instance("SSN1", V["SSN1"], "M1", "string", note="/Subject"),
                        Instance("ACCT", V["ACCT"], "M1", "string", note="/Keywords"),
                        Instance("SSN1", V["SSN1"], "M2", "string", note="/TaxpayerSSN"),
                        Instance("NAME1", V["NAME1"], "M3", "xmp", note="dc:creator, dc:title"),
                        Instance("ACCT", V["ACCT"], "M3", "xmp", note="pdf:Keywords")])


def d17_package(work: Path, rng: random.Random) -> Built:
    title = "Tax return package 2025"
    pages = [f"<h1>Tax return package (fabricated sample)</h1><p>Prepared for "
             f"{esc(V['NAME1'])}</p><p>Contents: federal return, schedules, state return.</p>"]
    for name in ("Federal return", "Schedule B", "State return"):
        pages.append(f"<h2>{name}</h2>" + "".join(
            f"<p>Line {i}: {_money(rng, 0, 9000)}</p>" for i in range(1, 12)))
    src = chrome(work, "d17-src", title, pages)
    pdf = post.edit(src)
    outline_title = f"{V['NAME1']} - federal return"
    post.add_outline(pdf, [("Cover", 0), (outline_title, 1), ("Schedule B", 2),
                           ("State return", 3)])
    csv = f"name,ssn,account\n{V['NAME1']},{V['SSN1']},{V['ACCT']}\n".encode()
    post.attach(pdf, "w2-import.csv", csv, f"W-2 import for {V['NAME1']}")
    out = work / "d17.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", title,
                 text=[TextSpec("NAME1", V["NAME1"], "T1", 1)],
                 other=[Instance("NAME1", V["NAME1"], "O1", "string",
                                 note=f"outline title {outline_title!r}"),
                        Instance("NAME1", V["NAME1"], "E1", "embedded", note="CSV and /Desc"),
                        Instance("SSN1", V["SSN1"], "E1", "embedded", note="CSV"),
                        Instance("ACCT", V["ACCT"], "E1", "embedded", note="CSV")])


def d18_thumbnails(work: Path, rng: random.Random) -> Built:
    title = "Account confirmation"
    pages = [
        "<h1>Account confirmation (fabricated sample)</h1>"
        f"<p style='font-size:22pt'>{esc(V['NAME1'])}</p>"
        f"<p style='font-size:22pt'>SSN {V['SSN1']}</p>" + _transactions(rng, 5),
        "<h2>Terms</h2>" + "".join(f"<p>Clause {i}: standard terms apply.</p>" for i in range(1, 9)),
    ]
    src = chrome(work, "d18-src", title, pages)
    import tempfile
    thumbs = producers.rasterise(src, 50, Path(tempfile.mkdtemp(prefix="rs-thumb-", dir=work)))
    pdf = post.edit(src)
    for i, png in enumerate(thumbs):
        post.add_thumbnail(pdf, i, png)
    out = work / "d18.pdf"
    post.save(pdf, out)
    pdf.close()
    return Built(out, "chrome+pikepdf", title,
                 text=[TextSpec("NAME1", V["NAME1"], "T1", 1), TextSpec("SSN1", V["SSN1"], "T1", 1)],
                 other=[Instance("NAME1", V["NAME1"], "P1", "pixels", 0,
                                 note="page 1 thumbnail, 50 dpi"),
                        Instance("SSN1", V["SSN1"], "P1", "pixels", 0,
                                 note="page 1 thumbnail, 50 dpi")])


def d19_variants(work: Path, rng: random.Random) -> Built:
    title = "Account change notice"
    page = (
        "<h1>Account change notice (fabricated sample)</h1>"
        "<p>Account holder (as printed on file): TESTPERSON, JORDAN Q</p>"
        "<p>Account: 7319-0462-8155</p>"
        f"<p>Mailing address: {esc(V['ADDR'])}, {CITY}</p>"
        "<p>Your new statement cycle starts next month.</p>")
    return Built(chrome(work, "d19", title, [page]), "chrome", title,
                 text=[TextSpec("NAME1", "TESTPERSON, JORDAN Q", "T2", 1),
                       TextSpec("ACCT", "7319-0462-8155", "T2", 1),
                       TextSpec("ADDR", V["ADDR"], "T1", 1)])


def d20_clean_control(work: Path, rng: random.Random) -> Built:
    title = "Household notes"
    pages = [
        "<h1>Household notes</h1><p>Library books due on the 14th.</p>"
        "<p>Recycling pickup moves to Thursdays.</p>" + _transactions(rng, 6),
        "<h2>Garden plan</h2>" + "".join(f"<p>Bed {i}: tomatoes and basil.</p>" for i in range(1, 8)),
    ]
    return Built(chrome(work, "d20", title, pages), "chrome", title, clean_control=True)


TEMPLATES: dict[str, Callable[[Path, random.Random], Built]] = {
    "D01": d01_w2, "D02": d02_1099int, "D03": d03_1040_fillable, "D04": d04_1040_printed,
    "D05": d05_state, "D06": d06_checking, "D07": d07_check_images, "D08": d08_card,
    "D09": d09_brokerage_columns, "D10": d10_scan_letter, "D11": d11_searchable_scan,
    "D12": d12_mixed_pack, "D13": d13_benefits_form, "D14": d14_quartz_letter,
    "D15": d15_annotated, "D16": d16_metadata, "D17": d17_package, "D18": d18_thumbnails,
    "D19": d19_variants, "D20": d20_clean_control,
}
