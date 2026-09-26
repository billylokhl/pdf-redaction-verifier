"""Unit tests for eval/caselib/rawpdf.py's own byte-level correctness —
the case library's tests only check the tool's verdict on the PDFs it
writes, which would not catch a structurally wrong cross-reference
stream that PyMuPDF happens to tolerate anyway."""

from __future__ import annotations

import struct

from caselib.rawpdf import build_objstm, one_page, stream, text

_ENTRY = struct.Struct(">BIH")   # /W [1 4 2]: type, offset/objstm-num, gen/index


def _parse_xref_stream(pdf: bytes) -> tuple[int, list[tuple[int, int, int]]]:
    """(xref object's own offset, [(type, field2, field3), ...] by object
    number), read independently of rawpdf's own bookkeeping."""
    xref_offset = int(pdf[pdf.rindex(b"startxref"):].split()[1])
    dict_start = pdf.index(b"<<", xref_offset)
    dict_end = pdf.index(b">>", dict_start) + 2
    xref_dict = pdf[dict_start:dict_end]
    assert b"/Type /XRef" in xref_dict
    size = int(xref_dict.split(b"/Size")[1].split()[0])
    body_start = pdf.index(b"stream\n", dict_end) + len(b"stream\n")
    body_end = pdf.index(b"\nendstream", body_start)
    data = pdf[body_start:body_end]
    assert len(data) == size * _ENTRY.size
    entries = [_ENTRY.unpack(data[i * _ENTRY.size:(i + 1) * _ENTRY.size]) for i in range(size)]
    return xref_offset, entries


def test_build_objstm_xref_stream_entries_are_correct() -> None:
    """Every in-use object — including the xref stream itself — is a type
    1 (direct) or type 2 (in the object stream) entry at the right place;
    a type-1 entry's offset really is that object's "N 0 obj" header."""
    objects = one_page(stream(b"", text("hello world")), o5=stream(b"", text("extra")))
    pdf = build_objstm(objects)
    xref_offset, entries = _parse_xref_stream(pdf)
    xref_num = len(entries) - 1

    assert entries[0][0] == 0                      # object 0 is always free
    for number, (kind, field2, field3) in enumerate(entries):
        if number == 0:
            continue
        assert kind in (1, 2), f"object {number} is free (type {kind})"
        if kind == 1:
            header_end = pdf.index(b"\n", field2)
            assert pdf[field2:header_end] == b"%d 0 obj" % number, (
                f"object {number}'s type-1 entry points at the wrong offset")

    # The xref stream is written directly (never packed into the object
    # stream it describes) and is itself in use: it must be type 1, at the
    # exact offset startxref names.
    kind, offset, _ = entries[xref_num]
    assert kind == 1, "the xref stream's own entry must be type 1, not free"
    assert offset == xref_offset


def test_build_objstm_xref_stream_survives_a_gap() -> None:
    """An object number with no body at all (a hole in *objects*) is a
    free entry, not mistaken for an in-use one."""
    objects = one_page(stream(b"", text("hello")), o9=stream(b"", text("extra")))
    del objects[6]                          # leave object 6 (the font) unused
    pdf = build_objstm(objects)
    _, entries = _parse_xref_stream(pdf)
    assert entries[6][0] == 0
