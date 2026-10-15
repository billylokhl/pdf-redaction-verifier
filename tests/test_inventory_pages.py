"""The page tree and page attributes (Phase 3a-7b, ADR 0011 item 2):
only the canonical tree reads alike in both readers; anything else is
flagged PAGE_TREE with its PageTreeRule, and an unflagged tree's page
list and inherited attributes agree with MuPDF and libqpdf. Every file
is generated here; fabricated content only."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from scorecard.inventory import compare
from scorecard.pdfgen import Writer

from redaction_verifier.budget import Budget, Limits
from redaction_verifier.inventory import build_inventory
from redaction_verifier.inventory.pages import PageTreeRule
from redaction_verifier.ledger import FlagReason

from .conftest import requires_qpdf
from .steps import charged_steps

# Lines the inventory executes per unit of work the page walk charges, at
# most (#94): coarser than the graph's charges (measured 11-34 lines a unit,
# callbacks into build.py included), but fixed.
LINES_PER_UNIT = 50

SSN = b"123-45-6789"
CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
CONTENT = b"BT /F1 9 Tf 9 9 Td (SSN " + SSN + b") Tj ET"


def _inventory(data: bytes):  # type: ignore[no-untyped-def]
    return build_inventory(data, budget=Budget(file_size=len(data)))


def _rules(data: bytes) -> set[PageTreeRule]:
    return {PageTreeRule(dict(f.params)["rule"]) for f in _inventory(data).flags
            if f.reason is FlagReason.PAGE_TREE}


def _tree(objects: dict[int, bytes], streams: dict[int, bytes] | None = None,
          root: bytes = CATALOG) -> bytes:
    """Catalog 1 (*root*) and *objects*; *streams* as stream objects."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, root), 0)
    for n, body in objects.items():
        table[n] = (1, w.obj(n, body), 0)
    for n, data in (streams or {}).items():
        table[n] = (1, w.stream(n, b"", data), 0)
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R" % (max(table) + 1)))
    return bytes(w.out)


PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] /Resources << >> >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /Contents 9 0 R >>"


def _one(page: bytes = PAGE, pages: bytes = PAGES, **more: bytes) -> bytes:
    return _tree({2: pages, 3: page} | {int(k[1:]): v for k, v in more.items()},
                 {9: CONTENT})


def test_a_canonical_tree_reads_without_a_flag() -> None:
    inv = _inventory(_one())
    assert inv.flags == ()
    pages = inv.pages[0]
    assert pages is not None and [p.number for p in pages] == [3]
    assert {k: a.source for k, a in pages[0].attributes.items()} == {
        b"MediaBox": 2, b"Resources": 2}


# ── Every rule ─────────────────────────────────────────────────────────────
R = PageTreeRule
CASES = [
    # The root.
    (_tree({2: PAGES.replace(b"/Type /Pages", b"/Type /Page"), 3: PAGE}, {9: CONTENT}), R.ROOT),
    (_tree({2: PAGES + b"", 3: PAGE}, {9: CONTENT},
           root=b"<< /Type /Catalog /Pages " + PAGES + b" >>"), R.ROOT),
    (_one(pages=PAGES.replace(b"/Count 1", b"/Count 1 /Parent 4 0 R"),
          n4=b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>"), R.ROOT),
    # Kids.
    (_one(pages=PAGES.replace(b"[3 0 R]", b"[3 0 R null]")), R.KID),
    (_one(pages=PAGES.replace(b"[3 0 R]", b"[3 0 R 7 0 R]").replace(b"/Count 1", b"/Count 2")),
     R.KID),
    (_one(pages=PAGES.replace(b"[3 0 R]", b"[3 0 R 3 0 R]").replace(b"/Count 1", b"/Count 2")),
     R.REACHED_TWICE),
    # Node types, counts, parents.
    (_one(page=PAGE.replace(b"/Type /Page", b"/Type /Pagex")), R.NODE_TYPE),
    (_one(pages=PAGES.replace(b"/Count 1", b"/Count 2")), R.COUNT),
    (_one(pages=PAGES.replace(b"/Count 1", b"/Count 1.0")), R.COUNT),
    (_one(page=PAGE.replace(b"/Parent 2 0 R", b"/Parent 1 0 R")), R.PARENT),
    (_one(page=PAGE.replace(b"/Parent 2 0 R ", b"")), R.PARENT),
    # Attributes.
    (_one(pages=PAGES.replace(b" /Resources << >>", b"")), R.ATTRIBUTE_MISSING),
    (_one(pages=PAGES.replace(b" /MediaBox [0 0 200 200]", b"")), R.ATTRIBUTE_MISSING),
    (_one(pages=PAGES.replace(b"/Resources << >>", b"/Resources 8 0 R"), n8=b"null"),
     R.ATTRIBUTE_NULL),
    (_one(pages=PAGES.replace(b"/Resources << >>", b"/Resources [1]")), R.ATTRIBUTE_INVALID),
    (_one(pages=PAGES.replace(b"[0 0 200 200]", b"[0 0 200 200 9]")), R.ATTRIBUTE_INVALID),
    (_one(pages=PAGES.replace(b"[0 0 200 200]", b"[0 0 200 0]")), R.ATTRIBUTE_INVALID),
    (_one(page=PAGE.replace(b"/Contents", b"/Rotate 45 /Contents")), R.ATTRIBUTE_INVALID),
    (_one(page=PAGE.replace(b"/Contents", b"/TrimBox [0 0 0 0] /Contents")),
     R.ATTRIBUTE_INVALID),
    (_one(page=PAGE.replace(b"/Contents", b"/CropBox [300 300 400 400] /Contents")), R.CROPBOX),
    (_one(pages=PAGES.replace(b"/Count 1", b"/Count 1 /TrimBox [0 0 9 9]")), R.NODE_BOX),
    (_one(page=PAGE.replace(b"/Contents 9 0 R", b"/Contents (x)")), R.CONTENTS),
    (_one(page=PAGE.replace(b"/Contents 9 0 R", b"/Contents [9 0 R 3 0 R]")), R.CONTENTS),
    (_tree({2: b"<< /Type /Pages /Kids [] /Count 0 >>"}), R.NO_PAGES),
]
# What MuPDF shows differently from what qpdf keeps (found in review): boxes
# in 32-bit floats; a media box, or its intersection with the crop box,
# under one unit (MuPDF: a 1x1 page) or empty (612x792); /UserUnit.
SHOWN = [
    (_one(pages=PAGES.replace(b"[0 0 200 200]", b"[100.00000001 0 100.00000002 200]")),
     R.ATTRIBUTE_INVALID),
    (_one(pages=PAGES.replace(b"[0 0 200 200]", b"[0 0 0.99 200]")), R.ATTRIBUTE_INVALID),
    (_one(page=PAGE.replace(b"/Contents", b"/CropBox [199.5 0 400 200] /Contents")),
     R.CROPBOX),
    (_one(pages=PAGES.replace(b"[0 0 200 200]", b"[100 0 200 200]"),
          page=PAGE.replace(b"/Contents", b"/CropBox [0 0 100.000001 200] /Contents")),
     R.CROPBOX),
    (_one(page=PAGE.replace(b"/Contents", b"/UserUnit 10 /Contents")), R.ATTRIBUTE_INVALID),
]
CASES += SHOWN


@pytest.mark.parametrize(("data", "rule"), CASES, ids=[f"{c[1].name}-{i}" for i, c in
                                                        enumerate(CASES)])
def test_a_tree_off_the_canonical_shape_is_flagged(data: bytes, rule: PageTreeRule) -> None:
    assert rule in _rules(data)


def test_a_deep_tree_is_flagged_past_100_levels() -> None:
    objects: dict[int, bytes] = {}
    levels = 102
    for i in range(levels):  # node 2 + i lists node 3 + i; the last lists the page
        parent = b" /Parent %d 0 R" % (1 + i) if i else b""
        objects[2 + i] = (b"<< /Type /Pages /Kids [%d 0 R] /Count 1%s"
                          b" /MediaBox [0 0 9 9] /Resources << >> >>" % (3 + i, parent))
    objects[2 + levels] = b"<< /Type /Page /Parent %d 0 R >>" % (1 + levels)
    assert R.DEPTH in _rules(_tree(objects))


# ── The readers agree on every canonical tree ─────────────────────────────
def _nested() -> bytes:
    """Two levels, inherited and overridden attributes, an indirect
    /Resources and /Kids, a /Contents array, /CropBox and /Rotate."""
    return _tree({
        2: b"<< /Type /Pages /Kids 7 0 R /Count 3 /MediaBox [0 0 300 300]"
           b" /Resources 8 0 R /Rotate 90 >>",
        7: b"[3 0 R 4 0 R]",
        8: b"<< /ProcSet [/PDF /Text] >>",
        3: b"<< /Type /Page /Parent 2 0 R /Contents [9 0 R 10 0 R] >>",
        4: b"<< /Type /Pages /Parent 2 0 R /Kids [5 0 R 6 0 R] /Count 2"
           b" /CropBox [10 10 290 290] >>",
        5: b"<< /Type /Page /Parent 4 0 R /Rotate 180 /Resources << /ProcSet [/PDF] >>"
           b" /Contents 9 0 R >>",
        6: b"<< /Type /Page /Parent 4 0 R /MediaBox [0 0 100.5 200] >>",
    }, {9: CONTENT, 10: b"BT ET"})


def test_a_nested_tree_inherits_nearest_first() -> None:
    inv = _inventory(_nested())
    assert inv.flags == ()
    pages = inv.pages[0]
    assert pages is not None and [p.number for p in pages] == [3, 5, 6]
    assert [{k: a.source for k, a in p.attributes.items()} for p in pages] == [
        {b"MediaBox": 2, b"Resources": 2, b"Rotate": 2},
        {b"MediaBox": 2, b"Resources": 5, b"Rotate": 5, b"CropBox": 4},
        {b"MediaBox": 6, b"Resources": 2, b"Rotate": 2, b"CropBox": 4}]


@requires_qpdf
@pytest.mark.parametrize("build", [_one, _nested], ids=["one", "nested"])
def test_a_canonical_tree_agrees_with_the_readers(tmp_path: Path, build) -> None:  # type: ignore[no-untyped-def]
    found = compare(build(), tmp_path)
    assert found.agrees, found


# ── Revisions ─────────────────────────────────────────────────────────────
def _updated(update: dict[int, bytes]) -> bytes:
    base = _one()
    w = Writer()
    w.out = bytearray(base)
    first = int(base.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    newer = {n: (1, w.obj(n, body), 0) for n, body in update.items()}
    size = max(9, *update) + 1
    w.epilogue(w.table(newer, b"/Size %d /Root 1 0 R /Prev %d" % (size, first)))
    return bytes(w.out)


def test_an_update_away_from_the_tree_reuses_the_older_page_list() -> None:
    inv = _inventory(_updated({11: b"(unrelated)"}))
    assert inv.flags == () and inv.pages[0] is inv.pages[1]


def test_an_update_breaking_the_tree_flags_that_revision() -> None:
    inv = _inventory(_updated({3: PAGE.replace(b"/Parent 2 0 R", b"/Parent 1 0 R")}))
    found = [dict(f.params) for f in inv.flags if f.reason is FlagReason.PAGE_TREE]
    assert found == [{"rule": int(R.PARENT), "object": 3, "revision": 0}]
    assert inv.pages[1] is not None and inv.pages[0] is None


@requires_qpdf
def test_an_updated_page_agrees_with_the_readers(tmp_path: Path) -> None:
    data = _updated({3: PAGE.replace(b"/Contents", b"/Rotate 270 /Contents")})
    found = compare(data, tmp_path)
    assert found.agrees, found


# ── Linearity: wide and deep trees over many revisions ────────────────────
def _wide(pages: int, updates: int) -> bytes:
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, CATALOG), 0)
    kids = b" ".join(b"%d 0 R" % (10 + i) for i in range(pages))
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [%s] /Count %d /MediaBox [0 0 9 9]"
                          b" /Resources << >> >>" % (kids, pages)), 0)
    for i in range(pages):
        table[10 + i] = (1, w.obj(10 + i, b"<< /Type /Page /Parent 2 0 R >>"), 0)
    size = 10 + pages
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for i in range(updates):  # each update rewrites one page: the tree is read again
        at = w.obj(10 + i % pages, b"<< /Type /Page /Parent 2 0 R /Rotate 90 >>")
        xref = w.table({10 + i % pages: (1, at, 0)},
                       b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(xref)
        prev = xref
    return bytes(w.out)


def _timed(data: bytes) -> tuple[float, int]:
    """(process time, work charged) of building *data*'s inventory."""
    budget = Budget(file_size=len(data))
    started = time.process_time()
    build_inventory(data, budget=budget)
    return time.process_time() - started, budget.work


def test_rereading_a_wide_tree_is_charged_and_bounded() -> None:
    # Each update that touches the tree re-reads it: revisions x pages, by
    # design (ADR 0011 item 7). All of it is charged -- the work grows as
    # its product -- and the budget bounds it (flagged, never hanging).
    # No hidden work: the lines the inventory executes per unit of work the
    # walk charges stay under a fixed bound (a deterministic count: #94);
    # (work done inside C, which no line count sees: the test below).
    small = _wide(40, 40)
    large = _wide(320, 320)
    work = []
    for data in (small, large):
        charged, lines, inv = charged_steps(data, "build_pages")
        work.append(charged)
        assert lines <= LINES_PER_UNIT * charged, (lines, charged)
        reasons = {f.reason for f in inv.flags}
        assert reasons <= {FlagReason.BUDGET_EXHAUSTED}, reasons
    w_s, w_l = work
    # Superlinear by design (8x the input, revisions x pages: ~64x the walk).
    assert w_l >= 16 * w_s or w_l >= 0.99 * Limits().work_limit(len(large)), (w_s, w_l)


def test_rereading_a_wide_tree_costs_its_charged_work_in_wall_clock_too() -> None:
    # The backstop for work done inside C (a linear `in` on a list, a copy:
    # review of #113): wall-clock time per charged unit does not grow with
    # the input. Main's bound and sizes; measured 2.0x, and 3.6x with
    # `list(self.kept)` scanned per page. Best of two: a loaded machine
    # only slows a run.
    per_unit = []
    for data in (_wide(100, 100), _wide(800, 800)):
        runs = [_timed(data) for _ in range(2)]
        per_unit.append(max(min(t for t, _ in runs), 0.02) / runs[0][1])
    assert per_unit[1] < 3 * per_unit[0], per_unit


@requires_qpdf
@pytest.mark.parametrize(("data", "rule"), SHOWN, ids=[r.name for _, r in SHOWN])
def test_what_mupdf_shows_differently_is_seen_by_the_gate_too(
        tmp_path: Path, data: bytes, rule: PageTreeRule) -> None:
    # Flagged; and were it not, the gate's comparison of the page as MuPDF
    # shows it would disagree (not only the inventory's rule sees it).
    inv = _inventory(data)
    assert rule in _rules(data)
    stripped = replace(inv, flags=(), pages=tuple(_unflagged(data, rev)
                                                  for rev in range(inv.revisions)))
    found = compare(data, tmp_path, inv=stripped)
    assert found.disagrees, found


def _unflagged(data: bytes, revision: int):  # type: ignore[no-untyped-def]
    """The page list a walk that ignores its own flags would return."""
    from redaction_verifier.inventory import pages as module
    real = module._Walk.flag
    try:
        module._Walk.flag = lambda self, rule, number: None  # type: ignore[method-assign]
        return _inventory(data).pages[revision]
    finally:
        module._Walk.flag = real  # type: ignore[method-assign]


@requires_qpdf
def test_a_hundred_levels_agree_and_a_hundred_and_one_are_flagged(tmp_path: Path) -> None:
    def chain(levels: int) -> bytes:
        objects: dict[int, bytes] = {}
        for i in range(levels):
            parent = b" /Parent %d 0 R" % (1 + i) if i else b""
            objects[2 + i] = (b"<< /Type /Pages /Kids [%d 0 R] /Count 1%s"
                              b" /MediaBox [0 0 9 9] /Resources << >> >>" % (3 + i, parent))
        objects[2 + levels] = b"<< /Type /Page /Parent %d 0 R >>" % (1 + levels)
        return _tree(objects)
    assert _inventory(chain(100)).flags == ()
    assert compare(chain(100), tmp_path).agrees
    assert R.DEPTH in _rules(chain(101))


@requires_qpdf
def test_indirect_type_and_count_agree_with_the_readers(tmp_path: Path) -> None:
    data = _one(pages=PAGES.replace(b"/Type /Pages", b"/Type 5 0 R").replace(
        b"/Count 1", b"/Count 6 0 R"), n5=b"/Pages", n6=b"1")
    assert _inventory(data).flags == ()
    found = compare(data, tmp_path)
    assert found.agrees, found


@requires_qpdf
def test_an_encrypted_tree_with_direct_resources_agrees(tmp_path: Path) -> None:
    import pikepdf
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(_nested())
    with pikepdf.open(plain) as pdf:
        pdf.save(tmp_path / "enc.pdf", encryption=pikepdf.Encryption(owner="o", user="", R=6),
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    data = (tmp_path / "enc.pdf").read_bytes()
    assert _inventory(data).flags == ()
    found = compare(data, tmp_path)
    assert found.agrees and found.encrypted, found


def test_an_unchanged_page_is_kept_once_across_revisions() -> None:
    data = _wide(50, 50)
    inv = _inventory(data)
    assert inv.flags == ()
    distinct = {id(p) for pages in inv.pages if pages for p in pages}
    assert len(distinct) <= 50 + 50  # each page once, plus each rewrite once


def _ancestor(pages: int, updates: int, vary: bool = False) -> bytes:
    """The review's shape: *pages* under a root holding the inherited
    /MediaBox and /Resources, /Kids an indirect array that never changes;
    each update rewrites only the root (~150 bytes) -- with a new
    /MediaBox each time when *vary*, so every page below reads anew."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    root = (b"<< /Type /Pages /Kids 3 0 R /Count %d /MediaBox [0 0 9 9] /Resources << >> >>"
            % pages)
    table[1] = (1, w.obj(1, CATALOG), 0)
    table[2] = (1, w.obj(2, root), 0)
    table[3] = (1, w.obj(3, b"[" + b" ".join(b"%d 0 R" % (10 + i) for i in range(pages))
                          + b"]"), 0)
    for i in range(pages):
        table[10 + i] = (1, w.obj(10 + i, b"<< /Type /Page /Parent 2 0 R >>"), 0)
    size = 10 + pages
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for i in range(updates):
        again = root.replace(b"[0 0 9 9]", b"[0 0 9 %d]" % (10 + i)) if vary else root
        xref = w.table({2: (1, w.obj(2, again), 0)},
                       b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(xref)
        prev = xref
    return bytes(w.out)


def test_rewriting_an_ancestor_pays_for_every_page_it_makes_new() -> None:
    # Page records are retained across revisions: each new one is charged
    # (pages.PAGE_RECORD_WORK), so memory stays within the work budget --
    # the file is flagged BUDGET_EXHAUSTED long before it grows (review).
    from redaction_verifier.inventory.pages import PAGE_RECORD_WORK
    data = _ancestor(1000, 1000, vary=True)
    budget = Budget(file_size=len(data))
    inv = build_inventory(data, budget=budget)
    kept = {id(p) for pages in inv.pages if pages for p in pages}
    assert len(kept) * PAGE_RECORD_WORK <= Limits().work_limit(len(data))
    assert {f.reason for f in inv.flags} == {FlagReason.BUDGET_EXHAUSTED}
    small = _ancestor(20, 20, vary=True)  # the same shape, small: within the budget
    assert _inventory(small).flags == ()


def test_rewriting_an_ancestor_whose_attributes_read_the_same_keeps_every_page() -> None:
    # A page insertion rewrites the root's /Kids and /Count; its /MediaBox
    # and /Resources read the same: no page record is made again (review).
    data = _ancestor(1000, 60)
    inv = _inventory(data)
    assert inv.flags == ()
    assert len({id(p) for pages in inv.pages if pages for p in pages}) == 1000


def test_equal_attribute_bytes_in_another_place_make_a_new_record() -> None:
    # Review: under encryption equal bytes read differently in an object
    # stream (plaintext) than in the file, or at another generation (the
    # key decrypts them). Moving the attribute-holding root into an object
    # stream with identical bytes makes each page's record again.
    w = Writer()
    root = b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 9 9] /Resources << >> >>"
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, CATALOG), 0)
    table[2] = (1, w.obj(2, root), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R >>"), 0)
    first = w.xref_stream(4, table, b"/Size 5 /Root 1 0 R")
    w.epilogue(first)
    newer = {5: (1, w.objstm(5, [(2, root)]), 0), 2: (2, 5, 0)}
    w.epilogue(w.xref_stream(6, newer, b"/Size 7 /Root 1 0 R /Prev %d" % first))
    inv = _inventory(bytes(w.out))
    assert inv.flags == ()
    old, new = inv.pages[1], inv.pages[0]
    assert old is not None and new is not None and old[0] is not new[0]
