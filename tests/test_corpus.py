"""Real vendor PDFs, when present: drop `foo.pdf` plus a
`foo.pdf.secrets.json` sidecar (a list of [name, value] pairs) into
tests/corpus_pdfs/ and each expected secret must be caught.

Generated producer layouts and carrier surfaces live in the case library
(eval/caselib/families/carriers.py); this hook exercises real Acrobat,
Word or scanner output without committing it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import run_verify

REAL_CORPUS_DIR = Path(__file__).parent / "corpus_pdfs"


def _real_corpus():
    if not REAL_CORPUS_DIR.is_dir():
        return
    for pdf in sorted(REAL_CORPUS_DIR.glob("*.pdf")):
        sidecar = pdf.with_suffix(".pdf.secrets.json")
        yield pdf, json.loads(sidecar.read_text()) if sidecar.exists() else []


_REAL_CORPUS = list(_real_corpus()) or [
    pytest.param(None, None,
                 marks=pytest.mark.skip(reason="no real vendor PDFs in tests/corpus_pdfs/")),
]


@pytest.mark.parametrize("pdf,expected", _REAL_CORPUS,
                         ids=lambda v: Path(v).name if isinstance(v, Path) else "")
def test_real_vendor_corpus(pdf, expected, tmp_path) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"name": name, "value": val} for name, val in expected]))
    result = run_verify(pdf, rules)
    assert result.returncode == 1, f"{pdf.name}: expected secrets not detected"
