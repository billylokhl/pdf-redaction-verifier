"""Shared fixtures for the verify.py regression suite.

Every PDF fixture is generated on the fly with PyMuPDF — no binary
fixtures are committed. Environment-dependent tests (Apple Vision OCR,
exiftool, qpdf) skip with a reason instead of failing when the tool is
unavailable, so the suite is runnable on any platform.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from redaction_verifier.views import _OCR_IMPORTS_OK  # noqa: E402

SSN = "123-45-6789"
# Placeholder values only — never a real person's data. 123-45-6789 is
# the canonical example SSN; the DOB is the Unix epoch.
DOB = "01/01/1970"

HAS_OCR = _OCR_IMPORTS_OK
HAS_EXIFTOOL = shutil.which("exiftool") is not None
HAS_QPDF = shutil.which("qpdf") is not None
# A clean document can only exit 0 when every layer can actually run.
FULL_ENV = HAS_OCR and HAS_EXIFTOOL and HAS_QPDF

# CI's full-environment job sets REQUIRE_FULL_ENV=1 so a broken Vision
# import or missing tool fails collection loudly instead of letting the
# environment-gated tests silently skip while the job stays green.
if os.environ.get("REQUIRE_FULL_ENV") == "1" and not FULL_ENV:
    _missing = [
        name for name, ok in [
            ("PyObjC Vision bridge", HAS_OCR),
            ("exiftool", HAS_EXIFTOOL),
            ("qpdf", HAS_QPDF),
        ] if not ok
    ]
    raise RuntimeError(
        f"REQUIRE_FULL_ENV=1 but the full environment is unavailable: "
        f"missing {', '.join(_missing)}"
    )

requires_full_env = pytest.mark.skipif(
    not FULL_ENV,
    reason="needs Apple Vision (macOS), exiftool, and qpdf for a certifiable scan",
)
requires_qpdf = pytest.mark.skipif(not HAS_QPDF, reason="needs qpdf")
requires_exiftool = pytest.mark.skipif(not HAS_EXIFTOOL, reason="needs exiftool")
requires_metadata_tools = pytest.mark.skipif(
    not (HAS_EXIFTOOL or HAS_QPDF), reason="needs exiftool or qpdf"
)


def run_verify(
    target: Path,
    secrets: Path,
    *extra: str,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "verify.py"),
         "--target", str(target), "--secrets", str(secrets), *extra],
        capture_output=True, text=True, env=env, timeout=300, cwd=cwd,
    )


@pytest.fixture(scope="session")
def secrets_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("secrets") / "secrets.json"
    path.write_text(json.dumps([
        {"name": "Target SSN", "value": SSN},
        {"name": "Target DOB", "value": DOB},
    ]))
    return path


@pytest.fixture(scope="session")
def clean_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pdfs") / "clean.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This document contains nothing sensitive.")
    page.insert_text((72, 100), "Reference number: 555-00-0000")
    doc.save(path)
    doc.close()
    return path


def _text_as_png(text: str) -> bytes:
    """Render text to PNG via a throwaway PDF page (no Pillow needed)."""
    tmp = fitz.open()
    page = tmp.new_page(width=500, height=100)
    page.insert_text((20, 60), text, fontsize=28)
    png = page.get_pixmap(dpi=150).tobytes("png")
    tmp.close()
    return png


@pytest.fixture(scope="session")
def leaky_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Adversarial fixture: attacks the Text, OCR, Metadata, and Objects layers.

    Page 1 draws the SSN digits in shuffled order into separate form
    boxes; page 2 carries the DOB only as image pixels; the SSN is also
    planted in the Info metadata.
    """
    path = tmp_path_factory.mktemp("pdfs") / "leaky.pdf"
    doc = fitz.open()

    page = doc.new_page()
    page.insert_text((72, 72), "Application Form - SSN boxes below")
    digits = SSN.replace("-", "")
    order = list(range(len(digits)))
    random.Random(42).shuffle(order)
    for idx in order:
        x, y = 72 + idx * 30, 140
        page.draw_rect(fitz.Rect(x - 4, y - 16, x + 16, y + 6), color=(0, 0, 0))
        page.insert_text((x, y), digits[idx], fontsize=14)

    page2 = doc.new_page()
    page2.insert_image(
        fitz.Rect(50, 100, 550, 200), stream=_text_as_png(f"Date of Birth: {DOB}")
    )

    doc.set_metadata({"subject": f"case file ref {SSN}", "title": "Benefits Application"})
    doc.save(path)
    doc.close()
    return path


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "grid: a member of a parameterised case-library grid (CI runs these on Linux)")
    config.addinivalue_line(
        "markers",
        "perf: a large performance file (eval/README.md); skipped unless RUN_PERF=1")
