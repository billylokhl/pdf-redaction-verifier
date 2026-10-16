"""eval/caselib/rawcrypt.py: hand-built encrypted files that both readers
(pikepdf/libqpdf and MuPDF) open with the empty user password and decrypt
alike, and the two hand-made departures 3a-9's cross-check is tested
against: an AES padding the readers split on, and a stream-level crypt
filter they read differently."""

from __future__ import annotations

import io

import pikepdf
import pymupdf as fitz
import pytest

from caselib import rawpdf
from caselib.rawcrypt import Handler, flip_last_block, rc4, replace_stream_data, stream_span

CONTENT = b"BT /F1 12 Tf 72 700 Td (SSN 123-45-6789) Tj ET"
FILE_ID = bytes(range(16))
MOVED = "pikepdf's or MuPDF's decryption moved: re-probe 3a-9's cross-check (crypt.py)"


def _file(h: Handler, stream_dict: bytes = b"", data: bytes | None = None) -> bytes:
    objects = {
        1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
        4: rawpdf.stream(stream_dict, h.stream_data(4, CONTENT) if data is None else data),
        5: b"<< /Title " + h.string(5, b"A title") + b" >>",
        6: rawpdf.HELVETICA, 7: h.encrypt_dict()}
    return rawpdf.build(objects, info=5, trailer=h.trailer(7))


def _readers(pdf: bytes) -> tuple[fitz.Document, pikepdf.Pdf]:
    doc = fitz.open(stream=pdf)
    assert not doc.needs_pass
    return doc, pikepdf.open(io.BytesIO(pdf), attempt_recovery=False)


def test_rc4_matches_a_published_vector() -> None:
    # The widely published "Key"/"Plaintext" RC4 test vector.
    assert rc4(b"Key", b"Plaintext").hex() == "bbf316e8d940af0ad3"


@pytest.mark.parametrize("handler", [
    Handler(2, b"owner", FILE_ID), Handler(3, b"owner", FILE_ID), Handler(4, b"owner", FILE_ID),
    Handler(4, b"owner", FILE_ID, encrypt_metadata=False)], ids=["r2", "r3", "r4", "r4-meta"])
def test_both_readers_decrypt_a_hand_built_file_alike(handler: Handler) -> None:
    doc, pdf = _readers(_file(handler))
    assert pdf.is_encrypted and pdf.encryption.R == handler.revision
    assert doc.xref_stream_raw(4) == pdf.get_object(4, 0).read_raw_bytes() == CONTENT
    assert doc.metadata["title"] == str(pdf.docinfo.Title) == "A title"
    assert "SSN 123-45-6789" in doc[0].get_text()
    assert pdf.get_warnings() == [] and not doc.is_repaired


def test_plain_metadata_is_read_as_stored_by_pikepdf() -> None:
    # /EncryptMetadata false: the catalog's /Metadata is written plain
    # (MuPDF decrypts it anyway: ADR 0001's amendment pins that).
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    xmp = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>"
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R /Metadata 8 0 R >>", 2: rawpdf.PAGES,
               3: rawpdf.page(), 4: rawpdf.stream(b"", h.stream_data(4, CONTENT)),
               6: rawpdf.HELVETICA, 7: h.encrypt_dict(),
               8: rawpdf.stream(b"/Type /Metadata /Subtype /XML", xmp)}
    _, pdf = _readers(rawpdf.build(objects, trailer=h.trailer(7)))
    assert pdf.get_object(8, 0).read_raw_bytes() == xmp
    assert pdf.get_object(4, 0).read_raw_bytes() == CONTENT


def test_handler_refuses_what_it_cannot_write() -> None:
    with pytest.raises(ValueError):
        Handler(6, b"owner", FILE_ID)
    with pytest.raises(ValueError):
        Handler(3, b"owner", FILE_ID, encrypt_metadata=False)


def test_a_stream_crypt_filter_named_alone_splits_the_readers() -> None:
    # /Filter /Crypt with no parameters means the Identity filter: MuPDF
    # reads the stored plaintext; pikepdf applies /StmF to it.
    h = Handler(4, b"owner", FILE_ID)
    doc, pdf = _readers(_file(h, b"/Filter /Crypt", CONTENT))
    assert doc.xref_stream_raw(4) == CONTENT, MOVED
    assert pdf.get_object(4, 0).read_raw_bytes() != CONTENT, MOVED
    assert pdf.get_warnings() == []


def test_an_inconsistent_aes_padding_splits_the_readers(tmp_path) -> None:
    # 14 bytes pad with 02 02; flipping the IV's byte 14 makes it 07 02.
    content = b"BT (SSN 1) Tj "
    path = tmp_path / "aes.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Contents = pdf.make_stream(content)
        pdf.save(path, compress_streams=False, static_id=True,
                 encryption=pikepdf.Encryption(owner="owner", user="", R=4, aes=True))
    data = path.read_bytes()
    with pikepdf.open(path) as pdf:
        number = pdf.pages[0].Contents.objgen[0]
    raw = _stored(data, number)
    flipped = replace_stream_data(data, number, flip_last_block(raw, 14, 0x02 ^ 0x07))
    doc, pdf = _readers(flipped)
    assert pdf.get_object(number, 0).read_raw_bytes() == content + b"\x07\x02", MOVED
    assert doc.xref_stream_raw(number) == content, MOVED
    assert pdf.get_warnings() == [] and not doc.is_repaired


def _stored(pdf: bytes, number: int) -> bytes:
    start, end = stream_span(pdf, number)
    return pdf[start:end]


def test_replace_stream_data_keeps_every_offset() -> None:
    h = Handler(3, b"owner", FILE_ID)
    pdf = _file(h)
    # Ending in an end-of-line byte: the data is sliced by /Length.
    other = b"\r" + b"x" * (len(CONTENT) - 2) + b"\n"
    out = replace_stream_data(pdf, 4, other)
    assert len(out) == len(pdf) and _stored(out, 4) == other
    with pytest.raises(ValueError):
        replace_stream_data(pdf, 4, other + b"x")


def test_the_word_stream_inside_a_dictionary_is_not_its_data() -> None:
    pdf = rawpdf.build({1: rawpdf.CATALOG, 4: b"<< /Length 5 /Title (mystream!) >>\nstream\n"
                        b"abcde\nendstream"})
    start, end = stream_span(pdf, 4)
    assert pdf[start:end] == b"abcde"
    assert b"mystream!" in replace_stream_data(pdf, 4, b"vwxyz")
