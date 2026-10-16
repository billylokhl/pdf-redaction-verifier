"""views.text.visual_text_glyphs: extract_visual_text's readings with the
glyphs each line was read from (the --visible-map's interface to them).

The interface must read exactly what the scan reads: its text is checked
against extract_visual_text's, reading for reading, on every page of
every case in the case library, and on a few fabricated pages built
here (SSN 123-45-6789 only).
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from redaction_verifier.views import text as views_text
from redaction_verifier.views import extract_visual_text, visual_text_glyphs


def _same_readings(page: fitz.Page) -> None:
    real = extract_visual_text(page)
    tagged = visual_text_glyphs(page)
    assert tagged is not None
    assert len(tagged.readings) == len(real)
    assert [tagged.text(r) for r in range(len(real))] == real
    n = len(tagged.chars)
    assert len(tagged.boxes) == n and len(tagged.visible) == n
    # Every glyph is read at most once per reading.
    for lines in tagged.readings:
        flat = [g for line in lines for g in line]
        assert len(flat) == len(set(flat)) and all(0 <= g < n for g in flat)


def _library_cases() -> list[str]:
    from caselib import REGISTRY, load

    load()
    # Bomb cases are never opened in the test process (tests/test_bomb_cases.py).
    return sorted(case_id for case_id, case in REGISTRY.items() if case.bomb is None)


@pytest.mark.parametrize("case_id", _library_cases())
def test_the_interface_reads_what_the_scan_reads_on_the_case_library(case_id, tmp_path) -> None:
    """No page is skipped silently: where the scan's reading raises, the
    interface must raise the same way."""
    from caselib import REGISTRY
    from caselib.run import build

    pdf = build(REGISTRY[case_id], tmp_path / "case.pdf")
    with fitz.open(pdf) as doc:
        assert not doc.needs_pass, "a password-protected case: compare its pages some other way"
        assert doc.page_count, "a case with no pages compares nothing"
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            try:
                extract_visual_text(page)
            except Exception as exc:
                with pytest.raises(type(exc)):
                    visual_text_glyphs(page)
                continue
            _same_readings(page)


def _page_with(tmp_path: Path, rotate: int = 0) -> fitz.Page:
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((20, 40), "SSN 123-45-6789")
    page.insert_text((20, 80), "123  45   6789")
    page.insert_text((150, 150), "rotated 123-45-6789", rotate=90)
    page.insert_text((250, 200), "off the edge 123-45-6789")    # runs past x=300
    page.insert_text((20, 400), "below 123-45-6789")              # wholly off-page
    page.set_cropbox(fitz.Rect(0, 0, 300, 300))
    page.set_rotation(rotate)
    path = tmp_path / f"p{rotate}.pdf"
    doc.save(path)
    return fitz.open(path)[0]


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_the_interface_names_each_glyph_and_its_visibility(tmp_path, rotate) -> None:
    page = _page_with(tmp_path, rotate)
    _same_readings(page)
    tagged = visual_text_glyphs(page)
    assert tagged is not None
    # Readings 0 and 3 hold only visible and only off-page glyphs.
    assert all(tagged.visible[g] for line in tagged.readings[0] for g in line)
    assert not any(tagged.visible[g] for line in tagged.readings[3] for g in line)
    # Reading 4 carries the line that runs past the edge on across it.
    across = [line for line in tagged.readings[4]
              if "123-45-6789" in tagged.line_text(line) and "edge" in tagged.line_text(line)]
    assert across and {tagged.visible[g] for g in across[0]} == {True, False}


def _raw_boxes(page: fitz.Page) -> list[tuple[float, float, float, float]]:
    raw = page.get_text("rawdict", clip=fitz.INFINITE_RECT(),
                        flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_MEDIABOX_CLIP)
    return [tuple(ch["bbox"]) for b in raw["blocks"] for ln in b.get("lines", [])
            for sp in ln["spans"] for ch in sp["chars"] if ch["c"] and not ch["c"].isspace()]


@pytest.mark.parametrize("user_unit", [1, 2])
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_each_glyph_keeps_its_own_box(tmp_path, rotate, user_unit) -> None:
    """boxes[g] is glyph g's own rawdict box, and visible[g] says whether
    its centre lies in the visible area (unrotated, at the origin, of
    page.rect's derotated size)."""
    page = _page_with(tmp_path, rotate)
    if user_unit != 1:
        doc = page.parent
        doc.xref_set_key(page.xref, "UserUnit", str(user_unit))
        path = tmp_path / f"uu{rotate}.pdf"
        doc.save(path)
        page = fitz.open(path)[0]
    tagged = visual_text_glyphs(page)
    assert tagged is not None
    assert list(tagged.boxes) == _raw_boxes(page)
    area = page.rect * page.derotation_matrix
    for (x0, y0, x1, y1), shown in zip(tagged.boxes, tagged.visible):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        assert shown == (0 <= cx <= area.width and 0 <= cy <= area.height)
    assert True in tagged.visible and False in tagged.visible
    # Every glyph's box is its own: no two glyphs of a line share one.
    (line,) = [ln for ln in tagged.readings[0] if tagged.line_text(ln) == "SSN123-45-6789"]
    assert len({tagged.boxes[g] for g in line}) == len(line)


def test_a_character_that_is_not_a_tag_raises() -> None:
    with pytest.raises(ValueError):
        views_text._glyph_indices(chr(views_text.TAG_BASE) + "-")


def test_an_empty_page_has_no_readings() -> None:
    doc = fitz.open()
    page = doc.new_page()
    assert extract_visual_text(page) == []
    tagged = visual_text_glyphs(page)
    assert tagged is not None and tagged.readings == () and tagged.chars == ()


def test_too_many_glyphs_to_tag_is_none(monkeypatch) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "123-45-6789")
    monkeypatch.setattr(views_text, "MAX_TAGGED_GLYPHS", 3)
    assert visual_text_glyphs(page) is None
    # The scan's own reading is untouched by the limit.
    assert extract_visual_text(page)[0] == "123-45-6789"
