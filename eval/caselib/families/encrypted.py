"""Encrypted files written by pikepdf (libqpdf), not PyMuPDF: one per
standard security handler revision, and one whose earlier revision is
encrypted under a different /Encrypt than the current one (ADR 0001:
Phase 3a adds that fixture; nothing exercised it before).

Every file opens with the empty user password (an owner password only),
as most "protected" PDFs do, so the secret is readable by anyone; the
bytes on disk hold it only encrypted. tests/test_pikepdf_raw_stage.py
uses these files for 3a-8's confirmation: pikepdf's decrypted, still
filtered stream read equals MuPDF's ``xref_stream_raw``, per stream and
per revision.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pikepdf

from ..model import SSN, case, expect

LIVE = expect(1, findings=(("SSN", "live"),))
OWNER_ONLY = ("Trusting \"password protection\" with only an owner password: the file "
              "opens for anyone, and the owner password only restricts editing and "
              "printing.")
OPEN_IT = "Open the file: no password is asked for, and the text is there."

# The standard security handler's revisions pikepdf writes (R2-R4 RC4 or
# AES-128; R6 AES-256). RC4 cannot leave the metadata unencrypted-flag set.
HANDLERS = ("rc4-40", "rc4-128", "aes-128", "aes-256")


def _encryption(handler: str, owner: str) -> pikepdf.Encryption:
    if handler == "rc4-40":
        return pikepdf.Encryption(owner=owner, user="", R=2, aes=False, metadata=False)
    if handler == "rc4-128":
        return pikepdf.Encryption(owner=owner, user="", R=3, aes=False, metadata=False)
    if handler == "aes-128":
        return pikepdf.Encryption(owner=owner, user="", R=4, aes=True)
    if handler == "aes-256":
        return pikepdf.Encryption(owner=owner, user="", R=6, aes=True)
    raise ValueError(handler)
# Fabricated filler in a stream of its own, left uncompressed: the
# confirmation test checks it is absent from the file's bytes (encrypted
# on disk) and present in the decrypted, unfiltered read.
FILLER = b"Fabricated filler: quarterly totals 1,042.50."


def encrypted_page(text: bytes, handler: str, owner: str) -> bytes:
    """One page drawing *text* in Helvetica, plus an uncompressed stream
    of filler, encrypted with *handler* (empty user password). Classic
    xref, no object streams, streams left as written: the layout ``stack``
    and the confirmation test rely on."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    page = pdf.pages[0]
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.obj.Contents = pdf.make_stream(b"BT /F1 12 Tf 72 700 Td (" + text + b") Tj ET")
    pdf.Root.PieceInfo = pdf.make_stream(FILLER)
    out = io.BytesIO()
    pdf.save(out, encryption=_encryption(handler, owner),
             object_stream_mode=pikepdf.ObjectStreamMode.disable, static_id=True,
             compress_streams=False)
    return out.getvalue()


def stack(first: bytes, second: bytes) -> bytes:
    """*second*'s objects appended to *first* as an incremental update:
    the same object numbers, re-encrypted under *second*'s own /Encrypt
    (its trailer, with /Prev at *first*'s xref). Both must be classic-xref
    files from ``encrypted_page``, so they number their objects alike."""
    prev = int(re.findall(rb"startxref\s+(\d+)", first)[-1])
    out = bytearray(first if first.endswith(b"\n") else first + b"\n")
    offsets = {}
    for m in re.finditer(rb"(\d+) 0 obj\b.*?endobj", second, re.S):
        offsets[int(m.group(1))] = len(out)
        out += m.group(0) + b"\n"
    xref = len(out)
    out += b"xref\n0 1\n0000000000 65535 f \n"
    for number in sorted(offsets):
        out += b"%d 1\n%010d 00000 n \n" % (number, offsets[number])
    trailer = re.findall(rb"trailer\s*(<<.*?>>)\s*startxref", second, re.S)[-1]
    out += b"trailer\n" + trailer[:-2] + b" /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (prev, xref)
    return bytes(out)


def _page_case(handler: str) -> None:
    @case(f"page.encrypted-{handler}", truth="leak", cells="live.plain", writer="pikepdf",
          expected=LIVE,
          story=f"A one-page notice \"protected\" with {handler.upper()} encryption and an "
                "owner password only: the SSN is on the page for anyone who opens it.",
          mistake=OWNER_ONLY, recovery=OPEN_IT)
    def build(path: Path) -> None:
        path.write_bytes(encrypted_page(b"SSN " + SSN.encode(), handler, "owner-" + handler))


for _handler in HANDLERS:
    _page_case(_handler)


@case("revision.encrypted-different-handlers", truth="leak", cells="superseded.plain",
      writer="pikepdf", expected=expect(1, findings=(("SSN", "superseded"),)),
      story="A notice saved encrypted with RC4-128, then \"redacted\" in an incremental "
            "update re-encrypted with AES-256 under a new owner password: the page shows "
            "the redaction, but the first revision, under its own /Encrypt, still holds "
            "the SSN.",
      mistake="Redacting by appending an update (and changing the encryption) instead of "
              "rewriting the file.",
      recovery="Cut the file at the first revision's xref and open that: it decrypts "
               "with its own /Encrypt, and the SSN is on the page.")
def different_handlers(path: Path) -> None:
    path.write_bytes(stack(encrypted_page(b"SSN " + SSN.encode(), "rc4-128", "owner-a"),
                           encrypted_page(b"SSN [redacted]", "aes-256", "owner-b")))
