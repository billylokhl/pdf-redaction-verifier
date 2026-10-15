"""Labels, second part (Phase 3a-7e-2, ADR 0011 item 5): the drawn
appearance each annotation records (``Annotation.appearance``, against
MuPDF's own choice), each revision's DRAWABLE streams -- the drawing
closure over item 5's closed key lists, one positive and one negative
case per role and key -- the ORPHANED remainder, per revision and per
body across revisions, the reuse rule condition by condition, and the
walk's cost and memory. Every file is generated here; fabricated content
only."""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest
from scorecard.inventory import compare, drawing_measures
from scorecard.pdfgen import Writer

from redaction_verifier.budget import Budget, Limits
from redaction_verifier.inventory import Inventory, build_inventory
from redaction_verifier.inventory.drawable import (CATALOG, COLOR_SPACE, EXTGSTATE,
                                                   FONT, FORM, FUNCTION, HALFTONE, IMAGE, LEAF,
                                                   PAGE, RESOURCES, role_at)
from redaction_verifier.inventory.graph import ARRAY
from redaction_verifier.inventory.labels import (ORPHAN, BodyLabels, RevisionLabels,
                                                 StructuralKind, build_body_labels)
from redaction_verifier.ledger import FlagReason, UnitKind, UnitRef

from .conftest import requires_qpdf
from .steps import charged_steps
from .test_inventory_annotations import CASES as ANNOTATION_CASES
from .test_inventory_labels import (CAT, LINES_PER_UNIT, PAGES, PER_UNIT_GROWTH, XMP, _retained,
                                    make)

M = pymupdf.mupdf

PAGE_RES = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R"
            b" /Resources << %s >>%s >>")
CONTENT = (b"", b"0 0 m")
IMG = b" /Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8"
GRAY = IMG + b" /ColorSpace /DeviceGray"
IMAGE_ = (GRAY, b"\x00")
FORM_ = b" /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources << %s >>"
ICC = (b" /N 1", b"icc")
FN = (b" /FunctionType 4 /Domain [0 1] /Range [0 1]", b"{ }")
LEAF_ = (b"", b"leaf")
TT = b"<< /Type /Font /Subtype /TrueType /BaseFont /X /FontDescriptor %d 0 R >>"
DESC = b"<< /Type /FontDescriptor /FontName /X /Flags 32 /FontFile2 %d 0 R >>"


def page(res: bytes = b"", extra: bytes = b"", objects: dict[int, bytes] | None = None,
         streams: dict[int, tuple[bytes, bytes]] | None = None, catalog: bytes = b"",
         updates: tuple[dict[int, bytes | None], ...] = ()) -> bytes:
    """Page 3 with resources *res* and *extra* keys, content stream 5."""
    return make(catalog=catalog, objects={3: PAGE_RES % (res, extra)} | (objects or {}),
                streams={5: CONTENT} | (streams or {}), updates=updates)


def _form(n: int, res: bytes = b"", extra: bytes = b"") -> tuple[bytes, bytes]:
    return (FORM_ % res + extra, b"0 0 m")


def _annotated(annot: bytes, objects: dict[int, bytes] | None = None,
               streams: dict[int, tuple[bytes, bytes]] | None = None,
               acroform: bytes = b"") -> bytes:
    """Annotation 4 on page 3; *streams* (6: a stored appearance, usually)."""
    return page(extra=b" /Annots [4 0 R]", objects={4: annot} | (objects or {}),
                streams=streams, catalog=acroform)


SQ = b"/Type /Annot /Subtype /Square /Rect [0 0 50 50]"
AP_FORM = _form(6, b"/XObject << /Im 7 0 R >>")
NEED = b" /AcroForm << /Fields [4 0 R] /NeedAppearances true /DR << /Font << /Helv 11 0 R >> >> >>"
DR_ONLY = b" /AcroForm << /Fields [4 0 R] /DR << /Font << /Helv 11 0 R >> >> >>"
TX = b"/Type /Annot /Subtype /Widget /Rect [0 0 50 50] /FT /Tx /T (t) /DA (/Helv 9 Tf 0 g)"

# (name, file, the current revision's DRAWABLE streams, its ORPHANED ones),
# by object number. Every file is unflagged: labels add no flag (R3).
DRAWABLE: list[tuple[str, bytes, set[int], set[int]]] = [
    # ── a page: /Contents, its own or inherited /Resources, its own /Group
    ("contents", page(), {5}, set()),
    ("contents-array", make(objects={3: PAGE_RES.replace(b"5 0 R", b"[5 0 R 6 0 R]") % (b"", b"")},
                            streams={5: CONTENT, 6: CONTENT}), {5, 6}, set()),
    ("contents-array-by-reference", make(
        objects={3: PAGE_RES.replace(b"5 0 R", b"7 0 R") % (b"", b""), 7: b"[5 0 R 6 0 R]"},
        streams={5: CONTENT, 6: CONTENT}), {5, 6}, set()),
    ("inherited-resources", make(
        objects={2: PAGES.replace(b">>", b"/Resources << /XObject << /Im 4 0 R >> >> >>"),
                 3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R >>"},
        streams={5: CONTENT, 4: IMAGE_}), {4, 5}, set()),
    ("inherited-resources-by-reference", make(
        objects={2: PAGES.replace(b">>", b"/Resources 8 0 R >>"),
                 8: b"<< /XObject << /Im 4 0 R >> >>",
                 3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R >>"},
        streams={5: CONTENT, 4: IMAGE_}), {4, 5}, set()),
    # The page's own /Resources: an ancestor's is not what it draws with.
    ("own-resources-over-an-ancestors", make(
        objects={2: PAGES.replace(b">>", b"/Resources << /XObject << /Im 6 0 R >> >> >>"),
                 3: PAGE_RES % (b"/XObject << /Im 4 0 R >>", b"")},
        streams={5: CONTENT, 4: IMAGE_, 6: IMAGE_}), {4, 5}, {6}),
    ("page-group", page(extra=b" /Group << /S /Transparency /CS [/ICCBased 4 0 R] >>",
                        streams={4: ICC}), {4, 5}, set()),
    # /Group is the page's own, never inherited.
    ("ancestor-group", make(
        objects={2: PAGES.replace(b">>", b"/Resources << >> /Group << /S /Transparency /CS"
                                         b" [/ICCBased 4 0 R] >> >>"),
                 3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R >>"},
        streams={5: CONTENT, 4: ICC}), {5}, {4}),
    # ── an image: /SMask, /Mask, /ColorSpace, /DecodeParms /JBIG2Globals
    ("image", page(b"/XObject << /Im 4 0 R >>", streams={
        4: (IMG + b" /SMask 6 0 R /ColorSpace [/ICCBased 7 0 R] /DecodeParms << /JBIG2Globals"
                  b" 8 0 R >>", b"\x00"),
        6: IMAGE_, 7: ICC, 8: LEAF_}), {4, 5, 6, 7, 8}, set()),
    ("image-mask-and-parameters-array", page(b"/XObject << /Im 4 0 R >>", streams={
        4: (GRAY + b" /Mask 6 0 R /DecodeParms [<< /JBIG2Globals 8 0 R >>]", b"\x00"),
        6: (b" /Type /XObject /Subtype /Image /Width 1 /Height 1 /ImageMask true", b"\x00"),
        8: LEAF_}), {4, 5, 6, 8}, set()),
    ("image-other-keys", page(b"/XObject << /Im 4 0 R >>", streams={
        4: (GRAY + b" /Alternates [<< /Image 6 0 R >>] /Foo 7 0 R"
                   b" /Resources << /XObject << /X 8 0 R >> >> /Group << /CS [/ICCBased 9 0 R] >>",
            b"\x00"),
        6: IMAGE_, 7: LEAF_, 8: IMAGE_, 9: ICC}), {4, 5}, {6, 7, 8, 9}),
    # ── a form: /Resources, /Group (its /CS)
    ("form", page(b"/XObject << /Fm 4 0 R >>", streams={
        4: _form(4, b"/XObject << /Im 6 0 R >>", b" /Group << /S /Transparency /CS"
                                                  b" [/ICCBased 7 0 R] >>"),
        6: IMAGE_, 7: ICC}), {4, 5, 6, 7}, set()),
    ("form-other-keys", page(b"/XObject << /Fm 4 0 R >>", streams={
        4: _form(4, extra=b" /SMask 6 0 R /Mask 7 0 R /Foo 8 0 R /Group << /G 9 0 R >>"),
        6: IMAGE_, 7: IMAGE_, 8: LEAF_, 9: _form(9)}), {4, 5}, {6, 7, 8, 9}),
    # ── an XObject is a form or an image by its /Subtype, read through one
    # reference; any other draws nothing (the stream itself is drawable).
    ("xobject-subtype-ps", page(b"/XObject << /X 4 0 R >>", streams={
        4: (b" /Type /XObject /Subtype /PS /Resources << /XObject << /Im 6 0 R >> >>", b"x"),
        6: IMAGE_}), {4, 5}, {6}),
    ("xobject-subtype-by-reference", page(b"/XObject << /X 4 0 R >>", objects={9: b"/Form"},
                                          streams={4: (b" /Subtype 9 0 R /BBox [0 0 1 1]"
                                                       b" /Resources << /XObject << /Im 6 0 R >>"
                                                       b" >>", b"0 0 m"), 6: IMAGE_}),
     {4, 5, 6}, set()),
    ("xobject-image-by-reference", page(b"/XObject << /X 4 0 R >>", objects={9: b"/Image"},
                                        streams={4: (b" /Subtype 9 0 R /Width 1 /Height 1"
                                                     b" /BitsPerComponent 8 /ColorSpace"
                                                     b" /DeviceGray /SMask 6 0 R", b"\x00"),
                                                 6: IMAGE_}), {4, 5, 6}, set()),
    # A form's /Subtype2 replaces its /Subtype (MuPDF's Do operator): an
    # image then (its /SMask drawn), /PS nothing (nothing under it drawn).
    ("form-subtype2-image", page(b"/XObject << /X 4 0 R >>", streams={
        4: (b" /Subtype /Form /Subtype2 /Image /Width 1 /Height 1 /BitsPerComponent 8"
            b" /ColorSpace /DeviceGray /SMask 6 0 R /BBox [0 0 1 1]"
            b" /Resources << /XObject << /Im 7 0 R >> >>", b"\x00"),
        6: IMAGE_, 7: IMAGE_}), {4, 5, 6}, {7}),
    ("form-subtype2-ps", page(b"/XObject << /X 4 0 R >>", streams={
        4: (b" /Subtype /Form /Subtype2 /PS /BBox [0 0 1 1]"
            b" /Resources << /XObject << /Im 6 0 R >> >>", b"0 0 m"), 6: IMAGE_}), {4, 5}, {6}),
    ("image-subtype2-form", page(b"/XObject << /X 4 0 R >>", streams={
        4: (GRAY + b" /Subtype2 /Form /SMask 6 0 R /BBox [0 0 1 1]"
                   b" /Resources << /XObject << /Im 7 0 R >> >>", b"\x00"),
        6: IMAGE_, 7: IMAGE_}), {4, 5, 6}, {7}),
    ("xobject-not-a-stream", page(b"/XObject << /X 9 0 R >>", objects={
        9: b"<< /Subtype /Form /Resources << /XObject << /Im 6 0 R >> >> >>"},
        streams={6: IMAGE_}), {5}, {6}),
    # An image is no form: its /Resources is not followed; nor is a form an image.
    ("image-holding-resources", page(b"/XObject << /Im 4 0 R >>", streams={
        4: (GRAY + b" /Resources << /XObject << /Im 6 0 R >> >>", b"\x00"), 6: IMAGE_}),
     {4, 5}, {6}),
    # ── a font: /FontDescriptor (/FontFile*, /CIDSet), /ToUnicode, /Encoding
    # (a CMap's /UseCMap), /DescendantFonts, /CIDToGIDMap
    ("font", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype /TrueType /BaseFont /X /FontDescriptor 10 0 R"
           b" /ToUnicode 6 0 R >>",
        10: b"<< /Type /FontDescriptor /FontName /X /FontFile2 7 0 R /CIDSet 8 0 R >>"},
        streams={6: LEAF_, 7: LEAF_, 8: LEAF_}), {5, 6, 7, 8}, set()),
    ("font-file-and-file3", page(b"/Font << /F1 9 0 R /F2 11 0 R >>", objects={
        9: TT % 10, 10: b"<< /Type /FontDescriptor /FontFile 6 0 R >>",
        11: TT % 12, 12: b"<< /Type /FontDescriptor /FontFile3 7 0 R >>"},
        streams={6: LEAF_, 7: LEAF_}), {5, 6, 7}, set()),
    ("font-type0", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding 6 0 R"
           b" /DescendantFonts [11 0 R] /ToUnicode 12 0 R >>",
        11: b"<< /Type /Font /Subtype /CIDFontType2 /FontDescriptor 10 0 R /CIDToGIDMap 8 0 R >>",
        10: DESC % 13},
        streams={6: (b" /Type /CMap /UseCMap 7 0 R", b"cmap"), 7: LEAF_, 8: LEAF_,
                 12: LEAF_, 13: LEAF_}), {5, 6, 7, 8, 12, 13}, set()),
    ("font-descendants-by-reference", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype /Type0 /DescendantFonts 14 0 R >>", 14: b"[11 0 R]",
        11: b"<< /Type /Font /Subtype /CIDFontType0 /FontDescriptor 10 0 R >>", 10: DESC % 13},
        streams={13: LEAF_}), {5, 13}, set()),
    ("font-direct", page(b"/Font << /F1 << /Subtype /TrueType /FontDescriptor << /FontFile2"
                         b" 7 0 R >> >> >>", streams={7: LEAF_}), {5, 7}, set()),
    # A ToUnicode CMap's /UseCMap, a descriptor's /FD and /Style, a simple
    # font's /CharProcs and /Resources: outside the lists.
    ("font-other-keys", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype /TrueType /FontDescriptor 10 0 R /ToUnicode 6 0 R"
           b" /CharProcs << /a 12 0 R >> /Resources << /XObject << /Im 13 0 R >> >> >>",
        10: b"<< /Type /FontDescriptor /FD << /X << /FontFile2 8 0 R >> >> /Foo 14 0 R >>"},
        streams={6: (b" /UseCMap 7 0 R", b"cmap"), 7: LEAF_, 8: LEAF_, 12: LEAF_, 13: IMAGE_,
                 14: LEAF_}), {5, 6}, {7, 8, 12, 13, 14}),
    # A Type3 font (its /Subtype read through one reference): /CharProcs
    # and /Resources; a glyph is drawn as it is (its own keys not followed).
    ("type3", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1 1] /FontMatrix [1 0 0 1 0 0]"
           b" /CharProcs << /a 6 0 R /Resources 8 0 R >> /Resources << /XObject << /Im 7 0 R >>"
           b" >> /Encoding << /Differences [0 /a] >> /FirstChar 0 /LastChar 0 /Widths [1] >>"},
        streams={6: (b" /Resources << /XObject << /Im 12 0 R >> >>", b"0 0 m"), 7: IMAGE_,
                 8: LEAF_, 12: IMAGE_}), {5, 6, 7, 8}, {12}),
    ("type3-subtype-by-reference", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype 10 0 R /CharProcs << /a 6 0 R >> /Resources << >> >>",
        10: b"/Type3"}, streams={6: LEAF_}), {5, 6}, set()),
    # A name is read through references (as MuPDF reads one): a TrueType
    # font two references away is no Type3, whatever its /CharProcs.
    ("truetype-subtype-two-references", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype 10 0 R /CharProcs << /a 6 0 R >> /Resources << >> >>",
        10: b"11 0 R", 11: b"/TrueType"}, streams={6: LEAF_}), {5}, {6}),
    # No /Subtype, or one MuPDF does not load by name, with /CharProcs:
    # guessed Type3 ("unknown font format, guessing type3").
    ("type3-guessed", page(b"/Font << /F1 9 0 R /F2 10 0 R /F3 11 0 R >>", objects={
        9: b"<< /Type /Font /CharProcs << /a 6 0 R >> /Resources << >> >>",
        10: b"<< /Type /Font /Subtype /Bogus /CharProcs << /a 7 0 R >> /Resources << >> >>",
        11: b"<< /Type /Font /Subtype /CIDFontType2 /DescendantFonts [] /CharProcs << /a 8 0 R >>"
            b" /Resources << /XObject << /Im 12 0 R >> >> >>"},
        streams={6: LEAF_, 7: LEAF_, 8: LEAF_, 12: IMAGE_}), {5, 6, 7, 8, 12}, set()),
    # A Type0 font's descendant is a CID font, never guessed Type3.
    ("descendant-never-type3", page(b"/Font << /F1 9 0 R >>", objects={
        9: b"<< /Type /Font /Subtype /Type0 /DescendantFonts [10 0 R] >>",
        10: b"<< /Type /Font /CharProcs << /a 6 0 R >> /Resources << >> >>"},
        streams={6: LEAF_}), {5}, {6}),
    # ── a colour space: every element of its array; in a dictionary or
    # stream so reached only /Functions, /Colorants, /Process (its /ColorSpace)
    ("color-spaces", page(
        b"/ColorSpace << /C0 [/ICCBased 4 0 R] /C1 [/Indexed /DeviceRGB 1 6 0 R]"
        b" /C2 [/DeviceN [/A] /DeviceGray 7 0 R << /Colorants << /A [/Separation /A /DeviceGray"
        b" 8 0 R] >> /Process << /ColorSpace [/ICCBased 9 0 R] /Components [/Gray] >> >>] >>",
        streams={4: ICC, 6: LEAF_, 7: FN, 8: FN, 9: ICC}), {4, 5, 6, 7, 8, 9}, set()),
    ("color-space-function-functions", page(
        b"/ColorSpace << /C0 [/Separation /A /DeviceGray << /FunctionType 3 /Domain [0 1]"
        b" /Functions [7 0 R] /Bounds [] /Encode [0 1] >>] >>", streams={7: FN}), {5, 7}, set()),
    ("color-space-by-reference", page(b"/ColorSpace << /C0 9 0 R >>",
                                      objects={9: b"[/ICCBased 4 0 R]"}, streams={4: ICC}),
     {4, 5}, set()),
    # An ICC profile's /Alternate and /Metadata, a function's and an
    # attributes dictionary's other keys: not followed (Q2).
    ("color-space-other-keys", page(
        b"/ColorSpace << /C0 [/ICCBased 4 0 R] /C1 [/DeviceN [/A] /DeviceGray 7 0 R"
        b" << /Foo 9 0 R /Process << /Foo 10 0 R >> >>] >>",
        streams={4: (b" /N 1 /Alternate [/ICCBased 6 0 R] /Metadata 8 0 R", b"icc"), 6: ICC,
                 7: (FN[0] + b" /Foo 12 0 R", FN[1]), 8: (b"", XMP), 9: LEAF_, 10: LEAF_,
                 12: LEAF_}), {4, 5, 7}, {6, 8, 9, 10, 12}),
    # ── a shading: /Function (or each in its array), /ColorSpace; a function:
    # /Functions
    ("shading", page(b"/Shading << /Sh0 4 0 R /Sh1 9 0 R >>", objects={
        9: b"<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [0 0 1 0] /Function [6 0 R 10 0 R]"
           b" >>",
        10: b"<< /FunctionType 3 /Domain [0 1] /Functions [12 0 R] /Bounds [] /Encode [0 1] >>"},
        streams={4: (b" /ShadingType 4 /ColorSpace [/ICCBased 7 0 R] /BitsPerCoordinate 8"
                     b" /BitsPerComponent 8 /BitsPerFlag 8 /Decode [0 1 0 1 0 1]"
                     b" /Function 8 0 R", b""),
                 6: FN, 7: ICC, 8: FN, 12: FN}), {4, 5, 6, 7, 8, 12}, set()),
    ("shading-other-keys", page(b"/Shading << /Sh0 9 0 R >>", objects={
        9: b"<< /ShadingType 2 /ColorSpace /DeviceGray /Coords [0 0 1 0] /Function 6 0 R"
           b" /Foo 7 0 R /Background [0] >>"},
        streams={6: (FN[0] + b" /Foo 8 0 R", FN[1]), 7: LEAF_, 8: LEAF_}), {5, 6}, {7, 8}),
    # ── a pattern: /Resources, /Shading, /ExtGState
    ("patterns", page(b"/Pattern << /P0 4 0 R /P1 9 0 R >>", objects={
        9: b"<< /PatternType 2 /Shading 7 0 R /ExtGState << /SMask << /S /Luminosity /G 8 0 R"
           b" >> >> >>"},
        streams={4: (b" /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 1 1] /XStep 1"
                     b" /YStep 1 /Resources << /XObject << /Im 6 0 R >> >>", b"0 0 m"),
                 6: IMAGE_,
                 7: (b" /ShadingType 4 /ColorSpace /DeviceGray /BitsPerCoordinate 8"
                     b" /BitsPerComponent 8 /BitsPerFlag 8 /Decode [0 1 0 1 0 1]", b""),
                 8: _form(8)}), {4, 5, 6, 7, 8}, set()),
    ("pattern-other-keys", page(b"/Pattern << /P0 4 0 R >>", streams={
        4: (b" /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 1 1] /XStep 1 /YStep 1"
            b" /Resources << >> /Foo 6 0 R /Group << /CS [/ICCBased 7 0 R] >>", b"0 0 m"),
        6: LEAF_, 7: ICC}), {4, 5}, {6, 7}),
    # ── a graphics state: /SMask (/G, /TR), /TR, /TR2, /BG, /BG2, /UCR,
    # /UCR2, /HT (/TransferFunction; type 5: every sub-halftone), /Font
    ("graphics-state", page(
        b"/ExtGState << /G0 << /SMask << /S /Alpha /G 4 0 R /TR 6 0 R >> /TR 7 0 R"
        b" /TR2 [7 0 R 7 0 R 7 0 R 8 0 R] /BG 9 0 R /BG2 9 0 R /UCR 10 0 R /UCR2 10 0 R"
        b" /HT 11 0 R /Font [14 0 R 12] >> >>",
        objects={11: b"<< /HalftoneType 5 /Default << /HalftoneType 1 /Frequency 60 /Angle 0"
                     b" /SpotFunction /Round /TransferFunction 12 0 R >> /Cyan 13 0 R >>",
                 14: TT % 15, 15: DESC % 16},
        streams={4: _form(4), 6: FN, 7: FN, 8: FN, 9: FN, 10: FN, 12: FN,
                 13: (b" /HalftoneType 6 /Width 1 /Height 1", b"\x00"), 16: LEAF_}),
     {4, 5, 6, 7, 8, 9, 10, 12, 13, 16}, set()),
    ("halftone-type-by-reference", page(b"/ExtGState << /G0 << /HT << /HalftoneType 9 0 R"
                                        b" /Default 13 0 R >> >> >>", objects={9: b"5"},
                                        streams={13: (b" /HalftoneType 6 /Width 1 /Height 1",
                                                      b"\x00")}), {5, 13}, set()),
    # Not type 5: no sub-halftones; a /SpotFunction is outside the list.
    ("halftone-other-keys", page(b"/ExtGState << /G0 << /HT << /HalftoneType 1 /Frequency 60"
                                 b" /Angle 0 /SpotFunction 6 0 R /Default 7 0 R /Foo 8 0 R >>"
                                 b" /Foo 9 0 R /SMask << /Foo 10 0 R >> >> >>",
                                 streams={6: FN, 7: (b" /HalftoneType 6 /Width 1 /Height 1",
                                                     b"\x00"), 8: LEAF_, 9: LEAF_, 10: LEAF_}),
     {5}, {6, 7, 8, 9, 10}),
    # A type 5 halftone's own keys are not sub-halftones.
    ("halftone-type5-own-keys", page(b"/ExtGState << /G0 << /HT << /HalftoneType 5"
                                     b" /HalftoneName 7 0 R /Default 13 0 R >> >> >>",
                                     streams={7: LEAF_, 13: (b" /HalftoneType 6 /Width 1 /Height 1",
                                                             b"\x00")}), {5, 13}, {7}),
    ("halftone-type-two-references", page(b"/ExtGState << /G0 << /HT << /HalftoneType 9 0 R"
                                          b" /Default 13 0 R >> >> >>",
                                          objects={9: b"10 0 R", 10: b"5"},
                                          streams={13: (b" /HalftoneType 6 /Width 1 /Height 1",
                                                        b"\x00")}), {5, 13}, set()),
    # ── a resource dictionary: /Properties draws nothing; other keys neither.
    ("resources-other-keys", page(b"/Properties << /MC0 << /Foo 4 0 R >> >> /Foo << /X 6 0 R >>"
                                  b" /ProcSet [/PDF]", streams={4: LEAF_, 6: IMAGE_}),
     {5}, {4, 6}),
    # ── outside the closure: a page's thumbnail, private data, other keys;
    # a template page; an unreferenced stream; one an unreachable object holds
    ("page-other-keys", page(extra=b" /Thumb 4 0 R /PieceInfo << /X << /Private 6 0 R >> >>"
                                   b" /Foo 7 0 R /B [8 0 R]",
                             objects={8: b"<< /Foo 9 0 R >>"},
                             streams={4: IMAGE_, 6: LEAF_, 7: LEAF_, 9: LEAF_}),
     {5}, {4, 6, 7, 9}),
    ("template-page", page(catalog=b" /Names << /Templates << /Names [(t) 8 0 R] >> >>",
                           objects={8: b"<< /Type /Template /MediaBox [0 0 10 10] /Contents 9 0 R"
                                       b" /Resources << /XObject << /Im 10 0 R >> >> >>"},
                           streams={9: CONTENT, 10: IMAGE_}), {5}, {9, 10}),
    ("unreferenced-and-unreachable", page(objects={8: b"<< /Contents 9 0 R >>"},
                                          streams={6: LEAF_, 9: LEAF_}), {5}, {6, 9}),
    # A declared type is not a role: an image under a key outside the lists.
    ("declared-type-under-another-key", page(catalog=b" /Foo 4 0 R", streams={4: IMAGE_}),
     {5}, {4}),
    # ── annotations: the stream MuPDF draws for one it does not synthesize
    ("stored-appearance", _annotated(b"<< %s /AP << /N 6 0 R >> >>" % SQ,
                                     streams={6: AP_FORM, 7: IMAGE_}), {5, 6, 7}, set()),
    # /AS selects the state; /D, /R and other states are not drawn.
    ("appearance-states", _annotated(
        b"<< %s /AS /On /AP << /N << /On 6 0 R /Off 8 0 R >> /D << /On 9 0 R >> /R 10 0 R >> >>"
        % SQ, streams={6: AP_FORM, 7: IMAGE_, 8: _form(8), 9: _form(9), 10: _form(10)}),
     {5, 6, 7}, {8, 9, 10}),
    ("link-and-popup-appearances", page(
        extra=b" /Annots [4 0 R 12 0 R]",
        objects={4: b"<< /Type /Annot /Subtype /Link /Rect [0 0 9 9] /AP << /N 6 0 R >> >>",
                 12: b"<< /Type /Annot /Subtype /Popup /Rect [0 0 9 9] /AP << /N 8 0 R >> >>"},
        streams={6: _form(6), 8: _form(8)}), {5}, {6, 8}),
    # A widget's stored /AP under /NeedAppearances is not drawn; the
    # /AcroForm /DR resources draw the synthesized one.
    ("need-appearances", _annotated(b"<< %s /AP << /N 6 0 R >> >>" % TX,
                                    objects={11: TT % 12, 12: DESC % 13},
                                    streams={6: AP_FORM, 7: IMAGE_, 13: LEAF_}, acroform=NEED),
     {5, 13}, {6, 7}),
    # No annotation synthesized: /DR draws nothing.
    ("default-resources-unused", _annotated(b"<< %s /AP << /N 6 0 R >> >>" % TX,
                                            objects={11: TT % 12, 12: DESC % 13},
                                            streams={6: AP_FORM, 7: IMAGE_, 13: LEAF_},
                                            acroform=DR_ONLY), {5, 6, 7}, {13}),
    ("signed-signature-stored", _annotated(
        b"<< /Type /Annot /Subtype /Widget /Rect [0 0 50 50] /FT /Sig /T (s) /V 9 0 R"
        b" /AP << /N 6 0 R >> >>", objects={9: b"<< /Type /Sig >>"},
        streams={6: AP_FORM, 7: IMAGE_}, acroform=b" /AcroForm << /Fields [4 0 R] >>"),
     {5, 6, 7}, set()),
    # ── drawn by name: a role comes from where an entry is, never its name
    ("category-entries-named-like-keys", page(
        b"/XObject << /Resources 4 0 R /Contents 6 0 R /SMask 8 0 R >>"
        b" /ExtGState << /SMask << /SMask << /G 9 0 R >> >> >>"
        b" /ColorSpace << /Functions [/ICCBased 10 0 R] >>",
        streams={4: IMAGE_, 6: _form(6, b"/XObject << /Im 7 0 R >>"), 7: IMAGE_, 8: IMAGE_,
                 9: _form(9), 10: ICC}), {4, 5, 6, 7, 8, 9, 10}, set()),
    ("a-category-outside-resources", page(extra=b" /Foo << /XObject << /Im 4 0 R >> >>",
                                          streams={4: IMAGE_}), {5}, {4}),
    ("colorants-named-like-keys", page(
        b"/ColorSpace << /C0 [/DeviceN [/Process] /DeviceGray 7 0 R << /Colorants << /Process"
        b" [/Separation /Process /DeviceGray 8 0 R] /ColorSpace [/ICCBased 9 0 R] >> >>] >>",
        streams={7: FN, 8: FN, 9: ICC}), {5, 7, 8, 9}, set()),
    # A stream both STRUCTURAL and DRAWABLE: drawable (not orphaned).
    ("contents-also-metadata", page(catalog=b" /Metadata 5 0 R"), {5}, set()),
]


def _inventory(data: bytes, limits: Limits | None = None) -> Inventory:
    return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)))


def _labels(inv: Inventory, revision: int = 0) -> RevisionLabels:
    found = inv.labels[revision]
    assert found is not None
    return found


def _drawn(inv: Inventory, revision: int = 0) -> set[int | None]:
    return {unit.obj for unit in _labels(inv, revision).drawable}


def _orphans(inv: Inventory, revision: int = 0) -> set[int | None]:
    return {unit.obj for unit in inv.orphaned(revision)}


@pytest.mark.parametrize(("data", "drawn", "orphans"), [(d, a, o) for _, d, a, o in DRAWABLE],
                         ids=[name for name, _, _, _ in DRAWABLE])
def test_the_drawing_closure_follows_each_roles_list(data: bytes, drawn: set[int],
                                                     orphans: set[int]) -> None:
    inv = _inventory(data)
    assert inv.flags == (), inv.flags  # labels add no flag (R3)
    assert _drawn(inv) == drawn
    assert _orphans(inv) == orphans
    # Per body (one revision): the same.
    by_body = {u.obj for u, b in inv.body_labels.items() if b.drawable}
    assert by_body == drawn
    assert {u.obj for u, b in inv.body_labels.items() if b.orphaned} == orphans


@requires_qpdf
def test_the_drawable_fixtures_agree_with_the_gate(tmp_path: Path) -> None:
    # Every fixture above is a file both readers read as we do (the gate's
    # every check, REACHABLE and the annotations' drawing included) -- but
    # those holding an object whose value is a lone reference (`10 0 obj
    # 11 0 R endobj`): qpdf --check warns "expected endobj" and the
    # inventory does not flag it (as test_inventory_annotations' GATED).
    failed = []
    for name, data, _, _ in DRAWABLE:
        if name.endswith("-two-references"):
            continue
        found = compare(data, tmp_path)
        if not (found.agrees and found.reachable_compared == 1):
            failed.append((name, found.check, found.templates))
    assert failed == []


def test_a_stream_both_structural_and_drawable_keeps_both() -> None:
    inv = _inventory(DRAWABLE[[n for n, *_ in DRAWABLE].index("contents-also-metadata")][1])
    (unit,) = [u for u in inv.body_labels if u.obj == 5]
    assert inv.labels_of(unit) == BodyLabels(True, (StructuralKind.XMP,))
    assert not inv.labels_of(unit).orphaned


# ── The automaton, role by role (each list's keys; others drop the edge) ──
@pytest.mark.parametrize(("role", "path", "expected"), [
    (PAGE, (b"Contents",), 4), (PAGE, (b"Contents", ARRAY), LEAF), (PAGE, (b"Thumb",), None),
    (PAGE, (b"Resources", b"XObject", b"X"), 13), (PAGE, (b"Resources", b"Properties", b"P"), None),
    (RESOURCES, (b"XObject", ARRAY), None), (RESOURCES, (b"Font", b"F", b"ToUnicode"), LEAF),
    (FORM, (b"Group", b"CS", ARRAY), COLOR_SPACE), (FORM, (b"SMask",), None),
    (IMAGE, (b"SMask",), IMAGE), (IMAGE, (b"DecodeParms", ARRAY, b"JBIG2Globals"), LEAF),
    (IMAGE, (b"DecodeParms", ARRAY, ARRAY, b"JBIG2Globals"), None), (IMAGE, (b"Resources",), None),
    (COLOR_SPACE, (ARRAY, ARRAY), COLOR_SPACE), (COLOR_SPACE, (b"Alternate",), None),
    (COLOR_SPACE, (b"Process", b"ColorSpace"), COLOR_SPACE), (COLOR_SPACE, (b"Process", b"X"), None),
    (FUNCTION, (b"Functions", ARRAY), FUNCTION), (FUNCTION, (b"Domain",), None),
    (EXTGSTATE, (b"HT", b"Default"), None), (EXTGSTATE, (b"Font", ARRAY), FONT),
    (EXTGSTATE, (b"SMask", b"G"), FORM), (EXTGSTATE, (b"SMask", b"BC"), None),
    (HALFTONE, (b"TransferFunction",), FUNCTION), (CATALOG, (b"AcroForm", b"DR"), RESOURCES),
    (CATALOG, (b"AcroForm", b"Fields", ARRAY), None), (FONT, (b"CharProcs", b"a"), None),
])
def test_role_at_reads_each_list(role: int, path: tuple[bytes, ...], expected: int | None) -> None:
    assert role_at(role, path) == expected


def _xobject(subtype: bytes, smask: bytes, image: bytes,
             objects: dict[int, bytes] | None = None) -> bytes:
    """XObject 4 with *subtype*, image and form keys both: as an image (5
    bytes of samples) it draws soft mask 6; as a form (its content `/X Do`)
    image 7."""
    gray = (b" /Type /XObject /Subtype /Image /Width 5 /Height 1 /BitsPerComponent 8"
            b" /ColorSpace /DeviceGray")
    return page(b"/XObject << /Im 4 0 R >>", objects=objects, streams={
        5: (b"", b"q 100 0 0 100 20 20 cm /Im Do Q"),
        4: (b" /Type /XObject %s /Width 5 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceGray"
            b" /SMask 6 0 R /BBox [0 0 1 1] /Resources << /XObject << /X 7 0 R >> >>" % subtype,
            b"/X Do"),
        6: (gray, smask), 7: (gray.replace(b"/Width 5", b"/Width 2"), image)})


def _pixels(data: bytes) -> bytes:
    return bytes(pymupdf.open(stream=data, filetype="pdf")[0].get_pixmap().samples)


@pytest.mark.parametrize(("subtype", "objects"), [
    (b"/Subtype /Form", None), (b"/Subtype /Image", None), (b"/Subtype /PS", None), (b"", None),
    (b"/Subtype /Form /Subtype2 /Form", None), (b"/Subtype /Form /Subtype2 /Image", None),
    (b"/Subtype /Image /Subtype2 /Form", None), (b"/Subtype /Form /Subtype2 /PS", None),
    (b"/Subtype /Form /Subtype2 /Bogus", None), (b"/Subtype /Form /Subtype2 7", None),
    (b"/Subtype /Form /Subtype2 null", None),
    (b"/Subtype /Form /Subtype2 9 0 R", {9: b"10 0 R", 10: b"/Image"}),
    (b"/Subtype 9 0 R /Subtype2 /Image", {9: b"/Form"}),
    (b"/Subtype 9 0 R", {9: b"10 0 R", 10: b"/Image"}),
])
def test_an_xobject_is_drawn_as_mupdf_reads_its_subtype(subtype: bytes,
                                                        objects: dict[int, bytes] | None) -> None:
    # Each stream is DRAWABLE exactly when MuPDF's drawing changes as it does.
    data = _xobject(subtype, b"\x00\xff\x00\xff\x00", b"\x00\xff", objects)
    inv = _inventory(data)
    drawn = _drawn(inv)
    base = _pixels(data)
    assert (_pixels(_xobject(subtype, b"\xff\x00\xff\x00\xff", b"\x00\xff", objects)) != base) \
        == (6 in drawn)
    assert (_pixels(_xobject(subtype, b"\x00\xff\x00\xff\x00", b"\xff\x00", objects)) != base) \
        == (7 in drawn)


def _broken_icc(alternate: bytes, stream: tuple[bytes, bytes]) -> bytes:
    """A page filling with colour space [/ICCBased 4 0 R], profile 4
    unreadable (MuPDF: "ignoring broken ICC profile", so it draws with the
    /Alternate), whose /Alternate names stream 7."""
    return page(b"/ColorSpace << /C0 [/ICCBased 4 0 R] >>", streams={
        5: (b"", b"/C0 cs 1 scn 0 0 100 100 re f"),
        4: (b" /N 1 /Alternate " + alternate, b"icc"), 7: stream})


_TINT = b" /FunctionType 4 /Domain [0 1] /Range [0 1 0 1 0 1]"
BROKEN_ICC = [
    ("separation-tint-function", b"[/Separation /A /DeviceRGB 7 0 R]",
     (_TINT, b"{ pop 1 0 0 }"), (_TINT, b"{ pop 0 0 1 }")),
    ("indexed-lookup", b"[/Indexed /DeviceRGB 1 7 0 R]",
     (b"", b"\x00\x00\x00\xff\x00\x00"), (b"", b"\x00\x00\x00\x00\x00\xff")),
]


@pytest.mark.parametrize(("alternate", "stream", "mutated"),
                         [(a, s, m) for _, a, s, m in BROKEN_ICC],
                         ids=[n for n, _, _, _ in BROKEN_ICC])
def test_known_gap_a_broken_icc_profiles_alternate_is_drawn_but_orphaned(
        alternate: bytes, stream: tuple[bytes, bytes], mutated: tuple[bytes, bytes]) -> None:
    # KNOWN GAP, recorded in ADR 0011's note on a colour-space array's
    # elements (Q2, coordinator decision 2026-10-15: measured gaps go to
    # notes, never silent widening). An ICC profile's /Alternate is not
    # followed, so a stream there is ORPHANED -- failing closed -- though
    # MuPDF draws it when the profile is broken. Fails if the lists are
    # widened (update the note with them) or if MuPDF stops drawing it
    # (the note is then stale).
    data = _broken_icc(alternate, stream)
    inv = _inventory(data)
    assert inv.flags == ()
    assert 4 in _drawn(inv) and 7 not in _drawn(inv)  # (a) ours: orphaned
    assert 7 in _orphans(inv)
    assert _pixels(_broken_icc(alternate, mutated)) != _pixels(data)  # (b) MuPDF draws it


T3_ = (b"<< /Type /Font %s /FontBBox [0 0 10 10] /FontMatrix [0.1 0 0 0.1 0 0]"
       b" /CharProcs << /a 6 0 R >> /Encoding << /Differences [97 /a] >> /FirstChar 97"
       b" /LastChar 97 /Widths [10] /Resources << >> >>")


def _glyph_font(subtype: bytes, glyph: bytes, objects: dict[int, bytes] | None = None) -> bytes:
    return page(b"/Font << /F1 9 0 R >>", objects={9: T3_ % subtype} | (objects or {}),
                streams={5: (b"", b"BT /F1 80 Tf 20 20 Td (a) Tj ET"),
                         6: (b"", b"10 0 0 0 10 10 d1 " + glyph)})


@pytest.mark.parametrize(("subtype", "objects"), [
    (b"/Subtype /Type3", None), (b"", None), (b"/Subtype /Bogus", None), (b"/Subtype 3", None),
    (b"/Subtype /CIDFontType2", None), (b"/DescendantFonts []", None),
    (b"/Subtype /TrueType", None), (b"/Subtype /Type1", None), (b"/Subtype /MMType1", None),
    (b"/Subtype 10 0 R", {10: b"11 0 R", 11: b"/TrueType"}),
    (b"/Subtype 10 0 R", {10: b"11 0 R", 11: b"/Type3"}),
])
def test_a_font_is_type3_as_mupdf_decides(subtype: bytes,
                                          objects: dict[int, bytes] | None) -> None:
    # The glyph is DRAWABLE exactly when MuPDF's drawing changes with it.
    data = _glyph_font(subtype, b"0 0 10 10 re f", objects)
    inv = _inventory(data)
    changed = _pixels(_glyph_font(subtype, b"0 0 3 3 re f", objects)) != _pixels(data)
    assert (6 in _drawn(inv)) == changed


def test_a_decided_role_takes_the_narrower_reading_without_its_value() -> None:
    # Mid-path, a direct XObject draws nothing; unread, a halftone is not
    # type 5 and a font not Type3.
    assert role_at(RESOURCES, (b"XObject", b"X", b"Resources")) is None
    assert role_at(RESOURCES, (b"XObject", b"X")) == 13  # decided when entered
    assert role_at(EXTGSTATE, (b"HT", b"Default", b"TransferFunction")) is None
    assert role_at(EXTGSTATE, (b"HT", b"TransferFunction")) == FUNCTION
    assert role_at(RESOURCES, (b"Font", b"F", b"CharProcs", b"a")) is None
    assert role_at(HALFTONE, (b"Default",)) is None


# ── Annotation.appearance: the stream MuPDF draws (pdf_annot_ap) ──────────
def _mupdf_appearances(data: bytes) -> list[int | None]:
    """The stream MuPDF draws for each annotation on page 0, in its list
    order (annotations, then widgets): pdf_annot_ap's object when
    pdf_is_stream holds of it -- a reference whose object is a stream, one
    reference away; MuPDF follows no chain to a stream (it draws nothing,
    "content stream is not a stream")."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    raw = M.pdf_page_from_fz_page(doc[0].this)
    found: list[int | None] = []
    for first, step in ((M.pdf_first_annot, M.pdf_next_annot),
                        (M.pdf_first_widget, M.pdf_next_widget)):
        annot = first(raw)
        while annot.m_internal:
            obj = M.pdf_annot_ap(annot)
            stream = obj.m_internal and M.pdf_is_indirect(obj) and M.pdf_is_stream(obj)
            found.append(M.pdf_to_num(obj) if stream else None)
            annot = step(annot)
    return found


def _chained(where: str, content: bytes = b"0 0 m") -> bytes:
    """A square whose /AS (*where* "as"), /AP (*where* "ap"), /N (a
    dictionary, *where* "n-dictionary"), state entry ("entry") or /N
    stream ("n") is given through two references (`20 0 obj 21 0 R`)."""
    annot = {"as": b"<< %s /AS 20 0 R /AP << /N << /On 12 0 R >> >> >>",
             "ap": b"<< %s /AS /On /AP 20 0 R >>",
             "n-dictionary": b"<< %s /AS /On /AP << /N 20 0 R >> >>",
             "entry": b"<< %s /AS /On /AP << /N << /On 20 0 R >> >> >>",
             "n": b"<< %s /AP << /N 20 0 R >> >>"}[where] % SQ
    target = {"as": b"/On", "ap": b"<< /N << /On 12 0 R >> >>",
              "n-dictionary": b"<< /On 12 0 R >>"}.get(where, b"12 0 R")
    return _annotated(annot, objects={20: b"21 0 R", 21: target},
                      streams={12: (FORM_ % b"", content)})


CHAINED = ["as", "ap", "n-dictionary", "entry", "n"]
APPEARANCES = [(name, data) for name, data, _ in ANNOTATION_CASES] + [
    (f"{where}-through-references", _chained(where)) for where in CHAINED] + [
    # An /AS entry naming a dictionary, not a stream: nothing drawn.
    ("as-entry-a-dictionary", _annotated(b"<< %s /AS /On /AP << /N << /On 9 0 R >> >> >>" % SQ,
                                         objects={9: b"<< /Foo 1 >>"}))]


@pytest.mark.parametrize("data", [d for _, d in APPEARANCES], ids=[n for n, _ in APPEARANCES])
def test_the_appearance_is_the_stream_mupdf_draws(data: bytes) -> None:
    # Every annotation 3a-7d classifies: not synthesized, ours is
    # pdf_annot_ap's stream (None where that is no stream); synthesized, None.
    inv = _inventory(data)
    found = inv.annotations[0]
    assert found is not None
    theirs = _mupdf_appearances(data)
    assert len(theirs) == len(found)
    for annotation, mupdf in zip(found, theirs):
        if annotation.synthesized:
            assert annotation.appearance is None
        else:
            body = inv.body(mupdf, 0) if mupdf is not None else None
            stream = body is not None and body.start in inv.stream_data
            assert annotation.appearance == (mupdf if stream else None), (annotation, mupdf)


def test_structural_streams_are_not_orphaned() -> None:
    # Cross-reference and object streams, an XMP packet: STRUCTURAL, so
    # neither ORPHANED in the revision nor for the verdict.
    from .test_inventory_labels import _object_streams
    w = Writer()
    w.out = bytearray(_object_streams())
    prev = int(bytes(w.out).rsplit(b"startxref", 1)[1].split()[0])
    at = {1: w.obj(1, CAT % b" /Metadata 7 0 R"), 7: w.stream(7, b"", XMP)}
    w.epilogue(w.table({n: (1, a, 0) for n, a in at.items()},
                       b"/Size 12 /Root 1 0 R /Prev %d" % prev))
    inv = _inventory(bytes(w.out))
    assert inv.flags == ()
    assert {u.obj for u in inv.orphaned(0)} == set()
    assert {u.obj for u in inv.orphaned(1)} == set()
    kinds = {u.obj: b.structural for u, b in inv.body_labels.items()}
    assert kinds == {7: (StructuralKind.XMP,), 10: (StructuralKind.OBJECT_STREAM,),
                     11: (StructuralKind.XREF_STREAM,)}


def test_a_revision_walked_again_to_the_same_drawable_set_shares_it() -> None:
    # The page gains a thumbnail: walked again (its references changed),
    # to the same DRAWABLE set -- kept once, by identity.
    data = page(b"/XObject << /Im 4 0 R >>", streams={4: IMAGE_, 6: IMAGE_},
                updates=({3: PAGE_RES % (b"/XObject << /Im 4 0 R >>", b" /Thumb 6 0 R")},))
    inv = _inventory(data)
    assert inv.flags == () and _labels(inv, 0) is not _labels(inv, 1)
    assert _labels(inv, 0).drawable is _labels(inv, 1).drawable


@pytest.mark.parametrize(("where", "drawn"), [("as", {5, 12}), ("ap", {5, 12}),
                                              ("n-dictionary", {5, 12}), ("entry", {5}),
                                              ("n", {5})])
def test_a_chained_appearance_is_drawn_as_mupdf_draws_it(where: str, drawn: set[int]) -> None:
    # Names and dictionaries are read through references; a stream only one
    # reference away: a chained state entry draws nothing (not synthesized),
    # a chained /N stream is no stream (synthesized). MuPDF's own drawing:
    # the stored form fills nothing here, so the pixels tell the three
    # apart only by whether 12 is drawn -- pinned by mutating it.
    data = _chained(where)
    inv = _inventory(data)
    assert _drawn(inv) == drawn
    (annotation,) = inv.annotations[0] or ()
    assert annotation.synthesized == (where == "n")
    assert annotation.appearance == (12 if 12 in drawn else None)
    filled = _chained(where, b"0 0 10 10 re f")
    pixels = [bytes(pymupdf.open(stream=d)[0].get_pixmap().samples) for d in (data, filled)]
    assert (pixels[0] != pixels[1]) == (12 in drawn)


# ── Per body, across revisions (ADR 0011 item 5, "Across revisions") ─────
def test_a_body_drawable_only_in_an_older_revision_is_not_orphaned() -> None:
    # Revision 1 draws 4; revision 0 draws 6 instead: 4 is orphaned in
    # revision 0's sense, but not for the verdict (a revision it lives in
    # draws it).
    data = page(streams={4: LEAF_, 6: LEAF_}, objects={3: PAGE_RES.replace(b"5 0 R", b"4 0 R")
                                                       % (b"", b"")},
                updates=({3: PAGE_RES.replace(b"5 0 R", b"6 0 R") % (b"", b"")},))
    inv = _inventory(data)
    assert inv.flags == ()
    assert _drawn(inv, 1) == {4} and _drawn(inv, 0) == {6}
    assert _orphans(inv, 0) == {4, 5} and _orphans(inv, 1) == {5, 6}
    labels = {u.obj: b for u, b in inv.body_labels.items()}
    assert labels[4].drawable and labels[6].drawable and labels[5].orphaned


def test_a_new_body_under_a_drawn_number_gets_no_label_from_the_old() -> None:
    # Revision 1 draws 4; revision 0 rewrites object 4 (a new body) and
    # draws 6: the new body of 4 is orphaned, the old one drawable.
    data = page(streams={4: LEAF_, 6: LEAF_}, objects={3: PAGE_RES.replace(b"5 0 R", b"4 0 R")
                                                       % (b"", b"")},
                updates=({3: PAGE_RES.replace(b"5 0 R", b"6 0 R") % (b"", b""),
                          4: b"<< /Length 3 >>\nstream\nnew\nendstream"},))
    inv = _inventory(data)
    assert inv.flags == ()
    old, new = sorted((u for u in inv.body_labels if u.obj == 4), key=lambda u: u.start)
    assert inv.labels_of(old).drawable and inv.labels_of(new).orphaned
    assert inv.orphaned(0) >= {new} and old not in inv.orphaned(0)


def test_a_dead_body_stream_is_orphaned() -> None:
    data = page()
    marker = data.index(b"xref")
    dead = b"9 0 obj\n<< /Length 4 >>\nstream\nlost\nendstream\nendobj\n"
    w = Writer()
    w.out = bytearray(data[:marker] + dead)
    # Re-write the table after the dead body (offsets before it unchanged).
    import re
    offsets = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", data[:marker])}
    w.epilogue(w.table({0: (0, 0, 65535)} | {n: (1, a, 0) for n, a in offsets.items()},
                       b"/Size %d /Root 1 0 R" % (max(offsets) + 1)))
    inv = _inventory(bytes(w.out))
    assert inv.flags == ()
    (unit,) = [u for u in inv.body_labels if u.kind is UnitKind.DEAD_BODY]
    assert inv.labels_of(unit) is ORPHAN and unit not in inv.orphaned(0)


def test_an_unlabelled_revision_labels_nothing_and_a_missing_body_is_orphaned() -> None:
    # The middle revision has no page list: it draws nothing; the body of 4
    # it alone holds is orphaned for the verdict.
    data = page(streams={4: LEAF_}, updates=(
        {3: PAGE_RES.replace(b"5 0 R", b"4 0 R") % (b"", b""),
         2: PAGES.replace(b"1 >>", b"2 >>")},
        {2: PAGES, 3: PAGE_RES % (b"", b""), 4: b"(gone)"}))
    inv = _inventory(data)
    assert {f.reason for f in inv.flags} == {FlagReason.PAGE_TREE}
    assert inv.labels[1] is None and inv.orphaned(1) == frozenset(
        u for u in inv.bodies_by_number(1).values() if u.start in inv.stream_data)
    (unit,) = [u for u in inv.body_labels if u.obj == 4]
    assert inv.labels_of(unit).orphaned
    assert inv.labels_of(UnitRef(UnitKind.OBJECT, 1)) is ORPHAN  # no such stream


def test_body_labels_union_every_kind_of_record() -> None:
    # An XMP stream, an object stream and a cross-reference stream: each
    # body keeps its kinds; a shared record is read once.
    from .test_inventory_labels import _streams_revisions
    inv = _inventory(_streams_revisions(rewrite_home=False))
    kinds = {u.obj: b.structural for u, b in inv.body_labels.items()}
    assert kinds[10] == (StructuralKind.OBJECT_STREAM,)
    assert kinds[11] == kinds[13] == (StructuralKind.XREF_STREAM,)
    assert build_body_labels((None,), [], Budget()) == {}
    assert build_body_labels((None,), [UnitRef(UnitKind.OBJECT, 0)], Budget(
        Limits(work_floor=0, work_per_byte=0))) == {}


# ── The reuse rule, condition by condition ────────────────────────────────
def _update(data: bytes, objects: dict[int, bytes], size: int, generation: int = 0) -> bytes:
    """*data* with an incremental update writing *objects* (at *generation*)."""
    w = Writer()
    w.out = bytearray(data)
    prev = int(data.rsplit(b"startxref", 1)[1].split()[0])
    table = {}
    for n, body in sorted(objects.items()):
        at = w.at()
        w.out += b"%d %d obj\n%s\nendobj\n" % (n, generation, body)
        table[n] = (1, at, generation)
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev)))
    return bytes(w.out)


def _state() -> bytes:
    """/AS /On, then /Off: only the annotation changes (a start)."""
    annot = b"<< %s /AS /%s /AP << /N << /On 6 0 R /Off 8 0 R >> >> >>"
    return make(objects={3: PAGE_RES % (b"", b" /Annots [4 0 R]"), 4: annot % (SQ, b"On")},
                streams={5: CONTENT, 6: _form(6), 8: _form(8)},
                updates=({4: annot % (SQ, b"Off")},))


def _subtype_rewritten() -> bytes:
    """XObject 4's /Subtype by reference (9): /Form, then /Image."""
    return page(b"/XObject << /X 4 0 R >>", objects={9: b"/Form"},
                streams={4: (b" /Subtype 9 0 R /BBox [0 0 1 1] /Width 1 /Height 1"
                             b" /BitsPerComponent 8 /ColorSpace /DeviceGray"
                             b" /Resources << /XObject << /Im 6 0 R >> >>", b"\x00"),
                         6: IMAGE_}, updates=({9: b"/Image"},))


def _font_retyped() -> bytes:
    """Font 9 rewritten from /TrueType to /Type3, its references the same:
    its glyph 6 is then drawn (the font decided its own role)."""
    font = (b"<< /Type /Font /Subtype /%s /FontBBox [0 0 1 1] /FontMatrix [1 0 0 1 0 0]"
            b" /CharProcs << /a 6 0 R >> /Resources << >> /Encoding << /Differences [0 /a] >>"
            b" /FirstChar 0 /LastChar 0 /Widths [1] >>")
    return page(b"/Font << /F1 9 0 R >>", objects={9: font % b"TrueType"}, streams={6: LEAF_},
                updates=({9: font % b"Type3"},))


def _category_rewritten() -> bytes:
    """The /XObject dictionary 8 (no stream) names image 4, then 6."""
    return page(b"/XObject 8 0 R", objects={8: b"<< /Im 4 0 R >>"},
                streams={4: IMAGE_, 6: IMAGE_}, updates=({8: b"<< /Im 6 0 R >>"},))


def _category_regenerated() -> bytes:
    """The /XObject dictionary 8 rewritten at generation 1, the same
    references: the page's 8 0 R no longer resolves (REF_GENERATION)."""
    data = page(b"/XObject 8 0 R", objects={8: b"<< /Im 4 0 R >>"}, streams={4: IMAGE_})
    return _update(data, {8: b"<< /Im 4 0 R >>"}, 9, generation=1)


def _form_rewritten(packed: bool) -> bytes:
    """Form 4 draws image 6, then 7: rewritten in place, or (*packed*) in
    its object stream 10, which only the home's entry names."""
    form = (b"<< /Length 5 /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /Im %d 0 R"
            b" >> >> >>\nstream\n0 0 m\nendstream")
    if not packed:
        return page(b"/XObject << /X 4 0 R >>", objects={4: form % 6},
                    streams={6: IMAGE_, 7: IMAGE_}, updates=({4: form % 7},))
    # A form must be a stream, so the packed object is the resources
    # dictionary the form names.
    w = Writer()
    at = {n: w.obj(n, body) for n, body in {
        1: CAT % b"", 2: PAGES, 3: PAGE_RES % (b"/XObject << /X 4 0 R >>", b"")}.items()}
    at |= {n: w.stream(n, *s) for n, s in {
        5: CONTENT, 6: IMAGE_, 7: IMAGE_,
        4: (b" /Subtype /Form /BBox [0 0 1 1] /Resources 8 0 R", b"0 0 m")}.items()}
    at10 = w.objstm(10, [(8, b"<< /XObject << /Im 6 0 R >> >>")])
    entries = {0: (0, 0, 65535)} | {n: (1, a, 0) for n, a in at.items()} | {
        10: (1, at10, 0), 8: (2, 10, 0)}
    prev = w.xref_stream(11, entries, b"/Size 12 /Root 1 0 R")
    w.epilogue(prev)
    at10 = w.objstm(10, [(8, b"<< /XObject << /Im 7 0 R >> >>")])
    w.epilogue(w.xref_stream(12, {10: (1, at10, 0)}, b"/Size 13 /Root 1 0 R /Prev %d" % prev))
    return bytes(w.out)


def _contents_swapped() -> bytes:
    """Both 4 and 6 reachable from the catalog; the page's /Contents moves
    from 4 to 6: the reachable set and records stay, DRAWABLE changes."""
    old = PAGE_RES.replace(b"5 0 R", b"4 0 R") % (b"", b"")
    return page(catalog=b" /All [4 0 R 6 0 R]", objects={3: old}, streams={4: LEAF_, 6: LEAF_},
                updates=({3: PAGE_RES.replace(b"5 0 R", b"6 0 R") % (b"", b"")},))


def _resources_renamed() -> bytes:
    """The page's /Resources dictionary 8 (by reference, so the page list
    is the same records) renames /XObject: the same target, another path."""
    return page(objects={3: PAGE_RES.replace(b"/Resources << %s >>", b"/Resources 8 0 R")
                         % b"", 8: b"<< /XObject << /Im 4 0 R >> >>"},
                streams={4: IMAGE_}, updates=({8: b"<< /Foo << /Im 4 0 R >> >>"},))


def _direct_font_retyped() -> bytes:
    """A font written directly in resource dictionary 8 turns Type3, its
    references the same: 8 decided a role, in a direct value."""
    font = (b"<< /Font << /F1 << /Type /Font /Subtype /%s /FontBBox [0 0 1 1]"
            b" /FontMatrix [1 0 0 1 0 0] /CharProcs << /a 6 0 R >> /Resources << >>"
            b" /Encoding << /Differences [0 /a] >> /FirstChar 0 /LastChar 0 /Widths [1] >> >> >>")
    return page(objects={3: PAGE_RES.replace(b"/Resources << %s >>", b"/Resources 8 0 R")
                         % b"", 8: font % b"TrueType"},
                streams={6: LEAF_}, updates=({8: font % b"Type3"},))


def _default_resources_rewritten() -> bytes:
    """A synthesized widget; the catalog's /AcroForm /DR changes its font
    (the catalog is not read: its /AcroForm as written is compared)."""
    data = _annotated(b"<< %s /AP << /N 6 0 R >> >>" % TX,
                      objects={11: TT % 12, 12: DESC % 13, 14: TT % 15, 15: DESC % 16},
                      streams={6: AP_FORM, 7: IMAGE_, 13: LEAF_, 16: LEAF_}, acroform=NEED)
    return _update(data, {1: CAT % NEED.replace(b"11 0 R", b"14 0 R")}, 17)


# (name, file, each revision's DRAWABLE numbers, newest first, its flags).
# Each fails when the reuse condition it names is dropped (each confirmed
# against its mutant, the plan's 3a-7e-2 entry): the starts (an
# annotation's /AS changed alone); the catalog's /AcroForm as written; a
# changed body read that is a stream, holds other references, has
# another generation or decided a role (in itself, or as a reference's
# target); a read object stream home replaced; and the labels' sharing
# requiring the same set. The trailer's /Root and whether an annotation
# is synthesized are implied by the starts (the catalog is one exactly
# then): equivalent mutants, kept as guards.
REUSE: list[tuple[str, bytes, list[set[int]], set[FlagReason]]] = [
    ("starts", _state(), [{5, 8}, {5, 6}], set()),
    ("acroform", _default_resources_rewritten(), [{5, 16}, {5, 13}], set()),
    ("stream-read", _form_rewritten(packed=False), [{4, 5, 7}, {4, 5, 6}], set()),
    ("references", _category_rewritten(), [{5, 6}, {4, 5}], set()),
    ("generation", _category_regenerated(), [{5}, {4, 5}], {FlagReason.REF_GENERATION}),
    ("decided-in-itself", _font_retyped(), [{5, 6}, {5}], set()),
    ("decided-by-a-target", _subtype_rewritten(), [{4, 5}, {4, 5, 6}], set()),
    ("home", _form_rewritten(packed=True), [{4, 5, 7}, {4, 5, 6}], set()),
    ("labels-shared", _contents_swapped(), [{6}, {4}], set()),
    ("key-paths", _resources_renamed(), [{5}, {4, 5}], set()),
    ("decided-in-a-direct-value", _direct_font_retyped(), [{5, 6}, {5}], set()),
]


@pytest.mark.parametrize(("data", "expected", "flags"), [(d, e, f) for _, d, e, f in REUSE],
                         ids=[n for n, _, _, _ in REUSE])
def test_each_reuse_condition_walks_the_drawing_again(data: bytes, expected: list[set[int]],
                                                      flags: set[FlagReason]) -> None:
    inv = _inventory(data)
    assert {f.reason for f in inv.flags} == flags, inv.flags
    assert [_drawn(inv, r) for r in range(inv.revisions)] == expected


def test_a_revision_without_a_page_list_resets_the_reuse() -> None:
    # The /XObject dictionary 8 is rewritten in a revision with no page list
    # (its page tree flagged); the next only repairs the tree. Nothing
    # records what the skipped revision changed, so the next walks afresh
    # (the review of #140's rule; the review of #158's gap.py).
    from .test_inventory_labels import PAGES as TREE
    data = page(b"/XObject 8 0 R", objects={8: b"<< /Im 4 0 R >>"},
                streams={4: IMAGE_, 6: IMAGE_},
                updates=({2: TREE.replace(b"1 >>", b"2 >>"), 8: b"<< /Im 6 0 R >>"}, {2: TREE}))
    inv = _inventory(data)
    assert {f.reason for f in inv.flags} == {FlagReason.PAGE_TREE}
    assert inv.labels[1] is None
    assert _drawn(inv, 0) == {5, 6} and _drawn(inv, 2) == {4, 5}


def test_a_stream_rewritten_alone_is_the_new_body_drawn() -> None:
    # The content stream rewritten: the same number, a new body -- labelled,
    # the old one drawable for the older revision only.
    data = _update(page(), {5: b"<< /Length 5 >>\nstream\n1 1 m\nendstream"}, 6)
    inv = _inventory(data)
    assert inv.flags == ()
    new, old = inv.body(5, 0), inv.body(5, 1)
    assert new != old and _labels(inv, 0).drawable == {new} and _labels(inv, 1).drawable == {old}


def test_a_revision_that_reads_nothing_new_shares_its_drawable_set() -> None:
    # Each update rewrites the catalog (not read: compared by its
    # /AcroForm), an object nothing draws, and the page and its /XObject
    # dictionary with the same references: the same set, by identity.
    data = page(b"/XObject 8 0 R", objects={8: b"<< /Im 4 0 R >>"}, streams={4: IMAGE_},
                updates=tuple({1: CAT % b" /PageMode /UseNone", 9: b"(%d)" % i,
                               3: PAGE_RES % (b"/XObject 8 0 R", b" /Rotate 0"),
                               8: b"<< /Im 4 0 R /Pad %d >>" % i} for i in range(3)))
    inv = _inventory(data)
    assert inv.flags == ()
    assert all(_labels(inv, r).drawable is _labels(inv, 0).drawable
               for r in range(inv.revisions))
    # A synthesized annotation: the catalog's /AcroForm as written decides.
    data = _annotated(b"<< %s /AP << /N 6 0 R >> >>" % TX, objects={11: TT % 12, 12: DESC % 13},
                      streams={6: AP_FORM, 7: IMAGE_, 13: LEAF_}, acroform=NEED)
    inv = _inventory(_update(data, {1: CAT % (NEED + b" /PageMode /UseNone")}, 14))
    assert inv.flags == () and _drawn(inv, 0) == {5, 13}
    assert _labels(inv, 0).drawable is _labels(inv, 1).drawable


def _page_rewrites(n: int) -> bytes:
    """A page drawing n images, and n updates each rewriting the page with
    the same references: the drawing is not walked again."""
    names = b" ".join(b"/Im%d %d 0 R" % (i, 10 + i) for i in range(n))
    return page(b"/XObject 8 0 R", objects={8: b"<< %s >>" % names},
                streams={10 + i: IMAGE_ for i in range(n)},
                updates=tuple({3: PAGE_RES % (b"/XObject 8 0 R", b" /Rotate %d" % (90 * (i % 2)))}
                              for i in range(n)))


# ── Cost: linear, charged, bounded (#94) ──────────────────────────────────
@pytest.fixture
def walk_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The walks' own charges only (the retained-memory charges are tested
    on their own, below)."""
    from redaction_verifier.inventory import labels
    for name in ("RECORD_WORK", "SET_WORK", "BY_KEY_WORK", "LABELS_WORK", "LIST_WORK",
                 "BODY_LABELS_WORK"):
        monkeypatch.setattr(labels, name, 0)


def _fan(n: int) -> bytes:
    """One page drawing n images, each with a soft mask and an ICC space."""
    names = b" ".join(b"/Im%d %d 0 R" % (i, 10 + 3 * i) for i in range(n))
    streams: dict[int, tuple[bytes, bytes]] = {}
    for i in range(n):
        streams[10 + 3 * i] = (IMG + b" /SMask %d 0 R /ColorSpace [/ICCBased %d 0 R]"
                               % (11 + 3 * i, 12 + 3 * i), b"\x00")
        streams[11 + 3 * i] = IMAGE_
        streams[12 + 3 * i] = ICC
    return page(b"/XObject << %s >>" % names, streams=streams)


def _deep(n: int) -> bytes:
    """A chain of n forms, each drawing the next."""
    streams = {10 + i: _form(10 + i, b"/XObject << /X %d 0 R >>" % (11 + i)) for i in range(n - 1)}
    streams[10 + n - 1] = _form(10 + n - 1)
    return page(b"/XObject << /X 10 0 R >>", streams=streams)


def _pages(n: int) -> bytes:
    """n pages sharing one resource dictionary (by reference) and font."""
    kids = b" ".join(b"%d 0 R" % (20 + i) for i in range(n))
    objects = {2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n),
               3: b"<< /Font << /F1 9 0 R >> /XObject << /Im 4 0 R >> >>",
               9: TT % 10, 10: DESC % 11}
    objects |= {20 + i: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] /Resources 3 0 R"
                        b" /Contents 5 0 R >>" for i in range(n)}
    return make(objects=objects, streams={4: IMAGE_, 5: CONTENT, 11: LEAF_})


def _glyphs(n: int) -> bytes:
    """A Type3 font of n glyphs, and n colour spaces sharing a profile."""
    procs = b" ".join(b"/g%d %d 0 R" % (i, 10 + i) for i in range(n))
    spaces = b" ".join(b"/C%d [/ICCBased 6 0 R]" % i for i in range(n))
    return page(b"/Font << /F1 9 0 R >> /ColorSpace << %s >>" % spaces, objects={
        9: b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1 1] /FontMatrix [1 0 0 1 0 0]"
           b" /CharProcs << %s >> /Resources << >> >>" % procs},
        streams={6: ICC} | {10 + i: LEAF_ for i in range(n)})


def _redrawn(n: int) -> bytes:
    """n images on the page, and n updates each rewriting the page to
    draw one more: every revision walks again (revisions x objects, by
    design; charged and bounded)."""
    names = b" ".join(b"/Im%d %d 0 R" % (i, 10 + i) for i in range(n))
    updates = tuple({3: PAGE_RES % (b"/XObject << %s /New %d 0 R >>" % (names, 10 + n + i), b""),
                     10 + n + i: b"<< /Length 1%s >>\nstream\n\x00\nendstream" % GRAY}
                    for i in range(n))
    return page(b"/XObject << %s >>" % names, streams={10 + i: IMAGE_ for i in range(n)},
                updates=updates)


def _shared(n: int) -> bytes:
    """A page drawing n images, and n updates rewriting nothing it reads."""
    names = b" ".join(b"/Im%d %d 0 R" % (i, 10 + i) for i in range(n))
    return page(b"/XObject << %s >>" % names, streams={10 + i: IMAGE_ for i in range(n)},
                updates=tuple({9: b"(%d)" % i} for i in range(n)))


def _direct_fonts(n: int) -> bytes:
    """One /Font dictionary of n fonts written directly, each with its own
    descriptor: each font's role is decided in place (read once each)."""
    fonts = b" ".join(b"/F%d << /Subtype /TrueType /FontDescriptor << /FontFile2 %d 0 R >> >>"
                      % (i, 10 + i) for i in range(n))
    return page(b"/Font << %s >>" % fonts, streams={10 + i: LEAF_ for i in range(n)})


def _catalog_grows(n: int) -> bytes:
    """A page drawing n images, a synthesized widget (the catalog's /DR
    joins), and n updates each rewriting the catalog with a new reference
    elsewhere: its /AcroForm as written is the same, so no walk again."""
    names = b" ".join(b"/Im%d %d 0 R" % (i, 20 + i) for i in range(n))
    data = page(b"/XObject << %s >>" % names, extra=b" /Annots [4 0 R]",
                objects={4: b"<< %s /AP << /N 6 0 R >> >>" % TX, 11: TT % 12, 12: DESC % 13},
                streams={6: AP_FORM, 7: IMAGE_, 13: LEAF_} | {20 + i: IMAGE_ for i in range(n)},
                catalog=NEED)
    for i in range(n):
        data = _update(data, {1: CAT % (NEED + b" /Last %d 0 R" % (7 if i % 2 else 13))},
                       20 + n)
    return data


def _wide_rewrites(n: int) -> bytes:
    """An /XObject dictionary of n images, rewritten n times with the same
    references: each rewrite's references compared, every one charged."""
    names = b" ".join(b"/Im%d %d 0 R" % (i, 10 + i) for i in range(n))
    return page(b"/XObject 8 0 R", objects={8: b"<< %s >>" % names},
                streams={10 + i: IMAGE_ for i in range(n)},
                updates=tuple({8: b"<< %s /Pad %d >>" % (names, i)} for i in range(n)))


def _big_dictionary(n: int) -> bytes:
    """One image XObject whose dictionary holds n direct entries: its
    /Subtype decision indexes them all (the review of #158's bigdict.py)."""
    pad = b" ".join(b"/K%d 0" % i for i in range(n))
    return page(b"/XObject << /Im 4 0 R >>", streams={4: (GRAY + b" " + pad, b"\x00")})


SHAPES = [_fan, _deep, _pages, _glyphs, _shared, _page_rewrites, _direct_fonts, _big_dictionary]


@pytest.mark.parametrize("make_file", SHAPES, ids=["fan", "deep", "pages", "glyphs", "shared",
                                                   "page-rewrites", "direct-fonts",
                                                   "big-dictionary"])
def test_the_drawing_walk_is_linear(make_file: object, walk_only: None) -> None:
    work = []
    for n in (60, 480):
        charged, lines, inv = charged_steps(make_file(n), "build_labels")  # type: ignore[operator]
        assert inv.flags == (), inv.flags
        work.append((charged, lines))
    _per_unit(work)
    assert work[1][0] <= 10 * work[0][0], work


def test_a_catalog_rewritten_elsewhere_is_not_drawn_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # With a synthesized widget the catalog's /AcroForm /DR is a start; the
    # catalog is compared by its /AcroForm as written, not read, so updates
    # rewriting it elsewhere (most do) never walk the drawing again. (The
    # reachable walk does: its references changed.)
    from redaction_verifier.inventory import labels
    walks = []
    real = labels.Drawing.run

    def run(self: object, starts: list[object]) -> object:
        walks.append(len(starts))
        return real(self, starts)  # type: ignore[arg-type]
    monkeypatch.setattr(labels.Drawing, "run", run)
    inv = _inventory(_catalog_grows(6))
    assert inv.flags == () and len(walks) == 1
    assert all(_labels(inv, r).drawable is _labels(inv, 0).drawable for r in range(7))
    assert {13, 20} <= _drawn(inv)


def test_comparing_rewritten_bodies_is_charged(walk_only: None) -> None:
    # Each update rewrites a wide /XObject dictionary with the same
    # references: compared with the old body's, every one charged (the
    # file grows with them: work linear in the file, not in n).
    work = []
    for n in (40, 320):
        charged, lines, inv = charged_steps(_wide_rewrites(n), "build_labels")
        assert inv.flags == () and all(_labels(inv, r).drawable is _labels(inv, 0).drawable
                                       for r in range(inv.revisions))
        work.append((charged, lines))
    _per_unit(work)


@requires_qpdf
def test_the_hint_stream_body_is_structural_not_orphaned(tmp_path: Path) -> None:
    import subprocess
    (tmp_path / "in.pdf").write_bytes(page())
    subprocess.run(["qpdf", "--linearize", "--object-streams=disable", str(tmp_path / "in.pdf"),
                    str(tmp_path / "lin.pdf")], check=True, timeout=60)
    inv = _inventory((tmp_path / "lin.pdf").read_bytes())
    assert inv.flags == ()
    hint = _labels(inv).hint
    assert hint is not None
    assert inv.labels_of(hint) == BodyLabels(False, (StructuralKind.HINT_STREAM,))
    assert hint not in inv.orphaned(0)


def test_shared_revisions_walk_the_drawing_once() -> None:
    inv = _inventory(_shared(40))
    assert all(found is inv.labels[0] for found in inv.labels)


def _pages_shared(n: int) -> bytes:
    """n pages, and n updates rewriting nothing they read: each revision
    still lists its starts (the page list, revisions x pages, charged)."""
    data = _pages(n)
    for i in range(n):
        data = _update(data, {6: b"(%d)" % i}, 20 + n)
    return data


@pytest.mark.parametrize("make_file", [_redrawn, _pages_shared], ids=["redrawn", "pages-shared"])
def test_revisions_that_draw_again_are_charged_and_bounded(make_file: object,
                                                           walk_only: None) -> None:
    # Every revision walks again, or lists its starts again: revisions x
    # objects, by design (ADR 0011 item 7) -- all of it charged, the work
    # growing as the product, and the budget bounding it.
    work = []
    for n in (40, 320):
        data = make_file(n)  # type: ignore[operator]
        charged, lines, inv = charged_steps(data, "build_labels")
        reasons = {f.reason for f in inv.flags}
        assert reasons <= {FlagReason.BUDGET_EXHAUSTED}, reasons
        work.append((charged, lines))
    _per_unit(work)
    (w_s, _), (w_l, _) = work
    assert w_l >= 16 * w_s or w_l >= 0.99 * Limits().work_limit(len(data)), work


@pytest.mark.parametrize("make_file", [_fan, lambda n: _redrawn(n // 8)], ids=["fan", "revisions"])
def test_body_labels_are_linear(make_file: object, walk_only: None) -> None:
    work = []
    for n in (80, 640):
        charged, lines, inv = charged_steps(make_file(n), "build_body_labels")  # type: ignore[operator]
        assert {f.reason for f in inv.flags} <= {FlagReason.BUDGET_EXHAUSTED}
        work.append((charged, lines))
    _per_unit(work)


def _per_unit(work: list[tuple[int, int]]) -> None:
    (w_s, l_s), (w_l, l_l) = work
    assert l_s <= LINES_PER_UNIT * w_s and l_l <= LINES_PER_UNIT * w_l, work
    assert l_l / w_l <= PER_UNIT_GROWTH * (l_s / w_s), work


@pytest.mark.parametrize("make_file", [lambda: _redrawn(40), lambda: _fan(300), lambda: _pages(300)],
                         ids=["redrawn", "fan", "pages"])
def test_drawable_sets_retain_at_most_a_byte_per_unit_of_work(make_file: object) -> None:
    # What the labels keep -- each new DRAWABLE set among them -- is charged
    # at least its size (DESIGN.md, "Bounded resources"); so is the per-body
    # dictionary (its BodyLabels values interned).
    data = make_file()  # type: ignore[operator]
    charged, _, inv = charged_steps(data, "build_labels")
    assert inv.flags == ()
    kept = _retained(inv.labels)
    assert kept <= charged, (kept, charged)
    charged, _, inv = charged_steps(data, "build_body_labels")
    body = sys.getsizeof(inv.body_labels) + sum(
        sys.getsizeof(b) for b in {id(b): b for b in inv.body_labels.values()}.values())
    assert body <= charged, (body, charged)
    assert kept + body <= Limits().work_per_byte * len(data)


# ── Spent budgets and measurement ─────────────────────────────────────────
def test_a_spent_budget_leaves_the_revision_unlabelled_and_its_bodies_orphaned() -> None:
    data = _deep(50)
    whole = Budget(file_size=len(data))
    charged, _, _ = charged_steps(data, "build_body_labels", whole)
    limits = Limits(work_floor=whole.work - charged - 1, work_per_byte=0)
    inv = _inventory(data, limits)
    assert {f.reason for f in inv.flags} == {FlagReason.BUDGET_EXHAUSTED}
    assert inv.labels[0] is None and inv.body_labels == {}
    assert all(inv.labels_of(u).orphaned for u in inv.orphaned(0))


def test_measurement_counts_labels_orphan_classes_and_the_closure_gap() -> None:
    data = page(b"/XObject << /Im 4 0 R >> /ColorSpace << /C [/ICCBased 6 0 R] >>",
                extra=b" /Thumb 7 0 R /PieceInfo << /X << /Private 8 0 R >> >> /Annots [12 0 R]",
                objects={9: b"<< /Foo 10 0 R >>",
                         12: b"<< %s /Metadata 13 0 R /AP << /N 14 0 R >> >>" % SQ},
                streams={4: IMAGE_, 6: (b" /N 1 /Metadata 11 0 R", b"icc"), 7: IMAGE_,
                         8: LEAF_, 10: LEAF_, 11: (b"", XMP), 13: (b"", XMP), 14: _form(14),
                         15: LEAF_})
    inv = _inventory(data)
    assert inv.flags == ()
    found = drawing_measures(data, inv)
    assert found["drawable_current"] == 4  # 4, 5, 6, 14
    assert found["orphaned_current"] == found["bodies_orphaned"] == 6  # 7, 8, 10, 11, 13, 15
    assert found["orphan_class_thumbnail"] == found["orphan_class_piece_info"] == 1
    assert found["orphan_class_unreferenced"] == 1  # 15
    assert found["orphan_class_unreachable_source"] == 1  # 10, held by 9
    assert found["orphan_metadata_annotation"] == found["orphan_metadata_icc"] == 1
    assert found["mupdf_listed"] == 3 and found["closure_gap"] == 0  # 4, 5, 14
