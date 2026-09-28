"""Decoding the lexer's string and name tokens (ISO 32000-1 §7.3.4-7.3.5,
§7.9.2).

``token_bytes`` gives a string token's bytes and ``name_bytes`` a name's;
``text_string`` reads string bytes as text, keeping the raw bytes beside
it. None of them raises on any input: malformed content decodes the way
a lenient reader would, and the lexer has already flagged it.

Literal strings here follow §7.3.4.2 exactly, fixing the two end-of-line
bugs of the legacy ``verify._decode_pdf_string`` (which stays frozen): a
backslash before an end-of-line is a line continuation and contributes
nothing, and an unescaped end-of-line (CR, LF or CR LF) is one LF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from .lexer import Token, TokenKind

_ESCAPES: Final = {
    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b",
    ord("f"): b"\f", ord("("): b"(", ord(")"): b")", ord("\\"): b"\\",
}
_OCTAL: Final = re.compile(rb"[0-7]{1,3}")
_HEX_DIGITS: Final = re.compile(rb"[0-9A-Fa-f]+")
_NAME_ESCAPE: Final = re.compile(rb"#([0-9A-Fa-f]{2})")
# Literal content up to the next byte that needs handling.
_SPECIAL: Final = re.compile(rb"[\\\r\n]")


def literal_bytes(body: bytes) -> bytes:
    """The bytes a literal string's *body* (between its outer parens)
    denotes (§7.3.4.2, Table 3). Balanced inner parens are kept as is;
    an escape of any other byte is that byte; a trailing lone backslash
    contributes nothing; an octal escape's overflow is ignored."""
    out = bytearray()
    i, n = 0, len(body)
    while i < n:
        hit = _SPECIAL.search(body, i)
        if hit is None:
            out += body[i:]
            break
        j = hit.start()
        out += body[i:j]
        c = body[j]
        if c != 0x5C:  # an unescaped end-of-line: CR, LF or CR LF is one LF
            out.append(0x0A)
            i = j + 2 if c == 0x0D and body[j + 1:j + 2] == b"\n" else j + 1
            continue
        if j + 1 >= n:  # a lone backslash at the very end
            break
        e = body[j + 1]
        if e in _ESCAPES:
            out += _ESCAPES[e]
            i = j + 2
        elif e == 0x0D:  # line continuation: backslash CR [LF]
            i = j + 3 if body[j + 2:j + 3] == b"\n" else j + 2
        elif e == 0x0A:  # line continuation: backslash LF
            i = j + 2
        else:
            octal = _OCTAL.match(body, j + 1)
            if octal:
                out.append(int(octal.group(), 8) & 0xFF)
                i = octal.end()
            else:  # §7.3.4.2: the backslash is ignored
                i = j + 1
    return bytes(out)


def hex_bytes(body: bytes) -> bytes:
    """The bytes a hex string's *body* (between < and >) denotes
    (§7.3.4.3): whitespace skipped, a final odd digit padded with 0.
    Any other byte is skipped like whitespace. Readers disagree there
    (MuPDF ends the current byte at a bad character, qpdf rejects the
    token), so this decoding is one reading among several: the lexer
    flags such a token INVALID_HEX_DIGIT, and that flag must never be
    treated as benign."""
    digits = b"".join(_HEX_DIGITS.findall(body))
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii"))


def name_bytes(body: bytes) -> bytes:
    """The bytes a name's *body* (after its '/') denotes (§7.3.5): each
    #xx is one byte; a malformed '#' (flagged by the lexer) is kept."""
    return _NAME_ESCAPE.sub(lambda m: bytes.fromhex(m.group(1).decode("ascii")), body)


def token_bytes(data: bytes, token: Token) -> bytes | None:
    """The bytes a LITERAL_STRING, HEX_STRING or NAME token denotes, or
    None for any other kind. An unterminated string decodes everything
    after its opening delimiter."""
    raw = data[token.start:token.end]
    if token.kind is TokenKind.LITERAL_STRING:
        return literal_bytes(raw[1:-1] if token.complete else raw[1:])
    if token.kind is TokenKind.HEX_STRING:
        return hex_bytes(raw[1:-1] if token.complete else raw[1:])
    if token.kind is TokenKind.NAME:
        return name_bytes(raw[1:])
    return None


class TextEncoding(Enum):
    UTF16BE = "utf-16be"          # FE FF byte-order mark
    UTF16LE = "utf-16le"          # FF FE: not in the spec, but real readers
                                  # (MuPDF, qpdf) show it as UTF-16LE text
    UTF8 = "utf-8"                # EF BB BF byte-order mark (PDF 2.0)
    PDFDOC = "pdfdocencoding"     # anything else


@dataclass(frozen=True)
class TextString:
    """A text string (§7.9.2.2) as text, with the bytes it came from.
    ``text`` has Unicode language tags removed; ``tagged_text`` keeps
    them, since readers disagree (MuPDF strips them, qpdf shows them)
    and a tag can split a value either way; ``tags_removed`` counts them."""

    raw: bytes
    text: str
    encoding: TextEncoding
    tagged_text: str
    tags_removed: int = 0
    # False when bytes did not decode and were replaced with U+FFFD: the
    # text is then one reading among several, and a caller must flag it
    # (ADR 0010: a lenient branch flags or is allowlisted).
    lossless: bool = True


# PDFDocEncoding (ISO 32000-1 Annex D.2) where it differs from Latin-1.
# The bytes the table leaves undefined (most controls, 0x7F, 0x9F, 0xAD)
# map to the same code point as in Latin-1, so no byte is ever lost.
_PDFDOC_DIFFS: Final = {
    0x18: "˘", 0x19: "ˇ", 0x1A: "ˆ", 0x1B: "˙",
    0x1C: "˝", 0x1D: "˛", 0x1E: "˚", 0x1F: "˜",
    0x80: "•", 0x81: "†", 0x82: "‡", 0x83: "…",
    0x84: "—", 0x85: "–", 0x86: "ƒ", 0x87: "⁄",
    0x88: "‹", 0x89: "›", 0x8A: "−", 0x8B: "‰",
    0x8C: "„", 0x8D: "“", 0x8E: "”", 0x8F: "‘",
    0x90: "’", 0x91: "‚", 0x92: "™", 0x93: "ﬁ",
    0x94: "ﬂ", 0x95: "Ł", 0x96: "Œ", 0x97: "Š",
    0x98: "Ÿ", 0x99: "Ž", 0x9A: "ı", 0x9B: "ł",
    0x9C: "œ", 0x9D: "š", 0x9E: "ž", 0xA0: "€",
}
_PDFDOC: Final = str.maketrans({chr(b): u for b, u in _PDFDOC_DIFFS.items()})
# §7.9.2.2.1: in a Unicode text string, ESC (U+001B) opens and closes a
# language tag (a 2-letter ISO 639 code, optionally a 2-letter ISO 3166
# code) that is not part of the text.
_LANGUAGE_TAG: Final = re.compile("\x1b[A-Za-z]{2}(?:[A-Za-z]{2})?\x1b")


_UNICODE_MARKS: Final = (
    (b"\xfe\xff", "utf-16-be", TextEncoding.UTF16BE),
    (b"\xff\xfe", "utf-16-le", TextEncoding.UTF16LE),
    (b"\xef\xbb\xbf", "utf-8", TextEncoding.UTF8),
)


def text_string(raw: bytes) -> TextString:
    """Decode a text string: UTF-16BE, UTF-16LE or UTF-8 when
    byte-order-marked (undecodable bytes replaced with U+FFFD), otherwise
    PDFDocEncoding. UTF-16LE is outside the spec, but a secret real
    viewers display must not decode here as NUL-separated letters."""
    for mark, codec, encoding in _UNICODE_MARKS:
        if raw.startswith(mark):
            body = raw[len(mark):]
            try:
                tagged, lossless = body.decode(codec), True
            except UnicodeDecodeError:
                tagged, lossless = body.decode(codec, errors="replace"), False
            text, removed = _LANGUAGE_TAG.subn("", tagged)
            return TextString(raw, text, encoding, tagged, removed, lossless)
    text = raw.decode("latin-1").translate(_PDFDOC)
    return TextString(raw, text, TextEncoding.PDFDOC, text)
