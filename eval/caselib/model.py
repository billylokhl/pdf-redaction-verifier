"""The case model: one generated PDF, what the tool should say about it,
and the story of how the redaction failed.

A case is the unit of the evaluation harness (docs/REDESIGN.md §5). The
same catalogue drives the test suite, the scorecard's corpus, the
evidence behind COVERAGE.md, and the gallery of how redaction fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .cells import CELLS

# The values every case plants unless it brings its own rules. Fabricated:
# the SSN is the canonical example number, never a real person's.
SSN = "123-45-6789"
CODE = "BLUEHERON"
DEFAULT_RULES: tuple[dict[str, str], ...] = (
    {"name": "SSN", "value": SSN},
    {"name": "Code", "value": CODE},
)

WRITERS = frozenset({
    "fitz",        # PyMuPDF — also the library the tool reads with
    "raw",         # hand-assembled bytes (eval/caselib/rawpdf.py)
    "qpdf",        # fitz output rewritten by qpdf
    "redactor",    # output of a real redaction tool, committed as a file
})
REQUIREMENTS = frozenset({"ocr", "qpdf", "exiftool"})

_ID_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")


@dataclass(frozen=True)
class Expect:
    """What a correct verifier reports.

    *exit* is the correct verdict. *findings* are (rule, storage) pairs
    that must appear among the hard findings; *warnings* are warning
    codes that must appear. Both are lower bounds: other findings and
    warnings may appear too, but they cannot change *exit*.
    """

    exit: int
    findings: frozenset[tuple[str, str]] = frozenset()
    warnings: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.exit not in (0, 1, 2):
            raise ValueError(f"exit must be 0, 1 or 2, not {self.exit}")
        if self.exit != 1 and self.findings:
            raise ValueError("findings imply exit 1")


def expect(exit: int, findings: tuple[tuple[str, str], ...] = (),
           warnings: tuple[str, ...] = ()) -> Expect:
    return Expect(exit, frozenset(findings), frozenset(warnings))


@dataclass(frozen=True)
class Case:
    id: str
    cells: tuple[str, ...]
    writer: str
    expected: Expect
    story: str                        # what the document is and what went wrong
    build: Callable[[Path], None] = field(repr=False, compare=False)
    # Cell id when today's tool is known to get this case wrong: the test
    # is a strict xfail, so closing the gap fails it until the label moves.
    known_gap: str | None = None
    mistake: str = ""                 # which tool or habit causes it
    recovery: str = ""                # how the secret is recovered by hand
    rules: tuple[dict[str, str], ...] = DEFAULT_RULES
    requires: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"case id {self.id!r} must be lowercase kebab/dot form")
        unknown = [c for c in (*self.cells, *filter(None, [self.known_gap]))
                   if c not in CELLS]
        if unknown:
            raise ValueError(f"{self.id}: unknown cell id(s) {unknown}")
        if not self.cells:
            raise ValueError(f"{self.id}: a case must exercise at least one cell")
        if self.writer not in WRITERS:
            raise ValueError(f"{self.id}: unknown writer {self.writer!r}")
        if not self.requires <= REQUIREMENTS:
            raise ValueError(f"{self.id}: unknown requirement(s) {set(self.requires) - REQUIREMENTS}")


REGISTRY: dict[str, Case] = {}


def case(
    id: str,
    *,
    cells: tuple[str, ...] | str,
    expected: Expect,
    story: str,
    writer: str = "fitz",
    known_gap: str | None = None,
    mistake: str = "",
    recovery: str = "",
    rules: tuple[dict[str, str], ...] = DEFAULT_RULES,
    requires: tuple[str, ...] = (),
) -> Callable[[Callable[[Path], None]], Callable[[Path], None]]:
    """Register a builder as a case. The builder writes the PDF to the
    path it is given and nothing else."""
    def register(build: Callable[[Path], None]) -> Callable[[Path], None]:
        if id in REGISTRY:
            raise ValueError(f"duplicate case id {id!r}")
        REGISTRY[id] = Case(
            id=id, cells=(cells,) if isinstance(cells, str) else tuple(cells),
            writer=writer, expected=expected, story=story, build=build,
            known_gap=known_gap, mistake=mistake, recovery=recovery,
            rules=rules, requires=frozenset(requires),
        )
        return build
    return register
