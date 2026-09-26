"""Real redaction-tool output, committed as files (the only binaries in
the library): generated documents can only show failures someone thought
to build. All data in these files is fabricated.

Provenance: redactor 0.1.0 at commit  (2026-09-13), run with
LLM detection disabled and these exact values:
"Jordan Q. Testperson", "123-45-6789", "01/01/1970", "1600 Fictional Ave",
"Springfield", "000123456789".
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..model import SSN, case, expect

REAL = Path(__file__).resolve().parent.parent / "real"
RULES = (
    {"name": "Name", "value": "Jordan Q. Testperson"},
    {"name": "SSN", "value": SSN},
    {"name": "DOB", "value": "01/01/1970"},
    {"name": "Address", "value": "1600 Fictional Ave"},
    {"name": "Account", "value": "000123456789"},
)
NO_GC = ("redactor applies the redactions correctly, then saves with PyMuPDF's "
         "default garbage=0, so the page's original content streams stay in the file.")


def _copy(name: str):
    def build(path: Path) -> None:
        shutil.copyfile(REAL / name, path)
    return build


case("redactor.redactor.literal", cells="orphaned.plain", writer="redactor", rules=RULES,
     expected=expect(1, findings=(("SSN", "orphaned"), ("Name", "orphaned"))),
     story="A benefits application redacted by redactor: the page shows black boxes, "
           "but every original line is still in the file as literal strings.",
     mistake=NO_GC,
     recovery="mutool clean -d file.pdf out.pdf && grep -a 123-45-6789 out.pdf",
     )(_copy("redactor-literal.pdf"))

case("redactor.redactor.hex", cells="orphaned.plain", writer="redactor", rules=RULES,
     expected=expect(1, findings=(("SSN", "orphaned"), ("Name", "orphaned"))),
     story="The same failure with the original text stored as hex strings: still plain "
           "character codes, so every value is recovered from the leftover streams.",
     mistake=NO_GC,
     recovery="mutool clean -d, then decode the hex strings (each pair of digits is a character).",
     )(_copy("redactor-hex.pdf"))

case("redactor.redactor.hex-compacted", cells="orphaned.plain", writer="redactor",
     rules=RULES, expected=expect(0),
     story="The hex-coded output re-saved with garbage collection: the leftovers are gone.",
     mistake="None — the fix for the failure above.",
     )(_copy("redactor-hex-compacted.pdf"))
