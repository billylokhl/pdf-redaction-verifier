#!/usr/bin/env python3
"""Spike S1b: the content-stream consumption witness (docs/REDESIGN.md
§4, "Consumption witness"; docs/adr/0006-recursion-and-decode-budget.md
and docs/adr/0003-not-applicable-reasons.md rely on this holding).

The claim being tested: MuPDF's content-stream interpreter silently
skips some malformed input (an unknown operator, a truncated inline
image, unbalanced BT/q) rather than raising, so a decoder that trusts
`get_texttrace()` alone can miss text a redactor left behind. The
proposed defence is a witness: our own content tokenizer counts the
character *codes* passed to every text-show operator (dividing each
shown string's length by its font's code length — 1 byte for a simple
font, 2 for an Identity-H/V Type0 font) and this must equal the number
of glyphs MuPDF reports in the page's texttrace. A mismatch means MuPDF
consumed the stream differently than our tokenizer's byte-for-byte
reading says it should have -- exactly the silent-skip failure mode.

This script does two things:

1. Runs six hand-built adversarial content streams (attached to a
   scratch page the way REDESIGN §4 describes: an undrawn stream given
   the page's own resolved resources) and reports, for each, whether
   MuPDF warned, whether the witness caught a mismatch, and whether the
   mismatch would have gone unnoticed by a warnings-only check.
2. Sweeps every page of the real corpus (see corpus.py) and reports the
   witness's reconciliation rate on ordinary, non-adversarial content --
   i.e. whether the witness is quiet enough on legitimate PDFs to be
   worth shipping.

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

# ── A font-tracking extension of verify._scan_content ──────────────────
# verify._scan_content already tokenizes a content stream and returns the
# shown strings in order (scan.shown), but does not track which font was
# active for each one -- it only needs to sniff "does this stream draw
# text", not compute a witness. This re-runs the same regex tokenizer
# (imported, not copied) while additionally watching Tf operands, which
# is exactly the extension Phase 4a's real decoder would make.


def _code_counts(buf: str, code_length: dict[str, int]) -> tuple[int, list[str]]:
    """(total code-unit count, shown strings) for every text-show
    operation in *buf*, dividing each shown string's length by the code
    length of the font active at that point (default 1 if unknown)."""
    operands: list[tuple[str, Any]] = []
    array: list[str] | None = None
    depth = 0
    active_font = "1"  # code length, not a name -- default simple-font width
    inline: list[str] | None = None
    total = 0
    shown: list[str] = []
    i, n = 0, len(buf)
    while i < n:
        if buf[i] == "(":
            end = verify._literal_end(buf, i)
            token = buf[i:end] if end < n or buf[end - 1] == ")" else buf[i:end] + ")"
            text = verify._decode_pdf_string(token) or ""
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
            text = verify._decode_pdf_string(token) or ""
            (array.append(text) if array is not None else operands.append(("str", text)))
            continue
        if token in ("<<", ">>", "{", "}", ")", "<", ">") or first == "/" or \
                verify._NUMBER_RE.match(token):
            if array is None and first == "/":
                operands.append(("name", token))
            continue
        if array is not None:
            continue
        if token == "Tf" and len(operands) >= 2 and operands[-2][0] == "name":
            active_font = str(code_length.get(operands[-2][1], 1))
        elif token in verify._TEXT_SHOW_OPS and operands:
            kind, value = operands[-1]
            width = code_length.get(active_font, 1) if active_font not in ("1", "2") \
                else int(active_font)
            if token == "TJ" and kind == "array":
                for piece in value:
                    total += len(piece) // width if width else len(piece)
                    shown.append(piece)
            elif token != "TJ" and kind == "str":
                total += len(value) // width if width else len(value)
                shown.append(value)
        elif token == "BI":
            inline = []
        operands.clear()
    return total, shown


def _font_code_lengths(page: fitz.Page) -> dict[str, int]:
    """{'/Name': code_length} from the page's declared fonts. Identity-H
    and Identity-V are the only 2-byte encodings modelled -- see
    eval/spikes/README.md's "Known limitations"."""
    lengths: dict[str, int] = {}
    for xref, _ext, _type, _basefont, name, encoding, *_rest in page.get_fonts(full=True):
        lengths["/" + name] = 2 if encoding in ("Identity-H", "Identity-V") else 1
    return lengths


# Codes below this map to no visible glyph in every simple-font encoding
# this tool sees (WinAnsi, MacRoman, StandardEncoding all leave C0 codes
# undefined); a witness must not expect a glyph for one. Space (0x20) DOES
# draw a glyph and is excluded from this set.
_NO_GLYPH_CODES = frozenset(range(0x20)) - {0x09}  # tab is also glyph-less


class _SkipPage(Exception):
    pass


def witness(page: fitz.Page, *, unit_only: bool = False) -> tuple[int, int, bool, str]:
    """(code_count, glyph_count, matches, mupdf_warnings) for one page.

    *unit_only*, used by the corpus sweep's refined pass: skip a page that
    draws through a Form XObject (its glyphs are not in
    page.read_contents(), so a whole-page comparison is not the
    same-decoding-unit comparison REDESIGN §4 actually specifies -- see
    RESULTS.md) and do not count a control code as an expected glyph.
    """
    buf = page.read_contents().decode("latin-1")
    if unit_only and re.search(r"(?:^|[\s\d])/[^\s/()<>\[\]{}%]+\s+Do(?:[\s(]|$)", buf):
        raise _SkipPage("draws through a Form XObject")
    code_length = _font_code_lengths(page)
    code_count, shown = _code_counts(buf, code_length)
    if unit_only:
        code_count -= sum(1 for s in shown for ch in s if ord(ch) in _NO_GLYPH_CODES)
    fitz.TOOLS.mupdf_warnings()  # clear
    trace = page.get_texttrace()
    warnings = fitz.TOOLS.mupdf_warnings()
    glyph_count = sum(len(span["chars"]) for span in trace)
    return code_count, glyph_count, code_count == glyph_count, warnings


# ── Part 1: hand-built adversarial content streams ──────────────────────

def _scratch_page(content: bytes) -> fitz.Page:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "placeholder", fontname="helv", fontsize=12)
    cxref = page.get_contents()[0]
    doc.update_stream(cxref, content)
    return page


ADVERSARIAL_CASES: list[tuple[str, bytes]] = [
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
"""),
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
"""),
    ("stray extra Q before content", b"""
Q
q
BT
1 0 0 1 72 700 Tm
/helv 12 Tf
(STRAY Q TEST) Tj
ET
Q
"""),
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
"""),
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
"""),
]


def run_adversarial() -> list[dict[str, Any]]:
    results = []
    for name, content in ADVERSARIAL_CASES:
        page = _scratch_page(content)
        code_count, glyph_count, matches, warnings = witness(page)
        results.append({
            "case": name,
            "code_count": code_count,
            "glyph_count": glyph_count,
            "witness_matches": matches,
            "mupdf_warned": bool(warnings.strip()),
            "warnings_would_have_caught_it": bool(warnings.strip()) or matches,
        })
        page.parent.close()
    return results


# ── Part 2: reconciliation rate over the real corpus ────────────────────

def run_corpus_sweep(limit: int | None = None, *, unit_only: bool = False) -> dict[str, Any]:
    """unit_only=False reproduces a naive whole-page comparison (informative
    about why that comparison is the wrong shape, see RESULTS.md); True
    approximates REDESIGN §4's actual per-decoding-unit comparison by
    skipping pages with Form XObjects and excluding glyph-less control
    codes from the expected count."""
    import corpus

    files = corpus.discover(limit=limit)
    pages_checked = 0
    pages_matched = 0
    pages_with_warnings = 0
    mismatches_without_warning = 0
    pages_skipped = 0
    errors = 0
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
        for page in doc:
            try:
                code_count, glyph_count, matches, warnings = witness(page, unit_only=unit_only)
            except _SkipPage:
                pages_skipped += 1
                continue
            except Exception:
                errors += 1
                continue
            pages_checked += 1
            if matches:
                pages_matched += 1
            if warnings.strip():
                pages_with_warnings += 1
            if not matches and not warnings.strip():
                mismatches_without_warning += 1
        doc.close()
    return {
        "unit_only": unit_only,
        "files": len(files),
        "pages_checked": pages_checked,
        "pages_skipped_form_xobject": pages_skipped,
        "pages_matched": pages_matched,
        "reconciliation_rate": pages_matched / pages_checked if pages_checked else None,
        "pages_with_mupdf_warnings": pages_with_warnings,
        "mismatches_without_a_warning": mismatches_without_warning,
        "errors": errors,
        "elapsed_s": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    print("=== Part 1: adversarial content streams ===")
    adversarial = run_adversarial()
    print(json.dumps(adversarial, indent=2))
    print()
    print("=== Part 2a: naive whole-page reconciliation sweep ===")
    print(json.dumps(run_corpus_sweep(), indent=2))
    print()
    print("=== Part 2b: refined per-decoding-unit reconciliation sweep ===")
    print(json.dumps(run_corpus_sweep(unit_only=True), indent=2))
