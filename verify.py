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
import bisect
import contextlib
import functools
import io
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn, Sequence

# ──────────────────────────────────────────────────────────────────────────
# Third-party and first-party imports (fail with a clear message, not a
# traceback): an operational failure must exit 2, never fall through to
# Python's default traceback + exit 1, which this code otherwise shares
# with "secret found".
# ──────────────────────────────────────────────────────────────────────────
def _fatal_import(what: str, exc: BaseException) -> NoReturn:
    """Report a fatal import-time failure and exit 2 — defensively.

    Every guard below catches BaseException, not just ImportError or
    Exception: a corrupt or partial install, a bytecode/ABI mismatch, or
    any other failure while executing a dependency's module bodies (a
    SyntaxError, an AttributeError, …) must exit 2 the same way a missing
    dependency does. Exception alone is not broad enough —
    SystemExit/KeyboardInterrupt (and GeneratorExit, asyncio's
    CancelledError, …) are BaseException, not Exception, so a stub or a
    corrupt module calling sys.exit() at import time, or a Ctrl-C or
    cancellation during import, would otherwise slip straight through as
    the process's own exit code — silently, with no message at all. A
    Ctrl-C here exiting 2 (rather than the interpreter's usual 130) is
    acceptable: this runs before any scanning has started, and the
    fail-closed contract does not carve out an exception for it.

    Formatting *exc* is itself untrusted: an ImportError subclass's (or
    anything else's) __str__ is arbitrary code and can raise, which must
    not turn this handler into a second, worse traceback in place of the
    first. Every step here is wrapped accordingly, down to the write
    itself.

    Exits via `os._exit(2)`, not `sys.exit(2)`: a dependency whose module
    body already ran far enough to replace `sys.exit` (accidentally or
    adversarially) before raising would make `sys.exit(2)` a no-op here,
    falling through into the rest of verify.py's module body with none of
    its own guards run yet. `os._exit` is a direct syscall a module-level
    monkeypatch cannot intercept, so this guard's exit code no longer
    depends on `run_cli`'s own `except BaseException: os._exit(...)`
    backstop to still exit 2. It skips buffer flushing like any os._exit
    call (see run_cli's own docstring), so stderr is flushed explicitly
    first — best-effort, since a broken stream must not turn this into an
    unguarded crash either.
    """
    try:
        detail = f"{type(exc).__name__}: {exc}"
    except BaseException:
        try:
            detail = type(exc).__name__
        except BaseException:
            detail = "unknown error"
    try:
        sys.stderr.write(
            f"[ERROR] cannot import {what} ({detail}); install it or run "
            "verify.py from the repository\n"
        )
        sys.stderr.flush()
    except BaseException:
        pass
    os._exit(2)


try:
    import pymupdf as fitz  # PyMuPDF
except BaseException as exc:  # pragma: no cover
    _fatal_import("PyMuPDF", exc)

# docs/REDESIGN.md §4, §6 ("Move, don't wrap"): the pure data model, the
# normalizer/value matcher, the pattern-class scanning engine, the rules
# loader, the page/OCR views and the report renderers now live in
# redaction_verifier.model, .matching, .rules, .views and .report.
# Re-exported here (one block, so there is exactly one place that needs
# this guard) so every name used outside the package — existing
# `verify.X` references, imports and the CLI — keeps working unchanged.
# Each is imported `as` itself (the explicit re-export convention,
# recognized by ruff's F401) because verify.py's OWN code below no
# longer defines these, it just uses them. Three implementation-detail
# constants are NOT re-exported, because nothing outside their own
# submodule ever reaches them as a bare name or `verify.X`:
# matching.values._NON_ALNUM_RE, matching.patterns._PATTERN_FOLD_TABLE,
# matching.validators._EMAIL_FILE_EXTENSIONS.
#
# The former dedicated Vision-import guard (a `_fatal_import("the OCR
# bridge (Vision)", exc)` for a BaseException at `import Vision` time)
# moved inside this same block along with the rest of the OCR view: the
# ordinary-failure degrade path (`_OCR_IMPORTS_OK = False`) still lives
# next to that import in redaction_verifier.views.ocr, but a BaseException
# there (SystemExit, KeyboardInterrupt, ...) is no longer caught by a
# dedicated guard — this package must never import verify, so it cannot
# call `_fatal_import` itself. It propagates out of `redaction_verifier`
# instead, caught by the `except BaseException` below like any other
# package-import failure, and reported as "cannot import
# redaction_verifier" rather than "cannot import the OCR bridge (Vision)".
try:
    from redaction_verifier.matching import BUILTIN_PATTERN_CLASSES as BUILTIN_PATTERN_CLASSES
    from redaction_verifier.matching import PATTERN_SCAN_BATCH as PATTERN_SCAN_BATCH
    from redaction_verifier.matching import PATTERN_SCAN_OVERLAP as PATTERN_SCAN_OVERLAP
    from redaction_verifier.matching import PatternRule as PatternRule
    from redaction_verifier.matching import PatternScanner as PatternScanner
    from redaction_verifier.matching import RollingScanner as RollingScanner
    from redaction_verifier.matching import SecretMatcher as SecretMatcher
    from redaction_verifier.matching import _fold_for_patterns as _fold_for_patterns
    from redaction_verifier.matching import _luhn_ok as _luhn_ok
    from redaction_verifier.matching import _valid_card as _valid_card
    from redaction_verifier.matching import _valid_email as _valid_email
    from redaction_verifier.matching import _valid_nanp as _valid_nanp
    from redaction_verifier.matching import _valid_ssn as _valid_ssn
    from redaction_verifier.matching import mask as mask
    from redaction_verifier.matching import match_patterns as match_patterns
    from redaction_verifier.matching import normalize_string as normalize_string
    from redaction_verifier.matching.policy import informational_rules as informational_rules
    from redaction_verifier.model import ADJACENCY as ADJACENCY
    from redaction_verifier.model import LAYERS as LAYERS
    from redaction_verifier.model import STORAGE_CLASSES as STORAGE_CLASSES
    from redaction_verifier.model import WARNING_CODES as WARNING_CODES
    from redaction_verifier.model import WARNING_FIELDS as WARNING_FIELDS
    from redaction_verifier.model import Finding as Finding
    from redaction_verifier.model import MaskIndex as MaskIndex
    from redaction_verifier.model import ScanReport as ScanReport
    from redaction_verifier.model import Secret as Secret
    from redaction_verifier.model import VerifyError as VerifyError
    from redaction_verifier.model import Warn as Warn
    from redaction_verifier.model import WarnList as WarnList
    from redaction_verifier.report import JSON_SCHEMA_VERSION as JSON_SCHEMA_VERSION
    from redaction_verifier.report import _sanitize_report_text as _sanitize_report_text
    from redaction_verifier.report import build_json_report as build_json_report
    from redaction_verifier.report import print_report as print_report
    from redaction_verifier.report import write_json_report as write_json_report
    from redaction_verifier.rules import ENTITY_TYPE_TO_CLASS as ENTITY_TYPE_TO_CLASS
    from redaction_verifier.rules import PARTIAL_ENTITY_COVERAGE as PARTIAL_ENTITY_COVERAGE
    from redaction_verifier.rules import UPSTREAM_CONFIG_KEYS as UPSTREAM_CONFIG_KEYS
    from redaction_verifier.rules import UPSTREAM_ENTITY_TYPES as UPSTREAM_ENTITY_TYPES
    from redaction_verifier.rules import RuleSet as RuleSet
    from redaction_verifier.rules import _load_rules_json as _load_rules_json
    from redaction_verifier.rules import _load_rules_yaml as _load_rules_yaml
    from redaction_verifier.rules import _make_class_rule as _make_class_rule
    from redaction_verifier.rules import _make_pattern_rule as _make_pattern_rule
    from redaction_verifier.rules import _make_value_rule as _make_value_rule
    from redaction_verifier.rules import _yaml_coercion_kind as _yaml_coercion_kind
    from redaction_verifier.rules import _yaml_section as _yaml_section
    from redaction_verifier.rules import load_rules as load_rules
    from redaction_verifier.views import MAX_LINE_TOLERANCE_PT as MAX_LINE_TOLERANCE_PT
    from redaction_verifier.views import MIN_LINE_TOLERANCE_PT as MIN_LINE_TOLERANCE_PT
    from redaction_verifier.views import OCR_DPI as OCR_DPI
    from redaction_verifier.views import OCR_GENUINE_READINGS as OCR_GENUINE_READINGS
    from redaction_verifier.views import TEXT_GENUINE_READINGS as TEXT_GENUINE_READINGS
    from redaction_verifier.views import TEXT_SEAM_READINGS as TEXT_SEAM_READINGS
    from redaction_verifier.views import TEXT_TAILS_INTO_NEXT_PAGE as TEXT_TAILS_INTO_NEXT_PAGE
    from redaction_verifier.views import TEXT_SCATTER_READINGS as TEXT_SCATTER_READINGS
    from redaction_verifier.views import _OCR_IMPORTS_OK as _OCR_IMPORTS_OK
    from redaction_verifier.views import _join_cluster as _join_cluster
    from redaction_verifier.views import _reconstruct as _reconstruct
    from redaction_verifier.views import _vision_recognize_batch as _vision_recognize_batch
    from redaction_verifier.views import extract_ocr_text as extract_ocr_text
    from redaction_verifier.views import extract_visual_text as extract_visual_text
    from redaction_verifier.views import ocr_page_too_large as ocr_page_too_large
    from redaction_verifier.visible_map.sink import MapSink as MapSink
    from redaction_verifier.visible_map.sink import offer as _offer
except BaseException as exc:  # pragma: no cover
    _fatal_import("redaction_verifier", exc)


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────
# The verdict semantics are versioned with the tool: any change that can
# move a file from 0 to 1/2, or from 1 to 2, bumps at least the minor.
# The single source of truth pyproject.toml's dynamic version reads
# (`attr = "verify.__version__"`) — it stays here, not in
# redaction_verifier.report, which only receives it as a parameter
# (build_json_report's `tool_version`) since that package must never
# import verify.
__version__ = "0.1.0"
SUBPROCESS_TIMEOUT_S: int = 120
QPDF_CHUNK_BYTES: int = 4 << 20
# qpdf's output is scanned only once this much of it has arrived behind it:
# the final trailer (and the /ID qpdf writes there) must be whole before
# its generated element can be told apart from the input's own (see
# _strip_generated_ids).
QPDF_TAIL_HOLD_BYTES: int = 64 << 10
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


# ──────────────────────────────────────────────────────────────────────────
# PHASE 2/3: Text and OCR views — moved to redaction_verifier.views
# ──────────────────────────────────────────────────────────────────────────
# docs/REDESIGN.md §4, §6: the layout-aware visual text extractor
# (_reconstruct, _join_cluster, extract_visual_text, TEXT_GENUINE_READINGS,
# TEXT_SEAM_READINGS, TEXT_TAILS_INTO_NEXT_PAGE and the line-clustering
# tolerance constants) and the Apple Vision OCR bridge
# (_vision_recognize_batch, extract_ocr_text, OCR_DPI, _OCR_IMPORTS_OK) now
# live in redaction_verifier.views, re-exported above in the single
# guarded block.


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
    seam_variants: int | None = None,
    tails_into_next: Sequence[int] = (),
    cross_page_reading: int = 0,
    scatter_readings: Sequence[int] = (),
    scatter_window: int = 5,
    fail_fast: bool = False,
    incomplete: Callable[[fitz.Page], str | None] | None = None,
    sink: MapSink | None = None,
) -> None:
    """Run a per-page text extractor over the document and match secrets.

    *incomplete*, when given, says why a page cannot be read whole (or
    None): the page is still read and matched, and also gets a
    PAGE_FAILED warning, so it never passes as clean (OCR: a page too
    large for Vision, #128).

    The extractor returns several readings per page. The first
    *hard_variants* are genuine reading orders; any others are
    reconstructions. Callers set it to the number of genuine readings:
    TEXT_GENUINE_READINGS for Text, OCR_GENUINE_READINGS for OCR. The
    first *seam_variants* of those (default: all of them) also feed the
    hard page seam; for Text, TEXT_SEAM_READINGS. The tail of each reading
    in *tails_into_next* also meets the next page's first reading in the
    manual-review net; for Text, TEXT_TAILS_INTO_NEXT_PAGE.
    *cross_page_reading* is the reading the cross-page pattern scan reads
    (manual review only): OCR's raw Vision reading, whose lines hold a
    vertical line no upright re-read confirmed whole. In each reading of
    *scatter_readings* (for Text, TEXT_SCATTER_READINGS) a known value
    assembled from two or three pieces of different lines within
    *scatter_window* consecutive lines, in any order
    (SecretMatcher.scattered), is a manual-review warning: within a page,
    or across a break from the previous page's last lines to this page's
    first ones — a value split around a header, footer or other text, or
    with its halves in reverse order. Never a finding.

    Known values:
    - hard: on one line of a genuine reading; or running from the last
      line of a page onto the first line of the next page, in the same
      seam reading;
    - manual review: on one line of a reconstructed reading only, when a
      page's lines are joined, or crossing a page break any other way
      (other lines, blank or unreadable pages in between).
    Each page break is checked on its own, so every place a value crosses
    one is reported where it happens. Every reading, genuine or not,
    keeps its own tail for that manual-review net. For Text, reading 4
    (the visible text, each stretch of a line between column breaks read
    on across the visible edge) feeds the hard seam; readings 5-6 feed
    only the net: their last column may be off-page text, which does not
    run on into the next page. The tails of the off-page reading (3) and
    of reading 4 also meet the next page's visible horizontal reading (0)
    in the net, so a value whose first half lies off the page, below or
    beside its last line, and whose second half opens the next page is
    reported for review; reading 9 (all horizontal text, visible or not)
    meets itself across the break, so one whose second half lies off the
    next page, above its top edge, is too.

    Pattern rules match per visual line of every genuine reading for hard
    findings (for Text, the readings across the visible edge included),
    aggregated per rule across pages. Matches that appear only in fused
    multi-line text, in reconstructed readings, or across page boundaries
    (the first reading only) are manual-review warnings.

    When *fail_fast* is True the scan stops after the first page that
    produces a finding, so the tool exits quickly on large documents.

    *sink* (--visible-map only) receives a copy of each scanned page's
    truth — rule names per tier — before the fail-fast stop, so the
    stopping page keeps its own.
    """
    keep = max(matcher.max_len - 1, 0)   # enough of one side to straddle a join
    # Per reading: the last `keep` characters read so far (the tail a match
    # crossing into the next page must start in), and — seam readings
    # only — the page number and end of the last line, for the seam.
    seams = hard_variants if seam_variants is None else min(seam_variants, hard_variants)
    tails: dict[int, str] = {}
    tail_page: dict[int, int] = {}       # the page that last fed each tail
    # Per scatter reading: (page number, its last scatter_window-1 lines).
    scatter_tail: dict[int, tuple[int, list[str]]] = {}
    last_line: dict[int, tuple[int, str]] = {}
    cross_page_patterns = PatternScanner(patterns, collapse_separators=True)
    layer_hard: dict[str, tuple[list[int], str]] = {}
    layer_soft: dict[str, tuple[list[int], str]] = {}
    for page_index in range(doc.page_count):
        try:
            page = doc.load_page(page_index)
            why: str | None = None
            if incomplete is not None:
                try:
                    why = incomplete(page)
                except Exception as exc:   # cannot tell: not fully read
                    why = f"could not check the page ({exc})"
            variants = extractor(page)
        except Exception as exc:  # a corrupt page must not abort the scan
            report.warn(
                "PAGE_FAILED",
                layer,
                f"{layer}: page {page_index + 1} failed ({exc})",
                # What went unread is the page's own content: live (#128).
                page=page_index + 1, storage="live",
            )
            continue
        if why is not None:
            report.warn(
                "PAGE_FAILED",
                layer,
                f"{layer}: page {page_index + 1} not fully read ({why})",
                page=page_index + 1, storage="live",
            )
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
        # last line onto this page's first line, in a seam reading. A
        # page that failed or held no text never updates last_line, so it
        # breaks the adjacency: what lies between the halves is unseen.
        seam_here: set[str] = set()
        for vi, lines in enumerate(norm_lines[:seams]):
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
        # Manual review: a value scattered over nearby lines of the page.
        for vi in scatter_readings:
            if vi >= len(norm_lines):
                continue
            for name in matcher.scattered(norm_lines[vi], scatter_window):
                if name in hard_here or name in page_warned:
                    continue
                page_warned.add(name)
                report.warn(
                    "REVIEW_CROSS_LINE",
                    layer,
                    CROSS_LINE_WARNING.format(layer=layer, page=page_no, name=name),
                    storage="live",
                    page=page_no,
                    rule=name,
                    adjacency="JOINED_LINES",
                )

        # Manual review: anything else crossing into this page from the text
        # before it, in any reading — the net that keeps every page-break
        # split, however joined, from being silent.
        # Each reading's tail meets its own head; the tails_into_next
        # readings' tails, when the previous page fed them, also meet the
        # first reading's head.
        fulls = ["".join(lines) for lines in norm_lines]
        joins = [(tails.get(vi, ""), full) for vi, full in enumerate(fulls)]
        if fulls:
            joins += [(tails[vi], fulls[0]) for vi in tails_into_next
                      if tail_page.get(vi) == page_no - 1]
        crossing_names: list[str] = []
        for tail, full in joins:
            if keep and tail and full:
                crossing_names += matcher.crossing(tail, full[:keep])
        # And a value scattered over the previous page's last lines and
        # this page's first ones, around a header or footer.
        for vi in scatter_readings:
            prev_lines = scatter_tail.get(vi)
            if vi < len(norm_lines) and prev_lines and prev_lines[0] == page_no - 1:
                head = norm_lines[vi][:scatter_window - 1]
                crossing_names += matcher.scattered(
                    prev_lines[1] + head, scatter_window, across=len(prev_lines[1]))
        crossing_warned: set[str] = set()
        for name in crossing_names:
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
        for vi in scatter_readings:
            if vi < len(norm_lines) and norm_lines[vi]:
                scatter_tail[vi] = (page_no, norm_lines[vi][-(scatter_window - 1):])
        for vi, lines in enumerate(norm_lines):
            if keep and fulls[vi]:
                tails[vi] = (tails.get(vi, "") + fulls[vi])[-keep:]
                tail_page[vi] = page_no
            if vi < seams and keep and lines:
                last_line[vi] = (page_no, lines[-1][-keep:])

        # Pattern rules, two tiers. Hard findings come only from single
        # visual lines of the genuine readings — matches that need fused
        # lines or vertical column reconstructions are the same
        # coincidental-concatenation class the value pipeline demotes, so
        # they become manual-review warnings instead of hard FAILs.
        page_hard: dict[str, str] = {}
        page_soft: dict[str, str] = {}
        if variants:
            for text in variants[:hard_variants]:
                for line in text.split("\n"):
                    page_hard.update(match_patterns(line, patterns, page_hard))
            for vi, text in enumerate(variants):
                seen = set(page_hard) | set(page_soft)
                if len(seen) == len(patterns):
                    break
                page_soft.update(
                    match_patterns(text, patterns, seen, collapse_separators=True)
                )
                # A reconstruction's own lines, as written: collapsing
                # separators can stop a rule that needs a run of them
                # ('REF--\d{6}') from matching anywhere above.
                if vi >= hard_variants:
                    for line in text.split("\n"):
                        page_soft.update(match_patterns(
                            line, patterns, set(page_hard) | set(page_soft)))
        for name, sample in page_hard.items():
            hard_pages, _ = layer_hard.setdefault(name, ([], sample))
            hard_pages.append(page_index + 1)
        for name, sample in page_soft.items():
            soft_pages, _ = layer_soft.setdefault(name, ([], sample))
            soft_pages.append(page_index + 1)
        if patterns:
            cross_page_patterns.feed(
                "\n" + (variants[cross_page_reading] if len(variants) > cross_page_reading else ""))
        if sink is not None:
            _offer(sink, "page", layer, page_no, hard_here, seam_here, page_hard, page_soft,
                   page_warned, crossing_warned)
        # A hit of an informational class rule (ADR 0012) is a notice: it
        # must not end the scan, or a listed value on a later page goes
        # unread and the run certifies.
        if fail_fast and (report.leaked
                          or any(n not in report.informational for n in layer_hard)):
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
            "only when lines, columns or pages are fused (sample "
            f"{mask(sample, report.exact_index, full=layer == 'OCR')})"
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
    env: dict[str, str], cwd: str, stderr: int = subprocess.DEVNULL,
) -> subprocess.Popen[bytes] | None:
    """Launch an external tool; a start failure degrades the layer loudly.

    *name* is the tool's logical name ("exiftool", "qpdf"), reported as-is
    regardless of which absolute path it resolved to, so TOOL_MISSING/
    TOOL_START_FAILED stay stable for consumers.
    """
    try:
        return subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=stderr,
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

    def launch(
        name: str, layer: str, args: Sequence[str], stderr: int = subprocess.DEVNULL,
    ) -> subprocess.Popen[bytes] | None:
        exe = shutil.which(name)
        if exe is None:
            report.warn(
                "TOOL_MISSING",
                layer,
                f"{layer}: {name} not installed — layer NOT scanned",
                tool=name,
            )
            return None
        return _start_tool(report, layer, name, [exe, *args], env, scratch.name, stderr)

    procs = {
        "exiftool": launch("exiftool", "Metadata", ["-config", "", "-json", "--", target]),
        # --deterministic-id: without it qpdf writes a fresh trailer /ID,
        # an MD5 of the current second, into the output the Binary layer
        # sweeps — so a short all-hex value rule (e.g. "45-67" -> "4567")
        # matched it about once in 2,000 runs, and the same file's verdict
        # depended on the clock. With it, the /ID qpdf writes is derived
        # from the file alone. stderr is kept: on exit 3 its warning lines
        # decide whether the output can still be certified
        # (_qpdf_warnings_unverified).
        "qpdf": launch(
            "qpdf", "Binary",
            ["--qdf", "--object-streams=disable", "--deterministic-id", target, "-"],
            stderr=subprocess.PIPE,
        ),
    }
    return procs, scratch


def _file_identity(path: Path) -> tuple[int, int, int, int] | None:
    """(size, mtime_ns, ctime_ns, inode) of the resolved file, or None."""
    try:
        st = path.resolve().stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino


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

    *page* (0-based), *xref* and *kind* ("annotation:content",
    "field:value", "link:uri", ...) say the same as *location* in plain
    values, for the --visible-map; they are filled on every run and never
    printed.
    """

    location: str
    text: str
    hard: bool = True
    page: int | None = None
    xref: int | None = None
    kind: str | None = None


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
                yield HiddenItem(f"JavaScript in object {xref} (/{key})", body,
                                 xref=xref, kind="javascript")

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


def _parent_xref(doc: fitz.Document, node: int) -> int:
    """A node's /Parent object number, or 0 at the root (no /Parent).
    Anything else (an inline dictionary, a number, a bad reference) raises,
    so the walk fails closed rather than stopping early."""
    kind, value = doc.xref_get_key(node, "Parent")
    if kind == "null":
        return 0
    parent = int(value.split()[0]) if kind == "xref" else 0
    if parent <= 0:
        raise ValueError("page tree /Parent is not a reference")
    return parent


def _thumb_carrier(doc: fitz.Document, page_xref: int, memo: dict[int, bool | None]) -> str:
    """Where a page's /Thumb sits: "its own", "inherited" (on a Pages node
    above it) or "" (none). Raises on a page tree cycle or unreadable key.

    ``memo`` maps each Pages node already walked to whether it or a node
    above it carries /Thumb, or None when its walk failed. Every page
    shares the chain above it, so without the memo N pages under a deep
    chain cost O(N x depth); with it each node is read once, O(objects).
    A node whose walk failed stays failed, so a later page reaching it
    through the memo fails closed too rather than passing.
    """
    if doc.xref_get_key(page_xref, "Thumb")[0] != "null":
        return "its own"
    path: list[int] = []
    on_path: set[int] = set()
    found = False
    try:
        node = _parent_xref(doc, page_xref)
        while node:
            if node in memo:
                hit = memo[node]
                if hit is None:
                    raise ValueError("page tree walk failed earlier")
                found = hit
                break
            # A walk that never repeats a node cannot visit more nodes
            # than the file has objects; a repeat (the page itself
            # included) is a cycle.
            if node in on_path or node == page_xref or len(path) >= doc.xref_length():
                raise ValueError("page tree cycle")
            path.append(node)
            on_path.add(node)
            if doc.xref_get_key(node, "Thumb")[0] != "null":
                found = True
                break
            node = _parent_xref(doc, node)
    except Exception:
        for walked in path:
            memo[walked] = None
        raise
    for walked in path:
        memo[walked] = found
    return "inherited" if found else ""


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
                yield HiddenItem(f"{where} ({key})", str(value), kind=f"attachment:{key}")
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
                kind="attachment:content",
            )

    thumb_memo: dict[int, bool | None] = {}
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
        # A page's /Thumb preview is a second picture of the page that
        # nothing here renders or OCRs (K24): flag every one, and fail
        # closed, until Phase 4b reads it. /Thumb is not inheritable in
        # the spec, but a Pages node carrying one is checked too.
        try:
            carrier = _thumb_carrier(doc, page.xref, thumb_memo)
        except Exception as exc:
            yield Warn(
                "HIDDEN_ITEM_FAILED",
                "Hidden",
                f"Hidden: page {human_page} thumbnail check failed "
                f"({type(exc).__name__}) — a page thumbnail may be present "
                "and NOT read; manual review recommended",
                storage="live",
                page=human_page,
            )
        else:
            if carrier:
                yield Warn(
                    "PAGE_THUMBNAIL",
                    "Hidden",
                    f"Hidden: page {human_page} carries a thumbnail image "
                    f"(/Thumb, {carrier}) that is NOT read or OCR'd — it may "
                    "still show what the page no longer does; manual review "
                    "recommended",
                    storage="live",
                    page=human_page,
                )
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
                        f"annotation {xref} on page {human_page} ({key})", str(value),
                        page=page_index, xref=xref, kind=f"annotation:{key}",
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
                    page=page_index, xref=xref, kind="annotation:file",
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
                        f"form field {xref} on page {human_page} ({label})", str(value),
                        page=page_index, xref=xref, kind=f"field:{label}",
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
                        f"link #{n} on page {human_page} ({key})", str(value),
                        page=page_index, xref=link.get("xref"), kind=f"link:{key}",
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
            yield HiddenItem(f"optional-content group {xref}", str(name), xref=xref,
                             kind="layer:name")


def scan_hidden_objects(
    doc: fitz.Document,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
    sink: MapSink | None = None,
) -> None:
    """Scan attachments, annotations, form fields, links, scripts and
    layer names — content no page renders.

    Items are consumed lazily and recorded as they arrive, so a carrier
    that fails later cannot discard what was already found. Discrete
    field values are hard findings; attachment bodies are fusion-prone
    runs of text and are demoted to manual-review warnings, like every
    other fusion-prone surface in the tool.

    *sink* (--visible-map only) receives each matching item's page, xref,
    kind, location, tier and rule names.
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
        if sink is not None and (hits or samples):
            _offer(sink, "hidden", item.page, item.xref, item.kind, item.location, item.hard,
                   [*hits, *samples])
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
                    f"pattern rule {name!r} (sample {mask(sample, report.exact_index)}) — this content "
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
                f"(sample {mask(sample, report.exact_index)}) — may originate from filesystem "
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


# The text shape of a generated document /ID written as text rather than
# bytes: hex digits, with or without dashes (UUID form). About one such
# 32-character string in thirteen holds a run the built-in pattern classes
# accept, so a pattern match in one is manual review (owner decision D);
# anything else in /ID is held to the hard tier like any PDF string.
_GENERATED_ID_RE = re.compile(r"[0-9A-Fa-f-]+")
_EOL_STR_RE = re.compile(r"[\r\n]")


def _id_element_spans(buf: str, spans: list[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """The string spans (from *spans*) that are elements of the ``/ID``
    array of the dictionary *buf* opens with — its own key, at its top
    level, not one in a nested dictionary or array, a string or a comment.
    One linear pass. An array that never closes yields nothing, and a key
    this misses (a name spelled with ``#`` escapes) only leaves its strings
    at the hard tier."""
    ends = dict(spans)
    elements: set[tuple[int, int]] = set()
    pending: list[tuple[int, int]] = []       # elements of the open /ID array
    dict_depth = array_depth = 0
    id_depth: int | None = None               # array depth of the open /ID array
    expect_array = False
    i, n = 0, len(buf)
    while i < n:
        if i in ends:                          # a string token
            if id_depth is not None and array_depth == id_depth and dict_depth == 1:
                pending.append((i, ends[i]))
            expect_array, i = False, ends[i]
            continue
        c = buf[i]
        if c == "%":                           # a comment, to the end of its line
            eol = _EOL_STR_RE.search(buf, i)
            i = n if eol is None else eol.start()
        elif buf.startswith("<<", i):
            dict_depth, expect_array, i = dict_depth + 1, False, i + 2
        elif buf.startswith(">>", i):
            dict_depth, expect_array, i = dict_depth - 1, False, i + 2
            if dict_depth < 1:
                break
        elif c == "[":
            array_depth += 1
            if expect_array:
                id_depth, pending = array_depth, []
            expect_array, i = False, i + 1
        elif c == "]":
            if id_depth is not None and array_depth == id_depth:
                elements.update(pending)
                id_depth, pending = None, []
            array_depth, expect_array, i = max(array_depth - 1, 0), False, i + 1
        elif c == "/":
            j = i + 1
            while j < n and buf[j] not in _PDF_DELIMITERS_AND_SPACE:
                j += 1
            expect_array = (buf[i:j] == "/ID" and dict_depth == 1 and array_depth == 0
                            and id_depth is None)
            i = j
        elif c in _PDF_SPACE:
            i += 1
        else:
            expect_array, i = False, i + 1
    return frozenset(elements)


_PDF_SPACE = frozenset(" \t\r\n\f\x00")
_PDF_DELIMITERS_AND_SPACE = _PDF_SPACE | frozenset("()<>[]{}/%")


def _feed_pdf_strings(
    buf: str,
    scanner: RollingScanner,
    hard_patterns: PatternScanner,
    soft_patterns: PatternScanner,
    id_patterns: PatternScanner | None = None,
) -> bool:
    """Extract PDF string/hex literals from buf and feed all scanners.

    The normalized secret scanner and the *soft* pattern scanner see
    literals back-to-back (adjacency preserved, so split values are
    caught); the *hard* pattern scanner gets a two-newline fence between
    literals — a single [-\\s.] separator slot cannot cross it — so a
    hard pattern finding can never be a fusion of unrelated tokens.

    With *id_patterns*, buf is a trailer dictionary: an element of its
    document /ID whose text is only hex digits and dashes (a generated
    identifier written as text) feeds *id_patterns*, the manual-review
    tier, instead of the hard pattern scanner (owner decision, option D);
    the soft (manual-review) scanner still sees it, so a value split
    across elements is reviewed. Known values are matched there like
    anywhere else.

    Returns True when the buffer ended inside an unterminated literal, so
    the caller can degrade the verdict rather than silently drop whatever
    followed the unclosed "(".
    """
    spans, truncated = _pdf_string_spans(buf)
    id_elements = _id_element_spans(buf, spans) if id_patterns is not None else frozenset()
    for start, end in spans:
        decoded = _decode_pdf_string(buf[start:end])
        if decoded is None:
            continue
        scanner.feed(normalize_string(decoded))
        if id_patterns is not None and (start, end) in id_elements \
                and _GENERATED_ID_RE.fullmatch(decoded):
            id_patterns.feed(decoded + "\n\n")
            soft_patterns.feed(decoded)        # a split value: adjacency review
            continue
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
    shown = f" (sample {mask(sample, report.exact_index)})" if sample else ""
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
    *,
    pdf_path: Path | None = None,
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

    A cross-reference stream is an object nothing references, but its
    dictionary is the trailer: given *pdf_path*, each one the chain
    reaches is labelled as the (current or an earlier revision's) trailer
    rather than ORPHANED, and its /ID read as a trailer's (see
    _feed_pdf_strings). Without it the plain object label stays.
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
    xref_streams: dict[int, _TrailerDict] = {}
    if pdf_path is not None:
        try:
            raw = Path(pdf_path).read_bytes()
        except OSError:
            raw = b""               # the trailer scan reports the read failure
        xref_streams = {t.number: t for t in _chain_trailers(raw)[0] if t.number is not None}

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
        revision = None
        chained = xref_streams.get(xref)
        is_trailer = chained is not None and _stream_key(doc, xref, "Type")[1] == "/XRef"
        if is_trailer:
            assert chained is not None
            storage, revision = chained.storage, chained.revision
            where = (f"cross-reference stream (trailer), object {xref}" if storage == "live"
                     else f"earlier revision {revision}, cross-reference stream (trailer), "
                          f"object {xref}" if revision is not None
                     else f"earlier revision's cross-reference stream (trailer), object {xref}")
        _scan_object(
            doc,
            xref,
            source,
            where,
            leftover=leftover,
            origin={"storage": storage, "object": xref, "revision": revision},
            matcher=matcher,
            patterns=patterns,
            report=report,
            tally=tally,
            trailer=is_trailer,
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
    trailer: bool = False,
) -> None:
    """Scan one object's dictionary and (text) stream body.

    *leftover* marks content the document no longer uses — an orphan, or
    an object's superseded version from an earlier revision. *origin* (the
    storage class, object and revision) is attached to every finding and
    warning this object produces. For leftovers
    the tool also reads attachment and XMP bodies as raw text, and flags
    text it cannot decode, since no other layer will see them. *trailer*
    marks a cross-reference stream, whose dictionary is a trailer.
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

    _match_texts(texts, where, origin=origin, trailer=trailer, matcher=matcher,
                 patterns=patterns, report=report, tally=tally)


def _match_texts(
    texts: Sequence[str],
    where: str,
    *,
    origin: dict[str, Any],
    trailer: bool,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
    tally: _ObjectTally,
) -> None:
    """Match the PDF strings in *texts* (one object's dictionary, then its
    text body) and report under *where*. *trailer* marks texts[0] as a
    trailer dictionary, whose generated-looking /ID elements are matched by
    pattern rules at the manual-review tier only."""
    literals = RollingScanner(matcher)
    hard_patterns = PatternScanner(patterns)
    soft_patterns = PatternScanner(patterns, collapse_separators=True)
    id_patterns = PatternScanner(patterns) if trailer else None
    truncated = False
    for i, text in enumerate(texts):
        # Fence so one object's tail cannot fuse with the next.
        if _feed_pdf_strings(text + "\n\n", literals, hard_patterns, soft_patterns,
                             id_patterns if i == 0 else None):
            truncated = True
    hard_patterns.flush()
    soft_patterns.flush()
    if truncated:
        tally.truncated += 1

    for secret in sorted(literals.found, key=lambda s: s.name):
        report.record("Objects", secret.name, where, **origin)
    for name, sample in hard_patterns.hits.items():
        report.record("Objects", name, where, sample, **origin)
    if id_patterns is not None:
        id_patterns.flush()
        for name, sample in id_patterns.hits.items():
            if name in hard_patterns.hits:
                continue
            report.warn(
                "REVIEW_OBJECT_TEXT",
                "Objects",
                f"Objects: {where}: the document /ID (a generated identifier, "
                f"written as hex digits) contains a sequence matching pattern rule "
                f"{name!r} (sample {mask(sample, report.exact_index)}) — such identifiers hold digit runs "
                "by coincidence; manual review recommended",
                rule=name,
                adjacency="NOISY_SOURCE",
                **origin,
            )
    for name, sample in soft_patterns.hits.items():
        if name in hard_patterns.hits or (id_patterns is not None and name in id_patterns.hits):
            continue
        report.warn(
            "REVIEW_ADJACENT_LITERALS",
            "Objects",
            f"Objects: {where}: adjacent literals fuse into a sequence "
            f"matching pattern rule {name!r} (sample {mask(sample, report.exact_index)}) — "
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
    skipping nested dictionaries, hex strings, literal strings and comments
    (a '%' outside a string runs to the end of its line, so a '>>' inside
    a comment cannot end the dictionary early)."""
    depth, i, n = 0, start, len(raw)
    while i < n:
        if raw[i] == 0x25:                           # '%' comment
            eol = _EOL_RE.search(raw, i)
            i = n if eol is None else eol.start()
        elif raw.startswith(b"<<", i):
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


_EOL_RE = re.compile(rb"[\r\n]")
# Whitespace (NUL included, as PDF counts it) and comments between a
# keyword and what follows it.
_GAP_RE = re.compile(rb"(?:[\s\x00]|%[^\r\n]*(?:[\r\n]|$))*")


def _split_comments(raw: bytes) -> tuple[bytes, list[bytes]]:
    """*raw* (a dictionary's bytes) with every comment outside a string
    blanked to spaces — the same reading as _dict_end — and the comments
    themselves. Linear."""
    out = bytearray(raw)
    comments: list[bytes] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == 0x25:                                # '%' comment
            eol = _EOL_RE.search(raw, i)
            end = n if eol is None else eol.start()
            comments.append(raw[i + 1:end])
            out[i:end] = b" " * (end - i)
            i = end
        elif raw.startswith(b"<<", i) or raw.startswith(b">>", i):
            i += 2
        elif c == 0x3C:                              # '<' hex string
            close = raw.find(b">", i + 1)
            i = n if close < 0 else close + 1
        elif c == 0x28:                              # '(' literal string
            level, i = 1, i + 1
            while i < n and level:
                if raw[i] == 0x5C:
                    i += 1
                elif raw[i] == 0x28:
                    level += 1
                elif raw[i] == 0x29:
                    level -= 1
                i += 1
        else:
            i += 1
    return bytes(out), comments


# A classic cross-reference table after its ``xref`` keyword: integers,
# n/f entry types and whitespace (NUL included) — one flat character
# class, so the regex engine keeps no per-token state (an alternation under
# a star held ~100 bytes per token: gigabytes on a large table) — and
# comments, skipped one by one.
_TABLE_FLAT_RE = re.compile(rb"[\s\x00\d+\-nNfF]*")
# Every token of a table, a stray sign included (so it breaks the shape
# rather than vanishing); comments match without a group and are dropped.
_TABLE_TOKEN_RE = re.compile(rb"%[^\r\n]*|([+-]?\d+|[nNfF]|[+-])")
_SIGNED_INT_RE = re.compile(rb"[+-]?\d+")
_ENTRY_TYPES = frozenset({b"n", b"N", b"f", b"F"})
_MAX_NUMBER_DIGITS = 10                  # object numbers, counts: 32-bit


def _section_trailer(raw: bytes, offset: int) -> tuple[int, int] | None:
    """(keyword, opening) of the trailer of the classic section at
    *offset* (``xref``): the ``trailer`` keyword right after the table
    (a comment naming it is part of the table), and where the dictionary
    after it opens. None when anything else follows the table, or no
    dictionary follows the keyword: the section's trailer cannot be
    read. Reads only the section's own table, in linear time and constant
    memory."""
    pos = offset + len(b"xref")
    while True:
        pos = _TABLE_FLAT_RE.match(raw, pos).end()   # type: ignore[union-attr]
        if not raw.startswith(b"%", pos):
            break
        eol = _EOL_RE.search(raw, pos)
        pos = len(raw) if eol is None else eol.start()
    if not raw.startswith(b"trailer", pos):
        return None
    opening = _trailer_opening(raw, pos)
    return None if opening is None else (pos, opening)


_SLOW = object()
_SUBSECTION_HEADER_RE = re.compile(rb"[ \t\r\n\f\v]*(\d+)[ \t]+(\d+)[ \t]*(?:\r\n|\r|\n)")
_DIGITS = b"0123456789"
_FAST_CHUNK_ENTRIES = 1 << 16


def _canonical_entries(chunk: bytes) -> bool:
    """Whether *chunk* is whole canonical 20-byte entries: 10 digits, a
    space, 5 digits, a space, n or f, and a two-byte end of line."""
    k = len(chunk) // 20
    # Callers cut chunks at multiples of 20; the length test is defensive.
    if len(chunk) % 20 or chunk[10::20] != b" " * k or chunk[16::20] != b" " * k:
        return False
    if not set(chunk[17::20]) <= {0x6E, 0x66} or not set(chunk[18::20]) <= {0x20, 0x0D} \
            or not set(chunk[19::20]) <= {0x0D, 0x0A}:
        return False
    # The five fixed non-digit bytes per entry checked, the other fifteen
    # must all be digits.
    return len(chunk.translate(None, _DIGITS)) == 5 * k


def _table_subsections_fast(table: bytes) -> list[range] | None | object:
    """_table_subsections for the common table: each subsection a header
    line then canonical 20-byte entries (ISO 32000 §7.5.4). Checked with
    slices in bounded chunks, at C speed and in constant memory. Returns
    _SLOW whenever the table is anything else, so the token walk decides:
    a range set comes back only where the walk would return the same."""
    ranges: list[range] = []
    pos, n = 0, len(table)
    while True:
        header = _SUBSECTION_HEADER_RE.match(table, pos)
        if header is None:
            return ranges if not table[pos:].strip() else _SLOW
        first, count = header.group(1), header.group(2)
        if len(first) > _MAX_NUMBER_DIGITS or len(count) > _MAX_NUMBER_DIGITS:
            return _SLOW
        size = int(count)
        start = header.end()
        stop = start + 20 * size
        if stop > n:
            return _SLOW
        for chunk_start in range(start, stop, 20 * _FAST_CHUNK_ENTRIES):
            chunk = table[chunk_start:min(stop, chunk_start + 20 * _FAST_CHUNK_ENTRIES)]
            if not _canonical_entries(chunk):
                return _SLOW
        ranges.append(range(int(first), int(first) + size))
        pos = stop


def _table_subsections(table: bytes) -> list[range] | None:
    """The object-number ranges a classic table's subsections define: a
    header (first number, count) then count entries of offset, generation
    and n/f, whatever the line layout and with comments skipped. None when
    the tokens do not have that shape (callers then treat every object as
    possibly changed). The common table takes a fast path; anything else
    is read one token at a time, in constant memory."""
    fast = _table_subsections_fast(table)
    if fast is not _SLOW:
        return fast  # type: ignore[return-value]
    tokens = (m.group(1) for m in _TABLE_TOKEN_RE.finditer(table) if m.group(1))
    ranges: list[range] = []
    for first in tokens:
        count = next(tokens, None)
        if (count is None or not first.isdigit() or not count.isdigit()
                or len(first) > _MAX_NUMBER_DIGITS or len(count) > _MAX_NUMBER_DIGITS):
            return None
        for _ in range(int(count)):
            entry = (next(tokens, None), next(tokens, None), next(tokens, None))
            if (entry[2] not in _ENTRY_TYPES or not _SIGNED_INT_RE.fullmatch(entry[0] or b"")
                    or not _SIGNED_INT_RE.fullmatch(entry[1] or b"")):
                return None
        ranges.append(range(int(first), int(first) + int(count)))
    return ranges


def _trailer_opening(raw: bytes, trailer: int) -> int | None:
    """Where the dictionary after the ``trailer`` keyword at *trailer*
    opens, skipping whitespace and comments (a '<<' inside a comment is not
    it); None when no dictionary follows."""
    after = _GAP_RE.match(raw, trailer + len(b"trailer"))
    opening = after.end() if after else trailer + len(b"trailer")
    return opening if raw.startswith(b"<<", opening) else None
_INDEX_RE = re.compile(rb"/Index\s*\[([\d\s]*)\]")
_SIZE_RE = re.compile(rb"/Size\s+(\d+)")


# Section reads for the one file being scanned: the earlier-revision walk
# and the trailer chain read the same sections. Keyed by the bytes object
# itself, so a new file starts a new memo and none outlives its file.
_section_memo: tuple[bytes | None, dict[int, Any]] = (None, {})


def _xref_section_memo(raw: bytes, offset: int) -> tuple[int, int | None, list[range] | None] | None:
    global _section_memo
    if _section_memo[0] is not raw:
        _section_memo = (raw, {})
    memo = _section_memo[1]
    if offset not in memo:
        memo[offset] = _xref_section(raw, offset)
    return memo[offset]


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
        located = _section_trailer(raw, offset)
        if located is None:
            return None
        trailer, opening = located
        end = _dict_end(raw, opening)
        if end is None:
            return None
        dictionary = raw[opening:end]
        objects: list[range] | None = _table_subsections(raw[offset + 4:trailer])
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


@functools.lru_cache(maxsize=1)
def _earlier_revisions(raw: bytes) -> tuple[tuple[int, int, frozenset[int] | None], ...]:
    """(xref offset, section end, changed later) for every revision before
    the current one, oldest first. *changed later* is the set of object
    numbers some newer section redefines or frees — the only objects whose
    earlier version can differ from the current one (None if unknown). Boundaries come from the file's own cross-reference
    chain — never from '%%EOF' bytes, which can occur inside a stream (an
    attached PDF) and which an incremental writer may omit."""
    starts = [int(m.group(1)) for m in _STARTXREF_RE.finditer(raw)]
    if not starts:
        return ()
    current = starts[-1]
    sections: dict[int, tuple[int, list[range] | None]] = {}
    tried: set[int] = set()         # parsed or not: each offset is read once
    pending = [current, *starts]
    while pending:
        offset = pending.pop()
        if offset in tried:
            continue
        tried.add(offset)
        found = _xref_section_memo(raw, offset)
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
        earlier.append((offset, end, None if changed is None else frozenset(changed)))
    return tuple(earlier)


_XREFSTM_RE = re.compile(rb"/XRefStm\s+(\d+)")
_OBJ_NUMBER_RE = re.compile(rb"\s*(\d+)\s+\d+\s+obj\b")


@dataclass(frozen=True)
class _TrailerDict:
    """One trailer dictionary on the file's cross-reference chain: a
    classic ``trailer << ... >>`` (*number* None) or a cross-reference
    stream's dictionary (*number* is that stream's object number)."""

    start: int
    end: int
    number: int | None
    storage: str                 # live, or superseded for an earlier revision
    revision: int | None         # the earlier-revision scan's number, if any


def _revision_numbers(earlier: Sequence[tuple[int, int, frozenset[int] | None]]) -> dict[int, int]:
    """Section offset -> the number scan_earlier_revisions gives that
    revision (it keeps the original and the latest 49 past the cap)."""
    if len(earlier) > MAX_EARLIER_REVISIONS:
        earlier = earlier[:1] + earlier[-(MAX_EARLIER_REVISIONS - 1):]
    return {offset: number for number, (offset, _, _) in enumerate(earlier, start=1)}


@functools.lru_cache(maxsize=1)
def _chain_trailers(raw: bytes) -> tuple[tuple[_TrailerDict, ...], tuple[int, ...]]:
    """(trailers, unreadable): every trailer dictionary the cross-reference
    chain reaches — the same walk as _earlier_revisions, plus each classic
    section's ``/XRefStm`` (a hybrid file's stream half) — and the offsets
    where the chain reaches a classic ``xref`` section whose trailer cannot
    be read. A section an earlier revision owns is superseded; every other
    one (the current section, a linearized file's first-page section) is
    live. Cached: scan_pdf_objects and scan_trailers both need it."""
    earlier = _earlier_revisions(raw)
    superseded = {offset for offset, _, _ in earlier}
    numbers = _revision_numbers(earlier)
    starts = [int(m.group(1)) for m in _STARTXREF_RE.finditer(raw)]
    found: dict[int, _TrailerDict] = {}
    unreadable: list[int] = []
    tried: set[int] = set()
    pending: list[tuple[int, int | None]] = [(offset, None) for offset in starts]
    while pending:
        offset, owner = pending.pop()
        if offset in tried:
            continue
        tried.add(offset)
        section_read = _xref_section_memo(raw, offset)
        if section_read is None:
            if raw.startswith(b"xref", offset):
                unreadable.append(offset)
            continue
        _, prev, _ = section_read
        section = offset if owner is None else owner
        storage = "superseded" if section in superseded else "live"
        revision = numbers.get(section)
        if raw.startswith(b"xref", offset):
            located = _section_trailer(raw, offset)
            opening = located[1] if located is not None else None
            end = _dict_end(raw, opening) if opening is not None else None
            if opening is None or end is None:
                unreadable.append(offset)
                continue
            found[offset] = _TrailerDict(opening, end, None, storage, revision)
            stm = _XREFSTM_RE.search(raw, opening, end)
            if stm:
                pending.append((int(stm.group(1)), section))
        else:
            header = _OBJ_NUMBER_RE.match(raw, offset)
            opening_at = raw.find(b"<<", offset)
            end = _dict_end(raw, opening_at)
            if header is None or end is None:
                continue
            found[offset] = _TrailerDict(opening_at, end, int(header.group(1)), storage, revision)
        if prev is not None:
            pending.append((prev, None))
    return tuple(sorted(found.values(), key=lambda t: t.start)), tuple(sorted(unreadable))


def scan_trailers(
    pdf_path: Path,
    doc: fitz.Document,
    matcher: SecretMatcher,
    patterns: Sequence[PatternRule],
    report: ScanReport,
) -> None:
    """Match the strings in every classic trailer dictionary.

    A trailer is not an object, so the object walk never reads it, and
    qpdf rewrites only the current one, with the document /ID re-encoded
    (first element) or replaced (second). Each is read from the file's
    bytes: the trailers the cross-reference chain reaches (current or an
    earlier revision's), then any other ``trailer << ... >>`` in the file.
    Cross-reference *stream* dictionaries are objects; scan_pdf_objects
    reads them. Every string is matched at the hard tier, except that a
    pattern match in a /ID element written as plain hex digits is manual
    review (see _feed_pdf_strings).
    """
    try:
        raw = Path(pdf_path).read_bytes()
    except OSError as exc:
        report.warn(
            "TRAILER_UNREADABLE",
            "Objects",
            f"Objects: file unreadable for the trailer scan ({exc}) — trailers NOT scanned",
        )
        return
    tally = _ObjectTally()

    def scan(start: int, end: int, where: str, origin: dict[str, Any]) -> None:
        # Comments are read apart from the dictionary: a '(' or '>>' in one
        # must not change what the dictionary's own strings are.
        dictionary, comments = _split_comments(raw[start:end])
        _match_texts([dictionary.decode("latin-1")], where, origin=origin, trailer=True,
                     matcher=matcher, patterns=patterns, report=report, tally=tally)
        if comments:
            _scan_orphaned_payload(b"\n".join(comments), "comment", f"{where} (comment)",
                                   hard=False, matcher=matcher, patterns=patterns,
                                   report=report, origin=origin)

    trailers, unreadable = _chain_trailers(raw)
    for offset in unreadable:
        report.warn(
            "TRAILER_UNREADABLE",
            "Objects",
            f"Objects: the cross-reference section at byte {offset} has a trailer this tool "
            "cannot read — NOT scanned; manual review recommended",
        )
    chain = {t.start: t for t in trailers if t.number is None}
    for t in chain.values():
        if t.storage == "live":
            where = "trailer"
        elif t.revision is not None:
            where = f"earlier revision {t.revision}, trailer"
        else:
            where = f"earlier revision's trailer at byte {t.start}"
        scan(t.start, t.end, where, {"storage": t.storage, "revision": t.revision})
    # Any other trailer: one the chain does not reach (a broken /Prev, a
    # repaired file) or one inside an uncompressed stream. Each match is
    # read once, and a match inside a dictionary already read is skipped,
    # so the search stays linear.
    resume = pos = 0
    while (keyword := raw.find(b"trailer", pos)) >= 0:
        after = keyword + len(b"trailer")
        gap = _GAP_RE.match(raw, after)
        gap_end = gap.end() if gap else after
        # A keyword inside the gap is comment text: skipping past the gap
        # keeps this linear (a regex over the gap restarted at each one).
        pos = max(after, gap_end)
        opening = gap_end if raw.startswith(b"<<", gap_end) else None
        if opening is None or opening < resume:
            continue
        if opening in chain:
            resume = chain[opening].end
            continue
        end = _dict_end(raw, opening)
        if end is None:
            report.warn(
                "TRAILER_UNREADABLE",
                "Objects",
                f"Objects: a trailer dictionary at byte {opening} never closes — it and "
                "anything after it NOT scanned as a trailer; manual review recommended",
                storage="unreferenced",
            )
            break
        scan(opening, end, f"trailer at byte {opening} (not on the cross-reference chain)",
             {"storage": "unreferenced"})
        resume = end
    if tally.truncated:
        report.warn(
            "UNTERMINATED_STRING",
            "Objects",
            f"Objects: {tally.truncated} trailer dictionary(ies) held an unterminated "
            "string literal — content after it was NOT scanned; manual review recommended",
        )


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


# A /ID key (not /IDx); its value must be an array of two strings.
_ID_KEY_RE = re.compile(rb"/ID(?=[\s\[\]<>()/{}%])")
_PDF_WS = b" \t\r\n\f\x00"
_LITERAL_ESCAPES = {
    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b",
    ord("f"): b"\f", ord("("): b"(", ord(")"): b")", ord("\\"): b"\\",
}
# The end of qpdf's QDF output: its one trailer, then startxref and %%EOF.
# Anchored at the very end, every line indented as QDF writes dictionary
# entries.
_QDF_TRAILER_RE = re.compile(
    rb"\ntrailer <<\n((?:  [^\n]*\n)*)>>\nstartxref\n[0-9]+\n%%EOF\n?\Z"
)
_QDF_ID_LINE_RE = re.compile(rb"^  /ID \[<([0-9a-f]*)><([0-9a-f]*)>\]$", re.M)


def _pdf_string_at(raw: bytes, i: int) -> tuple[bytes, int] | None:
    """The bytes of the PDF string (hex or literal) starting at *raw[i]*,
    and the index just past it; None if there is no well-formed one."""
    if raw[i:i + 1] == b"<":
        end = raw.find(b">", i + 1)
        if end < 0:
            return None
        digits = bytes(c for c in raw[i + 1:end] if c not in _PDF_WS)
        if any(c not in b"0123456789abcdefABCDEF" for c in digits):
            return None
        return bytes.fromhex((digits + b"0" if len(digits) % 2 else digits).decode()), end + 1
    if raw[i:i + 1] != b"(":
        return None
    out, depth, j, n = bytearray(), 1, i + 1, len(raw)
    while j < n:
        c = raw[j]
        if c == 0x5C:                               # backslash (§7.3.4.2, Table 3)
            if j + 1 >= n:
                return None
            e = raw[j + 1]
            if e in _LITERAL_ESCAPES:
                out += _LITERAL_ESCAPES[e]
                j += 2
            elif e in b"01234567":
                k = j + 1
                while k < min(j + 4, n) and raw[k] in b"01234567":
                    k += 1
                out.append(int(raw[j + 1:k], 8) & 0xFF)
                j = k
            elif e == 0x0D:                         # line continuation
                j += 3 if raw[j + 2:j + 3] == b"\n" else 2
            elif e == 0x0A:
                j += 2
            else:                                   # any other escaped byte is itself
                out.append(e)
                j += 2
        elif c == 0x0D:                             # an end of line is one LF
            out.append(0x0A)
            j += 2 if raw[j + 1:j + 2] == b"\n" else 1
        else:
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    return bytes(out), j + 1
            out.append(c)
            j += 1
    return None


def _input_document_ids(raw: bytes) -> frozenset[str] | None:
    """Every document /ID element the input file holds, as lowercase hex.

    None unless every /ID key in the file is followed by an array of two
    well-formed strings: then nothing qpdf writes can be called generated
    (see _strip_generated_ids), so nothing is stripped.
    """
    ids: set[str] = set()
    key = _ID_KEY_RE.search(raw)
    while key is not None:
        i = key.end()
        elements = []
        while i < len(raw) and raw[i] in _PDF_WS:
            i += 1
        if raw[i:i + 1] != b"[":
            return None
        i += 1
        for _ in range(2):
            while i < len(raw) and raw[i] in _PDF_WS:
                i += 1
            parsed = _pdf_string_at(raw, i)
            if parsed is None:
                return None
            element, i = parsed
            elements.append(element.hex())
        while i < len(raw) and raw[i] in _PDF_WS:
            i += 1
        if raw[i:i + 1] != b"]":
            return None
        ids.update(elements)
        # Resume after this array: restarting inside its strings is
        # quadratic on a file built of nested "/ID [(" runs.
        key = _ID_KEY_RE.search(raw, i + 1)
    return frozenset(ids)


def _strip_generated_ids(tail: bytes, input_ids: frozenset[str] | None) -> bytes:
    """Blank the /ID elements qpdf generated in its output's final trailer.

    qpdf writes a document /ID of its own: the input's first element is
    kept, but the second (both, if the input has none) is new. Those bytes
    are not in the file, and scanning them can only produce a coincidental
    match (a random hex string that happens to spell a short value rule).
    An element is blanked only when it appears nowhere among the input's
    own /ID elements, and only in a trailer of exactly qpdf's shape at the
    very end of the output, holding exactly one /ID, and only when the
    input's own /ID accounts for qpdf's first element: either that element
    is one of the input's, or, when no /ID was found in the input, qpdf's
    two elements are identical, as qpdf writes them when it generates both.
    The second rule is what makes a missed /ID key harmless: qpdf reads
    spellings this parser does not (/I#44, /ID followed by NUL), keeps the
    file's first element, and generates a different second one, so the
    elements differ and nothing is blanked.
    """
    if input_ids is None:
        return tail
    trailer = _QDF_TRAILER_RE.search(tail)
    if trailer is None or trailer.group(1).count(b"/ID") != 1:
        return tail
    line = _QDF_ID_LINE_RE.search(trailer.group(1))
    if line is None:
        return tail
    if input_ids and line.group(1).decode("ascii") not in input_ids:
        return tail
    if not input_ids and line.group(1) != line.group(2):
        return tail
    kept = [e if e.decode("ascii") in input_ids else b"" for e in line.groups()]
    start = trailer.start(1) + line.start()
    end = trailer.start(1) + line.end()
    return tail[:start] + b"  /ID [<" + kept[0] + b"><" + kept[1] + b">]" + tail[end:]


# ── qpdf exit 3: which warnings are provably harmless ───────────────────
# Owner decision 2026-10-15 (qpdf warning kinds, per-file byte check; ADR
# 0002's note of that date). qpdf exits 3 when it succeeded with warnings.
# The Binary layer used to treat every exit 3 as TOOL_EXIT_NONZERO (exit
# 2), because a truncated input also exits 3 with tail objects missing. It
# now reads qpdf's stderr, and exit 3 stops forcing exit 2 only when EVERY
# line is one of five kinds AND that kind's check against this file's own
# bytes passes. Anything else -- a line it cannot parse, a kind not listed
# here (xref reconstruction, integer overflow, ...), a check that cannot
# decide, a cap exceeded -- keeps TOOL_EXIT_NONZERO (fail closed).
#
# Nothing qpdf printed is ever echoed: the result is one of the fixed
# reasons below. Every check is bounded: a token budget, a work budget for
# the header scan (both proportional to the file's size) and a wall-clock
# deadline; exceeding any cap fails closed.
QPDF_STDERR_CAP_BYTES: int = 1 << 20
QPDF_WARNING_LINES_CAP: int = 10_000
QPDF_CHECK_MAX_FILE_BYTES: int = 512 << 20
QPDF_CHECK_MAX_TOKENS: int = 5_000_000
QPDF_HEADER_SCAN_WORK_PER_BYTE: int = 8
QPDF_CHECK_DEADLINE_S: float = 20.0
QPDF_CHECK_MAX_DEPTH: int = 64
QPDF_XREF_DECODE_CAP: int = 64 << 20

_PDF_WS = b"\x00\t\n\x0c\r "
_PDF_REGULAR = rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]"
# One token at a position. A ')' '>' '{' '}' on its own matches nothing
# and fails the parse.
_PDF_TOKEN = re.compile(
    rb"(?P<ws>[\x00\t\n\x0c\r ]+)|(?P<comment>%[^\r\n]*)"
    rb"|(?P<dopen><<)|(?P<dclose>>>)|(?P<hex><[0-9A-Fa-f\x00\t\n\x0c\r ]*>)"
    rb"|(?P<aopen>\[)|(?P<aclose>\])|(?P<name>/" + _PDF_REGULAR + rb"*)"
    rb"|(?P<lit>\()|(?P<regular>" + _PDF_REGULAR + rb"+)"
)
_PDF_LITERAL_STOP = re.compile(rb"[()\\]")
_PDF_UINT = re.compile(rb"\d{1,10}")
_PDF_WS_BYTES = frozenset(_PDF_WS)
_PDF_DIGIT_BYTES = frozenset(b"0123456789")
_PDF_ESCAPED_NAME = re.compile(rb"/" + _PDF_REGULAR + rb"*#" + _PDF_REGULAR + rb"*")

# The kinds (stderr line after "WARNING: <target> ").
_QPDF_REF00 = re.compile(
    rb"\(object \d{1,10} \d{1,5}, offset \d{1,15}\): "
    rb"treating bad indirect reference \(0 0 R\) as null")
_QPDF_OFFSET0 = re.compile(
    rb"\(object (\d{1,10}) \d{1,5}\): object has offset 0 - a common error "
    rb"handled correctly by qpdf and most other applications")
_QPDF_DUPKEY = re.compile(
    rb"\(object (\d{1,10}) (\d{1,5}), offset (\d{1,15})\): dictionary has "
    rb"duplicated key (/[!-~]+); last occurrence overrides earlier ones")
_QPDF_ENDOBJ = re.compile(
    rb"\((xref stream: )?object (\d{1,10}) (\d{1,5}), offset (\d{1,15})\): expected endobj")
_QPDF_FLATE = re.compile(
    rb"\(offset (\d{1,15})\): input stream is complete but output may still be valid")
_QPDF_TAIL_LINES = frozenset({
    b"qpdf: operation succeeded with warnings",
    b"qpdf: operation succeeded with warnings; resulting file may have some problems",
})


class _QpdfCheckFailed(Exception):
    """A byte check could not prove its kind harmless (fail closed)."""


def _pdf_name(raw: bytes) -> bytes:
    """A name token's bytes after the '/', with #xx escapes decoded."""
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and re.fullmatch(rb"[0-9A-Fa-f]{2}", raw[i + 1:i + 3]):
            out.append(int(raw[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


@dataclass
class _PdfStream:
    obj: tuple[int, int]
    dict_items: tuple[tuple[bytes, Any], ...]   # (decoded key, value), in order
    start: int                                 # first data byte
    end: int                                   # one past the last


@dataclass
class _PdfTiling:
    """What one front-to-back pass over the file found, keyed by the
    offsets qpdf's messages use."""
    # end of a dictionary's "<<" -> (object, keys seen twice, values all equal)
    dup_dicts: dict[int, tuple[tuple[int, int], frozenset[bytes], bool]] = field(
        default_factory=dict)
    # start of the token found where "endobj" belonged, when only
    # whitespace separates the two -> (object, its stream if it has one)
    missing_endobj: dict[int, tuple[tuple[int, int], _PdfStream | None]] = field(
        default_factory=dict)
    streams: dict[int, _PdfStream] = field(default_factory=dict)


class _PdfLexer:
    """A minimal PDF tokenizer and value parser over the whole file, with
    a token budget and a nesting limit (both fail closed)."""

    def __init__(self, data: bytes, headers: "_PdfHeaderScan", deadline: float) -> None:
        self.data = data
        self.headers = headers
        self.deadline = deadline
        self.budget = QPDF_CHECK_MAX_TOKENS
        self.dup_dicts: dict[int, tuple[frozenset[bytes], bool]] = {}

    def token(self, pos: int) -> tuple[str, int, int]:
        data = self.data
        while True:
            if pos >= len(data):
                return "eof", pos, pos
            self.budget -= 1
            if self.budget < 0:
                raise _QpdfCheckFailed("token budget")
            if not self.budget & 0xFFF and time.monotonic() > self.deadline:
                raise _QpdfCheckFailed("deadline")
            m = _PDF_TOKEN.match(data, pos)
            if m is None:
                raise _QpdfCheckFailed("unexpected byte")
            kind = m.lastgroup or ""
            if kind in ("ws", "comment"):
                pos = m.end()
                continue
            if kind == "lit":
                return kind, pos, self._literal_end(pos)
            return kind, pos, m.end()

    def _literal_end(self, pos: int) -> int:
        depth = 0
        i = pos
        while True:
            m = _PDF_LITERAL_STOP.search(self.data, i)
            if m is None:
                raise _QpdfCheckFailed("unterminated string")
            c = m.group()
            if c == b"\\":
                i = m.end() + 1
                continue
            depth += 1 if c == b"(" else -1
            i = m.end()
            if depth == 0:
                return i

    def value(self, pos: int, depth: int = 0) -> tuple[int, Any]:
        """Parse one value; return (end offset, a comparable form)."""
        if depth > QPDF_CHECK_MAX_DEPTH:
            raise _QpdfCheckFailed("nesting")
        kind, s, e = self.token(pos)
        data = self.data
        if kind == "dopen":
            items: list[tuple[bytes, Any]] = []
            p = e
            while True:
                k, ks, ke = self.token(p)
                if k == "dclose":
                    break
                if k != "name":
                    raise _QpdfCheckFailed("dictionary key")
                p, v = self.value(ke, depth + 1)
                items.append((_pdf_name(data[ks + 1:ke]), v))
            first: dict[bytes, Any] = {}
            dups: set[bytes] = set()
            same = True
            for key, v in items:
                if key in first:
                    dups.add(key)
                    same = same and first[key] == v
                else:
                    first[key] = v
            if dups:
                self.dup_dicts[e] = (frozenset(dups), same)
            return ke, ("dict", tuple(items))
        if kind == "aopen":
            elems: list[Any] = []
            p = e
            while True:
                k, ks, ke = self.token(p)
                if k == "aclose":
                    return ke, ("array", tuple(elems))
                p, v = self.value(ks, depth + 1)
                elems.append(v)
        if kind in ("name", "hex", "lit"):
            return e, (kind, data[s:e])
        if kind == "regular":
            tok = data[s:e]
            if _PDF_UINT.fullmatch(tok):
                k2, s2, e2 = self.token(e)
                if k2 == "regular" and _PDF_UINT.fullmatch(data[s2:e2]):
                    k3, s3, e3 = self.token(e2)
                    if k3 == "regular" and data[s3:e3] == b"R":
                        return e3, ("ref", int(tok), int(data[s2:e2]))
            if tok in (b"obj", b"endobj", b"stream", b"endstream", b"R",
                       b"xref", b"trailer", b"startxref"):
                raise _QpdfCheckFailed("keyword as value")
            return e, ("regular", tok)
        raise _QpdfCheckFailed("value")

    def is_header(self, pos: int) -> bool:
        k1, s1, e1 = self.token(pos)
        if k1 != "regular" or not _PDF_UINT.fullmatch(self.data[s1:e1]):
            return False
        k2, s2, e2 = self.token(e1)
        if k2 != "regular" or not _PDF_UINT.fullmatch(self.data[s2:e2]):
            return False
        k3, s3, e3 = self.token(e2)
        return k3 == "regular" and self.data[s3:e3] == b"obj"

    def direct_int(self, v: Any) -> int:
        """An integer value, resolving an indirect one only when the file
        has no object or xref stream (the length object could be packed in
        one) and exactly one possible 'N G obj' for it exists anywhere."""
        if v[0] == "ref":
            if self.headers.has_object_or_xref_streams:
                raise _QpdfCheckFailed("indirect length")
            spots = self.headers.spots(v[1])
            if len(spots) != 1:
                raise _QpdfCheckFailed("indirect length")
            k, s, e = self.token(spots[0])
            _, _, e = self.token(e)
            _, _, e = self.token(e)                 # past "N G obj"
            _, v = self.value(e)
        if v[0] != "regular" or not _PDF_UINT.fullmatch(v[1]):
            raise _QpdfCheckFailed("integer")
        return int(v[1])


class _PdfHeaderScan:
    """Where an 'N G obj' header may start, for every N, found generously
    so that a missed header can only make a check fail closed, never pass:

    - any generation, and either number signed ('6 +0 obj'), the
      generation even glued to 'obj' ('6 0obj');
    - comments between the parts, starting at ANY '%' on their line (a
      '%' inside a string is not a comment, but a backward scan cannot
      tell, so every reading counts);
    - the object number read as the whole digit run before the
      generation even when glued to what precedes it ('x6 0 obj'), and as
      any suffix of it whose offset the file spells somewhere, since a
      reader sent to the '6' of '96 0 obj' reads object 6.

    It walks backward from every 'obj' in the bytes (inside streams and
    comments too) and charges each byte it looks at to a work budget
    proportional to the file's size, so a crafted file fails closed
    instead of making the scan quadratic. Built lazily, once per check."""

    def __init__(self, data: bytes, deadline: float) -> None:
        self.data = data
        self.deadline = deadline
        self.work = QPDF_HEADER_SCAN_WORK_PER_BYTE * len(data) + 1_000_000
        self._by_number: dict[int, set[int]] | None = None
        self._streams: bool | None = None
        self._written: set[int] | None = None
        self._eols: list[int] | None = None
        self._calls = 0
        # Readers take every offset relative to where '%PDF-' starts (it
        # may follow junk, within the first 1024 bytes).
        self.header_shifts = {0} | {m.start() for m in re.finditer(rb"%PDF-", data[:1024 + 5])}

    def _spend(self, n: int) -> None:
        self.work -= n + 1
        if self.work < 0:
            raise _QpdfCheckFailed("header scan budget")
        self._calls += 1
        if not self._calls & 0xFFF and time.monotonic() > self.deadline:
            raise _QpdfCheckFailed("deadline")

    def _line_start(self, eol: int) -> int:
        """Start of the line that the end of line at *eol* closes."""
        if self._eols is None:
            self._spend(len(self.data))
            self._eols = [m.start() for m in re.finditer(rb"[\r\n]", self.data)]
        i = bisect.bisect_left(self._eols, eol)
        return self._eols[i - 1] + 1 if i else 0

    def _gap_starts(self, end: int) -> set[int]:
        """Every e < end such that data[e:end] can be a non-empty run of
        whitespace and comments (a comment runs from '%' to an end of
        line, the end of line itself being whitespace)."""
        data = self.data
        starts: set[int] = set()
        todo = [end]
        seen: set[int] = set()
        while todo:
            j = todo.pop()
            if j in seen or j == 0:
                continue
            seen.add(j)
            self._spend(1)
            c = data[j - 1]
            if c not in _PDF_WS_BYTES:
                continue
            starts.add(j - 1)
            todo.append(j - 1)
            if c in (0x0A, 0x0D):
                line = self._line_start(j - 1)
                self._spend(j - 1 - line)
                k = data.find(b"%", line, j - 1)
                while k != -1:
                    starts.add(k)
                    todo.append(k)
                    k = data.find(b"%", k + 1, j - 1)
        return starts

    def _digits_ending(self, end: int) -> int:
        """Start of the digit run ending at *end* (== end when none)."""
        start = end
        while start > 0 and self.data[start - 1] in _PDF_DIGIT_BYTES:
            start -= 1
        self._spend(end - start)
        return start

    @property
    def offsets_written(self) -> set[int]:
        """Every number the bytes spell as a whole digit run (up to 12
        digits): the offsets a classic xref table, /Prev or startxref
        could send a reader to. Offsets inside an xref stream are binary;
        a file with one fails the offset-0 check before this matters."""
        if self._written is None:
            self._spend(len(self.data))
            self._written = {int(m.group()) for m in re.finditer(rb"\d+", self.data)
                             if len(m.group()) <= 12}
        return self._written

    @property
    def by_number(self) -> dict[int, set[int]]:
        if self._by_number is None:
            data = self.data
            found: dict[int, set[int]] = {}
            for m in re.finditer(rb"obj", data):
                # A generation glued to "obj" ("6 0obj") still counts:
                # MuPDF's lexer ends a number at the first non-digit.
                for e in self._gap_starts(m.start()) | {m.start()}:
                    g = self._digits_ending(e)
                    if g == e:
                        continue
                    if g > 0 and data[g - 1] in b"+-":
                        g -= 1
                    for e2 in self._gap_starts(g):
                        n = self._digits_ending(e2)
                        for k in range(1, min(e2 - n, 12) + 1):
                            # A start inside the digit run is reachable
                            # only through an offset written somewhere in
                            # the file (an xref entry, /Prev, startxref),
                            # relative to any '%PDF-' a reader may take
                            # as the file's start.
                            if k == e2 - n or any(e2 - k - shift in self.offsets_written
                                                  for shift in self.header_shifts):
                                found.setdefault(int(data[e2 - k:e2]), set()).add(e2 - k)
            self._by_number = found
        return self._by_number

    def spots(self, number: int) -> list[int]:
        return sorted(self.by_number.get(number, ()))

    @property
    def has_object_or_xref_streams(self) -> bool:
        if self._streams is None:
            self._streams = _has_object_or_xref_streams(self.data)
        return self._streams


def _pdf_tile(data: bytes, headers: _PdfHeaderScan, deadline: float) -> _PdfTiling:
    """Walk the file front to back: objects (streams skipped by their
    /Length), xref tables, trailers and startxref, with only whitespace and
    comments between them. Anything else fails the walk, and every check
    that needs it then fails closed."""
    lex = _PdfLexer(data, headers, deadline)
    out = _PdfTiling()
    pos = 0
    while True:
        kind, s, e = lex.token(pos)
        if kind == "eof":
            return out
        tok = data[s:e] if kind == "regular" else b""
        if tok == b"xref":
            pos = e
            while True:
                k, s2, e2 = lex.token(pos)
                t = data[s2:e2]
                if k == "regular" and (_PDF_UINT.fullmatch(t) or t in (b"n", b"f")):
                    pos = e2
                    continue
                break
            continue
        if tok == b"trailer":
            pos, v = lex.value(e)
            if v[0] != "dict":
                raise _QpdfCheckFailed("trailer")
            continue
        if tok == b"startxref":
            k, s2, e2 = lex.token(e)
            if k != "regular" or not _PDF_UINT.fullmatch(data[s2:e2]):
                raise _QpdfCheckFailed("startxref")
            pos = e2
            continue
        if not lex.is_header(s):
            raise _QpdfCheckFailed("top level")
        _, s1, e1 = lex.token(s)
        _, s2, e2 = lex.token(e1)
        _, _, e3 = lex.token(e2)
        obj = (int(data[s1:e1]), int(data[s2:e2]))
        before = set(lex.dup_dicts)
        vend, v = lex.value(e3)
        for offset in set(lex.dup_dicts) - before:
            dups, same = lex.dup_dicts[offset]
            out.dup_dicts[offset] = (obj, dups, same)
        k, s4, e4 = lex.token(vend)
        stream = None
        if v[0] == "dict" and k == "regular" and data[s4:e4] == b"stream":
            if data[e4:e4 + 2] == b"\r\n":
                start = e4 + 2
            elif data[e4:e4 + 1] == b"\n":
                start = e4 + 1
            else:
                raise _QpdfCheckFailed("stream keyword")
            lengths = [val for key, val in v[1] if key == b"Length"]
            if len(lengths) != 1:
                raise _QpdfCheckFailed("stream length")
            end = start + lex.direct_int(lengths[0])
            if end > len(data):
                raise _QpdfCheckFailed("stream length")
            k, s5, e5 = lex.token(end)
            if data[end:s5].strip(_PDF_WS) or k != "regular" or data[s5:e5] != b"endstream":
                raise _QpdfCheckFailed("endstream")
            stream = _PdfStream(obj, v[1], start, end)
            out.streams[start] = stream
            vend = e5
            k, s4, e4 = lex.token(vend)
        if k == "regular" and data[s4:e4] == b"endobj":
            pos = e4
            continue
        # No endobj: harmless only if the object's end is followed by
        # nothing but whitespace, then the next object, xref or startxref.
        clean = not data[vend:s4].strip(_PDF_WS) and (
            (k == "regular" and data[s4:e4] in (b"xref", b"startxref"))
            or lex.is_header(s4))
        if not clean:
            raise _QpdfCheckFailed("stray bytes before endobj")
        out.missing_endobj[s4] = (obj, stream)
        pos = vend


def _xref_stream_rows_complete(data: bytes, stream: _PdfStream, lex: _PdfLexer) -> bool:
    """Whether a Flate xref stream decodes every row its /Index (or
    /Size) declares. Anything but the plain shapes fails closed."""
    d = dict(stream.dict_items)
    if len(d) != len(stream.dict_items):
        return False
    filt = d.get(b"Filter")
    if filt is not None and filt[0] == "array" and len(filt[1]) == 1:
        filt = filt[1][0]
    if filt is None or filt[0] != "name" or _pdf_name(filt[1][1:]) != b"FlateDecode":
        return False
    w = d.get(b"W")
    if w is None or w[0] != "array" or len(w[1]) != 3:
        return False
    widths = [lex.direct_int(x) for x in w[1] if x[0] == "regular"]
    if len(widths) != 3:
        return False
    row = sum(widths)
    index = d.get(b"Index")
    if index is None:
        entries = lex.direct_int(d.get(b"Size", ("none",)))
    else:
        if index[0] != "array" or len(index[1]) % 2 or any(x[0] != "regular" for x in index[1]):
            return False
        entries = sum(lex.direct_int(x) for x in index[1][1::2])
    parms = d.get(b"DecodeParms")
    if parms is not None and parms[0] == "array" and len(parms[1]) == 1:
        parms = parms[1][0]
    predictor = 1
    if parms is not None:
        if parms[0] != "dict":
            return False
        p = dict(parms[1])
        if set(p) - {b"Predictor", b"Columns", b"Colors", b"BitsPerComponent"}:
            return False
        predictor = lex.direct_int(p.get(b"Predictor", ("regular", b"1")))
        columns = lex.direct_int(p.get(b"Columns", ("regular", b"1")))
        if lex.direct_int(p.get(b"Colors", ("regular", b"1"))) != 1:
            return False
        if lex.direct_int(p.get(b"BitsPerComponent", ("regular", b"8"))) != 8:
            return False
        if predictor >= 10:
            if columns != row:
                return False
            row += 1
        elif predictor != 1:
            return False
    try:
        decoded = zlib.decompressobj().decompress(
            data[stream.start:stream.end], QPDF_XREF_DECODE_CAP + 1)
    except zlib.error:
        return False
    if len(decoded) > QPDF_XREF_DECODE_CAP:
        return False
    return row > 0 and len(decoded) >= row * entries


def _stream_type(stream: _PdfStream) -> bytes:
    """A stream's /Type, escapes decoded; b"" when it has none. A stream
    that could be an object stream without saying so (a /First key, an
    indirect or repeated /Type, duplicated keys) counts as one."""
    d = dict(stream.dict_items)
    if len(d) != len(stream.dict_items) or b"First" in d:
        return b"ObjStm"
    typ = d.get(b"Type")
    if typ is None:
        return b""
    if typ[0] != "name":
        return b"ObjStm"
    return _pdf_name(typ[1][1:])


def _has_object_or_xref_streams(data: bytes) -> bool:
    """Whether any name in the file, escapes decoded, is /ObjStm or /XRef:
    then an object's body could be stored where a header scan cannot see
    it (an object stream, reachable through an xref stream's entries)."""
    if b"/ObjStm" in data or b"/XRef" in data:
        return True
    return any(_pdf_name(m.group()[1:]) in (b"ObjStm", b"XRef")
               for m in _PDF_ESCAPED_NAME.finditer(data))


def _qpdf_warnings_unverified(stderr: bytes | None, target: str, data: bytes | None) -> str | None:
    """None when every line qpdf wrote to stderr is a warning kind the
    owner accepted (2026-10-15) AND its check against *data* (the file's
    bytes) passes; otherwise a short fixed reason, never file text."""
    if stderr is None or data is None:
        return "qpdf's warnings could not be read"
    if len(stderr) >= QPDF_STDERR_CAP_BYTES or len(data) > QPDF_CHECK_MAX_FILE_BYTES:
        return "too large to check"
    prefix = b"WARNING: " + os.fsencode(target) + b" "
    lines = stderr.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) > QPDF_WARNING_LINES_CAP:
        return "too many qpdf warnings to check"
    warnings = [line for line in lines if line not in _QPDF_TAIL_LINES]
    if not warnings:
        return "no qpdf warning to check"
    parsed: list[tuple[str, re.Match[bytes]]] = []
    for line in warnings:
        if not line.startswith(prefix):
            return "an unrecognised qpdf warning"
        rest = line[len(prefix):]
        for kind, rx in (("ref00", _QPDF_REF00), ("offset0", _QPDF_OFFSET0),
                         ("dupkey", _QPDF_DUPKEY), ("endobj", _QPDF_ENDOBJ),
                         ("flate", _QPDF_FLATE)):
            m = rx.fullmatch(rest)
            if m is not None:
                parsed.append((kind, m))
                break
        else:
            return "an unrecognised qpdf warning"
    try:
        deadline = time.monotonic() + QPDF_CHECK_DEADLINE_S
        headers = _PdfHeaderScan(data, deadline)
        tiling: _PdfTiling | None = None
        lex = _PdfLexer(data, headers, deadline)
        for kind, m in parsed:
            if kind == "ref00":
                continue
            if kind == "offset0":
                if headers.has_object_or_xref_streams or headers.spots(int(m.group(1))):
                    return "an offset-0 object whose body may be stored in the file"
                continue
            if tiling is None:
                tiling = _pdf_tile(data, headers, deadline)
            if kind == "dupkey":
                obj = (int(m.group(1)), int(m.group(2)))
                found = tiling.dup_dicts.get(int(m.group(3)))
                key = m.group(4)[1:]
                if found is None or found[0] != obj or not found[2] or key not in found[1]:
                    return "a duplicated key whose values differ"
            elif kind == "endobj":
                obj = (int(m.group(2)), int(m.group(3)))
                gap = tiling.missing_endobj.get(int(m.group(4)))
                is_xref = m.group(1) is not None
                if gap is None or gap[0] != obj or (is_xref and (
                        gap[1] is None or _stream_type(gap[1]) != b"XRef")):
                    return "bytes between an object and the next one"
            else:  # flate
                stream = tiling.streams.get(int(m.group(1)))
                if stream is None:
                    return "a truncated Flate stream that could not be located"
                name = _stream_type(stream)
                if name == b"ObjStm":
                    return "a truncated object stream"
                if name == b"XRef" and not _xref_stream_rows_complete(data, stream, lex):
                    return "a truncated xref stream"
    except Exception:  # noqa: BLE001 -- any failure fails closed
        return "a qpdf warning whose check could not complete"
    return None


def _collect_qpdf(
    proc: subprocess.Popen[bytes],
    matcher: SecretMatcher,
    report: ScanReport,
    input_ids: frozenset[str] | None = None,
    pdf_bytes: bytes | None = None,
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
    complete = False
    # qpdf's stderr, read alongside stdout so neither pipe can fill and
    # stall it; kept up to QPDF_STDERR_CAP_BYTES (a longer one is never
    # certified: _qpdf_warnings_unverified refuses it).
    err = bytearray()
    err_open = proc.stderr is not None
    # The last QPDF_TAIL_HOLD_BYTES are held back until qpdf finishes, so
    # its final trailer can be read whole; everything is scanned in order.
    held = b""
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
        watched = ([] if complete else [proc.stdout]) + ([proc.stderr] if err_open else [])
        if not watched:
            break
        ready, _, _ = select.select(watched, [], [], remaining)
        if not ready:
            proc.kill()
            proc.communicate()
            report.warn(
                "TOOL_TIMEOUT", "Binary", "Binary: qpdf timed out — scan incomplete", tool="qpdf"
            )
            complete = False
            break
        if proc.stderr is not None and proc.stderr in ready:
            data = os.read(proc.stderr.fileno(), QPDF_CHUNK_BYTES)
            if not data:
                err_open = False
            elif len(err) < QPDF_STDERR_CAP_BYTES:
                err += data[:QPDF_STDERR_CAP_BYTES - len(err)]
        if proc.stdout not in ready:
            continue
        chunk = os.read(proc.stdout.fileno(), QPDF_CHUNK_BYTES)
        if not chunk:
            complete = True
            continue
        got_output = True
        held += chunk
        if len(held) > QPDF_TAIL_HOLD_BYTES:
            ready_bytes, held = held[:-QPDF_TAIL_HOLD_BYTES], held[-QPDF_TAIL_HOLD_BYTES:]
            raw.feed(normalize_string(ready_bytes.decode("latin-1")))  # 1:1, nothing lost
    if complete:
        held = _strip_generated_ids(held, input_ids)
    raw.feed(normalize_string(held.decode("latin-1")))

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    # Any nonzero exit degrades the scan, and so does exit 3 unless every
    # warning is a kind the owner accepted whose byte check passes (owner
    # decision 2026-10-15; _qpdf_warnings_unverified). qpdf documents exit
    # 3 as "warnings", but a truncated input PDF exits 3 while emitting
    # only a partial QDF stream with tail objects silently absent, so
    # exit-3 output is certified only kind by kind, never wholesale.
    if proc.returncode != 0:
        why = None
        if proc.returncode == 3:
            args = proc.args if isinstance(proc.args, (list, tuple)) else []
            target = str(args[-2]) if len(args) >= 2 and args[-1] == "-" else ""
            why = _qpdf_warnings_unverified(
                bytes(err) if complete and not err_open and target else None,
                target, pdf_bytes)
        if proc.returncode != 3 or why is not None:
            detail = f" ({why})" if why else ""
            report.warn(
                "TOOL_EXIT_NONZERO",
                "Binary",
                f"Binary: qpdf exited {proc.returncode}{detail} — QDF output may be "
                "truncated, binary scan may be incomplete",
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
    input_ids: frozenset[str] | None = None,
    pdf_bytes: bytes | None = None,
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
            _collect_qpdf(procs["qpdf"], matcher, report, input_ids, pdf_bytes)
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
# docs/REDESIGN.md §4, §6: the console and --json report renderers
# (_sanitize_report_text, print_report, build_json_report,
# write_json_report, JSON_SCHEMA_VERSION) now live in
# redaction_verifier.report, re-exported above in the single guarded
# block.


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
        "--strict-classes",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="count the built-in classes' matches (ssn, credit-card, email, "
        "us-phone; a redactor config's entity_types) toward the verdict like "
        "any rule. By default they are reported as notices that never change "
        "the exit code (ADR 0012). A class rule with \"strict\": true always counts",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="FILE",
        help="also write a machine-readable report to FILE (stable warning "
        "codes, masked samples); the exit code is unchanged, except that a "
        "clean result becomes 2 if FILE cannot be written",
    )
    parser.add_argument(
        "--visible-map",
        type=Path,
        metavar="OUT.pdf",
        help="also write a masked map of where each finding is to OUT.pdf, a new file (an "
        "existing OUT.pdf is replaced only if this tool wrote it); built after the report, it "
        "never changes the verdict or the exit code, except exit 2 before scanning if OUT.pdf "
        "is unsafe to write (MAP_PATH_CONFLICT, MAP_PATH_UNWRITABLE)",
    )
    # --version works only on its own. As an argparse "version" action it
    # would also fire alongside --target (or abbreviated, as --v) and exit
    # 0 without scanning: the certified-clean code for a scan never run.
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args == ["--version"]:
        print(f"{parser.prog} {__version__}")
        return 0
    # argparse accepts any unambiguous prefix of an option, so --visible-map
    # would turn "--v X", an error before the flag existed, into a request
    # to write X. The flag is accepted only spelled out; every other
    # option's abbreviations keep working as they always have.
    # (Not alongside -h/--help, which argparse answers first, as before.)
    for arg in [] if {"-h", "--help"} & set(raw_args) else raw_args:
        name = arg.split("=", 1)[0]
        if len(name) > 2 and name != "--visible-map" and "--visible-map".startswith(name):
            parser.error(f"unrecognized arguments: {arg} (spell out --visible-map)")
    args = parser.parse_args(raw_args)

    pdf_path: Path = args.target
    report = ScanReport()
    json_path: Path | None = args.json
    # --visible-map: the job (redaction_verifier.visible_map.runner.MapJob)
    # and the sink the scan hands its truth to. Both stay None without the
    # flag, and nothing below evaluates anything of the map's then.
    map_job: Any = None
    sink: MapSink | None = None
    # SIGINT/SIGTERM handlers the map replaced, put back at the end.
    map_signals: dict[int, Any] = {}

    def finish(code: int, error: tuple[str, str] | None = None) -> int:
        report.settle_notices()
        if error:
            print(f"[ERROR] {error[1]}", file=sys.stderr)
        if json_path is not None:
            data = build_json_report(
                report, code, error, target=pdf_path, tool_version=__version__,
                ocr_available=_OCR_IMPORTS_OK,
                private_paths=(pdf_path, args.secrets),
            )
            if not write_json_report(json_path, data) and code == 0:
                # A caller that asked for the JSON report gates on it; a
                # missing report must not read as a certified pass (the
                # main path then prints a [NOT CERTIFIED] banner, never
                # a [PASS] first).
                return 2
        return code

    def _drop_stale_map() -> None:
        """The --json path refusals below end the run before the map's own
        path check, which is what removes an old map: remove it here too,
        best effort (only ever a file carrying both markers)."""
        if args.visible_map is None:
            return
        try:
            from redaction_verifier.visible_map import paths as map_paths

            inputs = [pdf_path, args.secrets] + ([args.json] if args.json else [])
            map_paths.check_inputs(args.visible_map, inputs)
            map_paths.remove_stale(args.visible_map)
        except Exception:
            pass

    def _map_after_report(final: int) -> None:
        """Build the --visible-map after the human report is out, from
        both of the scan's normal returns (never from finish() or crash()).

        Nothing here can reach *final*, or stdout: the map is built in a
        child process, and every failure — a KeyboardInterrupt included —
        ends as one stderr line and no map. All of it, the handler's own
        print and its cleanup of the child included, runs inside one
        suppress(BaseException), so an interrupt between any two steps is
        swallowed too; main()'s finally then kills whatever is left.

        A suppress's own __exit__ is Python code a signal can land in, so
        while the map is made SIGINT and SIGTERM do not raise at all: their
        handlers only stop the child (and the map), and they stay until the
        end of main()'s finally (_restore_map_signals), so a burst that
        outlasts the map cannot land in the cleanup either. The verdict was decided before the map began; a
        signal during it must not undo it (a burst of Ctrl-C used to turn a
        confirmed leak's 1 into the crash code).
        """
        if map_job is None or sink is None:
            return
        def stop_map(signum: int, frame: Any) -> None:
            map_job.interrupt()

        with contextlib.suppress(BaseException):
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    map_signals[signum] = signal.signal(signum, stop_map)
            except ValueError:
                pass            # not the main thread: signals stay as they are
            with contextlib.suppress(BaseException):
                _make_map(final)
        # The previous handlers come back only at the very end of main()'s
        # finally (_restore_map_signals): a burst that outlasts the map must
        # not land in the cleanup after it, where a KeyboardInterrupt would
        # turn the decided verdict into the crash code.

    def _restore_map_signals() -> None:
        for signum, handler in map_signals.items():
            with contextlib.suppress(BaseException):
                signal.signal(signum, handler)
        map_signals.clear()

    def _map_status(line: str) -> None:
        """One line of map status, on stderr only -- never stdout, which a
        closed fd 2 would otherwise make print() fall back to."""
        if sys.stderr is not None:
            with contextlib.suppress(Exception):
                print(_sanitize_report_text(line), file=sys.stderr)

    def _make_map(final: int) -> None:
        with contextlib.suppress(BaseException):
            sys.stdout.flush()
        try:
            from redaction_verifier.visible_map import runner as map_runner

            data = build_json_report(
                report, final, None, target=pdf_path, tool_version=__version__,
                ocr_available=_OCR_IMPORTS_OK, private_paths=(pdf_path, args.secrets),
            )
            items = (len(sink.hidden) + len(data["findings"]) + len(data["warnings"])
                     + len(data["notices"]) + sum(len(t) for t in sink.truth.values()))
            header = {
                "rules": map_runner.rules_header(rules),
                # ADR 0012: the rules whose matches are notices; the map
                # lists them and never boxes them.
                "informational": sorted(report.informational),
                "sink": sink.to_header(),
                "report": data,
                "exit_code": final,
                "scan_code": code,
                "target_name": pdf_path.name,
                "version": __version__,
            }
            shown = map_job.make(
                header, env=_minimal_subprocess_env(),
                cwd=tools_scratch.name if tools_scratch is not None else tempfile.gettempdir(),
                pages=sink.page_count or 0, items=items,
            )
            _map_status(f"[MAP] MAP_WRITTEN: visible map written to {args.visible_map.name}: "
                        f"{shown}")
        except BaseException as exc:
            with contextlib.suppress(BaseException):
                map_job.abort()
            failed = getattr(exc, "code", None) if hasattr(exc, "message") else None
            why = getattr(exc, "message", None) if failed else (
                f"internal error ({type(exc).__name__})")
            if map_job.wrote_map():
                # OUT already holds this run's map; only a step after
                # publishing failed (removing the temp file, an interrupt).
                _map_status(f"[MAP] MAP_WRITTEN: visible map written to {args.visible_map.name}; "
                            f"a later step failed ({failed or type(exc).__name__})")
            else:
                _map_status(f"[ERROR] {failed or 'MAP_FAILED'}: visible map not written: {why}")

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
                _drop_stale_map()
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
                _drop_stale_map()
                return finish(
                    2,
                    (
                        "JSON_PATH_UNWRITABLE",
                        f"cannot replace JSON report {bad.name}: " f"{exc.strerror or exc}",
                    ),
                )

        if args.visible_map is not None:
            # Checked like --json, before anything else can fail: a stale map
            # is removed here, so no run that ends without writing its own
            # leaves an earlier one behind. Refusing is the only way the map
            # can change the exit code.
            unchecked = ("MAP_PATH_UNWRITABLE", f"cannot check --visible-map "
                         f"{args.visible_map.name}")
            try:
                from redaction_verifier.visible_map import runner as map_runner
                from redaction_verifier.visible_map.paths import PathRefused
            except Exception as exc:
                return finish(2, (unchecked[0], f"{unchecked[1]} ({type(exc).__name__})"))
            try:
                inputs = [pdf_path, args.secrets] + ([json_path] if json_path else [])
                map_job = map_runner.prepare(args.visible_map, inputs)
            except PathRefused as exc:
                return finish(2, (exc.code, exc.message))
            except Exception as exc:
                return finish(2, (unchecked[0], f"{unchecked[1]} ({type(exc).__name__})"))
            sink = MapSink()

        if not pdf_path.is_file():
            return finish(2, ("TARGET_NOT_FOUND", f"Target PDF not found: {pdf_path}"))

        try:
            rules = load_rules(args.secrets)
        except VerifyError as exc:
            return finish(2, ("RULES_INVALID", str(exc)))
        secrets, patterns = rules.secrets, rules.patterns
        report.exact_index = MaskIndex(f for s in secrets for f in (s.normalized, s.folded) if f)
        # ADR 0012: which rules' matches are notices rather than verdict (by
        # default, every built-in class's). A malformed "strict" on a class
        # rule matters only then; --strict-classes reads rules as before.
        if not args.strict_classes and rules.strict_invalid:
            return finish(2, ("RULES_INVALID",
                              "'strict' must be true or false on class rule(s) "
                              f"{', '.join(repr(n) for n in sorted(rules.strict_invalid))}"))
        report.informational = informational_rules(
            {name: label.kind for name, label in rules.labels.items()},
            strict=args.strict_classes, strict_rules=rules.strict_rules,
        )

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
            # The file as qpdf is about to read it: if it is not the same
            # when its bytes are read for _qpdf_warnings_unverified, no
            # qpdf exit 3 is certified.
            qpdf_stat = _file_identity(pdf_path)
            if map_job is not None:
                # One read-only descriptor, hashed now and handed to the map's
                # child later; a failure only means no map.
                map_job.open_target(pdf_path)
            procs, tools_scratch = start_hidden_tools(pdf_path, report)

            try:
                doc = fitz.open(pdf_path)
            except Exception as exc:
                return finish(2, ("PDF_UNREADABLE", f"Cannot open PDF {pdf_path}: {exc}"))

            try:
                if doc.needs_pass:
                    return finish(2, ("PDF_PASSWORD", f"PDF is password-protected: {pdf_path}"))
                if sink is not None:
                    _offer(sink, "document", doc.page_count)

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
                    ("Hidden", functools.partial(scan_hidden_objects, sink=sink)),
                    ("Objects", functools.partial(scan_pdf_objects, pdf_path=pdf_path)),
                    ("Objects", functools.partial(scan_trailers, pdf_path)),
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
                        seam_variants=TEXT_SEAM_READINGS,
                        tails_into_next=TEXT_TAILS_INTO_NEXT_PAGE,
                        scatter_readings=TEXT_SCATTER_READINGS,
                        patterns=patterns, fail_fast=args.fail_fast, sink=sink,
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
                            # both are eligible for hard findings, except a
                            # vertical line no upright re-read confirms: that is
                            # whole only in the two raw readings after them
                            # (extract_ocr_text), reconstructions.
                            hard_variants=OCR_GENUINE_READINGS,
                            cross_page_reading=OCR_GENUINE_READINGS,
                            fail_fast=args.fail_fast,
                            incomplete=ocr_page_too_large,
                            sink=sink,
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
                    input_ids = None    # strip nothing qpdf wrote
                    pdf_bytes = None    # certify no qpdf exit 3
                    if procs["qpdf"] is not None:
                        try:
                            raw = pdf_path.read_bytes()
                            if (qpdf_stat is not None and _file_identity(pdf_path) == qpdf_stat
                                    and len(raw) == qpdf_stat[0]):
                                pdf_bytes = raw
                            input_ids = _input_document_ids(raw)
                        except Exception:
                            input_ids = None
                    check_hidden_layers(procs, matcher, patterns, report, input_ids, pdf_bytes)
                except Exception as exc:
                    report.warn(
                        "LAYER_CRASHED",
                        "Metadata/Binary",
                        f"Metadata/Binary: collection crashed ({exc}) — NOT fully scanned",
                    )

            # 2 = clean-so-far, but not certifiable as a real PASS.
            report.settle_notices()
            code = 1 if report.leaked else 2 if report.degraded else 0
            # Render first, print last: a renderer crash happens before any
            # JSON report exists (crash() then writes its own), and the
            # verdict printed is the one finish() settles on -- so a run
            # whose JSON report cannot be written never prints [PASS].
            rendered = io.StringIO()
            with contextlib.redirect_stdout(rendered):
                print_report(report, pdf_path, rules=rules)
            try:
                final = finish(code)
            except BaseException:
                if code == 1:  # a confirmed leak never moves down: show it
                    sys.stdout.write(rendered.getvalue())
                raise
            if final != code:
                print_report(report, pdf_path, rules=rules, json_unwritten=True)
                _map_after_report(final)
                return final
            try:
                sys.stdout.write(rendered.getvalue())
            except BaseException:
                # The JSON report already says this run's verdict; if the
                # human report cannot be printed, crash() rewrites it as an
                # error -- and should that fail too, no JSON report must be
                # left behind saying more than the exit code does.
                if json_path is not None:
                    with contextlib.suppress(OSError):
                        json_path.unlink()
                raise
            _map_after_report(final)
            return final
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
            if map_job is not None:
                # Kill a child an interrupt left running: the map's cleanup,
                # like the map, must never reach the verdict.
                with contextlib.suppress(BaseException):
                    map_job.abort()
                try:
                    map_job.close()
                except Exception as exc:
                    _map_status(f"[ERROR] could not close the visible map's target: {exc}")
                _restore_map_signals()
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
