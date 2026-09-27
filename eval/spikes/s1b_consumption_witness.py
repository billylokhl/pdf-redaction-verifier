#!/usr/bin/env python3
"""Spike S1b: the content-stream consumption witness (docs/REDESIGN.md
§4, "Consumption witness"; docs/adr/0008-consumption-witness-granularity.md
is the decision this measures).

The claim being tested: MuPDF's content-stream interpreter silently
skips or reshapes some input rather than raising, so a decoder that
trusts `get_texttrace()` alone can miss text a redactor left behind, or
misjudge how much was drawn. The proposed defence is a witness: our own
content tokenizer counts the character *codes* passed to every
text-show operator (dividing each shown string's length by its font's
code length -- 1 byte for a simple font, 2 for an Identity-H/V Type0
font) and this must equal the number of glyphs MuPDF reports in the
page's texttrace, after excluding a texttrace char entry that is a
ToUnicode continuation (glyph id -1, no glyph of its own) and -- only on
a page that sets text render mode 2 or 6 (fill+stroke) -- de-duplicating
a (glyph, origin) pair that appears twice (MuPDF reports the fill and the
stroke of one glyph as two separate spans). De-duplication is confined to
such pages because on any other page a repeated (glyph, origin) pair is a
real second draw (a run of zero-width glyphs at one position, the same
text painted twice at one spot) that the code count also counts.

**This is the third correction to this script** (the second is below;
the third, in this version: (d) the fill+stroke de-duplication above was
applied to every page, which removed real repeated draws and caused 8 of
the 9 files the previous version reported as mismatching -- it is now
confined to pages that set `2 Tr` or `6 Tr`; (e) the Form XObject skip
was a regex over the page's content that missed `0 TL/Fm0 Do` (no
whitespace before the name) and skipped any page drawing *any* XObject,
images included -- a `Do` operand is now resolved against the page's
resources, and only a draw of a real `/Subtype /Form` XObject skips the
page, so image-only pages are now measured.)

**The second correction:** the first version had an operand-stack bug (numeric operands were dropped, so `Tf` was never
recognised -- see git history / RESULTS.md's first Correction note). The
version after that added a per-font "does this code have a real glyph"
exclusion via `doc.get_char_widths`, which looked clean (92.7%
reconciliation) but was itself wrong: `get_char_widths` resolves a code
through the font's own cmap as if it were a Unicode code point, ignoring
`/Encoding`, `/Differences`, and `/CIDToGIDMap` entirely, and returns an
empty table for a font it cannot load -- for 690 real pages in this
corpus, that fabricated an "no font has any glyph" result that excluded
every code shown, producing a false match (0 expected == 0 counted) that
hid the real question. The premise behind the exclusion was also false:
`get_texttrace()` DOES emit a char entry (unicode U+FFFD, glyph id 0)
for a code with no glyph -- `(A\x01\x02B\x7f\x81)` shown in Helvetica
traces 6 chars, not 2. **The exclusion is removed entirely in this
version.** What actually explained the residual mismatch (root-caused by
re-running this script's own output against real content) was: (a) this
script's own literal-string decoder kept backslash-end-of-line
continuations and did not normalise a raw CRLF inside a literal to a
single LF, both of which `verify._decode_pdf_string` also does -- fixed
locally in this script (not in `verify.py`) with `_decode_literal_fixed`;
(b) annotation *and form-widget* appearance text is included in a
whole-page `get_texttrace()` call (MuPDF's page interpreter runs both --
and PyMuPDF's `page.annots()` does not enumerate widgets, so deleting
only `annots()` looked sufficient until a filled-in form field's value
showed up as an unexplained mismatch; `page.widgets()` must be deleted
too) -- both are excluded here by deleting them from the page before
tracing; (c) a few mixed-width CJK CMaps (e.g. `90msp-RKSJ-H`) are not
1- or 2-byte fixed-width and are not modelled -- pages using one are
skipped, not silently miscounted.

This script does two things:

1. Runs eleven hand-built content streams (attached to a scratch page the
   way REDESIGN §4 describes: an undrawn stream given the page's own
   resolved resources) and reports, for each, whether MuPDF warned,
   whether the witness caught a mismatch, and whether the mismatch would
   have gone unnoticed by a warnings-only check. `3 Tr` (ordinary
   invisible text, ordinary OCR-layer usage) and `2 Tr` (fill+stroke,
   now de-duplicated) both reconcile -- included as the comparison the
   `7 Tr` and other no-warning cases need, and so does the same text painted
   twice at one spot under `0 Tr` (the control for correction (d): it
   must count twice on both sides). Three cases produce a witness
   mismatch with **no MuPDF warning at all**: clip-only text (`7 Tr`),
   an inline image whose declared-length pixel data literally spells a
   second, phantom text-show operation that MuPDF correctly treats as
   opaque bytes but this script's own naive inline-image skip does not
   (a false flag caused by this script's tokenizer over-counting, not a
   leak), and text inside a switched-off optional-content group
   (invisible to both `get_texttrace()` and `get_text()`, but present in
   the raw stream). The optional-content case is REDESIGN §8's K6 shape.
2. Sweeps every page of the real corpus (see corpus.py) and reports the
   witness's reconciliation rate, on the denominator that actually
   carries signal (pages that show *some* text -- most corpus pages show
   none at all and trivially "match" 0 == 0), and per file.

It extends verify._scan_content's tokenizer with font-aware code
counting rather than modifying verify.py -- see inventory_lite.py's
module docstring for why this stays a spike.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz  # noqa: E402
import verify  # noqa: E402

# ── A corrected literal-string decoder (spike-local; verify.py untouched) ──
# verify._unescape_pdf_literal keeps a backslash-end-of-line continuation as
# a literal newline character (it should contribute nothing -- PDF spec
# 7.3.4.2) and never normalises an unescaped raw CR or CRLF inside a
# literal to a single LF (it should -- the same clause). Both bugs make
# this script's OWN code count disagree with what MuPDF actually shows for
# a string written with either convention, which is common: several real
# corpus files' mismatches (verified against this script's own output)
# were exactly this off-by-a-handful-of-CRs shape. Fixed locally here.

_OCTAL = frozenset("01234567")
_LITERAL_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}


def _decode_literal_fixed(body: str) -> str:
    """The `(...)`  body (parens stripped) of a PDF literal string,
    decoded per spec: backslash-EOL is a line continuation (contributes
    nothing); an unescaped raw CR or CRLF is normalised to a single LF;
    octal and named escapes as usual."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if nxt == "\r":
                i += 3 if (i + 2 < n and body[i + 2] == "\n") else 2
                continue
            if nxt == "\n":
                i += 2
                continue
            if nxt in _OCTAL:
                j = i + 1
                end = min(j + 3, n)
                k = j
                while k < end and body[k] in _OCTAL:
                    k += 1
                out.append(chr(int(body[j:k], 8) & 0xFF))
                i = k
                continue
            out.append(_LITERAL_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if c == "\r":
            out.append("\n")
            i += 2 if (i + 1 < n and body[i + 1] == "\n") else 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _decode_pdf_string_fixed(token: str) -> str:
    """Same contract as verify._decode_pdf_string for the two token
    shapes this script's tokenizer produces, with the literal-string fix
    above. Hex strings have no line-ending ambiguity, so that branch is
    unchanged from verify's own (re-implemented, not imported, to keep
    this script's decoding path self-contained and reviewable in one
    place)."""
    if token.startswith("("):
        decoded = _decode_literal_fixed(token[1:-1])
        if decoded.startswith("\xfe\xff"):
            decoded = (
                decoded[2:].encode("latin-1", errors="replace")
                .decode("utf-16-be", errors="replace")
            )
        return decoded
    hex_chars = re.sub(r"\s+", "", token[1:-1])
    if len(hex_chars) % 2:
        hex_chars += "0"
    try:
        data = bytes.fromhex(hex_chars)
    except ValueError:
        return ""
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", errors="replace")
    return data.decode("latin-1")


# ── A font-tracking extension of verify._scan_content's tokenizer ──────
# Reuses verify's compiled token regexes (imported, not copied) but (a)
# keeps numeric operands on the stack, so a `Tf` preceded by [name, size]
# is recognised, and (b) decodes shown strings with the fix above instead
# of verify._decode_pdf_string.


class _Scan:
    """What one pass of the tokenizer below found in a content stream."""

    def __init__(self) -> None:
        self.total = 0                   # code units shown
        self.shown: list[str] = []       # the shown strings
        self.do_names: set[str] = set()  # every `/Name Do` operand
        self.fill_stroke = False         # a `2 Tr` or `6 Tr` was set


def _code_counts(buf: str, code_length: dict[str, int]) -> tuple[int, list[str]]:
    """(total code-unit count, shown strings) for every text-show
    operation in *buf*. Each shown string's codes are counted using the
    code length of the font active at that point (default 1 before any
    `Tf` or for a font not on the page)."""
    scan = _scan_codes(buf, code_length)
    return scan.total, scan.shown


def _scan_codes(buf: str, code_length: dict[str, int]) -> _Scan:
    """_code_counts's pass, also recording the names drawn with `Do` and
    whether a fill+stroke render mode (2 or 6) is ever set."""
    scan = _Scan()
    operands: list[tuple[str, Any]] = []
    array: list[str] | None = None
    depth = 0
    active_font: str | None = None
    inline: list[str] | None = None
    i, n = 0, len(buf)
    while i < n:
        if buf[i] == "(":
            end = verify._literal_end(buf, i)
            token = buf[i:end] if end < n or buf[end - 1] == ")" else buf[i:end] + ")"
            text = _decode_pdf_string_fixed(token)
            if array is not None:
                array.append(text)
            else:
                operands.append(("str", text))
            i = end
            continue
        m = verify._CONTENT_TOKEN_RE.match(buf, i)
        if m is None:
            i += 1
            continue
        token, i = m.group(), m.end()
        first = token[0]
        if first in verify._WS or first == "%":
            continue
        if inline is not None:
            if token == "ID":
                end = verify._INLINE_IMAGE_END_RE.search(buf, i + 1)
                i = end.end() if end else n
                inline = None
            else:
                inline.append(token)
            continue
        if token == "[":
            depth += 1
            if array is None:
                array = []
            continue
        if token == "]":
            depth -= 1
            if depth <= 0:
                operands.append(("array", array or []))
                array, depth = None, 0
            continue
        if first == "<" and token not in ("<", "<<"):
            text = _decode_pdf_string_fixed(token)
            (array.append(text) if array is not None else operands.append(("str", text)))
            continue
        if token in ("<<", ">>", "{", "}", ")", "<", ">") or first == "/" or \
                verify._NUMBER_RE.match(token):
            if array is None:
                if first == "/":
                    operands.append(("name", token))
                elif verify._NUMBER_RE.match(token):
                    operands.append(("num", token))  # kept: Tf needs [name, size]
            continue
        if array is not None:
            continue
        # An operator.
        if token == "Tf" and len(operands) >= 2 and operands[-2][0] == "name":
            active_font = operands[-2][1]
        elif token in verify._TEXT_SHOW_OPS and operands:
            kind, value = operands[-1]
            if token == "TJ":
                pieces = value if kind == "array" else []
            else:
                pieces = [value] if kind == "str" else []
            width = code_length.get(active_font, 1) if active_font else 1
            for piece in pieces:
                scan.shown.append(piece)
                scan.total += (len(piece) // width) if width else len(piece)
        elif token == "Tr" and operands and operands[-1][0] == "num":
            if float(operands[-1][1]) in (2, 6):
                scan.fill_stroke = True
        elif token == "Do" and operands and operands[-1][0] == "name":
            scan.do_names.add(operands[-1][1])
        elif token == "BI":
            inline = []
        operands.clear()
    return scan


# Mixed-width CJK CMaps (Shift-JIS-, EUC- and Big5-based predefined CMaps)
# are not 1- or 2-byte fixed-width and are not modelled by this spike; a
# page using one is skipped rather than silently miscounted. Identity-H/V
# and the fixed-2-byte "Uni*-UCS2/UTF16-H/V" family are the only 2-byte
# encodings this script counts correctly.
_FIXED_2BYTE_RE = re.compile(r"^(Identity-[HV]|Uni\w*-(UCS2|UTF16)-[HV])$")


def _font_code_lengths(page: fitz.Page) -> dict[str, int] | None:
    """{'/Name': code_length}, or None if any font on the page uses an
    unmodelled (not fixed 1- or 2-byte) encoding -- the caller should
    skip the page in that case rather than guess."""
    lengths: dict[str, int] = {}
    for xref, _ext, kind, _basefont, name, encoding, *_rest in page.get_fonts(full=True):
        if encoding in ("", "WinAnsiEncoding", "MacRomanEncoding", "StandardEncoding",
                        "PDFDocEncoding"):
            lengths["/" + name] = 1
        elif _FIXED_2BYTE_RE.match(encoding or ""):
            lengths["/" + name] = 2
        elif kind == "Type0":
            return None  # a CID font with an unrecognised, possibly mixed-width CMap
        else:
            lengths["/" + name] = 1  # an unlisted simple-font encoding: still 1 byte/code
    return lengths


def _glyph_count(trace: list[dict[str, Any]], *, fill_stroke: bool = False) -> int:
    """Glyphs actually drawn, from get_texttrace()'s chars tuples:
    excludes a ToUnicode continuation entry (glyph id -1 -- a second or
    further Unicode character for one glyph, e.g. a ligature's expansion,
    not a second glyph). Only when *fill_stroke* (the page sets render
    mode 2 or 6) is a recurring (glyph, origin) pair de-duplicated: a
    fill-then-stroke render mode draws -- and MuPDF reports -- the same
    glyph twice, as TWO SEPARATE SPANS, one per paint operation (so the
    dedup set is shared across the whole page's trace, not reset per
    span). On any other page a recurring pair is a real second draw -- a
    run of zero-width glyphs at one position, or the same text painted
    twice at one spot -- which the code count counts too, so removing it
    would manufacture a mismatch."""
    total = 0
    seen: set[tuple[int, tuple[float, float]]] = set()
    for span in trace:
        for _unicode, glyph, origin, _bbox in span["chars"]:
            if glyph == -1:
                continue
            if fill_stroke:
                key = (glyph, origin)
                if key in seen:
                    continue
                seen.add(key)
            total += 1
    return total


class _SkipPage(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _form_names(page: fitz.Page) -> set[str]:
    """'/Name' for every Form XObject in the page's own resources (the
    ones its content stream can draw directly; `invoker` 0 -- a form
    nested inside another form is reached only through its parent)."""
    return {"/" + name for _xref, name, invoker, _bbox in page.get_xobjects()
            if invoker == 0}


def _image_names(page: fitz.Page) -> set[str]:
    return {"/" + entry[7] for entry in page.get_images()}


def witness(page: fitz.Page, *, unit_only: bool = False) -> tuple[int, int, bool, str]:
    """(code_count, glyph_count, matches, mupdf_warnings) for one page.

    *unit_only*, used by the corpus sweep's refined pass: skip a page
    whose content stream draws a Form XObject -- a `Do` whose operand
    resolves, in the page's resources, to a `/Subtype /Form` XObject (its
    glyphs are not in page.read_contents(), so a whole-page comparison is
    not the same-decoding-unit comparison REDESIGN §4 actually specifies
    -- such a page is entirely unmeasured by this script, not counted
    either way). A `Do` of an image XObject does not skip the page (an
    image shows no text codes and no texttrace glyphs); a `Do` whose name
    resolves to neither is skipped too ("unresolved_xobject"), rather
    than guessed. Also skips a page that uses an unmodelled mixed-width
    CMap. Annotation appearance text is always excluded (annotations are
    deleted from the in-memory page before tracing -- this script never
    writes the file).
    """
    buf = page.read_contents().decode("latin-1")
    code_length = _font_code_lengths(page)
    scan = _scan_codes(buf, code_length or {})
    if unit_only and scan.do_names:
        if scan.do_names & _form_names(page):
            raise _SkipPage("form_xobject")
        if scan.do_names - _image_names(page):
            raise _SkipPage("unresolved_xobject")
    if code_length is None:
        raise _SkipPage("mixed_width_cmap")
    code_count = scan.total
    # page.annots() does NOT include Widget-subtype annotations (form
    # fields) -- PyMuPDF surfaces those separately via page.widgets() --
    # and a widget's appearance stream (e.g. a filled-in text field) is
    # drawn, and shows up in get_texttrace(), exactly like any other
    # annotation. Both must be removed to isolate the page's own content
    # stream, or a form field's value looks like an unexplained mismatch.
    for annot in list(page.annots()):
        page.delete_annot(annot)
    for widget in list(page.widgets()):
        page.delete_widget(widget)
    fitz.TOOLS.mupdf_warnings()  # clear
    trace = page.get_texttrace()
    warnings = fitz.TOOLS.mupdf_warnings()
    glyph_count = _glyph_count(trace, fill_stroke=scan.fill_stroke)
    return code_count, glyph_count, code_count == glyph_count, warnings


# ── Part 1: hand-built content streams ──────────────────────────────────

def _scratch_page(content: bytes) -> fitz.Page:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "placeholder", fontname="helv", fontsize=12)
    cxref = page.get_contents()[0]
    doc.update_stream(cxref, content)
    return page


def _ocg_off_scratch_page(content_template: bytes) -> fitz.Page:
    """A scratch page with one optional-content group added and switched
    OFF, its xref exposed under /Properties /MC0 in the page's own
    resources -- *content_template* should reference "/MC0"."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "placeholder", fontname="helv", fontsize=12)
    ocg_xref = doc.add_ocg("HiddenLayer", on=False)
    res_ref = doc.xref_get_key(page.xref, "Resources")[1]
    res_num = int(res_ref.split()[0])
    doc.xref_set_key(res_num, "Properties", f"<< /MC0 {ocg_xref} 0 R >>")
    cxref = page.get_contents()[0]
    doc.update_stream(cxref, content_template)
    return page


ADVERSARIAL_CASES: list[tuple[str, bytes | None, Any]] = [
    ("unknown operator between two shows", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(FIRST TEXT) Tj
1 2 3 XYZBAD
(SECOND TEXT) Tj
ET
Q
""", _scratch_page),
    ("nested BT without matching ET", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(OUTER) Tj
BT
1 0 0 1 72 650 Tm
(INNER) Tj
ET
ET
Q
""", _scratch_page),
    ("stray extra Q before content", b"""
Q
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(STRAY Q TEST) Tj
ET
Q
""", _scratch_page),
    ("well-formed inline image (control)", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(BEFORE IMAGE) Tj
ET
Q
q
BI /W 2 /H 2 /BPC 8 /CS /G ID \xff\xff\xff\xff EI
Q
BT
1 0 0 1 72 650 Tm
/helv 12 Tf
(AFTER IMAGE) Tj
ET
""", _scratch_page),
    ("inline image whose declared size exceeds its data", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(BEFORE IMAGE) Tj
ET
Q
q
BI /W 100 /H 100 /BPC 8 /CS /G ID \xff\xff\xff\xff EI
Q
BT
1 0 0 1 72 650 Tm
/helv 12 Tf
(AFTER IMAGE) Tj
ET
""", _scratch_page),
    ("ordinary invisible text (3 Tr) -- comparison, should reconcile", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
3 Tr
(SAMPLE) Tj
ET
Q
""", _scratch_page),
    ("fill+stroke text (2 Tr) -- de-duplicated, should now reconcile", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
2 Tr
(SAMPLE) Tj
ET
Q
""", _scratch_page),
    ("same text painted twice at one spot (0 Tr) -- two real draws, must "
     "count twice on both sides", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(TWICE) Tj
1 0 0 1 72 700 Tm
(TWICE) Tj
ET
Q
""", _scratch_page),
    # The three cases below produce NO MuPDF warning at all -- a
    # warnings-only check (the other half of REDESIGN §4's rejection
    # rule) would not catch any of them. The phantom-Tj case is a false
    # flag from this script's own tokenizer over-counting, not a leak;
    # the 7 Tr and switched-off-layer cases each already exit 1 under
    # today's verify.py (its Objects layer searches the raw strings) --
    # they matter for the rewrite's DECODED discharge, see docs/adr/0008.
    ("clip-only text (7 Tr): drawn, zero texttrace glyphs, no warning", b"""
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
7 Tr
(HIDDEN SECRET) Tj
ET
Q
""", _scratch_page),
    ("inline image data spells a phantom Tj: MuPDF reads it as pixels, "
     "this script's naive EI-skip re-parses it as content, no warning",
     b"q\nBT\n1 0 0 1 72 700 Tm\n/helv 12 Tf\n(REAL) Tj\nET\nQ\n"
     b"q\nBI /W 24 /H 1 /BPC 8 /CS /G ID "
     b"\x01\x01\x01 EI (PHANTOM) Tj \x01\x01\x01\x01\x01\x01\x01\x01"
     b" EI\nQ\n"
     b"BT\n1 0 0 1 72 650 Tm\n/helv 12 Tf\n(AFTER) Tj\nET\n", _scratch_page),
    ("text in a switched-OFF optional-content group: absent from "
     "get_texttrace() AND get_text(), present in the raw stream (REDESIGN "
     "§8's K6), no warning",
     b"q\nBT\n1 0 0 1 72 700 Tm\n/helv 12 Tf\n(VISIBLE) Tj\nET\nQ\n"
     b"/OC /MC0 BDC\n"
     b"BT\n1 0 0 1 72 650 Tm\n/helv 12 Tf\n(HIDDENLAYER) Tj\nET\n"
     b"EMC\n", _ocg_off_scratch_page),
]


def run_adversarial() -> list[dict[str, Any]]:
    results = []
    for name, content, builder in ADVERSARIAL_CASES:
        page = builder(content)
        code_count, glyph_count, matches, warnings = witness(page)
        get_text = page.get_text().strip()
        results.append({
            "case": name,
            "code_count": code_count,
            "glyph_count": glyph_count,
            "witness_matches": matches,
            "mupdf_warned": bool(warnings.strip()),
            "warning_alone_would_have_caught_it": bool(warnings.strip()) or matches,
            "get_text": get_text,
        })
        page.parent.close()
    return results


# ── Part 2: reconciliation rate over the real corpus ────────────────────

def run_corpus_sweep(limit: int | None = None, *, unit_only: bool = False) -> dict[str, Any]:
    """unit_only=False reproduces a naive whole-page comparison (informative
    about why that comparison is the wrong shape, see RESULTS.md); True
    approximates REDESIGN §4's actual per-decoding-unit comparison by
    skipping a page that draws a Form XObject (resolved in the page's
    resources; an image `Do` does not skip), draws an XObject name that
    resolves to nothing, or uses an unmodelled mixed-width CMap.

    Reports on two denominators, both because most corpus pages show no
    text at all and would otherwise pad the reconciliation rate with a
    meaningless 0 == 0 "match": *text pages* (code_count>0 or
    glyph_count>0) among pages actually measured, and *in-scope files*
    (a text-bearing file that contributed >=1 text page after the two
    skips above). Skipped pages are unmeasured, not counted either way;
    their counts, and the files with at least one, are reported."""
    import corpus

    files = corpus.discover(limit=limit)
    pages_checked = 0
    text_pages = 0
    text_pages_matched = 0
    pages_with_warnings = 0
    mismatches_without_warning = 0
    skipped: dict[str, int] = {"form_xobject": 0, "unresolved_xobject": 0,
                               "mixed_width_cmap": 0}
    files_with_skipped: dict[str, int] = dict.fromkeys(skipped, 0)
    errors = 0
    in_scope_files = 0
    files_with_mismatch = 0
    t0 = time.time()
    for path in files:
        try:
            doc = fitz.open(str(path))
        except Exception:
            errors += 1
            continue
        if doc.needs_pass:
            doc.close()
            continue
        file_has_text_page = False
        file_has_mismatch = False
        file_skips: set[str] = set()
        for page in doc:
            try:
                code_count, glyph_count, matches, warnings = witness(page, unit_only=unit_only)
            except _SkipPage as skip:
                skipped[skip.reason] = skipped.get(skip.reason, 0) + 1
                file_skips.add(skip.reason)
                continue
            except Exception:
                errors += 1
                continue
            pages_checked += 1
            if code_count == 0 and glyph_count == 0:
                continue  # no text shown either way -- not a signal
            file_has_text_page = True
            text_pages += 1
            if matches:
                text_pages_matched += 1
            else:
                file_has_mismatch = True
            if warnings.strip():
                pages_with_warnings += 1
            if not matches and not warnings.strip():
                mismatches_without_warning += 1
        doc.close()
        for reason in file_skips:
            files_with_skipped[reason] += 1
        if file_has_text_page:
            in_scope_files += 1
            if file_has_mismatch:
                files_with_mismatch += 1
    return {
        "unit_only": unit_only,
        "files": len(files),
        "in_scope_files": in_scope_files,
        "files_with_any_mismatching_text_page": files_with_mismatch,
        "file_mismatch_rate": (
            files_with_mismatch / in_scope_files if in_scope_files else None
        ),
        "pages_checked": pages_checked,
        "text_pages": text_pages,
        "trivial_pages_both_zero": pages_checked - text_pages,
        "text_pages_matched": text_pages_matched,
        "reconciliation_rate": text_pages_matched / text_pages if text_pages else None,
        "pages_with_mupdf_warnings": pages_with_warnings,
        "mismatches_without_a_warning": mismatches_without_warning,
        "skipped_pages": skipped,
        "files_with_a_skipped_page": files_with_skipped,
        "errors": errors,
        "elapsed_s": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    print("=== Part 1: hand-built content streams ===")
    adversarial = run_adversarial()
    print(json.dumps(adversarial, indent=2))
    print()
    print("=== Part 2a: naive whole-page reconciliation sweep ===")
    print(json.dumps(run_corpus_sweep(), indent=2))
    print()
    print("=== Part 2b: refined per-decoding-unit reconciliation sweep ===")
    print(json.dumps(run_corpus_sweep(unit_only=True), indent=2))
