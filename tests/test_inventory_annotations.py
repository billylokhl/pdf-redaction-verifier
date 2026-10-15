"""Synthesized annotations (Phase 3a-7d, ADR 0011 item 5 with its
2026-10-11 amendment): which annotations MuPDF builds an appearance for,
the SYNTHESIZED units, FIELD_TREE (owner decision P5),
APPEARANCE_UNSUPPORTED, and the agreement gate's annotation part (owner
decision 11). Every classification is checked against MuPDF's own
drawing (scorecard.annotations, the gate's method). Every file is
generated here; fabricated content only."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from scorecard.annotations import NOTHING, SYNTHESIZED, readings
from scorecard.inventory import Check, compare
from scorecard.pdfgen import Writer

from redaction_verifier.budget import Budget, Counter, Limits
from redaction_verifier.inventory import Inventory, build_inventory
from redaction_verifier.inventory.annotations import (AppearanceRule, FieldTreeRule, Synthesized,
                                                      build_annotations)
from redaction_verifier.ledger import FlagReason, Status

from .conftest import requires_qpdf
from .steps import charged_steps

# Lines the inventory executes per unit of work the annotation walk
# charges, at most (#94): measured about 10; 15 leaves room for a change in
# the mix of steps, not for an uncharged loop (review of #140: at 50, six
# of the reviewer's mutants survived). The per-unit count must also not
# grow with the input (PER_UNIT_GROWTH).
LINES_PER_UNIT = 15
PER_UNIT_GROWTH = 1.5

RECT = b"/Rect [10 10 60 30]"
AP = b"/AP << /N 5 0 R >>"
SQ = b"/Type /Annot /Subtype /Square"
W = b"/Type /Annot /Subtype /Widget"
TX = W + b" /FT /Tx /T (t) /V (SSN 123-45-6789) /DA (/Helv 9 Tf 0 g)"
SIG = W + b" /FT /Sig /T (s)"
FIELDS = b" /AcroForm << /Fields [4 0 R] >>"
NEED = b" /AcroForm << /Fields [4 0 R] /NeedAppearances true >>"
RED = b"1 0 0 rg 0 0 50 20 re f"
FORM = b" /Subtype /Form /BBox [0 0 50 20] /Resources << >>"


def build(annot: bytes, extra: dict[int, bytes] | None = None, acroform: bytes = b"",
          direct: bool = False, also: bytes = b"",
          updates: tuple[dict[int, bytes], ...] = ()) -> bytes:
    """One page (object 3); annotation 4 (or *annot* written directly in
    /Annots), then *also*; object 5 a stored appearance (a red box);
    *extra* objects (from 6); each of *updates* an incremental update
    rewriting those objects (1: the catalog, whose /AcroForm is
    *acroform* unless rewritten)."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R%s >>" % acroform,
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >>"
           b" /Annots [%s%s] >>" % (annot if direct else b"4 0 R", also)}
    if not direct:
        objects[4] = annot
    objects |= extra or {}
    for n, body in sorted(objects.items()):
        table[n] = (1, w.obj(n, body), 0)
    table[5] = (1, w.stream(5, FORM, RED), 0)
    size = max(table) + 1
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for update in updates:
        size = max(size, max(update) + 1)
        found = {n: (1, w.obj(n, body), 0) for n, body in sorted(update.items())}
        prev = w.table(found, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(prev)
    return bytes(w.out)


def _inventory(data: bytes, limits: Limits | None = None) -> Inventory:
    return build_inventory(data, limits, budget=Budget(limits, file_size=len(data)))


def _ours(data: bytes, revision: int = 0) -> list[bool]:
    found = _inventory(data).annotations[revision]
    assert found is not None
    return [a.synthesized for a in found]


def _reasons(data: bytes) -> set[FlagReason]:
    return {f.reason for f in _inventory(data).flags}


# ── Classification: ADR 0011 item 5, as MuPDF reads it ────────────────────
# (name, file, synthesized). The spike's 35 fixtures' conditions, plus the
# shapes found while implementing it (references, nulls, field types).
CASES: list[tuple[str, bytes, bool]] = [
    # A stored /AP /N: a stream, or a state dictionary whose /AS names an entry.
    ("ap-stream", build(b"<< %s %s %s >>" % (SQ, RECT, AP)), False),
    ("ap-state", build(b"<< %s %s /AS /On /AP << /N << /On 5 0 R >> >> >>" % (SQ, RECT)), False),
    ("as-by-reference", build(b"<< %s %s /AS 6 0 R /AP << /N << /On 5 0 R >> >> >>" % (SQ, RECT),
                              {6: b"/On"}), False),
    ("as-entry-not-a-stream", build(b"<< %s %s /C [0 0 1] /AS /On /AP << /N << /On 3 >> >> >>"
                                    % (SQ, RECT)), False),
    ("ap-by-reference", build(b"<< %s %s /AP 6 0 R >>" % (SQ, RECT), {6: b"<< /N 5 0 R >>"}),
     False),
    ("n-by-reference", build(b"<< %s %s /AS /On /AP << /N 6 0 R >> >>" % (SQ, RECT),
                             {6: b"<< /On 5 0 R >>"}), False),
    ("direct", build(b"<< %s %s %s >>" % (SQ, RECT, AP), direct=True), False),
    ("hidden", build(b"<< %s %s /F 2 %s >>" % (SQ, RECT, AP)), False),
    ("noview", build(b"<< %s %s /F 32 %s >>" % (SQ, RECT, AP)), False),
    ("text", build(b"<< /Type /Annot /Subtype /Text %s %s >>" % (RECT, AP)), False),
    ("no-subtype-stored", build(b"<< /Type /Annot %s %s >>" % (RECT, AP)), False),
    ("need-on-a-square", build(b"<< %s %s %s >>" % (SQ, RECT, AP), acroform=NEED), False),
    ("ft-sig-on-a-square", build(b"<< %s /FT /Sig %s %s >>" % (SQ, RECT, AP), acroform=FIELDS),
     False),
    # No usable stored /AP: synthesized.
    ("no-ap", build(b"<< %s %s /C [0 0 1] >>" % (SQ, RECT)), True),
    ("ap-empty", build(b"<< %s %s /C [0 0 1] /AP << >> >>" % (SQ, RECT)), True),
    ("ap-d-only", build(b"<< %s %s /C [0 0 1] /AP << /D 5 0 R >> >>" % (SQ, RECT)), True),
    ("n-a-number", build(b"<< %s %s /C [0 0 1] /AP << /N 7 >> >>" % (SQ, RECT)), True),
    ("n-null", build(b"<< %s %s /C [0 0 1] /AP << /N null >> >>" % (SQ, RECT)), True),
    ("n-a-dictionary-not-a-stream", build(b"<< %s %s /C [0 0 1] /AP << /N 6 0 R >> >>"
                                          % (SQ, RECT), {6: b"<< /Length 3 >>"}), True),
    ("state-without-as", build(b"<< %s %s /C [0 0 1] /AP << /N << /On 5 0 R >> >> >>"
                               % (SQ, RECT)), True),
    ("as-names-no-entry", build(b"<< %s %s /C [0 0 1] /AS /Off /AP << /N << /On 5 0 R >> >> >>"
                                % (SQ, RECT)), True),
    ("as-a-string", build(b"<< %s %s /C [0 0 1] /AS (On) /AP << /N << /On 5 0 R >> >> >>"
                          % (SQ, RECT)), True),
    ("as-entry-null", build(b"<< %s %s /C [0 0 1] /AS /On /AP << /N << /On null >> >> >>"
                            % (SQ, RECT)), True),
    ("no-ap-hidden", build(b"<< %s %s /F 2 /C [0 0 1] >>" % (SQ, RECT)), True),
    ("freetext", build(b"<< /Type /Annot /Subtype /FreeText %s /Contents (SSN 123-45-6789)"
                       b" /DA (/Helv 9 Tf 0 g) >>" % RECT), True),
    ("freetext-rc", build(b"<< /Type /Annot /Subtype /FreeText %s /Contents (x) /DA (/Helv 9 Tf"
                          b" 0 g) /RC (<body><p>SSN 123-45-6789</p></body>) >>" % RECT), True),
    ("subtype-by-reference", build(b"<< /Type /Annot /Subtype 6 0 R %s >>" % RECT,
                                   {6: b"/Text"}), True),
    ("annotation-a-stream", b"", True),  # replaced below
    # Widgets: /NeedAppearances true replaces the stored /AP.
    ("widget-ap", build(b"<< %s %s %s >>" % (TX, RECT, AP), acroform=FIELDS), False),
    ("widget-no-ap", build(b"<< %s %s >>" % (TX, RECT), acroform=FIELDS), True),
    ("widget-need", build(b"<< %s %s %s >>" % (TX, RECT, AP), acroform=NEED), True),
    ("widget-need-by-reference", build(b"<< %s %s %s >>" % (TX, RECT, AP), {6: b"true"},
                                       b" /AcroForm << /Fields [4 0 R] /NeedAppearances 6 0 R"
                                       b" >>"), True),
    ("widget-acroform-by-reference", build(b"<< %s %s %s >>" % (TX, RECT, AP),
                                           {6: b"<< /Fields [4 0 R] /NeedAppearances true >>"},
                                           b" /AcroForm 6 0 R"), True),
    ("widget-need-1", build(b"<< %s %s %s >>" % (TX, RECT, AP),
                            acroform=b" /AcroForm << /Fields [4 0 R] /NeedAppearances 1 >>"),
     False),
    ("widget-need-false", build(b"<< %s %s %s >>" % (TX, RECT, AP),
                                acroform=b" /AcroForm << /Fields [4 0 R] /NeedAppearances false"
                                         b" >>"), False),
    ("widget-inherited-need", build(b"<< %s /Parent 6 0 R %s %s /DA (/Helv 9 Tf 0 g) >>"
                                    % (W, RECT, AP),
                                    {6: b"<< /FT /Tx /T (t) /V (SSN 123-45-6789) /Kids [4 0 R]"
                                        b" >>"},
                                    b" /AcroForm << /Fields [6 0 R] /NeedAppearances true >>"),
     True),
    ("checkbox-need", build(b"<< %s /FT /Btn /T (c) /V /On /AS /On %s /AP << /N << /On 5 0 R >>"
                            b" >> >>" % (W, RECT), acroform=NEED), True),
    ("pushbutton-need", build(b"<< %s /FT /Btn /Ff 65536 /T (b) %s %s /MK << /CA (Go) >> >>"
                              % (W, RECT, AP), acroform=NEED), True),
    ("combo-need", build(b"<< %s /FT /Ch /Ff 131072 /T (c) /V (a) /Opt [(a) (b)] /DA (/Helv 9"
                         b" Tf 0 g) %s %s >>" % (W, RECT, AP), acroform=NEED), True),
    # Signature fields: synthesized exactly when unsigned.
    ("sig-unsigned", build(b"<< %s %s %s >>" % (SIG, RECT, AP), acroform=FIELDS), True),
    ("sig-signed", build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                         {6: b"<< /Type /Sig /Filter /Adobe.PPKLite >>"}, FIELDS), False),
    ("sig-signed-need", build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                              {6: b"<< /Type /Sig >>"}, NEED), False),
    ("sig-v-without-type", build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                                 {6: b"<< /Filter /X >>"}, FIELDS), False),
    ("sig-v-a-stream", build(b"<< %s /V 5 0 R %s %s >>" % (SIG, RECT, AP), acroform=FIELDS),
     False),
    ("sig-v-direct", build(b"<< %s /V << /Type /Sig >> %s %s >>" % (SIG, RECT, AP),
                           acroform=FIELDS), False),
    ("sig-v-chain", build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                          {6: b"7 0 R", 7: b"<< /Type /Sig >>"}, FIELDS), False),
    ("sig-type-by-reference", build(b"<< %s /V << /Type 6 0 R >> %s %s >>" % (SIG, RECT, AP),
                                    {6: b"/Sig"}, FIELDS), False),
    ("sig-doc-timestamp", build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP),
                                {6: b"<< /Type /DocTimeStamp >>"}, FIELDS), True),
    ("sig-type-a-string", build(b"<< %s /V << /Type (Sig) >> %s %s >>" % (SIG, RECT, AP),
                                acroform=FIELDS), True),
    ("sig-v-a-string", build(b"<< %s /V (x) %s %s >>" % (SIG, RECT, AP), acroform=FIELDS), True),
    ("sig-hidden", build(b"<< %s /F 2 %s %s >>" % (SIG, RECT, AP), acroform=FIELDS), True),
    ("sig-inherited-signed", build(b"<< %s /Parent 6 0 R %s %s >>" % (W, RECT, AP),
                                   {6: b"<< /FT /Sig /T (s) /V 7 0 R /Kids [4 0 R] >>",
                                    7: b"<< /Type /Sig >>"},
                                   b" /AcroForm << /Fields [6 0 R] >>"), False),
    ("sig-inherited-unsigned", build(b"<< %s /Parent 6 0 R %s %s >>" % (W, RECT, AP),
                                     {6: b"<< /FT /Sig /T (s) /Kids [4 0 R] >>"},
                                     b" /AcroForm << /Fields [6 0 R] >>"), True),
    ("sig-own-v-null-parent-signed", build(b"<< %s /Parent 6 0 R /V null %s %s >>"
                                           % (W, RECT, AP),
                                           {6: b"<< /FT /Sig /T (s) /V << /Type /Sig >>"
                                               b" /Kids [4 0 R] >>"},
                                           b" /AcroForm << /Fields [6 0 R] >>"), False),
    ("sig-own-ft-null-parent-sig", build(b"<< %s /Parent 6 0 R /FT null %s %s >>"
                                         % (W, RECT, AP),
                                         {6: b"<< /FT /Sig /T (s) /Kids [4 0 R] >>"},
                                         b" /AcroForm << /Fields [6 0 R] >>"), True),
    ("sig-own-v-string-parent-signed", build(b"<< %s /Parent 6 0 R /V (x) %s %s >>"
                                             % (W, RECT, AP),
                                             {6: b"<< /FT /Sig /T (s) /V << /Type /Sig >>"
                                                 b" /Kids [4 0 R] >>"},
                                             b" /AcroForm << /Fields [6 0 R] >>"), True),
    ("sig-parent-direct", build(b"<< %s /Parent << /FT /Sig /T (s) >> %s %s >>"
                                % (W, RECT, AP), acroform=FIELDS), True),
    ("tx-own-over-parent-sig", build(b"<< %s /Parent 6 0 R /FT /Tx %s %s >>" % (W, RECT, AP),
                                     {6: b"<< /FT /Sig /T (s) /Kids [4 0 R] >>"},
                                     b" /AcroForm << /Fields [6 0 R] >>"), False),
    # /FT by reference is not /Sig to MuPDF's test: a widget with a stored /AP.
    ("sig-ft-by-reference", build(b"<< %s /FT 6 0 R /T (s) %s %s >>" % (W, RECT, AP),
                                  {6: b"/Sig"}, FIELDS), False),
    ("sig-ft-a-string", build(b"<< %s /FT (Sig) /T (s) %s %s >>" % (W, RECT, AP),
                              acroform=FIELDS), False),
    ("sig-deep-chain-unsigned", b"", True),  # replaced below
    ("sig-deep-chain-signed", b"", False),   # replaced below
]


def _chain(depth: int, top: bytes) -> bytes:
    """A widget whose /Parent chain is *depth* nodes long, *top* on the last."""
    extra = {10 + i: b"<< /T (f%d) /Kids [%d 0 R]%s %s >>" % (
        i, 4 if i == 0 else 9 + i, b" /Parent %d 0 R" % (11 + i) if i < depth - 1 else b"",
        top if i == depth - 1 else b"") for i in range(depth)}
    return build(b"<< %s /Parent 10 0 R %s %s >>" % (W, RECT, AP), extra,
                 b" /AcroForm << /Fields [%d 0 R] >>" % (9 + depth))


def _stream_annotation() -> bytes:
    """An annotation that is a stream object (MuPDF lists it: a stream's
    dictionary is a dictionary)."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                            b" /Resources << >> /Annots [4 0 R] >>"), 0)
    table[4] = (1, w.stream(4, b" " + SQ + b" " + RECT + b" /C [0 0 1]", b"xx"), 0)
    w.epilogue(w.table(table, b"/Size 5 /Root 1 0 R"))
    return bytes(w.out)


_REPLACED = {"annotation-a-stream": _stream_annotation(),
             "sig-deep-chain-unsigned": _chain(100, b"/FT /Sig"),
             "sig-deep-chain-signed": _chain(100, b"/FT /Sig /V << /Type /Sig >>")}
CASES = [(name, _REPLACED.get(name, data), expected) for name, data, expected in CASES]


@pytest.mark.parametrize(("data", "expected"), [(d, e) for _, d, e in CASES],
                         ids=[n for n, _, _ in CASES])
def test_each_condition_is_classified_as_mupdf_draws_it(data: bytes, expected: bool) -> None:
    inv = _inventory(data)
    assert inv.flags == (), inv.flags
    assert _ours(data) == [expected]
    # MuPDF's own drawing (the gate's reading) agrees: NOTHING agrees with both.
    found = list(readings(data).values())
    assert len(found) == 1 and found[0] in (NOTHING, SYNTHESIZED if expected else "STORED"), \
        found
    assert len(inv.synthesized[0]) == int(expected)


# An object whose value is a lone reference (`6 0 obj 7 0 R endobj`):
# MuPDF follows it, qpdf --check warns "expected endobj" and the inventory
# does not flag it -- outside this PR (reported); the gate disagrees.
GATED = [(n, d) for n, d, _ in CASES if n != "sig-v-chain"]


@requires_qpdf
@pytest.mark.parametrize("data", [d for _, d in GATED], ids=[n for n, _ in GATED])
def test_each_condition_agrees_with_the_gate(data: bytes, tmp_path: Path) -> None:
    found = compare(data, tmp_path)
    assert found.agrees and found.annotations_compared == 1, found


def test_links_popups_and_non_dictionaries_are_not_listed() -> None:
    for annot in (b"<< /Type /Annot /Subtype /Link %s >>" % RECT,
                  b"<< /Type /Annot /Subtype /Popup %s >>" % RECT, b"7", b"null"):
        assert _ours(build(annot)) == []
    # /Subtype resolved, as MuPDF resolves it.
    assert _ours(build(b"<< /Type /Annot /Subtype 6 0 R %s >>" % RECT, {6: b"/Link"})) == []
    assert readings(build(b"<< /Type /Annot /Subtype 6 0 R %s >>" % RECT, {6: b"/Link"})) == {}


def test_an_annotation_listed_twice_is_listed_twice_and_one_unit() -> None:
    data = build(b"<< %s %s /C [0 0 1] >>" % (SQ, RECT), also=b" 4 0 R")
    inv = _inventory(data)
    found = inv.annotations[0]
    assert found is not None and [(a.index, a.number) for a in found] == [(0, 4), (1, 4)]
    assert len(inv.synthesized[0]) == 1


def test_annotations_written_directly_are_one_unit_each_by_position() -> None:
    one = b"<< %s %s /C [0 0 1] >>" % (SQ, RECT)
    inv = _inventory(build(one, direct=True, also=b" " + one))
    units = inv.synthesized[0]
    page = inv.pages[0]
    assert page is not None
    assert units == (Synthesized(page[0].unit, 0), Synthesized(page[0].unit, 1))
    assert all(u.status is Status.UNEXAMINED for u in units)


@requires_qpdf
def test_several_annotations_on_a_page_agree_with_the_gate(tmp_path: Path) -> None:
    square = {6: b"<< %s /Rect [70 10 120 30] %s >>" % (SQ, AP),
              7: b"<< %s /Rect [130 10 180 30] /C [0 0 1] >>" % SQ}
    for acroform, expected in ((FIELDS, [False, False, True]), (NEED, [True, False, True])):
        data = build(b"<< %s %s %s >>" % (TX, RECT, AP), square, acroform,
                     also=b" 6 0 R 7 0 R << %s /Rect [10 40 60 60] /C [0 0 1] >>" % SQ)
        assert _ours(data) == [*expected, True]
        found = compare(data, tmp_path)
        assert found.agrees and found.annotations_compared == 4, found


# ── SYNTHESIZED units ──────────────────────────────────────────────────────
def test_units_are_per_body_and_revision_and_count_against_max_units() -> None:
    no_ap = b"<< %s %s /C [0 0 1] >>" % (SQ, RECT)
    data = build(no_ap, updates=({3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                                      b" /Resources << >> /Annots [4 0 R] /Rotate 90 >>"},))
    inv = _inventory(data)
    assert [len(u) for u in inv.synthesized] == [1, 1]
    flags = _inventory(data, Limits(max_units=1)).flags
    assert [(f.reason, dict(f.params)["counter"]) for f in flags] == [
        (FlagReason.BUDGET_EXHAUSTED, int(Counter.UNITS))]
    assert _inventory(data, Limits(max_units=2)).flags == ()


def test_a_revision_that_reads_nothing_new_shares_its_predecessors_units() -> None:
    no_ap = b"<< %s %s /C [0 0 1] >>" % (SQ, RECT)
    # The update rewrites an object the walk does not read (the catalog's
    # /AcroForm is the same as written): the result is reused.
    data = build(no_ap, {6: b"(x)"}, updates=({6: b"(y)"}, {1: b"<< /Type /Catalog /Pages 2 0 R"
                                                               b" /PageMode /UseNone >>"}))
    inv = _inventory(data)
    assert inv.annotations[0] is inv.annotations[2]
    assert inv.synthesized[0] is inv.synthesized[2]


@pytest.mark.parametrize(("update", "before", "after"), [
    # Form filling: /NeedAppearances set, /V signs the field, /AS changes.
    ({1: b"<< /Type /Catalog /Pages 2 0 R" + NEED + b" >>"}, False, True),
    ({6: b"<< /Type /Sig >>"}, True, False),
    ({4: b"<< %s %s /AS /Off /AP << /N << /On 5 0 R >> >> >>" % (SQ, RECT)}, False, True),
])
def test_form_filling_changes_the_classification_while_the_body_stays(
        update: dict[int, bytes], before: bool, after: bool) -> None:
    if 6 in update:
        annot, extra = b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP), {6: b"(unsigned)"}
    elif 4 in update:
        annot, extra = b"<< %s %s /AS /On /AP << /N << /On 5 0 R >> >> >>" % (SQ, RECT), {}
    else:
        annot, extra = b"<< %s %s %s >>" % (TX, RECT, AP), {}
    data = build(annot, extra, FIELDS, updates=(update,))
    inv = _inventory(data)
    assert inv.flags == ()
    assert (_ours(data, 1), _ours(data, 0)) == ([before], [after])
    assert [len(u) for u in inv.synthesized] == [int(after), int(before)]


# ── FIELD_TREE (owner decision P5) ─────────────────────────────────────────
@pytest.mark.parametrize(("data", "mupdf_fails"), [
    (build(b"<< %s /Parent 4 0 R %s %s >>" % (W, RECT, AP), acroform=FIELDS), True),
    (build(b"<< %s /Parent 10 0 R %s %s >>" % (W, RECT, AP),
           {10: b"<< /Parent 11 0 R >>", 11: b"<< /Parent 10 0 R /FT /Sig >>"},
           b" /AcroForm << /Fields [10 0 R] >>"), True),
    (build(b"<< %s /Parent << /Parent 4 0 R >> %s %s >>" % (W, RECT, AP), acroform=FIELDS),
     True),
    # MuPDF's lookups end below the cycle here (the widget has its own /FT,
    # /V absent... found nowhere is read through it); the field tree still
    # has no defined inheritance, and P5 flags every cycle.
    (build(b"<< %s /FT /Tx /T (t) /Parent 10 0 R %s %s >>" % (W, RECT, AP),
           {10: b"<< /Parent 10 0 R >>"}, FIELDS), False),
], ids=["self", "two-nodes", "through-a-direct-parent", "own-values-below-a-cycle"])
def test_a_parent_cycle_among_fields_flags_field_tree(data: bytes, mupdf_fails: bool) -> None:
    flags = [f for f in _inventory(data).flags if f.reason is FlagReason.FIELD_TREE]
    assert [dict(f.params)["rule"] for f in flags] == [int(FieldTreeRule.CYCLE)], flags
    if mupdf_fails:  # MuPDF fails on such a file ("cycle in tree")
        with pytest.raises(Exception):
            readings(data)


def test_a_cycle_persisting_through_revisions_flags_once() -> None:
    data = build(b"<< %s /Parent 4 0 R %s %s >>" % (W, RECT, AP), {6: b"1"}, FIELDS,
                 updates=({6: b"2"}, {6: b"3"}))
    found = [f for f in _inventory(data).flags if f.reason is FlagReason.FIELD_TREE]
    assert len(found) == 1


def test_a_square_parent_cycle_is_not_a_field() -> None:
    # MuPDF reads /Parent only up a widget's chain (a Popup's /Parent is its
    # markup annotation): no flag, and MuPDF draws it.
    data = build(b"<< %s /Parent 4 0 R %s %s >>" % (SQ, RECT, AP))
    assert _reasons(data) == set()
    assert list(readings(data).values()) == ["STORED"]


# ── APPEARANCE_UNSUPPORTED: MuPDF would synthesize it but cannot ───────────
@pytest.mark.parametrize(("annot", "acroform", "rule", "mupdf"), [
    (b"<< /Type /Annot /Subtype /Movie %s >>" % RECT, b"", AppearanceRule.SUBTYPE, NOTHING),
    (b"<< /Type /Annot /Subtype /Screen /F 2 %s >>" % RECT, b"", AppearanceRule.SUBTYPE,
     NOTHING),
    (b"<< /Type /Annot /Subtype (Square) %s >>" % RECT, b"", AppearanceRule.SUBTYPE, NOTHING),
    (b"<< %s /T (t) %s >>" % (W, RECT), FIELDS, AppearanceRule.FIELD_TYPE, NOTHING),
    (b"<< %s /FT /Foo /T (t) %s >>" % (W, RECT), FIELDS, AppearanceRule.FIELD_TYPE, NOTHING),
    # Under /NeedAppearances MuPDF fails and draws the stored /AP instead:
    # what ADR 0011 calls synthesized, MuPDF draws as stored.
    (b"<< %s /FT /Foo /T (t) %s %s >>" % (W, RECT, AP), NEED, AppearanceRule.FIELD_TYPE,
     "STORED"),
    (b"<< %s /FT 6 0 R /T (c) /V /On /AS /On %s /AP << /N << /On 5 0 R >> >> >>" % (W, RECT),
     NEED, AppearanceRule.FIELD_TYPE, "STORED"),
], ids=["movie", "screen-hidden", "subtype-a-string", "widget-no-ft", "widget-ft-foo",
        "need-ft-foo", "need-ft-btn-by-reference"])
def test_an_appearance_mupdf_cannot_build_is_flagged(
        annot: bytes, acroform: bytes, rule: AppearanceRule, mupdf: str) -> None:
    import pymupdf
    data = build(annot, {6: b"/Btn"}, acroform)
    flags = [dict(f.params) for f in _inventory(data).flags
             if f.reason is FlagReason.APPEARANCE_UNSUPPORTED]
    assert [f["rule"] for f in flags] == [int(rule)]
    pymupdf.TOOLS.mupdf_warnings()
    pymupdf.open(stream=data, filetype="pdf").load_page(0)
    assert "cannot create appearance stream" in pymupdf.TOOLS.mupdf_warnings()
    assert list(readings(data).values()) == [mupdf]


def test_an_unsupported_subtype_with_a_stored_appearance_is_not_flagged() -> None:
    data = build(b"<< /Type /Annot /Subtype /Movie %s %s >>" % (RECT, AP))
    assert _reasons(data) == set() and _ours(data) == [False]


# ── The gate catches a misclassification, a missing or an extra one ────────
def _with(inv: Inventory, change: object) -> Inventory:
    found = inv.annotations[0]
    assert found is not None
    return replace(inv, annotations=(change(found),) + inv.annotations[1:])  # type: ignore[operator]


@requires_qpdf
@pytest.mark.parametrize(("name", "change", "check"), [
    ("flipped", lambda found: tuple(replace(a, synthesized=not a.synthesized) for a in found),
     Check.ANNOTATION),
    ("missing", lambda found: found[1:], Check.ANNOTATION_LIST),
    ("extra", lambda found: found + found[:1], Check.ANNOTATION_LIST),
    ("renumbered", lambda found: tuple(replace(a, number=6) for a in found),
     Check.ANNOTATION_LIST),
    ("moved", lambda found: tuple(replace(a, page=1) for a in found), Check.ANNOTATION_LIST),
], ids=lambda v: v if isinstance(v, str) else "")
def test_the_gate_fails_closed_on_a_wrong_annotation_reading(
        tmp_path: Path, name: str, change: object, check: Check) -> None:
    for data in (build(b"<< %s %s %s >>" % (TX, RECT, AP), acroform=NEED),
                 build(b"<< %s %s %s >>" % (SQ, RECT, AP))):
        inv = _inventory(data)
        assert compare(data, tmp_path, inv=inv).agrees
        found = compare(data, tmp_path, inv=_with(inv, change))
        assert found.disagrees and found.check is check, (name, found)


@requires_qpdf
def test_the_gate_fails_closed_on_a_missed_widget_flag(tmp_path: Path) -> None:
    # A widget MuPDF cannot synthesize under /NeedAppearances draws its
    # stored /AP: were the inventory not to flag it, the gate disagrees.
    data = build(b"<< %s /FT /Foo /T (t) %s %s >>" % (W, RECT, AP), acroform=NEED)
    inv = _inventory(data)
    assert {f.reason for f in inv.flags} == {FlagReason.APPEARANCE_UNSUPPORTED}
    found = compare(data, tmp_path, inv=replace(inv, flags=()))
    assert found.disagrees and found.check in (Check.ANNOTATION, Check.MUPDF_WARNING), found


@requires_qpdf
def test_the_gate_fails_closed_on_a_missed_cycle(tmp_path: Path) -> None:
    data = build(b"<< %s /Parent 4 0 R %s %s >>" % (W, RECT, AP), acroform=FIELDS)
    inv = _inventory(data)
    found = compare(data, tmp_path, inv=replace(inv, flags=()))
    assert found.disagrees, found


@requires_qpdf
def test_the_gate_fails_closed_on_a_direct_annotations_position(tmp_path: Path) -> None:
    # Two annotations written directly in /Annots: keyed by position, so
    # ours naming the wrong position disagrees.
    one = b"<< %s %s %s >>" % (SQ, RECT, AP)
    data = build(one, direct=True, also=b" << %s /Rect [70 10 120 30] /C [0 0 1] >>" % SQ)
    inv = _inventory(data)
    assert compare(data, tmp_path, inv=inv).agrees
    for change in (lambda found: tuple(replace(a, index=a.index + 2) for a in found),
                   lambda found: (found[0], replace(found[1], index=0))):
        found = compare(data, tmp_path, inv=_with(inv, change))
        assert found.disagrees and found.check is Check.ANNOTATION_LIST, found
    # Their classifications are read by position too: swapped, they disagree.
    swapped = _with(inv, lambda found: (replace(found[0], synthesized=True),
                                        replace(found[1], synthesized=False)))
    found = compare(data, tmp_path, inv=swapped)
    assert found.disagrees and found.check is Check.ANNOTATION, found


def _two_pages(annots: tuple[bytes, bytes], objects: dict[int, bytes]) -> bytes:
    """Pages 3 and 7 listing *annots*; annotation objects *objects*."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    found = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
             2: b"<< /Type /Pages /Kids [3 0 R 7 0 R] /Count 2 >>"}
    for number, listed in zip((3, 7), annots):
        found[number] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >>"
                         b" /Annots [%s] >>" % listed)
    for n, body in sorted((found | objects).items()):
        table[n] = (1, w.obj(n, body), 0)
    table[5] = (1, w.stream(5, FORM, RED), 0)
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R" % (max(table) + 1)))
    return bytes(w.out)


@requires_qpdf
@pytest.mark.parametrize("synthesized", [False, True])
def test_an_annotation_listed_twice_and_on_two_pages_agrees_with_the_gate(
        tmp_path: Path, synthesized: bool) -> None:
    annot = b"<< %s %s %s >>" % (SQ, RECT, b"/C [0 0 1]" if synthesized else AP)
    # Once on each of two pages: both readers agree, and the gate compares
    # both listings (paired in MuPDF's copies by the tag of the first).
    data = _two_pages((b"4 0 R", b"4 0 R"), {4: annot})
    inv = _inventory(data)
    assert inv.flags == ()
    assert [(a.page, a.synthesized) for a in inv.annotations[0] or ()] == [
        (0, synthesized), (1, synthesized)]
    assert len(inv.synthesized[0]) == int(synthesized)  # one body, one unit
    found = compare(data, tmp_path)
    assert found.agrees and found.annotations_compared == 2, found
    found = compare(data, tmp_path, inv=_with(inv, lambda f: f[:-1]))  # one listing dropped
    assert found.disagrees and found.check is Check.ANNOTATION_LIST, found
    # Twice in one page's /Annots: MuPDF lists it twice, as we do, but qpdf
    # --check warns ("Annots has duplicate entry"): the gate fails closed.
    # qpdf's annotation and AcroForm analysis warnings are issue #144's.
    data = build(annot, also=b" 4 0 R")
    assert [a.index for a in _inventory(data).annotations[0] or ()] == [0, 1]
    found = compare(data, tmp_path)
    assert found.disagrees and found.check is Check.QPDF_CHECK, found


# ── Reuse across revisions without a page list (review of #140, B1) ────────
PAGES_OK = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGES_BAD = b"<< /Type /Pages /Kids [3 0 R] /Count 2 >>"


@pytest.mark.parametrize(("before", "middle", "synthesized"), [
    (b"<< /Type /Sig >>", b"(unsigned)", True),   # signed, then unsigned while unread
    (b"(unsigned)", b"<< /Type /Sig >>", False),  # the mirror
], ids=["signed-then-unsigned", "unsigned-then-signed"])
def test_a_revision_without_a_page_list_is_not_skipped_by_reuse(
        before: bytes, middle: bytes, synthesized: bool) -> None:
    # The middle revision's page tree is flagged (no page list, no walk) and
    # changes /V; the last restores the tree. The current revision must be
    # walked afresh, not reuse the oldest revision's reading.
    data = build(b"<< %s /V 6 0 R %s %s >>" % (SIG, RECT, AP), {6: before}, FIELDS,
                 updates=({2: PAGES_BAD, 6: middle}, {2: PAGES_OK}))
    inv = _inventory(data)
    assert inv.pages[1] is None
    assert (_ours(data, 2), _ours(data, 0)) == ([not synthesized], [synthesized])
    assert list(readings(data).values()) == [SYNTHESIZED if synthesized else "STORED"]


# ── The field type MuPDF builds, as written or by reference (D1) ──────────
@pytest.mark.parametrize(("ft", "where", "need"), [
    (ft, where, need) for ft in (b"Tx", b"Ch", b"Sig", b"Btn")
    for where in ("own", "parent", "chain") for need in (False, True)])
def test_a_field_type_by_reference_is_flagged_exactly_when_mupdf_cannot_build_it(
        ft: bytes, where: str, need: bool) -> None:
    import pymupdf
    extra = {b"Tx": b" /V (x) /DA (/Helv 9 Tf 0 g)",
             b"Ch": b" /V (a) /Opt [(a) (b)] /DA (/Helv 9 Tf 0 g)",
             b"Sig": b"", b"Btn": b" /V /On /AS /On"}[ft]
    ap = (b" /AP << /N << /On 5 0 R >> >>" if ft == b"Btn" else b" " + AP) if need else b""
    objects = {6: b"/" + ft}
    if where == "own":
        annot = b"<< %s /FT 6 0 R /T (f)%s %s%s >>" % (W, extra, RECT, ap)
        fields = b"4 0 R"
    else:
        if where == "chain":
            objects[7] = b"6 0 R"
        objects[8] = b"<< /FT %s /T (f) /Kids [4 0 R] >>" % (
            b"6 0 R" if where == "parent" else b"7 0 R")
        annot = b"<< %s /Parent 8 0 R%s %s%s >>" % (W, extra, RECT, ap)
        fields = b"8 0 R"
    data = build(annot, objects, b" /AcroForm << /Fields [%s]%s >>" % (
        fields, b" /NeedAppearances true" if need else b""))
    flags = [dict(f.params) for f in _inventory(data).flags]
    pymupdf.TOOLS.mupdf_warnings()
    mupdf = list(readings(data).values())
    warned = "cannot create appearance stream" in pymupdf.TOOLS.mupdf_warnings()
    # MuPDF builds /Tx, /Ch and /Sig through references, /Btn only as a name.
    assert warned == (ft == b"Btn")
    assert flags == ([{"rule": int(AppearanceRule.FIELD_TYPE), "object": 4 if where == "own"
                       else 8, "revision": 0}] if ft == b"Btn" else [])
    assert mupdf == (["STORED" if need else NOTHING] if ft == b"Btn" else [SYNTHESIZED])


def test_one_flag_per_cause_not_per_widget() -> None:
    # 50 widgets inheriting /FT /Foo from one field: one FIELD_TYPE flag, on
    # the field; 50 /Movie annotations: one flag each (each its own cause).
    kids = b" ".join(b"%d 0 R" % (20 + i) for i in range(50))
    objects = {8: b"<< /FT /Foo /T (f) /Kids [%s] >>" % kids}
    objects |= {20 + i: b"<< %s /Parent 8 0 R /T (k%d) %s >>" % (W, i, RECT) for i in range(50)}
    data = build(b"<< /Type /Annot /Subtype /Movie %s >>" % RECT, objects,
                 b" /AcroForm << /Fields [8 0 R] >>", also=b" " + kids)
    found = [dict(f.params) for f in _inventory(data).flags]
    assert found == [{"rule": int(AppearanceRule.SUBTYPE), "object": 4, "revision": 0},
                     {"rule": int(AppearanceRule.FIELD_TYPE), "object": 8, "revision": 0}]


# ── Linearity: many fields deep in a /Parent chain, over many revisions ────
def _fields(n: int, updates: int, touch: bool = True) -> bytes:
    """*n* widgets on one page, widget i a kid of field node i of a /Parent
    chain *n* nodes deep (the root field at the top holds /FT /Sig): the
    inherited lookups are n^2 / 2 without the memo. Each of *updates*
    revisions rewrites the root field's /V (*touch*: re-reads every
    widget) or an object the walk does not read."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    top = 10 + n - 1
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [%d 0 R]"
                            b" >> >>" % top), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    annots = b" ".join(b"%d 0 R" % (10 + n + i) for i in range(n))
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                            b" /Resources << >> /Annots [%s] >>" % annots), 0)
    table[5] = (1, w.stream(5, FORM, RED), 0)
    table[6] = (1, w.obj(6, b"(x)"), 0)
    for i in range(n):
        parent = b" /Parent %d 0 R" % (11 + i) if i < n - 1 else b" /FT /Sig"
        table[10 + i] = (1, w.obj(10 + i, b"<< /T (f%d)%s >>" % (i, parent)), 0)
        table[10 + n + i] = (1, w.obj(10 + n + i, b"<< %s /Parent %d 0 R %s %s >>"
                                      % (W, 10 + i, RECT, AP)), 0)
    size = 10 + 2 * n
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for i in range(updates):
        number, body = ((top, b"<< /T (f%d) /FT /Sig /V (%d) >>" % (n - 1, i)) if touch
                        else (6, b"(%d)" % i))
        at = w.obj(number, body)
        prev = w.table({number: (1, at, 0)}, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(prev)
    return bytes(w.out)


def test_a_deep_field_chain_is_walked_once() -> None:
    # The memo: n widgets, each a kid of its own node of an n-deep chain,
    # cost linear work (not n^2 / 2), and no hidden work: the lines executed
    # per unit of work charged stay under a fixed bound (#94).
    work = []
    for n in (100, 800):
        charged, lines, inv = charged_steps(_fields(n, 0), "build_annotations")
        assert inv.flags == ()
        assert len(inv.synthesized[0]) == n  # unsigned signature fields
        work.append((charged, lines))
    _per_unit(work)
    assert work[1][0] <= 10 * work[0][0], work


def test_revisions_that_read_nothing_new_cost_no_walk() -> None:
    work = []
    for n in (40, 320):
        charged, lines, inv = charged_steps(_fields(n, n, touch=False), "build_annotations")
        assert inv.flags == ()
        assert all(found is inv.annotations[0] for found in inv.annotations)
        work.append((charged, lines))
    _per_unit(work)
    assert work[1][0] <= 10 * work[0][0], work


def test_refilling_a_deep_form_over_many_revisions_is_charged_and_bounded() -> None:
    # Each update rewrites the root field: every widget is read again,
    # revisions x widgets, by design (ADR 0011 item 7) -- all of it charged
    # (the work grows as the product), and the budget bounds it: flagged,
    # never hanging. The units too: one per widget and revision.
    work = []
    for n in (40, 320):
        data = _fields(n, n)
        charged, lines, inv = charged_steps(data, "build_annotations")
        reasons = {f.reason for f in inv.flags}
        assert reasons <= {FlagReason.BUDGET_EXHAUSTED}, reasons
        if FlagReason.BUDGET_EXHAUSTED not in reasons:
            assert sum(len(u) for u in inv.synthesized) == n * (n + 1)
        work.append((charged, lines))
        size = len(data)
    _per_unit(work)
    (w_s, _), (w_l, _) = work
    assert w_l >= 16 * w_s or w_l >= 0.99 * Limits().work_limit(size), work


def _odd_annots(n: int) -> bytes:
    """One page whose /Annots holds *n* integers and *n* references to the
    head of a nine-reference chain ending in one Square: each item walked,
    each hop followed (review of #140: mutants m2 and m8)."""
    chain = {20 + i: b"%d 0 R" % (21 + i) for i in range(8)}
    chain[28] = b"<< %s %s %s >>" % (SQ, RECT, AP)
    items = b" ".join(b"7 20 0 R" for _ in range(n))
    return build(b"<< %s %s %s >>" % (SQ, RECT, AP), chain, also=b" " + items)


def _int_annots(n: int) -> bytes:
    """One page whose /Annots holds a Square and *n* integers (mutant m2:
    items no reader lists still cost a step each)."""
    return build(b"<< %s %s %s >>" % (SQ, RECT, AP), also=b" " + b" ".join([b"7"] * n))


def _rewritten_tree(n: int) -> bytes:
    """*n* pages, *n* revisions each rewriting the page-tree root as it was:
    the page records stay, the annotation walk is reused, and comparing the
    page lists is charged (mutant m9)."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    kids = b" ".join(b"%d 0 R" % (10 + i) for i in range(n))
    root = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), 0)
    table[2] = (1, w.obj(2, root), 0)
    for i in range(n):
        table[10 + i] = (1, w.obj(10 + i, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9]"
                                         b" /Resources << >> >>"), 0)
    size = 10 + n
    prev = w.table(table, b"/Size %d /Root 1 0 R" % size)
    w.epilogue(prev)
    for _ in range(n):
        prev = w.table({2: (1, w.obj(2, root), 0)}, b"/Size %d /Root 1 0 R /Prev %d" % (size, prev))
        w.epilogue(prev)
    return bytes(w.out)


@pytest.mark.parametrize("make", [_odd_annots, _int_annots, _rewritten_tree],
                         ids=["chains", "integers", "tree"])
def test_every_item_hop_and_page_comparison_is_charged(make: object) -> None:
    work = []
    for n in (40, 320):
        charged, lines, inv = charged_steps(make(n), "build_annotations")  # type: ignore[operator]
        assert inv.flags == (), inv.flags
        work.append((charged, lines))
    _per_unit(work)


def _per_unit(work: list[tuple[int, int]]) -> None:
    """(work charged, lines executed) at n and 8n: lines per charged unit
    under a fixed bound, and not growing with the input (#94; review of
    #140)."""
    (w_s, l_s), (w_l, l_l) = work
    assert l_s <= LINES_PER_UNIT * w_s and l_l <= LINES_PER_UNIT * w_l, work
    assert l_l / w_l <= PER_UNIT_GROWTH * (l_s / w_s), work


def test_build_annotations_runs_on_its_own() -> None:
    # No page list: nothing read, no unit, no flag.
    assert build_annotations(1, [[]], lambda n, r: None, lambda n, r: None, [None], (None,),
                             Budget()) == ((None,), ((),), [])
