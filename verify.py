#!/usr/bin/env python3
"""
verify.py — Forensic PDF Redaction Verification Suite.

Detects sensitive strings (secrets) inside a PDF across four independent
layers:

  1. DOM      — layout-aware text extraction in horizontal AND vertical
                reading order, defeats out-of-order draw commands,
                per-character form boxes, and rotated-matrix text.
  2. OCR      — rasterize + Apple Vision (native, hardware-accelerated),
                run with language correction both on and off, defeats
                vector/outlined/image text.
  3. Metadata — exiftool sweep of XMP/Info/embedded metadata (filesystem-
                derived fields excluded so the local path cannot match).
  4. Binary   — qpdf QDF normalization streamed with bounded memory;
                string/hex literals are decoded for high-confidence
                findings, and raw-byte-stream matches are surfaced as
                manual-review warnings (they can be numeric-operand or
                binary-data collisions).

All comparisons happen on *normalized* strings: NFKD-decomposed (folding
fullwidth and compatibility forms to ASCII), combining marks stripped,
casefolded, then reduced to alphanumerics — so dashes, slashes, spaces,
newlines, and Unicode look-alikes cannot hide a match. Secrets are also
searched across page boundaries.

Rules come in three kinds: known 'value' secrets (normalized matching as
above), custom 'pattern' regexes, and built-in pattern 'class' rules
(see BUILTIN_PATTERN_CLASSES or --help for the roster) with validators
(Luhn, SSA area rules, NANP) that suppress structurally invalid matches.
Pattern matching is two-tier: single-line/single-literal matches are
hard findings; matches that only appear in fused lines, vertical
reconstructions, or across token/page boundaries are demoted to
manual-review warnings (exit 2). Matched samples are masked and
sanitized in the report.

Usage:
    python verify.py --target document.pdf --secrets secrets.json

Exit codes:
    0 — PASS: no secret found in any layer, all layers ran clean.
    1 — FAIL: at least one secret leaked in at least one layer.
    2 — ERROR / UNCERTIFIED: operational failure (bad input, missing
        tool), a layer could not be (fully) scanned, or a raw-stream
        match needs manual review — a clean result would not be
        trustworthy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterator, NoReturn, Sequence

# ──────────────────────────────────────────────────────────────────────────
# Third-party imports (fail with a clear message, not a traceback)
# ──────────────────────────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.stderr.write("[ERROR] PyMuPDF is required: pip install pymupdf\n")
    sys.exit(2)

try:
    import Vision
    from Foundation import NSData

    _OCR_IMPORTS_OK = True
except ImportError:  # pragma: no cover
    _OCR_IMPORTS_OK = False


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────
SUBPROCESS_TIMEOUT_S: int = 120
OCR_DPI: int = 300
# Floor for the visual-line clustering tolerance; the effective tolerance
# scales with the median glyph size on the page so large form-box digits
# with baseline jitter still cluster into one line.
MIN_LINE_TOLERANCE_PT: float = 4.0
# Ceiling: prevents a page dominated by large glyphs (watermarks,
# headers) from inflating the tolerance so much that fine-print lines
# get merged, scrambling their text and causing false negatives.
MAX_LINE_TOLERANCE_PT: float = 12.0
QPDF_CHUNK_BYTES: int = 4 << 20
# A PDF string literal left unclosed by damaged output must not grow the
# carry buffer without bound.
MAX_LITERAL_CARRY: int = 64 << 10
# Pattern scanning: matches may span feed boundaries up to the overlap;
# feeds are batched before regex sweeps (per-tiny-literal sweeps measure
# ~100x slower than batched ones).
PATTERN_SCAN_OVERLAP: int = 512
PATTERN_SCAN_BATCH: int = 64 << 10

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# PDF string objects in QDF output: (literal with \-escapes) or <hex>.
# Balanced *unescaped* nested parens are legal PDF but rare in qpdf
# output (it escapes them); such a literal would be truncated here — the
# raw-stream sweep remains as the recall backstop.
_PDF_STRING_RE = re.compile(r"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>", re.S)
_LITERAL_ESCAPE_RE = re.compile(r"\\([0-7]{1,3}|.)", re.S)

# exiftool fields that describe the local filesystem or tool, not the
# document — a secret matching the file's own path or timestamps must not
# count as a leak inside the PDF.
EXIFTOOL_FILESYSTEM_FIELDS: frozenset[str] = frozenset({
    "SourceFile",
    "ExifToolVersion",
    "FileName",
    "Directory",
    "FileSize",
    "FileModifyDate",
    "FileAccessDate",
    "FileInodeChangeDate",
    "FilePermissions",
})


class VerifyError(Exception):
    """Operational failure that must exit with code 2, never 1."""


# ──────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Secret:
    """A named sensitive value's normalized search key."""

    name: str
    normalized: str


@dataclass(frozen=True)
class Finding:
    """A single leak: which rule surfaced in which layer, and where.

    *sample* carries the raw matched text for pattern rules (empty for
    value secrets); it is masked and sanitized at render time only, and
    excluded from equality so dedup keys on (layer, rule, location).
    """

    layer: str          # "DOM" | "OCR" | "Metadata" | "Binary"
    secret_name: str
    location: str
    sample: str = field(default="", compare=False)


@dataclass
class ScanReport:
    """Aggregated results across all layers. Findings are unique."""

    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _seen: set[Finding] = field(default_factory=set, repr=False)

    def record(
        self, layer: str, secret_name: str, location: str, sample: str = ""
    ) -> None:
        finding = Finding(layer, secret_name, location, sample)
        if finding not in self._seen:
            self._seen.add(finding)
            self.findings.append(finding)

    @property
    def leaked(self) -> bool:
        return bool(self.findings)

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


# ──────────────────────────────────────────────────────────────────────────
# PHASE 1: The Normalizer
# ──────────────────────────────────────────────────────────────────────────
def normalize_string(text: str) -> str:
    """Collapse a string to its forensic essence.

    NFKD decomposition folds fullwidth/compatibility forms (１２３ → 123)
    and splits accents into combining marks, which are then stripped
    (José → jose); casefold handles case beyond ASCII (ß → ss); finally
    everything non-alphanumeric is removed. "123-45-6789", "123 45 6789",
    and "１２３－４５－６７８９" all normalize to "123456789".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM_RE.sub("", without_marks.casefold())


class SecretMatcher:
    """Single-pass, overlap-safe search for every secret at once.

    Uses a lookahead alternation so one occurrence cannot consume the
    text of an overlapping occurrence of another secret.
    """

    def __init__(self, secrets: Sequence[Secret]) -> None:
        self._secrets: list[Secret] = list(secrets)
        norms = sorted({s.normalized for s in self._secrets}, key=len, reverse=True)
        # A rules file may hold only pattern rules — an empty matcher is
        # valid and simply never matches.
        self._pattern = (
            re.compile("(?=(" + "|".join(map(re.escape, norms)) + "))") if norms else None
        )
        self.max_len: int = max(map(len, norms), default=0)

    def search(self, normalized_haystack: str) -> list[Secret]:
        if self._pattern is None:
            return []
        hits = {m.group(1) for m in self._pattern.finditer(normalized_haystack)}
        return [s for s in self._secrets if s.normalized in hits]


class RollingScanner:
    """Feed normalized text incrementally with bounded memory.

    Keeps a tail of max_len-1 characters so matches spanning feed
    boundaries (page breaks, adjacent PDF string tokens, byte-stream
    chunks) are still found.
    """

    def __init__(self, matcher: SecretMatcher) -> None:
        self._matcher = matcher
        self._tail: str = ""
        self.found: set[Secret] = set()

    def feed(self, normalized_chunk: str) -> None:
        window = self._tail + normalized_chunk
        self.found.update(self._matcher.search(window))
        keep = self._matcher.max_len - 1
        self._tail = window[-keep:] if keep > 0 else ""


# ──────────────────────────────────────────────────────────────────────────
# Pattern rules: match CLASSES of sensitive data, not just known values
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PatternRule:
    """A rule that matches a class of sensitive data (e.g. "any SSN").

    Patterns run against the *raw* extracted text of each layer (visual
    reconstruction, OCR output, decoded metadata values, decoded PDF
    literals), folded through _fold_for_patterns first so Unicode dashes,
    exotic spaces, and fullwidth digits cannot evade an ASCII regex.
    """

    name: str
    regex: re.Pattern[str]
    validator: Callable[[str], bool] | None = None


# Unicode look-alikes folded to ASCII before pattern matching: hyphen and
# dash variants to '-', space variants to ' ', NULs (UTF-16 interleaving
# residue) removed. NFKC in _fold_for_patterns handles fullwidth digits.
_PATTERN_FOLD_TABLE = {
    **{cp: "-" for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)},
    **{cp: " " for cp in (0x00A0, 0x2007, 0x2009, 0x200A, 0x202F, 0x3000)},
    0x0000: None,
}


def _fold_for_patterns(text: str) -> str:
    """Fold text so class regexes see canonical ASCII digits/separators."""
    return unicodedata.normalize("NFKC", text).translate(_PATTERN_FOLD_TABLE)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _valid_ssn(matched: str) -> bool:
    """SSA-issued SSNs never use area 000/666/9xx, group 00, or serial 0000."""
    d = re.sub(r"\D", "", matched)
    return not (
        d[:3] in ("000", "666") or d[0] == "9" or d[3:5] == "00" or d[5:] == "0000"
    )


def _valid_card(matched: str) -> bool:
    d = re.sub(r"\D", "", matched)
    if not (13 <= len(d) <= 19 and len(set(d)) > 1 and _luhn_ok(d)):
        return False
    # PDF date stamps (D:YYYYMMDDHHmmSS) are 14-digit runs that pass Luhn
    # ~10% of the time; no real 14-digit card IIN starts with 19 or 20.
    if len(d) == 14 and d[:2] in ("19", "20"):
        return False
    return True


_EMAIL_FILE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "tif", "tiff",
    "pdf", "eps", "ico", "heic",
})


def _valid_email(matched: str) -> bool:
    """Reject retina-asset-style filenames like logo@2x.png."""
    return matched.rsplit(".", 1)[-1].lower() not in _EMAIL_FILE_EXTENSIONS


def _valid_nanp(matched: str) -> bool:
    """NANP: 10 digits (optionally +1); area and exchange start with 2-9."""
    d = re.sub(r"\D", "", matched)
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789" and d[3] in "23456789"


# name -> (regex source, validator). Regexes tolerate common separators,
# use digit-boundary guards (including a preceding '.', so decimal
# fractions cannot match), and require CONSISTENT separators via a
# backreference so ZIP+4 codes ('12345-6789') cannot regroup into SSNs.
BUILTIN_PATTERN_CLASSES: dict[str, tuple[str, Callable[[str], bool] | None]] = {
    "ssn": (r"(?<![\d.])(?:\d{3}([-\s.])\d{2}\1\d{4}|\d{9})(?!\d)", _valid_ssn),
    "credit-card": (r"(?<![\d.])(?:\d[-\s.]?){12,18}\d(?!\d)", _valid_card),
    "email": (
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        _valid_email,
    ),
    "us-phone": (
        r"(?<![\d.])(?:\+?1[-\s.]?)?\(?\d{3}\)?[-\s.]?\d{3}[-\s.]?\d{4}(?!\d)",
        _valid_nanp,
    ),
}


def mask(matched: str) -> str:
    """Mask a matched sample for the report.

    Reveals at most 4 trailing characters and never more than half the
    match; the fixed '****' prefix hides the true length.
    """
    reveal = min(4, len(matched) // 2)
    return "****" + (matched[-reveal:] if reveal else "")


def match_patterns(
    raw_text: str,
    patterns: Sequence[PatternRule],
    already: Collection[str] = (),
    *,
    collapse_separators: bool = False,
) -> dict[str, str]:
    """First validated match per rule not in *already*: {name: matched text}.

    A validator rejection retries from just inside the rejected span (a
    greedy superspan must not swallow an embedded valid match), and
    zero-width matches are never findings. *collapse_separators* squeezes
    runs of dashes/whitespace to one '-' so line-wrapped values like
    '123-45-\\n6789' still match — fusion-prone, so only the soft
    (manual-review) tier enables it.
    """
    remaining = [r for r in patterns if r.name not in already]
    if not remaining:
        return {}
    text = _fold_for_patterns(raw_text)
    if collapse_separators:
        text = re.sub(r"[-\s]{2,}", "-", text)
    hits: dict[str, str] = {}
    for rule in remaining:
        pos = 0
        while pos <= len(text):
            m = rule.regex.search(text, pos)
            if m is None:
                break
            matched = m.group(0)
            if not matched:
                pos = m.end() + 1
                continue
            if rule.validator is None or rule.validator(matched):
                hits[rule.name] = matched
                break
            pos = m.start() + 1
    return hits


class PatternScanner:
    """Rolling raw-text pattern scanner with bounded memory.

    Feeds are buffered and matched in batches (per-literal regex sweeps
    are ~100x slower); the overlap tail lets a match span feed boundaries
    up to PATTERN_SCAN_OVERLAP chars. Callers must flush() when the
    stream ends.
    """

    def __init__(
        self, patterns: Sequence[PatternRule], *, collapse_separators: bool = False
    ) -> None:
        self._patterns = list(patterns)
        self._collapse = collapse_separators
        self._buffer: list[str] = []
        self._buffered = 0
        self._tail: str = ""
        self.hits: dict[str, str] = {}

    def feed(self, raw_text: str) -> None:
        if len(self.hits) == len(self._patterns):
            return  # every rule already hit; further scanning is unobservable
        self._buffer.append(raw_text)
        self._buffered += len(raw_text)
        if self._buffered >= PATTERN_SCAN_BATCH:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        window = self._tail + "".join(self._buffer)
        self._buffer.clear()
        self._buffered = 0
        for name, sample in match_patterns(
            window, self._patterns, self.hits,
            collapse_separators=self._collapse,
        ).items():
            self.hits[name] = sample
        self._tail = window[-PATTERN_SCAN_OVERLAP:]


# ──────────────────────────────────────────────────────────────────────────
# PHASE 2: Layout-Aware Text Extraction (DOM layer)
# ──────────────────────────────────────────────────────────────────────────
def _reconstruct(
    glyphs: list[tuple[float, float, float, float, str]],
    cluster_axis: int,
) -> str:
    """Cluster glyphs into visual lines along one axis, sort along the other.

    glyphs: (x_center, y_center, width, height, char).
    cluster_axis 1 clusters by y (horizontal lines, read left-to-right);
    cluster_axis 0 clusters by x (vertical columns, read top-to-bottom).
    The clustering tolerance scales with the median glyph extent along the
    cluster axis, so 30pt form-box digits with 6pt baseline jitter still
    land in one line while 8pt fine print keeps its lines separate.
    """
    if not glyphs:
        return ""

    extents = sorted(g[3] if cluster_axis == 1 else g[2] for g in glyphs)
    median_extent = extents[len(extents) // 2]
    tolerance = max(MIN_LINE_TOLERANCE_PT, min(MAX_LINE_TOLERANCE_PT, 0.5 * median_extent))
    order_axis = 1 - cluster_axis

    ordered = sorted(glyphs, key=lambda g: g[cluster_axis])
    clusters: list[list[tuple[float, float, float, float, str]]] = []
    current = [ordered[0]]
    center = ordered[0][cluster_axis]
    for glyph in ordered[1:]:
        if abs(glyph[cluster_axis] - center) <= tolerance:
            current.append(glyph)
            # Running mean keeps the cluster stable against drift.
            center += (glyph[cluster_axis] - center) / len(current)
        else:
            clusters.append(current)
            current = [glyph]
            center = glyph[cluster_axis]
    clusters.append(current)

    lines: list[str] = []
    for cluster in clusters:
        cluster.sort(key=lambda g: g[order_axis])
        lines.append(_join_cluster(cluster, order_axis))
    return "\n".join(lines)


def _join_cluster(
    cluster: list[tuple[float, float, float, float, str]], order_axis: int
) -> str:
    """Join one visual line's glyphs, breaking it at column gaps.

    Whitespace glyphs are dropped during extraction, so without this a
    table ROW ('123' | '45' | '6789' in three columns) would concatenate
    into '123456789' and read as one contiguous number. A gap is treated
    as a column break — emitted as a newline, which no single [-\\s.]
    separator slot can cross — when it is BOTH a large outlier against the
    other gaps on this line AND wider than a character. Both conditions
    matter: form boxes space every glyph widely but *uniformly* (no
    outlier, so the run stays intact), while ordinary word spaces are
    narrower than a character (so 'SSN 123 45 6789' stays intact too).
    """
    if len(cluster) < 2:
        return "".join(g[4] for g in cluster)

    extent_index = 2 if order_axis == 0 else 3
    gaps: list[float] = []
    for prev, nxt in zip(cluster, cluster[1:]):
        prev_end = prev[order_axis] + prev[extent_index] / 2.0
        next_start = nxt[order_axis] - nxt[extent_index] / 2.0
        gaps.append(max(0.0, next_start - prev_end))

    ordered_gaps = sorted(gaps)
    median_gap = ordered_gaps[len(ordered_gaps) // 2]
    extents = sorted(g[extent_index] for g in cluster)
    median_extent = extents[len(extents) // 2]
    threshold = max(3.0 * median_gap, 1.5 * median_extent)

    out = [cluster[0][4]]
    for gap, glyph in zip(gaps, cluster[1:]):
        if gap > threshold:
            out.append("\n")
        out.append(glyph[4])
    return "".join(out)


def extract_visual_text(page: fitz.Page) -> list[str]:
    """Reconstruct page text in visual reading orders.

    Returns up to three variants: horizontal (top-to-bottom lines, the
    primary reading order), vertical columns top-to-bottom, and vertical
    columns bottom-to-top. The vertical variants catch text drawn with a
    rotated matrix, whose glyphs form vertical runs that a horizontal
    line sort scrambles.
    """
    glyphs: list[tuple[float, float, float, float, str]] = []
    raw: dict[str, Any] = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    c: str = char.get("c", "")
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = char["bbox"]
                    glyphs.append(
                        ((x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0, c)
                    )

    if not glyphs:
        return []

    horizontal = _reconstruct(glyphs, cluster_axis=1)
    vertical_down = _reconstruct(glyphs, cluster_axis=0)
    vertical_up = "\n".join(line[::-1] for line in vertical_down.split("\n"))
    return [horizontal, vertical_down, vertical_up]


# ──────────────────────────────────────────────────────────────────────────
# PHASE 3: OCR Visual Fallback (Apple Vision)
# ──────────────────────────────────────────────────────────────────────────
def _vision_recognize_batch(png_bytes: bytes) -> tuple[str, str]:
    """OCR a PNG with Apple Vision in one pass: correction on AND off.

    Both requests share a single VNImageRequestHandler so the image is
    decoded once instead of twice.  Raises on any Vision-level failure.
    """
    ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    results: dict[bool, list[str]] = {True: [], False: []}
    errors: dict[bool, list[str]] = {True: [], False: []}

    def _make_handler(correction: bool) -> Callable[[Any, Any], None]:
        def handler(request: Any, error: Any) -> None:
            if error:
                errors[correction].append(str(error))
                return
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates:
                    results[correction].append(candidates[0].string())
        return handler

    request_on = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
        _make_handler(True)
    )
    request_on.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request_on.setUsesLanguageCorrection_(True)

    request_off = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
        _make_handler(False)
    )
    request_off.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request_off.setUsesLanguageCorrection_(False)

    image_handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        ns_data, {}
    )
    success, perform_error = image_handler.performRequests_error_(
        [request_on, request_off], None
    )
    if not success:
        raise RuntimeError(f"Apple Vision request failed: {perform_error}")
    for correction in (True, False):
        if errors[correction]:
            raise RuntimeError(f"Apple Vision error: {'; '.join(errors[correction])}")

    return ("\n".join(results[True]), "\n".join(results[False]))


def extract_ocr_text(page: fitz.Page) -> list[str]:
    """Render a page and OCR it twice: language correction on AND off.

    Correction-on reads prose reliably; correction-off preserves literal
    code/serial-number character sequences that the language model might
    otherwise "correct". Searching the union maximizes recall. Grayscale
    rendering: Vision does not need color and the pixmap is 3x smaller.

    Both recognition passes share one image handler so the PNG is decoded
    once (see _vision_recognize_batch).
    """
    pix: fitz.Pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=fitz.csGRAY)
    png_bytes: bytes = pix.tobytes("png")
    pix = None  # release the raster before Vision runs
    text_on, text_off = _vision_recognize_batch(png_bytes)
    return [text_on, text_off]


# ──────────────────────────────────────────────────────────────────────────
# Shared per-page layer driver (DOM and OCR)
# ──────────────────────────────────────────────────────────────────────────
def scan_page_layer(
    doc: fitz.Document,
    matcher: SecretMatcher,
    report: ScanReport,
    *,
    layer: str,
    extractor: Callable[[fitz.Page], list[str]],
    note: str,
    patterns: Sequence[PatternRule] = (),
    hard_variants: int = 1,
    fail_fast: bool = False,
) -> None:
    """Run a per-page text extractor over the document and match secrets.

    Every extracted variant is searched per page; *each* variant is
    additionally streamed through its own RollingScanner so secrets that
    span a page boundary are still caught — including secrets in vertical
    or rotated text (reported without a page number).

    Pattern rules match per visual line of the first *hard_variants*
    variants — those the extractor produces in natural reading order —
    giving hard findings aggregated per rule across pages. Matches that
    appear only in fused multi-line text, in reconstruction-derived
    variants (DOM's vertical column orders), or across page boundaries
    are demoted to manual-review warnings. Callers set *hard_variants* to
    the number of leading variants that are genuine reads rather than
    reconstructions: 1 for DOM, all of them for OCR.

    When *fail_fast* is True the scan stops after the first page that
    produces a finding, so the tool exits quickly on large documents.
    """
    cross_page_scanners: list[RollingScanner] = []
    cross_page_patterns = PatternScanner(patterns, collapse_separators=True)
    layer_hard: dict[str, tuple[list[int], str]] = {}
    layer_soft: dict[str, tuple[list[int], str]] = {}
    for page_index in range(doc.page_count):
        try:
            page = doc.load_page(page_index)
            variants = extractor(page)
        except Exception as exc:  # a corrupt page must not abort the scan
            report.warnings.append(f"{layer}: page {page_index + 1} failed ({exc})")
            continue
        for vi, text in enumerate(variants):
            normalized_full = normalize_string(text)
            # High-confidence: match each visual line individually.
            per_line_hits: set[str] = set()
            for line in text.split("\n"):
                norm_line = normalize_string(line)
                if norm_line:
                    for secret in matcher.search(norm_line):
                        per_line_hits.add(secret.name)
                        report.record(layer, secret.name,
                                      f"page {page_index + 1} ({note})")
            # Cross-line: the full normalized text fuses all lines into
            # one string.  A match that appears only here (not within
            # any single line) may be a coincidental concatenation of
            # adjacent tokens — the same collision class the Binary
            # layer explicitly demotes to a manual-review warning.
            for secret in matcher.search(normalized_full):
                if secret.name not in per_line_hits:
                    report.warnings.append(
                        f"{layer}: page {page_index + 1} contains a cross-line "
                        f"sequence matching secret {secret.name!r} — possibly "
                        "a coincidental concatenation of adjacent tokens; "
                        "manual review recommended"
                    )
            # Feed every variant into its own cross-page scanner so
            # vertical/rotated text spanning a page boundary is caught.
            while len(cross_page_scanners) <= vi:
                cross_page_scanners.append(RollingScanner(matcher))
            cross_page_scanners[vi].feed(normalized_full)
        # Pattern rules, two tiers. Hard findings come only from single
        # visual lines of the primary variant (natural reading order) —
        # matches that need fused lines, vertical column reconstructions,
        # or secondary variants are the same coincidental-concatenation
        # class the value pipeline demotes, so they become manual-review
        # warnings instead of hard FAILs.
        page_hard: dict[str, str] = {}
        page_soft: dict[str, str] = {}
        if variants:
            for text in variants[:hard_variants]:
                for line in text.split("\n"):
                    page_hard.update(match_patterns(line, patterns, page_hard))
            for text in variants:
                seen = set(page_hard) | set(page_soft)
                if len(seen) == len(patterns):
                    break
                page_soft.update(
                    match_patterns(text, patterns, seen, collapse_separators=True)
                )
        for name, sample in page_hard.items():
            hard_pages, _ = layer_hard.setdefault(name, ([], sample))
            hard_pages.append(page_index + 1)
        for name, sample in page_soft.items():
            soft_pages, _ = layer_soft.setdefault(name, ([], sample))
            soft_pages.append(page_index + 1)
        if patterns:
            cross_page_patterns.feed("\n" + (variants[0] if variants else ""))
        if fail_fast and (report.leaked or layer_hard):
            break

    found_in_layer = {f.secret_name for f in report.findings if f.layer == layer}
    for scanner in cross_page_scanners:
        for secret in scanner.found:
            if secret.name not in found_in_layer:
                report.record(layer, secret.name, f"across page boundaries ({note})")

    # Aggregate pattern results: one finding (or warning) per rule.
    def _pages_label(pages: list[int]) -> str:
        shown = ", ".join(str(p) for p in pages[:3])
        extra = f" (+{len(pages) - 3} more)" if len(pages) > 3 else ""
        return f"page{'s' if len(pages) > 1 else ''} {shown}{extra}"

    for name, (pages, sample) in layer_hard.items():
        report.record(layer, name, f"{_pages_label(pages)} ({note})", sample)
    cross_page_patterns.flush()
    for name, sample in cross_page_patterns.hits.items():
        if name not in layer_hard and name not in layer_soft:
            layer_soft[name] = ([], sample)
    for name, (pages, sample) in layer_soft.items():
        if name in layer_hard:
            continue
        where = _pages_label(pages) if pages else "across page boundaries"
        report.warnings.append(
            f"{layer}: {where}: sequence matching pattern rule {name!r} appears "
            f"only when lines, columns or pages are fused (sample {mask(sample)})"
            " — possibly coincidental concatenation; manual review recommended"
        )


# ──────────────────────────────────────────────────────────────────────────
# PHASE 4: Metadata & Stream Scraping (exiftool / qpdf, run concurrently)
# ──────────────────────────────────────────────────────────────────────────
def _start_tool(
    report: ScanReport, layer: str, argv: Sequence[str]
) -> subprocess.Popen[bytes] | None:
    """Launch an external tool; a start failure degrades the layer loudly."""
    try:
        return subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        report.warnings.append(
            f"{layer}: {argv[0]} not installed — layer NOT scanned"
        )
    except OSError as exc:
        report.warnings.append(
            f"{layer}: {argv[0]} failed to start ({exc}) — layer NOT scanned"
        )
    return None


def start_hidden_tools(
    pdf_path: Path, report: ScanReport
) -> dict[str, subprocess.Popen[bytes] | None]:
    """Kick off exiftool and qpdf now so they run behind the DOM/OCR scans."""
    return {
        "exiftool": _start_tool(
            report, "Metadata", ["exiftool", "-json", str(pdf_path)]
        ),
        "qpdf": _start_tool(
            report,
            "Binary",
            ["qpdf", "--qdf", "--object-streams=disable", str(pdf_path), "-"],
        ),
    }


def kill_hidden_tools(procs: dict[str, subprocess.Popen[bytes] | None]) -> None:
    for proc in procs.values():
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()


def _iter_json_strings(node: Any) -> Iterator[str]:
    """Yield every string VALUE in a decoded JSON structure (keys skipped)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_json_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_json_strings(value)


def scan_xmp_metadata(
    doc: fitz.Document,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Scan the in-document XMP packet directly via PyMuPDF.

    XMP is bare XML inside a stream — invisible to the qpdf string-literal
    sweep — so it gets its own first-class scan for both rule kinds.
    """
    try:
        xmp = doc.get_xml_metadata()
    except Exception as exc:
        report.warnings.append(f"Metadata: XMP packet unreadable ({exc})")
        return
    if not xmp:
        return
    for secret in matcher.search(normalize_string(xmp)):
        report.record("Metadata", secret.name, "XMP packet (in-document)")
    for name, sample in match_patterns(xmp, patterns).items():
        report.record("Metadata", name, "XMP packet (in-document)", sample)


def _collect_exiftool(
    proc: subprocess.Popen[bytes],
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    try:
        out, _ = proc.communicate(timeout=SUBPROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        report.warnings.append("Metadata: exiftool timed out — layer NOT scanned")
        return
    if proc.returncode != 0:
        report.warnings.append(
            f"Metadata: exiftool exited {proc.returncode} — results may be incomplete"
        )
    if not out:
        report.warnings.append("Metadata: exiftool produced no output — layer NOT scanned")
        return

    text = out.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
        # Exclude filesystem-derived fields: the local path or timestamps
        # matching a secret is not a leak inside the document.
        if isinstance(payload, list):
            non_dict_count = sum(1 for e in payload if not isinstance(e, dict))
            if non_dict_count:
                report.warnings.append(
                    f"Metadata: exiftool JSON contained {non_dict_count} "
                    "non-object entry(ies) — skipped; results may be incomplete"
                )
            filtered = [
                {k: v for k, v in entry.items() if k not in EXIFTOOL_FILESYSTEM_FIELDS}
                for entry in payload
                if isinstance(entry, dict)
            ]
        elif isinstance(payload, dict):
            filtered = {
                k: v for k, v in payload.items()
                if k not in EXIFTOOL_FILESYSTEM_FIELDS
            }
        else:
            filtered = payload
        # Secrets search the serialized JSON (normalization erases the
        # escaping); patterns need DECODED values — json.dumps escapes
        # \n/\t inside values, which would break \s separator slots — and
        # string values only, so numeric fields can't false-positive.
        # Fence values with a blank line: a single newline would satisfy
        # the class regexes' [-\s.] separator slot and let two unrelated
        # fields fuse into a hard finding (as the literal fence in
        # _feed_pdf_strings already guards against).
        value_text = "\n\n".join(_iter_json_strings(filtered))
        for secret in matcher.search(
            normalize_string(json.dumps(filtered, ensure_ascii=False))
        ):
            report.record(
                "Metadata", secret.name, "exiftool field sweep (XMP/Info/embedded)"
            )
        for name, sample in match_patterns(value_text, patterns).items():
            report.record(
                "Metadata", name, "exiftool field sweep (XMP/Info/embedded)", sample
            )
    except json.JSONDecodeError:
        report.warnings.append(
            "Metadata: exiftool output was not valid JSON — scanned raw output, "
            "which includes filesystem fields (path collisions possible)"
        )
        for secret in matcher.search(normalize_string(text)):
            report.record(
                "Metadata", secret.name, "exiftool field sweep (raw fallback)"
            )
        # Raw fallback text includes filesystem paths, so pattern hits
        # here are collision-prone: manual-review warnings, not findings.
        for name, sample in match_patterns(text, patterns).items():
            report.warnings.append(
                f"Metadata: raw exiftool output matches pattern rule {name!r} "
                f"(sample {mask(sample)}) — may originate from filesystem "
                "fields; manual review recommended"
            )


def _unescape_pdf_literal(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        esc = match.group(1)
        if esc and esc[0] in "01234567" and all(c in "01234567" for c in esc):
            return chr(int(esc, 8) & 0xFF)
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(esc, esc)

    return _LITERAL_ESCAPE_RE.sub(repl, body)


def _feed_pdf_strings(
    buf: str,
    scanner: RollingScanner,
    hard_patterns: PatternScanner,
    soft_patterns: PatternScanner,
) -> str:
    """Extract PDF string/hex literals from buf and feed all scanners.

    The normalized secret scanner and the *soft* pattern scanner see
    literals back-to-back (adjacency preserved, so split values are
    caught); the *hard* pattern scanner gets a two-newline fence between
    literals — a single [-\\s.] separator slot cannot cross it — so a
    hard pattern finding can never be a fusion of unrelated tokens.
    Returns the unconsumed tail as carry for the next chunk.
    """
    last_end = 0
    for match in _PDF_STRING_RE.finditer(buf):
        token = match.group(0)
        last_end = match.end()
        if token.startswith("("):
            decoded = _unescape_pdf_literal(token[1:-1])
            # A literal may itself hold UTF-16BE text (BOM-prefixed).
            if decoded.startswith("\xfe\xff"):
                decoded = (
                    decoded[2:].encode("latin-1", errors="replace")
                    .decode("utf-16-be", errors="replace")
                )
        else:
            hex_chars = re.sub(r"\s+", "", token[1:-1])
            if len(hex_chars) % 2:
                hex_chars += "0"
            try:
                data = bytes.fromhex(hex_chars)
            except ValueError:
                continue
            if data[:2] == b"\xfe\xff":
                decoded = data[2:].decode("utf-16-be", errors="replace")
            else:
                decoded = data.decode("latin-1")
        scanner.feed(normalize_string(decoded))
        hard_patterns.feed(decoded + "\n\n")
        soft_patterns.feed(decoded)
    return buf[last_end:]


def _collect_qpdf(
    proc: subprocess.Popen[bytes],
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Stream qpdf's QDF output with bounded memory.

    Two scanners run over the stream: decoded string/hex literals give
    high-confidence findings; the raw fused byte stream is the recall
    backstop, but its matches can be coincidental collisions (adjacent
    numeric operands, compressed binary data), so raw-only matches are
    reported as manual-review warnings (exit 2), not hard findings.
    Pattern rules run on decoded literals only — regexes over the raw
    latin-1 byte soup would false-positive on compressed stream data.
    """
    raw = RollingScanner(matcher)
    literals = RollingScanner(matcher)
    hard_patterns = PatternScanner(patterns)
    soft_patterns = PatternScanner(patterns, collapse_separators=True)
    carry = ""
    carry_truncated = False
    got_output = False
    deadline = time.monotonic() + SUBPROCESS_TIMEOUT_S
    if proc.stdout is None:
        report.warnings.append("Binary: qpdf stdout unavailable — layer NOT scanned")
        return

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            proc.communicate()
            report.warnings.append("Binary: qpdf timed out — scan incomplete")
            break
        # Use select() so the deadline is enforced even if qpdf stalls
        # mid-stream — a plain read() would block indefinitely.
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            proc.kill()
            proc.communicate()
            report.warnings.append("Binary: qpdf timed out — scan incomplete")
            break
        chunk = os.read(proc.stdout.fileno(), QPDF_CHUNK_BYTES)
        if not chunk:
            break
        got_output = True
        text = chunk.decode("latin-1")  # 1:1 byte mapping, nothing lost
        raw.feed(normalize_string(text))
        carry = _feed_pdf_strings(carry + text, literals, hard_patterns, soft_patterns)
        if len(carry) > MAX_LITERAL_CARRY:
            carry = carry[-MAX_LITERAL_CARRY:]
            carry_truncated = True

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    # Any nonzero exit degrades the scan — including 3. qpdf documents
    # exit 3 as "warnings", but empirically a truncated input PDF exits 3
    # while emitting only a partial QDF stream with tail objects silently
    # absent, so exit-3 output cannot be certified complete.
    if proc.returncode != 0:
        report.warnings.append(
            f"Binary: qpdf exited {proc.returncode} — QDF output may be truncated, "
            "binary scan may be incomplete"
        )
    if carry_truncated:
        report.warnings.append(
            f"Binary: an unclosed PDF string literal exceeded "
            f"{MAX_LITERAL_CARRY // 1024}KB — carry buffer was truncated, "
            "some literal content may not have been scanned"
        )
    if not got_output:
        report.warnings.append("Binary: qpdf produced no output — layer NOT scanned")
        return

    hard_patterns.flush()
    soft_patterns.flush()
    for secret in sorted(literals.found, key=lambda s: s.name):
        report.record("Binary", secret.name, "qpdf QDF string/hex literals")
    for name, sample in hard_patterns.hits.items():
        report.record("Binary", name, "qpdf QDF string/hex literals", sample)
    for name, sample in soft_patterns.hits.items():
        if name in hard_patterns.hits:
            continue
        report.warnings.append(
            f"Binary: adjacent decoded literals fuse into a sequence matching "
            f"pattern rule {name!r} (sample {mask(sample)}) — possibly a "
            "coincidental concatenation of unrelated tokens; manual review "
            "recommended"
        )
    for secret in sorted(raw.found - literals.found, key=lambda s: s.name):
        report.warnings.append(
            f"Binary: raw byte stream contains a sequence matching secret "
            f"{secret.name!r} — possibly a coincidental collision of numeric "
            "operands or binary data; manual review recommended"
        )


def check_hidden_layers(
    procs: dict[str, subprocess.Popen[bytes] | None],
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Phase 4 collector: reap the tools started by start_hidden_tools."""
    if procs["exiftool"] is not None:
        _collect_exiftool(procs["exiftool"], matcher, patterns, report)
    if procs["qpdf"] is not None:
        _collect_qpdf(procs["qpdf"], matcher, patterns, report)


# ──────────────────────────────────────────────────────────────────────────
# PHASE 5: The CLI Orchestrator
# ──────────────────────────────────────────────────────────────────────────
# redactor entity types that have a regex equivalent here. The value
# is one of THIS tool's built-in classes: verification deliberately uses
# its own regexes and validators rather than importing the redactor's, so
# a flaw in the redactor's detection cannot hide itself from the check.
ENTITY_TYPE_TO_CLASS: dict[str, str] = {
    "ssn": "ssn",
    "email": "email",
    "phone": "us-phone",
    "credit_card": "credit-card",
}


@dataclass
class RuleSet:
    """Rules to scan for, plus what the source config asked for that
    cannot be scanned for at all."""

    secrets: list[Secret] = field(default_factory=list)
    patterns: list[PatternRule] = field(default_factory=list)
    # Entity types a redactor was told to remove that have no regex
    # equivalent (LLM-detected categories). Recorded, never dropped:
    # they are a hole in verification scope and must be reported.
    unverifiable: list[str] = field(default_factory=list)


def load_rules(rules_path: Path) -> RuleSet:
    """Load verification rules from a JSON rules file or a YAML config.

    A '.yaml'/'.yml' suffix selects the redactor's redact_config format,
    so one file can drive both redaction and verification; anything else
    is parsed as this tool's native JSON rules array.
    """
    if rules_path.suffix.lower() in (".yaml", ".yml"):
        return _load_rules_yaml(rules_path)
    return _load_rules_json(rules_path)


def _load_rules_yaml(rules_path: Path) -> RuleSet:
    """Load a redactor's redact_config.yaml as verification rules.

    Mapping:
      exact_values -> value rules (normalized matching)
      patterns     -> pattern rules, compiled IGNORECASE as the redactor
                      compiles them, plus MULTILINE for anchor sanity
      entity_types -> this tool's own built-in classes; types with no
                      regex equivalent land in RuleSet.unverifiable

    Keys the redactor needs but verification does not (backend, model,
    llm_url, scrub_metadata) are ignored.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - declared dependency
        raise VerifyError("reading a YAML config needs PyYAML: pip install pyyaml")
    try:
        raw: Any = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VerifyError(f"Cannot read rules file {rules_path}: {exc}")
    if not isinstance(raw, dict):
        raise VerifyError("a YAML rules file must be a mapping")

    rules = RuleSet()
    seen: set[str] = set()

    def claim(name: str) -> str:
        """Names are rule identity and must be unique; YAML lists carry
        none, so synthesize stable ones from the source location."""
        candidate, n = name, 2
        while candidate in seen:
            candidate, n = f"{name} ({n})", n + 1
        seen.add(candidate)
        return candidate

    values = raw.get("exact_values") or []
    if not isinstance(values, list):
        raise VerifyError("exact_values must be a list")
    for i, value in enumerate(values):
        normalized = normalize_string(str(value))
        if not normalized:
            raise VerifyError(
                f"exact_values[{i}] normalizes to an empty string — it would "
                "match everything or nothing; refusing to scan"
            )
        rules.secrets.append(Secret(claim(f"exact_values[{i}]"), normalized))

    raw_patterns = raw.get("patterns") or []
    if not isinstance(raw_patterns, list):
        raise VerifyError("patterns must be a list")
    for i, spec in enumerate(raw_patterns):
        if not isinstance(spec, str):
            raise VerifyError(
                f"patterns[{i}] must be a string, got {type(spec).__name__}"
            )
        try:
            regex = re.compile(spec, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            raise VerifyError(f"patterns[{i}]: invalid regex: {exc}")
        if regex.match(""):
            raise VerifyError(
                f"patterns[{i}]: matches the empty string; refusing to scan"
            )
        rules.patterns.append(PatternRule(claim(f"patterns[{i}]"), regex))

    entity_types = raw.get("entity_types") or []
    if not isinstance(entity_types, list):
        raise VerifyError("entity_types must be a list")
    for entity in entity_types:
        key = str(entity)
        mapped = ENTITY_TYPE_TO_CLASS.get(key)
        if mapped is None:
            rules.unverifiable.append(key)
            continue
        regex_src, validator = BUILTIN_PATTERN_CLASSES[mapped]
        rules.patterns.append(
            PatternRule(claim(f"entity_types:{key}"), re.compile(regex_src), validator)
        )

    # A config of only unverifiable entity types is allowed: it scans
    # nothing, warns loudly, and exits 2 — which is the honest answer.
    if not (rules.secrets or rules.patterns or rules.unverifiable):
        raise VerifyError("the rules file contains no rules")
    return rules


def _load_rules_json(rules_path: Path) -> RuleSet:
    """Parse the rules JSON file, validating its shape.

    Each entry carries a 'name' and exactly one of:
      'value'   — a known secret string, matched via normalization;
      'pattern' — a custom regex matched against raw extracted text;
      'class'   — a built-in pattern class (see BUILTIN_PATTERN_CLASSES).

    Raises VerifyError for every operational problem so main can exit 2.
    """
    try:
        payload: Any = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"Cannot read rules file {rules_path}: {exc}")

    if not isinstance(payload, list):
        raise VerifyError("the rules file must be a JSON array of objects")

    secrets: list[Secret] = []
    patterns: list[PatternRule] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict) or "name" not in entry:
            raise VerifyError(f"rules entry {i} must be an object with 'name'")
        name = str(entry["name"])
        # Names are rule identity throughout the pipeline; a duplicate
        # would silently overwrite another rule's hits in the report.
        if name in seen_names:
            raise VerifyError(f"rules entry {i}: duplicate rule name {name!r}")
        seen_names.add(name)
        kind_keys = [k for k in ("value", "pattern", "class") if k in entry]
        if len(kind_keys) != 1:
            raise VerifyError(
                f"rules entry {i} ({name!r}) must have exactly one of "
                f"'value', 'pattern', or 'class' (found: {kind_keys or 'none'})"
            )
        kind = kind_keys[0]
        spec = entry[kind]
        if not isinstance(spec, str):
            raise VerifyError(
                f"rules entry {i} ({name!r}): {kind!r} must be a string, "
                f"got {type(spec).__name__}"
            )

        if kind == "value":
            normalized = normalize_string(spec)
            if not normalized:
                raise VerifyError(
                    f"secret {name!r} normalizes to an empty string — it "
                    "would match everything or nothing; refusing to scan"
                )
            secrets.append(Secret(name, normalized))
        elif kind == "pattern":
            try:
                # MULTILINE so grep-style ^/$ anchors match per line of
                # the extracted page text instead of silently never
                # matching mid-page.
                regex = re.compile(spec, re.MULTILINE)
            except re.error as exc:
                raise VerifyError(f"rule {name!r}: invalid regex: {exc}")
            if regex.match(""):
                raise VerifyError(
                    f"rule {name!r}: pattern matches the empty string; "
                    "refusing to scan"
                )
            patterns.append(PatternRule(name, regex))
        else:
            if spec not in BUILTIN_PATTERN_CLASSES:
                raise VerifyError(
                    f"rule {name!r}: unknown class {spec!r} — valid classes: "
                    f"{', '.join(sorted(BUILTIN_PATTERN_CLASSES))}"
                )
            regex_src, validator = BUILTIN_PATTERN_CLASSES[spec]
            patterns.append(PatternRule(name, re.compile(regex_src), validator))

    if not secrets and not patterns:
        raise VerifyError("the rules file contains no rules")
    return RuleSet(secrets=secrets, patterns=patterns)


def _sanitize_report_text(text: str) -> str:
    """Strip control characters so PDF-derived bytes (ANSI escapes,
    newlines) cannot inject into or spoof the terminal report."""
    return "".join(ch if ch.isprintable() or ch == " " else "�" for ch in text)


def print_report(report: ScanReport, pdf_path: Path) -> None:
    """Render the final verdict banner and per-finding detail.

    This is the single choke point where pattern samples are masked and
    all document-derived text is sanitized.
    """
    bar = "=" * 70
    print(f"\n{bar}")
    if report.leaked:
        print(f"  [FAIL]  SENSITIVE DATA DETECTED IN: {pdf_path.name}")
        print(bar)
        for f in report.findings:
            line = f"  ✖ LAYER: {f.layer:<8} | RULE: {f.secret_name!r:<24} | {f.location}"
            if f.sample:
                line += f" — sample {mask(f.sample)}"
            print(_sanitize_report_text(line))
    else:
        print(f"  [PASS]  No target secrets detected in: {pdf_path.name}")
    print(bar)

    if report.degraded:
        print("\n  ⚠ ATTENTION — warnings were raised during the scan:")
        for w in report.warnings:
            print(_sanitize_report_text(f"    - {w}"))
        if not report.leaked:
            print("\n  A [PASS] with warnings is NOT a certified clean result "
                  "(exit code 2): resolve the warnings above.")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    # The report must never crash on a non-UTF-8 stdout (that would turn a
    # degraded PASS into a bogus exit 1).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):  # pragma: no cover
            pass

    parser = argparse.ArgumentParser(
        # prog is left to argparse so --help names however it was
        # invoked: "verify.py" as a script, "pdf-verify" as the
        # installed console script.
        description="Forensic PDF verification: detect secrets across DOM, "
        "OCR, metadata, and binary-stream layers.",
    )
    parser.add_argument("--target", required=True, type=Path, help="PDF file to verify")
    parser.add_argument(
        "--secrets", required=True, type=Path,
        help="JSON array of rules: known values, custom regex patterns, "
        "and/or built-in pattern classes "
        f"({', '.join(sorted(BUILTIN_PATTERN_CLASSES))})",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop scanning as soon as any secret is found (default: scan "
        "every layer for a complete forensic report)",
    )
    args = parser.parse_args(argv)

    pdf_path: Path = args.target
    if not pdf_path.is_file():
        print(f"[ERROR] Target PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    try:
        rules = load_rules(args.secrets)
    except VerifyError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    secrets, patterns = rules.secrets, rules.patterns

    matcher = SecretMatcher(secrets)
    report = ScanReport()

    # Scope, not coverage: a shared redaction config can ask for entity
    # types this tool has no way to search for. Say so — a PASS that
    # silently skipped a category would be the false assurance the whole
    # exit-code contract exists to prevent.
    if rules.unverifiable:
        report.warnings.append(
            "Scope: the config asks a redactor to remove "
            f"{', '.join(sorted(set(rules.unverifiable)))} — these are "
            "identified by LLM judgement and have no regex equivalent, so "
            "this tool CANNOT verify they were removed"
        )

    # Start the independent subprocess layers now; they run concurrently
    # behind the in-process DOM/OCR scans.
    procs = start_hidden_tools(pdf_path, report)

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        kill_hidden_tools(procs)
        print(f"[ERROR] Cannot open PDF {pdf_path}: {exc}", file=sys.stderr)
        return 2

    try:
        if doc.needs_pass:
            kill_hidden_tools(procs)
            print(f"[ERROR] PDF is password-protected: {pdf_path}", file=sys.stderr)
            return 2

        if doc.page_count == 0:
            report.warnings.append(
                "PDF contains zero pages — DOM/OCR content layers cannot "
                "scan an empty document"
            )

        print(f"[*] Scanning {pdf_path.name} ({doc.page_count} page(s)) "
              f"for {len(secrets)} secret(s) and {len(patterns)} pattern rule(s)...")

        scan_xmp_metadata(doc, matcher, patterns, report)

        print("[*] Phase 2: DOM layer (layout-aware visual text)...")
        try:
            scan_page_layer(
                doc, matcher, report,
                layer="DOM", extractor=extract_visual_text, note="visual text layer",
                patterns=patterns, fail_fast=args.fail_fast,
            )
        except Exception as exc:
            report.warnings.append(f"DOM: layer crashed ({exc}) — NOT fully scanned")

        if args.fail_fast and report.leaked:
            report.warnings.append("OCR: skipped (--fail-fast after earlier finding)")
        elif not _OCR_IMPORTS_OK:
            report.warnings.append(
                "OCR: PyObjC Vision bridge not available — visual layer NOT scanned "
                "(uv pip install pyobjc-framework-Vision pyobjc-framework-Quartz; macOS only)"
            )
        else:
            print("[*] Phase 3: OCR layer (Apple Vision, correction on+off)...")
            try:
                scan_page_layer(
                    doc, matcher, report,
                    layer="OCR", extractor=extract_ocr_text,
                    note=f"Apple Vision @ {OCR_DPI} dpi",
                    patterns=patterns,
                    # Both Vision passes (language correction on and off)
                    # are independent natural-order reads of the same
                    # pixels, not reconstructions — correction-off is in
                    # fact the more reliable read for digit strings — so
                    # both are eligible for hard findings.
                    hard_variants=2,
                    fail_fast=args.fail_fast,
                )
            except Exception as exc:
                report.warnings.append(f"OCR: layer crashed ({exc}) — NOT fully scanned")
    finally:
        doc.close()

    if args.fail_fast and report.leaked:
        kill_hidden_tools(procs)
        report.warnings.append(
            "Metadata/Binary: skipped (--fail-fast after earlier finding)"
        )
    else:
        print("[*] Phase 4: Metadata + binary-stream layers (exiftool / qpdf)...")
        try:
            check_hidden_layers(procs, matcher, patterns, report)
        except Exception as exc:
            kill_hidden_tools(procs)
            report.warnings.append(
                f"Metadata/Binary: collection crashed ({exc}) — NOT fully scanned"
            )

    print_report(report, pdf_path)

    if report.leaked:
        return 1
    if report.degraded:
        return 2  # clean-so-far, but not certifiable as a real PASS
    return 0


def run_cli(argv: Sequence[str] | None = None) -> NoReturn:
    """Process entry point: compute the verdict, then leave immediately.

    **This kills the interpreter and never returns.** Call it only as a
    process entry point; in-process callers (tests, embedding code) want
    main(), which returns the exit code normally.

    By the time main() returns, the verdict is decided and the report is
    written — the only thing left is for the process to carry the exit
    code out. Interpreter shutdown is a surprisingly risky place to do
    that here: Apple Vision's native teardown can SIGKILL the process on
    virtualized macOS (observed in CI as a complete report on stdout
    followed by -9), which would hand a release gate a crash instead of
    the "cannot certify" answer the tool had already reached.

    So flush explicitly and call os._exit, which skips atexit handlers,
    garbage collection, and native teardown. That trade is only safe
    because nothing here needs cleanup: findings are already printed and
    the PDF and subprocesses are closed by the time main() returns.
    """
    code = main(argv)
    # os._exit skips buffer flushing, and stdout is block-buffered when
    # piped — which is exactly how CI and shell pipelines run this.
    # AttributeError matters as much as the I/O errors: sys.stdout can be
    # None or a minimal substitute with no flush(), and if that escaped
    # here we would skip os._exit and land back in the teardown this
    # function exists to avoid. main() guards its own stream loop the
    # same way.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass
    os._exit(code)


if __name__ == "__main__":
    run_cli()
