"""Encrypted files written by pikepdf (libqpdf), not PyMuPDF: one per
standard security handler revision; one with object and cross-reference
streams; one with plain-text metadata (/EncryptMetadata false); and one
whose earlier revision is encrypted under a different /Encrypt and /ID
than the current one (ADR 0001: Phase 3a adds that fixture; nothing
exercised it before).

Every file opens with the empty user password (an owner password only),
as most "protected" PDFs do, so the secret is readable by anyone; the
bytes on disk hold it only encrypted. tests/test_pikepdf_raw_stage.py
uses these files for 3a-8's confirmation: pikepdf's decrypted, still
filtered stream read equals MuPDF's ``xref_stream_raw`` for every stream
the handler encrypts, per revision (and pins where the two differ on
streams it does not).
"""

from __future__ import annotations

import io
import re
import zlib
from pathlib import Path

import pikepdf

from ..model import SSN, case, expect

LIVE = expect(1, findings=(("SSN", "live"),))
OWNER_ONLY = ("Trusting \"password protection\" with only an owner password: the file "
              "opens for anyone, and the owner password only restricts editing and "
              "printing.")
OPEN_IT = "Open the file: no password is asked for, and the text is there."

# The standard security handler's revisions pikepdf writes: R2-R4 RC4 or
# AES-128, R6 AES-256. Under RC4 pikepdf requires metadata=False (it does
# not write /EncryptMetadata for R2/R3); none of these files carries
# metadata unless a builder adds it.
HANDLERS = ("rc4-40", "rc4-128", "aes-128", "aes-256")


def _encryption(handler: str, owner: str, *, encrypt_metadata: bool = True
                ) -> pikepdf.Encryption:
    if handler == "rc4-40":
        return pikepdf.Encryption(owner=owner, user="", R=2, aes=False, metadata=False)
    if handler == "rc4-128":
        return pikepdf.Encryption(owner=owner, user="", R=3, aes=False, metadata=False)
    if handler == "aes-128":
        return pikepdf.Encryption(owner=owner, user="", R=4, aes=True,
                                  metadata=encrypt_metadata)
    if handler == "aes-256":
        return pikepdf.Encryption(owner=owner, user="", R=6, aes=True,
                                  metadata=encrypt_metadata)
    raise ValueError(handler)


# Fabricated filler in a stream of its own, left uncompressed: the
# confirmation test checks it is absent from the file's bytes (encrypted
# on disk) and present in the decrypted, unfiltered read.
FILLER = b"Fabricated filler: quarterly totals 1,042.50."

XMP = ("<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>"
       "<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF "
       "xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
       "<rdf:Description rdf:about='' xmlns:dc='http://purl.org/dc/elements/1.1/'>"
       "<dc:title><rdf:Alt><rdf:li xml:lang='x-default'>{title}</rdf:li></rdf:Alt></dc:title>"
       "</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end='w'?>")


def encrypted_page(text: bytes, handler: str, owner: str, *, xref_streams: bool = False,
                   xmp_title: str | None = None, encrypt_metadata: bool = True) -> bytes:
    """One page drawing *text* in Helvetica (its content stream
    Flate-compressed, so the stage under test is still filtered), plus an
    uncompressed stream of filler, encrypted with *handler* (empty user
    password). By default a classic xref table and no object streams, the
    layout ``stack`` needs; *xref_streams* writes object and
    cross-reference streams instead. *xmp_title* adds a document XMP
    packet, encrypted or not as *encrypt_metadata* says."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
    page = pdf.pages[0]
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.obj.Contents = pdf.make_stream(
        zlib.compress(b"BT /F1 12 Tf 72 700 Td (" + text + b") Tj ET"),
        Filter=pikepdf.Name.FlateDecode)
    pdf.Root.PieceInfo = pdf.make_stream(FILLER)
    if xmp_title is not None:
        pdf.Root.Metadata = pdf.make_stream(XMP.format(title=xmp_title).encode(),
                                            Type=pikepdf.Name.Metadata,
                                            Subtype=pikepdf.Name.XML)
    out = io.BytesIO()
    mode = (pikepdf.ObjectStreamMode.generate if xref_streams
            else pikepdf.ObjectStreamMode.disable)
    pdf.save(out, encryption=_encryption(handler, owner, encrypt_metadata=encrypt_metadata),
             object_stream_mode=mode, static_id=True, compress_streams=False)
    return out.getvalue()


def stack(first: bytes, second: bytes) -> bytes:
    """*second*'s objects appended to *first* as an incremental update:
    the same object numbers, re-encrypted under *second*'s own /Encrypt
    (its trailer, with /Prev at *first*'s xref, and a /ID of its own). Both
    must be classic-xref files from ``encrypted_page``, so they number
    their objects alike. *second*'s handler must not use /ID (R6), since
    its /ID is replaced after it was encrypted."""
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
    # Its own /ID too: an earlier revision's key (R2-R4) depends on its
    # /ID, so a reader that used the current one would fail on it.
    trailer = re.sub(rb"/ID\s*\[\s*<[0-9a-fA-F]*>\s*<[0-9a-fA-F]*>\s*\]",
                     b"/ID [<" + NEW_ID + b"> <" + NEW_ID + b">]", trailer)
    out += b"trailer\n" + trailer[:-2] + b" /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (prev, xref)
    return bytes(out)


# A fabricated /ID for the update's trailer, unlike static_id's.
NEW_ID = b"0f1e2d3c4b5a69788796a5b4c3d2e1f0"


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


@case("page.encrypted-aes-256-xref-streams", truth="leak", cells="live.plain",
      writer="pikepdf", expected=LIVE,
      story="The same AES-256 notice saved with object streams and a cross-reference "
            "stream, as most current writers save: the SSN is on the page.",
      mistake=OWNER_ONLY, recovery=OPEN_IT)
def aes_256_xref_streams(path: Path) -> None:
    path.write_bytes(encrypted_page(b"SSN " + SSN.encode(), "aes-256", "owner-x",
                                    xref_streams=True))


@case("document.encrypted-plain-metadata", truth="leak", cells="metadata.plain",
      writer="pikepdf", expected=expect(1, findings=(("SSN", "live"),)),
      story="An AES-128 file whose page was cleaned but whose XMP title still holds the "
            "SSN, with /EncryptMetadata false: the metadata is stored in plain text "
            "inside an encrypted file.",
      mistake="Forgetting the document metadata, and assuming encryption covers it.",
      recovery="Read the XMP packet: it is not encrypted.")
def plain_metadata(path: Path) -> None:
    path.write_bytes(encrypted_page(b"Notice", "aes-128", "owner-m", xref_streams=True,
                                    xmp_title="Notice for SSN " + SSN,
                                    encrypt_metadata=False))
