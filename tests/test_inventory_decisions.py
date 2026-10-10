"""3a-6c: the owner's pending decisions (owner decision 7, 2026-10-03,
docs/phase3a-plan.md), each with a reader-agreement test:

- an incremental update to a linearized file is canonical: the linearized
  pair is the base revision (#44 item 1);
- an in-use xref entry at offset 0 is no object, as both readers read it;
- a claimed comment line with control bytes or an `N G obj` is flagged
  COMMENT_LINE (#46 item 1).

Every file is generated in tmp_path; fabricated content only."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pymupdf
import pytest
from scorecard import inventory as oracle
from scorecard.inventory import Check, compare
from scorecard.pdfgen import Spec, Writer, build_pdf

from redaction_verifier.ledger import FlagReason

from .conftest import requires_qpdf
from .test_inventory_xref import classic


def _reasons(data: bytes) -> set[FlagReason]:
    return {flag.reason for flag in oracle.inventory(data).flags}


# ── Updates to a linearized file (#44 item 1) ─────────────────────────────
def _linearized(tmp_path: Path, objstm: bool) -> bytes:
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page().insert_text((50, 50), f"Fabricated page {i}")
    doc.save(tmp_path / "in.pdf")
    subprocess.run(["qpdf", "--linearize", "--object-streams=" + ("generate" if objstm
                                                                  else "disable"),
                    str(tmp_path / "in.pdf"), str(tmp_path / "lin.pdf")], check=True, timeout=60)
    return (tmp_path / "lin.pdf").read_bytes()


def _update(tmp_path: Path, text: str) -> bytes:
    doc = pymupdf.open(tmp_path / "lin.pdf")
    doc[0].insert_text((50, 90), text)
    doc.save(tmp_path / "lin.pdf", incremental=True, encryption=0)
    return (tmp_path / "lin.pdf").read_bytes()


@requires_qpdf
@pytest.mark.parametrize("objstm", [False, True])
@pytest.mark.parametrize("updates", [1, 2])
def test_updates_to_a_linearized_file_agree_with_the_readers(
        tmp_path: Path, objstm: bool, updates: int) -> None:
    _linearized(tmp_path, objstm)
    for n in range(updates):
        data = _update(tmp_path, f"update {n}")
    chain = oracle.chain_of(data)
    assert chain.flags == () and len(chain.revisions) == updates + 1
    assert len(chain.revisions[-1]) == 2  # the linearized pair, the base
    found = compare(data, tmp_path)
    assert found.agrees and found.values_compared > 0, found


@requires_qpdf
def test_an_updated_file_whose_linearization_names_its_whole_length_is_flagged(
        tmp_path: Path) -> None:
    # Found in review (#53): /L equal to the updated file's length makes a
    # reader that trusts linearization read the stale first-page section
    # (qpdf --check calls it linearized and errs). Flagged.
    _linearized(tmp_path, objstm=False)
    data = _update(tmp_path, "update")
    lin_l = re.search(rb"/L (\d+)", data)
    assert lin_l is not None
    width = len(lin_l.group(1))
    forged = data[:lin_l.start(1)] + b"%0*d" % (width, len(data)) + data[lin_l.end(1):]
    assert len(forged) == len(data) and len(str(len(data))) == width
    assert FlagReason.XREF_TABLE_MALFORMED in _reasons(forged)
    assert oracle.inventory(data).flags == ()  # the honest /L is unflagged


# A pair that is not the base (its main section has a /Prev of its own) is
# still flagged: tests/test_inventory_xref.py's
# test_a_forward_prev_outside_the_linearized_shape_is_flagged.


# ── In-use entries at offset 0 ─────────────────────────────────────────────
def _offset_zero(numbers: tuple[int, ...] = (4, 5), extra: bytes = b"") -> bytes:
    """Catalog 1, pages 2, page 3, and in-use entries at offset 0 for
    *numbers*; *extra* goes into the catalog."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R%s >>" % extra), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9]"
                          b" /Resources << >> >>"), 0)
    table |= {n: (1, 0, 0) for n in numbers}
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R" % (max(table) + 1)))
    return bytes(w.out)


def test_an_in_use_entry_at_offset_zero_is_no_object() -> None:
    data = _offset_zero()
    assert oracle.chain_of(data).flags == () and _reasons(data) == set()
    assert oracle.inventory(data).body(4) is None


def test_object_zero_at_offset_zero_is_still_flagged() -> None:
    zero = classic().replace(b"0000000000 65535 f \n", b"0000000000 00000 n \n", 1)
    assert FlagReason.XREF_OFFSET_MISMATCH in _reasons(zero)


def test_a_catalog_named_at_offset_zero_is_missing() -> None:
    # Found in review (#53): the catalog check read the first object after
    # offset 0 -- the real catalog, object 1 -- and took it for object 4.
    data = _offset_zero().replace(b"/Root 1 0 R", b"/Root 4 0 R")
    assert FlagReason.MISSING_ROOT in _reasons(data)


@pytest.mark.parametrize("home", [4, 9])  # numbered below and above its member
def test_an_object_stream_home_at_offset_zero_is_flagged(home: int) -> None:
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9]"
                          b" /Resources << >> >>"), 0)
    table[home] = (1, 0, 0)
    table[6] = (2, home, 0)
    w.epilogue(w.xref_stream(7, table, b"/Size %d /Root 1 0 R" % (max(table) + 2)))
    assert FlagReason.XREF_OFFSET_MISMATCH in _reasons(bytes(w.out))


@requires_qpdf
@pytest.mark.parametrize("extra", [b"", b" /Probe 4 0 R"])
def test_offset_zero_entries_read_as_null_in_both_readers(tmp_path: Path, extra: bytes) -> None:
    # Referenced or not, both readers read null (a dangling reference is
    # the reference graph's to flag, 3a-7).
    data = _offset_zero(extra=extra)
    found = compare(data, tmp_path)
    assert found.agrees, found


@requires_qpdf
def test_an_offset_zero_entry_a_reader_reads_is_a_disagreement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = pymupdf.Document.xref_object

    def reads_something(self: pymupdf.Document, n: int, compressed: bool = False) -> str:
        return "<< /Found 1 >>" if n == 4 else real(self, n, compressed=compressed)
    monkeypatch.setattr(pymupdf.Document, "xref_object", reads_something)
    found = compare(_offset_zero(), tmp_path)
    assert (found.check, found.obj) == (Check.OBJECT_SET, 4)


# ── Comment lines (#46 item 1) ────────────────────────────────────────────
@pytest.mark.parametrize(("line", "flagged"), [
    (b"% Written by a test", False),
    (b"% 4 0 obj", True),                 # a repairing reader could take it for a header
    (b"% 12  0\tobj << >>", True),
    (b"% bell \x07 here", True),          # a control byte
    (b"% caf\xc3\xa9", True),             # past the binary marker: high bytes too
    (b"% tab\tis fine", False),
])
def test_odd_comment_lines_are_flagged(line: bytes, flagged: bool) -> None:
    data = build_pdf(Spec(comment=True)).replace(b"% Written by a test", line, 1)
    assert (FlagReason.COMMENT_LINE in _reasons(data)) is flagged


def test_the_binary_marker_is_not_an_odd_comment() -> None:
    data = build_pdf(Spec())
    assert data.split(b"\n")[1].startswith(b"%\xe2")  # the marker line
    assert _reasons(data) == set()


@requires_qpdf
def test_a_flagged_comment_line_is_still_read_alike(tmp_path: Path) -> None:
    # Flagging is fail-closed, not a claim the readers disagree: the
    # flagged file is never compared, and the clean one agrees.
    clean = build_pdf(Spec(comment=True))
    assert compare(clean, tmp_path).agrees
    odd = clean.replace(b"% Written by a test", b"% 4 0 obj", 1)
    assert compare(odd, tmp_path).flagged


def test_a_long_digit_comment_is_linear() -> None:
    # Found in review (#53): `\d+\s+\d+\s+obj` searched from every
    # position of a digit run backtracked through all of it (quadratic).
    def build(n: int) -> float:
        data = classic().replace(b"%PDF-1.7\n", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n%" + b"1" * n
                                 + b"\n", 1)
        start = time.perf_counter()
        oracle.inventory(data)
        return time.perf_counter() - start
    build(1000)
    small, large = build(100_000), build(800_000)
    assert large < 1.0 and large < 20 * max(small, 0.005), (small, large)
