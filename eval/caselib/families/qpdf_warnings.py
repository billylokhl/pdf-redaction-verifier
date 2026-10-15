"""qpdf exit 3 ("succeeded with warnings"): which warnings still flag.

The Binary layer runs qpdf's QDF rewrite. Exit 3 used to force exit 2
(TOOL_EXIT_NONZERO) whatever the warning. Owner decision 2026-10-15: it
stops forcing exit 2 only when every warning line is one of five kinds and
that kind's check against the file's own bytes passes (verify.py,
``_qpdf_warnings_unverified``; ADR 0002's note of that date).

The leak cases below are layouts where that flag is the ONLY thing between
the hidden SSN and a clean pass: no layer reads it, so each must still exit
2. The clean cases are the harmless layouts the real corpus is full of
(mostly Quartz), which now exit 0. All bytes are hand-assembled; the SSN
is the fabricated example number.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ..model import SSN, case, expect
from ..rawpdf import CATALOG, HELVETICA, PAGES, page, stream, text

SECRET = f"SSN {SSN}".encode()
FLAGGED = expect(2, warnings=(("TOOL_EXIT_NONZERO", None),))
CELL = "unindexed.plain.qpdf-warned"
CLEAN = expect(0)
CONTENT = stream(b"", text("Quarterly report"))


def _build(objects: list[tuple[int, bytes]], *, entries: dict[int, bytes] | None = None,
           startxref_shift: int = 0) -> bytes:
    """Objects in the order given, each written exactly as passed (header
    included); one classic xref table whose entry for a number is the last
    copy's offset unless *entries* overrides it."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number, raw in objects:
        offsets[number] = len(out)
        out += raw
    size = max(offsets) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        if entries and number in entries:
            out += entries[number]
        elif number in offsets:
            out += b"%010d 00000 n \n" % offsets[number]
        else:
            out += b"0000000000 00001 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        size, xref + startxref_shift)
    return bytes(out)


def obj(number: int, body: bytes, end: bytes = b"\nendobj\n") -> tuple[int, bytes]:
    return number, b"%d 0 obj\n" % number + body + end


def _base(contents: bytes = CONTENT, page_extra: bytes = b"") -> list[tuple[int, bytes]]:
    return [obj(1, CATALOG), obj(2, PAGES), obj(3, page(extra=page_extra)),
            obj(4, contents), obj(6, HELVETICA)]


def _unfinished_flate(data: bytes) -> bytes:
    """Deflate data with a sync flush but no final block: every byte
    decodes, the end marker never comes (qpdf: "input stream is complete
    but output may still be valid"). Level 0: the same bytes under every
    zlib."""
    z = zlib.compressobj(0)
    return z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH)


OFFSET_ZERO = {5: b"0000000000 00000 n \n"}


# ── Still flagged: the warning is the only signal ───────────────────────

def leak(id: str, story: str, **kw):
    return case(id, truth="leak", cells=CELL, expected=FLAGGED, story=story, writer="raw",
                requires=("qpdf",), **kw)


@leak("file.offset-zero-entry-stored-body",
      "Object 5 holds the SSN, but its in-use xref entry says offset 0. qpdf, MuPDF and "
      "PDFKit all read the object as null, so no layer sees the SSN; qpdf warns 'object has "
      "offset 0' and the stored body makes that warning count (exit 2).",
      mistake="A writer that zeroed an object's xref entry instead of removing its body.",
      recovery="Search the file's raw bytes for '5 0 obj' and read the body by hand.")
def offset_zero_stored_body(path: Path) -> None:
    path.write_bytes(_build([*_base(), obj(5, b"<< /Note (" + SECRET + b") >>")],
                            entries=OFFSET_ZERO))


@leak("file.offset-zero-entry-page-content",
      "The page's own content stream (object 5, holding the SSN as text) has an in-use xref "
      "entry at offset 0: every reader draws a blank page, and only qpdf's warning, with the "
      "body present in the file, keeps the verdict at exit 2.",
      mistake="A writer that zeroed the content stream's xref entry instead of rewriting it.",
      recovery="Search the file's raw bytes for '5 0 obj' and decode its stream by hand.")
def offset_zero_page_content(path: Path) -> None:
    objects = [obj(1, CATALOG), obj(2, PAGES), obj(3, page(contents=b"5 0 R")),
               obj(5, stream(b"", text(SECRET.decode()))), obj(6, HELVETICA)]
    path.write_bytes(_build(objects, entries=OFFSET_ZERO))


@leak("file.stray-bytes-before-endobj",
      "A string holding the SSN sits between object 5's dictionary and its endobj: no reader "
      "keeps it, and qpdf warns 'expected endobj'. Bytes other than whitespace there keep the "
      "warning counting (exit 2).",
      mistake="A tool that rewrote an object's value in place and left the old tail behind.",
      recovery="Open the file in a text editor and read what follows each object's value.")
def stray_before_endobj(path: Path) -> None:
    path.write_bytes(_build([*_base(), obj(5, b"<< /A 1 >> (" + SECRET + b")")]))


@leak("file.damaged-xref-earlier-body",
      "Two bodies of the page's content stream: the first holds the SSN, the second does "
      "not, and startxref points nowhere. qpdf, MuPDF and PDFKit all rebuild the xref and "
      "keep the last body, so no layer reads the SSN; qpdf's reconstruction warnings keep "
      "forcing exit 2.",
      mistake="A tool that appended a cleaned copy of an object and broke the xref offset.",
      recovery="Search the file's raw bytes for every '4 0 obj' and read each body.")
def damaged_xref_earlier_body(path: Path) -> None:
    objects = [obj(1, CATALOG), obj(2, PAGES), obj(3, page()),
               obj(4, stream(b"", text(SECRET.decode()))), obj(6, HELVETICA),
               obj(4, CONTENT)]
    path.write_bytes(_build(objects, startxref_shift=7))


@leak("file.duplicated-filter-first-wins",
      "The content stream's dictionary names /Filter twice (Flate, then ASCIIHex) and its "
      "data is Flate: readers take the last value, fail to decode, and draw nothing, while "
      "a first-wins reader shows the SSN. The values differ, so qpdf's duplicated-key "
      "warning keeps counting (exit 2).",
      mistake="A writer that appended a /Filter key instead of replacing it.",
      recovery="Decode the stream with the first /Filter value by hand.")
def duplicated_filter_first_wins(path: Path) -> None:
    data = zlib.compress(text(SECRET.decode()), 0)
    body = (b"<< /Filter /FlateDecode /Filter /ASCIIHexDecode /Length %d >>\nstream\n"
            % len(data) + data + b"\nendstream")
    path.write_bytes(_build([*_base(body)]))


# ── Harmless after the check: now exit 0 ─────────────────────────────────

def clean(id: str, story: str):
    return case(id, truth="clean", features="live.plain", expected=CLEAN, story=story,
                writer="raw", requires=("qpdf",))


@clean("file.qpdf-null-reference",
       "The page dictionary holds '/Foo 0 0 R'. Object 0 heads the free list, so no reader "
       "can resolve it; qpdf warns 'treating bad indirect reference (0 0 R) as null'.")
def null_reference(path: Path) -> None:
    path.write_bytes(_build(_base(page_extra=b"/Foo 0 0 R ")))


@clean("file.offset-zero-entry-no-body",
       "Object 5's in-use xref entry says offset 0 and no body of object 5 exists anywhere "
       "in the file (the Quartz layout): nothing can be lost.")
def offset_zero_no_body(path: Path) -> None:
    path.write_bytes(_build(_base(), entries=OFFSET_ZERO))


@clean("file.duplicated-filter-same-value",
       "The content stream names /Filter /FlateDecode twice: identical values, so every "
       "reader decodes the same data.")
def duplicated_filter_same(path: Path) -> None:
    data = zlib.compress(text("Quarterly report"), 0)
    body = (b"<< /Filter /FlateDecode /Length %d /Filter /FlateDecode >>\nstream\n"
            % len(data) + data + b"\nendstream")
    path.write_bytes(_build(_base(body)))


@clean("file.missing-endobj-after-stream",
       "The content stream ends with endstream and no endobj; only a newline separates it "
       "from the next object.")
def missing_endobj(path: Path) -> None:
    objects = _base()
    objects[3] = obj(4, CONTENT, end=b"\n")
    path.write_bytes(_build(objects))


@clean("file.quartz-offset-zero-duplicate-key-endobj",
       "The Quartz combination seen in the real corpus: an offset-0 entry with no body, a "
       "content stream naming /Filter /FlateDecode twice, and no endobj after its endstream.")
def quartz_combination(path: Path) -> None:
    data = zlib.compress(text("Quarterly report"), 0)
    body = (b"<< /Filter /FlateDecode /Length %d /Filter /FlateDecode >>\nstream\n"
            % len(data) + data + b"\nendstream")
    objects = _base()
    objects[3] = obj(4, body, end=b"\n")
    path.write_bytes(_build(objects, entries=OFFSET_ZERO))


@clean("file.truncated-flate-content",
       "The page's Flate content stream stops before zlib's end marker but every byte "
       "decodes: qpdf writes the decoded text and every layer reads it.")
def truncated_flate_content(path: Path) -> None:
    data = _unfinished_flate(text("Quarterly report"))
    path.write_bytes(_build(_base(stream(b"/Filter /FlateDecode", data))))


@clean("file.truncated-xref-stream-rows-complete",
       "A PDF 1.5 file whose Flate xref stream stops before zlib's end marker (a sync flush) "
       "and has no endobj before startxref, yet decodes every row its /Size declares.")
def truncated_xref_stream(path: Path) -> None:
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number, raw in _base():
        offsets[number] = len(out)
        out += raw
    xref_num = 7
    offsets[xref_num] = len(out)
    rows = bytearray()
    for number in range(xref_num + 1):
        rows += (struct.pack(">BIH", 1, offsets[number], 0) if number in offsets
                 else struct.pack(">BIH", 0, 0, 65535))
    data = _unfinished_flate(bytes(rows))
    out += (b"%d 0 obj\n<< /Type /XRef /W [1 4 2] /Size %d /Root 1 0 R /Filter /FlateDecode "
            b"/Length %d >>\nstream\n" % (xref_num, xref_num + 1, len(data))
            + data + b"\nendstream\n")
    out += b"startxref\n%d\n%%%%EOF\n" % offsets[xref_num]
    path.write_bytes(bytes(out))
