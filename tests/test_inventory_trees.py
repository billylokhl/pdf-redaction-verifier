"""Name and number trees, and resource scopes (Phase 3a-7c, ADR 0011
items 3 and 4 with the 2026-10-11 amendment): only canonical trees read
alike in both readers; a form, tiling pattern, Type3 font or appearance
stream must carry its own /Resources. Anything else is flagged; an
unflagged /EmbeddedFiles tree agrees with MuPDF and libqpdf. Every file
is generated here; fabricated content only."""

from __future__ import annotations

from pathlib import Path

import pytest
from scorecard.inventory import compare
from scorecard.pdfgen import Writer

from redaction_verifier.budget import Budget
from redaction_verifier.inventory import build_inventory
from redaction_verifier.inventory.objects import PdfRef
from redaction_verifier.inventory.trees import NameTreeRule
from redaction_verifier.ledger import FlagReason

from .conftest import requires_qpdf

SSN = b"123-45-6789"
PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] /Resources << >> >>"
PAGE = b"<< /Type /Page /Parent 2 0 R >>"


def _inventory(data: bytes):  # type: ignore[no-untyped-def]
    return build_inventory(data, budget=Budget(file_size=len(data)))


def _file(catalog: bytes = b"", objects: dict[int, bytes] | None = None,
          streams: dict[int, tuple[bytes, bytes]] | None = None,
          page: bytes = PAGE, pages: bytes = PAGES) -> bytes:
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R%s >>" % catalog), 0)
    table[2] = (1, w.obj(2, pages), 0)
    table[3] = (1, w.obj(3, page), 0)
    for n, body in (objects or {}).items():
        table[n] = (1, w.obj(n, body), 0)
    for n, (extra, data) in (streams or {}).items():
        table[n] = (1, w.stream(n, extra, data), 0)
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R" % (max(table) + 1)))
    return bytes(w.out)


def _rules(data: bytes, reason: FlagReason = FlagReason.NAME_TREE) -> set[int]:
    return {dict(f.params).get("rule", 0) for f in _inventory(data).flags if f.reason is reason}


def _files(*keys: bytes) -> bytes:
    """An /EmbeddedFiles tree: a root with /Kids, one leaf with *keys*."""
    pairs = b" ".join(b"%s %d 0 R" % (k, 10 + i) for i, k in enumerate(keys))
    objects = {4: b"<< /Kids [5 0 R] >>",
               5: b"<< /Limits [%s %s] /Names [%s] >>" % (keys[0], keys[-1], pairs)}
    for i in range(len(keys)):
        objects[10 + i] = b"<< /Type /Filespec /F (f%d.txt) >>" % i
    return _file(b" /Names << /EmbeddedFiles 4 0 R >>", objects)


# ── Canonical trees agree ──────────────────────────────────────────────────
def test_a_canonical_tree_reads_without_a_flag() -> None:
    inv = _inventory(_files(b"(a.txt)", b"(b.txt)", b"(c.txt)"))
    assert inv.flags == ()
    trees = inv.trees[0]
    assert trees is not None and [e.key for e in trees[b"EmbeddedFiles"]] == [
        b"a.txt", b"b.txt", b"c.txt"]


@requires_qpdf
@pytest.mark.parametrize("data", [
    _files(b"(a.txt)", b"(b.txt)", b"(c.txt)"),
    _files(b"<FEFF00410042>", b"<FEFF00430044>"),   # UTF-16BE keys, in order as text too
    _file(b" /Names << /EmbeddedFiles 4 0 R >>", {4: b"<< >>"}),          # empty roots (R5)
    _file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Names [] >>"}),
    _file(b" /PageLabels 4 0 R", {4: b"<< >>"}),
    _file(b" /PageLabels << /Nums [0 << /S /D >>] >>"),
], ids=["files", "utf16", "empty-files", "empty-dests", "empty-labels", "labels"])
def test_a_canonical_tree_agrees_with_the_readers(tmp_path: Path, data: bytes) -> None:
    assert _inventory(data).flags == ()
    found = compare(data, tmp_path)
    assert found.agrees, found


# ── Everything else is flagged ────────────────────────────────────────────
R = NameTreeRule
CASES = [
    (_file(b" /Names << /Dests [] >>"), R.NODE),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Names [(a) 1] /Kids [] >>"}), R.NODE),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Names [(a)] >>"}), R.ENTRIES),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Names [/a 1] >>"}), R.ENTRIES),
    (_file(b" /PageLabels 4 0 R", {4: b"<< /Nums [(0) << >>] >>"}), R.ENTRIES),
    (_files(b"(b)", b"(a)"), R.ORDER),
    (_files(b"(a)", b"(a)"), R.ORDER),
    (_file(b" /PageLabels << /Nums [3 << >> 1 << >>] >>"), R.ORDER),
    (_files(b"(a)", b"<FEFF0061>"), R.TEXT),          # P3: one name, two byte strings
    (_files(b"(a)", b"(\x80)"), R.TEXT),              # bytes the readers map differently
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Kids [5 0 R] >>", 5: b"(x)"}), R.NODE),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Kids [<< /Names [(a) 1] >>] >>"}), R.KIDS),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Kids [4 0 R] >>"}), R.REACHED_TWICE),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Kids [5 0 R] >>",
                                             5: b"<< /Names [(a) 1 (b) 2] /Limits [(a) (c)] >>"}),
     R.LIMITS),
    (_file(b" /Names << /Dests 4 0 R >>", {4: b"<< /Kids [5 0 R] >>",
                                             5: b"<< /Names [(a) 1] >>"}), R.LIMITS),
    (_file(b" /Names 4 0 R", {4: b"[1]"}), R.NAMES),
]


@pytest.mark.parametrize(("data", "rule"), CASES,
                         ids=[f"{r.name}-{i}" for i, (_, r) in enumerate(CASES)])
def test_a_tree_off_the_canonical_shape_is_flagged(data: bytes, rule: NameTreeRule) -> None:
    assert int(rule) in _rules(data)


@requires_qpdf
def test_a_non_empty_tree_in_an_encrypted_revision_is_flagged(tmp_path: Path) -> None:
    import pikepdf
    plain = tmp_path / "plain.pdf"
    plain.write_bytes(_files(b"(a.txt)", b"(b.txt)"))
    with pikepdf.open(plain) as pdf:
        pdf.save(tmp_path / "enc.pdf", encryption=pikepdf.Encryption(owner="o", user="", R=6),
                 object_stream_mode=pikepdf.ObjectStreamMode.disable)
    assert int(R.ENCRYPTED) in _rules((tmp_path / "enc.pdf").read_bytes())


def test_a_wide_tree_stays_linear() -> None:
    import time

    def wide(leaves: int) -> bytes:
        objects: dict[int, bytes] = {}
        kids = []
        for i in range(leaves):
            key = b"(k%08d)" % i
            objects[10 + i] = b"<< /Limits [%s %s] /Names [%s 1] >>" % (key, key, key)
            kids.append(b"%d 0 R" % (10 + i))
        objects[4] = b"<< /Kids [" + b" ".join(kids) + b"] >>"
        return _file(b" /Names << /Dests 4 0 R >>", objects)
    times = []
    for n in (2000, 16000):
        data = wide(n)
        budget = Budget(file_size=len(data))
        started = time.process_time()
        assert build_inventory(data, budget=budget).flags == ()
        times.append((time.process_time() - started, budget.work))
    (t_s, w_s), (t_l, w_l) = times
    # TODO(#113): the wall-clock half was dropped (it flaked under CI load,
    # as #94's did); restore it as steps.py's line events per unit charged.
    assert w_l < 10 * w_s, times


# ── Resource scopes (ADR 0011 item 3) ─────────────────────────────────────
FORM = b" /Subtype /Form /BBox [0 0 9 9]"
DRAW = b"0 0 m 9 9 l S"


def _drawing(form: bytes, extra: dict[int, bytes] | None = None,
             resources: bytes = b"<< /XObject << /F 4 0 R >> >>") -> bytes:
    return _file(objects=extra, streams={4: (form, DRAW)},
                 page=b"<< /Type /Page /Parent 2 0 R /Resources %s >>" % resources)


@pytest.mark.parametrize(("data", "flagged"), [
    (_drawing(FORM), True),                                        # by type
    (_drawing(FORM + b" /Resources << >>"), False),
    (_drawing(FORM + b" /Resources 5 0 R", {5: b"<< >>"}), False),
    (_drawing(FORM + b" /Resources 5 0 R", {5: b"[1]"}), True),     # not a dictionary
    (_drawing(b" /BBox [0 0 9 9]"), True),                         # by role: an XObject
    (_drawing(b" /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8"
              b" /ColorSpace /DeviceGray"), False),                # an image needs none
    (_drawing(b" /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 9 9] /XStep 9 /YStep 9",
              resources=b"<< /Pattern << /P 4 0 R >> >>"), True),
], ids=["form", "form-res", "form-res-ref", "form-res-array", "role", "image", "pattern"])
def test_a_drawing_stream_must_carry_its_own_resources(data: bytes, flagged: bool) -> None:
    found = FlagReason.RESOURCE_SCOPE in {f.reason for f in _inventory(data).flags}
    assert found is flagged


def test_an_appearance_and_a_type3_font_must_carry_their_own_resources() -> None:
    annot = _file(objects={5: b"<< /Type /Annot /Subtype /Square /Rect [0 0 9 9]"
                              b" /AP << /N 4 0 R >> >>"},
                  streams={4: (b" /BBox [0 0 9 9]", DRAW)},
                  page=b"<< /Type /Page /Parent 2 0 R /Annots [5 0 R] >>")
    assert FlagReason.RESOURCE_SCOPE in {f.reason for f in _inventory(annot).flags}
    type3 = _file(objects={5: b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1 1]"
                              b" /FontMatrix [1 0 0 1 0 0] /CharProcs << >> /Encoding << >>"
                              b" /FirstChar 0 /LastChar 0 /Widths [0] >>"},
                  page=b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /T 5 0 R >> >> >>")
    assert FlagReason.RESOURCE_SCOPE in {f.reason for f in _inventory(type3).flags}


@requires_qpdf
def test_a_form_with_its_own_resources_agrees(tmp_path: Path) -> None:
    data = _drawing(FORM + b" /Resources 5 0 R", {5: b"<< /ProcSet [/PDF] >>"})
    inv = _inventory(data)
    assert inv.flags == () and list(inv.graph.scopes.values())
    found = compare(data, tmp_path)
    assert found.agrees, found


# ── Roles: every hop direct or by reference ───────────────────────────────
PLAIN = b" /BBox [0 0 9 9]"  # a stream with no type: only its role obliges it


def _scoped(data: bytes) -> bool:
    return FlagReason.RESOURCE_SCOPE in {f.reason for f in _inventory(data).flags}


def _page(extra: bytes) -> bytes:
    return b"<< /Type /Page /Parent 2 0 R%s >>" % extra


@pytest.mark.parametrize(("data", "flagged"), [
    (_file(objects={6: b"<< /XObject << /F 4 0 R >> >>"}, streams={4: (PLAIN, DRAW)},
           page=_page(b" /Resources 6 0 R")), True),
    (_file(objects={6: b"<< /F 4 0 R >>"}, streams={4: (PLAIN, DRAW)},
           page=_page(b" /Resources << /XObject 6 0 R >>")), True),
    (_file(objects={6: b"<< /XObject 7 0 R >>", 7: b"<< /F 4 0 R >>"},
           streams={4: (PLAIN, DRAW)}, page=_page(b" /Resources 6 0 R")), True),
    (_file(objects={6: b"<< /Pattern 7 0 R >>", 7: b"<< /P 4 0 R >>"},
           streams={4: (PLAIN, DRAW)}, page=_page(b" /Resources 6 0 R")), True),
    (_file(b" /AcroForm << /Fields [] /DR << /XObject << /F 4 0 R >> >> >>",
           streams={4: (PLAIN, DRAW)}), True),
    (_file(objects={6: b"<< /N 4 0 R >>", 5: b"<< /Type /Annot /Subtype /Square"
                    b" /Rect [0 0 9 9] /AP 6 0 R >>"},
           streams={4: (PLAIN, DRAW)}, page=_page(b" /Annots [5 0 R]")), True),
    (_file(objects={6: b"<< /On 4 0 R >>", 5: b"<< /Type /Annot /Subtype /Square"
                    b" /Rect [0 0 9 9] /AP << /N 6 0 R >> /AS /On >>"},
           streams={4: (PLAIN, DRAW)}, page=_page(b" /Annots [5 0 R]")), True),
    (_file(streams={4: (PLAIN, DRAW)},  # a direct annotation in /Annots
           page=_page(b" /Annots [<< /Type /Annot /Subtype /Square /Rect [0 0 9 9]"
                      b" /AP << /N << /On 4 0 R >> >> /AS /On >>]")), True),
    (_file(streams={4: (PLAIN, DRAW)},  # a resource named like a holder key
           page=_page(b" /Resources << /XObject << /AP 4 0 R >> >>")), True),
    (_file(objects={6: b"/Form"}, streams={4: (b" /Subtype 6 0 R" + PLAIN, DRAW)}), True),
    (_file(objects={6: b"6"}, streams={4: (b" /Subtype 6 0 R" + PLAIN, DRAW)}), True),
    (_file(objects={6: b"1"}, streams={4: (b" /PatternType 6 0 R" + PLAIN, DRAW)}), True),
    (_file(objects={6: b"/Image"},
           streams={4: (b" /Subtype 6 0 R /Width 1 /Height 1 /BitsPerComponent 8"
                        b" /ColorSpace /DeviceGray", b"\x00")},
           page=_page(b" /Resources << /XObject << /I 4 0 R >> >>")), False),
    (_file(objects={6: b"<< /Type /FontDescriptor /FontFile2 4 0 R >>"},
           streams={4: (b"", b"font")}), False),         # a font program: no role, no type
    (_file(streams={4: (PLAIN, DRAW)}), False),          # referenced by nothing, no type
    (_file(objects={6: b"<< /F 4 0 R >>"}, streams={4: (PLAIN, DRAW)}), False),
], ids=["resources-ref", "category-ref", "both-ref", "pattern-ref", "acroform-dr", "ap-ref",
        "ap-state-ref", "direct-annot", "holder-named", "subtype-ref", "subtype-unreadable",
        "patterntype-ref", "image-subtype-ref", "font-program", "unreferenced",
        "dictionary-no-role"])
def test_roles_are_found_through_every_hop(data: bytes, flagged: bool) -> None:
    assert _scoped(data) is flagged


def test_a_scope_records_its_innermost_dictionary_as_written() -> None:
    inv = _inventory(_drawing(FORM + b" /Resources 5 0 R", {5: b"<< >>"}))
    assert inv.flags == ()
    [(unit, scope)] = inv.graph.scopes.items()
    assert unit.obj == 4 and isinstance(scope, PdfRef) and scope.num == 5


def _update(base: bytes, update: dict[int, bytes],
            streams: dict[int, tuple[bytes, bytes]] | None = None) -> bytes:
    w = Writer()
    w.out = bytearray(base)
    first = int(base.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    newer = {n: (1, w.obj(n, body), 0) for n, body in update.items()}
    for n, (extra, data) in (streams or {}).items():
        newer[n] = (1, w.stream(n, extra, data), 0)
    older = int(base.rsplit(b"/Size ", 1)[1].split(b" ")[0])
    size = max(older - 1, *newer) + 1
    w.epilogue(w.table(newer, b"/Size %d /Root 1 0 R /Prev %d" % (size, first)))
    return bytes(w.out)


def _scope_flags(data: bytes) -> list[dict[str, int]]:
    return [dict(f.params) for f in _inventory(data).flags
            if f.reason is FlagReason.RESOURCE_SCOPE]


def test_an_update_breaking_a_scopes_target_flags_its_unchanged_holder() -> None:
    base = _drawing(FORM + b" /Resources 5 0 R", {5: b"<< >>"})
    assert _scope_flags(base) == []
    assert _scope_flags(_update(base, {5: b"[1]"})) == [{"object": 4, "revision": 0}]


def test_an_update_giving_a_role_flags_the_stream_it_reaches() -> None:
    # Object 6 is a dictionary nothing uses; the update makes it the page's
    # /XObject dictionary: 6 becomes a category, 4 an XObject, in revision 0.
    base = _file(objects={6: b"<< /F 4 0 R >>"}, streams={4: (PLAIN, DRAW)})
    assert _scope_flags(base) == []
    updated = _update(base, {3: _page(b" /Resources << /XObject 6 0 R >>")})
    assert _scope_flags(updated) == [{"object": 4, "revision": 0}]


def test_an_update_taking_a_role_away_leaves_the_older_flag() -> None:
    base = _file(streams={4: (PLAIN, DRAW)}, page=_page(b" /Resources << /XObject << /F 4 0 R"
                                                           b" >> >>"))
    flags = _scope_flags(_update(base, {3: _page(b" /Resources << >>")}))
    assert flags == [{"object": 4, "revision": 1}]


# ── More tree shapes ──────────────────────────────────────────────────────
def test_a_node_two_trees_share_is_reached_twice() -> None:
    data = _file(b" /Names << /Dests 4 0 R /EmbeddedFiles 4 0 R >>", {4: b"<< /Names [(a) 1] >>"})
    assert int(R.REACHED_TWICE) in _rules(data)


def test_an_update_away_from_the_trees_reuses_the_older_result() -> None:
    base = _files(b"(a.txt)", b"(b.txt)")
    inv = _inventory(_update(base, {9: b"(unrelated)"}))
    assert inv.flags == () and inv.trees[0] is inv.trees[1]


def test_an_update_breaking_a_tree_flags_that_revision() -> None:
    base = _files(b"(a.txt)", b"(b.txt)")
    inv = _inventory(_update(base, {5: b"<< /Limits [(a.txt) (b.txt)] /Names [(b.txt) 10 0 R"
                                       b" (a.txt) 11 0 R] >>"}))
    found = [dict(f.params) for f in inv.flags if f.reason is FlagReason.NAME_TREE]
    assert {"rule": int(R.ORDER), "object": 4, "revision": 0} in found
    assert {f["revision"] for f in found} == {0}
    assert inv.trees[1] is not None and inv.trees[0] is None


# ── Linearity (ADR 0010: work counts, n vs 8n) ────────────────────────────
def _measure(make, n: int):  # type: ignore[no-untyped-def]
    import time
    out = []
    for size in (n, 8 * n):
        data = make(size)
        budget = Budget(file_size=len(data))
        started = time.process_time()
        inv = build_inventory(data, budget=budget)
        out.append((time.process_time() - started, budget.work, inv.flags))
    return out


def _deep(levels: int) -> bytes:
    """A caterpillar: node i lists a leaf and node i+1, so every leaf
    lies below every node above it (copying a subtree's keys per level
    would be quadratic)."""
    objects: dict[int, bytes] = {}
    first, last = b"(k%08d)" % 0, b"(k%08d)" % (levels - 1)
    for i in range(levels):
        node, leaf = 10 + 2 * i, 11 + 2 * i
        key = b"(k%08d)" % i
        objects[leaf] = b"<< /Limits [%s %s] /Names [%s 1] >>" % (key, key, key)
        kids = b"%d 0 R" % leaf + (b" %d 0 R" % (node + 2) if i + 1 < levels else b"")
        objects[node] = (b"<< /Kids [%s] >>" % kids if i == 0 else
                         b"<< /Limits [%s %s] /Kids [%s] >>" % (key, last, kids))
    del first
    return _file(b" /Names << /Dests 10 0 R >>", objects)


def test_a_deep_tree_stays_linear() -> None:
    (t_s, w_s, f_s), (t_l, w_l, f_l) = _measure(_deep, 1000)
    assert f_s == () and f_l == ()
    # Work counts are the exact measure (8x the bytes, within 10x the work).
    # TODO(#113): the wall-clock half was dropped (it flaked under CI load,
    # as #94's did); restore it as steps.py's line events per unit charged.
    assert w_l < 10 * w_s, (w_s, w_l)


def test_many_trees_naming_one_node_stay_linear() -> None:
    def shared(keys: int) -> bytes:
        names = b" ".join(b"/T%d 4 0 R" % i for i in range(keys))
        leaf = b"<< /Names [" + b" ".join(b"(k%08d) 1" % i for i in range(keys)) + b"] >>"
        return _file(b" /Names << %s >>" % names, {4: leaf})
    (t_s, w_s, f_s), (t_l, w_l, f_l) = _measure(shared, 1000)
    assert FlagReason.NAME_TREE in {f.reason for f in f_l}
    # TODO(#113): the wall-clock half was dropped (it flaked under CI load,
    # as #94's did); restore it as steps.py's line events per unit charged.
    assert w_l < 10 * w_s, (w_s, w_l)


def _forms(forms: int, updates: int, toggle: bool = False) -> bytes:
    """*forms* form XObjects in one indirect /XObject dictionary (object
    6) the page's resources name; *updates* revisions each rewriting the
    page -- keeping the same resources (no role changes), or, *toggle*,
    alternately dropping and restoring them (every form loses and regains
    its role: the work is revisions x forms, bounded by the budget)."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), 0)
    table[2] = (1, w.obj(2, PAGES), 0)
    with_forms = _page(b" /Resources << /XObject 6 0 R >>")
    table[3] = (1, w.obj(3, with_forms), 0)
    table[6] = (1, w.obj(6, b"<< " + b" ".join(b"/F%d %d 0 R" % (i, 10 + i)
                                               for i in range(forms)) + b" >>"), 0)
    for i in range(forms):
        table[10 + i] = (1, w.stream(10 + i, FORM + b" /Resources << >>", DRAW), 0)
    size = 10 + forms
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for i in range(updates):
        body = _page(b"") if toggle and i % 2 == 0 else with_forms
        xref = w.table({3: (1, w.obj(3, body), 0)},
                       b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(xref)
        prev = xref
    return bytes(w.out)


def test_scopes_over_many_revisions_stay_linear() -> None:
    (t_s, w_s, f_s), (t_l, w_l, f_l) = _measure(lambda n: _forms(n, n), 300)
    assert f_s == () and f_l == ()
    # TODO(#113): the wall-clock half was dropped (it flaked under CI load,
    # as #94's did); restore it as steps.py's line events per unit charged.
    assert w_l < 10 * w_s, (w_s, w_l)


def test_toggling_a_role_is_charged_and_bounded() -> None:
    # Each toggle re-derives the category's every entry: revisions x forms,
    # by design (ADR 0011 item 7) -- all of it charged, the budget bounding
    # it: within the linear bound, or flagged BUDGET_EXHAUSTED, never more.
    from redaction_verifier.budget import Limits
    for n in (100, 800):
        data = _forms(n, n, toggle=True)
        budget = Budget(file_size=len(data))
        inv = build_inventory(data, budget=budget)
        assert budget.work <= Limits().work_limit(len(data)) + 4096
        assert {f.reason for f in inv.flags} <= {FlagReason.BUDGET_EXHAUSTED,
                                                 FlagReason.PAGE_TREE}


# ── The gate (ADR 0011 item 8) ────────────────────────────────────────────
@requires_qpdf
def test_the_gate_fails_on_embedded_files_read_differently(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Not vacuous: an unflagged reading whose entries differ from both
    # readers' is a NAME_TREE disagreement.
    import redaction_verifier.inventory.build as build
    from redaction_verifier.inventory import trees as trees_module
    from scorecard.inventory import Check

    real = trees_module.build_trees

    def misread(*args, **kwargs):  # type: ignore[no-untyped-def]
        results, flags = real(*args, **kwargs)
        return tuple({k: v[::-1] for k, v in r.items()} if r else r for r in results), flags
    monkeypatch.setattr(build, "build_trees", misread)
    found = compare(_files(b"(a.txt)", b"(b.txt)"), tmp_path)
    assert not found.agrees and found.check is Check.NAME_TREE, found


def test_the_empty_root_allowlist_is_anchored() -> None:
    from scorecard.inventory import EMPTY_TREE_ROOT
    line = (b" (Name/Number tree node (object 4)): name/number tree node has neither non-empty"
            b" /Names nor /Kids")
    assert EMPTY_TREE_ROOT.fullmatch(line)
    assert EMPTY_TREE_ROOT.fullmatch(line.replace(b" (object 4)", b""))
    assert not EMPTY_TREE_ROOT.fullmatch(line + b"; and more")
    assert not EMPTY_TREE_ROOT.fullmatch(line.replace(b"Name/Number tree node (", b"page ("))


# ── A trailing NUL in a name key (owner decision 2026-10-12, amending P3) ──
NUL_KEYS = [b"(a.txt\\000)", b"(b.txt\\000)"]


@requires_qpdf
def test_both_readers_resolve_a_key_with_a_trailing_nul_to_the_same_object(
        tmp_path: Path) -> None:
    # The condition the owner set for allowing the key: MuPDF reads it as a
    # C string (the NUL cut off), libqpdf keeps the NUL -- and each, listing
    # the tree or looking the key up as written, finds the same object.
    import pikepdf
    import pymupdf
    path = tmp_path / "nul.pdf"
    path.write_bytes(_files(*NUL_KEYS))
    mupdf = pymupdf.mupdf
    doc = pymupdf.open(path)
    pdf = pymupdf._as_pdf_document(doc)
    name = mupdf.pdf_new_name("EmbeddedFiles")
    loaded = mupdf.pdf_load_name_tree(pdf, name)
    listed = {mupdf.pdf_to_name(mupdf.pdf_dict_get_key(loaded, i)):
              mupdf.pdf_to_num(mupdf.pdf_dict_get_val(loaded, i))
              for i in range(mupdf.pdf_dict_len(loaded))}
    assert listed == {"a.txt": 10, "b.txt": 11}
    looked = {k: mupdf.pdf_to_num(mupdf.pdf_lookup_name(pdf, name, mupdf.pdf_new_string(k, len(k))))
              for k in ("a.txt\0", "b.txt\0")}
    assert looked == {"a.txt\0": 10, "b.txt\0": 11}
    with pikepdf.open(path) as qpdf:
        tree = pikepdf.NameTree(qpdf.Root.Names.EmbeddedFiles)
        assert [(k, v.objgen[0]) for k, v in tree.items()] == [("a.txt\0", 10), ("b.txt\0", 11)]
        assert {k: tree[k].objgen[0] for k in ("a.txt\0", "b.txt\0")} == looked
        assert qpdf.get_warnings() == []
    # Known asymmetry, outside the key itself: MuPDF also finds the name
    # without its NUL (its C-string compare); libqpdf does not.
    assert mupdf.pdf_to_num(mupdf.pdf_lookup_name(pdf, name, mupdf.pdf_new_string("a.txt", 5))) == 10


@requires_qpdf
def test_a_tree_with_trailing_nul_keys_agrees_with_the_readers(tmp_path: Path) -> None:
    data = _files(*NUL_KEYS)
    assert _inventory(data).flags == ()
    found = compare(data, tmp_path)
    assert found.agrees, found


@pytest.mark.parametrize("keys", [
    [b"(a)", b"(a\\000)"],            # one name once the NUL is cut: MuPDF keeps the last
    [b"(a\\000\\000)", b"(b)"],         # two NULs
    [b"(a\\000b)", b"(c)"],            # a NUL inside
    [b"(a\\001)", b"(b)"],             # another control byte at the end
], ids=["collides", "two-nuls", "inner-nul", "other-control"])
def test_other_nul_and_control_keys_stay_flagged(keys: list[bytes]) -> None:
    assert int(R.TEXT) in _rules(_files(*keys))


# ── Round-1 review findings ───────────────────────────────────────────────
@pytest.mark.parametrize("keys", [
    [b"<61AD62>", b"<61AD63>"],                 # 0xAD: MuPDF reads U+0000, qpdf does not
    [b"(a\\000)", b"<FEFF00610000>"],            # one name to libqpdf, two to MuPDF
    [b"<FEFF00610000>", b"<FEFF0062>"],          # U+0000 in a UTF-16 key
    [b"<FEFF001B4142001B0061>", b"<FEFF0062>"],  # ESC: MuPDF strips a language escape
    [b"<FEFF001B0041001B0061>", b"<FEFF0062>"],  # ... of any bytes; libqpdf strips none
    [b"(a)", b"<FEFF001B4142001B0061>"],         # one name to MuPDF ("a"), two to libqpdf
    [b"<A100>", b"<B0>"],                       # MuPDF keeps the NUL after a high byte
    [b"<C3A9>"],                                # MuPDF reads UTF-8 "\u00e9", libqpdf two characters
    [b"<C3A9>", b"<E9>"],                       # one name to MuPDF, two to libqpdf
    [b"<C3A900>"],                              # UTF-8 to MuPDF, which then cuts the NUL
], ids=["soft-hyphen", "nul-mixed-encodings", "utf16-nul", "utf16-esc-any", "utf16-esc-ascii",
        "utf16-esc-collides", "high-byte-nul", "utf8", "utf8-collides", "utf8-nul"])
def test_keys_the_readers_read_apart_are_flagged(keys: list[bytes]) -> None:
    assert int(R.TEXT) in _rules(_files(*keys))


def test_a_soft_hyphen_key_is_flagged_in_trees_the_gate_does_not_compare() -> None:
    data = _file(b" /Names << /JavaScript 4 0 R >>",
                 {4: b"<< /Names [(a!) 5 0 R <61AD> 6 0 R] >>", 5: b"<< >>", 6: b"<< >>"})
    assert int(R.TEXT) in _rules(data)


@pytest.mark.parametrize("gs", [
    b"<< /SMask << /S /Luminosity /G 4 0 R >> >>",   # direct soft mask
    b"<< /SMask 8 0 R >>",                           # soft mask by reference
], ids=["direct", "by-reference"])
def test_a_soft_masks_group_must_carry_its_own_resources(gs: bytes) -> None:
    # MuPDF runs /G as a form whatever its /Subtype says, borrowing the
    # page's resources when it has none (round-1 review, B3).
    objects = {6: gs, 8: b"<< /S /Luminosity /G 4 0 R >>"}
    data = _file(objects=objects, streams={4: (b" /BBox [0 0 9 9]", DRAW)},
                 page=_page(b" /Resources << /ExtGState << /GS0 6 0 R >> >>"))
    assert _scoped(data)
    fixed = _file(objects=objects, streams={4: (b" /BBox [0 0 9 9] /Resources << >>", DRAW)},
                  page=_page(b" /Resources << /ExtGState << /GS0 6 0 R >> >>"))
    assert not _scoped(fixed)


def test_an_images_soft_mask_needs_no_resources() -> None:
    image = b" /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceGray"
    data = _file(streams={4: (image + b" /SMask 5 0 R", b"\x00"), 5: (image, b"\x00")},
                 page=_page(b" /Resources << /XObject << /I 4 0 R >> >>"))
    assert not _scoped(data)


# Pinned regressions of review round 2 (3a-7c): one in a tree the gate
# compares, one in a tree it does not -- the inventory flags both.
@requires_qpdf
def test_an_escape_key_in_embedded_files_is_flagged_not_compared(tmp_path: Path) -> None:
    data = _files(b"(a)", b"<FEFF001B4142001B0062>")
    assert int(R.TEXT) in _rules(data)
    assert not compare(data, tmp_path).agrees  # FLAGGED: never certified


def test_an_escape_key_in_dests_is_flagged() -> None:
    data = _file(b" /Names << /Dests 4 0 R >>",
                 {4: b"<< /Names [(a) [3 0 R /Fit] <FEFF001B4142001B0062> [3 0 R /Fit]] >>"})
    assert int(R.TEXT) in _rules(data)


def test_a_shared_limits_array_of_a_long_key_is_charged() -> None:
    # Review round 2: d nodes naming one /Limits array of an L-byte key
    # compared d x L bytes uncharged. Now charged: linear, or BUDGET_EXHAUSTED.
    def chain(levels: int) -> bytes:
        key = b"(" + b"a" * (64 * levels) + b")"
        objects: dict[int, bytes] = {6: b"[%s %s]" % (key, key)}
        for i in range(levels):
            node = 100 + i
            below = (b"/Kids [%d 0 R]" % (node + 1) if i + 1 < levels
                     else b"/Names [%s 1]" % key)
            objects[node] = b"<< %s %s >>" % (b"/Limits 6 0 R" if i else b"", below)
        return _file(b" /Names << /Dests 100 0 R >>", objects)
    (t_s, w_s, f_s), (t_l, w_l, f_l) = _measure(chain, 200)
    for flags in (f_s, f_l):
        assert {f.reason for f in flags} <= {FlagReason.BUDGET_EXHAUSTED}
    # TODO(#113): the wall-clock half was dropped (it flaked under CI load,
    # as #94's did); restore it as steps.py's line events per unit charged.
    assert FlagReason.BUDGET_EXHAUSTED in {f.reason for f in f_l}


@requires_qpdf
def test_a_lone_high_byte_key_agrees_with_the_readers(tmp_path: Path) -> None:
    data = _files(b"<E9>", b"<F1>")  # not UTF-8: both read PDFDocEncoding
    assert _inventory(data).flags == ()
    found = compare(data, tmp_path)
    assert found.agrees, found


def _catalog_rewrites(keys: int, revisions: int) -> bytes:
    """An /EmbeddedFiles tree of *keys* entries, then *revisions* updates
    each rewriting only the catalog (an /OpenAction change, say): the trees
    are not walked again (review round 3)."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    catalog = b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >>%s >>"
    table[1] = (1, w.obj(1, catalog % b""), 0)
    table[2] = (1, w.obj(2, PAGES), 0)
    table[3] = (1, w.obj(3, PAGE), 0)
    pairs = b" ".join(b"(k%08d) %d 0 R" % (i, 10 + i) for i in range(keys))
    table[4] = (1, w.obj(4, b"<< /Names [%s] >>" % pairs), 0)
    for i in range(keys):
        table[10 + i] = (1, w.obj(10 + i, b"<< /Type /Filespec /F (f.txt) >>"), 0)
    size = 10 + keys
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for i in range(revisions):
        body = catalog % (b" /PageMode /UseNone" if i % 2 else b" /PageMode /UseOutlines")
        xref = w.table({1: (1, w.obj(1, body), 0)},
                       b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(xref)
        prev = xref
    return bytes(w.out)


def test_rewriting_the_catalog_does_not_walk_the_trees_again() -> None:
    (t_s, w_s, f_s), (t_l, w_l, f_l) = _measure(lambda n: _catalog_rewrites(n, n), 300)
    assert f_s == () and f_l == ()
    # TODO(#113): the wall-clock half was dropped (it flaked under CI load,
    # as #94's did); restore it as steps.py's line events per unit charged.
    assert w_l < 10 * w_s, (w_s, w_l)
    inv = _inventory(_catalog_rewrites(20, 5))
    assert all(t is inv.trees[-1] for t in inv.trees)  # one result, reused


def test_rewriting_the_catalogs_names_walks_the_trees_again() -> None:
    base = _files(b"(a.txt)", b"(b.txt)")
    broken = _update(base, {1: b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles [] >> >>"})
    found = [dict(f.params) for f in _inventory(broken).flags if f.reason is FlagReason.NAME_TREE]
    assert found and {f["revision"] for f in found} == {0}
