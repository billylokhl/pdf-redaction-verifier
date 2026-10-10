"""3a-6b: the agreement gate compares values (ADR 0010 item 2, amended):
every live object, object-stream member and trailer, against MuPDF and
against libqpdf's own reading (pikepdf); and the gate's written allowlist
of qpdf messages that are not disagreements, each with a reader-agreement
test. Every file is generated in tmp_path; fabricated content only."""

from __future__ import annotations

import re
import subprocess
import zlib
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from scorecard import cli
from scorecard import inventory as oracle
from scorecard.inventory import (Check, compare, float32_ordinal, same_as_mupdf,
                                 structural)
from scorecard.pdfgen import Spec, Update, Writer, build_pdf

from redaction_verifier.ledger import FlagReason

from .conftest import requires_qpdf

SSN = b"123-45-6789"
OBJSTM = Spec(xref="stream", objstm=True, updates=(Update("stream", objstm=True),))


def _file(catalog: bytes = b"", page: bytes = b"", pages: bytes = b"",
          trailer: bytes = b"", extra: dict[int, bytes] | None = None,
          content: bytes | None = None, content_extra: bytes = b"") -> bytes:
    """Catalog 1, pages 2, page 3, content 4 (fabricated text), then
    *extra* objects; one classic xref section."""
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R%s >>" % catalog), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1%s >>" % pages), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
                          b" /Contents 4 0 R%s >>" % page), 0)
    data = content if content is not None else b"BT /F1 9 Tf 9 9 Td (SSN " + SSN + b") Tj ET"
    table[4] = (1, w.stream(4, content_extra, data), 0)
    for number, body in (extra or {}).items():
        table[number] = (1, w.obj(number, body), 0)
    w.epilogue(w.table(table, b"/Size %d /Root 1 0 R%s" % (max(table) + 1, trailer)))
    return bytes(w.out)


CLEAN = _file(page=b" /Resources << >>")


# ── Values are compared, with both readers ────────────────────────────────
@requires_qpdf
@pytest.mark.parametrize("data", [CLEAN, build_pdf(OBJSTM)], ids=["classic", "objstm"])
def test_every_object_and_trailer_is_compared(tmp_path: Path, data: bytes) -> None:
    found = compare(data, tmp_path)
    objects = len(oracle.qpdf_map(_write(tmp_path, data))[0])
    assert found.agrees and found.values_compared >= objects + 1, found


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "probe.pdf"
    path.write_bytes(data)
    return path


@requires_qpdf
def test_each_value_check_fires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def check(target: object, name: str, value: object, data: bytes = CLEAN) -> Check | None:
        with monkeypatch.context() as patch:
            patch.setattr(target, name, value)
            found = compare(data, tmp_path)
        assert found.disagrees, found
        return found.check

    assert compare(CLEAN, tmp_path).agrees  # the positive control
    assert check(oracle, "libqpdf_form", lambda obj, top=False: ("int", -1)) in (
        Check.OBJECT_VALUE, Check.TRAILER_VALUE)
    assert check(pikepdf.Pdf, "get_object", lambda self, objgen: None) is Check.OBJECT_VALUE
    assert check(pikepdf.Pdf, "get_warnings",
                 lambda self: ["probe.pdf: something"]) is Check.LIBQPDF_WARNING
    assert check(pymupdf.Document, "xref_object",
                 lambda self, n, compressed=False: "<< /Other 1 >>") is Check.OBJECT_VALUE
    assert check(pymupdf.Document, "xref_object", lambda self, n, compressed=False:
                 "<< /Other 1 >>", build_pdf(OBJSTM)) in (Check.OBJECT_VALUE, Check.MEMBER_VALUE)
    assert check(pymupdf.Document, "pdf_trailer",
                 lambda self, compressed=False: "<< /Size 1 >>") is Check.TRAILER_VALUE
    assert check(oracle, "qpdf_warnings",
                 lambda path: [b"ERROR: probe.pdf: an error"]) is Check.QPDF_CHECK


@requires_qpdf
def test_a_qpdf_cli_unlike_libqpdf_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(oracle, "qpdf_versions", lambda: ("11.9.0", "12.4.2"))
    found = compare(CLEAN, tmp_path)
    assert (found.check, found.templates) == (Check.ORACLE_ERROR, ("qpdf version mismatch",))
    assert cli.main(["inventory", "fuzz", "--count", "1", "--json", str(tmp_path / "a.json"),
                     "--detail", str(tmp_path / "d.jsonl"), "--allow-outside"]) == 2
    assert "one qpdf major.minor version" in capsys.readouterr().err


# ── What the value comparison sees that the old gate did not ──────────────
@requires_qpdf
@pytest.mark.parametrize(("data", "check"), [
    # MuPDF resolves a reference by its number; libqpdf reads `1 5 R` as
    # null. Unflagged until the reference graph (3a-7): the gate sees it.
    (_file(catalog=b" /Probe 1 5 R", page=b" /Resources << >>"), Check.OBJECT_VALUE),
    # A trailer is not an object: an old-generation /Info reaches MuPDF's
    # metadata (a fabricated author) and not libqpdf's.
    (_file(page=b" /Resources << >>", trailer=b" /Info 5 3 R",
           extra={5: b"<< /Author (SSN " + SSN + b") >>"}), Check.TRAILER_VALUE),
])
def test_a_generation_mismatch_is_a_disagreement(tmp_path: Path, data: bytes,
                                                 check: Check) -> None:
    assert oracle.inventory(data).flags == ()
    assert compare(data, tmp_path).check is check


@requires_qpdf
@pytest.mark.parametrize(("entry", "seen"), [
    (b" /Matrix [0.00000000000000000000000000000000000000000000001 0 0 1 0 0]", True),
    (b" /Pg 18446744073386459286 0 R", True),  # qpdf: the whole object null
    # Readers that differ only within MuPDF's 32-bit reals (2^31 for
    # 2147483648.5) or only in use (MuPDF's pdf_to_int truncates /Rotate
    # to 90) look alike to the value comparison: the flag is what sees them.
    (b" /BBox [0 0 2147483648.5 1000]", False),
    (b" /Rotate 4294967386", False),
])
def test_numbers_the_readers_read_differently_are_flagged(
        tmp_path: Path, entry: bytes, seen: bool) -> None:
    data = _file(page=b" /Resources << >>" + entry)
    inv = oracle.inventory(data)
    assert FlagReason.NUMBER_OUT_OF_RANGE in {f.reason for f in inv.flags}
    assert compare(data, tmp_path, inv=inv).flagged
    # Without the flag, the oracle itself sees the first two disagree.
    assert compare(data, tmp_path, inv=replace(inv, flags=())).disagrees is seen


@requires_qpdf
def test_inherited_page_attributes_are_read_as_written(tmp_path: Path) -> None:
    # pikepdf's default open pushes /Resources and /Rotate from /Pages down
    # to the page (new objects, rewritten page dictionaries); the oracle
    # opens without it, so libqpdf's values are the file's own.
    data = _file(pages=b" /Resources << /ProcSet [/PDF] >> /Rotate 90")
    found = compare(data, tmp_path)
    assert found.agrees and found.values_compared >= 5, found


@requires_qpdf
@pytest.mark.parametrize("text", [b"(A)", b"<FEFF0041>", b"(\\377\\376A\\000)", b"<EFBBBF41>",
                                  b"/A#FF", b"/#E9", b"4.", b"-.5", b"+.5", b"-007.5",
                                  b"66.089836", b"16777215.5", b"0.123456789012345678"])
def test_strings_names_and_numbers_agree_exactly(tmp_path: Path, text: bytes) -> None:
    # qpdf's JSON would print the four strings alike ("u:A") and some reals
    # as invalid JSON; libqpdf's own values are exact.
    data = _file(catalog=b" /Probe " + text, page=b" /Resources << >>")
    assert oracle.inventory(data).flags == ()
    assert compare(data, tmp_path).agrees


@requires_qpdf
@pytest.mark.parametrize("key", [b"/A#FF", b"/#E9", b"/A#20B"])
def test_keys_that_are_not_utf8_are_compared(tmp_path: Path, key: bytes) -> None:
    # Found by the fuzz gate: pikepdf returns such a key surrogate-escaped
    # and will not look it up again (the oracle failed closed, ORACLE_ERROR).
    data = _file(catalog=b" " + key + b" (x)", page=b" /Resources << >>",
                 extra={5: b"<< /Length 3 " + key + b" 7 >>\nstream\nxyz\nendstream"})
    found = compare(data, tmp_path)
    assert found.agrees and found.values_compared >= 6, found


# ── Reals: MuPDF holds 32-bit floats ──────────────────────────────────────
def test_float32_rounds_once_from_the_exact_value() -> None:
    one = float32_ordinal(Fraction(1))
    assert float32_ordinal(1 + Fraction(1, 2 ** 24) + Fraction(1, 2 ** 60)) == one + 1
    assert float32_ordinal(1 + Fraction(1, 2 ** 24)) == one  # a tie: to even
    assert float32_ordinal(Fraction(0)) == float32_ordinal(-Fraction(0)) == 0
    assert float32_ordinal(Fraction(1, 2 ** 149)) == 1      # the smallest subnormal
    assert float32_ordinal(-Fraction(1, 2 ** 149)) == -1
    assert float32_ordinal(Fraction(2 ** 128)) == 0xFF << 23


@pytest.mark.parametrize(("ours", "mupdf", "same"), [
    (b"100000004.5", b"100000000", True),   # MuPDF's conversion: one step off
    (b"100000004.5", b"100000024", False),  # two steps (one float32 step is 8)
    (b"66.089836", b"66.089839", True), (b"5.0", b"5", True), (b"1", b"1.0", False),
    (b"-0.0", b"0", True), (b"0.5", b"-0.5", False),
])
def test_a_real_may_be_one_float32_step_from_mupdf(ours: bytes, mupdf: bytes, same: bool) -> None:
    assert same_as_mupdf(oracle.text_form(ours, ()), oracle.text_form(mupdf, ())) is same


# ── The written allowlist of qpdf messages ────────────────────────────────
def test_the_allowlist_matches_whole_lines_on_the_exact_path(tmp_path: Path) -> None:
    path = tmp_path / "r0.pdf"
    lint = b"WARNING: %s: first page object offset mismatch" % str(path).encode()
    assert structural([lint], path, linearized=True) == []
    assert structural([lint], path) == [lint]                               # not linearized
    assert structural([lint.replace(b"r0.pdf", b"r1.pdf")], path, linearized=True)
    assert structural([lint + b" (offset 5)"], path, linearized=True)        # not whole
    cut = b"WARNING: %s (offset 77): input stream is complete but output may still be valid"
    assert structural([cut % str(path).encode()], path, undecoded={77}) == []
    assert structural([cut % str(path).encode()], path, undecoded={78})
    zero = b"WARNING: %s (object 3 0, offset 9): treating bad indirect reference (%s R) as null"
    assert structural([zero % (str(path).encode(), b"0 0")], path) == []
    assert structural([zero % (str(path).encode(), b"1 65535")], path)
    assert structural([b"ERROR: %s: an error" % str(path).encode()], path)


def _linearized(tmp_path: Path, objstm: bool) -> bytes:
    doc = pymupdf.open()
    for i in range(5):
        doc.new_page().insert_text((72, 72), f"Fabricated page {i}")
    doc.set_toc([[1, f"Chapter {i}", i + 1] for i in range(5)])
    doc.save(tmp_path / "base.pdf")
    subprocess.run(["qpdf", "--linearize", "--compress-streams=n", "--decode-level=none",
                    "--object-streams=" + ("generate" if objstm else "disable"),
                    str(tmp_path / "base.pdf"), str(tmp_path / "lin.pdf")], check=True,
                   timeout=60)
    return (tmp_path / "lin.pdf").read_bytes()


@requires_qpdf
@pytest.mark.parametrize("objstm", [False, True])
def test_hint_table_lint_leaves_both_readers_objects_unchanged(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, objstm: bool) -> None:
    """Corrupt the (uncompressed) hint stream one bit at a time. Every
    variant whose only qpdf messages are linearization lint agrees with
    both readers -- their objects are the clean file's, the hint stream
    aside -- and the lint shapes reached are counted, so the allowlist is
    exercised, not vacuous."""
    monkeypatch.setattr(oracle, "QPDF_TIMEOUT", 20.0)
    data = _linearized(tmp_path, objstm)
    assert compare(data, tmp_path).agrees
    hint = re.search(rb"/H \[ ?(\d+) (\d+)", data)
    assert hint is not None
    head = re.compile(rb"stream\r?\n").search(data, int(hint.group(1)))
    length = re.compile(rb"/Length (\d+)").search(data, int(hint.group(1)))
    assert head is not None and length is not None
    start = head.end()
    clean = _objects(tmp_path, data)
    shapes: set[int] = set()
    for at in range(4, int(length.group(1))):  # bytes 0-3 can make --check run for minutes
        flipped = bytearray(data)
        flipped[start + at] ^= 0x01
        path = _write(tmp_path, bytes(flipped))
        try:
            lines = oracle.qpdf_warnings(path)
        except subprocess.TimeoutExpired:
            continue
        lint = [line for line in lines if line.startswith(b"WARNING")
                and not structural([line], path, linearized=True)]
        if not lint or structural(lines, path, linearized=True):
            continue
        for line in lint:
            shapes |= {i for i, p in enumerate(oracle.LINEARIZATION_LINT)
                       if p.fullmatch(line.split(b": ", 2)[2])}
        assert compare(bytes(flipped), tmp_path).agrees, at
        changed = {n for n, v in _objects(tmp_path, bytes(flipped)).items() if clean.get(n) != v}
        assert len(changed) <= 1, (at, changed)  # the hint stream only
    assert len(shapes) >= 5, shapes


def _objects(tmp_path: Path, data: bytes) -> dict[int, tuple[str, bytes]]:
    """Both readers' objects, MuPDF's printed and libqpdf's unparsed."""
    path = _write(tmp_path, data)
    doc = pymupdf.open(path)
    with pikepdf.Pdf.open(path, attempt_recovery=False, inherit_page_attributes=False) as pdf:
        return {n: (doc.xref_object(n), pdf.get_object((n, 0)).unparse())
                for n in range(1, doc.xref_length())
                if pdf.get_object((n, 0)) is not None}


@requires_qpdf
@pytest.mark.parametrize("cut", [4, 9])
def test_truncated_flate_content_is_the_content_decoders(tmp_path: Path, cut: int) -> None:
    # A content stream whose zlib data stops early (before its checksum, or
    # mid-block): both readers give the same partial output; the raw bytes
    # are compared; decoding content is Phase 4's.
    packed = zlib.compress(b"\n".join([b"BT /F1 9 Tf 9 9 Td (SSN " + SSN + b") Tj ET"] * 20))
    packed = packed[:-cut]
    data = _file(page=b" /Resources << >>", content=packed, content_extra=b" /Filter /FlateDecode")
    lines = oracle.qpdf_warnings(_write(tmp_path, data))
    assert any(b"input stream is complete" in line for line in lines)
    assert oracle.inventory(data).flags == ()
    assert compare(data, tmp_path).agrees


@requires_qpdf
def test_a_reference_to_object_zero_reads_null_in_both_readers(tmp_path: Path) -> None:
    # In an object qpdf --show-xref does not read (in the catalog it would
    # warn there too, exit 3: an OBJECT_SET disagreement, fail closed).
    data = _file(page=b" /Resources << >>", extra={5: b"<< /Probe [0 0 R] /K 0 0 R >>"})
    assert oracle.inventory(data).flags == ()
    assert compare(data, tmp_path).agrees
