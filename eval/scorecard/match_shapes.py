"""The shape of a built-in pattern-class match: what a false hard looks
like, for Phase 5's measurement (docs/REDESIGN.md §1, ADR 0005).

Pure and dependency-free: `scorecard.pattern_recorder` calls `classify`
inside the verifier's own process, on the matched text and the text
around it, and keeps only the shape's name. The matched text itself is
never stored or printed, so a corpus run publishes counts per shape and
nothing else.

A shape is evidence about *why* a clean file's text matched a class, not
a verdict: "formatted" is a match that already looks like the real thing
(a dashed SSN, a punctuated phone number), the shape a context rule could
not tell apart from a secret, while "uuid", "hex-id" and "date-or-time"
are digit runs inside something else. The categories are checked in the
order of `SHAPES`, so a dashed run inside a UUID is "uuid", not
"formatted".
"""

from __future__ import annotations

import hashlib
import re

# The environment variable naming the file pattern_recorder writes its
# records to (here, so pattern_baseline need not import the recorder,
# which patches the verifier on import).
OUT_ENV = "PATTERN_RECORDER_OUT"

SHAPES: tuple[str, ...] = (
    "email-address",  # the email class: a true positive for "an address is here" (ADR 0005)
    "uuid",           # inside an 8-4-4-4-12 hex UUID
    "hex-id",         # inside a token whose letters are all hex digits (a hash, an object id)
    "date-or-time",   # a YYYYMMDD run, a PDF date (D:...), or epoch seconds/milliseconds
    "alnum-id",       # inside a token with other letters (a serial, a part number)
    "formatted",      # separators grouping the digits as the class's real values are
    "number-list",    # separators, but grouped otherwise (a list of small numbers)
    "bare-digits",    # a plain digit run standing alone (a number, an id)
)

_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_TOKEN_CHAR = re.compile(r"[0-9A-Za-z_-]")
_HEX_LETTERS = frozenset("abcdefABCDEF")
# Seconds and milliseconds since the epoch, 2000-01-01 to 2040-01-01.
_EPOCH_SECONDS = (946_684_800, 2_208_988_800)
_EPOCH_MILLIS = (_EPOCH_SECONDS[0] * 1000, _EPOCH_SECONDS[1] * 1000)


def record_key(rule: str, masked_sample: str) -> str:
    """The join key between a recorder record and a --json finding: a hash,
    so the record file holds neither the match nor its masked sample."""
    return hashlib.sha256(f"{rule}\0{masked_sample}".encode()).hexdigest()[:24]


# Digit-group lengths of each class's real written forms.
def _canonical_groups(class_name: str, groups: list[int]) -> bool:
    if class_name == "ssn":
        return groups == [3, 2, 4]
    if class_name == "us-phone":
        return groups in ([3, 3, 4], [1, 3, 3, 4])
    if class_name == "credit-card":
        return (groups in ([4, 6, 5], [4, 6, 4])
                or (len(groups) >= 3 and all(g == 4 for g in groups[:-1])
                    and 1 <= groups[-1] <= 4))
    return False


def _token_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen [start, end) over the letters, digits, '_' and '-' around it."""
    while start > 0 and _TOKEN_CHAR.match(text[start - 1]):
        start -= 1
    while end < len(text) and _TOKEN_CHAR.match(text[end]):
        end += 1
    return start, end


def _yyyymmdd(digits: str) -> bool:
    if len(digits) != 8:
        return False
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
    return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31


def _date_like(digits: str, text: str, token_start: int) -> bool:
    if text[max(0, token_start - 2):token_start] == "D:":
        return True  # a PDF date string, D:YYYYMMDDHHmmSS
    if _yyyymmdd(digits[:8]) or _yyyymmdd(digits[-8:]):
        return True
    if len(digits) == 10 and _EPOCH_SECONDS[0] <= int(digits) <= _EPOCH_SECONDS[1]:
        return True
    return len(digits) == 13 and _EPOCH_MILLIS[0] <= int(digits) <= _EPOCH_MILLIS[1]


def classify(class_name: str, text: str, start: int, end: int) -> str:
    """The shape (one of SHAPES) of the match text[start:end] of the
    built-in class *class_name*. *text* is the text the class ran on, so
    the characters around the match are its context."""
    if class_name == "email":
        return "email-address"
    for m in _UUID.finditer(text, max(0, start - 36), min(len(text), end + 36)):
        if m.start() < end and start < m.end():
            return "uuid"
    token_start, token_end = _token_span(text, start, end)
    letters = {c for c in text[token_start:token_end] if c.isalpha()}
    if letters and letters <= _HEX_LETTERS:
        return "hex-id"
    matched = text[start:end]
    # Only an unbroken digit run is read as a date: a dashed SSN or a
    # punctuated phone number is "formatted" even when its digits happen to
    # spell a date or an epoch time ("(212) 555-0147" is 2125550147 s).
    if matched.isdigit() and _date_like(matched, text, token_start):
        return "date-or-time"
    if letters:
        return "alnum-id"
    if not matched.isdigit():
        groups = [len(g) for g in re.findall(r"\d+", matched)]
        return "formatted" if _canonical_groups(class_name, groups) else "number-list"
    return "bare-digits"
