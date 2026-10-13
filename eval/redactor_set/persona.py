"""The fabricated household every real-redactor-set document is about.

Every value here is fabricated and chosen so it cannot belong to anyone:
an SSN area that is never issued (666) or the repo's own fabricated
123-45-6789, an EIN prefix that is never issued (00), a routing number
whose Federal Reserve prefix (99) is unassigned, the card networks' test
number, and example.com. No two values share a run of six or more
digits, so a hit on one is never mistaken for another, and the dashed
SSN is the only form of 123-45-6789 ever written: its undashed digits
also occur in common font tables (README, Binary layer).

One persona and one redactor config drive every document, the way the
owner uses one config for all of their files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Secret:
    id: str
    value: str          # the configured form, exactly as in exact_values
    kind: str           # what it is, for the reader of a truth manifest


SECRETS: tuple[Secret, ...] = (
    Secret("NAME1", "Jordan Q. Testperson", "person name"),
    Secret("NAME2", "Avery R. Samplename", "person name"),
    Secret("SSN1", "123-45-6789", "SSN"),
    Secret("SSN2", "666-38-2914", "SSN"),
    Secret("ADDR", "1600 Fictional Ave", "street address"),
    Secret("ACCT", "731904628155", "bank account number"),
    Secret("ROUT", "997382611", "routing number"),
    Secret("CARD", "4111 1111 1111 1111", "card number"),
    Secret("EIN", "00-4857261", "employer id"),
    Secret("DOB", "03/14/1971", "date of birth"),
    Secret("EMAIL", "jordan.testperson@example.com", "email"),
)
BY_ID = {s.id: s for s in SECRETS}

# Written forms a document may carry that the config does not list
# verbatim. Each maps back to its secret: the oracle and the verifier both
# count it as that secret. (T2 in the location taxonomy.)
VARIANTS: dict[str, str] = {
    "666382914": "SSN2",               # undashed SSN
    "TESTPERSON, JORDAN Q": "NAME1",   # surname-first word order
    "7319-0462-8155": "ACCT",          # regrouped account number
}

# The redactor config's patterns: the same two the example config ships
# for SSNs and card numbers. The SSN pattern is dashed-only on purpose,
# like the example's, so an undashed SSN tests a realistic gap.
PATTERNS: tuple[str, ...] = (
    r"\d{3}-\d{2}-\d{4}",
    r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
)


def written_forms() -> dict[str, str]:
    """Every string a document may write for a secret -> its secret id."""
    forms = {s.value: s.id for s in SECRETS}
    forms.update(VARIANTS)
    return forms


def _yaml_str(value: str) -> str:
    # Double-quoted YAML scalar: never parsed as a number, a date or
    # octal. None of the values need escaping beyond backslash and quote.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def redactor_config_yaml() -> str:
    """The fabricated redactor config: the same keys and structure as a
    real one (exact values, patterns, entity types), LLM detection off.
    It is also the verifier's --secrets file (README, "Shared YAML
    config"), so one file drives both."""
    lines = [
        "# Fabricated values only (eval/redactor_set/persona.py).",
        "entity_types: []",
        "exact_values:",
        *(f"  - {_yaml_str(s.value)}" for s in SECRETS),
        "patterns:",
        *(f"  - '{p}'" for p in PATTERNS),
        "scrub_metadata: true",
        "",
    ]
    return "\n".join(lines)


def digit_runs(value: str, length: int = 6) -> set[str]:
    digits = re.sub(r"\D", "", value)
    return {digits[i:i + length] for i in range(len(digits) - length + 1)}


def shared_digit_runs() -> list[tuple[str, str, str]]:
    """(id, id, run) for every pair of written forms of *different*
    secrets sharing a run of six or more digits. Must be empty."""
    forms = list(written_forms().items())
    found = []
    for i, (a, a_id) in enumerate(forms):
        for b, b_id in forms[i + 1:]:
            if a_id == b_id:
                continue
            for run in sorted(digit_runs(a) & digit_runs(b)):
                found.append((a_id, b_id, run))
    return found
