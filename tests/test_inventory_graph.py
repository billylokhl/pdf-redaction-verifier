"""The reference graph, part a (Phase 3a-7a, ADR 0011 item 4): every
body's references with a use kind, and per revision every reference
resolved -- to a live body of its generation, or flagged REF_GENERATION
or REF_DANGLING (owner decision R1). Every file is generated here;
fabricated content only."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from scorecard.inventory import compare
from scorecard.pdfgen import Writer

import redaction_verifier.inventory.build as build_module
from redaction_verifier.budget import Budget, Counter, Limits
from redaction_verifier.inventory import build_inventory
from redaction_verifier.inventory.graph import ARRAY, UseKind, use_kind
from redaction_verifier.ledger import Flag, FlagReason

from .conftest import requires_qpdf

CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] /Resources << >> >>"
SSN = b"123-45-6789"


def _inventory(data: bytes, limits: Limits | None = None):  # type: ignore[no-untyped-def]
    return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)))


def _refs(data: bytes) -> list[tuple[str, dict[str, int]]]:
    return [(f.reason.name, dict(f.params)) for f in _inventory(data).flags
            if f.reason in (FlagReason.REF_DANGLING, FlagReason.REF_GENERATION)]


def _file(extra: dict[int, bytes] | None = None, trailer: bytes = b"",
          gens: dict[int, int] | None = None, free: tuple[int, ...] = ()) -> bytes:
    """Catalog 1, pages 2, page 3, and *extra* objects; *gens* writes an
    object at another generation; *free* lists numbers marked free."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    bodies = {1: CATALOG, 2: PAGES, 3: PAGE} | (extra or {})
    for n, body in bodies.items():
        gen = (gens or {}).get(n, 0)
        at = w.at()
        w.out += b"%d %d obj\n" % (n, gen) + body + b"\nendobj\n"
        table[n] = (1, at, gen)
    for n in free:
        table[n] = (0, 0, 1)
    size = max(table) + 1
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R%s" % (size, trailer)))
    return bytes(w.out)


# ── Use kinds: a closed table, by key path ────────────────────────────────
@pytest.mark.parametrize(("path", "kind"), [
    ((b"Root",), UseKind.ROOT),
    ((b"Kids", ARRAY), UseKind.PAGES),
    ((b"Parent",), UseKind.PARENT),
    ((b"Resources",), UseKind.RESOURCES),
    ((b"Resources", b"Font", b"F1"), UseKind.FONT),
    ((b"Resources", b"XObject", b"Im0"), UseKind.XOBJECT),
    ((b"Contents", ARRAY), UseKind.CONTENTS),
    ((b"AP", b"N"), UseKind.APPEARANCE),
    ((b"Names", b"EmbeddedFiles"), UseKind.NAME_TREE),
    ((b"Thumb",), UseKind.THUMBNAIL),
    ((b"PieceInfo", b"App", b"Private"), UseKind.PIECE_INFO),
    ((b"Probe",), UseKind.OTHER),
    ((), UseKind.OTHER),
])
def test_a_use_kind_comes_from_the_key_path(path: tuple[bytes, ...], kind: UseKind) -> None:
    assert use_kind(path) is kind


def test_a_bodys_edges_are_collected_with_their_kinds() -> None:
    inv = _inventory(_file({4: b"<< /Probe [5 0 R] /Thumb 5 0 R >>", 5: b"(x)"}))
    body = inv.body(4)
    assert body is not None
    assert [(e.kind, e.num, e.gen, e.path) for e in inv.graph.edges[body]] == [
        (UseKind.OTHER, 5, 0, (b"Probe", ARRAY)), (UseKind.THUMBNAIL, 5, 0, (b"Thumb",))]
    assert [(e.kind, e.num) for e in inv.graph.trailers[0]] == [(UseKind.ROOT, 1)]


def test_a_clean_file_has_no_reference_flag() -> None:
    assert _refs(_file({4: b"<< /Probe [1 0 R 2 0 R 3 0 R] >>"})) == []


# ── Every reference resolves, or flags (R1) ───────────────────────────────
@pytest.mark.parametrize(("extra", "free", "reason", "target"), [
    ({4: b"<< /Probe 9 0 R >>"}, (), "REF_DANGLING", 9),          # past the table
    ({4: b"<< /Probe 5 0 R >>"}, (5,), "REF_DANGLING", 5),         # free
    ({4: b"<< /Probe [0 0 R] >>"}, (), "REF_DANGLING", 0),         # object 0
    ({4: b"<< /Probe 3 5 R >>"}, (), "REF_GENERATION", 3),         # another generation
    ({4: b"<< /K << /L [3 1 R] >> >>"}, (), "REF_GENERATION", 3),  # nested
])
def test_a_reference_that_does_not_resolve_is_flagged(
        extra: dict[int, bytes], free: tuple[int, ...], reason: str, target: int) -> None:
    found = _refs(_file(extra, free=free))
    assert [(r, p["object"], p["target"]) for r, p in found] == [(reason, 4, target)]


def test_a_trailer_reference_is_resolved() -> None:
    found = _refs(_file(trailer=b" /Info 9 0 R"))
    assert found == [("REF_DANGLING", {"object": -1, "target": 9, "generation": 0,
                                       "revision": 0})]


def test_an_object_at_another_generation_resolves_by_its_own() -> None:
    data = _file({4: b"<< /Probe 5 2 R /Old 5 0 R >>", 5: b"(x)"}, gens={5: 2})
    assert [(r, p["generation"]) for r, p in _refs(data)] == [("REF_GENERATION", 0)]


def test_an_offset_zero_entry_is_no_target() -> None:
    w = Writer()
    table = {0: (0, 0, 65535), 1: (1, w.obj(1, CATALOG), 0), 2: (1, w.obj(2, PAGES), 0),
             3: (1, w.obj(3, PAGE), 0), 4: (1, w.obj(4, b"<< /Probe 5 0 R >>"), 0),
             5: (1, 0, 0)}
    w.epilogue(w.table(table, b"/Size 6 /Root 1 0 R"))
    assert [r for r, _ in _refs(bytes(w.out))] == ["REF_DANGLING"]


# ── Compressed targets: generation 0, in a live home ──────────────────────
def _objstm(probe: bytes, members: list[tuple[int, bytes]]) -> bytes:
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in ((1, CATALOG), (2, PAGES), (3, PAGE), (5, probe)):
        table[n] = (1, w.obj(n, body), 0)
    table[4] = (1, w.objstm(4, members), 0)
    table |= {n: (2, 4, i) for i, (n, _) in enumerate(members)}
    w.epilogue(w.xref_stream(9, table, b"/Size 10 /Root 1 0 R"))
    return bytes(w.out)


def test_a_compressed_target_resolves_at_generation_0() -> None:
    assert _refs(_objstm(b"<< /Probe 6 0 R >>", [(6, b"(x)")])) == []
    found = _refs(_objstm(b"<< /Probe 6 1 R >>", [(6, b"(x)")]))
    assert [(r, p["target"]) for r, p in found] == [("REF_GENERATION", 6)]


def test_a_compressed_source_is_resolved_too() -> None:
    found = _refs(_objstm(b"<< /Probe 6 0 R >>", [(6, b"<< /K 8 0 R >>")]))
    assert [(r, p["object"], p["target"]) for r, p in found] == [("REF_DANGLING", 6, 8)]


# ── Revision by revision ──────────────────────────────────────────────────
def _updated(update: dict[int, tuple[int, int, int] | bytes], base: dict[int, bytes],
             trailer: bytes = b"") -> bytes:
    """A base revision (catalog, pages, page and *base*) and one update
    writing *update*: a body (in use, generation 0) or an entry."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in ({1: CATALOG, 2: PAGES, 3: PAGE} | base).items():
        table[n] = (1, w.obj(n, body), 0)
    first = w.table(table, b"/Size %d /Root 1 0 R" % (max(table) + 1))
    size = max(max(table), *update) + 1
    w.epilogue(first)
    newer = {n: (1, w.obj(n, v), 0) if isinstance(v, bytes) else v for n, v in update.items()}
    w.epilogue(w.table(newer, b"/Size %d /Root 1 0 R /Prev %d%s" % (size, first, trailer)))
    return bytes(w.out)


def test_a_target_freed_by_an_update_flags_in_that_revision() -> None:
    data = _updated({5: (0, 0, 1)}, {4: b"<< /Probe 5 0 R >>", 5: b"(x)"})
    found = _refs(data)
    assert [(r, p["object"], p["target"], p["revision"]) for r, p in found] == [
        ("REF_DANGLING", 4, 5, 0)]


def test_a_reference_dangling_in_an_older_revision_flags_there() -> None:
    # Revision 1 references 5, which only revision 0 adds: revision 1 reads null.
    data = _updated({5: b"(x)"}, {4: b"<< /Probe 5 0 R >>"})
    found = _refs(data)
    assert [(r, p["object"], p["target"], p["revision"]) for r, p in found] == [
        ("REF_DANGLING", 4, 5, 1)]


def test_one_flag_per_reference_whatever_the_revisions() -> None:
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in ((1, CATALOG), (2, PAGES), (3, PAGE), (4, b"<< /Probe 9 0 R >>")):
        table[n] = (1, w.obj(n, body), 0)
    prev = w.table(table, b"/Size 10 /Root 1 0 R")
    w.epilogue(prev)
    for _ in range(5):  # updates that touch nothing the reference uses
        at = w.obj(6, b"(y)")
        xref = w.table({6: (1, at, 0)}, b"/Size 10 /Root 1 0 R /Prev %d" % prev)
        w.epilogue(xref)
        prev = xref
    assert [(r, p["target"]) for r, p in _refs(bytes(w.out))] == [("REF_DANGLING", 9)]


def test_a_replaced_object_stream_moves_its_members() -> None:
    # Revision 1: 6 lives in object stream 4. Revision 0 replaces 4 with an
    # object stream that no longer holds 6: 5's reference dangles there.
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in ((1, CATALOG), (2, PAGES), (3, PAGE), (5, b"<< /Probe 6 0 R >>")):
        table[n] = (1, w.obj(n, body), 0)
    table[4] = (1, w.objstm(4, [(6, b"(x)")]), 0)
    table[6] = (2, 4, 0)
    first = w.xref_stream(9, table, b"/Size 11 /Root 1 0 R")
    w.epilogue(first)
    newer = {4: (1, w.objstm(4, [(7, b"(y)")]), 0), 7: (2, 4, 0)}
    w.epilogue(w.xref_stream(10, newer, b"/Size 11 /Root 1 0 R /Prev %d" % first))
    reasons = {f.reason.name for f in _inventory(bytes(w.out)).flags}
    assert "OBJSTM_ENTRY_MISMATCH" in reasons  # 6's entry names a home without it
    assert ("REF_DANGLING", 5, 6, 0) in [
        (r, p["object"], p["target"], p["revision"]) for r, p in _refs(bytes(w.out))]


# ── The readers (R1: flagged whatever they read; never compared) ──────────
@requires_qpdf
@pytest.mark.parametrize("data", [
    _file({4: b"<< /Probe 9 0 R >>"}),
    _file({4: b"<< /Probe 3 5 R /K (SSN " + SSN + b") >>"}),
    _file(trailer=b" /Info 9 0 R"),
])
def test_a_flagged_reference_is_never_compared(tmp_path: Path, data: bytes) -> None:
    assert compare(data, tmp_path).flagged


@requires_qpdf
def test_resolved_references_agree_with_the_readers(tmp_path: Path) -> None:
    data = _file({4: b"<< /Probe [1 0 R 2 0 R 3 0 R 5 2 R] >>", 5: b"(x)"}, gens={5: 2})
    found = compare(data, tmp_path)
    assert found.agrees, found


# ── The budget: the graph runs before its flags are gathered ──────────────
def test_a_graph_that_runs_out_of_budget_flags_the_file(
        monkeypatch: pytest.MonkeyPatch) -> None:
    data = _file({4: b"<< /Probe [" + b"1 0 R " * 2000 + b"] >>"})
    spent: list[int] = []
    real = build_module.build_graph

    def recording(*args, **kwargs):  # type: ignore[no-untyped-def]
        spent.append(args[-1].work)  # the budget's work before the graph
        return real(*args, **kwargs)
    monkeypatch.setattr(build_module, "build_graph", recording)
    assert _inventory(data).flags == ()
    tight = Limits(work_floor=spent[0] + 100, work_per_byte=0)
    budget = Budget(tight, file_size=len(data))
    flags = build_inventory(data, tight, budget=budget).flags
    assert budget.exhausted(Counter.WORK)
    assert FlagReason.BUDGET_EXHAUSTED in {f.reason for f in flags}


# ── Linearity (ADR 0011 item 7; ADR 0010: count work and wall clock) ──────
def _rewrites(bodies: int, updates: int, flip: bool) -> bytes:
    """*bodies* objects referencing object 4; *updates* revisions each
    rewriting 4 -- at the same generation, or (*flip*) alternately freeing
    it, every reference flipping between resolved and dangling."""
    w = Writer()
    first = 10
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for n, body in ((1, CATALOG), (2, PAGES), (3, PAGE), (4, b"(target)")):
        table[n] = (1, w.obj(n, body), 0)
    for n in range(first, first + bodies):
        table[n] = (1, w.obj(n, b"<< /Probe 4 0 R >>"), 0)
    size = first + bodies
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for i in range(updates):
        entry = (0, 0, 1) if flip and i % 2 == 0 else (1, w.obj(4, b"(again)"), 0)
        xref = w.table({4: entry}, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(xref)
        prev = xref
    return bytes(w.out)


def _work(data: bytes) -> tuple[int, float, tuple[Flag, ...]]:
    budget = Budget(file_size=len(data))
    started = time.process_time()
    flags = build_inventory(data, budget=budget).flags
    return budget.work, time.process_time() - started, flags


def test_rewriting_a_shared_target_stays_linear() -> None:
    small, _, _ = _work(_rewrites(200, 200, flip=False))
    large, _, flags = _work(_rewrites(1600, 1600, flip=False))
    assert FlagReason.BUDGET_EXHAUSTED not in {f.reason for f in flags}
    assert large < 10 * small, (small, large)  # 8x the input: about 8x the work


def test_flipping_a_shared_target_is_charged() -> None:
    # Every flip re-resolves every reference into it: changes x fan-in, by
    # design (docs/phase3a-plan.md 3a-7a). The budget bounds it -- within the
    # limit or flagged BUDGET_EXHAUSTED -- provided all of it is charged:
    # its time per unit of work stays that of a linear shape (a ratio, so
    # any machine and coverage's slowdown cancel; best of three runs).
    data = _rewrites(800, 800, flip=True)
    work, _, flags = _work(data)
    assert work <= Limits().work_limit(len(data))
    linear = _rewrites(800, 800, flip=False)
    base_work = _work(linear)[0]
    per = min(_work(data)[1] for _ in range(3)) / work
    base = min(_work(linear)[1] for _ in range(3)) / base_work
    assert per < 3 * base, (per, base)
    assert FlagReason.REF_DANGLING in {f.reason for f in flags}


def _wide(n: int) -> bytes:
    """Object 4 references n numbers the base lacks; one update adds them
    all: every reference flips from dangling to resolved at once."""
    w = Writer()
    first = 10
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    probe = b"[" + b" ".join(b"%d 0 R" % k for k in range(first, first + n)) + b"]"
    for k, body in ((1, CATALOG), (2, PAGES), (3, PAGE), (4, probe)):
        table[k] = (1, w.obj(k, body), 0)
    base = w.table(table, b"/Size 5 /Root 1 0 R")
    w.epilogue(base)
    newer = {k: (1, w.obj(k, b"(x)"), 0) for k in range(first, first + n)}
    w.epilogue(w.table(newer, b"/Size %d /Root 1 0 R /Prev %d" % (first + n, base)))
    return bytes(w.out)


def _stale(bodies: int, flips: int) -> bytes:
    """*bodies* objects reference 4 and are then freed; 4 then flips
    between free and live *flips* times. Freed sources must cost nothing."""
    w = Writer()
    first = 10
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    for k, body in ((1, CATALOG), (2, PAGES), (3, PAGE), (4, b"(target)")):
        table[k] = (1, w.obj(k, body), 0)
    for k in range(first, first + bodies):
        table[k] = (1, w.obj(k, b"<< /Probe 4 0 R >>"), 0)
    size = first + bodies
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    xref = w.table({k: (0, 0, 1) for k in range(first, first + bodies)},
                   b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
    w.epilogue(xref)
    prev = xref
    for i in range(flips):
        entry = (0, 0, 1) if i % 2 == 0 else (1, w.obj(4, b"(again)"), 0)
        xref = w.table({4: entry}, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(xref)
        prev = xref
    return bytes(w.out)


@pytest.mark.parametrize("shape", [lambda n: _wide(n), lambda n: _stale(n, n // 4)],
                         ids=["wide", "stale"])
def test_every_step_of_the_graph_is_charged(shape) -> None:  # type: ignore[no-untyped-def]
    # Wall clock, not only work counts (ADR 0010): 8x the input costs
    # about 8x the time, and the work charged keeps pace with it.
    small, large = shape(4000), shape(32000)
    work_s, _, flags_s = _work(small)
    work_l, _, flags_l = _work(large)
    # The best of three runs each: a loaded machine slows a run, never speeds one.
    time_s = min(_work(small)[1] for _ in range(3))
    time_l = min(_work(large)[1] for _ in range(3))
    assert FlagReason.BUDGET_EXHAUSTED not in {f.reason for f in flags_s + flags_l}
    assert work_l < 10 * work_s, (work_s, work_l)
    assert time_l < 12 * max(time_s, 0.05), (time_s, time_l)  # quadratic: ~18x and up
