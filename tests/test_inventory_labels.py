"""Labels, first part (Phase 3a-7e-1, ADR 0011 item 5): each revision's
reachable objects and STRUCTURAL streams, and the agreement gate's
REACHABLE check (coordinator decision Q5): our reachable set against what
MuPDF and libqpdf keep when they write the revision. Every file is
generated here; fabricated content only."""

from __future__ import annotations

import re
import subprocess
import sys
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from scorecard.inventory import Check, compare
from scorecard.pdfgen import Writer

from redaction_verifier.budget import Budget, Limits
from redaction_verifier.inventory import Inventory, build_inventory
from redaction_verifier.inventory.labels import (CHAIN, RevisionLabels, Structural,
                                                 StructuralKind, build_labels)
from redaction_verifier.ledger import FlagReason

from .conftest import requires_qpdf
from .steps import charged_steps

K = StructuralKind
# Lines the inventory executes per unit of work the label walk charges, at
# most (#94): measured about 11 (each edge's key path is matched, one unit);
# 15, as in the annotation tests, leaves room for a change in the mix of
# steps, not for an uncharged loop. The per-unit count must also not grow
# with the input (PER_UNIT_GROWTH).
LINES_PER_UNIT = 15
PER_UNIT_GROWTH = 1.5

CAT = b"<< /Type /Catalog /Pages 2 0 R%s >>"
PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >>%s >>"
XMP = b"<?xpacket begin=''?><x:xmpmeta xmlns:x='adobe:ns:meta/'/><?xpacket end='r'?>"


def make(catalog: bytes = b"", page: bytes = b"", objects: dict[int, bytes] | None = None,
         streams: dict[int, tuple[bytes, bytes]] | None = None, trailer: bytes = b"",
         free: tuple[int, ...] = (), updates: tuple[dict[int, bytes | None], ...] = ()) -> bytes:
    """Catalog 1 (with *catalog* inside), page tree 2, page 3 (with
    *page*), then *objects* and *streams* (number: (dictionary entries,
    data)); *free* numbers listed free; each of *updates* an incremental
    update writing those objects (None: freeing the number)."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in sorted(({1: CAT % catalog, 2: PAGES, 3: PAGE % page}
                           | (objects or {})).items()):
        table[n] = (1, w.obj(n, body), 0)
    for n, (extra, data) in sorted((streams or {}).items()):
        table[n] = (1, w.stream(n, extra, data), 0)
    for n in free:
        table[n] = (0, 0, 1)
    size = max(table) + 1
    prev = w.table(table, b"/Size %d /Root 1 0 R%s" % (size, trailer))
    w.epilogue(prev)
    for update in updates:
        found = {n: (1, w.obj(n, body), 0) if body is not None else (0, 0, 1)
                 for n, body in sorted(update.items())}
        size = max(size, max(update) + 1)
        prev = w.table(found, b"/Size %d /Root 1 0 R%s /Prev %d" % (size, trailer, prev))
        w.epilogue(prev)
    return bytes(w.out)


def _object_streams() -> bytes:
    """Members 2, 3, 4 and an unreferenced 6 in object stream 10; 5 plain;
    cross-reference stream 11."""
    w = Writer()
    at1 = w.obj(1, CAT % b"")
    at10 = w.objstm(10, [(2, PAGES), (3, PAGE % b" /Foo 4 0 R"), (4, b"<< /X 5 0 R >>"),
                         (6, b"(unused)")])
    at5 = w.obj(5, b"(five)")
    entries = {0: (0, 0, 65535), 1: (1, at1, 0), 10: (1, at10, 0), 5: (1, at5, 0),
               2: (2, 10, 0), 3: (2, 10, 1), 4: (2, 10, 2), 6: (2, 10, 3)}
    w.epilogue(w.xref_stream(11, entries, b"/Size 12 /Root 1 0 R"))
    return bytes(w.out)


def _hybrid() -> bytes:
    """A table whose /XRefStm (object 11) lists 4, compressed in object
    stream 10; nothing references 4."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in {1: CAT % b"", 2: PAGES, 3: PAGE % b""}.items():
        table[n] = (1, w.obj(n, body), 0)
    table[10] = (1, w.objstm(10, [(4, b"<< /Hidden true >>")]), 0)
    stm = w.xref_stream(11, {4: (2, 10, 0)}, b"/Size 12")
    w.epilogue(w.table(table, b"/Size 12 /Root 1 0 R /XRefStm %d" % stm))
    return bytes(w.out)


def _generation() -> bytes:
    """Object 4 of generation 1, referencing itself and the catalog."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in {1: CAT % b" /X 4 1 R", 2: PAGES, 3: PAGE % b""}.items():
        table[n] = (1, w.obj(n, body), 0)
    table[4] = (1, w.at(), 1)
    w.out += b"4 1 obj\n<< /Self 4 1 R /Up 1 0 R >>\nendobj\n"
    w.epilogue(w.table(table, b"/Size 5 /Root 1 0 R"))
    return bytes(w.out)


def _xref_stream_trailer() -> bytes:
    """A cross-reference stream (4) whose dictionary, the trailer, names /Info 5."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in {1: CAT % b"", 2: PAGES, 3: PAGE % b"", 5: b"<< /Title (t) >>"}.items():
        table[n] = (1, w.obj(n, body), 0)
    w.epilogue(w.xref_stream(4, table, b"/Size 6 /Root 1 0 R /Info 5 0 R"))
    return bytes(w.out)


def _indirect_length(shared: bool = False) -> bytes:
    """Stream 4 (page 3's contents) whose /Length is object 5 -- also the
    catalog's /N when *shared*."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    catalog = CAT % (b" /N 5 0 R" if shared else b"")
    for n, body in {1: catalog, 2: PAGES, 3: PAGE % b" /Contents 4 0 R"}.items():
        table[n] = (1, w.obj(n, body), 0)
    table[4] = (1, w.obj(4, b"<< /Length 5 0 R >>\nstream\n0 0 m\nendstream"), 0)
    table[5] = (1, w.obj(5, b"5"), 0)
    w.epilogue(w.table(table, b"/Size 6 /Root 1 0 R"))
    return bytes(w.out)


FLATE = zlib.compress(b"0 0 m")

# (name, file, each revision's reachable numbers, newest first)
REACHABLE: list[tuple[str, bytes, list[list[int]]]] = [
    # Unreferenced stream 4: not reachable. A thumbnail and /PieceInfo
    # private data are references like any other: reachable (ADR 0011
    # item 5: they are orphaned by having no other label, not by this).
    ("thumbnail-and-private-data", make(
        page=b" /Thumb 5 0 R /PieceInfo << /X << /Private 7 0 R >> >>",
        streams={4: (b"", b"x"), 5: (b"", b"y"), 7: (b"", b"z")}), [[1, 2, 3, 5, 7]]),
    # Field 6 is reached only through annotation 4's /Parent (not in
    # /Fields): both readers follow /Parent (coordinator decision Q1).
    ("parent-only", make(page=b" /Annots [4 0 R]", objects={
        4: b"<< /Type /Annot /Subtype /Square /Rect [0 0 9 9] /Parent 6 0 R >>",
        6: b"<< /T (f) /Kids [4 0 R] >>"}), [[1, 2, 3, 4, 6]]),
    # A stream's indirect /Length: read by both readers, though their
    # writers write it direct (the gate adds it back).
    ("indirect-length", _indirect_length(), [[1, 2, 3, 4, 5]]),
    ("shared-length", _indirect_length(shared=True), [[1, 2, 3, 4, 5]]),
    ("length-key-elsewhere", make(catalog=b" /D << /Length 5 0 R >>",
                                  page=b" /Contents 4 0 R /Length 6 0 R",
                                  objects={5: b"5", 6: b"6", 7: b"7"},
                                  streams={4: (b" /X << /Length 7 0 R >>", b"0 0 m")}),
     [[1, 2, 3, 4, 5, 6, 7]]),
    # Every trailer key is an edge: /Info and keys no reader names.
    ("trailer-keys", make(objects={4: b"<< /Title (t) >>", 5: b"<< /X 1 >>", 6: b"(id)"},
                          trailer=b" /Info 4 0 R /Foo 5 0 R /Bar [6 0 R]"),
     [[1, 2, 3, 4, 5, 6]]),
    ("xref-stream-trailer", _xref_stream_trailer(), [[1, 2, 3, 5]]),
    # /ID by reference (two corpus files): qpdf's writer writes a new one;
    # its elements by reference: MuPDF's writes a new second element.
    ("id-elements-by-reference", make(
        objects={6: b"<C3D565188EE7777BBA7478BC964D1499>",
                 7: b"<C3D565188EE7777BBA7478BC964D1499>"},
        trailer=b" /ID [6 0 R 7 0 R]"), [[1, 2, 3, 6, 7]]),
    ("id-by-reference", make(objects={4: b"[<C3D565188EE7777BBA7478BC964D1499>"
                                         b" <C3D565188EE7777BBA7478BC964D1499>]"},
                             trailer=b" /ID 4 0 R"), [[1, 2, 3, 4]]),
    ("free-entry", make(free=(4,)), [[1, 2, 3]]),
    # Object and cross-reference streams: no reference reaches them.
    ("object-stream", _object_streams(), [[1, 2, 3, 4, 5]]),
    ("hybrid", _hybrid(), [[1, 2, 3]]),
    ("generation", _generation(), [[1, 2, 3, 4]]),
    # Per revision: the update drops /Foo, so 4 is reachable only before;
    # 5 is new and unreferenced.
    ("update", make(page=b" /Foo 4 0 R", objects={4: b"(old)"},
                    updates=({3: PAGE % b"", 5: b"(new)"},)), [[1, 2, 3], [1, 2, 3, 4]]),
    # Stream dictionaries' references: a filter and its parameters by
    # reference; /DecodeParms without /Filter (qpdf's writer drops it).
    ("filter-by-reference", make(
        page=b" /Contents [4 0 R 8 0 R]",
        objects={5: b"<< /Columns 1 >>", 6: b"/FlateDecode", 7: b"[/FlateDecode]"},
        streams={4: (b" /Filter 6 0 R /DecodeParms 5 0 R", FLATE), 8: (b" /Filter 7 0 R", FLATE)}),
     [[1, 2, 3, 4, 5, 6, 7, 8]]),
    ("decode-parms-without-filter", make(
        catalog=b" /Metadata 4 0 R", objects={5: b"<< /Columns 1 >>"},
        streams={4: (b" /Type /Metadata /Subtype /XML /DecodeParms 5 0 R", XMP)}),
     [[1, 2, 3, 4, 5]]),
]


def _inventory(data: bytes, limits: Limits | None = None) -> Inventory:
    return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)))


def _labels(inv: Inventory, revision: int = 0) -> RevisionLabels:
    found = inv.labels[revision]
    assert found is not None
    return found


def _kinds(inv: Inventory, revision: int = 0) -> dict[int | None, set[K]]:
    """Each structural stream's object number and kinds."""
    return {unit.obj: {r.kind for r in records}
            for unit, records in _labels(inv, revision).structural.items()}


# ── Reachable, against both readers (the gate's REACHABLE check) ──────────
@requires_qpdf
@pytest.mark.parametrize(("data", "expected"), [(d, e) for _, d, e in REACHABLE],
                         ids=[name for name, _, _ in REACHABLE])
def test_reachable_is_what_both_readers_keep(tmp_path: Path, data: bytes,
                                             expected: list[list[int]]) -> None:
    inv = _inventory(data)
    assert inv.flags == ()  # labels add no flag (R3)
    assert [sorted(_labels(inv, r).reachable) for r in range(inv.revisions)] == expected
    found = compare(data, tmp_path, inv=inv)
    assert found.agrees and found.reachable_compared == inv.revisions, found


@requires_qpdf
def test_reachable_in_an_encrypted_file_agrees(tmp_path: Path) -> None:
    # The readers write it unencrypted: the gate adds the /Encrypt
    # dictionary back as each reads the trailer.
    import pikepdf
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(make(objects={4: b"<< /Title (t) >>"}, trailer=b" /Info 4 0 R"))
    with pikepdf.open(plain) as pdf:
        pdf.save(tmp_path / "enc.pdf", encryption=pikepdf.Encryption(owner="o", user="", R=4),
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    data = (tmp_path / "enc.pdf").read_bytes()
    inv = _inventory(data)
    assert inv.flags == ()
    encrypt = [e.num for e in inv.graph.trailers[0] if e.path == (b"Encrypt",)]
    assert encrypt and set(encrypt) <= _labels(inv).reachable
    found = compare(data, tmp_path, inv=inv)
    assert found.agrees and found.encrypted and found.reachable_compared == 1, found


@requires_qpdf
def test_an_embedded_pdf_with_its_own_table_agrees(tmp_path: Path) -> None:
    # An uncompressed attachment holding a whole PDF (a case-library case):
    # MuPDF's written file then holds two tables; the gate reads the one
    # its final startxref names.
    data = make(catalog=b" /Names << /EmbeddedFiles << /Names [(a.pdf) 6 0 R] >> >>",
                objects={6: b"<< /Type /Filespec /F (a.pdf) /EF << /F 7 0 R >> >>"},
                streams={7: (b" /Type /EmbeddedFile", make())})
    inv = _inventory(data)
    assert inv.flags == () and _kinds(inv) == {7: {K.EMBEDDED_FILE}}
    found = compare(data, tmp_path, inv=inv)
    assert found.agrees and found.reachable_compared == 1, found


def _with_reachable(inv: Inventory, revision: int, numbers: frozenset[int]) -> Inventory:
    labels = list(inv.labels)
    labels[revision] = replace(_labels(inv, revision), reachable=numbers)
    return replace(inv, labels=tuple(labels))


@requires_qpdf
def test_the_gate_fails_closed_on_a_reachable_set_one_off(tmp_path: Path) -> None:
    data = REACHABLE[0][1]
    inv = _inventory(data)
    ours = _labels(inv).reachable
    assert compare(data, tmp_path, inv=inv).agrees
    for numbers, differs in ((ours | {4}, 4), (ours - {5}, 5), (ours | {99}, 99),
                             (ours - {1}, 1)):
        found = compare(data, tmp_path, inv=_with_reachable(inv, 0, numbers))
        assert found.disagrees and found.check is Check.REACHABLE, found
        assert found.obj == differs and found.templates == ("mupdf",), found
    # An older revision too: 4 is reachable there only.
    data = REACHABLE[[name for name, _, _ in REACHABLE].index("update")][1]
    inv = _inventory(data)
    found = compare(data, tmp_path, inv=_with_reachable(inv, 1, _labels(inv, 1).reachable - {4}))
    assert found.disagrees and found.check is Check.REACHABLE and found.revision == 1, found
    # No labels at all: never on an unflagged file, and fails closed.
    found = compare(data, tmp_path, inv=replace(inv, labels=(None, inv.labels[1])))
    assert found.disagrees and found.check is Check.REACHABLE and found.revision == 0, found


@requires_qpdf
def test_the_gate_fails_closed_where_a_writer_drops_more_than_a_leaf(tmp_path: Path) -> None:
    # qpdf's writer drops /DecodeParms without /Filter. Its reference (5)
    # is added back as libqpdf reads it -- exact while 5 holds no reference
    # of its own; here it does (6), which the writer drops too: the
    # reader's set is not exact there, so a disagreement, not an allowance.
    parms = b" /Type /Metadata /Subtype /XML /DecodeParms << /G 5 0 R >>"
    data = make(catalog=b" /Metadata 4 0 R", objects={5: b"(globals)"},
                streams={4: (parms, XMP)})
    assert compare(data, tmp_path).agrees
    data = make(catalog=b" /Metadata 4 0 R", objects={5: b"<< /G 6 0 R >>", 6: b"(globals)"},
                streams={4: (parms, XMP)})
    inv = _inventory(data)
    assert inv.flags == () and _labels(inv).reachable == {1, 2, 3, 4, 5, 6}
    found = compare(data, tmp_path, inv=inv)
    assert found.disagrees and found.check is Check.REACHABLE, found
    assert found.templates == ("libqpdf", "not exact"), found


# ── STRUCTURAL by the chain ───────────────────────────────────────────────
def test_cross_reference_and_object_streams_are_structural_by_the_chain() -> None:
    inv = _inventory(_object_streams())
    assert inv.flags == ()
    assert _kinds(inv) == {10: {K.OBJECT_STREAM}, 11: {K.XREF_STREAM}}
    assert all(r == Structural(r.kind, CHAIN, b"")
               for records in _labels(inv).structural.values() for r in records)
    inv = _inventory(_hybrid())  # a hybrid's /XRefStm
    assert inv.flags == () and _kinds(inv) == {10: {K.OBJECT_STREAM}, 11: {K.XREF_STREAM}}


def _streams_revisions(rewrite_home: bool) -> bytes:
    """Revision 1: object stream 10 holds 4 (referenced by the catalog);
    cross-reference stream 11. Revision 0, a cross-reference stream 13:
    4 is rewritten plain (10 is then home to nothing, still in use), or
    with *rewrite_home* object stream 12 replaces 10 as 4's home."""
    w = Writer()
    at1 = w.obj(1, CAT % b" /X 4 0 R")
    at2, at3 = w.obj(2, PAGES), w.obj(3, PAGE % b"")
    at10 = w.objstm(10, [(4, b"(packed)")])
    entries = {0: (0, 0, 65535), 1: (1, at1, 0), 2: (1, at2, 0), 3: (1, at3, 0),
               10: (1, at10, 0), 4: (2, 10, 0)}
    prev = w.xref_stream(11, entries, b"/Size 12 /Root 1 0 R")
    w.epilogue(prev)
    if rewrite_home:
        update = {12: (1, w.objstm(12, [(4, b"(repacked)")]), 0), 4: (2, 12, 0)}
    else:
        update = {4: (1, w.obj(4, b"(plain)"), 0)}
    w.epilogue(w.xref_stream(13, update, b"/Size 14 /Root 1 0 R /Prev %d" % prev))
    return bytes(w.out)


@pytest.mark.parametrize("rewrite_home", [False, True])
def test_the_chain_is_per_revision(rewrite_home: bool) -> None:
    inv = _inventory(_streams_revisions(rewrite_home))
    assert inv.flags == ()
    # The older revision: its own chain only, 10 home to 4.
    assert _kinds(inv, 1) == {10: {K.OBJECT_STREAM}, 11: {K.XREF_STREAM}}
    # The newer: both cross-reference streams; an /ObjStm home to nothing
    # gets no label (coordinator decision Q4), the new home does.
    homes = {12: {K.OBJECT_STREAM}} if rewrite_home else {}
    assert _kinds(inv) == {11: {K.XREF_STREAM}, 13: {K.XREF_STREAM}} | homes


@requires_qpdf
@pytest.mark.parametrize("updated", [False, True])
def test_the_hint_stream_is_structural_stale_or_not(tmp_path: Path, updated: bool) -> None:
    import pymupdf
    (tmp_path / "in.pdf").write_bytes(make(page=b" /Contents 4 0 R",
                                           streams={4: (b"", b"0 0 m")}))
    subprocess.run(["qpdf", "--linearize", "--object-streams=disable", str(tmp_path / "in.pdf"),
                    str(tmp_path / "lin.pdf")], check=True, timeout=60)
    if updated:  # an update leaves /L stale (coordinator decision Q8: still structural)
        doc = pymupdf.open(tmp_path / "lin.pdf")
        doc[0].insert_text((50, 90), "fabricated")
        doc.save(tmp_path / "lin.pdf", incremental=True, encryption=0)
    data = (tmp_path / "lin.pdf").read_bytes()
    inv = _inventory(data)
    assert inv.flags == () and inv.revisions == 1 + updated
    found = re.search(rb"/H \[ ?(\d+) ", data)
    assert found is not None
    hint = inv.objects[int(found.group(1))]
    for revision in range(inv.revisions):
        records = _labels(inv, revision).structural[hint]
        assert records == (Structural(K.HINT_STREAM, CHAIN, b""),)
        assert hint.obj not in _labels(inv, revision).reachable  # nothing references it
    assert compare(data, tmp_path, inv=inv).agrees
    # /H off an object's start: no hint stream (the linearization
    # dictionary is not checked further; the chain still accepts the pair).
    offset = found.group(1)
    moved = b"%0*d" % (len(offset), int(offset) + 1)
    forged = data[:found.start(1)] + moved + data[found.end(1):]
    inv = _inventory(forged)
    assert all(K.HINT_STREAM not in kinds for kinds in _kinds(inv).values())
    # /H naming a stream something references (the page's contents): not
    # the hint stream (item 5: nothing references it).
    inv = _inventory(data)
    drawn = [u for u in inv.objects.values() if u.start in inv.stream_data
             and u.obj in _labels(inv).reachable]
    assert drawn and len(str(drawn[0].start)) <= len(offset)
    forged = data[:found.start(1)] + b"%0*d" % (len(offset), drawn[0].start) + data[found.end(1):]
    inv = _inventory(forged)
    assert inv.flags == ()
    assert all(K.HINT_STREAM not in kinds for kinds in _kinds(inv).values())


# ── STRUCTURAL by key, from reachable objects ─────────────────────────────
def _js(where: bytes) -> dict[int, bytes]:
    return {6: b"<< /S /JavaScript /JS 7 0 R >>"} if where else {}


PROFILE = (b" /N 3", b"icc")
# (name, file, structural streams of the current revision: number -> kinds)
BY_KEY: list[tuple[str, bytes, dict[int | None, set[K]]]] = [
    ("xmp", make(catalog=b" /Metadata 4 0 R",
                 streams={4: (b" /Type /Metadata /Subtype /XML", XMP)}), {4: {K.XMP}}),
    ("xmp-on-a-page", make(page=b" /Metadata 4 0 R", streams={4: (b"", XMP)}), {4: {K.XMP}}),
    # The holder's /Type by reference (read through one reference).
    ("xmp-holder-type-by-reference", make(catalog=b" /Extra 9 0 R",
                                          objects={9: b"<< /Type 10 0 R /Metadata 4 0 R >>",
                                                   10: b"/Page"},
                                          streams={4: (b"", XMP)}), {4: {K.XMP}}),
    # An image XObject holder: no allowlisted /Type, its /Subtype /Image.
    ("xmp-on-an-image", make(objects={3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                                         b" /Resources << /XObject << /Im1 9 0 R >> >> >>"},
                             streams={9: (b" /Subtype /Image /Width 1 /Height 1 /ColorSpace"
                                          b" /DeviceGray /BitsPerComponent 8 /Metadata 4 0 R",
                                          b"\x00"),
                                      4: (b"", XMP)}), {4: {K.XMP}}),
    # Unreachable source: its /Metadata leads to nothing structural.
    ("xmp-under-an-unreachable-object", make(objects={5: b"<< /Metadata 4 0 R >>"},
                                             streams={4: (b"", XMP)}), {}),
    # Image or form keys: never structural, whatever the key.
    ("xmp-with-width", make(catalog=b" /Metadata 4 0 R",
                            streams={4: (b" /Width 1", XMP)}), {}),
    ("xmp-with-bbox", make(catalog=b" /Metadata 4 0 R",
                           streams={4: (b" /BBox [0 0 1 1]", XMP)}), {}),
    ("xmp-subtype-form", make(catalog=b" /Metadata 4 0 R",
                              streams={4: (b" /Subtype /Form /Resources << >>", XMP)}), {}),
    ("xmp-subtype-image-by-reference", make(catalog=b" /Metadata 4 0 R",
                                            objects={5: b"/Image"},
                                            streams={4: (b" /Subtype 5 0 R", XMP)}), {}),
    ("xmp-not-a-stream", make(catalog=b" /Metadata 4 0 R", objects={4: b"<< >>"}), {}),
    ("javascript", make(catalog=b" /OpenAction 6 0 R", objects=_js(b"y"),
                        streams={7: (b"", b"app.alert(1)")}), {7: {K.JAVASCRIPT}}),
    ("javascript-unreachable", make(objects=_js(b"y"), streams={7: (b"", b"app.alert(1)")}), {}),
    ("javascript-as-a-string", make(catalog=b" /OpenAction << /S /JavaScript /JS (x) >>"), {}),
    ("embedded-file", make(
        catalog=b" /Names << /EmbeddedFiles << /Names [(a.txt) 6 0 R] >> >>",
        objects={6: b"<< /Type /Filespec /F (a.txt) /EF << /F 7 0 R /UF 7 0 R >> >>"},
        streams={7: (b" /Type /EmbeddedFile", b"fabricated")}), {7: {K.EMBEDDED_FILE}}),
    ("embedded-file-ef-by-reference", make(
        catalog=b" /Names << /EmbeddedFiles << /Names [(a.txt) 6 0 R] >> >>",
        objects={6: b"<< /Type /Filespec /F (a.txt) /EF 8 0 R >>", 8: b"<< /F 7 0 R >>"},
        streams={7: (b" /Type /EmbeddedFile", b"fabricated")}), {7: {K.EMBEDDED_FILE}}),
    ("embedded-file-as-a-form", make(
        catalog=b" /Names << /EmbeddedFiles << /Names [(a.txt) 6 0 R] >> >>",
        objects={6: b"<< /Type /Filespec /F (a.txt) /EF << /F 7 0 R >> >>"},
        streams={7: (b" /Subtype /Form /BBox [0 0 1 1] /Resources << >>", b"")}), {}),
    ("output-intent", make(
        catalog=b" /OutputIntents [<< /S /GTS_PDFA1 /DestOutputProfile 8 0 R >>]",
        streams={8: PROFILE}), {8: {K.OUTPUT_INTENT}}),
    ("output-intent-items-by-reference", make(
        catalog=b" /OutputIntents 5 0 R",
        objects={5: b"[6 0 R]", 6: b"<< /S /GTS_PDFA1 /DestOutputProfile 8 0 R >>"},
        streams={8: PROFILE}), {8: {K.OUTPUT_INTENT}}),
    ("output-intent-item-by-reference", make(
        catalog=b" /OutputIntents [6 0 R]",
        objects={6: b"<< /S /GTS_PDFA1 /DestOutputProfile 8 0 R >>"},
        streams={8: PROFILE}), {8: {K.OUTPUT_INTENT}}),
    # A page's /OutputIntents (PDF 2.0) is not the catalog's: not in the list.
    ("output-intent-on-a-page", make(
        page=b" /OutputIntents [<< /S /GTS_PDFA1 /DestOutputProfile 8 0 R >>]",
        streams={8: PROFILE}), {}),
    ("profile-elsewhere", make(catalog=b" /X << /DestOutputProfile 8 0 R >>",
                               streams={8: PROFILE}), {}),
    # Several labels on one stream: each record kept.
    ("xmp-and-javascript", make(catalog=b" /Metadata 7 0 R /OpenAction 6 0 R",
                                objects=_js(b"y"), streams={7: (b"", XMP)}),
     {7: {K.XMP, K.JAVASCRIPT}}),
]


@pytest.mark.parametrize(("data", "expected"), [(d, e) for _, d, e in BY_KEY],
                         ids=[name for name, _, _ in BY_KEY])
def test_structural_by_key_from_a_reachable_object(data: bytes,
                                                   expected: dict[int | None, set[K]]) -> None:
    inv = _inventory(data)
    assert inv.flags == ()  # labels add no flag (R3)
    assert _kinds(inv) == expected


def test_a_record_names_its_source_and_key() -> None:
    inv = _inventory(BY_KEY[[n for n, _, _ in BY_KEY].index("xmp-and-javascript")][1])
    (records,) = _labels(inv).structural.values()
    assert set(records) == {Structural(K.XMP, 1, b"Metadata"),
                            Structural(K.JAVASCRIPT, 6, b"JS")}


@requires_qpdf
def test_the_by_key_fixtures_agree_with_the_gate(tmp_path: Path) -> None:
    # Every fixture above is a file both readers read as we do.
    for name, data, _ in BY_KEY:
        found = compare(data, tmp_path)
        assert found.agrees and found.reachable_compared == 1, (name, found)


def test_a_key_outside_the_list_leads_nowhere_structural() -> None:
    # Named like structural streams, but under keys outside the list: no
    # label from a declared type alone (ADR 0011 item 5).
    data = make(catalog=b" /Meta 4 0 R /Script 5 0 R /Profile 6 0 R",
                streams={4: (b" /Type /Metadata /Subtype /XML", XMP),
                         5: (b" /Type /EmbeddedFile", b"x"), 6: PROFILE})
    inv = _inventory(data)
    assert inv.flags == () and _kinds(inv) == {}
    assert {4, 5, 6} <= _labels(inv).reachable


# ── Across revisions: shared by identity, walked again when read ──────────
def test_revisions_that_read_nothing_new_share_their_labels() -> None:
    # Each update rewrites object 9, which nothing references: shared.
    data = make(catalog=b" /Metadata 4 0 R", streams={4: (b"", XMP)},
                updates=tuple({9: b"(%d)" % i} for i in range(3)))
    inv = _inventory(data)
    assert inv.flags == ()
    assert all(found is inv.labels[-1] for found in inv.labels)
    # Rewriting a reachable object walks the revision again.
    data = make(catalog=b" /Metadata 4 0 R", streams={4: (b"", XMP)},
                updates=({3: PAGE % b" /Foo 5 0 R", 5: b"(new)"},))
    inv = _inventory(data)
    assert inv.labels[0] is not inv.labels[1]
    assert _labels(inv, 0).reachable == {1, 2, 3, 4, 5}
    assert _labels(inv, 1).reachable == {1, 2, 3, 4}


def test_a_new_cross_reference_stream_shares_the_reachable_set() -> None:
    # Revision 0 adds a cross-reference stream and rewrites nothing read:
    # a new record (one more structural stream), the same reachable set.
    inv = _inventory(_streams_revisions(rewrite_home=False))
    assert _labels(inv, 0).structural != _labels(inv, 1).structural
    w = Writer()
    at = {n: w.obj(n, body) for n, body in {1: CAT % b"", 2: PAGES, 3: PAGE % b""}.items()}
    entries = {0: (0, 0, 65535)} | {n: (1, a, 0) for n, a in at.items()}
    prev = w.xref_stream(4, entries, b"/Size 5 /Root 1 0 R")
    w.epilogue(prev)
    at9 = w.obj(9, b"(unread)")
    w.epilogue(w.xref_stream(10, {9: (1, at9, 0)}, b"/Size 11 /Root 1 0 R /Prev %d" % prev))
    inv = _inventory(bytes(w.out))
    assert inv.flags == ()
    assert inv.labels[0] is not inv.labels[1]
    assert _labels(inv, 0).reachable is _labels(inv, 1).reachable
    assert _kinds(inv, 0) == {4: {K.XREF_STREAM}, 10: {K.XREF_STREAM}}
    assert _kinds(inv, 1) == {4: {K.XREF_STREAM}}


def test_a_revision_without_a_page_list_has_no_labels() -> None:
    # The middle revision's page tree is flagged (no page list, no walk,
    # and the next walks afresh: the review of #140's rule) and adds
    # /Metadata; the newest restores the tree.
    data = make(updates=({1: CAT % b" /Metadata 4 0 R", 2: PAGES.replace(b"1 >>", b"2 >>"),
                          4: b"(not a stream)"},
                         {2: PAGES}))
    inv = _inventory(data)
    assert {f.reason for f in inv.flags} == {FlagReason.PAGE_TREE}
    assert inv.labels[1] is None and inv.labels[0] is not None
    assert _labels(inv, 0).reachable == {1, 2, 3, 4}
    assert _labels(inv, 2).reachable == {1, 2, 3}


def test_a_spent_budget_leaves_the_rest_unlabelled() -> None:
    data = make(catalog=b" /Metadata 4 0 R", streams={4: (b"", XMP)},
                updates=({3: PAGE % b" /Foo 5 0 R", 5: b"(new)"},))
    whole = Budget(file_size=len(data))
    build_inventory(data, budget=whole)
    # Just short of the whole: the labels, built last, stop first (the
    # current revision, walked last).
    limits = Limits(work_floor=whole.work - 1, work_per_byte=0)
    inv = _inventory(data, limits)
    assert {f.reason for f in inv.flags} == {FlagReason.BUDGET_EXHAUSTED}
    assert inv.labels[0] is None and inv.labels[1] is not None
    assert inv.annotations[0] is not None  # cut in the labels, not before


def test_graph_work_counts_the_graph_stages() -> None:
    data = REACHABLE[0][1]
    inv = _inventory(data)
    whole = Budget(file_size=len(data))
    build_inventory(data, budget=whole)
    assert 0 < inv.graph_work < whole.work


def test_build_labels_runs_on_its_own() -> None:
    # No page list: nothing read, no labels.
    assert build_labels(1, [[]], lambda n, r: None, lambda n, r: None, {}, ((),), (None,),
                        [[]], None, Budget()) == (None,)


# ── Linearity: long chains, wide fan-outs, many revisions (#94) ───────────
def _chain(n: int) -> bytes:
    """A chain n objects long from the catalog, each also holding /Metadata
    to a shared XMP stream and a /Parent back to the catalog."""
    objects = {10 + i: b"<< /Next %d 0 R /Parent 1 0 R /Metadata 4 0 R >>" % (11 + i)
               for i in range(n - 1)}
    objects[10 + n - 1] = b"<< /End true >>"
    return make(catalog=b" /Next 10 0 R", objects=objects, streams={4: (b"", XMP)})


def _fan(n: int) -> bytes:
    """One array of n references to n leaves, each referencing three
    shared objects 16 times: many edges to numbers already reached."""
    items = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    shared = b" ".join([b"5 0 R 6 0 R 7 0 R 5 0 R"] * 4)
    objects = {10 + i: b"<< /A [%s] >>" % shared for i in range(n)}
    objects |= {5: b"(a)", 6: b"(b)", 7: b"(c)"}
    return make(catalog=b" /All [%s]" % items, objects=objects)


def _revisions(n: int, touch: bool) -> bytes:
    """n objects reachable from the catalog, and n updates, each rewriting
    one reachable object to reference a new one (*touch*: every revision
    walks again, and its reachable set differs) or rewriting one nothing
    references (shared)."""
    items = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    objects = {10 + i: b"<< /I %d >>" % i for i in range(n)}
    updates = tuple({10 + i: b"<< /I %d /New %d 0 R >>" % (i, 10 + n + i),
                     10 + n + i: b"(new)"} if touch else {9: b"(%d)" % i}
                    for i in range(n))
    return make(catalog=b" /All [%s] /Metadata 4 0 R" % items, objects=objects,
                streams={4: (b"", XMP)}, updates=updates)


def _stream_revisions(n: int) -> bytes:
    """n updates, each with its own cross-reference stream and object
    stream (home to one new object the catalog does not reference): the
    chain's records grow with every revision."""
    w = Writer()
    at = {n_: w.obj(n_, body) for n_, body in {1: CAT % b"", 2: PAGES, 3: PAGE % b""}.items()}
    entries = {0: (0, 0, 65535)} | {k: (1, a, 0) for k, a in at.items()}
    prev = w.xref_stream(4, entries, b"/Size 5 /Root 1 0 R")
    w.epilogue(prev)
    size = 5
    for i in range(n):
        home, member, xref = size, size + 1, size + 2
        size += 3
        update = {home: (1, w.objstm(home, [(member, b"(%d)" % i)]), 0), member: (2, home, 0)}
        prev = w.xref_stream(xref, update, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(prev)
    return bytes(w.out)


@pytest.fixture
def walk_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The walk's own charges only: the retained-memory charges (tested on
    their own, test_labels_retain_at_most_a_byte_per_unit_of_work) would
    otherwise dilute an uncharged loop's lines below the per-unit bound."""
    from redaction_verifier.inventory import labels
    for name in ("RECORD_WORK", "SET_WORK", "BY_KEY_WORK", "LABELS_WORK", "LIST_WORK"):
        monkeypatch.setattr(labels, name, 0)


def _holders(n: int) -> bytes:
    """n XMP holders, each also holding 32 nested direct dictionaries its
    holder map walks."""
    junk = b" ".join([b"<< /A << >> >>"] * 16)
    objects = {10 + i: b"<< /Type /Page /Metadata 4 0 R /J [%s] >>" % junk for i in range(n)}
    items = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    return make(catalog=b" /All [%s]" % items, objects=objects, streams={4: (b"", XMP)})


@pytest.mark.parametrize("make_file", [_chain, _fan, lambda n: _revisions(n, False),
                                       lambda n: _catalog_rewrites(n), lambda n: _xref_only(n),
                                       _holders],
                         ids=["chain", "fan", "shared-revisions", "catalog-rewrites", "xref-only",
                              "holders"])
def test_the_label_walk_is_linear(make_file: object, walk_only: None) -> None:
    work = []
    for n in (100, 800):
        charged, lines, inv = charged_steps(make_file(n), "build_labels")  # type: ignore[operator]
        assert inv.flags == ()
        work.append((charged, lines))
    _per_unit(work)
    assert work[1][0] <= 10 * work[0][0], work


def test_comparing_rewritten_bodies_is_charged(walk_only: None) -> None:
    # Each update rewrites a catalog listing every object: its references
    # are compared with the old body's, every one charged (the file grows
    # with them too: work linear in the file, not in n).
    work = []
    for n in (40, 320):
        charged, lines, inv = charged_steps(_catalog_rewrites(n, wide=True), "build_labels")
        assert inv.flags == () and all(found is inv.labels[0] for found in inv.labels)
        work.append((charged, lines))
    _per_unit(work)


def test_shared_revisions_cost_no_walk() -> None:
    inv = _inventory(_revisions(40, touch=False))
    assert all(found is inv.labels[0] for found in inv.labels)


@pytest.mark.parametrize("make_file", [lambda n: _revisions(n, True), _stream_revisions],
                         ids=["walked-revisions", "stream-revisions"])
def test_revisions_that_walk_again_are_charged_and_bounded(make_file: object,
                                                            walk_only: None) -> None:
    # Every revision walks again (or its chain grows): revisions x objects,
    # by design (ADR 0011 item 7) -- all of it charged, so the work grows as
    # the product, and the budget bounds it: flagged, never hanging.
    work = []
    for n in (40, 320):
        data = make_file(n)  # type: ignore[operator]
        charged, lines, inv = charged_steps(data, "build_labels")
        reasons = {f.reason for f in inv.flags}
        assert reasons <= {FlagReason.BUDGET_EXHAUSTED}, reasons
        if not reasons:
            assert all(found is not None for found in inv.labels)
        work.append((charged, lines))
        size = len(data)
    _per_unit(work)
    (w_s, _), (w_l, _) = work
    assert w_l >= 16 * w_s or w_l >= 0.99 * Limits().work_limit(size), work


def test_a_revision_walked_again_to_the_same_labels_shares_them() -> None:
    # Each update rewrites the catalog as it was (an update's usual shape):
    # walked again, the same labels -- shared, nothing new retained.
    data = make(catalog=b" /Metadata 4 0 R", streams={4: (b"", XMP)},
                updates=tuple({1: CAT % b" /Metadata 4 0 R"} for _ in range(5)))
    inv = _inventory(data)
    assert inv.flags == ()
    assert all(found is inv.labels[-1] for found in inv.labels)


@pytest.mark.parametrize(("update", "kinds", "reachable"), [
    # The same references under another key: the record goes.
    ({1: CAT % b" /Meta 4 0 R"}, {}, {1, 2, 3, 4}),
    # A by-key target rewritten with the same references, now with /Width.
    ({4: b"<< /Width 1 /Length 1 >>\nstream\nx\nendstream"}, {}, {1, 2, 3, 4}),
    # A reachable object rewritten to reference something new.
    ({3: PAGE % b" /Foo 5 0 R", 5: b"(new)"}, {4: {K.XMP}}, {1, 2, 3, 4, 5}),
    # The same references under another key, on a body holding none of
    # item 5's keys: the catalog's /OutputIntents renamed.
    ({1: CAT % b" /Metadata 4 0 R /OutputIntentz [<< /DestOutputProfile 8 0 R >>]"},
     {4: {K.XMP}}, {1, 2, 3, 4, 8}),
], ids=["key-renamed", "target-rewritten", "new-reference", "intents-renamed"])
def test_a_rewritten_object_is_read_again_unless_its_references_are_the_same(
        update: dict[int, bytes | None], kinds: dict[int | None, set[K]],
        reachable: set[int]) -> None:
    intents = b" /OutputIntents [<< /DestOutputProfile 8 0 R >>]" if 8 in reachable else b""
    data = make(catalog=b" /Metadata 4 0 R" + intents, streams={4: (b"", XMP), 8: PROFILE},
                updates=(update,))
    inv = _inventory(data)
    assert inv.flags == ()
    assert _kinds(inv, 1) == {4: {K.XMP}} | ({8: {K.OUTPUT_INTENT}} if intents else {})
    assert _kinds(inv, 0) == kinds
    assert _labels(inv, 0).reachable == reachable


def test_a_revision_walked_again_to_the_same_set_keeps_it_once() -> None:
    # A key renamed on the page: walked again (its references' paths
    # changed), to the same labels -- shared, not kept twice.
    data = make(page=b" /Foo 5 0 R", objects={5: b"(x)"}, updates=({3: PAGE % b" /Bar 5 0 R"},))
    inv = _inventory(data)
    assert inv.flags == () and inv.labels[0] is inv.labels[1]


def _catalog_rewrites(n: int, wide: bool = False) -> bytes:
    """n objects reachable from the catalog (listed in it when *wide*, so
    each rewritten catalog's references are compared), and n updates each
    rewriting the catalog with the same references: not walked again."""
    items = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    held = b"[%s]" % items if wide else b"6 0 R"
    # Wide, the catalog holds none of item 5's keys: only its references
    # are compared (no holder map made of it).
    meta = b"" if wide else b" /Metadata 4 0 R"
    catalog = CAT % (b" /All %s%s%%s" % (held, meta))
    return make(catalog=b" /All %s%s" % (held, meta),
                objects={6: b"[%s]" % items} | {10 + i: b"<< /I %d >>" % i for i in range(n)},
                streams={4: (b"", XMP)},
                updates=tuple({1: catalog % (b" /PageMode /UseNone" if i % 2 else b"")}
                              for i in range(n)))


def _xref_only(n: int) -> bytes:
    """n updates each adding only its own cross-reference stream: nothing
    read changes, the chain grows (the review of #153's xref_only)."""
    w = Writer()
    at = {k: w.obj(k, body) for k, body in {1: CAT % b"", 2: PAGES, 3: PAGE % b""}.items()}
    entries = {0: (0, 0, 65535)} | {k: (1, a, 0) for k, a in at.items()}
    prev = w.xref_stream(4, entries, b"/Size 5 /Root 1 0 R")
    w.epilogue(prev)
    for i in range(n):
        prev = w.xref_stream(5 + i, {}, b"/Size %d /Root 1 0 R /Prev %d" % (6 + i, prev))
        w.epilogue(prev)
    return bytes(w.out)


def _many_keys(n: int) -> bytes:
    """n pages-like holders each with an XMP stream, and n updates each
    adding one more, referenced from the page tree: every revision walks
    again and keeps a new set of by-key records."""
    objects = {10 + i: b"<< /Type /Page /Metadata %d 0 R >>" % (10 + n + i) for i in range(n)}
    items = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    xmp = b"<< /Length 1 >>\nstream\nx\nendstream"
    return make(catalog=b" /All [%s]" % items, objects=objects,
                streams={10 + n + i: (b"", b"x") for i in range(n)},
                updates=tuple({10 + 2 * n + i: b"<< /Type /Page /Metadata %d 0 R >>" % (10 + 3 * n + i),
                               10 + 3 * n + i: xmp,
                               2: PAGES.replace(b">>", b"/X %d 0 R >>" % (10 + 2 * n + i))}
                              for i in range(n)))


_KEPT = (frozenset, set, dict, tuple, list, RevisionLabels, Structural)


def _retained(labels: tuple[RevisionLabels | None, ...]) -> int:
    """The bytes the labels keep that the rest of the inventory does not:
    their containers, records and revision records (sys.getsizeof; the
    ints, units and keys are the graph's)."""
    seen: set[int] = set()
    total = 0
    stack: list[object] = [found for found in labels if found is not None]
    while stack:
        item = stack.pop()
        if id(item) in seen or not isinstance(item, _KEPT):
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            stack += [*item.keys(), *item.values()]
        elif isinstance(item, (tuple, list, set, frozenset)):
            stack += list(item)
        else:
            stack += [getattr(item, name) for name in item.__slots__]
    return total


@pytest.mark.parametrize("make_file", [lambda: _revisions(40, True), lambda: _stream_revisions(40),
                                       lambda: _many_keys(60), lambda: _xref_only(300)],
                         ids=["walked-revisions", "stream-revisions", "many-keys", "xref-only"])
def test_labels_retain_at_most_a_byte_per_unit_of_work(make_file: object) -> None:
    # What the labels keep -- a new reachable set, new records, a record per
    # revision -- is charged at least its size (DESIGN.md, "Bounded
    # resources"): at most one byte per unit, so at most the budget's 64
    # per byte of the file however many revisions (the review of #153
    # measured 316x the file under a per-element charge). Measured (n = 40
    # to 2,000): 0.30 to 0.86 bytes per unit of the labels' own work, at most 52.5 bytes per
    # byte of the file.
    data = make_file()  # type: ignore[operator]
    charged, _, inv = charged_steps(data, "build_labels")
    assert inv.flags == ()
    kept = _retained(inv.labels)
    assert kept <= charged, (kept, charged)
    assert kept <= Limits().work_per_byte * len(data), (kept, len(data))


def _per_unit(work: list[tuple[int, int]]) -> None:
    """(work charged, lines executed) at n and 8n: lines per charged unit
    under a fixed bound, and not growing with the input (#94)."""
    (w_s, l_s), (w_l, l_l) = work
    assert l_s <= LINES_PER_UNIT * w_s and l_l <= LINES_PER_UNIT * w_l, work
    assert l_l / w_l <= PER_UNIT_GROWTH * (l_s / w_s), work


# ── Item 5's keys only on the holders it names (review of #153, B1) ───────
T3 = (b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 10 10] /FontMatrix [0.1 0 0 0.1 0 0]"
      b" /CharProcs %s /Encoding << /Type /Encoding /Differences [65 /JS /Metadata] >>"
      b" /FirstChar 65 /LastChar 66 /Widths [10 10] /Resources << >> >>")
GLYPH = b"10 0 0 0 10 10 d1 0 0 10 10 re f"
DRAWN = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R"
         b" /Resources << %s >> >>")
SHADING = (b" /ShadingType 4 /ColorSpace /DeviceGray /BitsPerCoordinate 8 /BitsPerComponent 8"
           b" /BitsPerFlag 8 /Decode [0 200 0 200 0 1]")
TRIANGLE = bytes([0, 0, 0, 0, 0, 200, 0, 255, 0, 0, 200, 128])
TEXT = (b"", b"BT /F1 12 Tf 20 20 Td (AB) Tj ET")
def _appearance(ap: bytes, objects: dict[int, bytes], state: bytes = b"Metadata") -> bytes:
    """A stamp annotation (9) whose /AS names *state*, with *ap*; stream 7
    an appearance (a form without /BBox: MuPDF draws nothing, so the gate
    agrees either way -- the fixture pins the rule, not a drawing)."""
    return make(objects={3: PAGE % b" /Annots [9 0 R]",
                         9: b"<< /Type /Annot /Subtype /Stamp /F 4 /Rect [0 0 200 200] /AS /%s %s >>"
                            % (state, ap)} | objects,
                streams={7: (b" /Resources << >>", b"0 0 0 rg 0 0 200 200 re f")})


# Every file draws or holds its streams under one of item 5's key names, on
# a holder item 5 does not name: none is structural (the reviewer's p1).
MISNAMED: list[tuple[str, bytes]] = [
    ("type3-glyphs-named-js-and-metadata", make(
        objects={6: T3 % b"<< /JS 7 0 R /Metadata 8 0 R >>", 3: DRAWN % b"/Font << /F1 6 0 R >>"},
        streams={5: TEXT, 7: (b"", GLYPH), 8: (b"", GLYPH)})),
    # An indirect /CharProcs: its glyphs' paths are (Metadata,), (JS,).
    ("indirect-charprocs", make(
        objects={6: T3 % b"9 0 R", 9: b"<< /JS 7 0 R /Metadata 8 0 R >>",
                 3: DRAWN % b"/Font << /F1 6 0 R >>"},
        streams={5: TEXT, 7: (b"", GLYPH), 8: (b"", GLYPH)})),
    # ... with a /Type item 5 allows: still a dictionary drawn by name.
    ("indirect-charprocs-typed", make(
        objects={6: T3 % b"9 0 R", 9: b"<< /Type /Page /Metadata 8 0 R /JS 7 0 R >>",
                 3: DRAWN % b"/Font << /F1 6 0 R >>"},
        streams={5: TEXT, 7: (b"", GLYPH), 8: (b"", GLYPH)})),
    ("shadings-named-metadata-and-js", make(
        objects={3: DRAWN % b"/Shading << /Metadata 7 0 R /JS 8 0 R >>"},
        streams={5: (b"", b"/Metadata sh /JS sh"), 7: (SHADING, TRIANGLE),
                 8: (SHADING, TRIANGLE)})),
    ("shadings-typed-and-javascript", make(
        objects={3: DRAWN % b"/Shading << /Type /Page /S /JavaScript /Metadata 7 0 R /JS 8 0 R >>"},
        streams={5: (b"", b"/Metadata sh /JS sh"), 7: (SHADING, TRIANGLE),
                 8: (SHADING, TRIANGLE)})),
    # A shading named /EF: its /Function reached through an "/EF" dictionary.
    ("shading-named-ef", make(
        objects={3: DRAWN % b"/Shading << /EF 9 0 R >>",
                 9: b"<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [0 0 200 0]"
                    b" /Function 7 0 R >>"},
        streams={5: (b"", b"/EF sh"), 7: (b" /FunctionType 4 /Domain [0 1] /Range [0 1]",
                                          b"{ }")})),
    ("js-on-a-uri-action", make(
        catalog=b" /OpenAction << /S /URI /URI (http://x.invalid) /JS 7 0 R >>",
        streams={7: (b"", b"not run")})),
    ("js-on-the-catalog", make(catalog=b" /JS 7 0 R", streams={7: (b"", b"not run")})),
    ("ef-not-on-a-file-specification", make(catalog=b" /Foo << /EF << /F 7 0 R >> >>",
                                            streams={7: (b"", b"x")})),
    ("ef-by-reference-not-on-a-file-specification", make(
        catalog=b" /Foo << /EF 9 0 R >>", objects={9: b"<< /F 7 0 R >>"},
        streams={7: (b"", b"x")})),
    ("metadata-on-an-untyped-dictionary", make(catalog=b" /Foo << /Metadata 7 0 R >>",
                                               streams={7: (b"", XMP)})),
    # A "file specification" that is a resource-category dictionary.
    ("ef-in-a-category-dictionary", make(
        objects={3: DRAWN % b"/Shading << /Type /Filespec /EF << /F 7 0 R >> >>"},
        streams={5: (b"", b"0 0 m"), 7: (SHADING, TRIANGLE)})),
    # The catalog's /OutputIntents array also named as a page's /ExtGState
    # dictionary: its items are not read for a profile.
    ("output-intents-drawn-by-name", make(
        catalog=b" /OutputIntents 9 0 R", page=b"",
        objects={9: b"[6 0 R]", 6: b"<< /S /GTS_PDFA1 /DestOutputProfile 8 0 R >>",
                 3: DRAWN % b"/ExtGState 9 0 R"},
        streams={5: (b"", b"0 0 m"), 8: PROFILE})),
    # Appearance states are drawn by name (/AS): a state dictionary is no
    # holder, given by reference, written directly, or in an /AP given by
    # reference (the review of #153, round 2: p5, p6).
    ("appearance-state-by-reference", _appearance(
        b"/AP << /N 10 0 R >>", {10: b"<< /Type /Page /Metadata 7 0 R >>"})),
    ("appearance-state-direct", _appearance(
        b"/AP << /N << /Type /Page /Metadata 7 0 R >> >>", {})),
    ("appearance-state-as-an-action", _appearance(
        b"/AP << /N 10 0 R >>", {10: b"<< /S /JavaScript /JS 7 0 R >>"}, b"JS")),
    ("appearance-dictionary-by-reference", _appearance(
        b"/AP 11 0 R", {11: b"<< /N 10 0 R >>", 10: b"<< /Type /Page /Metadata 7 0 R >>"})),
    # An appearance stream itself (at a state) is no holder either.
    ("appearance-stream-holding-metadata", _appearance(
        b"/AP << /N << /Metadata 10 0 R >> >>",
        {10: b"<< /Type /XObject /Subtype /Form /BBox [0 0 200 200] /Resources << >>"
             b" /Metadata 7 0 R /Length 0 >>\nstream\n\nendstream"})),
    ("appearance-dictionary-by-reference-state-direct", _appearance(
        b"/AP 11 0 R", {11: b"<< /D << /Type /Page /Metadata 7 0 R >> >>"})),
    # A real file specification whose /EF dictionary is also a page's
    # /Shading dictionary: its entry /F is drawn as a shading.
    ("ef-dictionary-drawn-by-name", make(
        catalog=b" /Names << /EmbeddedFiles << /Names [(a) 6 0 R] >> >>",
        objects={6: b"<< /Type /Filespec /F (a) /EF 9 0 R >>", 9: b"<< /F 7 0 R >>",
                 3: DRAWN % b"/Shading 9 0 R"},
        streams={5: (b"", b"/F sh"), 7: (SHADING, TRIANGLE)})),
]


@pytest.mark.parametrize("data", [d for _, d in MISNAMED], ids=[n for n, _ in MISNAMED])
def test_a_key_on_a_holder_item_5_does_not_name_labels_nothing(data: bytes) -> None:
    inv = _inventory(data)
    assert inv.flags == ()
    assert _kinds(inv) == {}


@requires_qpdf
def test_the_misnamed_fixtures_agree_with_the_gate(tmp_path: Path) -> None:
    for name, data in MISNAMED:
        found = compare(data, tmp_path)
        assert found.agrees, (name, found)


def test_javascript_in_the_name_tree_is_structural() -> None:
    # An action written directly in the /JavaScript name tree's /Names.
    data = make(catalog=b" /Names << /JavaScript << /Names [(a) << /S /JavaScript /JS 7 0 R >>]"
                        b" >> >>", streams={7: (b"", b"app.alert(1)")})
    inv = _inventory(data)
    assert inv.flags == () and _kinds(inv) == {7: {K.JAVASCRIPT}}


def test_a_holder_changing_alone_is_read_again() -> None:
    # The action's /S changes, its references do not: the reuse rule must
    # still read it again (a holder's checks are compared, not only its
    # references).
    data = make(catalog=b" /OpenAction 6 0 R", objects={6: b"<< /S /URI /JS 7 0 R >>"},
                streams={7: (b"", b"app.alert(1)")},
                updates=({6: b"<< /S /JavaScript /JS 7 0 R >>"},))
    inv = _inventory(data)
    assert inv.flags == ()
    assert _kinds(inv, 1) == {} and _kinds(inv, 0) == {7: {K.JAVASCRIPT}}


# ── The reuse rule, condition by condition (review of #153, B2) ───────────
def _objstm_base(update: str) -> bytes:
    """Revision 1: object stream 10 holds 4 (the catalog's /X), its own
    cross-reference stream 11. Revision 0, a table: *update* "home": 10
    rewritten (4 now references a new 6); "home-referenced": the same, the
    catalog also referencing 10; "plain": 4 rewritten as a plain object."""
    w = Writer()
    at1 = w.obj(1, CAT % (b" /X 4 0 R" + (b" /S 10 0 R" if update == "home-referenced" else b"")))
    at2, at3 = w.obj(2, PAGES), w.obj(3, PAGE % b"")
    at10 = w.objstm(10, [(4, b"(packed)")])
    entries = {0: (0, 0, 65535), 1: (1, at1, 0), 2: (1, at2, 0), 3: (1, at3, 0),
               10: (1, at10, 0), 4: (2, 10, 0)}
    prev = w.xref_stream(11, entries, b"/Size 12 /Root 1 0 R")
    w.epilogue(prev)
    if update.startswith("home"):
        table = {10: (1, w.objstm(10, [(4, b"<< /Y 6 0 R >>")]), 0),
                 6: (1, w.obj(6, b"(six)"), 0)}
    else:
        table = {4: (1, w.obj(4, b"(plain)"), 0)}
    w.epilogue(w.table(table, b"/Size 12 /Root 1 0 R /Prev %d" % prev))
    return bytes(w.out)


def _startxref(data: bytes) -> int:
    return int(data.rsplit(b"startxref", 1)[1].split()[0])


def _regenerated() -> bytes:
    """An update rewrites 5 at generation 1; the catalog still says 5 0 R."""
    w = Writer()
    w.out = bytearray(make(catalog=b" /X 5 0 R", objects={5: b"(five)"}))
    prev = _startxref(bytes(w.out))
    at = w.at()
    w.out += b"5 1 obj\n(five again)\nendobj\n"
    w.epilogue(w.table({5: (1, at, 1)}, b"/Size 6 /Root 1 0 R /Prev %d" % prev))
    return bytes(w.out)


def _gains_info() -> bytes:
    """An update whose trailer alone gains /Info."""
    w = Writer()
    w.out = bytearray(make(objects={5: b"<< /Title (t) >>"}))
    prev = _startxref(bytes(w.out))
    w.epilogue(w.table({}, b"/Size 6 /Root 1 0 R /Info 5 0 R /Prev %d" % prev))
    return bytes(w.out)


XS, OS = K.XREF_STREAM, K.OBJECT_STREAM
# (name, file, each revision's (reachable, structural kinds by number), newest
# first). Each fails if the reuse condition it names is dropped (the
# reviewer's reuse.py, each confirmed against its mutant): a reference's
# number, its generation, the body's generation, the trailer's references,
# an object stream home read (its members' bodies change with it), a home
# reached by a reference, the chain's homes read, and a home gaining or
# losing its live members. The condition that a changed number was
# reachable is implied by the others (labels.py: an equivalent mutant).
REUSE: list[tuple[str, bytes, list[tuple[list[int], dict[int | None, set[K]]]]]] = [
    ("edge-number", make(catalog=b" /X 5 0 R", objects={5: b"(five)", 6: b"(six)"},
                         updates=({1: CAT % b" /X 6 0 R"},)),
     [([1, 2, 3, 6], {}), ([1, 2, 3, 5], {})]),
    ("edge-generation", make(catalog=b" /X 5 0 R", objects={5: b"(five)"},
                             updates=({1: CAT % b" /X 5 1 R"},)),
     [([1, 2, 3], {}), ([1, 2, 3, 5], {})]),
    ("body-generation", _regenerated(), [([1, 2, 3], {}), ([1, 2, 3, 5], {})]),
    ("trailer", _gains_info(), [([1, 2, 3, 5], {}), ([1, 2, 3], {})]),
    ("home-rewritten", _objstm_base("home"),
     [([1, 2, 3, 4, 6], {10: {OS}, 11: {XS}}), ([1, 2, 3, 4], {10: {OS}, 11: {XS}})]),
    ("home-rewritten-referenced", _objstm_base("home-referenced"),
     [([1, 2, 3, 4, 6, 10], {10: {OS}, 11: {XS}}), ([1, 2, 3, 4, 10], {10: {OS}, 11: {XS}})]),
    ("member-made-plain", _objstm_base("plain"),
     [([1, 2, 3, 4], {11: {XS}}), ([1, 2, 3, 4], {10: {OS}, 11: {XS}})]),
    # A holder's /Type held by reference, rewritten to a type item 5 does
    # not allow: the reference's target was read for the record (review of
    # #153, round 2: name-keyed-dropped).
    ("holder-type-by-reference", make(
        catalog=b" /Extra 9 0 R", objects={9: b"<< /Type 10 0 R /Metadata 7 0 R >>", 10: b"/Page"},
        streams={7: (b"", b"<x/>")}, updates=({10: b"/Annot"},)),
     [([1, 2, 3, 7, 9, 10], {}), ([1, 2, 3, 7, 9, 10], {7: {K.XMP}})]),
]


@pytest.mark.parametrize(("data", "expected"), [(d, e) for _, d, e in REUSE],
                         ids=[n for n, _, _ in REUSE])
def test_each_reuse_condition_reads_the_revision_again(
        data: bytes, expected: list[tuple[list[int], dict[int | None, set[K]]]]) -> None:
    inv = _inventory(data)
    assert {f.reason for f in inv.flags} <= {FlagReason.REF_GENERATION}
    assert [(sorted(_labels(inv, r).reachable), _kinds(inv, r))
            for r in range(inv.revisions)] == expected
    # Every object stream record is that revision's own body (a rewritten
    # home's old body would be stale, and the gate cannot see it).
    for revision in range(inv.revisions):
        for unit, records in _labels(inv, revision).structural.items():
            if OS in {r.kind for r in records}:
                assert unit.obj is not None and inv.body(unit.obj, revision) == unit


# ── body()'s checks (review of #153, nit 5) ───────────────────────────────
def test_object_zero_is_never_reachable() -> None:
    # body_of answers None for 0 too: the check in body() is a guard (its
    # mutant is equivalent).
    inv = _inventory(make(catalog=b" /X 0 0 R"))
    assert FlagReason.REF_DANGLING in {f.reason for f in inv.flags}
    assert 0 not in _labels(inv).reachable


def test_an_object_stream_home_that_is_not_a_stream_gets_no_label() -> None:
    w = Writer()
    at = {n: w.obj(n, body) for n, body in
          {1: CAT % b"", 2: PAGES, 3: PAGE % b"", 10: b"<< /Type /ObjStm >>"}.items()}
    entries = {0: (0, 0, 65535)} | {n: (1, a, 0) for n, a in at.items()} | {4: (2, 10, 0)}
    w.epilogue(w.xref_stream(11, entries, b"/Size 12 /Root 1 0 R"))
    inv = _inventory(bytes(w.out))
    assert FlagReason.OBJSTM_ENTRY_MISMATCH in {f.reason for f in inv.flags}
    assert _kinds(inv) == {11: {XS}}


# ── The gate's add-back conditions (review of #153, nit 2) ────────────────
@requires_qpdf
def test_an_unreachable_streams_length_is_not_added_back(tmp_path: Path) -> None:
    # Stream 4 is reachable from nothing, nor is its /Length 5: MuPDF's
    # add-back keeps only the /Length of a stream it kept.
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in {1: CAT % b"", 2: PAGES, 3: PAGE % b"", 5: b"1"}.items():
        table[n] = (1, w.obj(n, body), 0)
    table[4] = (1, w.obj(4, b"<< /Length 5 0 R >>\nstream\nx\nendstream"), 0)
    w.epilogue(w.table(table, b"/Size 6 /Root 1 0 R"))
    data = bytes(w.out)
    inv = _inventory(data)
    assert inv.flags == () and _labels(inv).reachable == {1, 2, 3}
    found = compare(data, tmp_path, inv=inv)
    assert found.agrees and found.reachable_compared == 1, found


@requires_qpdf
def test_an_encrypt_dictionary_holding_references_fails_closed(tmp_path: Path) -> None:
    # The readers write the revision unencrypted, dropping /Encrypt; here an
    # update gives it a reference to a dictionary that holds one: MuPDF's
    # set is not exact there, and the check disagrees rather than allowing it.
    import pikepdf
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(make())
    with pikepdf.open(plain) as pdf:
        pdf.save(tmp_path / "enc.pdf", encryption=pikepdf.Encryption(owner="o", user="", R=4),
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    data = (tmp_path / "enc.pdf").read_bytes()
    inv = _inventory(data)
    (encrypt,) = [e.num for e in inv.graph.trailers[0] if e.path == (b"Encrypt",)]
    held = re.search(rb"\n%d 0 obj\s*(<<.*?>>)\s*endobj" % encrypt, data, re.S)
    ident = re.search(rb"/ID\s*\[[^\]]*\]", data[data.rindex(b"trailer"):])
    assert held is not None and ident is not None
    w = Writer()
    w.out = bytearray(data)
    size = max(inv.entries) + 1
    found = {encrypt: (1, w.obj(encrypt, held.group(1)[:-2] + b" /X %d 0 R >>" % size), 0),
             size: (1, w.obj(size, b"<< /Y %d 0 R >>" % (size + 1)), 0),
             size + 1: (1, w.obj(size + 1, b"7"), 0)}
    w.epilogue(w.table(found, b"/Size %d /Root 1 0 R /Encrypt %d 0 R %s /Prev %d" % (
        size + 2, encrypt, ident.group(0), _startxref(data))))
    data = bytes(w.out)
    inv = _inventory(data)
    assert inv.flags == ()
    result = compare(data, tmp_path, inv=inv)
    assert result.disagrees and result.check is Check.REACHABLE, result
    assert result.templates == ("mupdf", "not exact"), result
