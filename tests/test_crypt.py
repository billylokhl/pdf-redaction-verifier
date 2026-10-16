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
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.EFF)}


def test_an_eff_flags_the_revision_even_without_a_type() -> None:
    # /Type is optional on an embedded file: a reader honouring /EFF shows
    # an attachment no /Type names, so the whole revision is untrusted.
    h = Handler(4, b"owner", FILE_ID, stm_f="Identity")
    attachment = rawpdf.stream(b"", h.stream_data(8, b"SSN 123-45-6789", 0))
    pdf = hand_built(h, x8=attachment, data=CONTENT, x7=h.encrypt_dict().replace(
        b"/StrF /StdCF", b"/StrF /StdCF /EFF /StdCF"))
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.EFF)}
    assert revision.streams == {}


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
    # dictionaries print alike (`/CF 9 0 R`). (The second, /CFM /None, is
    # outside the closed set too; its identity is computed first.)
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


# ── The closed set (ADR 0001's canonical-form note) ───────────────────
def _form(**entries: object) -> object:
    return ("dict", tuple(sorted((k.encode(), v) for k, v in entries.items())))


N = lambda name: ("name", name.encode())     # noqa: E731
I = lambda value: ("int", value)              # noqa: E731
STD = N("Standard")
CF4 = _form(StdCF=_form(CFM=N("AESV2"), AuthEvent=N("DocOpen"), Length=I(16)))
CF5 = _form(StdCF=_form(CFM=N("AESV3"), AuthEvent=N("DocOpen"), Length=I(32)))


@pytest.mark.parametrize("form, ok", [
    (_form(Filter=STD, V=I(1), R=I(2)), True),
    (_form(Filter=STD, V=I(1), R=I(2), Length=I(40)), True),
    (_form(Filter=STD, V=I(1), R=I(2), Length=I(128)), False),
    (_form(Filter=STD, V=I(2), R=I(3), Length=I(128)), True),
    (_form(Filter=STD, V=I(2), R=I(3), Length=I(56)), True),
    (_form(Filter=STD, V=I(2), R=I(3)), False),                  # /Length left to the reader
    (_form(Filter=STD, V=I(2), R=I(3), Length=I(44)), False),
    (_form(Filter=STD, V=I(4), R=I(4), CF=CF4, StmF=N("StdCF"), StrF=N("StdCF")), True),
    (_form(Filter=STD, V=I(4), R=I(4), CF=CF4, StmF=N("StdCF"), StrF=N("Identity"),
           EncryptMetadata=("bool", False)), True),
    (_form(Filter=STD, V=I(4), R=I(4), CF=CF4, StmF=N("Other"), StrF=N("StdCF")), False),
    (_form(Filter=STD, V=I(4), R=I(4), CF=CF4, StmF=N("StdCF"), StrF=N("StdCF"),
           EFF=N("Identity")), True),                            # flagged later: rule EFF
    (_form(Filter=STD, V=I(4), R=I(4), StmF=N("StdCF"), StrF=N("StdCF"),
           CF=_form(StdCF=_form(CFM=N("None")))), False),
    (_form(Filter=STD, V=I(4), R=I(4), StmF=N("StdCF"), StrF=N("StdCF"),
           CF=_form(StdCF=_form(CFM=N("V2"), AuthEvent=N("EFOpen")))), False),
    (_form(Filter=STD, V=I(4), R=I(4), Length=I(64), CF=CF4), False),
    (_form(Filter=STD, V=I(5), R=I(6), CF=CF5, StmF=N("StdCF"), StrF=N("StdCF")), True),
    (_form(Filter=STD, V=I(5), R=I(5), CF=CF5, StmF=N("StdCF"), StrF=N("StdCF")), False),
    (_form(Filter=STD, V=I(3), R=I(3), Length=I(128)), False),
    (_form(Filter=N("Other"), V=I(2), R=I(3), Length=I(128)), False),
    (_form(Filter=STD, SubFilter=N("adbe.x"), V=I(2), R=I(3), Length=I(128)), False),
    (_form(Filter=STD, V=I(2), R=I(3), Length=I(128), EncryptMetadata=I(0)), False),
], ids=lambda x: "" if isinstance(x, tuple) else str(x))
def test_the_closed_set_of_handlers(form: object, ok: bool) -> None:
    assert crypt._canonical(form) is ok


def test_a_handler_outside_the_set_is_never_trusted() -> None:
    # The readers agree (neither reads /AuthEvent for the empty password):
    # the set, not their agreement, decides.
    h = Handler(4, b"owner", FILE_ID)
    pdf = hand_built(h, x7=h.encrypt_dict().replace(b"/AuthEvent /DocOpen", b"/AuthEvent /EFOpen"))
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.NONCANONICAL)}
    assert revision.values == {} and revision.streams == {}



# ── Round 1 (#178): what a reader shows that the verifier would not ────
def test_a_stream_calling_itself_xref_is_not_exempt() -> None:
    # qpdf leaves any /Type /XRef stream undecrypted; MuPDF decrypts it and
    # draws the page. Only the chain's own cross-reference streams are plain.
    h = Handler(3, b"owner", FILE_ID)
    (revision,) = decrypt(hand_built(h, b"/Type /XRef"))
    assert rules(revision) == {("decryption_unverified", 4, CryptRule.XREF_TYPE)}
    assert (4, 0) not in revision.streams


def test_ciphertext_where_plain_metadata_belongs() -> None:
    # /EncryptMetadata false, but the catalog's XMP stored encrypted: pikepdf
    # returns the stored bytes, MuPDF decrypts them to the XMP it shows.
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    xmp = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>SSN 123-45-6789</x:xmpmeta>"
    pdf = hand_built(h, x1=b"<< /Type /Catalog /Pages 2 0 R /Metadata 8 0 R >>",
                     x8=rawpdf.stream(b"/Type /Metadata /Subtype /XML", h.stream_data(8, xmp)))
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", 8, CryptRule.METADATA_CIPHER)}
    assert (8, 0) not in revision.streams


def test_identity_streams_are_plain_not_stored_plain() -> None:
    import zlib
    h = Handler(4, b"owner", FILE_ID, stm_f="Identity")
    (revision,) = decrypt(hand_built(h, b"/Filter /FlateDecode", zlib.compress(CONTENT)))
    assert revision.flags == ()
    assert revision.streams[(4, 0)] == zlib.compress(CONTENT)


def test_an_unencrypted_revision_pikepdf_cannot_open_is_not_ours_to_flag() -> None:
    # A bad xref offset: MuPDF repairs, pikepdf (no recovery) refuses; no
    # /Encrypt anywhere, so nothing here is crypt's.
    pdf = rawpdf.build({1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
                        4: rawpdf.stream(b"", CONTENT), 6: rawpdf.HELVETICA})
    pdf = re.sub(rb"startxref\n\d+", b"startxref\n9", pdf)
    assert all(r.identity is None and r.flags == () for r in decrypt(pdf))


def test_entries_are_charged_reused_or_not() -> None:
    # Revisions x entries is the work: the cap stops it, whatever is reused.
    h = Handler(3, b"owner", FILE_ID)
    pdf = hand_built(h)
    for i in range(5):
        pdf = rawpdf.incremental_update(pdf, {5: b"<< /Title " + h.string(5, b"v%d" % i)
                                              + b" >>"}, info=5)
        pdf = pdf.replace(b"/Prev", h.trailer(7) + b" /Prev", 1) if i == 0 else \
            pdf[:pdf.rindex(b"/Prev")] + h.trailer(7) + b" " + pdf[pdf.rindex(b"/Prev"):]
    revisions = decrypt(pdf, max_entries=20)
    assert any(f.reason is FlagReason.BUDGET_EXHAUSTED for r in revisions for f in r.flags)


def test_a_body_is_read_once_under_one_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Handler(3, b"owner", FILE_ID)
    pdf = rawpdf.incremental_update(hand_built(h), {5: b"<< /Title " + h.string(5, b"New")
                                                    + b" >>"}, info=5)
    pdf = pdf.replace(b"/Prev", h.trailer(7) + b" /Prev")
    reads: list[int] = []
    real = pikepdf.Pdf.get_object
    monkeypatch.setattr(pikepdf.Pdf, "get_object",
                        lambda self, *a: reads.append(a[0]) or real(self, *a))
    decrypt(pdf)
    assert sorted(reads) == [1, 2, 3, 4, 5, 5, 6, 7]   # only the rewritten /Info twice


def test_an_untrusted_revision_verifies_nothing_for_another(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # A revision found REPAIRED after its reads must not hand its bodies to
    # the next revision under the same identity.
    h = Handler(3, b"owner", FILE_ID)
    pdf = rawpdf.incremental_update(hand_built(h), {5: b"<< /Title " + h.string(5, b"New")
                                                    + b" >>"}, info=5)
    pdf = pdf.replace(b"/Prev", h.trailer(7) + b" /Prev")
    repaired = iter([True, False])
    real_open = crypt.pymupdf.open

    def opened(*args: object, **kwargs: object) -> object:
        doc = real_open(*args, **kwargs)
        flag = next(repaired)
        return type("Doc", (), {"__getattr__": lambda s, n: getattr(doc, n),
                                "is_repaired": flag})()
    monkeypatch.setattr(crypt.pymupdf, "open", opened)
    reads: list[int] = []
    real = pikepdf.Pdf.get_object
    monkeypatch.setattr(pikepdf.Pdf, "get_object",
                        lambda self, *a: reads.append(a[0]) or real(self, *a))
    current, earlier = decrypt(pdf)
    assert ("decryption_unverified", -1, CryptRule.REPAIRED) in rules(current)
    assert earlier.flags == () and (4, 0) in earlier.streams
    assert reads.count(4) == 2                  # read afresh, not reused


def test_every_encrypted_case_in_the_library_verifies_clean(tmp_path: Path) -> None:
    from caselib import REGISTRY, load
    from caselib.run import available, build

    load()
    have = available()
    seen = 0
    for cid, case in sorted(REGISTRY.items()):
        if "encrypted" not in cid or case.bomb is not None or case.requires - have:
            continue
        data = build(case, tmp_path / f"{cid}.pdf").read_bytes()
        revisions = decrypt(data)
        if any(r.identity for r in revisions):
            seen += 1
            assert [f for r in revisions for f in r.flags] == [], cid
    assert seen >= 8


# ── Round 2 (#178): plain metadata proved, not guessed; trailer splits ──
def _metadata_file(h: Handler, stored: bytes, extra: bytes = b"") -> bytes:
    return hand_built(h, x1=b"<< /Type /Catalog /Pages 2 0 R /Metadata 8 0 R >>",
                      x8=rawpdf.stream(b"/Type /Metadata /Subtype /XML " + extra, stored))


SECRET_XMP = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>SSN 123-45-6789</x:xmpmeta>"


@pytest.mark.parametrize("plaintext", [
    SECRET_XMP + b"\xff", SECRET_XMP.decode().encode("utf-16"),
    b"<x:xmpmeta>Jos\xe9 SSN 123-45-6789</x:xmpmeta>"], ids=["trailing-ff", "utf-16", "latin-1"])
def test_ciphertext_as_plain_metadata_is_caught_whatever_it_decrypts_to(plaintext: bytes) -> None:
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    (revision,) = decrypt(_metadata_file(h, h.stream_data(8, plaintext)))
    assert rules(revision) == {("decryption_unverified", 8, CryptRule.METADATA_CIPHER)}
    assert revision.views[(8, 0)] == plaintext        # what MuPDF shows, for the matcher


@pytest.mark.parametrize("stored, extra", [
    (SECRET_XMP, b""), (SECRET_XMP.decode().encode("utf-16"), b""),
    (__import__("zlib").compress(b"<?xpacket begin=''?>" + SECRET_XMP), b"/Filter /FlateDecode")],
    ids=["utf-8", "utf-16", "flate"])
def test_plain_metadata_that_is_an_xmp_packet_is_trusted(stored: bytes, extra: bytes) -> None:
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    (revision,) = decrypt(_metadata_file(h, stored, extra))
    assert revision.flags == ()
    assert revision.streams[(8, 0)] == stored
    assert (8, 0) in revision.views                   # MuPDF's decryption of it, scanned too


@pytest.mark.parametrize("target", [b"null", None], ids=["null", "missing"])
def test_an_encrypt_entry_naming_nothing_is_a_split_not_a_crash(target: bytes | None) -> None:
    objects = {1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
               4: rawpdf.stream(b"", CONTENT), 6: rawpdf.HELVETICA}
    if target is not None:
        objects[7] = target
    pdf = rawpdf.build(objects, trailer=b" /Encrypt 7 0 R")
    (revision,) = decrypt(pdf)
    assert rules(revision) == {("decryption_unverified", -1, CryptRule.TRAILER_SPLIT)}


# ── Round 3 (#178): the packet check is not proof; its cost is bounded ──
def test_ground_ciphertext_passes_as_a_packet_and_only_views_hold_the_secret() -> None:
    # Any printable string can be ciphertext: grind the metadata object's
    # number (its key) until "123-45-6789" encrypts to printable ASCII
    # inside a well-formed packet. The check passes; MuPDF shows the secret,
    # which views carries for the matcher (3a-9b and 4a must scan it).
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    head, secret, tail = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>", b"123-45-6789", b"</x:xmpmeta>"
    for number in range(8, 200_000):
        middle = h.stream_data(number, head + secret)[len(head):]
        if all(32 <= b < 127 for b in middle):
            break
    else:
        pytest.fail("no printable ciphertext found")
    stored = head + middle + tail
    plaintext = h.stream_data(number, stored)            # RC4: what MuPDF shows
    assert secret in plaintext
    pdf = hand_built(h, x1=b"<< /Type /Catalog /Pages 2 0 R /Metadata %d 0 R >>" % number,
                     **{f"x{number}": rawpdf.stream(b"/Type /Metadata /Subtype /XML", stored)})
    (revision,) = decrypt(pdf)
    assert revision.flags == ()                          # passes as a packet: not proof
    assert secret in revision.views[(number, 0)]


@pytest.mark.parametrize("stored, extra", [
    (SECRET_XMP + b"\0" * 40, b""),
    (SECRET_XMP, b"/DecodeParms << >>"),
    (__import__("zlib").compress(SECRET_XMP) + b"\r\n", b"/Filter /FlateDecode")],
    ids=["nul-padded", "empty-parms", "flate-crlf"])
def test_packets_real_producers_write_are_trusted(stored: bytes, extra: bytes) -> None:
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    (revision,) = decrypt(_metadata_file(h, stored, extra))
    assert revision.flags == ()


def test_the_packet_check_is_charged_and_done_once_per_body() -> None:
    import zlib
    h = Handler(4, b"owner", FILE_ID, encrypt_metadata=False)
    big = zlib.compress(b"<x:xmpmeta>" + b" " * (900_000) + b"</x:xmpmeta>", 9)
    pdf = _metadata_file(h, big, b"/Filter /FlateDecode")
    for _ in range(3):
        pdf = rawpdf.incremental_update(pdf, {5: b"<< /Title " + h.string(5, b"x") + b" >>"},
                                        info=5)
        j = pdf.rindex(b"/Prev")
        pdf = pdf[:j] + h.trailer(7) + b" " + pdf[j:]
    revisions = decrypt(pdf)
    assert all(r.flags == () for r in revisions)
    # The inflated 900 KB is charged, so a cap below it stops the read...
    assert any(f.reason is FlagReason.BUDGET_EXHAUSTED
               for r in decrypt(pdf, max_bytes=800_000) for f in r.flags)
    # ...and charged once (memoized): four revisions fit in a cap that
    # holds one inflate and every revision's cuts, not two inflates.
    cuts = 4 * len(pdf) * 2
    assert all(r.flags == () for r in decrypt(pdf, max_bytes=cuts + 1_500_000))



def test_warnings_mupdf_holds_from_before_are_not_this_files() -> None:
    # MuPDF's warning buffer is process-wide: one left by an unrelated read
    # (here, a damaged file) must not flag a clean file opened next.
    import pymupdf as fitz
    try:
        fitz.open(stream=b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<< >>\n", filetype="pdf")
    except Exception:
        pass
    assert fitz.TOOLS.mupdf_warnings(reset=False)
    (revision,) = decrypt(hand_built(Handler(3, b"owner", FILE_ID)))
    assert revision.flags == ()
