"""redaction_verifier.matching.validators — pattern-class validators.

Moved verbatim from verify.py as Phase 2, step 1 ("Move") of
docs/REDESIGN.md: the Luhn, SSA, card, email and NANP validators that
suppress structurally invalid pattern-class matches. Behaviour is
byte-identical to the code this replaced.
"""

from __future__ import annotations

import re


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _valid_ssn(matched: str) -> bool:
    """SSA-issued SSNs never use area 000/666/9xx, group 00, or serial 0000."""
    d = re.sub(r"\D", "", matched)
    return not (
        d[:3] in ("000", "666") or d[0] == "9" or d[3:5] == "00" or d[5:] == "0000"
    )


def _valid_card(matched: str) -> bool:
    d = re.sub(r"\D", "", matched)
    if not (13 <= len(d) <= 19 and len(set(d)) > 1 and _luhn_ok(d)):
        return False
    # PDF date stamps (D:YYYYMMDDHHmmSS) are 14-digit runs that pass Luhn
    # ~10% of the time; no real 14-digit card IIN starts with 19 or 20.
    if len(d) == 14 and d[:2] in ("19", "20"):
        return False
    return True


_EMAIL_FILE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "tif", "tiff",
    "pdf", "eps", "ico", "heic",
})


def _valid_email(matched: str) -> bool:
    """Reject retina-asset-style filenames like logo@2x.png."""
    return matched.rsplit(".", 1)[-1].lower() not in _EMAIL_FILE_EXTENSIONS


def _valid_nanp(matched: str) -> bool:
    """NANP: 10 digits (optionally +1); area and exchange start with 2-9."""
    d = re.sub(r"\D", "", matched)
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789" and d[3] in "23456789"
