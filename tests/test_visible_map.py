"""The --visible-map's own logic: the sink, the headline table, positions,
classes, tiers and the self-check (docs/DESIGN.md "The visible map").

The CLI-level guarantees — the flag changes no verdict, faults in the map
cost only the map, the output file's safety rules — are in
test_visible_map_cli.py.
"""

from __future__ import annotations

import itertools
import os
import re
from pathlib import Path

import pymupdf as fitz
import pytest

import verify
from redaction_verifier.model import WARNING_CODES
from redaction_verifier.visible_map import headline, locate, plan
from redaction_verifier.visible_map.sink import MapSink, offer

from .conftest import HAS_OCR, requires_full_env
from .visible_map_support import (
    NAME,
    SSN,
    SSN_RULES,
    boxes,
    mapped,
    plan_of,
    run_main,
    sections,
    without_ocr_warning,
    write_rules,
)

EMPTY_AP = b""


def _save(doc: fitz.Document, path: Path) -> Path:
    doc.save(path)
    doc.close()
    return path


def _blank_appearance(path: Path, *xrefs: int) -> None:
    """Give the annotations *xrefs* an appearance that draws nothing, so
    the page does not show their value."""
    doc = fitz.open(path)
    for xref in xrefs:
        ap = doc.get_new_xref()
        doc.update_object(ap, "<</Type/XObject/Subtype/Form/BBox[0 0 1 1]>>")
        doc.update_stream(ap, EMPTY_AP)
        doc.xref_set_key(xref, "AP", f"<</N {ap} 0 R>>")
    doc.saveIncr()
    doc.close()


# ── The sink ──────────────────────────────────────────────────────────
class TestSink:
    def test_offer_copies_names_only(self) -> None:
        sink = MapSink()
        hard, pattern_hard = {"ssn"}, {"any ssn": "123-45-6789"}
        offer(sink, "page", "Text", 1, hard, set(), pattern_hard, {}, set(), set())
        hard.add("later")
        entry = sink.truth["Text"][1]
        assert entry["hard"] == ["ssn"]
        # A pattern hit's sample never reaches the map: only its rule name.
        assert entry["pattern_hard"] == ["any ssn"]
        assert "123-45-6789" not in repr(sink.to_header())

    def test_a_page_with_no_hit_costs_one_number(self) -> None:
        sink = MapSink()
        for n in range(1, 1001):
            offer(sink, "page", "Text", n, set(), set(), {}, {}, set(), set())
        assert sink.scanned["Text"] == list(range(1, 1001)) and sink.truth == {}

    def test_a_raising_sink_is_marked_failed_never_raises(self) -> None:
        class Broken(MapSink):
            def take(self, kind, refs):
                raise RuntimeError("boom")

        sink = Broken()
        offer(sink, "page", "Text", 1, {"ssn"}, set(), {}, {}, set(), set())
        assert sink.failed

    def test_a_mutating_sink_gets_copies(self) -> None:
        class Mutating(MapSink):
            def take(self, kind, refs):
                for ref in refs:
                    if isinstance(ref, list):
                        ref.clear()

        hard = {"ssn"}
        offer(Mutating(), "page", "Text", 1, hard, set(), {}, {}, set(), set())
        assert hard == {"ssn"}

    def test_a_non_name_is_refused(self) -> None:
        sink = MapSink()
        offer(sink, "page", "Text", 1, {object()}, set(), {}, {}, set(), set())
        assert sink.failed


# ── The hooks change no verdict (design §9 test 1) ───────────────────
RUN_ALL = os.environ.get("RUN_MAP_IDENTITY") == "1"


def _identity_cases() -> list[str]:
    from caselib import REGISTRY, load
    from caselib.cells import parts
    from caselib.run import available

    load()
    have = available()
    picked: dict[tuple[str, str, str], str] = {}
    for case_id, case in sorted(REGISTRY.items()):
        if case.perf or case.requires - have or (case.grid and not RUN_ALL):
            continue
        if RUN_ALL:
            picked[(case_id, "", "")] = case_id
            continue
        row = parts(case.cells[0])[0] if case.cells else "-"
        picked.setdefault((case_id.split(".")[0], row, case.truth), case_id)
    return sorted(picked.values())


@pytest.mark.parametrize("case_id", _identity_cases())
def test_a_sink_changes_no_verdict_on_the_case_library(case_id, tmp_path, monkeypatch) -> None:
    """A representative of every (family, row, truth) cell of the case
    library (RUN_MAP_IDENTITY=1: every buildable case), scanned with and
    without a sink at the hooks: the same exit code, stdout and --json
    report, and a plan that builds."""
    from caselib import REGISTRY
    from caselib.run import build

    case = REGISTRY[case_id]
    pdf = build(case, tmp_path / f"{case_id}.pdf")
    rules = write_rules(tmp_path, list(case.rules))
    off = run_main(monkeypatch, pdf, rules, ocr=HAS_OCR, sink=False)
    on = run_main(monkeypatch, pdf, rules, ocr=HAS_OCR)
    assert (off.code, off.stdout, off.stderr, off.report) == (
        on.code, on.stdout, on.stderr, on.report)
    if on.header is not None and not on.header["sink"]["failed"]:
        with fitz.open(pdf) as doc:
            locked, pages = doc.needs_pass, doc.page_count
        if not locked:
            # The sink was really handed the scan's truth.
            assert bool(on.header["sink"]["scanned"].get("Text")) == bool(pages)
            assert plan_of(pdf, on.header).row in headline.ROWS


class _RaisingSink(MapSink):
    def take(self, kind: str, refs: list) -> None:
        refs.clear()                    # tries to change what it was handed
        raise RuntimeError("a broken sink")


def test_a_raising_sink_changes_no_verdict(tmp_path, monkeypatch) -> None:
    """A sink that raises (and mutates what it was handed) through the
    real scan: the same exit code, stdout and --json report as no sink,
    with and without --fail-fast; the sink is only marked failed."""
    from . import visible_map_support as support

    monkeypatch.setattr(support, "MapSink", _RaisingSink)
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((100, 100), f"SSN {SSN}")
        page.add_text_annot((100, 300), f"note {SSN}")
    pdf = _save(doc, tmp_path / "raising.pdf")
    rules = write_rules(tmp_path, SSN_RULES)
    for extra in ((), ("--fail-fast",)):
        off = run_main(monkeypatch, pdf, rules, *extra, sink=False)
        on = run_main(monkeypatch, pdf, rules, *extra)
        assert (off.code, off.stdout, off.report) == (on.code, on.stdout, on.report)
        assert on.header is not None and on.header["sink"]["failed"]


def test_without_a_sink_nothing_is_offered(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(verify, "_offer", lambda *a: calls.append(a))
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), f"SSN {SSN}")
    page.add_text_annot((100, 300), f"note {SSN}")
    pdf = _save(doc, tmp_path / "leaky.pdf")
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), sink=False)
    assert run.code == 1 and calls == []


# ── The headline (design §7) ──────────────────────────────────────────
CODE_CLASSES = ("page", "document", "scope", "review", "live", "outside")
# What each code class sets (the planner's mapping).
CLASS_FLAGS = {"page": "I", "document": "I", "scope": "I", "review": "P", "live": "L",
               "outside": ""}


def _feasible(exit_code: int, j: bool, flags: frozenset[str], classes: frozenset[str]) -> bool:
    """main()'s own invariants: J only turns a 0 into a 2; a 2 needs a
    warning or J; a 0 has no warning and nothing found."""
    if j and exit_code != 2:
        return False
    if exit_code == 2 and not (classes or j):
        return False
    if exit_code == 0 and (classes or flags & {"V", "P", "L", "X"}):
        return False
    return True


class TestHeadline:
    def test_every_combination_has_exactly_one_row(self) -> None:
        reached_8 = []
        for exit_code, j in itertools.product((0, 1, 2), (False, True)):
            for n in range(6):
                for flag_set in itertools.combinations("VIPLX", n):
                    for m in range(len(CODE_CLASSES) + 1):
                        for classes in itertools.combinations(CODE_CLASSES, m):
                            flags = set(flag_set) | {CLASS_FLAGS[c] for c in classes} - {""}
                            given = headline.Flags(**{k: k in flags for k in "VIPLX"})
                            codes = [f"CODE_{c.upper()}" for c in classes]
                            outside_only = bool(classes) and set(classes) == {"outside"}
                            rows = [r for r in headline.ROWS
                                    if r == headline.row(given, exit_code=exit_code,
                                                         json_unwritten=j, codes=codes,
                                                         outside_only=outside_only)]
                            assert len(rows) == 1
                            text = headline.text(given, exit_code=exit_code, json_unwritten=j,
                                                 codes=codes, outside_only=outside_only)
                            if j:
                                assert headline.J_REASON in text
                            if rows[0] == 8 and _feasible(exit_code, j, frozenset(flags),
                                                          frozenset(classes)):
                                reached_8.append((exit_code, j, frozenset(flags)))
        assert set(reached_8) <= headline.ROW_8_ALLOWED

    def test_row_8_is_the_fail_closed_answer_to_an_impossible_run(self) -> None:
        # exit 2 with no warning and the JSON report written cannot come
        # out of main(); were it ever to, the map still says NOT CERTIFIED.
        flags = headline.Flags()
        assert headline.row(flags, exit_code=2, json_unwritten=False, codes=[],
                            outside_only=False) == 8

    @pytest.mark.parametrize("flags, exit_code, j, codes, outside, row", [
        (headline.Flags(V=True), 1, False, [], False, 1),
        (headline.Flags(V=True, I=True), 2, False, ["OCR_UNAVAILABLE"], False, 1),
        (headline.Flags(I=True), 0, False, [], False, 2),
        (headline.Flags(L=True, X=True), 1, False, [], False, 2),
        (headline.Flags(X=True), 1, False, [], False, 3),
        (headline.Flags(), 1, False, [], False, 4),
        (headline.Flags(), 2, False, ["LEFTOVER_IMAGE"], True, 5),
        (headline.Flags(), 2, True, [], False, 6),
        (headline.Flags(), 0, False, [], False, 7),
    ])
    def test_rows(self, flags, exit_code, j, codes, outside, row) -> None:
        assert headline.row(flags, exit_code=exit_code, json_unwritten=j, codes=codes,
                            outside_only=outside) == row

    def test_every_warning_code_is_classified_once(self) -> None:
        tables = (headline.PAGE_CODES, headline.DOCUMENT_CODES, headline.OBJECT_COVERAGE_CODES,
                  headline.REVIEW_CODES, headline.STORED_REVIEW_CODES, headline.SCOPE_CODES)
        for code in WARNING_CODES:
            assert sum(code in t for t in tables) == 1, code
        assert set().union(*tables) == set(WARNING_CODES)

    def test_an_unclassified_code_counts_as_document_level(self) -> None:
        assert headline.code_level("SOME_FUTURE_CODE", None) == "document"

    def test_object_codes_split_by_storage(self) -> None:
        assert headline.code_level("LEFTOVER_IMAGE", "orphaned") == "outside"
        assert headline.code_level("LEFTOVER_IMAGE", "live") == "document"
        assert headline.code_level("REVIEW_BINARY", "live") == "live"
        assert headline.code_level("REVIEW_OBJECT_TEXT", "superseded") == "outside"


# ── Positions (design §9 test 4) ──────────────────────────────────────
# Page boxes for the geometry tests: a MediaBox and CropBox both shifted
# off the origin, and a CropBox reaching past the MediaBox -- where
# PyMuPDF's rotation_matrix is offset from the frame its glyphs use.
BOXES = {    # MediaBox, CropBox, a user-space y below the CropBox
    "shifted": ("[50 60 662 852]", "[100 120 500 700]", 90),
    "overhang": ("[0 0 612 792]", "[0 200 612 992]", 150),
}


def _geometry_pdf(path: Path, rotate: int, crop: str, *, content: str = "text",
                  outside: bool = False) -> Path:
    """One thing on an otherwise blank page, at user-space (200, 400):
    the value as raw text, a FreeText annotation or a text field --
    plus, with *outside*, the value again below the CropBox."""
    media, cropbox, below = BOXES[crop]
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    if content == "widget":
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "f1"
        w.rect = fitz.Rect(200, 380, 360, 400)
        w.field_value = SSN
        page.add_widget(w)
    elif content == "freetext":
        page.add_freetext_annot(fitz.Rect(200, 370, 400, 400), f"FT {SSN}")
    text = b"BT /F1 14 Tf 200 400 Td (" + SSN.encode() + b") Tj ET" if content == "text" else b""
    if outside:
        text += b" BT /F1 14 Tf 200 %d Td (" % below + SSN.encode() + b") Tj ET"
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, text)
    doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
    doc.xref_set_key(page.xref, "Resources",
                     "<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>")
    doc.xref_set_key(page.xref, "MediaBox", media)
    doc.xref_set_key(page.xref, "CropBox", cropbox)
    doc.xref_set_key(page.xref, "Rotate", str(rotate))
    return _save(doc, path)


def _ink(page: fitz.Page, zoom: float = 4.0, annots: bool = False) -> fitz.Rect | None:
    """The bounding box of dark pixels, in displayed coordinates: what a
    viewer draws, independent of every coordinate frame the map uses."""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY,
                          annots=annots)
    xs, ys = [], []
    for y in range(pix.height):
        row = pix.samples[y * pix.width:(y + 1) * pix.width]
        for x, v in enumerate(row):
            if v < 128:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return fitz.Rect(min(xs) / zoom, min(ys) / zoom, (max(xs) + 1) / zoom, (max(ys) + 1) / zoom)


def _around(rect: fitz.Rect, by: float) -> fitz.Rect:
    return fitz.Rect(rect.x0 - by, rect.y0 - by, rect.x1 + by, rect.y1 + by)


@pytest.mark.parametrize("crop", sorted(BOXES))
@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
class TestGeometry:
    def test_a_text_box_sits_on_the_ink(self, tmp_path, monkeypatch, rotate, crop) -> None:
        """The box holds every dark pixel the value draws, and is no more
        than the glyphs' own height and the padding larger (7 pt): checked
        against the rendered page, not against any matrix the map uses."""
        pdf = _geometry_pdf(tmp_path / f"geom{rotate}.pdf", rotate, crop)
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        (box,) = boxes(the_plan, plan.SOLID)
        page = fitz.open(pdf)[0]
        ink = _ink(page)
        assert ink is not None
        assert _around(box.rect, 0.5).contains(ink), (box.rect, ink)
        assert _around(ink, 7.0).contains(box.rect), (box.rect, ink)
        pt = locate.page_text(page)
        assert pt is not None
        assert locate.verify_text_box(page, pt, box.rect, "123456789")
        shifted = fitz.Rect(box.rect) + (box.rect.width, 0, box.rect.width, 0)
        if rotate in (90, 270):
            shifted = fitz.Rect(box.rect) + (0, box.rect.height, 0, box.rect.height)
        assert not locate.verify_text_box(page, pt, shifted, "123456789")

    @pytest.mark.parametrize("content", ["freetext", "widget"])
    def test_an_annotation_box_sits_on_its_ink(self, tmp_path, monkeypatch, rotate, crop,
                                               content) -> None:
        """A FreeText or a text field whose appearance shows the value is
        a solid "on page" box over that appearance's ink (never dashed or
        hatched), and the box verifies against its raw /Rect."""
        pdf = _geometry_pdf(tmp_path / f"{content}{rotate}.pdf", rotate, crop, content=content)
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        page = fitz.open(pdf)[0]
        ink = _ink(page, annots=True)
        assert ink is not None
        drawn = [b for b in the_plan.boxes if b.how == "on page"]
        assert len(drawn) == 1 and drawn[0].style == plan.SOLID, the_plan.boxes
        assert _around(drawn[0].rect, 1.0).contains(ink), (drawn[0].rect, ink)
        assert not boxes(the_plan, plan.HATCHED) and not boxes(the_plan, plan.DASHED)
        (annot,) = [*page.annots(), *page.widgets()]
        assert locate.verify_annot_box(page, page.parent, annot.xref, drawn[0].rect)
        shifted = fitz.Rect(drawn[0].rect) + (drawn[0].rect.width, 0, drawn[0].rect.width, 0)
        assert not locate.verify_annot_box(page, page.parent, annot.xref, shifted)

    def test_text_outside_the_crop_is_never_boxed(self, tmp_path, monkeypatch, rotate,
                                                  crop) -> None:
        """The value once inside the crop and once below it. The inside
        copy is boxed on its ink and never listed "outside"; the outside
        copy is listed "outside the crop". The map reads the page through
        the scan's own readings, so a rotated, overhanging crop no longer
        leaves it unpositioned."""
        pdf = _geometry_pdf(tmp_path / f"out{rotate}.pdf", rotate, crop, outside=True)
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        page = fitz.open(pdf)[0]
        ink = _ink(page)            # the inside copy only: the other is cropped away
        assert ink is not None
        (box,) = boxes(the_plan, plan.SOLID)
        assert _around(box.rect, 0.5).contains(ink), (box.rect, ink)
        assert sections(the_plan, "outside") == [(1, "value rule #1", "")]
        assert not sections(the_plan, "unpositioned")


# ── Across the visible edge (readings 4-6) ────────────────────────────
EDGE = 300.0        # the CropBox's right edge, and (rotated text) its top


def _edge_pdf(path: Path, rotate: int, kind: str) -> Path:
    """A value running past the CropBox [0 0 300 792]: *kind* "flat" puts
    it on one baseline with the edge just after "123-";
    "vertical" writes the value upward across the top edge of a CropBox
    [0 0 612 300]; "slug" puts it wholly past the edge, a word space after
    visible text (one stretch with it in reading 4)."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    if kind == "vertical":
        start = EDGE - fitz.get_text_length("123-", "helv", 14)
        text = b"BT /F1 14 Tf 0 1 -1 0 200 %.2f Tm (" % start + SSN.encode() + b") Tj ET"
        crop = "[0 0 612 300]"
    else:
        before = "123-" if kind == "flat" else "Page 7 "
        x = EDGE - fitz.get_text_length(before, "helv", 14)
        shown = SSN if kind == "flat" else "Page 7 " + SSN
        text = b"BT /F1 14 Tf %.2f 400 Td (" % x + shown.encode() + b") Tj ET"
        crop = "[0 0 300 792]"
    contents = doc.get_new_xref()
    doc.update_object(contents, "<<>>")
    doc.update_stream(contents, text)
    doc.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
    doc.xref_set_key(page.xref, "Resources",
                     "<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>")
    doc.xref_set_key(page.xref, "CropBox", crop)
    doc.xref_set_key(page.xref, "Rotate", str(rotate))
    return _save(doc, path)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("rule", ["value", "pattern"])
class TestAcrossTheEdge:
    @staticmethod
    def _rules(rule: str) -> tuple[list[dict[str, str]], str]:
        if rule == "value":
            return SSN_RULES, "value rule #1"
        return [{"name": "any ssn", "pattern": r"\d{3}-\d{2}-\d{4}"}], "pattern rule #1"

    @pytest.mark.parametrize("kind", ["flat", "vertical"])
    def test_a_match_running_past_the_edge_is_boxed_where_it_shows(
            self, tmp_path, monkeypatch, rotate, rule, kind) -> None:
        """Found only across the edge (reading 4 or 5): its visible part
        is a solid box on its ink, marked part of a value, and its
        off-page part is listed outside the crop."""
        rules, label = self._rules(rule)
        pdf = _edge_pdf(tmp_path / f"{kind}{rotate}.pdf", rotate, kind)
        run, the_plan = mapped(monkeypatch, tmp_path, pdf, rules)
        assert run.code == 1
        (box,) = boxes(the_plan, plan.SOLID)
        assert box.part and "past the visible edge" in box.how
        assert box.label.startswith(label)
        page = fitz.open(pdf)[0]
        ink = _ink(page)
        assert ink is not None
        assert _around(box.rect, 0.5).contains(ink), (box.rect, ink)
        assert _around(page.rect, 1.5).contains(box.rect), (box.rect, page.rect)
        assert [(p, n) for p, _, n in sections(the_plan, "outside")] == [
            (1, "the part past the visible edge")]
        assert not sections(the_plan, "unpositioned")

    def test_a_match_wholly_past_the_edge_is_only_listed(self, tmp_path, monkeypatch,
                                                         rotate, rule) -> None:
        """Wholly off the page, though reading 4 reads it on one stretch
        with visible text: listed outside the crop, never boxed."""
        rules, label = self._rules(rule)
        pdf = _edge_pdf(tmp_path / f"slug{rotate}.pdf", rotate, "slug")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf, rules)
        assert run.code == 1
        assert not boxes(the_plan, plan.SOLID)
        listed = sections(the_plan, "outside")
        assert [(p, n) for p, _, n in listed] == [(1, "")] and listed[0][1].startswith(label)
        assert not sections(the_plan, "unpositioned")


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_a_seam_half_running_past_the_edge(tmp_path, monkeypatch, rotate) -> None:
    """A value whose first half ends page 1's last line, partly past the
    visible edge (read only in reading 4), and whose second half opens
    page 2: both visible parts boxed, the off-page part listed."""
    doc = fitz.open()
    first = doc.new_page(width=612, height=792)
    x = EDGE - fitz.get_text_length("ID 123-", "helv", 14)
    first.insert_text((x, 700), "ID 123-45", fontname="helv", fontsize=14)
    first.set_cropbox(fitz.Rect(0, 0, EDGE, 792))
    first.set_rotation(rotate)
    doc.new_page(width=612, height=792).insert_text((72, 72), "-6789 end", fontname="helv",
                                                    fontsize=14)
    pdf = _save(doc, tmp_path / f"seam{rotate}.pdf")
    run, the_plan = mapped(monkeypatch, tmp_path, pdf)
    assert run.code == 1
    solid = boxes(the_plan, plan.SOLID)
    assert sorted(b.page for b in solid) == [1, 2] and all(b.part for b in solid)
    assert sections(the_plan, "outside") == [
        (1, "value rule #1", "part of a value across pages 1-2, past the visible edge")]
    assert not sections(the_plan, "unpositioned")


# ── Classes (design §2, §9 test 5) ────────────────────────────────────
def _vis_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    off = doc.add_ocg("Layer A", on=False)
    for i, (flag, name) in enumerate([
            (fitz.PDF_ANNOT_IS_HIDDEN, "HIDDEN"), (fitz.PDF_ANNOT_IS_NO_VIEW, "NOVIEW"),
            (fitz.PDF_ANNOT_IS_NO_VIEW | fitz.PDF_ANNOT_IS_PRINT, "NOVIEWPRINT")]):
        a = page.add_freetext_annot(fitz.Rect(100, 300 + 40 * i, 400, 330 + 40 * i),
                                    f"{name} {SSN}")
        a.set_flags(flag)
        a.update()
    a = page.add_freetext_annot(fitz.Rect(100, 500, 400, 530), f"ANNOCG {SSN}")
    a.set_oc(off)
    a.update()
    return _save(doc, path)


class TestClasses:
    def test_flags_and_layers(self, tmp_path, monkeypatch) -> None:
        pdf = _vis_pdf(tmp_path / "vis.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        hows = [(b.style, b.how) for b in the_plan.boxes]
        # NoView+Print: dotted, "when printed" -- and only that one.
        assert hows.count((plan.DOTTED, "when printed")) == 1
        # Hidden, and NoView without Print: counted as not visible.
        assert the_plan.not_visible.get("hidden annotation") == 2
        # On a switched-off layer: listed, never boxed; the layer makes the
        # map incomplete.
        assert sections(the_plan, "off_layer") == [(1, "value rule #1", "")]
        assert the_plan.off_layers == 1
        assert any("switched-off layer" in r for r in the_plan.reasons)

    def test_off_layer_that_prints(self, tmp_path, monkeypatch) -> None:
        # Document.get_ocgs reports a group with /Usage as on even though
        # the default configuration turns it off (and MuPDF hides it): the
        # map counts the configuration's /OFF too.
        doc = fitz.open()
        page = doc.new_page()
        off = doc.add_ocg("Print only", on=False)
        doc.xref_set_key(off, "Usage", "<</Print<</PrintState/ON>>>>")
        a = page.add_freetext_annot(fitz.Rect(100, 100, 400, 130), f"P {SSN}")
        a.set_oc(off)
        a.update()
        pdf = _save(doc, tmp_path / "printstate.pdf")
        assert fitz.open(pdf).get_ocgs()[off]["on"] is True
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert sections(the_plan, "off_layer") == [(1, "value rule #1", "may print")]
        assert the_plan.off_layers == 1

    def test_widget_on_off_layer_gets_no_box(self, tmp_path, monkeypatch) -> None:
        # The widget's value is stored, but the page text at its rectangle
        # is empty: the layer check must come before the page-text check.
        doc = fitz.open()
        page = doc.new_page()
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "f1"
        w.rect = fitz.Rect(100, 100, 300, 120)
        w.field_value = SSN
        page.add_widget(w)
        off = doc.add_ocg("L", on=False)
        doc.xref_set_key(next(page.widgets()).xref, "OC", f"{off} 0 R")
        pdf = _save(doc, tmp_path / "ocwidget.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert not [b for b in the_plan.boxes if b.how != "selectable text"]
        assert sections(the_plan, "off_layer")

    def test_base_state_off_is_counted_off(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        layer = doc.add_ocg("L", on=True)
        page.insert_text((100, 100), f"LAYER {SSN}", oc=layer)
        cat = doc.pdf_catalog()
        doc.xref_set_key(cat, "OCProperties/D/BaseState", "/OFF")
        doc.xref_set_key(cat, "OCProperties/D/ON", "[]")
        doc.xref_set_key(cat, "OCProperties/D/OFF", "[]")
        pdf = _save(doc, tmp_path / "basestate.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert the_plan.off_layers == 1
        assert any("1 switched-off layer" in r for r in the_plan.reasons)

    def test_ocmd_annotation(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        layer = doc.add_ocg("L", on=True)
        hiding = doc.set_ocmd(ve=["not", layer])
        shown = page.add_freetext_annot(fitz.Rect(100, 100, 400, 130), f"SHOWN {SSN}")
        shown.set_oc(doc.set_ocmd(ve=["and", layer]))
        shown.update()
        sq = page.add_rect_annot(fitz.Rect(100, 300, 200, 350))
        sq.set_info(content=f"note {SSN}")
        sq.set_oc(hiding)
        sq.update()
        pdf = _save(doc, tmp_path / "ocmd.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        # Shown on the page: a solid box, whatever the layer rule says.
        assert any(b.how == "on page" for b in boxes(the_plan, plan.SOLID))
        # Not shown: the map does not evaluate the expression, and says so.
        assert sections(the_plan, "layer_rule") == [(1, "value rule #1", "")]

    def test_markup_without_popup_is_on_hover(self, tmp_path, monkeypatch) -> None:
        # A Square and a Highlight carrying /Contents, with no /Popup: shown
        # on hover and in the comments panel, never "not visible".
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((100, 310), "highlighted words")
        a = page.add_rect_annot(fitz.Rect(100, 100, 200, 150))
        a.set_info(content=f"note {SSN}")
        a.update()
        h = page.add_highlight_annot(fitz.Rect(100, 300, 200, 320))
        h.set_info(content=f"hl {SSN}")
        h.update()
        pdf = _save(doc, tmp_path / "popup.pdf")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        dashed = boxes(the_plan, plan.DASHED)
        assert len(dashed) == 2
        assert {b.how for b in dashed} == {"on hover / in the comments panel"}
        # The annotations' own dictionaries hold the value too (the Objects
        # layer finds it there): accounted for, not "live content".
        assert sections(the_plan, "dict_copy") and not sections(the_plan, "live")
        alone = plan_of(pdf, without_ocr_warning(run.header))
        assert alone.row == 3 and alone.flags.names() == {"X"}

    def test_open_popup_and_open_note_are_on_page(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.add_text_annot((100, 100), f"note {SSN}")
        sq = page.add_rect_annot(fitz.Rect(100, 300, 200, 350))
        sq.set_info(content=f"sq {SSN}")
        sq.set_popup(fitz.Rect(300, 300, 500, 400))
        sq.update()
        pdf = tmp_path / "open.pdf"
        doc.save(pdf)
        text_xref = [a for a in page.annots() if a.type[1] == "Text"][0].xref
        popup = doc.xref_get_key(sq.xref, "Popup")[1].split()[0]
        doc.close()
        doc = fitz.open(pdf)
        doc.xref_set_key(text_xref, "Open", "true")
        doc.xref_set_key(int(popup), "Open", "true")
        doc.saveIncr()
        doc.close()
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert [b.how for b in boxes(the_plan, plan.SOLID)].count("on page, open note") == 2

    def test_an_open_note_shows_its_contents_only(self, tmp_path, monkeypatch) -> None:
        # The author (/T) of an open note is not drawn as its text is: it
        # stays "on hover / in the comments panel".
        doc = fitz.open()
        page = doc.new_page()
        note = page.add_text_annot((100, 100), "nothing here")
        note.set_info(title=NAME)
        note.update()
        xref = note.xref
        pdf = _save(doc, tmp_path / "author.pdf")
        doc = fitz.open(pdf)
        doc.xref_set_key(xref, "Open", "true")
        doc.saveIncr()
        doc.close()
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf, [{"name": "who", "value": NAME}])
        assert not boxes(the_plan, plan.SOLID)
        (box,) = boxes(the_plan, plan.DASHED)
        assert box.how == "on hover / in the comments panel"

    def test_toggled_view_is_on_hover(self, tmp_path, monkeypatch) -> None:
        # NoView with ToggleNoView: hidden until the pointer is over it.
        doc = fitz.open()
        page = doc.new_page()
        a = page.add_rect_annot(fitz.Rect(100, 100, 200, 150))
        a.set_info(content=f"note {SSN}")
        a.update()
        doc.xref_set_key(a.xref, "F", str(32 | 256))
        pdf = _save(doc, tmp_path / "toggle.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        (box,) = boxes(the_plan, plan.DASHED)
        assert box.how == "on hover (toggled view)"
        assert not the_plan.not_visible

    def test_stale_widget_value_is_hatched(self, tmp_path, monkeypatch) -> None:
        # /V changed, the appearance not: shown by viewers that redraw
        # fields (NeedAppearances, pdf.js).
        doc = fitz.open()
        page = doc.new_page()
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.field_name = "f1"
        w.rect = fitz.Rect(100, 100, 300, 120)
        w.field_value = "nothing here"
        page.add_widget(w)
        xref = next(page.widgets()).xref
        pdf = tmp_path / "stale.pdf"
        doc.save(pdf)
        doc.close()
        doc = fitz.open(pdf)
        doc.xref_set_key(xref, "V", f"({SSN})")
        doc.saveIncr()
        doc.close()
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        (box,) = boxes(the_plan, plan.HATCHED)
        assert box.how == "on page in viewers that redraw form fields"
        assert the_plan.flags.V

    @pytest.mark.parametrize("kind", ["listbox", "checkbox", "password"])
    def test_fields_whose_value_the_page_does_not_show(self, tmp_path, monkeypatch,
                                                       kind) -> None:
        doc = fitz.open()
        page = doc.new_page()
        w = fitz.Widget()
        w.field_name = "f1"
        w.rect = fitz.Rect(100, 100, 300, 160)
        if kind == "listbox":
            w.field_type = fitz.PDF_WIDGET_TYPE_LISTBOX
            w.choice_values = ["none", SSN]
            w.field_value = SSN
        elif kind == "checkbox":
            w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
            w.rect = fitz.Rect(100, 100, 115, 115)
            w.field_value = True
        else:
            w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.field_flags = fitz.PDF_TX_FIELD_IS_PASSWORD
            w.field_value = SSN
        page.add_widget(w)
        xref = next(page.widgets()).xref
        if kind == "checkbox":
            # The export (on-state) name is the value; the check mark is
            # all the page draws.
            on, off = (doc.xref_get_key(xref, f"AP/N/{s}")[1] for s in ("Yes", "Off"))
            doc.xref_set_key(xref, "AP", f"<</N<</Off {off}/123456789 {on}>>>>")
            doc.xref_set_key(xref, "V", "/123456789")
            doc.xref_set_key(xref, "AS", "/123456789")
        pdf = _save(doc, tmp_path / f"{kind}.pdf")
        if kind != "checkbox":
            _blank_appearance(pdf, xref)
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert not boxes(the_plan, plan.HATCHED)          # never hatched
        if kind == "listbox":
            (box,) = boxes(the_plan, plan.DASHED)
            assert box.how == "in the field's list, on click or scroll"
        elif kind == "checkbox":
            assert sections(the_plan, "stored") == [
                (1, "value rule #1", "checkbox or radio export value")]
        else:
            assert sections(the_plan, "password") == [(1, "value rule #1", "")]
            assert plan.SECTIONS["password"][0] == (
                "Stored; not shown by viewers that honour the password flag")

    def test_attachments_links_and_missing_rect(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        doc.embfile_add("notes.txt", f"call about {SSN}\n".encode(), filename="notes.txt")
        page.add_file_annot((300, 300), f"attached {SSN}\n".encode(), "a.txt")
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(10, 20, 110, 40),
                          "uri": f"https://example.test/{SSN}"})
        sq = page.add_rect_annot(fitz.Rect(100, 500, 200, 550))
        sq.set_info(content=f"gone {SSN}")
        sq.update()
        pdf = tmp_path / "attach.pdf"
        doc.save(pdf)
        doc.close()
        doc = fitz.open(pdf)
        doc.xref_set_key(sq.xref, "Rect", "null")
        doc.saveIncr()
        doc.close()
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        hows = {b.how for b in boxes(the_plan, plan.DASHED)}
        assert "on hover" in hows
        # The attached files' bodies are review-tier matches in the scan
        # (REVIEW_HIDDEN_TEXT): listed for review, never boxed or listed
        # as certain.
        assert "when opened" not in hows and not sections(the_plan, "attachments")
        assert sections(the_plan, "review")
        assert any(p == 1 for p, _, _ in sections(the_plan, "unpositioned"))

    def test_a_review_tier_hidden_match_is_never_boxed(self, tmp_path, monkeypatch) -> None:
        """A value only in an annotation's attached file: the scan warns
        (exit 2, no finding), so the map lists it for review and draws
        nothing."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Cover page")
        page.add_file_annot((300, 300), f"attached {SSN}\n".encode(), "a.txt")
        pdf = _save(doc, tmp_path / "payload.pdf")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert run.code == 2 and not run.report["findings"]
        assert not the_plan.boxes
        assert sections(the_plan, "review") and not sections(the_plan, "attachments")

    def test_switched_off_layer_text_and_live_content(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        off = doc.add_ocg("Layer A", on=False)
        page.insert_text((100, 100), f"OFFLAYER {SSN}", oc=off)
        pdf = tmp_path / "offlayer.pdf"
        doc.save(pdf, garbage=4, deflate=True)
        doc.close()
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert the_plan.off_layers == 1 and sections(the_plan, "live")
        assert any("switched-off layer" in r for r in the_plan.reasons), the_plan.reasons

    def test_a_stored_field_keeps_its_live_copy_flagged(self, tmp_path, monkeypatch) -> None:
        """A checkbox whose export value is the SSN and whose tooltip (/TU,
        which the Hidden layer never reads) holds it too: the Objects
        layer's finding in the widget may be the tooltip, shown on hover,
        so a "stored, not shown" classification must not absorb it."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Cover page")
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
        w.field_name = "agree"
        w.rect = fitz.Rect(100, 200, 115, 215)
        w.field_value = True
        xref = page.add_widget(w).xref
        doc.xref_set_key(xref, "TU", fitz.get_pdf_str(f"Holder {SSN}"))
        doc.xref_set_key(xref, "V", "/" + SSN.replace("-", "#2D"))
        doc.xref_set_key(xref, "AS", "/" + SSN.replace("-", "#2D"))
        pdf = _save(doc, tmp_path / "tooltip.pdf")
        run, _ = mapped(monkeypatch, tmp_path, pdf)
        alone = plan_of(pdf, without_ocr_warning(run.header))
        assert sections(alone, "stored") and sections(alone, "live")
        assert alone.row == 2 and "LEAK, NOT VISIBLE" not in alone.headline

    def test_a_shared_appearance_stays_flagged_unless_every_owner_is_boxed(
            self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        for n, (value, y) in enumerate(((SSN, 100), ("other", 200))):
            w = fitz.Widget()
            w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            w.field_name = f"f{n}"
            w.rect = fitz.Rect(100, y, 300, y + 20)
            w.field_value = value
            page.add_widget(w)
        first, second = [w.xref for w in page.widgets()]
        doc.xref_set_key(second, "AP", f"<</N {doc.xref_get_key(first, 'AP/N')[1]}>>")
        pdf = _save(doc, tmp_path / "shared.pdf")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        appearance = int(fitz.open(pdf).xref_get_key(first, "AP/N")[1].split()[0])
        live = [i.detail for i in the_plan.listed if i.section == "live"]
        assert any(d.startswith(f"object {appearance},") for d in live), live

    def test_an_appearance_under_a_dashed_box_stays_live(self, tmp_path, monkeypatch) -> None:
        """A Square's appearance is drawn on the page; its dashed box is
        about /Contents on hover. Here the appearance draws the value in
        a font whose ToUnicode maps every digit to "X": the page text never
        shows it, the Objects layer finds it in the appearance stream, and
        that copy must stay live content."""
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Cover page")
        a = page.add_rect_annot(fitz.Rect(100, 200, 300, 230))
        a.set_info(content=SSN)
        a.update()
        pdf = tmp_path / "ap.pdf"
        doc.save(pdf)
        doc.close()
        doc = fitz.open(pdf)
        ap = int(doc.xref_get_key(a.xref, "AP/N")[1].split()[0])
        cmap = doc.get_new_xref()
        doc.update_object(cmap, "<<>>")
        doc.update_stream(cmap, (
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def "
            "1 begincodespacerange <00> <FF> endcodespacerange 11 beginbfchar "
            + " ".join(f"<{ord(c):02X}> <0058>" for c in "0123456789-")
            + " endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
        ).encode())
        font = doc.get_new_xref()
        doc.update_object(font, f"<</Type/Font/Subtype/Type1/BaseFont/Helvetica"
                                f"/Encoding/WinAnsiEncoding/ToUnicode {cmap} 0 R>>")
        doc.update_object(ap, f"<</Type/XObject/Subtype/Form/BBox[0 0 200 30]"
                              f"/Resources<</Font<</FX {font} 0 R>>>>>>")
        doc.update_stream(ap, f"BT /FX 12 Tf 4 10 Td ({SSN}) Tj ET".encode())
        doc.saveIncr()
        doc.close()
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert boxes(the_plan, plan.DASHED)
        live = [i.detail for i in the_plan.listed if i.section == "live"]
        assert any(d.startswith(f"object {ap},") for d in live), the_plan.listed

    @pytest.mark.parametrize("crop", ["[0 200 600 1000]", "[0 -50 600 750]", "[10 0 600 800]"])
    def test_a_shifted_crop_counts(self, tmp_path, monkeypatch, crop) -> None:
        doc = fitz.open()
        doc.new_page(width=600, height=800).insert_text((72, 72), "Cover page")
        pdf = tmp_path / "shifted.pdf"
        doc.save(pdf)
        doc.close()
        doc = fitz.open(pdf)
        doc.xref_set_key(doc[0].xref, "CropBox", crop)
        doc.saveIncr()
        doc.close()
        page = fitz.open(pdf)[0]
        assert locate.outside_crop(page)
        run, _ = mapped(monkeypatch, tmp_path, pdf)
        alone = plan_of(pdf, without_ocr_warning(run.header))
        assert alone.row == 2 and any("outside the crop" in r for r in alone.reasons)

    def test_an_uncropped_page_does_not_count(self) -> None:
        doc = fitz.open()
        assert not locate.outside_crop(doc.new_page(width=600, height=800))

    @pytest.mark.parametrize("hidden_by", ["layer rule", "crop"])
    def test_images_behind_a_layer_rule_or_the_crop(self, tmp_path, monkeypatch,
                                                    hidden_by) -> None:
        """A picture of the value that no check reads: behind a visibility
        expression over a layer that is on, or below the CropBox. Neither
        may read "Nothing visible found"."""
        src = fitz.open()
        src_page = src.new_page(width=400, height=80)
        src_page.insert_text((20, 50), f"SSN {SSN}", fontsize=24)
        png = src_page.get_pixmap(dpi=100).tobytes("png")
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Cover page")
        if hidden_by == "layer rule":
            rule = doc.set_ocmd(ocgs=[doc.add_ocg("Draft", on=True)], policy="AllOff")
            page.insert_image(fitz.Rect(72, 200, 472, 280), stream=png, oc=rule)
        else:
            page.insert_image(fitz.Rect(72, 700, 472, 780), stream=png)
            page.set_cropbox(fitz.Rect(0, 0, 595, 600))
        pdf = tmp_path / "picture.pdf"
        doc.save(pdf, garbage=4, deflate=True)
        run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        alone = plan_of(pdf, without_ocr_warning(run.header))
        want = "1 layer rule: content behind" if hidden_by == "layer rule" else (
            "1 page has area outside the crop; images there not checked")
        assert any(r.startswith(want) for r in alone.reasons)
        assert alone.row == 2 and "Nothing visible found" not in alone.headline


# ── Tiers and truth (design §9 tests 6, 8) ────────────────────────────
class TestTiers:
    def test_fused_only_and_reconstructed_only_never_get_a_box(self, tmp_path,
                                                                monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((100, 100), "Ref 123-45-")
        page.insert_text((100, 114), "6789 end")
        for i, ch in enumerate(SSN.replace("-", "")):  # one character per line
            page.insert_text((400, 200 + 14 * i), ch)
        pdf = _save(doc, tmp_path / "tiers.pdf")
        rules = [{"name": "any ssn", "class": "ssn"}, {"name": "ssn", "value": SSN}]
        run, the_plan = mapped(monkeypatch, tmp_path, pdf, rules)
        codes = {w["code"] for w in run.report["warnings"]}
        assert {"REVIEW_FUSED_PATTERN", "REVIEW_CROSS_LINE"} <= codes
        assert not the_plan.boxes
        assert {label for _, label, _ in sections(the_plan, "review")} >= {
            "built-in type rule #1", "value rule #1"}
        assert the_plan.flags.P

    def test_every_box_has_a_truth_entry(self, tmp_path, monkeypatch) -> None:
        pdf = _geometry_pdf(tmp_path / "truth.pdf", 90, "shifted", content="widget")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        truth = run.header["sink"]["truth"]["Text"]
        hidden = {(e["page"], name) for e in run.header["sink"]["hidden"] if e["hard"]
                  for name in e["rules"]}
        for box in the_plan.boxes:
            if box.how == "selectable text":
                assert truth[str(box.page)]["hard"]
            else:
                assert (box.page, "ssn") in hidden

    def test_a_dropped_box_is_listed_could_not_be_positioned(self, tmp_path,
                                                             monkeypatch) -> None:
        doc = fitz.open()
        doc.new_page().insert_text((100, 100), f"SSN {SSN}")
        pdf = _save(doc, tmp_path / "drop.pdf")
        run, _ = mapped(monkeypatch, tmp_path, pdf)
        monkeypatch.setattr(locate, "verify_text_box", lambda *a: False)
        the_plan = plan_of(pdf, without_ocr_warning(run.header))
        assert not the_plan.boxes
        assert sections(the_plan, "unpositioned") == [(1, "value rule #1", "Text layer")]
        assert the_plan.reasons == [] and the_plan.flags.I and the_plan.row == 2

    def test_a_page_that_cannot_be_located_costs_only_its_findings(
            self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        for _ in range(2):
            doc.new_page().insert_text((100, 100), f"SSN {SSN}")
        pdf = _save(doc, tmp_path / "raise.pdf")
        run, _ = mapped(monkeypatch, tmp_path, pdf)
        real = locate.value_hits

        def broken_on_page_one(pt, page_no, secret):
            if page_no == 1:
                raise RuntimeError("cannot locate")
            return real(pt, page_no, secret)

        monkeypatch.setattr(locate, "value_hits", broken_on_page_one)
        the_plan = plan_of(pdf, run.header)
        assert [b.page for b in boxes(the_plan, plan.SOLID)] == [2]
        assert (1, "value rule #1", "Text layer, could not be located") in sections(
            the_plan, "unpositioned")

    def test_fail_fast_keeps_the_stopping_pages_truth(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        for _ in range(3):
            doc.new_page().insert_text((100, 100), f"SSN {SSN}")
        pdf = _save(doc, tmp_path / "ff.pdf")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf, SSN_RULES, "--fail-fast")
        assert {b.page for b in the_plan.boxes} == {1}
        assert sections(the_plan, "not_scanned") == [(None, "Text layer", "pp. 2-3")]


class TestOccurrences:
    """Every occurrence is boxed or listed on its own (verification reads
    the glyphs in the scan's geometric order, not the stream's)."""

    @staticmethod
    def _split(page: fitz.Page, x: float, y: float) -> None:
        # The value drawn right half first: out of reading order.
        left, right = "123-45-", "6789"
        width = fitz.get_text_length(left, fontname="helv", fontsize=12)
        page.insert_text((x + width, y), right, fontname="helv", fontsize=12)
        page.insert_text((x, y), left, fontname="helv", fontsize=12)

    def test_a_value_drawn_out_of_order_is_boxed(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        self._split(doc.new_page(), 100, 200)
        pdf = _save(doc, tmp_path / "ooo.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert len(boxes(the_plan, plan.SOLID)) == 1
        assert not sections(the_plan, "unpositioned")

    def test_an_out_of_order_copy_beside_a_plain_one(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((100, 100), SSN, fontname="helv", fontsize=12)
        self._split(page, 100, 300)
        pdf = _save(doc, tmp_path / "two.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert len(boxes(the_plan, plan.SOLID)) == 2
        assert not sections(the_plan, "unpositioned")

    def test_a_copy_that_cannot_be_boxed_is_listed(self, tmp_path, monkeypatch) -> None:
        # Two copies, the second half cut by a clip path; were either to
        # fail its box, it must be listed, not dropped behind the other.
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), f"Ref {SSN}", fontname="helv", fontsize=12)
        pdf = tmp_path / "clipped.pdf"
        doc.save(pdf)
        doc.close()
        doc = fitz.open(pdf)
        page = doc[0]
        xref = page.get_contents()[0]
        font = re.findall(r"/(\w+)\s+\d+ 0 R", doc.xref_get_key(page.xref, "Resources/Font")[1])[0]
        doc.update_stream(xref, doc.xref_stream(xref) + (
            f"\nq 72 600 m 120 600 l 120 640 l 72 640 l h W n BT /{font} 14 Tf 72 610 Td "
            f"({SSN}) Tj ET Q\n").encode())
        doc.saveIncr()
        doc.close()
        run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert len(boxes(the_plan, plan.SOLID)) == 2
        real = locate.verify_text_box
        calls = []

        def second_fails(page, pt, box, needle):
            calls.append(box)
            return len(calls) == 1 and real(page, pt, box, needle)

        monkeypatch.setattr(locate, "verify_text_box", second_fails)
        the_plan = plan_of(pdf, run.header)
        assert len(boxes(the_plan, plan.SOLID)) == 1
        assert sections(the_plan, "unpositioned") == [(1, "value rule #1", "Text layer")]


# ── The gallery oracle (design §9 test 7) ─────────────────────────────
ORACLE = {
    "page.box-over-text": "full",
    "layout.page-break": "partial",
    "layout.raw-line-wrap": "wrapped",
    "layout.several-values-ssn": "later",
    "page.extreme-coordinates": "none",
}


@pytest.mark.parametrize("case_id, visibility", sorted(ORACLE.items()))
def test_gallery_visibility_oracle(case_id, visibility, tmp_path, monkeypatch) -> None:
    from caselib import REGISTRY, load
    from caselib.run import build as build_case
    from gallery.build import _secret_visibility

    load()
    case = REGISTRY[case_id]
    pdf = build_case(case, tmp_path / f"{case_id}.pdf")
    assert _secret_visibility(case, pdf) == visibility
    run, the_plan = mapped(monkeypatch, tmp_path, pdf, list(case.rules))
    on_one = [b for b in the_plan.boxes if b.page == 1 and b.style == plan.SOLID]
    if visibility == "full":
        assert on_one
    elif visibility == "partial":
        assert any(b.part for b in on_one)
    elif visibility == "wrapped":
        assert any(p == 1 for p, _, _ in sections(the_plan, "review"))
    elif visibility == "later":
        assert not on_one and any(b.page > 1 for b in the_plan.boxes)
    else:
        assert not on_one


# ── Masked labels and the plan's limits (design §6, §8) ─────────────
def _value_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), f"SSN {SSN} for {NAME}")
    return _save(doc, path)


class TestMaskedLabels:
    """The plan's own masking (Rules.label / Rules.masked). That nothing
    of a value reaches the map file is the self-check's job, tested with
    the rendering."""

    def test_a_pattern_tail_that_is_part_of_a_value_is_dropped(self, tmp_path,
                                                                monkeypatch) -> None:
        pdf = _value_pdf(tmp_path / "overlap.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf,
                                f'exact_values:\n  - "{SSN}"\nentity_types: [ssn]\n')
        labels = {b.label for b in the_plan.boxes}
        assert "built-in type: SSN ****" in labels
        assert "exact value #1 (exact_values[0])" in labels and not the_plan.legend
        # With no exact value it would reveal, the masked tail stays.
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf, "entity_types: [ssn]\n")
        assert "built-in type: SSN ****6789" in {b.label for b in the_plan.boxes}

    def test_a_one_character_tail_on_a_value_is_hidden(self, tmp_path, monkeypatch) -> None:
        # \d\d matches 12, 34, 67, 89 inside the value; mask() of each shows
        # one digit of it.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), f"Ref {SSN} end")
        pdf = _save(doc, tmp_path / "pairs.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf,
                                [{"name": "ssn", "value": SSN},
                                 {"name": "pair", "pattern": r"\d\d"}])
        pairs = [b for b in the_plan.boxes if b.label.startswith("pattern rule #1")]
        assert pairs and {b.label for b in pairs} == {"pattern rule #1 ****"}
        assert all("****" == i.label.split(" ")[-1] for i in the_plan.listed
                   if i.label.startswith("pattern rule #1"))

    def test_a_one_character_report_sample_on_a_value_is_hidden(self) -> None:
        rules = plan.Rules({"secrets": [["ssn", "123456789"]], "patterns": [],
                            "labels": [["ssn", "value", "'ssn'"]]}, lambda t, *n: t)
        assert rules.masked("****9") == "****"
        assert rules.masked("****0") == "****0"

    def test_a_pattern_running_past_a_value_hides_its_tail(self, tmp_path, monkeypatch) -> None:
        # 123-45-67890 holds the value and runs on: mask() would show 7890,
        # three of the value's characters.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Account 123-45-67890 ok")
        pdf = _save(doc, tmp_path / "runon.pdf")
        run, the_plan = mapped(monkeypatch, tmp_path, pdf,
                               f'exact_values:\n  - "{SSN}"\npatterns:\n'
                               '  - "\\\\d{3}-\\\\d{2}-\\\\d{5}"\n')
        assert any("****7890" in f["sample"] for f in run.report["findings"])
        assert "pattern #1 (patterns[0]) ****" in {b.label for b in the_plan.boxes}
        assert not any("7890" in i.label for i in the_plan.listed)

    def test_a_legacy_rule_is_numbered(self, tmp_path, monkeypatch) -> None:
        # The name is the user's own words, and may be the secret itself.
        pdf = _value_pdf(tmp_path / "named.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf, [{"name": SSN, "value": SSN}])
        assert {b.label for b in the_plan.boxes} == {"value rule #1"}
        assert the_plan.legend == ["value rule #1 = 1st value entry, rules-file entry 1"]

    def test_the_box_cap_makes_the_map_incomplete(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        page = doc.new_page(width=612, height=2000)
        for row in range(70):
            page.insert_text((20, 20 + row * 28), f"{SSN} {SSN} {SSN}", fontsize=8)
        pdf = _save(doc, tmp_path / "many.pdf")
        _run, the_plan = mapped(monkeypatch, tmp_path, pdf)
        assert len(the_plan.boxes) == plan.MAX_BOXES_PER_PAGE
        assert any(r.startswith("box cap reached on p.1 (10 not drawn)")
                   for r in the_plan.reasons)
        assert the_plan.flags.I

    def test_same_label_overlapping_boxes_merge(self) -> None:
        a = plan.Box(1, fitz.Rect(0, 0, 10, 10), plan.SOLID, "l", "h")
        b = plan.Box(1, fitz.Rect(2, 0, 12, 10), plan.SOLID, "l", "h")
        c = plan.Box(1, fitz.Rect(2, 0, 12, 10), plan.SOLID, "other", "h")
        merged = plan._merge([a, b, c])
        assert len(merged) == 2 and merged[0].rect == fitz.Rect(0, 0, 12, 10)


# ── Headline fixtures, one per reachable row (design §9 test 9) ───────
@requires_full_env
class TestHeadlineFixtures:
    def _run(self, tmp_path, monkeypatch, pdf: Path, *extra: str):
        return mapped(monkeypatch, tmp_path, pdf, SSN_RULES, *extra, ocr=True)

    def test_row_1_visible(self, tmp_path, monkeypatch) -> None:
        _run, the_plan = self._run(tmp_path, monkeypatch, _value_pdf(tmp_path / "r1.pdf"))
        assert the_plan.row == 1 and "VISIBLE SECRETS FOUND" in the_plan.headline

    def test_row_3_shown_in_a_panel(self, tmp_path, monkeypatch) -> None:
        # Only in a compressed XMP packet: a properties panel shows it, no
        # page does, and no live object holds it as a PDF string.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Nothing to see")
        doc.set_xml_metadata(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/" dc:source="{SSN}"/></rdf:RDF>'
            '</x:xmpmeta>')
        pdf = tmp_path / "r3.pdf"
        doc.save(pdf, garbage=4, deflate=True)
        doc.close()
        run, the_plan = self._run(tmp_path, monkeypatch, pdf)
        assert run.code == 1 and {f["layer"] for f in run.report["findings"]} == {"Metadata"}
        assert the_plan.row == 3
        assert "SHOWN ON CLICK / HOVER / PRINT / OPEN / IN PANELS" in the_plan.headline

    def test_row_4_stored_only(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Nothing to see")
        orphan = doc.get_new_xref()
        doc.update_object(orphan, f"<</Note ({SSN})>>")
        pdf = tmp_path / "r4.pdf"
        doc.save(pdf)           # no garbage collection: the orphan stays
        doc.close()
        run, the_plan = self._run(tmp_path, monkeypatch, pdf)
        assert run.code == 1
        assert the_plan.row == 4 and "LEAK, NOT VISIBLE" in the_plan.headline

    def test_row_7_clean(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Nothing to see")
        run, the_plan = self._run(tmp_path, monkeypatch, _save(doc, tmp_path / "r7.pdf"))
        assert run.code == 0
        assert the_plan.row == 7 and the_plan.headline == headline.ROWS[7]

    def test_row_2_and_5(self, tmp_path, monkeypatch) -> None:
        # Exit 0 with a switched-off layer: incomplete (row 2), as approved.
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Nothing to see")
        page.insert_text((72, 100), "Draft only", oc=doc.add_ocg("Draft", on=False))
        run, the_plan = self._run(tmp_path, monkeypatch, _save(doc, tmp_path / "r2.pdf"))
        assert run.code == 0 and the_plan.row == 2 and "MAP INCOMPLETE" in the_plan.headline
        # Exit 2 for content stored outside the document only (row 5): a
        # text-sized orphaned image.
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Nothing to see")
        img = doc.get_new_xref()
        doc.update_object(img, "<</Type/XObject/Subtype/Image/Width 400/Height 40"
                               "/ColorSpace/DeviceGray/BitsPerComponent 8>>")
        doc.update_stream(img, bytes(400 * 40))
        pdf = tmp_path / "r5.pdf"
        doc.save(pdf)
        doc.close()
        run, the_plan = self._run(tmp_path, monkeypatch, pdf)
        assert run.code == 2
        assert {(w["code"], w["storage"]) for w in run.report["warnings"]} == {
            ("LEFTOVER_IMAGE", "orphaned")}
        assert the_plan.row == 5
        assert "Nothing visible found; uncertified (exit 2) for: LEFTOVER_IMAGE" in (
            the_plan.headline)
