"""Oracle channel R1: decompress and search raw bytes, with no PDF library.

Standard library only (``re``, ``zlib``, ``base64``, ``binascii``) --
tests/test_redactor_set_oracle.py checks it -- so it shares no code
with the verifier, PyMuPDF or qpdf, and CI can re-run it on committed
files.

It finds every ``stream ... endstream`` body in the file's bytes, from
anywhere (an object the cross-reference table lists or not, after the
final ``%%EOF``), tries each decoder it knows, and searches the raw
bytes and every decoded body for a planted instance's forms:

- the written text as ASCII / PDFDocEncoding, as a PDF literal string
  (escaped) and hex string, and as UTF-16BE with and without a BOM;
- each fingerprint (the codes a font draws it with) inside the joined
  text operands of one ``BT ... ET`` block of a content stream, so a
  value drawn a glyph or a word at a time still matches.

Every hit says what kind of body held it (``KINDS``). A hit inside a
font program or image samples is reported but never counts as
recoverable by itself: those hold glyph outlines and pixels, not text,
and a byte coincidence there is not a leak (the generator's preflight
keeps planted documents free of them).

Partial residue (four or more consecutive characters of a value; owner
decision: a leak) is searched only where text lives -- decoded string
tokens and joined text operands -- never in operators' numbers or
binary bodies, where four digits occur by chance.
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from dataclasses import dataclass, field

KINDS = ("content", "string", "xmp", "embedded", "font", "image", "other", "raw")
NOT_RECOVERABLE_ALONE = frozenset({"font", "image"})

_OBJ_HEAD = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_STREAM = re.compile(rb"stream(?:\r\n|\n|\r)")
_ENDSTREAM = re.compile(rb"\r?\n?endstream")


@dataclass
class Body:
    """One stream body found in the raw bytes."""
    obj: int | None          # the object number before it, if one was found
    start: int               # byte offset of the body in the file
    dictionary: bytes        # the dictionary text before ``stream``
    raw: bytes
    decoded: bytes | None    # None when no known decoder applies
    kind: str = "other"
    # "" (presumably the object's current body), "superseded" (a later
    # body has the same object number: an incremental update replaced it)
    # or "unindexed" (after the file's final %%EOF).
    storage: str = ""


@dataclass
class Hit:
    form: str                # the string searched for
    kind: str                # KINDS member
    obj: int | None
    offset: int              # body start (or raw offset for kind "raw")
    how: str                 # "ascii", "literal", "hex", "utf16", "codes", "string",
                             # "partial:<piece>"
    storage: str = ""        # the body's (Body.storage); "unindexed" past %%EOF


@dataclass
class Report:
    bodies: int
    undecodable: int
    hits: list[Hit] = field(default_factory=list)


# ── Decoding ───────────────────────────────────────────────────────────

def _inflate(data: bytes) -> bytes | None:
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(data)
            if out:
                return out + d.flush()
        except zlib.error:
            continue
    return None


def _ascii_hex(data: bytes) -> bytes | None:
    text = re.sub(rb"\s", b"", data).split(b">")[0]
    if len(text) % 2:
        text += b"0"
    try:
        return binascii.unhexlify(text)
    except (binascii.Error, ValueError):
        return None


def _ascii85(data: bytes) -> bytes | None:
    text = re.sub(rb"\s", b"", data)
    if text.startswith(b"<~"):
        text = text[2:]
    text = text.split(b"~>")[0]
    try:
        return base64.a85decode(text)
    except ValueError:
        return None


def decode(dictionary: bytes, raw: bytes) -> bytes | None:
    """Apply the dictionary's /Filter chain where every filter is one this
    channel knows (Flate, ASCIIHex, ASCII85); unfiltered bodies are
    returned as is. Anything else (DCT, JBIG2, LZW, a predictor
    mismatch) gives None."""
    filters = re.findall(rb"/(FlateDecode|Fl|ASCIIHexDecode|AHx|ASCII85Decode|A85|[A-Za-z0-9]+Decode)",
                         _filter_entry(dictionary))
    data: bytes | None = raw
    for f in filters:
        if data is None:
            return None
        if f in (b"FlateDecode", b"Fl"):
            data = _inflate(data)
        elif f in (b"ASCIIHexDecode", b"AHx"):
            data = _ascii_hex(data)
        elif f in (b"ASCII85Decode", b"A85"):
            data = _ascii85(data)
        else:
            return None
    if data is not None and b"/Predictor" in dictionary:
        return None if _predictor(dictionary) > 1 else data
    return data


def _filter_entry(dictionary: bytes) -> bytes:
    m = re.search(rb"/Filter\s*(\[[^\]]*\]|/[A-Za-z0-9]+)", dictionary)
    return m.group(1) if m else b""


def _predictor(dictionary: bytes) -> int:
    m = re.search(rb"/Predictor\s+(\d+)", dictionary)
    return int(m.group(1)) if m else 1


def classify(dictionary: bytes, decoded: bytes | None) -> str:
    d = dictionary
    if re.search(rb"/Subtype\s*/Image\b", d):
        return "image"
    if re.search(rb"/Length[123]\b", d) or re.search(
            rb"/Subtype\s*/(Type1C|CIDFontType0C|OpenType)\b", d):
        return "font"
    if re.search(rb"/Type\s*/Metadata\b", d):
        return "xmp"
    if re.search(rb"/Type\s*/EmbeddedFile\b", d):
        return "embedded"
    if re.search(rb"/Type\s*/ObjStm\b", d):
        return "string"     # an object stream holds dictionaries and their strings
    if decoded is not None and re.search(rb"\b(BT|Tj|TJ|re|cm|Do)\b", decoded[:4096]):
        return "content"
    return "other"


def bodies(raw: bytes) -> list[Body]:
    """Every stream body in *raw*, in file order. A body's dictionary is
    read back to its own ``N G obj`` header, never past the previous
    object's ``endobj`` or ``endstream``."""
    found: list[Body] = []
    last_eof = raw.rfind(b"%%EOF")
    for m in _STREAM.finditer(raw):
        start = m.end()
        window_start = max(0, m.start() - 65536)
        window = raw[window_start:m.start()]
        cut = max(window.rfind(b"endobj"), window.rfind(b"endstream"))
        if cut >= 0:
            window_start += cut + 1
            window = raw[window_start:m.start()]
        # Only a `stream` keyword right after a dictionary's `>>` opens one.
        if not window.rstrip().endswith(b">>"):
            continue
        end_m = _ENDSTREAM.search(raw, start)
        if not end_m:
            continue
        head = None
        for h in _OBJ_HEAD.finditer(window):
            head = h
        obj = int(head.group(1)) if head is not None else None
        dictionary = window[head.end():] if head is not None else window
        body = raw[start:end_m.start()]
        decoded = decode(dictionary, body)
        storage = "unindexed" if last_eof >= 0 and start > last_eof else ""
        found.append(Body(obj, start, dictionary, body, decoded, classify(dictionary, decoded),
                          storage))
    # A later body with the same object number replaced the earlier one.
    latest: dict[int, int] = {}
    for i, b in enumerate(found):
        if b.obj is not None and b.storage != "unindexed":
            latest[b.obj] = i
    for i, b in enumerate(found):
        if b.obj is not None and b.storage == "" and latest.get(b.obj) != i:
            b.storage = "superseded"
    return found


# ── Strings and text operands ──────────────────────────────────────────

_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
        b"(": b"(", b")": b")", b"\\": b"\\"}


def _literal(data: bytes, i: int) -> tuple[bytes, int]:
    """Decode the literal string starting after '(' at *i*; return
    (bytes, index after the closing ')')."""
    out = bytearray()
    depth = 1
    n = len(data)
    while i < n:
        c = data[i:i + 1]
        if c == b"\\":
            nxt = data[i + 1:i + 2]
            if nxt in _ESC:
                out += _ESC[nxt]
                i += 2
            elif nxt in (b"\r", b"\n"):
                i += 2 if data[i + 1:i + 3] == b"\r\n" else 1
                i += 1 if nxt == b"\n" or data[i:i + 1] != b"\n" else 0
            elif nxt and nxt in b"01234567":
                m = re.match(rb"[0-7]{1,3}", data[i + 1:i + 4])
                digits = m.group(0) if m else nxt
                out.append(int(digits, 8) & 0xFF)
                i += 1 + len(digits)
            else:
                i += 1          # any other escaped character stands for itself
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out += c
        i += 1
    return bytes(out), n


_TOKEN = re.compile(rb"\(|<(?!<)([0-9A-Fa-f\s]*)>|\[|\]|[A-Za-z'\"]+|-?[\d.]+|/[^\s/<>\[\]()]*")


def string_tokens(data: bytes) -> list[bytes]:
    """Every literal and hex string token in *data*, decoded."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        m = _TOKEN.search(data, i)
        if not m:
            break
        if m.group(0) == b"(":
            s, i = _literal(data, m.end())
            out.append(s)
            continue
        if m.group(1) is not None:
            h = re.sub(rb"\s", b"", m.group(1))
            if len(h) % 2:
                h += b"0"
            try:
                out.append(binascii.unhexlify(h))
            except binascii.Error:
                pass
        i = m.end()
    return out


def text_blocks(content: bytes) -> list[bytes]:
    """The string operands of the text-showing operators (Tj, TJ, ', ")
    of each BT ... ET block, joined; TJ's kerning numbers dropped."""
    blocks: list[bytes] = []
    buf = bytearray()
    pending: list[bytes] = []
    in_array = False
    array: list[bytes] = []
    i, n = 0, len(content)
    while i < n:
        m = _TOKEN.search(content, i)
        if not m:
            break
        tok = m.group(0)
        if tok == b"(":
            s, i = _literal(content, m.end())
            (array if in_array else pending).append(s)
            continue
        i = m.end()
        if m.group(1) is not None:
            h = re.sub(rb"\s", b"", m.group(1))
            try:
                s = binascii.unhexlify(h + (b"0" if len(h) % 2 else b""))
            except binascii.Error:
                s = b""
            (array if in_array else pending).append(s)
        elif tok == b"[":
            in_array, array = True, []
        elif tok == b"]":
            in_array = False
            pending.append(b"".join(array))
        elif tok == b"BT":
            buf, pending = bytearray(), []
        elif tok in (b"Tj", b"TJ", b"'", b"\""):
            if pending:
                buf += pending[-1]
            pending = []
        elif tok == b"ET":
            blocks.append(bytes(buf))
            buf, pending = bytearray(), []
        elif tok[:1].isalpha():
            pending = []
    if buf:
        blocks.append(bytes(buf))
    return blocks


# ── Search ─────────────────────────────────────────────────────────────

def text_forms(form: str) -> list[tuple[str, bytes]]:
    """(how, bytes) for the ways a string object or raw text can hold
    *form*."""
    utf16 = form.encode("utf-16-be")
    out = [("ascii", form.encode("latin-1", "replace")),
           ("utf16", utf16),
           ("hex", form.encode("latin-1", "replace").hex().encode()),
           ("hex", form.encode("latin-1", "replace").hex().upper().encode()),
           # A two-byte font whose codes are Unicode, written as a hex
           # string (an OCR text layer, say).
           ("hex16", utf16.hex().encode()),
           ("hex16", utf16.hex().upper().encode())]
    escaped = form.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    if escaped != form:
        out.append(("literal", escaped.encode("latin-1", "replace")))
    return out


RESIDUE_RUN = 4


def residue(text: str, form: str, run: int = RESIDUE_RUN) -> str | None:
    """A partial-residue piece of *form* in *text*, by the same rule the
    generator keeps filler free of (persona.residue_in; a test checks
    they agree; owner decision, 2026-10-14): a run of *run* of the
    value's digits inside one whitespace-separated token of *text*, other
    characters in the token dropped; or one of its words of *run* or more
    letters standing as a whole word in *text*, case folded. A whole occurrence of *form* is not residue (the caller
    removes those first)."""
    digits = re.sub(r"\D", "", form)
    if len(digits) >= run:
        runs = {digits[i:i + run] for i in range(len(digits) - run + 1)}
        for token in text.split():
            t = re.sub(r"\D", "", token)
            for i in range(len(t) - run + 1):
                if t[i:i + run] in runs:
                    return t[i:i + run]
    words = {w.lower() for w in re.findall(r"[A-Za-z]{%d,}" % run, form)}
    for w in re.findall(r"[A-Za-z]+", text):
        if w.lower() in words:
            return w
    return None


def _text_residue(data: bytes, form: str) -> str | None:
    text = data.decode("latin-1")
    for _, needle in text_forms(form):
        text = text.replace(needle.decode("latin-1"), " ")
    return residue(text, form)


def _decode_blocks(block: bytes, form: str, code: bytes) -> list[str]:
    """*block*'s characters as far as *form*'s own fingerprint maps them
    (the codes of the characters the value is written with), read with a
    one-byte and with a two-byte code width; any other code reads as ' ',
    so it ends a word or a token. Some producers place words apart instead
    of drawing a space: then the fingerprint covers the value without its
    spaces."""
    out = []
    for width in (1, 2):
        for chars in (list(form), [c for c in form if c != " "]):
            if len(code) != width * len(chars):
                continue
            table = {code[i * width:(i + 1) * width]: ch for i, ch in enumerate(chars)}
            if len(table) < len(set(chars)):
                continue            # two characters share a code: not this width
            out.append("".join(table.get(block[i:i + width], " ")
                               for i in range(0, len(block) - width + 1, width)))
    return out


# Machine-written dates (a PDF date string; XMP date elements) hold digit
# runs that match a secret by chance -- at 12:34 an SSN's 1234 -- so they
# are never searched for partial residue.
_PDF_DATE = re.compile(rb"^D:\d{4,14}")
_XMP_DATES = re.compile(rb"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?|uuid:[0-9a-fA-F-]+")


def _without_dates(data: bytes) -> bytes:
    return b"" if _PDF_DATE.match(data) else _XMP_DATES.sub(b" ", data)


def scan(raw: bytes, forms: dict[str, list[str]], *, partial: bool = True) -> Report:
    """Search *raw* (a whole PDF file) for each written form.

    *forms* maps each written form to its fingerprints (hex). Returns
    every whole hit, and -- when *partial* -- every partial-residue hit
    in a string token, an XMP or attachment body or a joined text block,
    for a form with no whole hit anywhere. Machine-written dates are
    never searched for partial residue.
    """
    found = bodies(raw)
    report = Report(len(found), sum(1 for b in found if b.decoded is None))
    last_eof = raw.rfind(b"%%EOF")
    # Raw bytes outside stream bodies: dictionaries, strings, trailers,
    # anything after %%EOF.
    outside = bytearray(raw)
    for b in found:
        outside[b.start:b.start + len(b.raw)] = b" " * len(b.raw)
    outside_b = bytes(outside)
    # Tokenised once, for every form.
    outside_tokens = _tokens_with_offsets(outside_b)
    body_tokens = {id(b): string_tokens(b.decoded) for b in found
                   if b.decoded is not None and b.kind in ("string", "other")}
    body_blocks = {id(b): text_blocks(b.decoded) for b in found
                   if b.decoded is not None and b.kind == "content"}

    def raw_storage(offset: int) -> str:
        return "unindexed" if last_eof >= 0 and offset > last_eof else ""

    for form, prints in forms.items():
        for how, needle in text_forms(form):
            for m in re.finditer(re.escape(needle), outside_b):
                report.hits.append(Hit(form, "raw", None, m.start(), how,
                                       raw_storage(m.start())))
            for b in found:
                if b.decoded is not None and needle in b.decoded:
                    report.hits.append(Hit(form, b.kind, b.obj, b.start, how, b.storage))
        # Decoded string tokens (escaped literals, hex strings, UTF-16 in
        # either): a match there that the byte search above missed.
        plain = [n for how, n in text_forms(form) if how in ("ascii", "utf16")]
        for offset, token in outside_tokens:
            if any(n in token for n in plain):
                report.hits.append(Hit(form, "raw", None, offset, "string", raw_storage(offset)))
        for b in found:
            if any(any(n in t for n in plain) for t in body_tokens.get(id(b), [])):
                report.hits.append(Hit(form, b.kind, b.obj, b.start, "string", b.storage))
        # Joined text blocks: the form's own fingerprints, or its UTF-16
        # (a two-byte font whose codes are Unicode, split across operands).
        codes = [bytes.fromhex(p) for p in prints] + [form.encode("utf-16-be")]
        for b in found:
            if any(c and c in block for c in codes for block in body_blocks.get(id(b), [])):
                report.hits.append(Hit(form, "content", b.obj, b.start, "codes", b.storage))
        # A whole hit only in a font program or image never counts, so it
        # does not stand in for the partial search either.
        if not partial or any(h.form == form and h.kind not in NOT_RECOVERABLE_ALONE
                              for h in report.hits):
            continue
        # Partial residue, where text lives only, and only when no whole
        # occurrence was found anywhere.
        for offset, token in outside_tokens:
            piece = _text_residue(_without_dates(token), form)
            if piece:
                report.hits.append(Hit(form, "raw", None, offset, f"partial:{piece}",
                                       raw_storage(offset)))
        for b in found:
            if b.decoded is None or b.kind in NOT_RECOVERABLE_ALONE:
                continue
            texts = (body_blocks.get(id(b), []) if b.kind == "content"
                     else body_tokens.get(id(b), []) if b.kind in ("string", "other")
                     else [b.decoded] if b.kind in ("xmp", "embedded") else [])
            for t in texts:
                piece = _text_residue(_without_dates(t), form)
                if piece:
                    report.hits.append(Hit(form, b.kind, b.obj, b.start, f"partial:{piece}",
                                           b.storage))
                    break
            for block in body_blocks.get(id(b), []):
                piece = next((p for c in codes for t in _decode_blocks(block, form, c)
                              if (p := residue(t, form))), None)
                if piece:
                    report.hits.append(Hit(form, "content", b.obj, b.start, f"partial:{piece}",
                                           b.storage))
                    break
    return report


def _tokens_with_offsets(data: bytes) -> list[tuple[int, bytes]]:
    """(offset, decoded string) for every string token in *data*."""
    out = []
    i, n = 0, len(data)
    while i < n:
        m = _TOKEN.search(data, i)
        if not m:
            break
        if m.group(0) == b"(":
            s, i = _literal(data, m.end())
            out.append((m.start(), s))
            continue
        if m.group(1) is not None:
            h = re.sub(rb"\s", b"", m.group(1))
            try:
                out.append((m.start(), binascii.unhexlify(h + (b"0" if len(h) % 2 else b""))))
            except binascii.Error:
                pass
        i = m.end()
    return out
