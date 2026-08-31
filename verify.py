#!/usr/bin/env python3
"""
verify.py — Forensic PDF Redaction Verification Suite.

Detects sensitive strings (secrets) inside a PDF across four independent
layers:

  1. DOM      — layout-aware text extraction (visual reading order), defeats
                out-of-order draw commands and per-character form boxes.
  2. OCR      — rasterize + Apple Vision (native, hardware-accelerated),
                defeats vector/outlined/image text.
  3. Metadata — exiftool sweep of XMP/Info/embedded metadata.
  4. Binary   — qpdf QDF normalization, exposes decompressed content streams,
                orphaned objects, and incremental-update leftovers.

All comparisons happen on *normalized* strings (alphanumeric-only,
lowercased), so formatting variations (dashes, slashes, spaces, newlines)
cannot hide a match.

Usage:
    python verify.py --target document.pdf --secrets secrets.json

Exit codes:
    0 — PASS: no secret found in any layer.
    1 — FAIL: at least one secret leaked in at least one layer.
    2 — ERROR: operational failure (bad input, missing tool) or a layer
        could not be scanned, so a clean result would not be trustworthy.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

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
# Vertical clustering tolerance (points) when grouping glyphs into visual
# lines. Chosen to tolerate slight baseline jitter in form boxes without
# merging adjacent lines of ~10pt text.
LINE_TOLERANCE_PT: float = 4.0

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ──────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Secret:
    """A named sensitive value plus its normalized search key."""

    name: str
    raw_value: str
    normalized: str


@dataclass(frozen=True)
class Finding:
    """A single leak: which secret surfaced in which layer, and where."""

    layer: str          # "DOM" | "OCR" | "Metadata" | "Binary"
    secret_name: str
    location: str       # e.g. "page 3" or "exiftool field sweep"


@dataclass
class ScanReport:
    """Aggregated results across all layers."""

    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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

    Strips ALL non-alphanumeric characters (spaces, dashes, slashes,
    periods, newlines, unicode punctuation, …) and lowercases, so that
    "123-45-6789", "123 45 6789", and "1 2 3\n4 5 6 7 8 9" all normalize
    to "123456789".
    """
    return _NON_ALNUM_RE.sub("", text.lower())


def find_secrets(normalized_haystack: str, secrets: Sequence[Secret]) -> list[Secret]:
    """Return every secret whose normalized value occurs in the haystack."""
    return [s for s in secrets if s.normalized and s.normalized in normalized_haystack]


# ──────────────────────────────────────────────────────────────────────────
# PHASE 2: Layout-Aware Text Extraction (DOM layer)
# ──────────────────────────────────────────────────────────────────────────
def extract_visual_text(page: fitz.Page) -> str:
    """Reconstruct page text in *visual* reading order.

    Standard extraction returns text in content-stream order, which fails
    when digits are drawn out of order (e.g. individual form boxes). This
    routine pulls every glyph with its bounding box via ``rawdict``,
    clusters glyphs into visual lines by vertical position (with tolerance
    for baseline jitter), then sorts each line left-to-right.
    """
    glyphs: list[tuple[float, float, float, str]] = []  # (y_center, x0, y0, char)

    raw: dict[str, Any] = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    c: str = char.get("c", "")
                    if not c or c.isspace():
                        continue
                    x0, y0, x1, y1 = char["bbox"]
                    glyphs.append(((y0 + y1) / 2.0, x0, y0, c))

    if not glyphs:
        return ""

    # Primary sort: vertical center (top-to-bottom).
    glyphs.sort(key=lambda g: g[0])

    # Cluster into visual lines: a glyph joins the current line while its
    # vertical center stays within tolerance of the line's running center.
    lines: list[list[tuple[float, float, float, str]]] = []
    current: list[tuple[float, float, float, str]] = [glyphs[0]]
    current_center: float = glyphs[0][0]

    for glyph in glyphs[1:]:
        if abs(glyph[0] - current_center) <= LINE_TOLERANCE_PT:
            current.append(glyph)
            # Running mean keeps the cluster stable against drift.
            current_center += (glyph[0] - current_center) / len(current)
        else:
            lines.append(current)
            current = [glyph]
            current_center = glyph[0]
    lines.append(current)

    # Secondary sort within each line: left-to-right by x0.
    reconstructed_lines: list[str] = []
    for line_glyphs in lines:
        line_glyphs.sort(key=lambda g: g[1])
        reconstructed_lines.append("".join(g[3] for g in line_glyphs))

    return "\n".join(reconstructed_lines)


def scan_dom_layer(doc: fitz.Document, secrets: Sequence[Secret], report: ScanReport) -> None:
    """Phase 2 driver: visual-order DOM text, per page."""
    for page_index in range(doc.page_count):
        try:
            page = doc.load_page(page_index)
            visual_text = extract_visual_text(page)
        except Exception as exc:  # a corrupt page must not abort the scan
            report.warnings.append(f"DOM: page {page_index + 1} unreadable ({exc})")
            continue
        haystack = normalize_string(visual_text)
        for secret in find_secrets(haystack, secrets):
            report.findings.append(
                Finding("DOM", secret.name, f"page {page_index + 1} (visual text layer)")
            )


# ──────────────────────────────────────────────────────────────────────────
# PHASE 3: OCR Visual Fallback
# ──────────────────────────────────────────────────────────────────────────
def extract_ocr_text(page: fitz.Page) -> str:
    """Render a PyMuPDF page to memory and OCR it with Apple's native
    Vision framework (Neural Engine) — no external binaries.

    Catches text that exists only as pixels or vector outlines — invisible
    to DOM extraction but perfectly visible to a human reader.
    """
    # Render page to memory
    pix: fitz.Pixmap = page.get_pixmap(dpi=OCR_DPI)
    png_bytes: bytes = pix.tobytes("png")

    # Bridge to Objective-C
    ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    extracted_lines: list[str] = []
    handler_errors: list[str] = []

    def recognize_text_handler(request: Any, error: Any) -> None:
        if error:
            handler_errors.append(str(error))
            return
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if candidates:
                extracted_lines.append(candidates[0].string())

    # Configure Vision request
    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(
        recognize_text_handler
    )
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)

    # Execute
    request_handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        ns_data, {}
    )
    success, perform_error = request_handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError(f"Apple Vision request failed: {perform_error}")
    if handler_errors:
        raise RuntimeError(f"Apple Vision error: {'; '.join(handler_errors)}")

    return "\n".join(extracted_lines)


def scan_ocr_layer(doc: fitz.Document, secrets: Sequence[Secret], report: ScanReport) -> None:
    """Phase 3 driver: OCR every page; degrade loudly if OCR is unavailable."""
    if not _OCR_IMPORTS_OK:
        report.warnings.append(
            "OCR: PyObjC Vision bridge not available — visual layer NOT scanned "
            "(uv pip install pyobjc-framework-Vision pyobjc-framework-Quartz; macOS only)"
        )
        return

    for page_index in range(doc.page_count):
        try:
            page = doc.load_page(page_index)
            ocr_text = extract_ocr_text(page)
        except Exception as exc:
            report.warnings.append(f"OCR: page {page_index + 1} failed ({exc})")
            continue
        haystack = normalize_string(ocr_text)
        for secret in find_secrets(haystack, secrets):
            report.findings.append(
                Finding("OCR", secret.name, f"page {page_index + 1} (Apple Vision @ {OCR_DPI} dpi)")
            )


# ──────────────────────────────────────────────────────────────────────────
# PHASE 4: Metadata & Stream Scraping
# ──────────────────────────────────────────────────────────────────────────
def _run_tool(argv: Sequence[str]) -> bytes | None:
    """Run an external CLI tool, returning raw stdout or None on failure."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # qpdf exits 3 on warnings but still emits usable output; accept any
    # run that produced stdout.
    if not proc.stdout:
        return None
    return proc.stdout


def check_hidden_layers(pdf_path: Path, secrets: Sequence[Secret], report: ScanReport) -> None:
    """Phase 4: sweep metadata (exiftool) and raw object streams (qpdf).

    These layers catch leaks a human reviewer never sees: XMP/Info
    metadata, and text sitting in decompressed content streams — including
    content "hidden" under redaction rectangles or orphaned by incremental
    updates.
    """
    # -- 4a: exiftool metadata sweep ---------------------------------------
    exif_out = _run_tool(["exiftool", "-json", str(pdf_path)])
    if exif_out is None:
        report.warnings.append(
            "Metadata: exiftool unavailable or failed — metadata layer NOT scanned"
        )
    else:
        haystack = normalize_string(exif_out.decode("utf-8", errors="replace"))
        for secret in find_secrets(haystack, secrets):
            report.findings.append(
                Finding("Metadata", secret.name, "exiftool field sweep (XMP/Info/embedded)")
            )

    # -- 4b: qpdf QDF binary-stream sweep ----------------------------------
    # --qdf decompresses every stream into readable form; scanning its full
    # output exposes raw text operators, orphaned objects, and anything a
    # botched redaction left behind in the file body.
    qdf_out = _run_tool(
        ["qpdf", "--qdf", "--object-streams=disable", str(pdf_path), "-"]
    )
    if qdf_out is None:
        report.warnings.append(
            "Binary: qpdf unavailable or failed — binary-stream layer NOT scanned"
        )
    else:
        # latin-1 maps every byte 1:1, so no binary content is lost.
        haystack = normalize_string(qdf_out.decode("latin-1"))
        for secret in find_secrets(haystack, secrets):
            report.findings.append(
                Finding("Binary", secret.name, "qpdf QDF decompressed object streams")
            )


# ──────────────────────────────────────────────────────────────────────────
# PHASE 5: The CLI Orchestrator
# ──────────────────────────────────────────────────────────────────────────
def load_secrets(secrets_path: Path) -> list[Secret]:
    """Parse and normalize the secrets JSON file, validating its shape."""
    try:
        payload: Any = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"[ERROR] Cannot read secrets file {secrets_path}: {exc}")

    if not isinstance(payload, list):
        raise SystemExit("[ERROR] secrets.json must be a JSON array of objects")

    secrets: list[Secret] = []
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict) or "name" not in entry or "value" not in entry:
            raise SystemExit(
                f"[ERROR] secrets.json entry {i} must be an object with 'name' and 'value'"
            )
        normalized = normalize_string(str(entry["value"]))
        if not normalized:
            raise SystemExit(
                f"[ERROR] secret '{entry['name']}' normalizes to an empty string — "
                "it would match everything or nothing; refusing to scan"
            )
        secrets.append(Secret(str(entry["name"]), str(entry["value"]), normalized))

    if not secrets:
        raise SystemExit("[ERROR] secrets.json contains no secrets")
    return secrets


def print_report(report: ScanReport, pdf_path: Path) -> None:
    """Render the final verdict banner and per-finding detail."""
    bar = "=" * 70
    print(f"\n{bar}")
    if report.leaked:
        print(f"  [FAIL]  SENSITIVE DATA DETECTED IN: {pdf_path.name}")
        print(bar)
        # Deduplicate while preserving order.
        seen: set[Finding] = set()
        for f in report.findings:
            if f in seen:
                continue
            seen.add(f)
            print(f"  ✖ LAYER: {f.layer:<8} | SECRET: {f.secret_name!r:<24} | {f.location}")
    else:
        print(f"  [PASS]  No target secrets detected in: {pdf_path.name}")
    print(bar)

    if report.degraded:
        print("\n  ⚠ SCAN INCOMPLETE — the following layers degraded or were skipped:")
        for w in report.warnings:
            print(f"    - {w}")
        if not report.leaked:
            print("\n  A [PASS] from an incomplete scan is NOT a clean bill of health.")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description="Forensic PDF verification: detect secrets across DOM, "
        "OCR, metadata, and binary-stream layers.",
    )
    parser.add_argument("--target", required=True, type=Path, help="PDF file to verify")
    parser.add_argument("--secrets", required=True, type=Path, help="JSON array of secrets")
    args = parser.parse_args(argv)

    pdf_path: Path = args.target
    if not pdf_path.is_file():
        print(f"[ERROR] Target PDF not found: {pdf_path}", file=sys.stderr)
        return 2

    secrets = load_secrets(args.secrets)
    report = ScanReport()

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        print(f"[ERROR] Cannot open PDF {pdf_path}: {exc}", file=sys.stderr)
        return 2

    try:
        if doc.needs_pass:
            print(f"[ERROR] PDF is password-protected: {pdf_path}", file=sys.stderr)
            return 2

        print(f"[*] Scanning {pdf_path.name} ({doc.page_count} page(s)) "
              f"for {len(secrets)} secret(s)...")

        print("[*] Phase 2: DOM layer (layout-aware visual text)...")
        scan_dom_layer(doc, secrets, report)

        print("[*] Phase 3: OCR layer (rendered pixels)...")
        scan_ocr_layer(doc, secrets, report)
    finally:
        doc.close()

    print("[*] Phase 4: Metadata + binary-stream layers (exiftool / qpdf)...")
    check_hidden_layers(pdf_path, secrets, report)

    print_report(report, pdf_path)

    if report.leaked:
        return 1
    if report.degraded:
        return 2  # clean-so-far, but a layer was skipped: not a real PASS
    return 0


if __name__ == "__main__":
    sys.exit(main())
