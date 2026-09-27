"""The case model: one PDF, whether a secret is really in it, what the
tool should say about it, and the story of how the redaction failed.

A case is the unit of the evaluation harness (docs/REDESIGN.md §5). The
same catalogue drives the test suite, the scorecard's corpus, the
evidence behind COVERAGE.md, and the gallery of how redaction fails.

Ids are ``<family>.<slug>`` where the family says where the content is
(page, layout, document, attachment, leftover, revision, file). An id
never changes: baselines, accepted differences and gallery links key on
it. Whether the file is clean, whether the tool gets it right today, and
who made the file are fields, never part of the id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cells import CELLS, NEW_CELL_ALLOWLIST

# The values every case plants unless it brings its own rules. Fabricated:
# the SSN is the canonical example number, never a real person's.
SSN = "123-45-6789"
CODE = "BLUEHERON"
DEFAULT_RULES: tuple[dict[str, str], ...] = (
    {"name": "SSN", "value": SSN},
    {"name": "Code", "value": CODE},
)

FAMILIES = frozenset({"page", "layout", "document", "attachment", "leftover",
                      "revision", "file"})
TRUTHS = frozenset({"leak", "clean"})
WRITERS = frozenset({
    "fitz",        # PyMuPDF — also the library the tool reads with
    "raw",         # hand-assembled bytes (eval/caselib/rawpdf.py)
    "qpdf",        # fitz output rewritten by qpdf
    "file",        # a committed file (real tool output), used as is
})
ORIGINS = frozenset({"generated", "redactor", "redteam"})
# "no-ocr": judged only where OCR is absent — for a gap in the text layer
# that OCR happens to cover on macOS.
REQUIREMENTS = frozenset({"ocr", "qpdf", "exiftool", "no-ocr"})
STORAGE = frozenset({"live", "orphaned", "unreferenced", "superseded"})

_ID_RE = re.compile(r"^[a-z]+\.[a-z0-9]+(?:-[a-z0-9]+)*$")
# A red-team case may cite a storage place COVERAGE.md has no row for yet
# (cells.py otherwise rejects unknown ids outright). It must still be
# listed in cells.NEW_CELL_ALLOWLIST with a tracking issue, or CI fails —
# see eval/README.md and eval/caselib/redteam/README.md.
_NEW_CELL_RE = re.compile(r"^new\.[a-z0-9]+(?:-[a-z0-9]+)*$")

# Leak cases missing a gallery field (mistake, recovery) they should
# eventually carry, kept for cases nobody has filled in yet. May only
# shrink — filling either field must remove the id here.
GALLERY_FIELDS_PENDING: frozenset[str] = frozenset()

# The kinds tests/test_case_library.py's privacy scrub reports. A
# Case.privacy_allowlist entry must name exactly one of these — matched
# only against a finding of that same kind, never any other.
PRIVACY_KINDS = frozenset({"home-path", "email", "hostname", "xmp-id"})


@dataclass(frozen=True)
class Expect:
    """A verdict and what must appear in the report.

    *findings* are (rule, storage) hard findings — exact per rule: a rule
    listed here may not also be found in a storage class not listed; *warnings* are
    (code, storage or None) warnings; *layers* are (rule, layer) — the
    layer that must report the finding, for cases whose cell is about one
    layer's reading. The report may hold more findings and review or
    scope warnings, but no coverage warning it does not list: an
    unexpected "could not read" is a regression, not noise.

    Warning codes are the current tool's (verify.WARNING_CODES); the
    redesign maps them when the legacy path retires.
    """

    exit: int
    findings: frozenset[tuple[str, str]] = frozenset()
    warnings: frozenset[tuple[str, str | None]] = frozenset()
    layers: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        if self.exit not in (0, 1, 2):
            raise ValueError(f"exit must be 0, 1 or 2, not {self.exit}")
        if self.exit != 1 and self.findings:
            raise ValueError("findings imply exit 1")
        bad = {s for _, s in self.findings} - STORAGE
        bad |= {s for _, s in self.warnings if s is not None} - STORAGE
        if bad:
            raise ValueError(f"unknown storage class(es) {sorted(bad)}")


def expect(exit: int, findings: tuple[tuple[str, str], ...] = (),
           warnings: tuple[tuple[str, str | None], ...] = (),
           layers: tuple[tuple[str, str], ...] = ()) -> Expect:
    return Expect(exit, frozenset(findings), frozenset(warnings), frozenset(layers))


@dataclass(frozen=True)
class KnownGap:
    """Today's tool gets this case wrong. *today* pins exactly what it
    reports instead, so any change — a fix, a partial fix (0 → 2), a
    broken generator — fails the test until the label is updated."""

    cell: str
    today: Expect


@dataclass(frozen=True)
class Case:
    id: str
    truth: str                        # "leak": a secret is in the file; "clean": none is
    cells: tuple[str, ...]            # where the planted secret is (leak cases only)
    expected: Expect                  # what a correct verifier reports
    story: str
    build: Callable[[Path], None] = field(repr=False, compare=False)
    features: tuple[str, ...] = ()    # other cells the document exercises (context)
    writer: str = "fitz"
    origin: str = "generated"
    known_gap: KnownGap | None = None
    mistake: str = ""                 # which tool or habit causes it
    recovery: str = ""                # how the secret is recovered by hand
    rules: tuple[dict[str, str], ...] = DEFAULT_RULES
    requires: frozenset[str] = frozenset()   # tools needed to judge it
    # Grid cases: one member of a parameterised family (the grid's name and
    # this member's parameters). The id is the grid's slug plus the values.
    grid: str | None = None
    params: tuple[tuple[str, str], ...] = ()
    # Explicit exceptions for tests/test_case_library.py's privacy scrub
    # (a committed binary's bytes, or its decompressed streams, matching a
    # home-directory path, an email, a hostname or a machine-generated XMP
    # id): ({"kind": <one of PRIVACY_KINDS>, "pattern": <full-match regex>,
    # "reason": <why it's fine>}, ...). "kind" must match the finding's own
    # kind exactly (an "email" entry never excuses a "hostname" finding);
    # "pattern" is matched with re.fullmatch against the finding's text, not
    # as a substring, and must not be trivially broad (bare "." or ".*").
    # Only meaningful for a case whose bytes are committed as-is
    # (writer="file"); every entry is checked by the scrub test itself.
    privacy_allowlist: tuple[dict[str, str], ...] = ()
    # A large performance file (docs/REDESIGN.md §5's representative-file
    # requirement): slow to build and/or slow to scan on purpose, so it is
    # excluded from the default test run and the build lock (see
    # tests/test_case_library.py and caselib.lock.lockable).
    perf: bool = False

    @property
    def family(self) -> str:
        return self.id.split(".", 1)[0]

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id) or self.family not in FAMILIES:
            raise ValueError(f"case id {self.id!r} must be <family>.<kebab-slug>, "
                             f"family one of {sorted(FAMILIES)}")
        if self.truth not in TRUTHS:
            raise ValueError(f"{self.id}: truth must be leak or clean")
        if (self.truth == "leak") != bool(self.cells):
            raise ValueError(f"{self.id}: a leak names the cells its secret is in; "
                             "a clean case names none (use features)")
        if self.truth == "clean" and self.expected.exit == 1:
            raise ValueError(f"{self.id}: a clean file's correct verdict is never 1")
        ids = [*self.cells, *self.features, *([self.known_gap.cell] if self.known_gap else [])]

        def known(cell_id: str) -> bool:
            if cell_id in CELLS:
                return True
            # A red-team case may name a storage place COVERAGE.md has no
            # row for yet, but only through the tracked allowlist.
            return (self.origin == "redteam" and bool(_NEW_CELL_RE.match(cell_id))
                    and cell_id in NEW_CELL_ALLOWLIST)

        unknown = [c for c in ids if not known(c)]
        if unknown:
            raise ValueError(f"{self.id}: unknown cell id(s) {unknown}")
        if self.writer not in WRITERS or self.origin not in ORIGINS:
            raise ValueError(f"{self.id}: unknown writer or origin")
        if not self.requires <= REQUIREMENTS:
            raise ValueError(f"{self.id}: unknown requirement(s) {set(self.requires) - REQUIREMENTS}")


REGISTRY: dict[str, Case] = {}


def case(
    id: str,
    *,
    truth: str,
    expected: Expect,
    story: str,
    cells: tuple[str, ...] | str = (),
    features: tuple[str, ...] | str = (),
    writer: str = "fitz",
    origin: str = "generated",
    known_gap: KnownGap | None = None,
    mistake: str = "",
    recovery: str = "",
    rules: tuple[dict[str, str], ...] = DEFAULT_RULES,
    requires: tuple[str, ...] = (),
    grid: str | None = None,
    params: tuple[tuple[str, str], ...] = (),
    privacy_allowlist: tuple[dict[str, str], ...] = (),
    perf: bool = False,
) -> Callable[[Callable[[Path], None]], Callable[[Path], None]]:
    """Register a builder as a case. The builder writes the PDF to the
    path it is given and nothing else."""
    def as_tuple(value: tuple[str, ...] | str) -> tuple[str, ...]:
        return (value,) if isinstance(value, str) else tuple(value)

    def register(build: Callable[[Path], None]) -> Callable[[Path], None]:
        if id in REGISTRY:
            raise ValueError(f"duplicate case id {id!r}")
        REGISTRY[id] = Case(
            id=id, truth=truth, cells=as_tuple(cells), expected=expected, story=story,
            build=build, features=as_tuple(features), writer=writer, origin=origin,
            known_gap=known_gap, mistake=mistake, recovery=recovery, rules=rules,
            requires=frozenset(requires), grid=grid, params=params,
            privacy_allowlist=privacy_allowlist, perf=perf,
        )
        return build
    return register
