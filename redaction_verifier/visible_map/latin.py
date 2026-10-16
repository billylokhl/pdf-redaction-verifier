"""redaction_verifier.visible_map.latin — text as the map draws it.

The map's only font is Helvetica, written in Latin-1: a string is drawn
composed (NFC, so a macOS file name's "e" plus accent is one character),
typographic marks become ASCII, and any other character outside
printable Latin-1 becomes \\u{XXXX}. Anything checked against the rules
before it is drawn is checked in this form too, since the escape itself
adds characters (hex digits) the original did not have. Standard
library only.
"""

from __future__ import annotations

import unicodedata

_ASCII = str.maketrans({"—": "--", "–": "-", "…": "...", "‘": "'",
                        "’": "'", "“": '"', "”": '"'})


def latin(text: str) -> str:
    """*text* as the map draws it."""
    out = []
    for ch in unicodedata.normalize("NFC", text).translate(_ASCII):
        if ord(ch) < 256 and ch.isprintable():
            out.append(ch)
        else:
            out.append(f"\\u{{{ord(ch):04X}}}")
    return "".join(out)
