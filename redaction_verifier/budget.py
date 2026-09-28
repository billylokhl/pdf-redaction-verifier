"""redaction_verifier.budget — one work-based budget for the whole run.

docs/REDESIGN.md §4 ("Budget") and docs/adr/0006: depth, units, inflated
bytes and OCR pixels, all counted as work done (Principle 6), never wall
clock. Exhaustion is not an exception and not a special exit rule: the
``charge_*`` methods return False, the caller marks what it was doing
``FLAGGED``, and ``Budget.flags()`` reports each exhausted counter once as
a BUDGET_EXHAUSTED flag (Principle 1 then keeps the file from exiting 0).
Nothing here ever raises.

The numbers are ADR 0006's accepted *placeholders*, not measurements. In
particular ``max_units`` is re-derived from Phase 3a's own measurements
of real unit counts (object-stream members and decoder-discovered
children included), with generous headroom, and re-checked in 4c/4d;
hitting the cap means the file cannot exit 0 (FLAGGED; a confirmed hard
finding still exits 1) (owner decision, 2026-09-27).

OCR (for Phase 4b): the page count a Budget is built with must come from
enumerating the page tree's leaves (qpdf's anchored count, §4), never
from a /Count entry a file can inflate; OCR pixels are charged once per
stored image, not once per place it is drawn.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .ledger import Flag, FlagReason

GIB = 1 << 30


@dataclass(frozen=True)
class Limits:
    """Named limits (Principle 9). The first four are ADR 0006's."""

    # Decoding recursion only: a form Do, a Type3 glyph, a nested PDF or
    # container entry. Reference-graph walks are iterative instead.
    max_depth: int = 25
    # Units decoded per run. Placeholder: re-derived in Phase 3a (above).
    max_units: int = 200_000
    # Bytes we inflate ourselves, cumulative over the run.
    max_inflated_bytes: int = 2 * GIB
    # Stored-image OCR pixels per page; the run cap is this times the
    # page count the parent anchors (ocr_run_pixels)...
    max_ocr_pixels_per_page: int = 200_000_000
    # ...but never above this flat ceiling (20 Gpx: 100 pages at the full
    # per-page cap), so a file cannot buy unbounded OCR with page count.
    max_ocr_run_pixels: int = 20_000_000_000
    # Parse limits for the inventory's own object parser. Real objects
    # nest a handful of levels deep; the cap keeps the iterative parser's
    # stack bounded. Placeholders re-checked by the Phase 3a corpus gate.
    max_container_depth: int = 256
    # Tokens in one indirect object (not its stream data): large /Widths,
    # /Kids or /Nums arrays run to tens of thousands; this is headroom.
    max_tokens_per_object: int = 1_000_000
    # Digits in one number token: PDF's own implementation limits are far
    # smaller, and Python's int() refuses more than 4,300 digits.
    max_number_digits: int = 64
    # Flags one object keeps; the rest are counted in a FLAGS_TRUNCATED
    # flag, so a hostile object cannot grow the report without bound.
    max_flags_per_object: int = 64
    # Owners a CONTESTED region lists (tile() records the true claimant
    # count beside them): bounds the tiling at O(n) for n claims however
    # many claims overlap.
    max_contested_owners: int = 16

    def ocr_run_pixels(self, page_count: int) -> int:
        """ADR 0006's derived whole-run OCR cap, capped by the flat
        ceiling; no pages, no budget."""
        derived = self.max_ocr_pixels_per_page * max(page_count, 0)
        return min(derived, self.max_ocr_run_pixels)


class Counter(IntEnum):
    """Which counter a BUDGET_EXHAUSTED flag is about (its ``counter``)."""

    DEPTH = 1
    UNITS = 2
    INFLATED_BYTES = 3
    OCR_PAGE_PIXELS = 4
    OCR_RUN_PIXELS = 5


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class Budget:
    """The run's mutable counters. Exhaustion is sticky: once a counter
    refuses a charge it refuses every later one (the per-page OCR cap:
    for that page), so work cut short can never silently resume. A
    malformed charge (negative, not an int) is refused the same way --
    fail closed, never raise."""

    def __init__(self, limits: Limits | None = None, page_count: int = 0) -> None:
        self.limits = limits if limits is not None else Limits()
        self.page_count = page_count if _is_count(page_count) else 0
        self.depth = 0  # the deepest level accepted so far
        self.units = 0
        self.inflated_bytes = 0
        self.ocr_pixels = 0
        self.ocr_pixels_by_page: dict[int, int] = {}
        self._pages_exhausted: set[int] = set()
        self._exhausted: dict[Counter, Flag] = {}

    def _refuse(self, counter: Counter, limit: int, used: int, requested: object) -> bool:
        if counter not in self._exhausted:
            # int(): a Flag takes plain ints only, and a caller's count
            # may be an int subclass that _is_count accepts.
            params = [("counter", int(counter)), ("limit", int(limit)), ("used", int(used))]
            if isinstance(requested, int) and _is_count(requested):
                params.append(("requested", int(requested)))
            self._exhausted[counter] = Flag(FlagReason.BUDGET_EXHAUSTED, None, tuple(params))
        return False

    def exhausted(self, counter: Counter) -> bool:
        """Has *counter* refused a charge? For OCR_PAGE_PIXELS this means
        some page hit its cap (or a charge was malformed or named a page
        outside the count); other pages may still be charged."""
        return counter in self._exhausted

    def charge_depth(self, depth: int) -> bool:
        """May decoding recurse to *depth* (the root is 0)?"""
        limit = self.limits.max_depth
        if self.exhausted(Counter.DEPTH) or not _is_count(depth) or depth > limit:
            return self._refuse(Counter.DEPTH, limit, self.depth, depth)
        self.depth = max(self.depth, depth)
        return True

    def charge_units(self, n: int = 1) -> bool:
        limit = self.limits.max_units
        if self.exhausted(Counter.UNITS) or not _is_count(n) or self.units + n > limit:
            return self._refuse(Counter.UNITS, limit, self.units, n)
        self.units += n
        return True

    def charge_inflated_bytes(self, n: int) -> bool:
        limit = self.limits.max_inflated_bytes
        if (self.exhausted(Counter.INFLATED_BYTES) or not _is_count(n)
                or self.inflated_bytes + n > limit):
            return self._refuse(Counter.INFLATED_BYTES, limit, self.inflated_bytes, n)
        self.inflated_bytes += n
        return True

    def charge_ocr_pixels(self, page: int, n: int) -> bool:
        """Charge *n* stored-image OCR pixels to 0-based *page*, against
        both the per-page cap and the run cap derived from page_count. A
        page outside the anchored count is refused; a page that hit its
        cap stays refused, other pages keep their own allowance. The page
        cap is checked first; a malformed charge counts against it."""
        page_limit = self.limits.max_ocr_pixels_per_page
        if not _is_count(n) or not _is_count(page) or page >= self.page_count:
            return self._refuse(Counter.OCR_PAGE_PIXELS, page_limit, 0, n)
        used = self.ocr_pixels_by_page.get(page, 0)
        if page in self._pages_exhausted or used + n > page_limit:
            self._pages_exhausted.add(page)
            return self._refuse(Counter.OCR_PAGE_PIXELS, page_limit, used, n)
        run_limit = self.limits.ocr_run_pixels(self.page_count)
        if self.exhausted(Counter.OCR_RUN_PIXELS) or self.ocr_pixels + n > run_limit:
            return self._refuse(Counter.OCR_RUN_PIXELS, run_limit, self.ocr_pixels, n)
        self.ocr_pixels_by_page[page] = used + n
        self.ocr_pixels += n
        return True

    def flags(self) -> tuple[Flag, ...]:
        """One BUDGET_EXHAUSTED flag per exhausted counter, in counter order."""
        return tuple(self._exhausted[c] for c in sorted(self._exhausted))
