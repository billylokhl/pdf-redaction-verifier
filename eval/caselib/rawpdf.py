"""A minimal hand-assembled PDF writer — the non-fitz writer.

PyMuPDF is also the library the verifier reads with, so a corpus written
only by PyMuPDF hides every place the two could disagree. This writer
emits exactly the bytes a case asks for: objects in number order, one
classic cross-reference table, nothing repaired or normalised.
"""

from __future__ import annotations

import re
import struct
import zlib

CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def page(contents: bytes = b"4 0 R", resources: bytes = b"<< /Font << /F1 6 0 R >> >>",
         extra: bytes = b"") -> bytes:
    return (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
            + contents + b" /Resources " + resources + b" " + extra + b">>")


def stream(dictionary: bytes, data: bytes) -> bytes:
    return (b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data)
            + data + b"\nendstream")


def flate(dictionary: bytes, data: bytes, trailing: bytes = b"") -> bytes:
    """A Flate stream; *trailing* bytes follow the zlib end marker inside
    the stream (its /Length counts them)."""
    # Level 0 (stored blocks): identical bytes under every zlib version.
    return stream(b"/Filter /FlateDecode " + dictionary, zlib.compress(data, 0) + trailing)


def text(line: str, y: int = 700) -> bytes:
    return b"BT /F1 12 Tf 72 %d Td (%s) Tj ET" % (y, line.encode("latin-1"))


def one_page(contents: bytes, **objects: bytes) -> dict[int, bytes]:
    """A one-page document: 1 catalog, 2 pages, 3 page, 4 contents,
    6 Helvetica. Extra objects are passed as ``o5=...``, ``o7=...``."""
    objs = {1: CATALOG, 2: PAGES, 3: page(), 4: contents, 6: HELVETICA}
    for key, body in objects.items():
        objs[int(key[1:])] = body
    return objs


def build(objects: dict[int, bytes], root: int = 1, after_eof: bytes = b"",
          info: int | None = None) -> bytes:
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[number] if number in offsets
                else b"0000000000 65535 f \n")
    extra = b" /Info %d 0 R" % info if info is not None else b""
    out += b"trailer\n<< /Size %d /Root %d 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        size, root, extra, xref)
    return bytes(out) + after_eof


# ── Incremental updates ─────────────────────────────────────────────────
# An incremental save never rewrites earlier bytes: it appends the changed
# objects, a fresh cross-reference section covering only them (plus any
# freed), and a trailer whose /Prev chains back to the previous section —
# found the same way verify.py's own revision walk finds it, from the
# file's last startxref.

_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")
_SIZE_RE = re.compile(rb"/Size\s+(\d+)")


def _last_startxref(pdf: bytes) -> int:
    matches = list(_STARTXREF_RE.finditer(pdf))
    if not matches:
        raise ValueError("no startxref in the base PDF")
    return int(matches[-1].group(1))


def _last_size(pdf: bytes) -> int:
    matches = list(_SIZE_RE.finditer(pdf))
    if not matches:
        raise ValueError("no /Size in the base PDF")
    return int(matches[-1].group(1))


def incremental_update(pdf: bytes, objects: dict[int, bytes | None], root: int = 1,
                        info: int | None = None) -> bytes:
    """*pdf* with one more incremental update appended: the given objects
    (a body of ``None`` marks the object free — deleted, not rewritten), a
    classic cross-reference section listing exactly those object numbers,
    and a trailer with ``/Prev`` pointing at *pdf*'s own latest section.

    Every earlier version — including any object this update redefines
    under the same number — stays at its old offset, exactly as a real
    incremental save leaves it, which is what lets the verifier's earlier-
    revision scan find it again.
    """
    prev = _last_startxref(pdf)
    size = max(_last_size(pdf), max(objects) + 1)
    out = bytearray(pdf)
    if not out.endswith(b"\n"):
        out += b"\n"
    offsets: dict[int, int] = {}
    for number in sorted(n for n, body in objects.items() if body is not None):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n"
    for number in sorted(objects):
        out += b"%d 1\n" % number
        out += (b"%010d 00000 n \n" % offsets[number] if number in offsets
                else b"0000000000 65535 f \n")
    extra = b" /Info %d 0 R" % info if info is not None else b""
    out += (b"trailer\n<< /Size %d /Root %d 0 R%s /Prev %d >>\nstartxref\n%d\n%%%%EOF\n"
            % (size, root, extra, prev, xref))
    return bytes(out)


# ── Object streams and cross-reference streams (PDF 1.5) ────────────────
# Packs the non-stream objects into one /ObjStm and writes the cross-
# reference as a stream (/Type /XRef) instead of a classic table — the
# format most current producers write, and untested by a writer that only
# ever emits classic tables.

_XREF_ENTRY = struct.Struct(">BIH")     # /W [1 4 2]: type, offset/objstm, gen/index


def build_objstm(objects: dict[int, bytes], root: int = 1,
                  in_objstm: set[int] | None = None, compress: bool = False) -> bytes:
    """Like ``build``, but a PDF 1.5 file: every object in *in_objstm*
    (default: every object whose body is not itself a stream — an object
    stream cannot hold one) is packed into a single object stream, and the
    cross-reference is an ``/XRef`` stream rather than a table."""
    if in_objstm is None:
        in_objstm = {n for n, body in objects.items() if b"stream" not in body}
    packed = sorted(in_objstm)
    direct = sorted(n for n in objects if n not in in_objstm)

    header_pairs: list[tuple[int, int]] = []
    payload = bytearray()
    for number in packed:
        header_pairs.append((number, len(payload)))
        payload += objects[number] + b"\n"
    header = b" ".join(b"%d %d" % pair for pair in header_pairs)
    first = len(header) + 1
    objstm_dict = b"/Type /ObjStm /N %d /First %d" % (len(packed), first)
    objstm_body = header + b"\n" + bytes(payload)
    objstm_obj = flate(objstm_dict, objstm_body) if compress else stream(objstm_dict, objstm_body)

    objstm_num = max(objects) + 1
    xref_num = objstm_num + 1
    size = xref_num + 1

    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {objstm_num: len(out)}
    out += b"%d 0 obj\n" % objstm_num + objstm_obj + b"\nendobj\n"
    for number in direct:
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"

    index_in_objstm = {number: i for i, number in enumerate(packed)}
    entries = bytearray()
    for number in range(size):
        if number == 0:
            entries += _XREF_ENTRY.pack(0, 0, 65535)
        elif number in offsets:
            entries += _XREF_ENTRY.pack(1, offsets[number], 0)
        elif number in index_in_objstm:
            entries += _XREF_ENTRY.pack(2, objstm_num, index_in_objstm[number])
        else:
            entries += _XREF_ENTRY.pack(0, 0, 65535)   # a gap: unused object number

    xref_offset = len(out)
    xref_dict = b"/Type /XRef /W [1 4 2] /Size %d /Root %d 0 R" % (size, root)
    xref_obj = flate(xref_dict, bytes(entries)) if compress else stream(xref_dict, bytes(entries))
    out += b"%d 0 obj\n" % xref_num + xref_obj + b"\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref_offset
    return bytes(out)
