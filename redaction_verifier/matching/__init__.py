"""redaction_verifier.matching — the normalizer, value matcher and pattern
scanning engine.

Split into modules per docs/REDESIGN.md §4 ("matching/ values, patterns,
tiers"): values.py (normalizer, SecretMatcher, RollingScanner),
validators.py (the pattern-class validators), patterns.py (PatternRule,
the built-in classes, match_patterns, PatternScanner). This module
re-exports everything so callers can do `from redaction_verifier.matching
import X` without knowing which submodule defines it.
"""

from __future__ import annotations

from redaction_verifier.matching.patterns import (
    BUILTIN_PATTERN_CLASSES,
    PATTERN_SCAN_BATCH,
    PATTERN_SCAN_OVERLAP,
    PatternRule,
    PatternScanner,
    _fold_for_patterns,
    match_patterns,
    mask,
)
from redaction_verifier.matching.validators import (
    _luhn_ok,
    _valid_card,
    _valid_email,
    _valid_nanp,
    _valid_ssn,
)
from redaction_verifier.matching.values import (
    RollingScanner,
    SecretMatcher,
    normalize_string,
)

__all__ = [
    "BUILTIN_PATTERN_CLASSES",
    "PATTERN_SCAN_BATCH",
    "PATTERN_SCAN_OVERLAP",
    "PatternRule",
    "PatternScanner",
    "RollingScanner",
    "SecretMatcher",
    "_fold_for_patterns",
    "_luhn_ok",
    "_valid_card",
    "_valid_email",
    "_valid_nanp",
    "_valid_ssn",
    "mask",
    "match_patterns",
    "normalize_string",
]
