"""The shape of a built-in pattern-class match: what a false hard looks
like, for Phase 5's measurement (docs/REDESIGN.md §1, ADR 0005).

Pure and dependency-free: `scorecard.pattern_recorder` calls `classify`
inside the verifier's own process, on the matched text and the text
around it, and keeps only the shape's name. The matched text itself is
never stored or printed, so a corpus run publishes counts per shape and
nothing else.

A shape is evidence about *why* a clean file's text matched a class, not
a verdict. "formatted" is a match grouped exactly as the class's real
values are (a dashed SSN, a punctuated phone number) -- the shape a
context rule could not tell apart from a secret, so it is decided first,
even when a letter touches the match ("SSN123-45-6789"). "uuid", "hex-id",
"alnum-id" and "date-or-time" are digit runs inside something else. Where
the evidence is thin the classifier errs toward the shapes no option
demotes: a date needs context (a PDF "D:" prefix or a full timestamp), and
a token stops at '-', so "ID-123456789" is "bare-digits", not "alnum-id".
"""

from __future__ import annotations

import hashlib
import re

# The environment variables pattern_recorder reads (here, so
# pattern_baseline need not import the recorder): the file it writes its
# records to, and the per-run salt for their keys.
OUT_ENV = "PATTERN_RECORDER_OUT"
SALT_ENV = "PATTERN_RECORDER_SALT"

SHAPES: tuple[str, ...] = (
    "email-address",  # the email class: a true positive for "an address is here" (ADR 0005)
    "formatted",      # separators grouping the digits as the class's real values are
    "uuid",           # inside an 8-4-4-4-12 hex UUID
    "hex-id",         # inside a token whose letters are all hex digits (a hash, an object id)
    "date-or-time",   # a PDF date (D:...), a YYYYMMDDhhmm... timestamp, epoch milliseconds
    "alnum-id",       # inside a token with other letters (a serial, a part number)
    "number-list",    # separators, but grouped otherwise (a list of small numbers)
    "bare-digits",    # a plain digit run standing alone (a number, an id)
)

_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_TOKEN_CHAR = re.compile(r"[0-9A-Za-z_]")
_HEX_LETTERS = frozenset("abcdefABCDEF")
# Milliseconds since the epoch, 2000-01-01 to 2040-01-01. Epoch *seconds*
# are not read as a date: ten digits from 0946... to 2208... are also every
# bare phone number in area codes 201-220.
_EPOCH_MILLIS = (946_684_800_000, 2_208_988_800_000)


def record_key(salt: str, rule: str, masked_sample: str) -> str:
    """The join key between a recorder record and a --json finding: a salted
    hash, so the record file holds neither the match nor its masked sample,
    and without the run's salt (never written) the masked sample's last
    digits cannot be recovered from it by trying every value."""
    return hashlib.sha256(f"{salt}\0{rule}\0{masked_sample}".encode()).hexdigest()[:32]


def _canonical_groups(class_name: str, groups: list[int]) -> bool:
    """Digit-group lengths of each class's real written forms."""
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
    """Widen [start, end) over the letters, digits and '_' touching it."""
    while start > 0 and _TOKEN_CHAR.match(text[start - 1]):
        start -= 1
    while end < len(text) and _TOKEN_CHAR.match(text[end]):
        end += 1
    return start, end


def _yyyymmdd(digits: str) -> bool:
    if len(digits) != 8 or not digits.isdigit():
        return False
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
    return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31


def _date_like(matched: str, token: str, text: str, token_start: int) -> bool:
    if text[max(0, token_start - 2):token_start] == "D:" and _yyyymmdd(token[:8]):
        return True  # a PDF date string, D:YYYYMMDDHHmmSS
    if len(token) >= 12 and token.isdigit() and _yyyymmdd(token[:8]):
        return True  # a YYYYMMDDhhmm[ss] timestamp the match sits inside
    return len(matched) == 13 and _EPOCH_MILLIS[0] <= int(matched) <= _EPOCH_MILLIS[1]


def classify(class_name: str, text: str, start: int, end: int) -> str:
    """The shape (one of SHAPES) of the match text[start:end] of the
    built-in class *class_name*. *text* is the text the class ran on, so
    the characters around the match are its context."""
    if class_name == "email":
        return "email-address"
    matched = text[start:end]
    formatted = not matched.isdigit()
    if formatted and _canonical_groups(
            class_name, [len(g) for g in re.findall(r"\d+", matched)]):
        return "formatted"
    for m in _UUID.finditer(text, max(0, start - 36), min(len(text), end + 36)):
        if m.start() < end and start < m.end():
            return "uuid"
    token_start, token_end = _token_span(text, start, end)
    token = text[token_start:token_end]
    letters = {c for c in token if c.isalpha()}
    if letters and letters <= _HEX_LETTERS:
        return "hex-id"
    if not formatted and _date_like(matched, token, text, token_start):
        return "date-or-time"
    if letters:
        return "alnum-id"
    return "number-list" if formatted else "bare-digits"
