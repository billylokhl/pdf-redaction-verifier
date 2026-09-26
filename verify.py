#!/usr/bin/env python3
"""
verify.py — Forensic PDF Redaction Verification Suite.

Detects sensitive strings (secrets) inside a PDF across six independent
layers:

  1. Text     — layout-aware text extraction in horizontal AND vertical
                reading order, defeats out-of-order draw commands,
                per-character form boxes, and rotated-matrix text.
  2. OCR      — rasterize + Apple Vision (native, hardware-accelerated),
                run with language correction both on and off, defeats
                vector/outlined/image text.
  3. Metadata — exiftool sweep of XMP/Info/embedded metadata (filesystem-
                derived fields excluded so the local path cannot match).
  4. Objects  — every PDF object walked structurally via PyMuPDF: string
                literals in dictionaries and text stream bodies decoded
                for high-confidence findings, with image/font/binary
                bodies excluded by key and by content so their bytes are
                never parsed as text. A finding names its carrier and
                whether the document still references it (ORPHANED);
                objects an incremental update rewrote are read from each
                earlier revision. Leftover content it finds but cannot
                read (font-coded text, images, containers) is flagged
                for manual review rather than passed silently.
  5. Binary   — qpdf QDF rewrite of the reachable objects, streamed with
                bounded memory: a second parser's view (damaged-xref
                recovery, decompressed attachments). Orphaned objects and
                earlier incremental revisions are not in its output.
                Value-secret matches only, always manual-review warnings
                (they can be numeric-operand or binary-data collisions).
  6. Hidden   — attachments, annotations, form-field values, link
                targets, JavaScript and optional-content group names via
                PyMuPDF: content no page renders, needing no external
                binary, and naming the carrier — the qpdf sweep sees an
                attachment only as anonymous, manual-review bytes.

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
    python verify.py --target document.pdf --secrets redact_config.yaml
    python verify.py --target document.pdf --secrets secrets.json --json out.json

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
import datetime
import functools
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib
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
# The verdict semantics are versioned with the tool: any change that can
# move a file from 0 to 1/2, or from 1 to 2, bumps at least the minor.
__version__ = "0.1.0"
# Bumped when a field of the --json report is renamed, removed or changes
# meaning; adding a field does not bump it.
JSON_SCHEMA_VERSION: int = 1
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
# Image codecs PyMuPDF does not decode to text. Feeding their bytes to a
# text parser is the category error the structural pass exists to avoid:
# roughly 1 byte in 256 of a JPEG is "(", which stalls a literal scanner
# exactly as a real unclosed string would.
_BINARY_STREAM_FILTERS: frozenset[str] = frozenset({
    "/DCTDecode", "/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode",
})
# Payloads that are program data, not text. A font program's tables are
# full of bytes that tokenize as string literals and normalize into
# digit runs, which produced hard findings on clean documents; embedded
# files are arbitrary binaries the Hidden layer already scans (as
# manual-review warnings, which is the honest tier for them).
_OPAQUE_STREAM_SUBTYPES: frozenset[str] = frozenset({
    "/Image", "/Type1C", "/CIDFontType0C", "/OpenType",
})
# Pattern scanning: matches may span feed boundaries up to the overlap;
# feeds are batched before regex sweeps (per-tiny-literal sweeps measure
# ~100x slower than batched ones).
PATTERN_SCAN_OVERLAP: int = 512
PATTERN_SCAN_BATCH: int = 64 << 10

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# PDF string objects in QDF output: (literal with \-escapes) or <hex>.
# Hex strings only. Literal strings nest, so they are tokenized by
# _iter_pdf_strings rather than by a regex.
_PDF_HEX_STRING_RE = re.compile(r"<[0-9A-Fa-f\s]+>", re.S)
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

    layer: str          # Text | OCR | Metadata | Objects | Binary | Hidden
    secret_name: str
    location: str
    sample: str = field(default="", compare=False)
    # Where the matched content is stored (STORAGE_CLASSES) and, for the
    # Objects layer, which object and earlier revision; for page layers,
    # the page when the match is on one page. Informational: dedup keys
    # on (layer, rule, location) alone, which already encodes them.
    storage: str = field(default="live", compare=False)
    object: int | None = field(default=None, compare=False)
    revision: int | None = field(default=None, compare=False)
    page: int | None = field(default=None, compare=False)


# Every warning carries a stable code, so machine consumers (the --json
# report, the evaluation harness) never depend on message wording. The
# kind says why it blocks certification:
#   review   — a possible match a human must judge
#   coverage — content that was not (fully) scanned or read
#   scope    — the rules cannot express or verify what was asked
WARNING_CODES: dict[str, str] = {
    # review
    "REVIEW_CROSS_LINE": "review",
    "REVIEW_CROSS_PAGE": "review",
    "REVIEW_FUSED_PATTERN": "review",
    "REVIEW_HIDDEN_TEXT": "review",
    "REVIEW_METADATA_RAW": "review",
    "REVIEW_OBJECT_TEXT": "review",
    "REVIEW_ADJACENT_LITERALS": "review",
    "REVIEW_BINARY": "review",
    # coverage
    "PAGE_FAILED": "coverage",
    "LAYER_CRASHED": "coverage",
    "OCR_UNAVAILABLE": "coverage",
    "SKIPPED_FAIL_FAST": "coverage",
    "EMPTY_DOCUMENT": "coverage",
    "TOOL_MISSING": "coverage",
    "TOOL_START_FAILED": "coverage",
    "TOOL_TIMEOUT": "coverage",
    "TOOL_EXIT_NONZERO": "coverage",
    "TOOL_NO_OUTPUT": "coverage",
    "TOOL_OUTPUT_MALFORMED": "coverage",
    "XMP_UNREADABLE": "coverage",
    "XREF_UNREADABLE": "coverage",
    "OBJECT_UNREADABLE": "coverage",
    "UNTERMINATED_STRING": "coverage",
    "LEFTOVER_UNDECODABLE_TEXT": "coverage",
    "LEFTOVER_IMAGE": "coverage",
    "LEFTOVER_CONTAINER": "coverage",
    "PAYLOAD_TRUNCATED": "coverage",
    "REVISION_SCAN_FAILED": "coverage",
    "REVISION_UNREADABLE": "coverage",
    "REVISION_CAP": "coverage",
    "HIDDEN_ITEM_FAILED": "coverage",
    "ATTACHMENT_TOO_LARGE": "coverage",
    "ATTACHMENT_EMPTY": "coverage",
    "ATTACHMENT_NOT_TEXT": "coverage",
    # scope
    "RULES_UNKNOWN_KEY": "scope",
    "RULES_UNQUOTED_VALUE": "scope",
    "RULES_TRUNCATED_PATTERN": "scope",
    "RULES_CAPTURING_GROUP": "scope",
    "SCOPE_PARTIAL_ENTITY": "scope",
    "SCOPE_UNVERIFIABLE": "scope",
}


# Where the content a finding or warning is about is stored:
#   live         — content the current document uses
#   orphaned     — an object nothing references (reachability trusted)
#   unreferenced — not reached by a reachability walk that is not trusted
#   superseded   — an earlier revision's version of a rewritten object
STORAGE_CLASSES: frozenset[str] = frozenset({"live", "orphaned", "unreferenced", "superseded"})
# How the text a review match was found in was assembled — a match that
# needs joining, or comes from an arbitrary run of text, may be a
# coincidence (see the two-tier model in DESIGN.md).
ADJACENCY: frozenset[str] = frozenset(
    {"JOINED_LINES", "JOINED_PAGES", "JOINED_LITERALS", "NOISY_SOURCE"}
)
LAYERS: frozenset[str] = frozenset(
    {
        "Text",
        "OCR",
        "Metadata",
        "Objects",
        "Binary",
        "Hidden",
        "Metadata/Binary",
        "Rules",
        "Document",
    }
)
# Structured fields a warning may carry, beyond code, layer and message.
WARNING_FIELDS: tuple[str, ...] = (
    "storage",
    "rule",
    "adjacency",
    "tool",
    "page",
    "object",
    "revision",
    # The tool's own exit status on TOOL_EXIT_NONZERO, separated out from
    # the message text so a consumer can tell a real qpdf failure from its
    # benign (version-dependent) exit 3 "succeeded with warnings" without
    # parsing prose (see #3).
    "returncode",
)


class Warn(str):
    """A warning message with a stable code, the layer that raised it, and
    optional structured fields (see WARNING_FIELDS).

    A str subclass, so the human report and every existing comparison
    treat it as the message; the rest rides along for the JSON report.
    An unregistered code or field raises at construction, which a layer
    turns into a crash warning — never a silent pass.
    """

    code: str
    layer: str
    fields: dict[str, Any]

    def __new__(cls, code: str, layer: str, message: str, **fields: Any) -> "Warn":
        if code not in WARNING_CODES:
            raise ValueError(f"unregistered warning code {code!r}")
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}")
        unknown = set(fields) - set(WARNING_FIELDS)
        if unknown:
            raise ValueError(f"unknown warning field(s) {sorted(unknown)}")
        if fields.get("storage") not in STORAGE_CLASSES | {None}:
            raise ValueError(f"unknown storage class {fields['storage']!r}")
        if fields.get("adjacency") not in ADJACENCY | {None}:
            raise ValueError(f"unknown adjacency {fields['adjacency']!r}")
        self = super().__new__(cls, message)
        self.code = code
        self.layer = layer
        self.fields = fields
        return self

    @property
    def kind(self) -> str:
        return WARNING_CODES[self.code]


class WarnList(list):
    """A list that accepts only Warn items, so a warning cannot reach the
    report without a code — however it is added. Rejecting raises, which
    a layer turns into a crash warning: exit 2, never a silent pass."""

    @staticmethod
    def _check(items: Any) -> list[Warn]:
        items = list(items)
        for item in items:
            if not isinstance(item, Warn):
                raise TypeError(f"warnings must be Warn, not {type(item).__name__}")
        return items

    def append(self, item: Any) -> None:
        super().append(*self._check([item]))

    def extend(self, items: Any) -> None:
        super().extend(self._check(items))

    def insert(self, index: Any, item: Any) -> None:
        super().insert(index, *self._check([item]))

    def __iadd__(self, items: Any) -> "WarnList":
        super().extend(self._check(items))
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            super().__setitem__(index, self._check(value))
        else:
            super().__setitem__(index, *self._check([value]))


@dataclass
class ScanReport:
    """Aggregated results across all layers. Findings are unique."""

    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=WarnList)
    _seen: set[Finding] = field(default_factory=set, repr=False)

    def warn(self, code: str, layer: str, message: str, **fields: Any) -> None:
        self.warnings.append(Warn(code, layer, message, **fields))

    def record(
        self, layer: str, secret_name: str, location: str, sample: str = "", **origin: Any
    ) -> None:
        finding = Finding(layer, secret_name, location, sample, **origin)
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

    def crossing(self, left: str, right: str) -> list[str]:
        """Names of secrets with an occurrence that straddles the join of
        *left* and *right* (both normalized).

        Decided by position, not by comparing which names occur on each
        side: a secret wholly inside one side must not mask a *separate*
        occurrence that crosses the join.
        """
        joined, cut = left + right, len(left)
        names: set[str] = set()
        for secret in self._secrets:
            needle = secret.normalized
            if len(needle) < 2:
                continue            # a 1-character value cannot straddle
            start = joined.find(needle, max(0, cut - len(needle) + 1))
            if 0 <= start < cut:
                names.add(secret.name)
        return sorted(names)

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
# U+00AD (soft hyphen) is included because fonts embedded by some producers
# (PyMuPDF with Arial, for one) extract an ordinary '-' as U+00AD, which
# NFKC leaves alone — so "123-45-6789" read back as "123\xad45\xad6789".
_PATTERN_FOLD_TABLE = {
    **{cp: "-" for cp in (0x00AD, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014,
                          0x2015, 0x2043, 0x2212)},
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
# PHASE 2: Layout-Aware Text Extraction (Text layer)
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


# How many of extract_visual_text's readings are genuine reading orders
# (hard-finding eligible); the rest are reconstructions (manual review).
TEXT_GENUINE_READINGS = 4


def extract_visual_text(page: fitz.Page) -> list[str]:
    """Reconstruct page text in visual reading orders.

    Returns six readings, or [] for a page with no glyphs:

    0. horizontal — visible, horizontally written text, lines top to bottom;
    1-2. rotated, top-to-bottom and bottom-to-top — visible text written
       vertically (a rotated matrix), read along its own direction;
    3. off-page — text placed outside the visible area (crop or media box),
       read horizontally on its own;
    4-5. every visible glyph read in vertical columns, top-to-bottom and
       bottom-to-top.

    Readings 0-3 are genuine: each reads text the way it was written.
    Off-page text is kept apart from the visible readings: merged into
    them, a slug or Bates stamp below the page became the page's "last
    line" and hid a value split across the page break. Readings 4-5 are
    reconstructions: a column of an ordinary horizontal page stacks one
    glyph from each of many lines (a ledger's last digits, a numbered
    list's numbers), so they can assemble a value by accident — yet they
    are also the only reading of a value written one character per line.
    Callers treat them as manual-review only.
    """
    Glyph = tuple[float, float, float, float, str]
    flat: list[Glyph] = []
    rotated: list[Glyph] = []
    off_page: list[Glyph] = []
    # The visible area in the (unrotated) coordinates glyphs are reported
    # in, computed once per page: a per-glyph Point * Matrix doubled the
    # Text layer's run time.
    vx0, vy0, vx1, vy1 = page.rect * page.derotation_matrix
    # No clipping to the page: text outside the visible area is invisible
    # to a reader but still in the file, and still a leak.
    raw: dict[str, Any] = page.get_text(
        "rawdict", clip=fitz.INFINITE_RECT(),
        flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_MEDIABOX_CLIP)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            # dir is the writing direction: (1, 0) for ordinary text,
            # (0, -1) / (0, 1) for text rotated 90 / 270 degrees.
            written_vertically = abs(line.get("dir", (1.0, 0.0))[1]) > 0.1
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    c: str = char.get("c", "")
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = char["bbox"]
                    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                    glyph = (cx, cy, x1 - x0, y1 - y0, c)
                    if not (vx0 <= cx <= vx1 and vy0 <= cy <= vy1):
                        off_page.append(glyph)
                    elif written_vertically:
                        rotated.append(glyph)
                    else:
                        flat.append(glyph)

    if not (flat or rotated or off_page):
        return []

    def columns(glyphs: list[Glyph]) -> tuple[str, str]:
        if not glyphs:
            return "", ""
        down = _reconstruct(glyphs, cluster_axis=0)
        return down, "\n".join(line[::-1] for line in down.split("\n"))

    horizontal = _reconstruct(flat, cluster_axis=1) if flat else ""
    rotated_down, rotated_up = columns(rotated)
    outside = _reconstruct(off_page, cluster_axis=1) if off_page else ""
    all_down, all_up = columns(flat + rotated)
    return [horizontal, rotated_down, rotated_up, outside, all_down, all_up]


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
# Shared per-page layer driver (Text and OCR)
# ──────────────────────────────────────────────────────────────────────────
# Report wording for the page-by-page layers, shared with the tests.
CROSS_PAGE_LOCATION = "across page boundaries, pages {prev}–{page} ({note})"
CROSS_LINE_WARNING = (
    "{layer}: page {page} contains a cross-line sequence matching secret "
    "{name!r} — possibly a coincidental concatenation of adjacent tokens; "
    "manual review recommended"
)
CROSS_PAGE_WARNING = (
    "{layer}: page {page}: a sequence matching secret {name!r} appears only "
    "when text is joined across the page break before it — possibly a "
    "coincidental concatenation; manual review recommended"
)


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

    The extractor returns several readings per page. The first
    *hard_variants* are genuine reading orders; any others are
    reconstructions. Callers set it to the number of genuine readings:
    TEXT_GENUINE_READINGS for Text, all of them for OCR.

    Known values:
    - hard: on one line of a genuine reading; or running from the last
      line of a page onto the first line of the next page, in a genuine
      reading;
    - manual review: on one line of a reconstructed reading only, when a
      page's lines are joined, or crossing a page break any other way
      (other lines, blank or unreadable pages in between).
    Each page break is checked on its own, so every place a value crosses
    one is reported where it happens.

    Pattern rules match per visual line of the genuine readings for hard
    findings, aggregated per rule across pages. Matches that appear only
    in fused multi-line text, in reconstructed readings, or across page
    boundaries are manual-review warnings.

    When *fail_fast* is True the scan stops after the first page that
    produces a finding, so the tool exits quickly on large documents.
    """
    keep = max(matcher.max_len - 1, 0)   # enough of one side to straddle a join
    # Per reading: the last `keep` characters read so far (the tail a match
    # crossing into the next page must start in), and — genuine readings
    # only — the page number and end of the last line, for the seam.
    tails: dict[int, str] = {}
    last_line: dict[int, tuple[int, str]] = {}
    cross_page_patterns = PatternScanner(patterns, collapse_separators=True)
    layer_hard: dict[str, tuple[list[int], str]] = {}
    layer_soft: dict[str, tuple[list[int], str]] = {}
    for page_index in range(doc.page_count):
        try:
            page = doc.load_page(page_index)
            variants = extractor(page)
        except Exception as exc:  # a corrupt page must not abort the scan
            report.warn(
                "PAGE_FAILED",
                layer,
                f"{layer}: page {page_index + 1} failed ({exc})",
                page=page_index + 1,
            )
            continue
        page_no = page_index + 1
        norm_lines = [
            [ln for ln in map(normalize_string, text.split("\n")) if ln]
            for text in variants
        ]

        # Hard: one line of a genuine reading.
        hard_here: set[str] = set()
        for lines in norm_lines[:hard_variants]:
            for line in lines:
                for secret in matcher.search(line):
                    hard_here.add(secret.name)
                    report.record(layer, secret.name, f"page {page_no} ({note})", page=page_no)

        # Hard: the page seam — a value running from the previous page's
        # last line onto this page's first line, in a genuine reading. A
        # page that failed or held no text never updates last_line, so it
        # breaks the adjacency: what lies between the halves is unseen.
        seam_here: set[str] = set()
        for vi, lines in enumerate(norm_lines[:hard_variants]):
            prev = last_line.get(vi)
            if lines and prev and prev[0] == page_no - 1:
                for name in matcher.crossing(prev[1], lines[0]):
                    seam_here.add(name)
                    report.record(layer, name, CROSS_PAGE_LOCATION.format(
                        prev=page_no - 1, page=page_no, note=note))

        # Manual review, once per secret per page: a reconstructed reading's
        # single line, or any reading's lines joined.
        page_warned: set[str] = set()
        for vi, lines in enumerate(norm_lines):
            candidates = [s for line in lines for s in matcher.search(line)] \
                if vi >= hard_variants else []
            candidates += matcher.search("".join(lines))
            for secret in candidates:
                if secret.name in hard_here or secret.name in page_warned:
                    continue
                page_warned.add(secret.name)
                report.warn(
                    "REVIEW_CROSS_LINE",
                    layer,
                    CROSS_LINE_WARNING.format(layer=layer, page=page_no, name=secret.name),
                    storage="live",
                    page=page_no,
                    rule=secret.name,
                    adjacency="JOINED_LINES",
                )

        # Manual review: anything else crossing into this page from the text
        # before it, in any reading — the net that keeps every page-break
        # split, however joined, from being silent.
        crossing_warned: set[str] = set()
        for vi, lines in enumerate(norm_lines):
            full = "".join(lines)
            tail = tails.get(vi, "")
            if keep and tail and full:
                for name in matcher.crossing(tail, full[:keep]):
                    if name in seam_here or name in crossing_warned:
                        continue
                    crossing_warned.add(name)
                    report.warn(
                        "REVIEW_CROSS_PAGE",
                        layer,
                        CROSS_PAGE_WARNING.format(layer=layer, page=page_no, name=name),
                        storage="live",
                        page=page_no,
                        rule=name,
                        adjacency="JOINED_PAGES",
                    )
            if keep and full:
                tails[vi] = (tail + full)[-keep:]
            if vi < hard_variants and keep and lines:
                last_line[vi] = (page_no, lines[-1][-keep:])

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

    # Aggregate pattern results: one finding (or warning) per rule.
    def _pages_label(pages: list[int]) -> str:
        shown = ", ".join(str(p) for p in pages[:3])
        extra = f" (+{len(pages) - 3} more)" if len(pages) > 3 else ""
        return f"page{'s' if len(pages) > 1 else ''} {shown}{extra}"

    for name, (pages, sample) in layer_hard.items():
        report.record(
            layer,
            name,
            f"{_pages_label(pages)} ({note})",
            sample,
            page=pages[0] if len(pages) == 1 else None,
        )
    cross_page_patterns.flush()
    for name, sample in cross_page_patterns.hits.items():
        if name not in layer_hard and name not in layer_soft:
            layer_soft[name] = ([], sample)
    for name, (pages, sample) in layer_soft.items():
        if name in layer_hard:
            continue
        where = _pages_label(pages) if pages else "across page boundaries"
        report.warn(
            "REVIEW_FUSED_PATTERN",
            layer,
            f"{layer}: {where}: sequence matching pattern rule {name!r} appears "
            f"only when lines, columns or pages are fused (sample {mask(sample)})"
            " — possibly coincidental concatenation; manual review recommended",
            storage="live",
            rule=name,
            adjacency="JOINED_LINES" if pages else "JOINED_PAGES",
        )


# ──────────────────────────────────────────────────────────────────────────
# PHASE 4: Metadata & Stream Scraping (exiftool / qpdf, run concurrently)
# ──────────────────────────────────────────────────────────────────────────
def _minimal_subprocess_env() -> dict[str, str]:
    """A stripped environment for exiftool/qpdf.

    Only PATH (so a resolved absolute executable can still find its own
    helpers/shared libraries) and a C locale (so the tools' own output —
    dates, decimal separators — can't drift with the invoking user's
    locale) cross over. Nothing else: an inherited variable that happens
    to steer a tool's own behaviour (a config-file search path, a proxy)
    must not reach it just because it was set in this process's shell.
    """
    env = {"LANG": "C", "LC_ALL": "C"}
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path
    return env


def _start_tool(
    report: ScanReport, layer: str, name: str, argv: Sequence[str],
    env: dict[str, str], cwd: str,
) -> subprocess.Popen[bytes] | None:
    """Launch an external tool; a start failure degrades the layer loudly.

    *name* is the tool's logical name ("exiftool", "qpdf"), reported as-is
    regardless of which absolute path it resolved to, so TOOL_MISSING/
    TOOL_START_FAILED stay stable for consumers.
    """
    try:
        return subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=cwd,
        )
    except FileNotFoundError:
        report.warn(
            "TOOL_MISSING",
            layer,
            f"{layer}: {name} not installed — layer NOT scanned",
            tool=name,
        )
    except OSError as exc:
        report.warn(
            "TOOL_START_FAILED",
            layer,
            f"{layer}: {name} failed to start ({exc}) — layer NOT scanned",
            tool=name,
        )
    return None


def start_hidden_tools(
    pdf_path: Path, report: ScanReport
) -> tuple[dict[str, subprocess.Popen[bytes] | None], tempfile.TemporaryDirectory[str]]:
    """Kick off exiftool and qpdf now so they run behind the Text/OCR scans.

    Hardened against a hostile target/environment (see docs/REDESIGN.md
    "0d. Hygiene"):
    - each tool is resolved to an absolute path via PATH once, with
      shutil.which, rather than letting exec() search PATH itself;
    - the target is always passed as its absolute, resolved path — never
      a bare filename — so a file literally named "-something.pdf" can
      never be read as an option by either tool (a resolved path is
      always rooted at "/", so it can never itself start with "-");
    - exiftool additionally gets '-config ""' first — this is what
      actually stops it from consulting ANY per-user config file at all
      (EXIFTOOL_HOME, HOME, HOMEDRIVE+HOMEPATH, or its own working-
      directory fallback once none of those is set — see below), verified
      directly against the real binary in tests/test_cli.py — plus its
      own documented '--' end-of-options convention;
    - both ALSO run inside a private, empty scratch directory rather than
      this process's own current directory, as DEFENCE IN DEPTH rather
      than the fix itself: exiftool's *.ExifTool_config* search falls
      back to its WORKING DIRECTORY once none of EXIFTOOL_HOME/HOME/
      HOMEDRIVE+HOMEPATH is set, so if '-config ""' were ever dropped (or
      a future exiftool version behaved differently), the directory it
      would fall back to must still not be the caller's own;
    - both run with a minimal environment (_minimal_subprocess_env), so
      no other inherited variable can steer them either.

    A missing tool still produces exactly the TOOL_MISSING warning it did
    before this hardening. Returns the two Popen slots plus the
    TemporaryDirectory they were launched in — the caller owns its
    lifetime and must call .cleanup() once both are collected or killed.
    """
    target = str(pdf_path.resolve())
    env = _minimal_subprocess_env()
    scratch = tempfile.TemporaryDirectory(prefix="pdf-redaction-verifier-")

    def launch(name: str, layer: str, args: Sequence[str]) -> subprocess.Popen[bytes] | None:
        exe = shutil.which(name)
        if exe is None:
            report.warn(
                "TOOL_MISSING",
                layer,
                f"{layer}: {name} not installed — layer NOT scanned",
                tool=name,
            )
            return None
        return _start_tool(report, layer, name, [exe, *args], env, scratch.name)

    procs = {
        "exiftool": launch("exiftool", "Metadata", ["-config", "", "-json", "--", target]),
        "qpdf": launch(
            "qpdf", "Binary",
            ["--qdf", "--object-streams=disable", target, "-"],
        ),
    }
    return procs, scratch


def kill_hidden_tools(procs: dict[str, subprocess.Popen[bytes] | None]) -> None:
    """Kill whatever of the two tools is still running. Idempotent: safe to
    call more than once, and safe to call when neither ever started."""
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
        report.warn("XMP_UNREADABLE", "Metadata", f"Metadata: XMP packet unreadable ({exc})")
        return
    if not xmp:
        return
    for secret in matcher.search(normalize_string(xmp)):
        report.record("Metadata", secret.name, "XMP packet (in-document)")
    for name, sample in match_patterns(xmp, patterns).items():
        report.record("Metadata", name, "XMP packet (in-document)", sample)


# One attachment must not be able to exhaust memory: a small PDF can
# declare gigabytes of highly-compressible payload. Checked against the
# UNCOMPRESSED size before reading, since reading is what allocates.
MAX_ATTACHMENT_BYTES: int = 16 << 20


@dataclass(frozen=True)
class HiddenItem:
    """One piece of non-page content, with where it came from.

    *location* identifies the carrier by index or xref and never embeds
    document text — a filename can itself be the secret, and locations
    are printed unmasked.

    *hard* is False for content that is a run of arbitrary text rather
    than a discrete field value: an attachment body has lines that fuse
    across newlines exactly like page text, and a binary attachment
    decoded as text is mojibake that can synthesize matches. Those get
    the manual-review tier the rest of the tool uses for fusion-prone
    surfaces.
    """

    location: str
    text: str
    hard: bool = True


def _js_sources(doc: fitz.Document) -> Iterator[HiddenItem]:
    """Yield JavaScript bodies by walking the action graph.

    Scanning object *source* for the substring "/JS" is wrong in both
    directions: it feeds whole object dictionaries to the matchers (a
    widget's /Rect fuses into a Luhn-valid digit run), and it misses the
    common case where a producer stores the script as a stream, because
    xref_object returns only the dictionary.
    """
    def script_text(xref: int, key: str) -> str | None:
        try:
            kind, value = doc.xref_get_key(xref, key)
        except Exception:
            return None
        if kind == "string":
            return value
        if kind == "xref":
            try:
                target = int(str(value).split()[0])
                if doc.xref_is_stream(target):
                    return doc.xref_stream(target).decode("utf-8", errors="replace")
                kind2, value2 = doc.xref_get_key(target, "JS")
                return value2 if kind2 == "string" else None
            except Exception:
                return None
        return None

    def walk(xref: int) -> Iterator[HiddenItem]:
        for key in ("JS", "A/JS", "AA/K/JS", "AA/F/JS", "AA/V/JS", "AA/C/JS",
                    "OpenAction/JS", "A/A/JS"):
            body = script_text(xref, key)
            if body:
                yield HiddenItem(f"JavaScript in object {xref} (/{key})", body)

    try:
        catalog = doc.pdf_catalog()
    except Exception:
        return
    yield from walk(catalog)
    # The document-level name tree, where Acrobat registers doc scripts.
    try:
        kind, value = doc.xref_get_key(catalog, "Names/JavaScript/Names")
    except Exception:
        kind, value = "null", None
    if kind == "array" and value:
        # Entries are name/reference pairs and a name string can abut its
        # reference with no space ("[(s)6 0 R]"), so match references
        # rather than splitting on whitespace.
        for ref in re.findall(r"(\d+)\s+\d+\s+R", str(value)):
            yield from walk(int(ref))


def _hidden_objects(doc: fitz.Document) -> Iterator[HiddenItem | str]:
    """Yield HiddenItem for content no page renders, or a str warning.

    Warnings are yielded rather than raised so one unreadable carrier
    cannot discard everything already found — the caller consumes items
    lazily and records each as it arrives.
    """
    try:
        names = doc.embfile_names()
    except Exception as exc:
        yield Warn(
            "HIDDEN_ITEM_FAILED",
            "Hidden",
            f"Hidden: embedded-file index unreadable ({exc}) — attachments NOT scanned",
            storage="live",
        )
        names = []
    for i, _name in enumerate(names):
        where = f"embedded file #{i}"
        try:
            info = doc.embfile_info(i)
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: {where} metadata unreadable ({exc}) — NOT scanned",
                storage="live",
            )
            info = {}
        # Identity fields are discrete values, so they stay hard.
        for key in ("filename", "ufilename", "description"):
            value = info.get(key)
            if value:
                yield HiddenItem(f"{where} ({key})", str(value))
        # 'size' is the uncompressed length; 'length' is the compressed
        # stream, so only 'size' bounds what reading will allocate.
        declared = info.get("size") or 0
        if declared > MAX_ATTACHMENT_BYTES:
            yield Warn(
                "ATTACHMENT_TOO_LARGE",
                "Hidden",
                (
                    f"Hidden: {where} declares {declared} bytes, over the "
                    f"{MAX_ATTACHMENT_BYTES}-byte scan limit — content NOT "
                    "scanned"
                ),
                storage="live",
            )
            continue
        try:
            content = doc.embfile_get(i)
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: {where} unreadable ({exc}) — content NOT scanned",
                storage="live",
            )
            continue
        if not content and declared:
            # embfile_get returns b"" instead of raising on a stream it
            # cannot decompress; an unread attachment must never pass.
            yield Warn(
                "ATTACHMENT_EMPTY",
                "Hidden",
                f"Hidden: {where} declares {declared} bytes but decoded empty — content NOT scanned",
                storage="live",
            )
            continue
        if len(content) > MAX_ATTACHMENT_BYTES:
            yield Warn(
                "PAYLOAD_TRUNCATED",
                "Hidden",
                (
                    f"Hidden: {where} is {len(content)} bytes; only the first "
                    f"{MAX_ATTACHMENT_BYTES} were scanned"
                ),
                storage="live",
            )
            content = content[:MAX_ATTACHMENT_BYTES]
        if content:
            if not _is_readable_text(content):
                # A zip, Office file, image or nested PDF is decoded as
                # garbage below; say so rather than pass it as scanned.
                yield Warn(
                    "ATTACHMENT_NOT_TEXT",
                    "Hidden",
                    (
                        f"Hidden: {where} is not text (e.g. zip, Office, "
                        "image or PDF) — its contents are NOT scanned; manual "
                        "review recommended"
                    ),
                    storage="live",
                )
            yield HiddenItem(
                f"{where} (content)",
                content.decode("utf-8", errors="replace"),
                hard=False,
            )

    for page_index in range(doc.page_count):
        human_page = page_index + 1
        try:
            page = doc.load_page(page_index)
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: page {human_page} failed ({exc}) — NOT scanned",
                storage="live",
            )
            continue
        try:
            annots = list(page.annots())
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: page {human_page} annotations failed ({exc}) — NOT scanned",
                storage="live",
            )
            annots = []
        for annot in annots:
            try:
                xref = annot.xref
                info = annot.info
            except Exception as exc:
                yield Warn(
                    "HIDDEN_ITEM_FAILED",
                    "Hidden",
                    f"Hidden: an annotation on page {human_page} failed ({exc}) — NOT scanned",
                    storage="live",
                )
                continue
            for key in ("content", "title", "subject"):
                value = info.get(key)
                if value:
                    yield HiddenItem(
                        f"annotation {xref} on page {human_page} ({key})", str(value)
                    )
            # A /FileAttachment annotation carries its payload on the
            # page, outside the document-level EmbeddedFiles tree.
            try:
                payload = annot.get_file()
            except Exception:
                payload = None
            if payload:
                if len(payload) > MAX_ATTACHMENT_BYTES:
                    yield Warn(
                        "PAYLOAD_TRUNCATED",
                        "Hidden",
                        (
                            f"Hidden: attachment on annotation {xref} is "
                            f"{len(payload)} bytes; only the first "
                            f"{MAX_ATTACHMENT_BYTES} were scanned"
                        ),
                        storage="live",
                    )
                    payload = payload[:MAX_ATTACHMENT_BYTES]
                if not _is_readable_text(payload):
                    yield Warn(
                        "ATTACHMENT_NOT_TEXT",
                        "Hidden",
                        (
                            f"Hidden: file attached to annotation {xref} on page "
                            f"{human_page} is not text (e.g. zip, Office, image or "
                            "PDF) — its contents are NOT scanned; manual review "
                            "recommended"
                        ),
                        storage="live",
                    )
                yield HiddenItem(
                    f"file attached to annotation {xref} on page {human_page}",
                    payload.decode("utf-8", errors="replace"),
                    hard=False,
                )
        try:
            widgets = list(page.widgets())
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: page {human_page} form fields failed ({exc}) — NOT scanned",
                storage="live",
            )
            widgets = []
        for widget in widgets:
            try:
                xref = widget.xref
                pairs = (("value", widget.field_value), ("name", widget.field_name))
            except Exception as exc:
                yield Warn(
                    "HIDDEN_ITEM_FAILED",
                    "Hidden",
                    f"Hidden: a form field on page {human_page} failed ({exc}) — NOT scanned",
                    storage="live",
                )
                continue
            for label, value in pairs:
                if value:
                    yield HiddenItem(
                        f"form field {xref} on page {human_page} ({label})", str(value)
                    )
        try:
            links = page.get_links()
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: page {human_page} links failed ({exc}) — NOT scanned",
                storage="live",
            )
            links = []
        for n, link in enumerate(links):
            for key in ("uri", "file"):
                value = link.get(key)
                if value:
                    yield HiddenItem(
                        f"link #{n} on page {human_page} ({key})", str(value)
                    )

    try:
        yield from _js_sources(doc)
    except Exception as exc:
        yield Warn(
            "HIDDEN_ITEM_FAILED",
            "Hidden",
            f"Hidden: JavaScript sweep failed ({exc}) — NOT scanned",
            storage="live",
        )

    try:
        ocgs = doc.get_ocgs()
    except Exception as exc:
        yield Warn(
            "HIDDEN_ITEM_FAILED",
            "Hidden",
            f"Hidden: optional-content groups failed ({exc}) — NOT scanned",
            storage="live",
        )
        ocgs = {}
    for xref, ocg in ocgs.items():
        name = ocg.get("name") if isinstance(ocg, dict) else None
        if name:
            yield HiddenItem(f"optional-content group {xref}", str(name))


def scan_hidden_objects(
    doc: fitz.Document,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Scan attachments, annotations, form fields, links, scripts and
    layer names — content no page renders.

    Items are consumed lazily and recorded as they arrive, so a carrier
    that fails later cannot discard what was already found. Discrete
    field values are hard findings; attachment bodies are fusion-prone
    runs of text and are demoted to manual-review warnings, like every
    other fusion-prone surface in the tool.
    """
    items = _hidden_objects(doc)
    while True:
        try:
            item = next(items)
        except StopIteration:
            break
        except Exception as exc:
            report.warn(
                "LAYER_CRASHED",
                "Hidden",
                f"Hidden: object sweep failed ({exc}) — layer NOT fully scanned",
            )
            break
        if isinstance(item, str):
            # A yielded str that is not a Warn is still a failure notice;
            # keep it (coded as such) rather than crash the whole layer.
            report.warnings.append(
                item if isinstance(item, Warn) else Warn("HIDDEN_ITEM_FAILED", "Hidden", item)
            )
            continue
        if not item.text:
            continue
        hits = [s.name for s in matcher.search(normalize_string(item.text))]
        samples = match_patterns(item.text, patterns)
        if item.hard:
            for name in hits:
                report.record("Hidden", name, item.location)
            for name, sample in samples.items():
                report.record("Hidden", name, item.location, sample)
        else:
            for name in hits:
                report.warn(
                    "REVIEW_HIDDEN_TEXT",
                    "Hidden",
                    f"Hidden: {item.location} contains a sequence matching "
                    f"{name!r} — this content is a run of arbitrary text, so the "
                    "match may be a coincidental fusion; manual review recommended",
                    storage="live",
                    rule=name,
                    adjacency="NOISY_SOURCE",
                )
            for name, sample in samples.items():
                report.warn(
                    "REVIEW_HIDDEN_TEXT",
                    "Hidden",
                    f"Hidden: {item.location} contains a sequence matching "
                    f"pattern rule {name!r} (sample {mask(sample)}) — this content "
                    "is a run of arbitrary text, so the match may be a "
                    "coincidental fusion; manual review recommended",
                    storage="live",
                    rule=name,
                    adjacency="NOISY_SOURCE",
                )


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
        report.warn(
            "TOOL_TIMEOUT",
            "Metadata",
            "Metadata: exiftool timed out — layer NOT scanned",
            tool="exiftool",
        )
        return
    if proc.returncode != 0:
        report.warn(
            "TOOL_EXIT_NONZERO",
            "Metadata",
            f"Metadata: exiftool exited {proc.returncode} — results may be incomplete",
            tool="exiftool",
            returncode=proc.returncode,
        )
    if not out:
        report.warn(
            "TOOL_NO_OUTPUT",
            "Metadata",
            "Metadata: exiftool produced no output — layer NOT scanned",
            tool="exiftool",
        )
        return

    text = out.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
        # Exclude filesystem-derived fields: the local path or timestamps
        # matching a secret is not a leak inside the document.
        if isinstance(payload, list):
            non_dict_count = sum(1 for e in payload if not isinstance(e, dict))
            if non_dict_count:
                report.warn(
                    "TOOL_OUTPUT_MALFORMED",
                    "Metadata",
                    f"Metadata: exiftool JSON contained {non_dict_count} "
                    "non-object entry(ies) — skipped; results may be incomplete",
                    tool="exiftool",
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
        report.warn(
            "TOOL_OUTPUT_MALFORMED",
            "Metadata",
            "Metadata: exiftool output was not valid JSON — scanned raw output, "
            "which includes filesystem fields (path collisions possible)",
            tool="exiftool",
        )
        for secret in matcher.search(normalize_string(text)):
            report.record(
                "Metadata", secret.name, "exiftool field sweep (raw fallback)"
            )
        # Raw fallback text includes filesystem paths, so pattern hits
        # here are collision-prone: manual-review warnings, not findings.
        for name, sample in match_patterns(text, patterns).items():
            report.warn(
                "REVIEW_METADATA_RAW",
                "Metadata",
                f"Metadata: raw exiftool output matches pattern rule {name!r} "
                f"(sample {mask(sample)}) — may originate from filesystem "
                "fields; manual review recommended",
                storage="live",
                rule=name,
                adjacency="NOISY_SOURCE",
                tool="exiftool",
            )


def _unescape_pdf_literal(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        esc = match.group(1)
        if esc and esc[0] in "01234567" and all(c in "01234567" for c in esc):
            return chr(int(esc, 8) & 0xFF)
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(esc, esc)

    return _LITERAL_ESCAPE_RE.sub(repl, body)


def _pdf_string_spans(buf: str) -> tuple[list[tuple[int, int]], bool]:
    """Locate every PDF string literal in buf; report unterminated tails.

    Returns (spans, truncated): spans are (start, end) half-open offsets
    of each top-level literal or hex string, and truncated is True when
    the buffer ends inside an unclosed "(" literal.

    Literal strings nest: "(SSN (mine): 123-45-6789)" is ONE string
    whose text includes the inner parentheses, and the spec only
    requires escaping unbalanced ones. A regex that forbade "(" in the
    body matched the inner "(mine)" instead and dropped everything after
    it, so a legal content stream could hide a secret in plain sight.
    Depth tracking reads it the way a PDF parser does.

    An unterminated literal contributes no span — its extent is
    unknowable, and guessing one would fuse the rest of the object into
    a single token hard pattern rules could match across — but it sets
    the truncated flag so the caller can degrade rather than silently
    drop the content after it.

    Single pass, so cost is linear in len(buf). An earlier version
    rescanned from start+1 whenever a literal ran to the end unclosed,
    which is quadratic: a stream of unescaped "(" (crafted, or just
    binary that slipped the opaque-stream filter) took 13 minutes at
    200KB and hours at 1MB — a denial of service on a tool whose whole
    job is to answer.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(buf)
    stack: list[int] = []          # positions of currently-open "("
    while i < n:
        char = buf[i]
        if stack:                  # inside a literal: parens nest, "<" is data
            if char == "\\":       # escape — the next char cannot close
                i += 2
                continue
            if char == "(":
                stack.append(i)
            elif char == ")":
                start = stack.pop()
                if not stack:
                    spans.append((start, i + 1))
            i += 1
            continue
        if char == "(":
            stack.append(i)
            i += 1
            continue
        if char == "<":
            match = _PDF_HEX_STRING_RE.match(buf, i)
            if match:
                spans.append((match.start(), match.end()))
                i = match.end()
                continue
        i += 1
    return spans, bool(stack)


def _iter_pdf_strings(buf: str) -> Iterator[tuple[str, int]]:
    """Yield (token, end_offset) for each PDF string literal in buf."""
    spans, _ = _pdf_string_spans(buf)
    for start, end in spans:
        yield buf[start:end], end


def _strip_pdf_strings(source: str) -> str:
    """Blank out string/hex-literal spans, preserving offsets.

    Indirect references ("N G R") are structural syntax; the identical
    characters inside a string literal are data. Extracting references
    from the raw source treated '(see 7 0 R)' as a real edge to object 7,
    so a genuine orphan whose number appeared in some reachable object's
    text lost its ORPHANED label. Reference extraction runs on this
    stripped form instead.
    """
    spans, _ = _pdf_string_spans(source)
    if not spans:
        return source
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(source[last:start])
        out.append(" " * (end - start))
        last = end
    out.append(source[last:])
    return "".join(out)


_PDF_DATE_RE = re.compile(r"D:\d{4}(?:\d{2}){0,5}(?:[Zz+\-](?:\d{2}'?\d{0,2}'?)?)?$")


def _decode_pdf_string(token: str) -> str | None:
    """Decode one `(literal)` or `<hex>` token to text: escapes resolved,
    UTF-16BE when byte-order-marked, otherwise latin-1. None when a hex
    token is malformed."""
    if token.startswith("("):
        decoded = _unescape_pdf_literal(token[1:-1])
        # A literal may itself hold UTF-16BE text (BOM-prefixed).
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
        return None
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", errors="replace")
    return data.decode("latin-1")


def _feed_pdf_strings(
    buf: str,
    scanner: RollingScanner,
    hard_patterns: PatternScanner,
    soft_patterns: PatternScanner,
) -> bool:
    """Extract PDF string/hex literals from buf and feed all scanners.

    The normalized secret scanner and the *soft* pattern scanner see
    literals back-to-back (adjacency preserved, so split values are
    caught); the *hard* pattern scanner gets a two-newline fence between
    literals — a single [-\\s.] separator slot cannot cross it — so a
    hard pattern finding can never be a fusion of unrelated tokens.

    Returns True when the buffer ended inside an unterminated literal, so
    the caller can degrade the verdict rather than silently drop whatever
    followed the unclosed "(".
    """
    spans, truncated = _pdf_string_spans(buf)
    for start, end in spans:
        decoded = _decode_pdf_string(buf[start:end])
        if decoded is None:
            continue
        scanner.feed(normalize_string(decoded))
        if _PDF_DATE_RE.match(decoded):
            # A PDF timestamp (D:YYYYMMDDHHmmSS±HH'mm'): machine-written,
            # never user data, and its digit run passes the card checksum
            # about one time in ten — every incremental save adds another.
            continue
        hard_patterns.feed(decoded + "\n\n")
        soft_patterns.feed(decoded)
    return truncated


def _stream_key(doc: fitz.Document, xref: int, name: str) -> tuple[str, str]:
    """Read an object key, resolving one level of indirection.

    A /Subtype, /Type or /Filter may be written as an indirect reference
    (/Filter 12 0 R); xref_get_key then returns kind "xref", which is
    neither a name nor an array and used to fall through to "text", so an
    image or font behind an indirect key was parsed as text. Follow the
    single reference so the classifier sees the real value.
    """
    try:
        kind, value = doc.xref_get_key(xref, name)
    except Exception:
        return "null", "null"
    if kind == "xref":
        target = value.split()[0]
        if target.isdigit():
            try:
                value = doc.xref_object(int(target), compressed=True).strip()
            except Exception:
                return "null", "null"
            # A referenced value object is its own source: "/DCTDecode" or
            # "[/FlateDecode /DCTDecode]". Infer the kind the callers test.
            kind = "array" if value.startswith("[") else "name"
    return kind, value


# The uncompressed-length key of a Type1/TrueType font program, present on
# nothing else; a load-bearing signal that a stream is a font, not text.
_FONT_LENGTH_KEY = "Length1"
# Stream /Type values whose body is not document text: object streams pack
# the source of other objects (each of which is ALSO visited by its own
# xref number, so scanning the container re-finds and mislabels them), and
# cross-reference streams are packed binary offsets.
_OPAQUE_STREAM_TYPES: frozenset[str] = frozenset({"/ObjStm", "/XRef"})


def _is_opaque_stream(doc: fitz.Document, xref: int) -> bool:
    """Whether a stream body is program data or infrastructure, not text.

    Decided from the object's own keys, never by searching the dictionary
    source: "/Image" occurs as a substring in the
    /ProcSet[/PDF/Text/ImageB/ImageC/ImageI] array that countless
    producers emit on ordinary text-bearing Form XObjects, and skipping
    those loses their text silently.

    This is a fast structural pre-filter that keeps large, known-binary
    bodies (images, fonts, embedded files) from being decompressed and
    tokenized at all. It is deliberately NOT the last word: a marker-less
    binary stream (a CFF font with no /Length1, an ICC profile, a bare
    /FlateDecode blob) still slips through here and is caught downstream
    by a content sniff on the decompressed bytes (_looks_binary). Object
    and cross-reference streams are excluded because their bytes are the
    serialized form of objects scanned individually, not content.
    """
    kind, subtype = _stream_key(doc, xref, "Subtype")
    if kind == "name" and subtype in _OPAQUE_STREAM_SUBTYPES:
        return True
    kind, type_ = _stream_key(doc, xref, "Type")
    if kind == "name" and (type_ == "/EmbeddedFile" or type_ in _OPAQUE_STREAM_TYPES):
        return True
    try:
        if doc.xref_get_key(xref, _FONT_LENGTH_KEY)[0] != "null":
            return True
    except Exception:
        pass
    kind, value = _stream_key(doc, xref, "Filter")
    if kind == "name":
        return value in _BINARY_STREAM_FILTERS
    if kind == "array":
        # /Filter can be a pipeline. PyMuPDF renders arrays without
        # separators ("[/ASCII85Decode/DCTDecode]"), so split on the name
        # delimiter rather than on whitespace.
        return any(name in _BINARY_STREAM_FILTERS
                   for name in re.findall(r"/[^\s/\[\]<>(){}%]+", value))
    return False


# Bytes that occur freely in PDF content streams and dictionary text:
# printable ASCII plus tab/newline/formfeed/carriage-return.
_TEXTISH_BYTES = frozenset(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0C, 0x0D}
# A content stream is essentially all operators and text; image/font/
# profile bytes are mostly outside this set. The threshold sits well
# below real content streams (~1.0) and well above binary (measured
# <0.5 for JPEG/CFF/ICC), so it separates the two without decoding.
_TEXT_BYTE_FLOOR = 0.85


def _looks_binary(data: bytes) -> bool:
    """Whether decompressed bytes are binary rather than PDF text.

    The general text-vs-binary signal the structural pre-filter cannot
    give: it reads the actual body instead of enumerating carrier types,
    so a marker-less font/profile/blob that slipped _is_opaque_stream is
    still kept out of the tokenizer (where its "(" bytes would coin false
    findings). Sampled, so cost is bounded regardless of body size.
    """
    if not data:
        return False
    sample = data[:65536]
    textish = sum(1 for b in sample if b in _TEXTISH_BYTES)
    return textish / len(sample) < _TEXT_BYTE_FLOOR


_XREF_REF_RE = re.compile(r"\b(\d+)\s+\d+\s+R\b")


def _reachable_from_sources(
    trailer: str, sources: dict[int, str], size: int | None = None
) -> tuple[set[int], bool]:
    """Objects the document graph references, walked from the trailer.

    Reachability is walked from the trailer, not guessed from the page
    tree: annotations, form fields, appearance streams, /Info and the
    name tree hang off the catalog, not off a page, so a page-only walk
    labelled 61 of 73 findings on a real document "ORPHANED" — a claim
    that means "left behind by a redaction" and so must be earned.

    References are read from the *stripped* source (string literals
    blanked) so a ref-shaped run of characters inside a string cannot
    fake an edge. Returns (reachable, trusted); trusted is False when a
    referenced object was missing from *sources* (something could not be
    read), because then the set may be incomplete and no ORPHANED label
    can be earned from it.
    """
    trusted = True
    try:
        pending = [int(m) for m in _XREF_REF_RE.findall(_strip_pdf_strings(trailer))]
    except Exception:
        return set(), False
    if not pending:
        # A well-formed document always references a /Root from its
        # trailer; no roots means the trailer was unreadable, so the walk
        # cannot be trusted to distinguish an orphan from live content.
        return set(), False
    seen: set[int] = set()
    while pending:
        xref = pending.pop()
        if xref in seen or xref < 1:
            continue
        seen.add(xref)
        source = sources.get(xref)
        if source is None:
            # A reference past the end of the table is to an object that
            # does not exist, which the PDF spec reads as null — common in
            # real files and no reason to distrust the walk. Only an
            # existing object we could not read leaves the set incomplete.
            if size is None or xref < size:
                trusted = False
            continue
        pending.extend(
            int(m) for m in _XREF_REF_RE.findall(_strip_pdf_strings(source)))
    return seen, trusted


# ── Content-stream tokenizer ──────────────────────────────────────────────
# Leftover streams are judged by what they would draw, so they are read the
# way a PDF parser reads content: tokens separated by whitespace (including
# NUL) *or* delimiters, strings and inline-image data skipped as units.
# Regexes that required whitespace around operators missed content written
# compactly ("BT/F1 11 Tf", "]ID"), as clean_contents, redactor and many
# producers write it; windowed ones missed long text objects and
# backtracked for minutes on hostile input. This is one linear pass.
_WS = "\x00\t\n\x0c\r "
_CONTENT_TOKEN_RE = re.compile(
    rf"[{_WS}]+"                                  # whitespace
    r"|%[^\r\n]*"                                 # comment
    r"|<<|>>|[\[\]{{}}]"                             # dict / array / proc delimiters
    rf"|<[0-9A-Fa-f{_WS}]*>"                      # hex string
    rf"|/[^{_WS}()<>\[\]{{}}/%]*"                   # name
    rf"|[^{_WS}()<>\[\]{{}}/%]+"                    # number or operator
    r"|[)<>]")                                    # stray delimiter
_LITERAL_STEP_RE = re.compile(r"[()\\]")
# Inline image data ends at whitespace + EI + a token boundary.
_INLINE_IMAGE_END_RE = re.compile(rf"[{_WS}]EI(?=[{_WS}()<>\[\]{{}}/%]|$)")
_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)$")
_TEXT_SHOW_OPS = frozenset({"Tj", "TJ", "'", '"'})
# Common content operators between whitespace, for telling a leftover that
# is page content from a text payload to search raw. Deliberately *not*
# delimiter-bounded like the tokenizer: in prose, code and HTML, "(n)",
# "f(x)" and ">n<" would count, and a secret in such text would lose its
# raw-text search.
_CONTENT_OP_RE = re.compile(r"(?:^|\s)(?:q|Q|cm|re|Do|BT|ET|Tf|Td|Tm|m|l|f|S|W|n)(?=\s)")


@dataclass
class _ContentScan:
    """What a content stream would draw, as far as tokens tell."""

    text_objects: int = 0          # BT … ET blocks that show a string
    shown: list[str] = field(default_factory=list)   # one per show operation
    pieces: list[str] = field(default_factory=list)  # each shown string alone
    inline_images: list[tuple[int | None, int | None]] = field(default_factory=list)


def _literal_end(buf: str, start: int) -> int:
    """Index just past the literal string opening at *start* (nesting and
    escapes honoured), or len(buf) if it never closes."""
    depth, skip = 0, -1
    for m in _LITERAL_STEP_RE.finditer(buf, start):
        pos = m.start()
        if pos < skip:
            continue
        char = m.group()
        if char == "\\":
            skip = pos + 2
        elif char == "(":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return pos + 1
    return len(buf)


def _inline_dimensions(tokens: list[str]) -> tuple[int | None, int | None]:
    dims: dict[str, int] = {}
    for key, value in zip(tokens, tokens[1:]):
        if key in ("/W", "/Width", "/H", "/Height") and value.isdigit():
            dims[key[1]] = int(value)
    return dims.get("W"), dims.get("H")


def _scan_content(buf: str) -> _ContentScan:
    """Tokenize a content stream (latin-1 text) in one linear pass."""
    scan = _ContentScan()
    operands: list[tuple[str, Any]] = []
    array: list[str] | None = None
    depth = 0
    in_text = shown_in_block = False
    inline: list[str] | None = None
    i, n = 0, len(buf)
    while i < n:
        if buf[i] == "(":
            end = _literal_end(buf, i)
            token = buf[i:end] if end < n or buf[end - 1] == ")" else buf[i:end] + ")"
            text = _decode_pdf_string(token) or ""
            if array is not None:
                array.append(text)
            else:
                operands.append(("str", text))
            i = end
            continue
        m = _CONTENT_TOKEN_RE.match(buf, i)
        if m is None:               # unreachable: the pattern covers every char
            i += 1
            continue
        token, i = m.group(), m.end()
        first = token[0]
        if first in _WS or first == "%":
            continue
        if inline is not None:      # the inline image's dictionary, up to ID
            if token == "ID":
                scan.inline_images.append(_inline_dimensions(inline))
                end = _INLINE_IMAGE_END_RE.search(buf, i + 1)
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
            text = _decode_pdf_string(token) or ""
            if array is not None:
                array.append(text)
            else:
                operands.append(("str", text))
            continue
        if token in ("<<", ">>", "{", "}", ")", "<", ">") or first == "/" or _NUMBER_RE.match(token):
            if array is None and first == "/":
                operands.append(("name", token))
            continue
        if array is not None:       # a bare word inside [...] is data
            continue
        # An operator.
        if token == "BT":
            in_text, shown_in_block = True, False
        elif token == "ET":
            if in_text and shown_in_block:
                scan.text_objects += 1
            in_text = False
        elif token in _TEXT_SHOW_OPS and operands:
            kind, value = operands[-1]
            if token == "TJ" and kind == "array":
                scan.shown.append("".join(value))
                scan.pieces.extend(value)
            elif token != "TJ" and kind == "str":
                scan.shown.append(value)
                scan.pieces.append(value)
            else:
                value = None
            if value is not None and in_text:
                shown_in_block = True
        elif token == "BI":
            inline = []
        operands.clear()
    return scan


# Share of a string's characters that must be ordinary text for it to
# count as readable; glyph-ID codes (Identity-H) are half NULs.
_READABLE_CODE_FLOOR = 0.8
# WinAnsi's printable punctuation in 0x80-0x9F (smart quotes, bullets,
# dashes) decodes to C1 controls under latin-1; judge it as cp1252 does.
_CP1252_C1 = {i: bytes([i]).decode("cp1252", errors="ignore") or "\x00"
              for i in range(0x80, 0xA0)}


def _is_plain_code(c: str) -> bool:
    c = _CP1252_C1.get(ord(c), c)
    return c.isprintable() and ord(c) < 0x250


def _shows_unreadable_text(body_text: str) -> bool:
    """Whether a content stream draws any text whose codes are not plain
    characters — so decoding its strings as latin-1 cannot find a secret.

    Text in a CID/Identity-H font is stored as glyph numbers, a custom
    /Differences or Type3 font as arbitrary codes: the strings are there,
    but without the font they are not the characters on the page. Judged
    per string, because one run of glyph codes among ordinary text is
    exactly how a secret in a second font hides. Each TJ array is also
    judged joined, so kerning that splits glyph codes into one-character
    strings cannot hide them either.
    """
    return _unreadable(_scan_content(body_text))


def _unreadable(scan: _ContentScan) -> bool:
    return any(len(text) >= 2
               and sum(map(_is_plain_code, text)) / len(text) < _READABLE_CODE_FLOOR
               for text in (*scan.pieces, *scan.shown))


# File signatures of containers whose contents are not searchable as text
# even when their bytes are mostly printable (a small PDF is).
_CONTAINER_MAGIC = (b"%PDF-", b"PK\x03\x04", b"\x1f\x8b", b"\xd0\xcf\x11\xe0",
                    b"Rar!", b"7z\xbc\xaf")


def _is_container(data: bytes) -> bool:
    return data.lstrip()[:8].startswith(_CONTAINER_MAGIC)


def _is_readable_text(data: bytes) -> bool:
    """Whether bytes are text this tool can search: not a container (PDF,
    zip, gzip, Office, rar, 7z), and valid UTF-8 (any language), UTF-16
    with a byte-order mark, or mostly printable."""
    if _is_container(data):
        return False
    sample = data[:65536]
    if sample[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if exc.start >= len(sample) - 3:     # cut mid-character at 64 KB
            return True
    return not _looks_binary(data)


def _read_stream_capped(doc: fitz.Document, xref: int, cap: int) -> tuple[bytes, bool]:
    """A stream body decompressed to at most *cap* bytes, and whether it was
    cut. Flate streams are inflated incrementally, so a compression bomb
    costs *cap* bytes, not its full size; other filters decode in full."""
    if _stream_key(doc, xref, "Filter") == ("name", "/FlateDecode") \
            and _stream_key(doc, xref, "DecodeParms")[0] == "null":
        inflater = zlib.decompressobj()
        data = inflater.decompress(doc.xref_stream_raw(xref), cap)
        return data, bool(inflater.unconsumed_tail)
    data = doc.xref_stream(xref)
    return data[:cap], len(data) > cap


# Orphaned stream types whose bodies are plain text rather than PDF string
# syntax, scanned as raw text: {/Type: label}.
_ORPHAN_PAYLOAD_TYPES: dict[str, str] = {
    "/EmbeddedFile": "attachment",
    "/Metadata": "XMP metadata",
}

# Smallest leftover image worth flagging: a single line of text at a
# readable size is ~8 px tall, so strips that small are flagged too; below
# that, and anything under 32 px long, only masks and icons remain.
_MIN_TEXT_IMAGE_SIDE = 8
_MIN_TEXT_IMAGE_LENGTH = 32


def _text_sized(width: int, height: int) -> bool:
    return (min(width, height) >= _MIN_TEXT_IMAGE_SIDE
            and max(width, height) >= _MIN_TEXT_IMAGE_LENGTH)


def _is_text_sized_image(doc: fitz.Document, xref: int) -> bool:
    if _stream_key(doc, xref, "Subtype") != ("name", "/Image"):
        return False
    try:
        width = int(doc.xref_get_key(xref, "Width")[1])
        height = int(doc.xref_get_key(xref, "Height")[1])
    except (ValueError, TypeError, RuntimeError):
        return True        # unknown size: flag rather than assume harmless
    return _text_sized(width, height)


def _has_text_sized_inline_image(body_text: str) -> bool:
    return any(w is None or h is None or _text_sized(w, h)
               for w, h in _scan_content(body_text).inline_images)


def _is_content_stream(body_text: str) -> bool:
    """Whether a stream is page content that draws text."""
    return _scan_content(body_text).text_objects > 0


def _object_list(xrefs: list[int], limit: int = 8) -> str:
    shown = ", ".join(map(str, xrefs[:limit]))
    more = f" (+{len(xrefs) - limit} more)" if len(xrefs) > limit else ""
    return f"object{'s' if len(xrefs) > 1 else ''} {shown}{more}"


def _scan_orphaned_payload(
    body: bytes,
    label: str,
    where: str,
    *,
    hard: bool,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
    origin: dict[str, Any],
) -> None:
    """Search leftover text that is not PDF string syntax as raw text.

    A known value in XMP is held to the Metadata layer's tier (hard); every
    other hit — any pattern match, and anything in an attachment or other
    raw text — is manual review: IDs and dates in such text assemble digit
    runs that pattern classes accept by coincidence.
    """
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = body.decode("utf-16", errors="replace")
    else:
        text = body.decode("utf-8", errors="replace")
    for secret in matcher.search(normalize_string(text)):
        _report_payload_hit(report, where, secret.name, "", hard=hard, origin=origin)
    for name, sample in match_patterns(text, patterns).items():
        _report_payload_hit(report, where, name, sample, hard=False, origin=origin)


def _report_payload_hit(
    report: ScanReport, where: str, name: str, sample: str, *, hard: bool, origin: dict[str, Any]
) -> None:
    if hard:
        report.record("Objects", name, where, sample, **origin)
        return
    shown = f" (sample {mask(sample)})" if sample else ""
    report.warn(
        "REVIEW_OBJECT_TEXT",
        "Objects",
        f"Objects: {where} contains a sequence matching {name!r}{shown}"
        " — this content is a run of arbitrary text, so the match may be "
        "a coincidental fusion; manual review recommended",
        rule=name,
        adjacency="NOISY_SOURCE",
        **origin,
    )


def scan_pdf_objects(
    doc: fitz.Document,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Decode PDF string literals object by object.

    The Binary layer used to find literals by running a PDF-syntax regex
    over qpdf's whole byte stream. That stream interleaves structure with
    image data, and a text parser cannot tell them apart — so a "(" byte
    inside a JPEG stalled the scanner, which is what the 64KB carry cap
    and its warning existed to contain.

    Walking objects removes the category error rather than compensating
    for it. Every unit here is bounded and typed: a dictionary is always
    text, a stream body is scanned only when it reads as text (a fast key
    pre-filter, then a content sniff), and an unterminated literal
    degrades the verdict instead of silently dropping what follows it.

    Each object's source is read once into *sources*; reachability is
    computed from that same map, so no object is decompressed twice.
    """
    unreadable = 0
    try:
        xref_count = doc.xref_length()
    except Exception as exc:
        report.warn(
            "XREF_UNREADABLE",
            "Objects",
            f"Objects: xref table unreadable ({exc}) — layer NOT scanned",
        )
        return

    # One decompression per object; reachability reads this same map.
    sources: dict[int, str] = {}
    for xref in range(1, xref_count):
        try:
            sources[xref] = doc.xref_object(xref, compressed=True)
        except Exception:
            unreadable += 1
    try:
        trailer = doc.pdf_trailer()
    except Exception:
        trailer = ""            # reachability unknown -> no ORPHANED claims
    reachable, trusted = _reachable_from_sources(trailer, sources, xref_count)

    tally = _ObjectTally(unreadable=unreadable)
    for xref, source in sources.items():
        # The ORPHANED label is applied only when the reachability walk is
        # trustworthy; otherwise the plain "object N" is used rather than
        # accusing content falsely.
        # Leftover checks run on every object the walk did not reach; only
        # the ORPHANED label waits for a trustworthy walk. With no walk at
        # all (unreadable trailer), nothing can be called leftover.
        leftover = bool(reachable) and xref not in reachable
        orphaned = leftover and trusted
        where = f"{'ORPHANED object' if orphaned else 'object'} {xref}"
        storage = "orphaned" if orphaned else "unreferenced" if leftover else "live"
        _scan_object(
            doc,
            xref,
            source,
            where,
            leftover=leftover,
            origin={"storage": storage, "object": xref},
            matcher=matcher,
            patterns=patterns,
            report=report,
            tally=tally,
        )
    # With an untrusted walk the leftovers were not reached, but cannot be
    # called ORPHANED — the same restraint the per-object label shows.
    tally.report_to(
        report,
        leftover_kind="ORPHANED" if trusted else "unreferenced",
        storage="orphaned" if trusted else "unreferenced",
    )


@dataclass
class _ObjectTally:
    """What the Objects layer could not read, aggregated for one warning
    each instead of one per object."""

    unreadable: int = 0
    truncated: int = 0
    undecodable: list[int] = field(default_factory=list)
    unreadable_payloads: list[str] = field(default_factory=list)
    images: list[int] = field(default_factory=list)

    def report_to(
        self, report: ScanReport, *, leftover_kind: str, storage: str, scope: str = ""
    ) -> None:
        # Leftover content this layer found but cannot read is reported
        # rather than skipped: a leftover is exactly where a redactor's
        # original lives, so "could not read it" must not pass as clean.
        prefix = f"Objects: {scope}" if scope else "Objects: "
        if self.undecodable:
            report.warn(
                "LEFTOVER_UNDECODABLE_TEXT",
                "Objects",
                f"{prefix}{len(self.undecodable)} {leftover_kind} object(s) draw "
                "text this tool cannot decode (font codes that are not plain "
                "characters, or text mixed with binary data): "
                f"{_object_list(self.undecodable)} — NOT scanned; manual "
                "review recommended",
                storage=storage,
            )
        if self.images:
            report.warn(
                "LEFTOVER_IMAGE",
                "Objects",
                f"{prefix}{len(self.images)} {leftover_kind} image(s) this tool "
                f"does not read (stored images are not OCR'd): "
                f"{_object_list(self.images)} — NOT scanned; manual review "
                "recommended",
                storage=storage,
            )
        if self.unreadable_payloads:
            report.warn(
                "LEFTOVER_CONTAINER",
                "Objects",
                f"{prefix}{leftover_kind} payload(s) this tool cannot read as "
                f"text (e.g. zip, Office, image): "
                f"{', '.join(self.unreadable_payloads)} — NOT scanned; manual "
                "review recommended",
                storage=storage,
            )
        if self.truncated:
            report.warn(
                "UNTERMINATED_STRING",
                "Objects",
                f"{prefix}{self.truncated} object(s) held an unterminated "
                "string literal — content after it was NOT scanned; manual "
                "review recommended",
                storage=storage,
            )
        if self.unreadable:
            report.warn(
                "OBJECT_UNREADABLE",
                "Objects",
                f"{prefix}{self.unreadable} object(s) could not be read — NOT " "fully scanned",
                storage=storage,
            )


def _read_stream(doc: fitz.Document, xref: int, tally: _ObjectTally) -> bytes | None:
    try:
        return doc.xref_stream(xref)
    except Exception:
        tally.unreadable += 1
        return None


def _read_leftover(
    doc: fitz.Document,
    xref: int,
    where: str,
    report: ScanReport,
    tally: _ObjectTally,
    origin: dict[str, Any],
) -> bytes | None:
    """A leftover stream body, capped like live attachments are, so a
    decompression bomb left in the file cannot exhaust memory."""
    try:
        body, cut = _read_stream_capped(doc, xref, MAX_ATTACHMENT_BYTES)
    except Exception:
        tally.unreadable += 1
        return None
    if cut:
        report.warn(
            "PAYLOAD_TRUNCATED",
            "Objects",
            f"Objects: {where} is larger than {MAX_ATTACHMENT_BYTES} bytes "
            "decompressed; only the start was scanned",
            **origin,
        )
    return body


def _check_leftover_stream(
    xref: int,
    body: bytes,
    body_text: str,
    where: str,
    *,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
    tally: _ObjectTally,
    origin: dict[str, Any],
) -> None:
    """Leftover content no other layer will see: read what can be read as
    raw text, flag what cannot be read at all."""
    scan = _scan_content(body_text)
    if any(w is None or h is None or _text_sized(w, h) for w, h in scan.inline_images):
        tally.images.append(xref)               # pixels: not OCR'd
    if scan.text_objects:
        if _looks_binary(body) or _unreadable(scan):
            tally.undecodable.append(xref)       # glyph codes, or text by binary
    elif _is_container(body):
        tally.unreadable_payloads.append(f"{xref} (embedded file)")
    elif not _looks_binary(body) and len(_CONTENT_OP_RE.findall(body_text)) < 3:
        # Neither page content nor binary: an untyped attachment, a script
        # or private application data. Its text is not PDF string syntax,
        # so search it raw — at manual review, since it is arbitrary text.
        _scan_orphaned_payload(
            body,
            "raw text",
            f"{where} (raw text)",
            hard=False,
            matcher=matcher,
            patterns=patterns,
            report=report,
            origin=origin,
        )


def _scan_object(
    doc: fitz.Document,
    xref: int,
    source: str,
    where: str,
    *,
    leftover: bool,
    origin: dict[str, Any],
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
    tally: _ObjectTally,
) -> None:
    """Scan one object's dictionary and (text) stream body.

    *leftover* marks content the document no longer uses — an orphan, or
    an object's superseded version from an earlier revision. *origin* (the
    storage class, object and revision) is attached to every finding and
    warning this object produces. For leftovers
    the tool also reads attachment and XMP bodies as raw text, and flags
    text it cannot decode, since no other layer will see them.
    """
    texts = [source]
    try:
        is_stream = doc.xref_is_stream(xref)
    except Exception:
        tally.unreadable += 1
        is_stream = False
    payload = _stream_key(doc, xref, "Type")[1] if is_stream and leftover else None
    if is_stream and leftover and _is_text_sized_image(doc, xref):
        # A leftover image — e.g. the original scan a redaction replaced —
        # can hold the secret as pixels, and nothing OCRs stored images.
        tally.images.append(xref)
    if payload in _ORPHAN_PAYLOAD_TYPES:
        # An attachment or XMP packet: its body is not PDF string syntax,
        # and the Hidden and Metadata layers follow only what the document
        # still references — so nothing else reads a leftover one.
        label = _ORPHAN_PAYLOAD_TYPES[payload]
        body = _read_leftover(doc, xref, where, report, tally, origin)
        if body is not None:
            if _is_readable_text(body):
                _scan_orphaned_payload(
                    body,
                    label,
                    f"{where} ({label})",
                    hard=(payload == "/Metadata"),
                    matcher=matcher,
                    patterns=patterns,
                    report=report,
                    origin=origin,
                )
            else:
                tally.unreadable_payloads.append(f"{xref} ({label})")
    elif is_stream and not _is_opaque_stream(doc, xref):
        body = (
            _read_leftover(doc, xref, where, report, tally, origin)
            if leftover
            else _read_stream(doc, xref, tally)
        )
        if body is not None:
            body_text = body.decode("latin-1")
            # The content sniff is the general backstop to the key
            # pre-filter: a marker-less binary body is kept out of the
            # tokenizer here rather than tokenized into false findings.
            if not _looks_binary(body):
                texts.append(body_text)
            if leftover:
                _check_leftover_stream(
                    xref,
                    body,
                    body_text,
                    where,
                    matcher=matcher,
                    patterns=patterns,
                    report=report,
                    tally=tally,
                    origin=origin,
                )

    literals = RollingScanner(matcher)
    hard_patterns = PatternScanner(patterns)
    soft_patterns = PatternScanner(patterns, collapse_separators=True)
    truncated = False
    for text in texts:
        # Fence so one object's tail cannot fuse with the next.
        if _feed_pdf_strings(text + "\n\n", literals, hard_patterns, soft_patterns):
            truncated = True
    hard_patterns.flush()
    soft_patterns.flush()
    if truncated:
        tally.truncated += 1

    for secret in sorted(literals.found, key=lambda s: s.name):
        report.record("Objects", secret.name, where, **origin)
    for name, sample in hard_patterns.hits.items():
        report.record("Objects", name, where, sample, **origin)
    for name, sample in soft_patterns.hits.items():
        if name in hard_patterns.hits:
            continue
        report.warn(
            "REVIEW_ADJACENT_LITERALS",
            "Objects",
            f"Objects: {where}: adjacent literals fuse into a sequence "
            f"matching pattern rule {name!r} (sample {mask(sample)}) — "
            "possibly a coincidental concatenation; manual review recommended",
            rule=name,
            adjacency="JOINED_LITERALS",
            **origin,
        )


# Where each revision's cross-reference section starts: the file's final
# startxref names the newest; each section's trailer /Prev names the one
# before it. Every revision also writes its own startxref, so those offsets
# are collected too (a broken /Prev link then loses nothing).
_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)")
_OBJ_HEADER_RE = re.compile(rb"\s*\d+\s+\d+\s+obj\b")
_PREV_RE = re.compile(rb"/Prev\s+(\d+)")
MAX_EARLIER_REVISIONS = 50


def _dict_end(raw: bytes, start: int) -> int | None:
    """Offset just past the PDF dictionary opening at raw[start] ('<<'),
    skipping nested dictionaries, hex strings and literal strings."""
    depth, i, n = 0, start, len(raw)
    while i < n:
        if raw.startswith(b"<<", i):
            depth, i = depth + 1, i + 2
        elif raw.startswith(b">>", i):
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i
        elif raw[i] == 0x3C:                         # '<' hex string
            close = raw.find(b">", i + 1)
            i = n if close < 0 else close + 1
        elif raw[i] == 0x28:                         # '(' literal string
            level, i = 1, i + 1
            while i < n and level:
                if raw[i] == 0x5C:                   # backslash escape
                    i += 1
                elif raw[i] == 0x28:
                    level += 1
                elif raw[i] == 0x29:
                    level -= 1
                i += 1
        else:
            i += 1
    return None


_INT_PAIR_RE = re.compile(rb"^\s*(\d+)\s+(\d+)\s*$")
_INDEX_RE = re.compile(rb"/Index\s*\[([\d\s]*)\]")
_SIZE_RE = re.compile(rb"/Size\s+(\d+)")


def _xref_section(
    raw: bytes, offset: int
) -> tuple[int, int | None, list[range] | None] | None:
    """(end, prev, objects) for the cross-reference section at *offset*: the
    byte just past its trailer (classic table) or stream object (PDF 1.5+),
    its /Prev offset, and the object-number ranges it defines (None when
    they cannot be read). None when *offset* holds no section."""
    if not 0 <= offset < len(raw):
        return None
    if raw.startswith(b"xref", offset):
        trailer = raw.find(b"trailer", offset)
        opening = raw.find(b"<<", trailer) if trailer >= 0 else -1
        end = _dict_end(raw, opening) if opening >= 0 else None
        if end is None:
            return None
        dictionary = raw[opening:end]
        # Subsection headers are the lines holding exactly two integers.
        objects: list[range] | None = [
            range(int(m.group(1)), int(m.group(1)) + int(m.group(2)))
            for m in map(_INT_PAIR_RE.match, raw[offset + 4:trailer].splitlines())
            if m
        ]
    elif _OBJ_HEADER_RE.match(raw, offset):
        opening = raw.find(b"<<", offset)
        dict_end = _dict_end(raw, opening) if opening >= 0 else None
        if dict_end is None or b"/XRef" not in raw[opening:dict_end]:
            return None
        dictionary = raw[opening:dict_end]
        close = raw.find(b"endobj", dict_end)
        end = close + len(b"endobj") if close >= 0 else None
        if end is None:
            return None
        index = _INDEX_RE.search(dictionary)
        size = _SIZE_RE.search(dictionary)
        if index:
            numbers = [int(n) for n in index.group(1).split()]
            objects = [range(a, a + c) for a, c in zip(numbers[::2], numbers[1::2])]
        elif size:
            objects = [range(0, int(size.group(1)))]
        else:
            objects = None
    else:
        return None
    prev = _PREV_RE.search(dictionary)
    return end, (int(prev.group(1)) if prev else None), objects


def _earlier_revisions(raw: bytes) -> list[tuple[int, int, set[int] | None]]:
    """(xref offset, section end, changed later) for every revision before
    the current one, oldest first. *changed later* is the set of object
    numbers some newer section redefines or frees — the only objects whose
    earlier version can differ from the current one (None if unknown). Boundaries come from the file's own cross-reference
    chain — never from '%%EOF' bytes, which can occur inside a stream (an
    attached PDF) and which an incremental writer may omit."""
    starts = [int(m.group(1)) for m in _STARTXREF_RE.finditer(raw)]
    if not starts:
        return []
    current = starts[-1]
    sections: dict[int, tuple[int, list[range] | None]] = {}
    pending = [current, *starts]
    while pending:
        offset = pending.pop()
        if offset in sections:
            continue
        found = _xref_section(raw, offset)
        if found is None:
            continue
        end, prev, objects = found
        if prev is not None:
            pending.append(prev)
            if prev > offset and offset != current:
                # A /Prev pointing forward marks a linearized file's
                # first-page section: part of the same revision as the
                # main section it points to, not an earlier revision.
                continue
        sections[offset] = (end, objects)
    earlier = []
    for offset, (end, _) in sorted(sections.items()):
        if offset == current:
            continue
        changed: set[int] | None = set()
        # Newer sections are written after this one in the file.
        for other, (_, objects) in sections.items():
            if other <= offset:
                continue
            if objects is None:
                changed = None
                break
            for numbers in objects:
                changed.update(numbers)
        earlier.append((offset, end, changed))
    return earlier


def scan_earlier_revisions(
    pdf_path: Path,
    doc: fitz.Document,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Scan the superseded object versions an incremental update left behind.

    An incremental save appends changes after the previous revision; the
    earlier revision — including any object the update rewrote under the
    same number — stays in the bytes, but PyMuPDF and qpdf read only the
    newest. Each earlier revision is rebuilt by cutting the file just past
    its own cross-reference section, and every object whose content
    differs from the current version is scanned as leftover content.
    """
    try:
        raw = Path(pdf_path).read_bytes()
    except OSError as exc:
        report.warn(
            "REVISION_SCAN_FAILED",
            "Objects",
            f"Objects: file unreadable for the earlier-revision scan ({exc}) — "
            "earlier revisions NOT scanned",
            storage="superseded",
        )
        return
    revisions = _earlier_revisions(raw)
    if len(revisions) > MAX_EARLIER_REVISIONS:
        report.warn(
            "REVISION_CAP",
            "Objects",
            f"Objects: {len(revisions)} earlier revisions; only the original and "
            f"the latest {MAX_EARLIER_REVISIONS - 1} were scanned",
            storage="superseded",
        )
        # The first revision is where a pre-redaction original lives.
        revisions = revisions[:1] + revisions[-(MAX_EARLIER_REVISIONS - 1):]
    tally = _ObjectTally()
    current: dict[int, str] = {}           # current sources, read once
    for number, (offset, end, changed) in enumerate(revisions, start=1):
        prefix = raw[:end] + b"\nstartxref\n%d\n%%%%EOF\n" % offset
        try:
            old = fitz.open("pdf", prefix)
        except Exception:
            old = None
        if old is None or old.is_repaired:
            # A revision that only opens by repair would have its objects
            # guessed from raw bytes (an attached PDF's included): report
            # it as unread rather than scan invented content.
            if old is not None:
                old.close()
            report.warn(
                "REVISION_UNREADABLE",
                "Objects",
                f"Objects: earlier revision {number} could not be read cleanly — "
                "NOT scanned; manual review recommended",
                storage="superseded",
                revision=number,
            )
            continue
        try:
            candidates = range(1, old.xref_length()) if changed is None else \
                sorted(x for x in changed if 0 < x < old.xref_length())
            for xref in candidates:
                try:
                    source = old.xref_object(xref, compressed=True)
                except Exception:
                    continue
                if not _superseded(old, doc, xref, source, current):
                    continue
                _scan_object(
                    old,
                    xref,
                    source,
                    f"earlier revision {number}, object {xref}",
                    leftover=True,
                    origin={"storage": "superseded", "object": xref, "revision": number},
                    matcher=matcher,
                    patterns=patterns,
                    report=report,
                    tally=tally,
                )
        finally:
            old.close()
    tally.report_to(report, leftover_kind="superseded", storage="superseded")


def _superseded(old: fitz.Document, doc: fitz.Document, xref: int, source: str,
                current: dict[int, str]) -> bool:
    """Whether an earlier revision's version of *xref* differs from the
    current one (or the object is gone) — i.e. content only the earlier
    revision still holds. Streams are compared as stored (undecoded), so
    unchanged objects cost no decompression."""
    try:
        if xref >= doc.xref_length():
            return True
        if xref not in current:
            current[xref] = doc.xref_object(xref, compressed=True)
        if current[xref] != source:
            return True
        if old.xref_is_stream(xref):
            return old.xref_stream_raw(xref) != doc.xref_stream_raw(xref)
    except Exception:
        return True
    return False


def _collect_qpdf(
    proc: subprocess.Popen[bytes],
    matcher: SecretMatcher,
    report: ScanReport,
) -> None:
    """Sweep qpdf's QDF byte stream as the damaged-file backstop.

    Literal decoding happens structurally in scan_pdf_objects; this pass
    exists for what an object walk cannot: qpdf can recover objects from
    a damaged or unusually-chained xref that PyMuPDF's table walk misses.

    Matches here are always manual-review warnings, never hard findings:
    the stream fuses adjacent numeric operands and compressed binary, so
    a match can be coincidence. Pattern rules are deliberately not run
    over it at all, for the same reason — value secrets only. A secret
    the structural pass already reported as a hard finding is suppressed
    here, so a confirmed leak is not restated as a possible coincidence.
    """
    raw = RollingScanner(matcher)
    got_output = False
    deadline = time.monotonic() + SUBPROCESS_TIMEOUT_S
    if proc.stdout is None:
        report.warn(
            "TOOL_NO_OUTPUT",
            "Binary",
            "Binary: qpdf stdout unavailable — layer NOT scanned",
            tool="qpdf",
        )
        return

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            proc.communicate()
            report.warn(
                "TOOL_TIMEOUT", "Binary", "Binary: qpdf timed out — scan incomplete", tool="qpdf"
            )
            break
        # Use select() so the deadline is enforced even if qpdf stalls
        # mid-stream — a plain read() would block indefinitely.
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            proc.kill()
            proc.communicate()
            report.warn(
                "TOOL_TIMEOUT", "Binary", "Binary: qpdf timed out — scan incomplete", tool="qpdf"
            )
            break
        chunk = os.read(proc.stdout.fileno(), QPDF_CHUNK_BYTES)
        if not chunk:
            break
        got_output = True
        text = chunk.decode("latin-1")  # 1:1 byte mapping, nothing lost
        raw.feed(normalize_string(text))

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
        report.warn(
            "TOOL_EXIT_NONZERO",
            "Binary",
            f"Binary: qpdf exited {proc.returncode} — QDF output may be truncated, "
            "binary scan may be incomplete",
            tool="qpdf",
            returncode=proc.returncode,
        )
    if not got_output:
        report.warn(
            "TOOL_NO_OUTPUT",
            "Binary",
            "Binary: qpdf produced no output — layer NOT scanned",
            tool="qpdf",
        )
        return

    # Every raw-stream match is a warning: the structural pass owns hard
    # findings, and a match here may be a byte-level coincidence. Skip a
    # secret already confirmed as a hard finding — restating it as a
    # "possible coincidence" would contradict the verdict and re-add the
    # duplicate noise the old raw.found - literals.found dedup removed.
    already_found = {f.secret_name for f in report.findings}
    for secret in sorted(raw.found, key=lambda s: s.name):
        if secret.name in already_found:
            continue
        report.warn(
            "REVIEW_BINARY",
            "Binary",
            f"Binary: raw byte stream contains a sequence matching secret "
            f"{secret.name!r} — possibly a coincidental collision of numeric "
            "operands or binary data; manual review recommended",
            storage="live",
            rule=secret.name,
            adjacency="NOISY_SOURCE",
            tool="qpdf",
        )


def check_hidden_layers(
    procs: dict[str, subprocess.Popen[bytes] | None],
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Phase 4 collector: reap the tools started by start_hidden_tools.

    Each collector is isolated: one crashing must cost only its own
    coverage, so a failure in exiftool collection cannot skip the qpdf
    backstop (they used to share a single guard in main() that killed
    both if either raised).
    """
    if procs["exiftool"] is not None:
        try:
            _collect_exiftool(procs["exiftool"], matcher, patterns, report)
        except Exception as exc:
            report.warn(
                "LAYER_CRASHED",
                "Metadata",
                f"Metadata: collection crashed ({exc}) — NOT fully scanned",
                tool="exiftool",
            )
    if procs["qpdf"] is not None:
        try:
            _collect_qpdf(procs["qpdf"], matcher, report)
        except Exception as exc:
            report.warn(
                "LAYER_CRASHED",
                "Binary",
                f"Binary: collection crashed ({exc}) — NOT fully scanned",
                tool="qpdf",
            )


# ──────────────────────────────────────────────────────────────────────────
# PHASE 5: The CLI Orchestrator
# ──────────────────────────────────────────────────────────────────────────
# The redactor's entity types that have a regex equivalent here. The value
# is one of THIS tool's built-in classes: verification deliberately uses
# its own regexes and validators rather than importing the redactor's, so
# a flaw in the redactor's detection cannot hide itself from the check.
# The full entity-type roster the redactor supports (its EntityType enum).
# Declared explicitly so an unknown string is distinguishable from a known
# coverage gap: a typo must be rejected, not reported as "LLM-only".
UPSTREAM_ENTITY_TYPES: frozenset[str] = frozenset({
    "person_name", "ssn", "email", "phone", "address", "date_of_birth",
    "account_number", "credit_card", "drivers_license", "passport",
})

# The redactor's entity types that have a regex equivalent here. The value is
# one of THIS tool's built-in classes: verification deliberately uses its
# own regexes and validators rather than importing the redactor's, so a
# flaw in the redactor's detection cannot hide itself from the check.
ENTITY_TYPE_TO_CLASS: dict[str, str] = {
    "ssn": "ssn",
    "email": "email",
    "phone": "us-phone",
    "credit_card": "credit-card",
}

# Mapped types whose class covers only PART of what the redactor means by
# that name. Having a regex for a name is not the same as covering the
# name, so these raise the same scope warning an unmapped type does.
PARTIAL_ENTITY_COVERAGE: dict[str, str] = {
    "phone": "only North American (NANP) numbers are checked; "
             "international formats are not",
}

# Top-level keys the redactor itself understands. Anything else is a typo or
# an upstream addition; either way the section it names is not scanned, so
# it is surfaced rather than silently dropped.
UPSTREAM_CONFIG_KEYS: frozenset[str] = frozenset({
    "entity_types", "exact_values", "patterns",
    "backend", "model", "llm_url", "ollama_url", "scrub_metadata",
})

# These tables are edited for different reasons and live far apart; an
# unguarded subscript would turn a rename into a KeyError that exits 1,
# this tool's code for "secret detected".
assert set(ENTITY_TYPE_TO_CLASS) <= UPSTREAM_ENTITY_TYPES
assert set(ENTITY_TYPE_TO_CLASS.values()) <= set(BUILTIN_PATTERN_CLASSES)
assert set(PARTIAL_ENTITY_COVERAGE) <= set(ENTITY_TYPE_TO_CLASS)


def _make_value_rule(name: str, spec: str, label: str = "") -> Secret:
    """Build a value rule, shared by both rule formats so the guards
    cannot drift apart (they already did once: the JSON path rejected a
    non-string spec while the YAML path coerced it, which is how YAML
    implicit typing silently changed what was searched for)."""
    normalized = normalize_string(spec)
    if not normalized:
        raise VerifyError(
            f"{label or name} normalizes to an empty string — it would "
            "match everything or nothing; refusing to scan"
        )
    return Secret(name, normalized)


def _make_pattern_rule(
    name: str, spec: str, flags: int, label: str = ""
) -> PatternRule:
    """Build a custom-regex rule, shared by both rule formats."""
    try:
        regex = re.compile(spec, flags)
    except re.error as exc:
        raise VerifyError(f"{label or name}: invalid regex: {exc}")
    if regex.match(""):
        raise VerifyError(
            f"{label or name}: pattern matches the empty string; "
            "refusing to scan"
        )
    return PatternRule(name, regex)


def _make_class_rule(
    name: str, class_name: str, label: str = ""
) -> PatternRule:
    """Build a built-in class rule, shared by both rule formats."""
    if class_name not in BUILTIN_PATTERN_CLASSES:
        raise VerifyError(
            f"{label or name}: unknown class {class_name!r} — valid "
            f"classes: {', '.join(sorted(BUILTIN_PATTERN_CLASSES))}"
        )
    regex_src, validator = BUILTIN_PATTERN_CLASSES[class_name]
    return PatternRule(name, re.compile(regex_src), validator)


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
    # Problems with the rules file that do not stop the scan but make a
    # clean result untrustworthy (surfaced as warnings -> exit 2).
    warnings: list[str] = field(default_factory=WarnList)

    def warn(self, code: str, layer: str, message: str, **fields: Any) -> None:
        self.warnings.append(Warn(code, layer, message, **fields))


def load_rules(rules_path: Path) -> RuleSet:
    """Load verification rules from a JSON rules file or a YAML config.

    A '.yaml'/'.yml' suffix selects the redactor's redact_config format,
    so one file can drive both redaction and verification; anything else
    is parsed as this tool's native JSON rules array.
    """
    if rules_path.suffix.lower() in (".yaml", ".yml"):
        return _load_rules_yaml(rules_path)
    return _load_rules_json(rules_path)


def _yaml_section(raw: dict, key: str) -> list:
    """Read a list-valued section, distinguishing absent from malformed.

    `raw.get(key) or []` would collapse {} and '' into an empty section
    before any type check ran, silently narrowing the scan instead of
    refusing it.
    """
    if key not in raw:
        return []
    value = raw[key]
    if value is None:           # `key:` with nothing under it
        return []
    if not isinstance(value, list):
        raise VerifyError(f"{key} must be a list, got {type(value).__name__}")
    return value


def _yaml_coercion_kind(value: Any) -> str:
    """Name the TYPE a coercing YAML loader read a scalar as — never the
    value itself.

    Used only by the RULES_UNQUOTED_VALUE warning. That warning used to
    show mask() of both the coerced value and the literal spec; since the
    coerced value is a deterministic function of the whole spec, the two
    masked tails together narrow a short secret by orders of magnitude
    (see #2). Saying only the kind ("a number", "a boolean", ...) carries
    no digit or character of the value.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):        # bool is an int subclass: check first
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return "a date"
    return f"a {type(value).__name__}"


def _load_rules_yaml(rules_path: Path) -> RuleSet:
    """Load a redactor's redact_config.yaml as verification rules.

    Mapping:
      exact_values -> value rules (normalized matching)
      patterns     -> pattern rules, compiled IGNORECASE exactly as the
                      redactor compiles them
      entity_types -> this tool's own built-in classes; types with no
                      regex equivalent, or only partial coverage, are
                      recorded so the scope gap is reported

    Keys the redactor needs but verification does not (backend, model,
    llm_url, ollama_url, scrub_metadata) are ignored.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - declared dependency
        raise VerifyError("reading a YAML config needs PyYAML: pip install pyyaml")

    # YAML's implicit typing is actively dangerous for secrets: unquoted
    # 00123456 is octal int 42798, 1.50 is float 1.5, `yes` is True.
    # Coercing those back with str() makes the tool search for a string
    # the user never wrote. Drop the coercing scalar resolvers so plain
    # scalars stay literal — but KEEP the merge resolver, or `<<: *anchor`
    # silently stops merging and whole sections vanish from the scan.
    _MERGE = "tag:yaml.org,2002:merge"

    class RawScalars(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):  # type: ignore[override]
            # PyYAML keeps the LAST of duplicate keys without complaint,
            # which silently discards an entire earlier section.
            seen_keys: set[str] = set()
            for key_node, _ in node.value:
                key = key_node.value
                if isinstance(key, str) and key in seen_keys:
                    raise VerifyError(f"duplicate key {key!r} in {rules_path}")
                if isinstance(key, str):
                    seen_keys.add(key)
            return super().construct_mapping(node, deep)

    RawScalars.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag == _MERGE]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    try:
        text = rules_path.read_text(encoding="utf-8")
        raw: Any = yaml.load(text, RawScalars)
        coerced: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML's message quotes the offending line — which in a rules
        # file is a secret. Report the problem and position only.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or type(exc).__name__
        raise VerifyError(
            f"Cannot read rules file {rules_path}: invalid YAML{where}: " f"{problem}"
        )
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: a non-UTF-8 config is an
        # operational error (exit 2), never the leak code.
        raise VerifyError(f"Cannot read rules file {rules_path}: {exc}")
    if not isinstance(raw, dict):
        raise VerifyError("a YAML rules file must be a mapping")

    rules = RuleSet()

    unknown_keys = sorted(set(raw) - UPSTREAM_CONFIG_KEYS)
    if unknown_keys:
        rules.warn(
            "RULES_UNKNOWN_KEY",
            "Rules",
            f"Rules: unrecognized config key(s) {', '.join(unknown_keys)} — "
            "if one is a misspelled section its rules were NOT scanned",
        )

    def literal(section: str, i: int, value: Any) -> str:
        """Every rule spec must be literal text. A non-string survived
        YAML's typing (an explicit !!int tag, or a nested mapping), which
        means the tool would search for something other than what is
        written in the file."""
        if not isinstance(value, str):
            raise VerifyError(
                f"{section}[{i}] must be a quoted string, got "
                f"{type(value).__name__} — quote it in the config"
            )
        return value

    # A redactor reading this file with a plain safe_load sees coerced
    # values and removes THOSE strings, so divergence means the two tools
    # are working from different text. The warning names only the TYPE
    # YAML coerced the value to, never a masked form of either value: the
    # coerced value is a deterministic function of the whole literal spec,
    # so showing mask() of both together narrowed a short secret by orders
    # of magnitude (#2) — the warning must not itself leak what it is
    # warning about.
    coerced_values = (coerced or {}).get("exact_values") or []
    values = _yaml_section(raw, "exact_values")
    for i, value in enumerate(values):
        spec = literal("exact_values", i, value)
        if i < len(coerced_values) and str(coerced_values[i]) != spec:
            rules.warn(
                "RULES_UNQUOTED_VALUE",
                "Rules",
                f"Rules: exact_values[{i}] is unquoted, so YAML reads it as "
                f"{_yaml_coercion_kind(coerced_values[i])} rather than the "
                "literal text written — a redactor sharing this file may "
                "have removed the wrong string. Quote the value in the "
                "config.",
            )
        rules.secrets.append(_make_value_rule(f"exact_values[{i}]", spec))

    for i, value in enumerate(_yaml_section(raw, "patterns")):
        spec = literal("patterns", i, value)
        if f"{spec} #" in text:
            rules.warn(
                "RULES_TRUNCATED_PATTERN",
                "Rules",
                f"Rules: patterns[{i}] appears to be truncated at an "
                "unquoted '#' (YAML comment) — quote the pattern",
            )
        # IGNORECASE only, matching the redactor's own _compile_patterns;
        # adding MULTILINE here would make a shared rule mean different
        # things in the two tools.
        rule = _make_pattern_rule(f"patterns[{i}]", spec, re.IGNORECASE)
        if rule.regex.groups:
            rules.warn(
                "RULES_CAPTURING_GROUP",
                "Rules",
                f"Rules: patterns[{i}] has a capturing group — the redactor "
                "removes only the group text, so this rule verifies more "
                "than it removed; manual review recommended",
            )
        rules.patterns.append(rule)

    # Upstream defaults entity_types to the FULL roster when the key is
    # absent, so treating absent as empty would certify clean a document
    # whose ten redacted categories were never searched for.
    if "entity_types" in raw:
        entity_types = _yaml_section(raw, "entity_types")
    else:
        entity_types = sorted(UPSTREAM_ENTITY_TYPES)
    for i, entity in enumerate(dict.fromkeys(str(e) for e in entity_types)):
        if entity not in UPSTREAM_ENTITY_TYPES:
            # Not echoed: a value pasted under the wrong key is a secret.
            raise VerifyError(
                f"entity_types[{i}] is not a known entity type — valid types: "
                f"{', '.join(sorted(UPSTREAM_ENTITY_TYPES))}"
            )
        mapped = ENTITY_TYPE_TO_CLASS.get(entity)
        if mapped is None:
            rules.unverifiable.append(entity)
            continue
        rules.patterns.append(_make_class_rule(f"entity_types:{entity}", mapped))
        if entity in PARTIAL_ENTITY_COVERAGE:
            rules.warn(
                "SCOPE_PARTIAL_ENTITY",
                "Rules",
                f"Scope: entity type {entity!r} is only partly verifiable — "
                f"{PARTIAL_ENTITY_COVERAGE[entity]}",
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
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # ValueError covers UnicodeDecodeError: a non-UTF-8 rules file is
        # an operational error (exit 2), never the leak code.
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
            secrets.append(_make_value_rule(name, spec, f"secret {name!r}"))
        elif kind == "pattern":
            # MULTILINE so grep-style ^/$ anchors match per line of the
            # extracted page text instead of silently never matching.
            patterns.append(
                _make_pattern_rule(name, spec, re.MULTILINE, f"rule {name!r}")
            )
        else:
            patterns.append(_make_class_rule(name, spec, f"rule {name!r}"))

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


def build_json_report(
    report: ScanReport,
    exit_code: int,
    error: tuple[str, str] | None = None,
    *,
    target: Path,
    private_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """The machine-readable report: the same content as print_report,
    masked and sanitized the same way, plus stable codes and structured
    fields (layer, storage class, object, revision, page, rule, adjacency,
    tool) so consumers never parse message wording.

    Every field is always present (null when it does not apply), and
    findings and warnings are sorted, so the output is identical across
    runs. Paths given on the command line appear only as base names.
    """

    def text(value: str) -> str:
        for path in private_paths:
            for form in {str(path), str(path.resolve())}:
                if form not in (".", ""):
                    value = value.replace(form, path.name)
        return _sanitize_report_text(value)

    findings = sorted(
        (
            {
                "layer": f.layer,
                "rule": text(f.secret_name),
                "tier": "hard",
                "storage": f.storage,
                "object": f.object,
                "revision": f.revision,
                "page": f.page,
                "location": text(f.location),
                "sample": text(mask(f.sample)) if f.sample else "",
            }
            for f in report.findings
        ),
        key=lambda d: json.dumps(d, sort_keys=True),
    )
    warnings = []
    for w in report.warnings:
        coded = isinstance(w, Warn)
        fields = w.fields if coded else {}
        entry: dict[str, Any] = {
            "code": w.code if coded else "UNCODED",
            "kind": w.kind if coded else "coverage",
            "layer": w.layer if coded else None,
        }
        for name in WARNING_FIELDS:
            value = fields.get(name)
            entry[name] = text(value) if isinstance(value, str) else value
        entry["message"] = text(w)
        warnings.append(entry)
    warnings.sort(key=lambda d: json.dumps(d, sort_keys=True))
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool": {"name": "pdf-redaction-verifier", "version": __version__},
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "pymupdf": fitz.VersionBind,
            "ocr_available": _OCR_IMPORTS_OK,
        },
        "target": text(target.name),
        "exit_code": exit_code,
        "verdict": {0: "pass", 1: "fail", 2: "uncertified"}[exit_code],
        "error": ({"code": error[0], "message": text(error[1])} if error else None),
        "findings": findings,
        "warnings": warnings,
    }


def write_json_report(path: Path, data: dict[str, Any]) -> bool:
    """Write the JSON report atomically — to a temporary file beside it,
    then renamed over it — so a reader never sees a partial report.
    False (and a stderr note) if it cannot be written."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        # Private (0600), never through a symlink, never over an existing
        # file; the rename then replaces a link at *path*, not its target.
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "wb") as out:
            out.write(payload)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(
            f"[ERROR] Cannot write JSON report {path.name}: {exc.strerror or exc}", file=sys.stderr
        )
        return False
    return True


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
    except OSError:
        return False


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
        description="Forensic PDF verification: detect secrets across Text, "
        "OCR, metadata, binary-stream and hidden-object layers.",
    )
    parser.add_argument("--target", required=True, type=Path, help="PDF file to verify")
    parser.add_argument(
        "--secrets", required=True, type=Path,
        help="rules file: a JSON array of rules (name + one of value / "
        "pattern / class, classes being "
        f"{', '.join(sorted(BUILTIN_PATTERN_CLASSES))}), or a redactor's "
        "redact_config.yaml when the path ends in .yaml/.yml",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop scanning as soon as any secret is found (default: scan "
        "every layer for a complete forensic report)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="FILE",
        help="also write a machine-readable report to FILE (stable warning "
        "codes, masked samples); the exit code is unchanged, except that a "
        "clean result becomes 2 if FILE cannot be written",
    )
    # --version works only on its own. As an argparse "version" action it
    # would also fire alongside --target (or abbreviated, as --v) and exit
    # 0 without scanning: the certified-clean code for a scan never run.
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args == ["--version"]:
        print(f"{parser.prog} {__version__}")
        return 0
    args = parser.parse_args(raw_args)

    pdf_path: Path = args.target
    report = ScanReport()
    json_path: Path | None = args.json

    def finish(code: int, error: tuple[str, str] | None = None) -> int:
        if error:
            print(f"[ERROR] {error[1]}", file=sys.stderr)
        if json_path is not None:
            data = build_json_report(
                report, code, error, target=pdf_path, private_paths=(pdf_path, args.secrets)
            )
            if not write_json_report(json_path, data) and code == 0:
                # A caller that asked for the JSON report gates on it; a
                # missing report must not read as a certified pass.
                print(
                    "\n  The JSON report could not be written — this is NOT a "
                    "certified clean result (exit code 2)."
                )
                return 2
        return code

    def crash(exc: BaseException) -> int:
        """Any exception (or other BaseException — a SystemExit raised
        inside a layer must not carry its own arbitrary code out) that
        escapes the scan pipeline below — a bug in print_report,
        start_hidden_tools, the JSON writer, or anywhere else not already
        caught per-layer — must never be certified clean, and (unless a
        secret was already confirmed — see below) must exit 2, never 1: a
        secret was NOT necessarily found, so 1 (the leak code) would
        overstate it. The previous behaviour let it propagate uncaught,
        which under run_cli exited 1 on a document where nothing had
        actually been found (see #1).

        This project's severity order is 0 < 2 < 1: a confirmed leak
        outranks "cannot certify". So if report.leaked is already true —
        a hard finding was recorded before the crash — the verdict stays
        1, "fail", never downgraded to 2 just because the scan could not
        finish; the JSON report still carries error.code = INTERNAL_ERROR
        so a consumer can see the scan was cut short.

        The message is the exception's TYPE plus a truncated str(), never
        interpolated document content: an uncaught exception is exactly
        the place a raw string from inside the PDF could otherwise reach
        the report unmasked.
        """
        detail = f"{type(exc).__name__}: {exc}"[:500]
        code = 1 if report.leaked else 2
        try:
            return finish(code, ("INTERNAL_ERROR", f"internal error: {detail}"))
        except Exception:
            # finish() itself failed (building or writing the JSON report
            # is exactly the kind of thing #1 was raised about) — fall
            # back to the bare minimum so this can never re-raise and
            # exit 1 by accident when nothing was actually found.
            print(f"[ERROR] internal error: {detail}", file=sys.stderr)
            return code

    try:
        if json_path is not None:
            if _same_file(json_path, pdf_path) or _same_file(json_path, args.secrets):
                json_path = None  # never overwrite an input with the report
                return finish(
                    2, ("JSON_PATH_CONFLICT", "--json must not name the target PDF or the rules file")
                )
            # A report left by an earlier run must not survive a run that ends
            # before writing its own (a crash, a kill): no report means no
            # verdict, never the previous one.
            try:
                json_path.unlink()
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                pass  # the write will fail -> never a pass
            except OSError as exc:
                # The old report cannot be removed, so it could outlive this
                # run and be read as its verdict. Refuse before scanning.
                bad = json_path
                json_path = None
                return finish(
                    2,
                    (
                        "JSON_PATH_UNWRITABLE",
                        f"cannot replace JSON report {bad.name}: " f"{exc.strerror or exc}",
                    ),
                )

        if not pdf_path.is_file():
            return finish(2, ("TARGET_NOT_FOUND", f"Target PDF not found: {pdf_path}"))

        try:
            rules = load_rules(args.secrets)
        except VerifyError as exc:
            return finish(2, ("RULES_INVALID", str(exc)))
        secrets, patterns = rules.secrets, rules.patterns

        matcher = SecretMatcher(secrets)

        # Scope, not coverage: a shared redaction config can ask for entity
        # types this tool has no way to search for. Say so — a PASS that
        # silently skipped a category would be the false assurance the whole
        # exit-code contract exists to prevent.
        report.warnings.extend(rules.warnings)

        if rules.unverifiable:
            report.warn(
                "SCOPE_UNVERIFIABLE",
                "Rules",
                "Scope: the config asks a redactor to remove "
                f"{', '.join(sorted(set(rules.unverifiable)))} — these are "
                "identified by LLM judgement and have no regex equivalent, so "
                "this tool CANNOT verify they were removed",
            )

        # Start the independent subprocess layers now; they run concurrently
        # behind the in-process Text/OCR scans. procs/tools_scratch default
        # to "nothing started" so the outer finally below is always safe to
        # run, even if start_hidden_tools itself never returns normally.
        procs: dict[str, subprocess.Popen[bytes] | None] = {"exiftool": None, "qpdf": None}
        tools_scratch: tempfile.TemporaryDirectory[str] | None = None
        try:
            procs, tools_scratch = start_hidden_tools(pdf_path, report)

            try:
                doc = fitz.open(pdf_path)
            except Exception as exc:
                return finish(2, ("PDF_UNREADABLE", f"Cannot open PDF {pdf_path}: {exc}"))

            try:
                if doc.needs_pass:
                    return finish(2, ("PDF_PASSWORD", f"PDF is password-protected: {pdf_path}"))

                if doc.page_count == 0:
                    report.warn(
                        "EMPTY_DOCUMENT",
                        "Document",
                        "PDF contains zero pages — Text/OCR content layers cannot "
                        "scan an empty document",
                    )

                print(f"[*] Scanning {pdf_path.name} ({doc.page_count} page(s)) "
                      f"for {len(secrets)} secret(s) and {len(patterns)} pattern rule(s)...")

                # Each layer is independent, so one crashing must cost only its
                # own coverage — as a warning, which forces exit 2. Letting it
                # propagate printed a traceback and exited 1, the leak code, on
                # a document nothing had been found in.
                for layer, scan in (
                    ("Metadata", scan_xmp_metadata),
                    ("Hidden", scan_hidden_objects),
                    ("Objects", scan_pdf_objects),
                    ("Objects", functools.partial(scan_earlier_revisions, pdf_path)),
                ):
                    try:
                        scan(doc, matcher, patterns, report)
                    except Exception as exc:
                        report.warn(
                            "LAYER_CRASHED", layer, f"{layer}: layer crashed ({exc}) — NOT fully scanned"
                        )

                print("[*] Phase 2: Text layer (layout-aware visual text)...")
                try:
                    scan_page_layer(
                        doc, matcher, report,
                        layer="Text", extractor=extract_visual_text, note="visual text layer",
                        hard_variants=TEXT_GENUINE_READINGS,
                        patterns=patterns, fail_fast=args.fail_fast,
                    )
                except Exception as exc:
                    report.warn("LAYER_CRASHED", "Text", f"Text: layer crashed ({exc}) — NOT fully scanned")

                if args.fail_fast and report.leaked:
                    report.warn(
                        "SKIPPED_FAIL_FAST", "OCR", "OCR: skipped (--fail-fast after earlier finding)"
                    )
                elif not _OCR_IMPORTS_OK:
                    report.warn(
                        "OCR_UNAVAILABLE",
                        "OCR",
                        "OCR: PyObjC Vision bridge not available — visual layer NOT scanned "
                        "(uv pip install pyobjc-framework-Vision pyobjc-framework-Quartz; macOS only)",
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
                        report.warn(
                            "LAYER_CRASHED", "OCR", f"OCR: layer crashed ({exc}) — NOT fully scanned"
                        )
            finally:
                doc.close()

            if args.fail_fast and report.leaked:
                report.warn(
                    "SKIPPED_FAIL_FAST",
                    "Metadata/Binary",
                    "Metadata/Binary: skipped (--fail-fast after earlier finding)",
                )
            else:
                print("[*] Phase 4: Metadata + binary-stream layers (exiftool / qpdf)...")
                try:
                    check_hidden_layers(procs, matcher, patterns, report)
                except Exception as exc:
                    report.warn(
                        "LAYER_CRASHED",
                        "Metadata/Binary",
                        f"Metadata/Binary: collection crashed ({exc}) — NOT fully scanned",
                    )

            print_report(report, pdf_path)

            if report.leaked:
                return finish(1)
            if report.degraded:
                return finish(2)  # clean-so-far, but not certifiable as a real PASS
            return finish(0)
        finally:
            # Guaranteed on every exit from the block above — a normal
            # return, an early return, or ANY exception (including a
            # KeyboardInterrupt raised while blocked on a subprocess read,
            # which the per-layer `except Exception` guards above do not
            # catch) — so qpdf/exiftool are never left running as orphans
            # once main() has decided to leave, and the private scratch
            # directory they ran in (see start_hidden_tools) never
            # outlives them. Both calls are idempotent.
            #
            # Each is best-effort: if a `return finish(...)` above already
            # decided the verdict, print_report has already printed it —
            # letting a cleanup failure raise past this `finally` would
            # replace that already-computed, already-PRINTED verdict with
            # a fresh crash (caught by the `except BaseException` below),
            # silently turning a genuine 0 or 1 into a spurious 2. A
            # cleanup problem is real and worth knowing about, so it is
            # still reported — just never allowed to overrule the verdict.
            try:
                kill_hidden_tools(procs)
            except Exception as exc:
                print(
                    f"[ERROR] could not clean up a hidden-tool subprocess: {exc}",
                    file=sys.stderr,
                )
            if tools_scratch is not None:
                try:
                    tools_scratch.cleanup()
                except Exception as exc:
                    print(
                        f"[ERROR] could not remove the tool scratch directory: {exc}",
                        file=sys.stderr,
                    )
    except BaseException as exc:
        # BaseException, not Exception: a SystemExit (or other
        # BaseException) raised somewhere inside the pipeline above must
        # not carry its own arbitrary code out — it is exactly as
        # "escaped uncaught" as any other exception here (see #1).
        # argparse's own SystemExit (bad flags, --version combined with
        # other args) is unaffected: it is raised by parser.parse_args()
        # well before this try block starts.
        return crash(exc)


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
    try:
        code = main(argv)
    except SystemExit:
        # argparse's own error path (a missing/unknown flag, or --version
        # combined with other flags) already exits 2 (or 0 for a bare
        # --version) entirely on its own, before main()'s own guarded
        # pipeline even starts — let it propagate exactly as it always
        # has, rather than reinterpreting it as an internal crash below.
        raise
    except BaseException as exc:
        # BaseException, not Exception: main() already turns every
        # Exception (and KeyboardInterrupt, and any other BaseException,
        # such as a stray SystemExit) reaching its own guarded pipeline
        # into exit 2 — or 1 if a secret had already been confirmed — via
        # the `crash` helper inside it (#1). This is the process-boundary
        # backstop for anything that still escapes it (e.g. before that
        # pipeline's own try block starts), so a crash's exit code is
        # never mistaken for 1, the leak code, by accident.
        detail = f"{type(exc).__name__}: {exc}"[:500]
        print(f"[ERROR] internal error: {detail}", file=sys.stderr)
        code = 2
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
