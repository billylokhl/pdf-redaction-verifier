"""redaction_verifier.crypt: each revision's decryption by pikepdf,
cross-checked against MuPDF (ADR 0001, as amended 2026-10-15), on
hand-built files (eval/caselib/rawcrypt.py) and pikepdf-written AES ones.
Every CryptRule and MismatchRule a fixture can reach is pinned here."""

from __future__ import annotations

import re
from pathlib import Path

import pikepdf
import pytest

from caselib import rawpdf
from caselib.rawcrypt import Handler, flip_last_block, replace_stream_data, stream_span
from redaction_verifier import crypt
from redaction_verifier.budget import Budget
from redaction_verifier.inventory.decrypted import CryptRule, Decryption, MismatchRule
from redaction_verifier.inventory.build import build_inventory
from redaction_verifier.inventory.xref import read_chain
from redaction_verifier.ledger import FlagReason

CONTENT = b"BT /F1 12 Tf 72 700 Td (SSN 123-45-6789) Tj ET"
FILE_ID = bytes(range(16))


def decrypt(pdf: bytes, **kwargs: int) -> tuple[Decryption, ...]:
    inventory = build_inventory(pdf, budget=Budget(file_size=len(pdf)))
    chain = read_chain(pdf, budget=Budget(file_size=len(pdf)))
    return crypt.decrypt(pdf, inventory, chain, **kwargs)


def rules(revision: Decryption) -> set[tuple[str, int, int]]:
    """(reason, object, rule) of each flag."""
    return {(f.reason.value, dict(f.params)["object"], dict(f.params)["rule"])
            for f in revision.flags}


def hand_built(h: Handler, stream_dict: bytes = b"", data: bytes | None = None,
               **extra: bytes) -> bytes:
    objects = {1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
               4: rawpdf.stream(stream_dict, h.stream_data(4, CONTENT) if data is None else data),
               5: b"<< /Title " + h.string(5, b"A title") + b" >>",
               6: rawpdf.HELVETICA, 7: h.encrypt_dict()}
    objects.update({int(k[1:]): v for k, v in extra.items()})
    return rawpdf.build(objects, info=5, trailer=h.trailer(7))


def pikepdf_written(tmp_path: Path, content: bytes, encryption: pikepdf.Encryption) -> bytes:
    path = tmp_path / "written.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Contents = pdf.make_stream(content)
        pdf.save(path, compress_streams=False, static_id=True, encryption=encryption)
    return path.read_bytes()


# ── Agreement ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("handler", [
    Handler(2, b"owner", FILE_ID), Handler(3, b"owner", FILE_ID), Handler(4, b"owner", FILE_ID),
    Handler(3, b"", FILE_ID)], ids=["r2", "r3", "r4", "r3-empty-owner"])
def test_a_file_both_readers_decrypt_alike_is_verified_whole(handler: Handler) -> None:
    (revision,) = decrypt(hand_built(handler))
    assert revision.flags == ()
    assert revision.identity is not None
    assert revision.streams == {(4, 0): CONTENT}
    assert revision.values[(5, 0)] == ("dict", ((b"Title", ("str", b"A title")),))
    assert set(revision.values) == {(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0)}


@pytest.mark.parametrize("aes", [4, 6])
def test_aes_written_by_pikepdf_is_verified(tmp_path: Path, aes: int) -> None:
    pdf = pikepdf_written(tmp_path, CONTENT,
                          pikepdf.Encryption(owner="owner", user="", R=aes, aes=True))
    (revision,) = decrypt(pdf)
    assert revision.flags == ()
    assert CONTENT in revision.streams.values()


def test_an_unencrypted_file_has_nothing_to_decrypt() -> None:
    pdf = rawpdf.build({1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
                        4: rawpdf.stream(b"", CONTENT), 6: rawpdf.HELVETICA})
    assert decrypt(pdf) == (Decryption(0, None, {}, {}, ()),)


def test_identity_tells_handlers_and_ids_apart() -> None:
    (a,) = decrypt(hand_built(Handler(3, b"owner", FILE_ID)))
    (b,) = decrypt(hand_built(Handler(3, b"other", FILE_ID)))
    (c,) = decrypt(hand_built(Handler(3, b"owner", bytes(16))))
    assert a.identity != b.identity and a.identity != c.identity
    assert a.identity is not None and a.identity[1] == FILE_ID


# ── DECRYPTION_MISMATCH ────────────────────────────────────────────────
def test_an_aes_padding_the_readers_strip_differently_mismatches(tmp_path: Path) -> None:
    content = b"BT (SSN 1) Tj "                 # 14 bytes: padded 02 02
    pdf = pikepdf_written(tmp_path, content,
                          pikepdf.Encryption(owner="owner", user="", R=4, aes=True))
    number = int(re.search(rb"/Contents (\d+) 0 R", pdf).group(1))
    start, end = stream_span(pdf, number)
    pdf = replace_stream_data(pdf, number, flip_last_block(pdf[start:end], 14, 0x02 ^ 0x07))
    (revision,) = decrypt(pdf)
    assert ("decryption_mismatch", number, MismatchRule.STREAM) in rules(revision)
    assert (number, 0) not in revision.streams


# ── DECRYPTION_UNVERIFIED ──────────────────────────────────────────────
def test_a_stream_crypt_filter_is_never_trusted() -> None:
    (revision,) = decrypt(hand_built(Handler(4, b"owner", FILE_ID), b"/Filter /Crypt", CONTENT))
    assert rules(revision) == {("decryption_unverified", 4, CryptRule.CRYPT_FILTER)}
    assert (4, 0) not in revision.streams


def test_a_reader_warning_on_an_object_leaves_it_unverified() -> None:
    # The header says generation 1, the xref 0: each reader keys the
    # decryption on a different one, and both say so only when reading.
    pdf = hand_built(Handler(3, b"owner", FILE_ID)).replace(b"\n4 0 obj\n", b"\n4 1 obj\n")
    # The page that refers to it is read differently too: pikepdf
    # resolves `4 0 R` to nothing.
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", 4, CryptRule.WARNING),
                               ("decryption_mismatch", 3, MismatchRule.VALUE)}
    assert (4, 0) not in revision.streams and (3, 0) not in revision.values


def test_a_user_password_we_do_not_have(tmp_path: Path) -> None:
    pdf = pikepdf_written(tmp_path, CONTENT,
                          pikepdf.Encryption(owner="owner", user="user", R=4, aes=True))
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.PASSWORD)}
    assert revision.values == {} and revision.streams == {}


def test_one_reader_opening_and_the_other_not_is_a_split() -> None:
    # /V 2 /R 3 with no /Length: pikepdf takes 40 bits, MuPDF 128.
    h = Handler(3, b"owner", FILE_ID)
    pdf = hand_built(h, x7=h.encrypt_dict().replace(b" /Length 128", b""))
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.AUTH_SPLIT)}


def test_a_perms_entry_that_does_not_check_out(tmp_path: Path) -> None:
    pdf = pikepdf_written(tmp_path, CONTENT,
                          pikepdf.Encryption(owner="owner", user="", R=6, aes=True))
    perms = re.search(rb"/Perms <([0-9a-fA-F])", pdf)
    assert perms is not None
    digit = perms.group(1)
    pdf = pdf[:perms.start(1)] + (b"0" if digit != b"0" else b"1") + pdf[perms.end(1):]
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.PERMS)}


def test_embedded_files_under_their_own_filter() -> None:
    h = Handler(4, b"owner", FILE_ID)
    attachment = rawpdf.stream(b"/Type /EmbeddedFile", h.stream_data(8, b"SSN 123-45-6789", 0))
    pdf = hand_built(h, x8=attachment, x7=h.encrypt_dict().replace(
        b"/StrF /StdCF", b"/StrF /StdCF /EFF /Identity"))
    (revision,) = decrypt(pdf)
    assert ("decryption_unverified", 8, CryptRule.EFF) in rules(revision)


def test_stored_bytes_that_already_inflate_are_not_ciphertext() -> None:
    import zlib
    plain = zlib.compress(CONTENT)
    pdf = hand_built(Handler(3, b"owner", FILE_ID), b"/Filter /FlateDecode",
                     Handler(3, b"owner", FILE_ID).stream_data(4, plain))
    (clean,) = decrypt(pdf)
    assert clean.flags == ()                     # Flate under encryption: fine
    stored = hand_built(Handler(3, b"owner", FILE_ID), b"/Filter /FlateDecode", plain)
    (revision,) = decrypt(stored)
    assert ("decryption_unverified", 4, CryptRule.STORED_PLAIN) in rules(revision)


def test_a_budget_stops_the_reads() -> None:
    (revision,) = decrypt(hand_built(Handler(3, b"owner", FILE_ID)), max_bytes=10)
    assert any(f.reason is FlagReason.BUDGET_EXHAUSTED for f in revision.flags)
    assert revision.streams == {}


def test_only_the_catalogs_metadata_is_plain_under_encrypt_metadata_false() -> None:
    # ISO 32000-1 7.6.1 (and qpdf): the document-level /Metadata stream
    # only. Another XMP stream stays encrypted; MuPDF is not asked about
    # the plain one (it decrypts it anyway: ADR 0001's amendment).
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    xmp = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'/>"
    pdf = hand_built(
        h, x1=b"<< /Type /Catalog /Pages 2 0 R /Metadata 8 0 R >>",
        x3=rawpdf.page(extra=b"/Metadata 9 0 R"),
        x8=rawpdf.stream(b"/Type /Metadata /Subtype /XML", xmp),
        x9=rawpdf.stream(b"/Type /Metadata /Subtype /XML", h.stream_data(9, xmp)))
    (revision,) = decrypt(pdf)
    assert revision.flags == ()
    assert revision.streams[(8, 0)] == revision.streams[(9, 0)] == xmp



# ── Identity, reuse and cost ───────────────────────────────────────────
def test_identity_sees_an_indirect_crypt_filter_change() -> None:
    # Two R4 files alike but for an indirect /StdCF's method: the /Encrypt
    # dictionaries print alike (`/CF 9 0 R`), the decryptions do not.
    h = Handler(4, b"owner", FILE_ID)
    indirect = h.encrypt_dict().replace(
        b"/CF << /StdCF << /CFM /V2 /Length 16 /AuthEvent /DocOpen >> >>", b"/CF 9 0 R")
    a = hand_built(h, x7=indirect, x9=b"<< /StdCF << /CFM /V2 /Length 16 /AuthEvent /DocOpen >> >>")
    b = hand_built(h, x7=indirect, x9=b"<< /StdCF << /CFM /None /Length 16 /AuthEvent /DocOpen >> >>")
    (first,), (second,) = decrypt(a), decrypt(b)
    assert first.identity is not None and second.identity is not None
    assert first.identity != second.identity


def test_a_body_two_revisions_share_under_one_identity_is_verified_once() -> None:
    h = Handler(3, b"owner", FILE_ID)
    base = hand_built(h)
    pdf = rawpdf.incremental_update(base, {5: b"<< /Title " + h.string(5, b"New") + b" >>"},
                                    info=5)
    pdf = pdf.replace(b"/Prev", h.trailer(7) + b" /Prev")
    current, earlier = decrypt(pdf)
    assert current.flags == earlier.flags == ()
    assert current.identity == earlier.identity
    assert current.streams == earlier.streams == {(4, 0): CONTENT}
    assert current.values[(5, 0)] != earlier.values[(5, 0)]   # the rewritten /Info


def test_every_value_read_is_charged() -> None:
    # No stream at all: the cap still stops the reads.
    h = Handler(3, b"owner", FILE_ID)
    pdf = rawpdf.build({1: rawpdf.CATALOG, 2: b"<< /Type /Pages /Kids [] /Count 0 >>",
                        5: b"<< /Title " + h.string(5, b"A title") + b" >>",
                        7: h.encrypt_dict()}, info=5, trailer=h.trailer(7))
    (revision,) = decrypt(pdf, max_bytes=20)
    assert any(f.reason is FlagReason.BUDGET_EXHAUSTED for f in revision.flags)


def test_members_of_an_encrypted_object_stream_are_read(tmp_path: Path) -> None:
    # The inventory cannot inflate the encrypted object stream yet (until
    # 3a-9b) but keeps it, without members; crypt reads them through both
    # readers.
    path = tmp_path / "objstm.pdf"
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page()
        page.Contents = pdf.make_stream(CONTENT)
        pdf.docinfo["/Title"] = "A title"
        pdf.save(path, object_stream_mode=pikepdf.ObjectStreamMode.generate, static_id=True,
                 encryption=pikepdf.Encryption(owner="owner", user="", R=6, aes=True))
    data = path.read_bytes()
    inventory = build_inventory(data, budget=Budget(file_size=len(data)))
    assert inventory.object_streams and all(
        s.members == () for s in inventory.object_streams.values())
    (revision,) = decrypt(data)
    assert revision.flags == ()
    assert ("str", b"A title") in [v for form in revision.values.values()
                                    if isinstance(form, tuple) and form[0] == "dict"
                                    for _, v in form[1]]
