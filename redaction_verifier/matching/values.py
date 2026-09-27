"""redaction_verifier.matching.values — the normalizer and value matcher.

Moved verbatim from verify.py as Phase 2, step 1 ("Move") of
docs/REDESIGN.md: normalize_string, SecretMatcher, RollingScanner.
Behaviour is byte-identical to the code this replaced.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from redaction_verifier.model import Secret

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


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
