"""Images painted over (K12, K38; the legacy hotfix of 2026-10-15,
redaction_verifier/views/covered.py and verify.scan_covered_images).
Every value is fabricated.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pymupdf as fitz
import pytest

import verify
from redaction_verifier.model import ScanReport
from redaction_verifier.views.covered import (COVERED_FRACTION, covered_images,
                                              images_only_page)

SSN = "666-51-4073"
IMAGE = fitz.Rect(72, 200, 372, 260)


def _png(text: str) -> bytes:
    tmp = fitz.open()
    tmp.new_page(width=300, height=60).insert_text((10, 40), text, fontsize=18)
    data = tmp[0].get_pixmap(dpi=144).tobytes("png")
    tmp.close()
    return data


def _page(*boxes: tuple[fitz.Rect, float], before: bool = False) -> fitz.Document:
    """One page with one image at IMAGE and *boxes* (rect, fill opacity)
    drawn after it, or before it with *before*."""
    doc = fitz.open()
    page = doc.new_page()

    def draw() -> None:
        for rect, opacity in boxes:
            page.draw_rect(rect, color=None, fill=(0, 0, 0), fill_opacity=opacity)
    if before:
        draw()
    page.insert_image(IMAGE, stream=_png("Quarterly report"))
    if not before:
        draw()
    return doc


@pytest.mark.parametrize("boxes, before, fraction", [
    ((), False, 0.0),
    (((IMAGE, 1.0),), False, 1.0),
    (((fitz.Rect(72, 200, 222, 260), 1.0),), False, 0.5),
    (((fitz.Rect(327, 200, 372, 260), 1.0),), False, 0.15),
    (((IMAGE, 1.0),), True, 0.0),                 # the box is behind the image
    (((IMAGE, 0.5),), False, 0.0),                # see-through
    (((IMAGE, 0.0),), False, 0.0),                # invisible
    (((fitz.Rect(400, 200, 500, 260), 1.0),), False, 0.0),   # beside it
    # Two boxes: the larger single one counts, not their sum.
    (((fitz.Rect(72, 200, 162, 260), 1.0), (fitz.Rect(162, 200, 252, 260), 1.0)), False, 0.3),
])
def test_the_fraction_one_opaque_fill_covers(boxes, before, fraction) -> None:
    doc = _page(*boxes, before=before)
    [image] = covered_images(doc[0])
    assert image.fraction == pytest.approx(fraction, abs=0.01)


def _raw_page(content: bytes, rotation: int = 0) -> fitz.Document:
    """One page drawing the image as /Img with *content*, in PDF space."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 10, 10), stream=_png("Quarterly report"))
    name = page.get_images(full=True)[0][7]
    doc.update_stream(page.get_contents()[0], content.replace(b"/Img", b"/" + name.encode()))
    page.set_rotation(rotation)
    return doc


DRAW = b"q 300 0 0 60 72 582 cm /Img Do Q "
BOX = b"0 0 0 rg 72 582 300 60 re f "


@pytest.mark.parametrize("content, fraction", [
    # Degenerate paths the log keeps and get_drawings skips (review B1).
    (b"0 0 0 rg 10 10 m f 10 10 m f 10 10 m f " + DRAW + BOX, 1.0),
    # A box over the whole visible window of a clipped image.
    (b"q 72 582 60 60 re W n " + DRAW + b"Q 0 0 0 rg 72 582 60 60 re f ", 1.0),
    # A box whose path also reaches far off the page (review B3).
    (DRAW + b"0 0 0 rg 72 582 300 60 re 2000000000 0 m 2000000001 1 l f ", 1.0),
    # A wide stroke is not a cover (a documented limit).
    (DRAW + b"0 0 0 RG 60 w 72 612 m 372 612 l S ", 0.0),
])
def test_the_fraction_in_the_draw_log(content, fraction) -> None:
    doc = _raw_page(content)
    [image] = covered_images(doc[0])
    assert image.fraction == pytest.approx(fraction, abs=0.01)


def test_a_see_through_box_after_degenerate_paths_stays_see_through() -> None:
    doc = _raw_page(b"0 0 0 rg 10 10 m f 10 10 m f " + DRAW)
    page = doc[0]
    page.draw_rect(fitz.Rect(72, 200, 372, 260), color=None, fill=(0, 0, 0), fill_opacity=0.5)
    [image] = covered_images(page)
    assert image.fraction == 0.0


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_rotated_page_is_measured_and_rebuilt_unrotated(rotation) -> None:
    """Review B2: the log reports in the unrotated page's space."""
    doc = _raw_page(b"q 300 0 0 60 72 50 cm /Img Do Q 0 0 0 rg 72 50 300 60 re f ", rotation)
    [image] = covered_images(doc[0])
    assert image.fraction == pytest.approx(1.0)
    rebuilt = images_only_page(doc[0])
    try:
        assert len(rebuilt[0].get_image_info()) == 1
        assert rebuilt[0].rotation == rotation and rebuilt[0].rect == doc[0].rect
    finally:
        rebuilt.close()


def test_the_threshold_is_the_owners_quarter() -> None:
    assert COVERED_FRACTION == 0.25


def test_the_rebuilt_page_holds_the_images_without_the_box() -> None:
    doc = _page((IMAGE, 1.0))
    rebuilt = images_only_page(doc[0])
    try:
        assert rebuilt[0].rect == doc[0].rect
        assert not rebuilt[0].get_drawings()
        [info] = rebuilt[0].get_image_info()
        assert fitz.Rect(info["bbox"]) == IMAGE
    finally:
        rebuilt.close()


# ── verify: flagged, and fail closed ──────────────────────────────────

def _main(monkeypatch, tmp_path: Path, doc: fitz.Document, *extra: str) -> tuple[int, dict]:
    """verify.main, OCR off, on *doc* with a value rule the file does not
    hold: the exit code and the --json report."""
    monkeypatch.setattr(verify, "_OCR_IMPORTS_OK", False)
    pdf = tmp_path / "t.pdf"
    doc.save(pdf)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"name": "Target", "value": SSN}]))
    out = tmp_path / "t.json"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = verify.main(["--target", str(pdf), "--secrets", str(rules), "--json", str(out),
                            *extra])
    return code, json.loads(out.read_text())


def _codes(report: dict) -> set[str]:
    return {w["code"] for w in report["warnings"]}


def test_a_covered_image_is_flagged_without_ocr(monkeypatch, tmp_path) -> None:
    code, report = _main(monkeypatch, tmp_path, _page((IMAGE, 1.0)))
    assert code == 2
    [warning] = [w for w in report["warnings"] if w["code"] == "IMAGE_COVERED"]
    assert warning["page"] == 1 and warning["storage"] == "live"
    assert SSN not in json.dumps(report)


def test_an_image_under_the_threshold_is_not(monkeypatch, tmp_path) -> None:
    code, report = _main(monkeypatch, tmp_path,
                         _page((fitz.Rect(327, 200, 372, 260), 1.0)))
    assert "IMAGE_COVERED" not in _codes(report)
    assert code == 2 and _codes(report) == {"OCR_UNAVAILABLE"}


def test_a_page_the_check_cannot_read_fails_closed(monkeypatch, tmp_path) -> None:
    def boom(page):
        raise RuntimeError("MuPDF could not interpret the page")
    monkeypatch.setattr(verify, "covered_images", boom)
    code, report = _main(monkeypatch, tmp_path, _page())
    assert code == 2
    assert any(w["code"] == "PAGE_FAILED" and w["page"] == 1 for w in report["warnings"])


def test_a_crash_of_the_whole_check_fails_closed(monkeypatch, tmp_path) -> None:
    def boom(doc, report):
        raise MemoryError("out of memory")
    monkeypatch.setattr(verify, "scan_covered_images", boom)
    code, report = _main(monkeypatch, tmp_path, _page())
    assert code == 2 and "LAYER_CRASHED" in _codes(report)


def test_a_rebuild_that_fails_is_a_failed_page(monkeypatch) -> None:
    """With OCR: a covered page that cannot be rebuilt is PAGE_FAILED (and
    stays IMAGE_COVERED), never a pass."""
    def boom(page):
        raise RuntimeError("cannot decode the image")
    monkeypatch.setattr(verify, "images_only_page", boom)
    doc = _page((IMAGE, 1.0))
    report = ScanReport()
    covered = verify.scan_covered_images(doc, report)
    assert covered == {0}
    matcher = verify.SecretMatcher([verify.Secret("Target", SSN)])
    verify.scan_page_layer(doc, matcher, report, layer="OCR", extractor=verify.images_only_ocr,
                           note="images only")
    codes = {w.code for w in report.warnings}
    assert codes == {"IMAGE_COVERED", "PAGE_FAILED"} and not report.leaked
