"""The planted-instance manifest: where every secret was written.

One ``<doc>.truth.json`` per generated PDF. The oracle (a later PR)
classifies each instance in each redacted output; the generator only
records what it planted. Coordinates are PDF points with the origin at
the page's **top-left** (y grows downward), the same frame a render's
pixel rows use, so a crop is ``bbox * dpi / 72``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import persona

# The location taxonomy (plan §1.3). A code names *where* a value sits,
# not what the redactor does to it -- that is measured.
LOCATIONS: dict[str, str] = {
    "T1": "live page text, configured format",
    "T2": "live page text, variant format",
    "T3a": "value wrapped at a simple line end",
    "T3b": "value split across columns or blocks",
    "T3c": "value split at a page break",
    "F1": "form field value (/V and appearance)",
    "F2": "comb form field",
    "A1": "FreeText annotation not overlapping page text",
    "A1x": "FreeText annotation overlapping page text",
    "A2": "sticky-note (Text) annotation /Contents",
    "A3": "highlight annotation /Contents",
    "A4": "link annotation URI",
    "M1": "document Info, standard key",
    "M2": "document Info, custom key",
    "M3": "document XMP",
    "O1": "outline (bookmark) title",
    "E1": "embedded file and its description",
    "S1": "image-only scanned page",
    "S2": "scanned page with an invisible OCR text layer",
    "I1": "raster image on a text page",
    "P1": "page thumbnail (/Thumb)",
    "Q1": "macOS Quartz page text (simple font, MacRoman literal strings)",
}

# How the instance's bytes are stored in the input.
ENCODINGS = frozenset({
    "page-text",    # drawn text: see fingerprints for the operand bytes
    "string",       # a PDF string object (field /V, /Contents, Info, outline, URI)
    "xmp",          # XML text in the XMP stream
    "embedded",     # bytes of an embedded file (or its /Desc string)
    "pixels",       # only in image samples
})


@dataclass
class Instance:
    secret: str                      # persona id
    written: str                     # the exact string as written
    loc: str                         # LOCATIONS key
    encoding: str                    # ENCODINGS member
    page: int | None = None          # 0-based page index, None if not on a page
    bbox: list[float] | None = None  # [x0, y0, x1, y1], points, top-left origin
    parts: list[str] = field(default_factory=list)   # pieces, for a split value
    # Per part: [page, x0, y0, x1, y1] (same frame as bbox).
    part_boxes: list[list[float]] = field(default_factory=list)
    # Hex of the byte sequences that encode `written` (or each part) in
    # the input's own content streams, one per candidate font; empty
    # for instances not stored as drawn text.
    fingerprints: list[str] = field(default_factory=list)
    note: str = ""

    def __post_init__(self) -> None:
        if self.secret not in persona.BY_ID:
            raise ValueError(f"unknown secret id {self.secret!r}")
        if persona.written_forms().get(self.written, self.secret) != self.secret and not self.parts:
            raise ValueError(f"{self.written!r} is a written form of another secret")
        if self.loc not in LOCATIONS:
            raise ValueError(f"unknown location code {self.loc!r}")
        if self.encoding not in ENCODINGS:
            raise ValueError(f"unknown encoding {self.encoding!r}")
        if self.bbox is not None and len(self.bbox) != 4:
            raise ValueError("bbox must be [x0, y0, x1, y1]")


@dataclass
class DocTruth:
    doc: str                  # template or probe id
    seed: int
    producer: str
    pages: int
    title: str
    instances: list[Instance]
    clean_control: bool = False   # carries no secret by design

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocTruth:
        data = dict(data)
        data["instances"] = [Instance(**i) for i in data["instances"]]
        return cls(**data)

    @classmethod
    def load(cls, path: Path) -> DocTruth:
        return cls.from_dict(json.loads(path.read_text()))
