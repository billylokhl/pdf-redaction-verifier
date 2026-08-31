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
from typing import Any, Callable, Sequence

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
    """A single leak: which secret surfaced in which layer, and where."""

    layer: str          # "DOM" | "OCR" | "Metadata" | "Binary"
    secret_name: str
    location: str


@dataclass
class ScanReport:
    """Aggregated results across all layers. Findings are unique."""

    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _seen: set[Finding] = field(default_factory=set, repr=False)

    def record(self, layer: str, secret_name: str, location: str) -> None:
        finding = Finding(layer, secret_name, location)
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
        self._pattern = re.compile("(?=(" + "|".join(map(re.escape, norms)) + "))")
        self.max_len: int = max(map(len, norms))

    def search(self, normalized_haystack: str) -> list[Secret]:
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
        lines.append("".join(g[4] for g in cluster))
    return "\n".join(lines)


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
    fail_fast: bool = False,
) -> None:
    """Run a per-page text extractor over the document and match secrets.

    Every extracted variant is searched per page; *each* variant is
    additionally streamed through its own RollingScanner so secrets that
    span a page boundary are still caught — including secrets in vertical
    or rotated text (reported without a page number).

    When *fail_fast* is True the scan stops after the first page that
    produces a finding, so the tool exits quickly on large documents.
    """
    cross_page_scanners: list[RollingScanner] = []
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
        if fail_fast and report.leaked:
            break

    found_in_layer = {f.secret_name for f in report.findings if f.layer == layer}
    for scanner in cross_page_scanners:
        for secret in scanner.found:
            if secret.name not in found_in_layer:
                report.record(layer, secret.name, f"across page boundaries ({note})")


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


def _collect_exiftool(
    proc: subprocess.Popen[bytes], matcher: SecretMatcher, report: ScanReport
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
        haystack = normalize_string(json.dumps(filtered, ensure_ascii=False))
    except json.JSONDecodeError:
        report.warnings.append(
            "Metadata: exiftool output was not valid JSON — scanned raw output, "
            "which includes filesystem fields (path collisions possible)"
        )
        haystack = normalize_string(text)

    for secret in matcher.search(haystack):
        report.record("Metadata", secret.name, "exiftool field sweep (XMP/Info/embedded)")


def _unescape_pdf_literal(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        esc = match.group(1)
        if esc and esc[0] in "01234567" and all(c in "01234567" for c in esc):
            return chr(int(esc, 8) & 0xFF)
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(esc, esc)

    return _LITERAL_ESCAPE_RE.sub(repl, body)


def _feed_pdf_strings(buf: str, scanner: RollingScanner) -> str:
    """Extract PDF string/hex literals from buf, feed them (in order, so
    adjacency across consecutive tokens is preserved), return the
    unconsumed tail as carry for the next chunk."""
    last_end = 0
    for match in _PDF_STRING_RE.finditer(buf):
        token = match.group(0)
        last_end = match.end()
        if token.startswith("("):
            scanner.feed(normalize_string(_unescape_pdf_literal(token[1:-1])))
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
    return buf[last_end:]


def _collect_qpdf(
    proc: subprocess.Popen[bytes], matcher: SecretMatcher, report: ScanReport
) -> None:
    """Stream qpdf's QDF output with bounded memory.

    Two scanners run over the stream: decoded string/hex literals give
    high-confidence findings; the raw fused byte stream is the recall
    backstop, but its matches can be coincidental collisions (adjacent
    numeric operands, compressed binary data), so raw-only matches are
    reported as manual-review warnings (exit 2), not hard findings.
    """
    raw = RollingScanner(matcher)
    literals = RollingScanner(matcher)
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
        carry = _feed_pdf_strings(carry + text, literals)
        if len(carry) > MAX_LITERAL_CARRY:
            carry = carry[-MAX_LITERAL_CARRY:]
            carry_truncated = True

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    # qpdf exit 3 means "warnings, but output is complete and usable" —
    # do not degrade the scan for benign recoverable issues.
    if proc.returncode not in (0, 3):
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

    for secret in sorted(literals.found, key=lambda s: s.name):
        report.record("Binary", secret.name, "qpdf QDF string/hex literals")
    for secret in sorted(raw.found - literals.found, key=lambda s: s.name):
        report.warnings.append(
            f"Binary: raw byte stream contains a sequence matching secret "
            f"{secret.name!r} — possibly a coincidental collision of numeric "
            "operands or binary data; manual review recommended"
        )


def check_hidden_layers(
    procs: dict[str, subprocess.Popen[bytes] | None],
    matcher: SecretMatcher,
    report: ScanReport,
) -> None:
    """Phase 4 collector: reap the tools started by start_hidden_tools."""
    if procs["exiftool"] is not None:
        _collect_exiftool(procs["exiftool"], matcher, report)
    if procs["qpdf"] is not None:
        _collect_qpdf(procs["qpdf"], matcher, report)


# ──────────────────────────────────────────────────────────────────────────
# PHASE 5: The CLI Orchestrator
# ──────────────────────────────────────────────────────────────────────────
def load_secrets(secrets_path: Path) -> list[Secret]:
    """Parse and normalize the secrets JSON file, validating its shape.

    Raises VerifyError for every operational problem so main can exit 2.
    """
    try:
        payload: Any = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"Cannot read secrets file {secrets_path}: {exc}")

    if not isinstance(payload, list):
        raise VerifyError("secrets.json must be a JSON array of objects")

    secrets: list[Secret] = []
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict) or "name" not in entry or "value" not in entry:
            raise VerifyError(
                f"secrets.json entry {i} must be an object with 'name' and 'value'"
            )
        value = entry["value"]
        if not isinstance(value, str):
            raise VerifyError(
                f"secrets.json entry {i} ({entry['name']!r}): 'value' must be "
                f"a string, got {type(value).__name__}"
            )
        normalized = normalize_string(value)
        if not normalized:
            raise VerifyError(
                f"secret {entry['name']!r} normalizes to an empty string — "
                "it would match everything or nothing; refusing to scan"
            )
        secrets.append(Secret(str(entry["name"]), normalized))

    if not secrets:
        raise VerifyError("secrets.json contains no secrets")
    return secrets


def print_report(report: ScanReport, pdf_path: Path) -> None:
    """Render the final verdict banner and per-finding detail."""
    bar = "=" * 70
    print(f"\n{bar}")
    if report.leaked:
        print(f"  [FAIL]  SENSITIVE DATA DETECTED IN: {pdf_path.name}")
        print(bar)
        for f in report.findings:
            print(f"  ✖ LAYER: {f.layer:<8} | SECRET: {f.secret_name!r:<24} | {f.location}")
    else:
        print(f"  [PASS]  No target secrets detected in: {pdf_path.name}")
    print(bar)

    if report.degraded:
        print("\n  ⚠ ATTENTION — warnings were raised during the scan:")
        for w in report.warnings:
            print(f"    - {w}")
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
        prog="verify.py",
        description="Forensic PDF verification: detect secrets across DOM, "
        "OCR, metadata, and binary-stream layers.",
    )
    parser.add_argument("--target", required=True, type=Path, help="PDF file to verify")
    parser.add_argument("--secrets", required=True, type=Path, help="JSON array of secrets")
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
        secrets = load_secrets(args.secrets)
    except VerifyError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    matcher = SecretMatcher(secrets)
    report = ScanReport()

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
              f"for {len(secrets)} secret(s)...")

        print("[*] Phase 2: DOM layer (layout-aware visual text)...")
        try:
            scan_page_layer(
                doc, matcher, report,
                layer="DOM", extractor=extract_visual_text, note="visual text layer",
                fail_fast=args.fail_fast,
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
            check_hidden_layers(procs, matcher, report)
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


if __name__ == "__main__":
    sys.exit(main())
