"""redaction_verifier.matching.patterns — pattern-class rules and scanning.

Moved verbatim from verify.py as Phase 2, step 1 ("Move") of
docs/REDESIGN.md: PatternRule, the built-in pattern classes, mask,
match_patterns and PatternScanner. Behaviour is byte-identical to the code
this replaced.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Collection, Sequence

from redaction_verifier.matching.validators import (
    _valid_card,
    _valid_email,
    _valid_nanp,
    _valid_ssn,
)

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


# Pattern scanning: matches may span feed boundaries up to the overlap;
# feeds are batched before regex sweeps (per-tiny-literal sweeps measure
# ~100x slower than batched ones).
PATTERN_SCAN_OVERLAP: int = 512
PATTERN_SCAN_BATCH: int = 64 << 10


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
