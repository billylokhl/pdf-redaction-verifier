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

import io
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Callable

import fitz

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


_DATE_RE = re.compile(rb"D:(\d{4,14})((?:[+\-]\d\d'?\d\d'?)?)")


def finalize(path: Path) -> None:
    """Pin PDF date strings in place (same length, so offsets hold):
    annotations and attachments are stamped with the build time."""
    raw = path.read_bytes()

    def pin(m: re.Match[bytes]) -> bytes:
        digits = b"20000101000000"[: len(m.group(1))]
        tz = re.sub(rb"\d", b"0", m.group(2))
        return b"D:" + digits + tz

    fixed = _DATE_RE.sub(pin, raw)
    if fixed != raw:
        path.write_bytes(fixed)
