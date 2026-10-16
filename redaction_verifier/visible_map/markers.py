"""redaction_verifier.visible_map.markers — the map file's own mark.

A map carries the line `%PRV-VISIBLE-MAP-1` as its second line and as
its last line. Only a file carrying both is ever treated as one of this
tool's own maps (the output file's safety rules build on this); the
self-check refuses a map without them. Standard library only.
"""

from __future__ import annotations

import re

MARKER = b"%PRV-VISIBLE-MAP-1"
MARKER_LINE = MARKER + b"\n"


def insert_markers(pdf: bytes) -> bytes:
    """*pdf* (a classic-xref file MuPDF wrote) with the marker as its
    second line and as its last line. Every xref offset and startxref
    move by the marker's length, so the file reopens unrepaired."""
    head = pdf.index(b"\n") + 1
    table = pdf.rindex(b"\nxref\n") + 1
    start = pdf.rindex(b"startxref\n")
    shift = len(MARKER_LINE)
    body, xref, trailer = pdf[:table], pdf[table:start], pdf[start:]
    xref = re.sub(rb"(?m)^(\d{10}) (\d{5}) n",
                  lambda m: b"%010d %s n" % (int(m.group(1)) + shift, m.group(2)), xref)
    trailer, n = re.subn(rb"startxref\n(\d+)",
                         lambda m: b"startxref\n%d" % (int(m.group(1)) + shift), trailer)
    if n != 1 or not pdf.endswith(b"%%EOF\n"):
        raise ValueError("unexpected file layout")
    return body[:head] + MARKER_LINE + body[head:] + xref + trailer + MARKER_LINE


def markers_in(head: bytes, tail: bytes) -> tuple[bool, bool]:
    """(line 2 is the marker, the last line is the marker)."""
    lines = head.split(b"\n", 2)
    return (len(lines) > 2 and lines[1] == MARKER, tail.endswith(MARKER_LINE))
