"""A minimal hand-assembled PDF writer — the non-fitz writer.

PyMuPDF is also the library the verifier reads with, so a corpus written
only by PyMuPDF hides every place the two could disagree. This writer
emits exactly the bytes a case asks for: objects in number order, one
classic cross-reference table, nothing repaired or normalised.
"""

from __future__ import annotations

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


def build(objects: dict[int, bytes], root: int = 1, after_eof: bytes = b"") -> bytes:
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
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        size, root, xref)
    return bytes(out) + after_eof
