"""Helpers for building case PDFs with PyMuPDF, portably and
deterministically.

Portable: no system fonts. ``embedded_font`` embeds one of MuPDF's
built-in fonts, which PyMuPDF writes as a Type0 / Identity-H font — text
stored as glyph codes, as Word, Chrome and most embedded TrueType output
store it. ``cjk_font`` embeds MuPDF's CJK fallback the same way.

Deterministic: saves never create a new file ID, and ``finalize`` pins
the dates PyMuPDF stamps on annotations and attachments.
"""

from __future__ import annotations

import base64
import io
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Callable

import pymupdf as fitz

FILLER = (
    "Quarterly operations summary for the regional office.",
    "All figures are unaudited and subject to revision.",
    "Contact the facilities team with questions about the schedule.",
    "Meeting notes were circulated to the distribution list.",
)


def body(page: fitz.Page, font: str = "helv", y0: float = 72,
         lines: tuple[str, ...] | list[str] = FILLER, **kw) -> None:
    for i, text in enumerate(lines):
        page.insert_text((72, y0 + 16 * i), text, fontname=font, **kw)


def embedded_font(page: fitz.Page, name: str = "EM") -> str:
    """An embedded Identity-H font: codes are glyph ids, not characters."""
    page.insert_font(fontname=name, fontbuffer=fitz.Font("helv").buffer)
    return name


def cjk_font(page: fitz.Page, name: str = "CJ") -> str:
    page.insert_font(fontname=name, fontbuffer=fitz.Font("cjk").buffer)
    return name


# Payloads that must really be compressed (so the secret is not in their
# bytes), precomputed once: compressor output varies between zlib
# versions, and the build lock needs identical bytes everywhere.
_COMPRESSED = {
    "zip:SSN 123-45-6789": "UEsDBBQAAAAIAAAAISgx9B4xEQAAAA8AAAAIAAAAbm90ZS50eHQLDvZTMDQy1jUx1TUzt7AEAFBLAQIUAxQAAAAIAAAAISgx9B4xEQAAAA8AAAAIAAAAAAAAAAAAAACAAQAAAABub3RlLnR4dFBLBQYAAAAAAQABADYAAAA3AAAAAAA=",
    "zlib:SSN 123-45-6789": "eNoLDvZTMDQy1jUx1TUzt7AEABvfA0w=",
    "gzip:SSN 123-45-6789\n": "H4sIAAAAAAAC/wsO9lMwNDLWNTHVNTO3sOQCAF0YOGMQAAAA",
}


def compressed(kind: str, text: str) -> bytes:
    """*text* compressed as *kind* (zip, zlib, gzip), from the table above."""
    return base64.b64decode(_COMPRESSED[f"{kind}:{text}"])


def lookalike_font(doc: fitz.Document) -> None:
    """Make the embedded font's Unicode map say soft hyphen (U+00AD) for
    '-' and no-break space (U+00A0) for ' ', as some fonts' maps do. The
    page looks the same, but a text search for "123-45-6789" misses it —
    which is how a redactor's search can fail to find what it must remove."""
    for xref in range(1, doc.xref_length()):
        if not doc.xref_is_stream(xref):
            continue
        data = doc.xref_stream(xref)
        if b"/CMapName /Adobe-Identity-UCS" not in data:
            continue
        data = (data.replace(b"<0001> <0007> <0020>", b"<0001> <0001> <00a0>\n<0002> <0007> <0021>")
                    .replace(b"<0009> <0040> <0028>",
                             b"<0009> <000d> <0028>\n<000e> <000e> <00ad>\n<000f> <0040> <002e>"))
        count = data.count(b"\n<0") - data.count(b"begincodespacerange\n<0")
        data = re.sub(rb"\d+ beginbfrange", b"%d beginbfrange" % count, data, count=1)
        doc.update_stream(xref, data)


def deflate(data: bytes) -> bytes:
    """zlib data as stored (level 0) blocks: a valid zlib stream whose
    bytes do not depend on the zlib version — compressed output does, and
    would make the build lock differ between machines."""
    return zlib.compress(data, 0)


def zipbytes(text: str, name: str = "note.txt") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=0) as zf:
        info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        zf.writestr(info, text)
    return buf.getvalue()


def gzipbytes(data: bytes) -> bytes:
    """A gzip member with fixed header fields and stored blocks."""
    body = zlib.compressobj(0, zlib.DEFLATED, -15)
    raw = body.compress(data) + body.flush()
    header = b"\x1f\x8b\x08\x00" + b"\x00\x00\x00\x00" + b"\x00\xff"
    return header + raw + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)


def png_of(text: str, width: int = 300, height: int = 60) -> bytes:
    """Text rendered to a PNG (no Pillow needed)."""
    tmp = fitz.open()
    tmp.new_page(width=width, height=height).insert_text((10, 40), text, fontsize=18)
    data = tmp[0].get_pixmap(dpi=144).tobytes("png")
    tmp.close()
    return data


def orphan(doc: fitz.Document, dictionary: str, data: bytes, compress: bool = True) -> int:
    """A stream object nothing references."""
    xref = doc.get_new_xref()
    doc.update_object(xref, dictionary)
    doc.update_stream(xref, data, compress=compress)
    return xref


def save(doc: fitz.Document, path: Path, **kw) -> None:
    doc.save(str(path), no_new_id=True, **kw)
    doc.close()


def update(path: Path, edit: Callable[[fitz.Document], None]) -> None:
    """Apply *edit* and append it as an incremental update."""
    doc = fitz.open(str(path))
    edit(doc)
    doc.save(str(path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP,
             no_new_id=True)
    doc.close()


def redact(doc: fitz.Document, needle: str) -> None:
    """Redact *needle* on every page the way PyMuPDF-based redactors do."""
    for page in doc:
        for rect in page.search_for(needle):
            page.add_redact_annot(rect)
        page.apply_redactions()


# Only the values of date keys: a date-shaped run elsewhere (page text,
# stream data) is content and must not be touched.
_DATE_RE = re.compile(rb"(/(?:CreationDate|ModDate|M)\s*\(D:)(\d{4,14})((?:[+\-]\d\d'?\d\d'?)?)\)")


def finalize(path: Path) -> None:
    """Pin the dates PyMuPDF stamps on annotations and attachments, in
    place and at the same length so offsets hold. Builds run under UTC
    (run.build), so no zone suffix varies between machines."""
    raw = path.read_bytes()

    def pin(m: re.Match[bytes]) -> bytes:
        digits = b"20000101000000"[: len(m.group(2))]
        return m.group(1) + digits + re.sub(rb"\d", b"0", m.group(3)) + b")"

    fixed = _DATE_RE.sub(pin, raw)
    if fixed != raw:
        path.write_bytes(fixed)
