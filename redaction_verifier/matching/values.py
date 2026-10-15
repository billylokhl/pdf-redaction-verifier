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

    def scattered(self, lines: Sequence[str], window: int,
                  across: int | None = None) -> list[str]:
        """Names of secrets that two or three pieces of *lines* (each
        normalized) assemble, in any order: the end of one line, then
        optionally one whole line, then the start of another, each piece
        from a different line, all within *window* consecutive lines.

        So a value whose parts lie apart, with a header or other short
        text between them or in the reverse order, is found; it never
        takes more than three pieces or reaches past *window* lines, so
        unrelated lines across a page cannot be pooled into a value. With
        *across* set, only assemblies using a line on each side of index
        *across* (lines[:across] and lines[across:]) count.
        """
        names: set[str] = set()
        n_lines = len(lines)
        last_chars = {line[-1] for line in lines if line}
        first_chars = {line[0] for line in lines if line}
        for secret in self._secrets:
            v = secret.normalized
            n = len(v)
            if n < 2:
                continue
            # Cut points k (1..n-1) whose first piece v[:k] could end a line
            # (its last character is some line's last character), and whose
            # last piece v[k:] could start one: a secret with none is
            # skipped before any line is compared, which keeps the cost on
            # a page of many lines and many rules near one pass per rule.
            cuts_end = [k for k in range(1, n) if v[k - 1] in last_chars]
            cuts_start = [k for k in range(1, n) if v[k] in first_chars]
            if not cuts_end or not cuts_start:
                continue
            # ends[i]: cut points k where lines[i] ends with v[:k];
            # starts[i]: cut points k where lines[i] starts with v[k:].
            ends = [{k for k in cuts_end if line.endswith(v[:k])} if line else set()
                    for line in lines]
            if not any(ends):
                continue
            starts = [{k for k in cuts_start if line.startswith(v[k:])} if line else set()
                      for line in lines]
            if not any(starts):
                continue
            found = False
            for i in range(n_lines):
                if not ends[i] or found:
                    continue
                low, high = max(0, i - window + 1), min(n_lines, i + window)
                for j in range(low, high):
                    if j == i or not starts[j]:
                        continue
                    # Two pieces: v[:k] ends line i, v[k:] starts line j.
                    if ends[i] & starts[j] and (
                            across is None or (i < across) != (j < across)):
                        found = True
                        break
                    # Three pieces: a whole line m in the middle.
                    for m in range(low, high):
                        if m in (i, j) or abs(m - j) >= window or not lines[m]:
                            continue
                        if across is not None and len({x < across for x in (i, j, m)}) < 2:
                            continue
                        mid = lines[m]
                        for k in ends[i]:
                            if v.startswith(mid, k) and (k + len(mid)) in starts[j]:
                                found = True
                                break
                        if found:
                            break
                    if found:
                        break
            if found:
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
