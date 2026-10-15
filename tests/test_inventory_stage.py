"""The reference graph's whole stage at scale (Phase 3a-7e-3, ADR 0011
item 7): the edges, page lists, name trees, annotations and labels
(reachable, STRUCTURAL, DRAWABLE, per body) together, with revisions and
objects scaled together, n against 8n. Every revision walks again, so the
work grows as their product, about 64 times; it is all charged, and the
budget bounds it: within the linear bound, or flagged BUDGET_EXHAUSTED,
never hanging. And the measures 3a-7e-3's cost briefing reads (eval:
scorecard.inventory.cost_measures, inventory_gate's labels_cost). Every
file is generated here; fabricated content only."""

from __future__ import annotations

import base64
import zlib
from types import SimpleNamespace

import pytest
from scorecard.inventory import cost_measures, drawing_measures
from scorecard.inventory_gate import _labels_cost
from scorecard.pdfgen import Writer

from redaction_verifier.budget import Budget, Limits
from redaction_verifier.inventory import build_inventory
from redaction_verifier.inventory.labels import StructuralKind
from redaction_verifier.ledger import FlagReason

from .steps import charged_steps, step_cost
from .test_inventory_labels import PER_UNIT_GROWTH, XMP, make

# Lines the inventory executes per unit of work the whole stage charges, at
# most (#94), with the retained-memory charges left out (walk_only):
# measured 10.3 to 10.6 on this shape (n = 20 to 320), as the label walks'
# own tests find; 15, as there, leaves room for a change in the mix of
# steps, not for an uncharged loop.
LINES_PER_UNIT = 15

IMG = (b" /Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8"
       b" /ColorSpace /DeviceGray")
FORM = b" /Type /XObject /Subtype /Form /BBox [0 0 9 9] /Resources << %s>>"


def _stage(n: int, updates: int | None = None) -> bytes:
    """A file of about 2n objects and n + 1 revisions. With q = n / 4:
    q pages sharing one resource dictionary, which draws a chain of q forms
    and q images; q JavaScript actions in the catalog's /JavaScript name
    tree (a leaf each), each with its script stream; the catalog's XMP
    packet; a stamp annotation with a stored appearance. Each of the n
    updates draws one page with a new content stream and rewrites the
    catalog and one leaf of the name tree as they were: every
    revision's page list, name trees, annotations, reachable set and
    drawing are made again (no reuse applies), each over the whole file."""
    q = max(n // 4, 1)
    page0, form0, image0, content0, action0, script0, leaf0 = (100 + k * q for k in range(7))
    new0 = 100 + 7 * q
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}

    def obj(number: int, body: bytes) -> None:
        table[number] = (1, w.obj(number, body), 0)

    def stream(number: int, extra: bytes, data: bytes) -> None:
        table[number] = (1, w.stream(number, extra, data), 0)

    def catalog(i: int) -> bytes:
        return (b"<< /Type /Catalog /Pages 2 0 R /Metadata 4 0 R /Names << /JavaScript 5 0 R >>"
                b" /PageMode /%s >>" % (b"UseNone" if i % 2 else b"UseOutlines"))

    def page(i: int, contents: int) -> bytes:
        return (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] /Resources 9 0 R"
                b" /Contents %d 0 R%s >>" % (contents, b" /Annots [10 0 R]" if i == 0 else b""))
    def leaf(i: int) -> bytes:
        return b"<< /Limits [(a%05d) (a%05d)] /Names [(a%05d) %d 0 R] >>" % (i, i, i, action0 + i)
    obj(1, catalog(0))
    obj(2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (page0 + i) for i in range(q)), q))
    stream(4, b" /Type /Metadata /Subtype /XML", XMP)
    obj(5, b"<< /Kids [%s] >>" % b" ".join(b"%d 0 R" % (leaf0 + i) for i in range(q)))
    obj(9, b"<< /XObject << /Fm0 %d 0 R %s >> >>"
        % (form0, b" ".join(b"/Im%d %d 0 R" % (i, image0 + i) for i in range(q))))
    obj(10, b"<< /Type /Annot /Subtype /Stamp /Rect [0 0 9 9] /AP << /N 11 0 R >> >>")
    stream(11, FORM % b"", b"0 0 m")
    for i in range(q):
        obj(page0 + i, page(i, content0 + i))
        stream(form0 + i, FORM % (b"/XObject << /X %d 0 R >> " % (form0 + i + 1)
                                  if i + 1 < q else b""), b"/X Do")
        stream(image0 + i, IMG, b"\x00")
        stream(content0 + i, b"", b"/Fm0 Do /Im%d Do" % i)
        obj(action0 + i, b"<< /S /JavaScript /JS %d 0 R >>" % (script0 + i))
        stream(script0 + i, b"", b"app.alert(%d);" % i)
        obj(leaf0 + i, leaf(i))
    prev = w.table(table, b"/Size %d /Root 1 0 R" % new0)
    w.epilogue(prev)
    for i in range(n if updates is None else updates):
        number = new0 + i
        table = {}
        stream(number, b"", b"/Im%d Do" % (i % q))
        obj(page0 + i % q, page(i % q, number))
        obj(1, catalog(i + 1))
        obj(leaf0 + i % q, leaf(i % q))
        prev = w.table(table, b"/Size %d /Root 1 0 R /Prev %d" % (number + 1, prev))
        w.epilogue(prev)
    return bytes(w.out)


@pytest.fixture
def walk_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The walks' own charges only: the lump charges for what pages and
    labels retain weigh more in a small file (their own tests bound them)."""
    from redaction_verifier.inventory import labels, pages
    for name in ("RECORD_WORK", "SET_WORK", "BY_KEY_WORK", "LABELS_WORK", "LIST_WORK",
                 "BODY_LABELS_WORK"):
        monkeypatch.setattr(labels, name, 0)
    monkeypatch.setattr(pages, "PAGE_RECORD_WORK", 0)


def test_the_shape_walks_every_stage_in_every_revision() -> None:
    # The shape does what the tests below rely on: no flag, each revision's
    # page list, name trees, annotations and labels made again (none shared
    # by identity with its predecessor's), and every label kind present.
    inv = build_inventory(data := _stage(8), budget=Budget(file_size=len(data)))
    assert inv.flags == () and inv.revisions == 9
    for r in range(inv.revisions - 1):
        assert inv.pages[r] is not inv.pages[r + 1]
        assert inv.trees[r] is not inv.trees[r + 1]
        assert inv.annotations[r] is not inv.annotations[r + 1]
        assert inv.labels[r] is not inv.labels[r + 1]
        found, older = inv.labels[r], inv.labels[r + 1]
        assert found is not None and older is not None
        assert found.reachable is not older.reachable and found.drawable is not older.drawable
    labels = inv.labels[0]
    assert labels is not None
    kinds = {record.kind for records in labels.structural.values() for record in records}
    assert kinds == {StructuralKind.XMP, StructuralKind.JAVASCRIPT}
    # q = 2: two forms, two images, each page's content stream now, the appearance.
    assert len(labels.drawable) == 2 + 2 + 2 + 1
    # The content streams the pages drew before: the two first, six updates'.
    assert len(inv.orphaned(0)) == 8


def test_the_stage_is_within_the_linear_bound_or_flagged() -> None:
    # ADR 0011 item 7: revisions and objects scaled together (n vs 8n each,
    # about 64 times the work). Measured: n = 40 charges 464,543 units to the
    # stage, within its file's limit of 2,950,080 (64 per byte, plus the
    # floor); n = 320 would charge about 64 times that, 30 million, against
    # a limit of 16.3 million: the budget stops the stage, the newest
    # revisions have no labels, and the file is flagged BUDGET_EXHAUSTED --
    # never an unflagged inventory, never a hang. (About 50-70 units per
    # live object or edge per revision walked again, n = 20 to 160: DESIGN.md,
    # "Bounded resources", the 64 N R bound.)
    work = []
    for n in (40, 320):
        data = _stage(n)
        budget = Budget(file_size=len(data))
        inv = build_inventory(data, budget=budget)
        reasons = {flag.reason for flag in inv.flags}
        assert reasons <= {FlagReason.BUDGET_EXHAUSTED}, reasons
        exhausted = FlagReason.BUDGET_EXHAUSTED in reasons
        assert budget.work <= Limits().work_limit(len(data)) or exhausted
        work.append((inv.graph_work, exhausted, inv.labels))
    (w_s, small_out, small_labels), (w_l, large_out, large_labels) = work
    assert not small_out and None not in small_labels
    # Superlinear by design (revisions x objects), and stopped at the cap.
    assert large_out and large_labels[0] is None and large_labels[-1] is not None
    assert w_l >= 16 * w_s, (w_s, w_l)


def test_every_step_of_the_stage_is_charged(walk_only: None) -> None:
    # Not only work counts (ADR 0010): the lines the inventory executes per
    # unit of work the whole stage charges stay under a fixed bound and do
    # not grow from n to 8n (#94), so no stage hides an uncharged loop.
    work = []
    for n in (20, 160):
        charged, lines, inv = charged_steps(_stage(n), "_reference_graph")
        assert inv.flags == (), inv.flags
        assert inv.graph_work == charged
        work.append((charged, lines))
    (w_s, l_s), (w_l, l_l) = work
    assert l_s <= LINES_PER_UNIT * w_s and l_l <= LINES_PER_UNIT * w_l, work
    assert l_l / w_l <= PER_UNIT_GROWTH * (l_s / w_s), work
    assert w_l >= 16 * w_s, work  # revisions x objects: every revision walked again


def test_the_stage_costs_its_charged_work_in_wall_clock_too(walk_only: None) -> None:
    # The backstop for work done inside C, which no line count sees (a
    # linear `in` on a list, a copy: review of #113): the whole stage's time
    # per unit it charges does not grow with the input. A C-level scan
    # costs nanoseconds a step, so it shows with many objects, not many
    # revisions: 2,000 against 16,000 objects' worth (n), four updates.
    # Measured clean 1.23-1.33x (0.24-0.33 us a unit), and with the
    # reachable walk's seen-set a list 4.6x (5.8x at 4,000 vs 32,000); with
    # n revisions as well (60 vs 480) clean 0.96-0.98x, that mutant only
    # 1.3x. 2x leaves 1.5x of room on the clean side and 2.3x on the
    # mutant's. Best of three at n, two at 8n: a loaded machine only slows
    # a run. Under coverage Python runs slower and C does not, diluting the
    # signal: the uncovered leg is the one that catches it (as for the graph
    # and the page walk).
    per_unit = []
    for n, runs in ((2000, 3), (16000, 2)):
        data = _stage(n, updates=4)
        timed = [step_cost(data, "_reference_graph") for _ in range(runs)]
        # The work charged is the same on every run (deterministic).
        per_unit.append(max(min(t for t, _ in timed), 0.02) / timed[0][1])
    assert per_unit[1] < 2 * per_unit[0], per_unit


# ── The cost briefing's measures (eval only) ──────────────────────────────
def _packet(inside: bytes) -> bytes:
    return (b"<?xpacket begin=''?><x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF><rdf:Description>"
            + inside + b"</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end='w'?>")


# A fabricated JPEG start and PNG signature, padded; the base64 broken by
# XML line breaks as writers write them.
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + bytes(range(96)))
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(96)))
META = b" /Type /Metadata /Subtype /XML"


def test_cost_measures_scan_xmp_packets_and_sum_the_graph() -> None:
    thumb = _packet(b"<xmpGImg:image>" + b"&#xA;".join(JPEG[i:i + 76]
                                                        for i in range(0, len(JPEG), 76))
                    + b"</xmpGImg:image>")
    custom = _packet(b"<my:preview>" + PNG + b"</my:preview>")  # any namespace
    digest = _packet(b"<my:digest>" + bytes(range(64)).hex().encode() + b"</my:digest>")
    text = _packet(b"<my:note>" + b"plain text, hex-encoded".hex().encode() * 3 + b"</my:note>")
    data = make(catalog=b" /Metadata 4 0 R", page=b" /Metadata 5 0 R /Thumb 6 0 R",
                objects={2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 /Metadata 7 0 R >>",
                         8: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                            b" /Metadata 9 0 R >>",
                         10: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                             b" /Metadata 11 0 R >>"},
                streams={4: (META, thumb), 5: (META + b" /Filter /FlateDecode",
                                               zlib.compress(custom)),
                         6: (IMG, b"\x00"), 7: (META, digest), 9: (META, text),
                         11: (META, text)})
    inv = build_inventory(data, budget=Budget(file_size=len(data)))
    assert inv.flags == ()
    found = cost_measures(data, inv)
    # Objects 1-11; edges 1: /Pages, /Metadata; 2: /Kids, /Metadata; 3:
    # /Parent, /Metadata, /Thumb; 8 and 10: /Metadata. One revision.
    assert found["graph_sum"] == 11 + 9
    assert found["xmp_bodies"] == 3  # 9 and 11: their holders are unreachable, no label
    assert found["xmp_image_bodies"] == found["xmp_image_current"] == 2  # 4, 5
    assert found["xmp_binary_bodies"] == 1  # 7: a digest is binary nothing accounts for
    assert found["xmp_undecoded_bodies"] == 0
    assert found["page_thumb_legacy"] == 1
    drawn = drawing_measures(data, inv)
    assert drawn["orphan_body_class_thumbnail"] == 1
    assert drawn["orphan_body_class_unreachable_source"] == 2  # 9, 11, held by 8, 10


def test_cost_measures_mark_no_thumbnail_and_no_image_where_there_is_none() -> None:
    data = make(catalog=b" /Metadata 4 0 R", streams={4: (META, _packet(b"<my:x>1</my:x>"))})
    found = cost_measures(data, build_inventory(data, budget=Budget(file_size=len(data))))
    assert found["page_thumb_legacy"] == found["xmp_image_bodies"] == 0
    assert found["xmp_bodies"] == 1


def test_the_cost_aggregate_counts_each_file_once() -> None:
    files = [
        # Page thumbnails only, which today's CLI already marks (#133).
        {"bodies_orphaned": 2, "orphan_body_class_thumbnail": 2, "page_thumb_legacy": 1,
         "orphaned_current": 2},
        # A hint stream and an XMP image: one file, two classes.
        {"structural_hint_stream": 1, "xmp_image_bodies": 1, "graph_work": 900,
         "graph_sum": 30},
        # An orphan labelled in an older revision: not the verdict's.
        {"orphaned_current": 1, "graph_work": 100, "graph_sum": 10},
        {"structural_javascript": 2, "synthesized_current": 1, "orphan_body_class_properties": 1,
         "bodies_orphaned": 1},
    ]
    found = _labels_cost([SimpleNamespace(measures=m) for m in files])  # type: ignore[misc]
    assert found["exit_2_files"] == 3 and found["files"] == 4
    assert found["exit_2_by_class"] == {"orphan": 2, "hint_stream": 1, "xmp_image": 1,
                                        "javascript": 1, "synthesized": 1, "output_intent": 0}
    assert found["exit_2_only_class"] == {"orphan": 1}
    assert found["exit_2_files_but_thumbnails_marked_today"] == 2
    assert found["orphan_files_current"] == 2
    assert found["orphan_body_classes"]["properties"] == {"files": 1, "total": 1}
    assert found["thumbnail_orphan_files_marked_today"] == 1
    assert found["graph_sum_of_largest_work"] == 30
    assert found["graph_work_per_sum"]["max"] == 30.0
