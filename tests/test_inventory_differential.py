"""Differential fuzzing of the inventory's parser against real readers
(Phase 3a-3b, docs/phase3a-plan.md).

Hypothesis builds small, otherwise valid PDFs whose stream object varies
in every framing detail readers are known to disagree on, then reads the
object with our parser, MuPDF and qpdf. The invariant is REDESIGN §4's
"ambiguity is flagged, never resolved silently": whenever our parser
raises no flag for the object, both readers must agree with us, byte for
byte, without repairing the file or warning. A flagged object may
disagree -- that is what the flag is for.

This replaces finding reader disagreements one review at a time. The
example count is small in CI; for a long local run set
DIFF_FUZZ_EXAMPLES (e.g. 20000).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from redaction_verifier.inventory.objects import IndirectObject, ObjectParser, PdfInt

from .conftest import requires_qpdf

EXAMPLES = int(os.environ.get("DIFF_FUZZ_EXAMPLES", "150"))
STREAM_OBJ = 4
LENGTH_OBJ = 5

# Bytes a stream's data or framing is drawn from: what readers disagree on.
_DATA = st.lists(st.sampled_from([
    b"a", b"Z", b"0", b" ", b"\x00", b"\t", b"\x0c", b"\r", b"\n", b"\r\n", b"%",
    b"(", b")", b"<", b">", b"/", b"\\", b"endstream", b"endobj", b"stream", b"obj",
    b"endstreamX", b"4 0 obj", b"\xff", b"\x80",
]), max_size=12).map(b"".join)
_EOL = st.sampled_from([b"\r\n", b"\n", b"\r", b"", b" ", b" \n", b"\r\r\n", b"\n\n"])
_TAIL = st.sampled_from([
    b"endstream\nendobj", b"endstream endobj", b"endstreamendobj", b"endstream\r\nendobj",
    b"endstream%c\nendobj", b"endstream", b"endstream\n", b"endstreamX endobj",
    b"endstream\n4 0 obj", b"",
])


@dataclass(frozen=True)
class StreamCase:
    header: bytes      # the stream dictionary's text
    eol: bytes         # after the `stream` keyword
    data: bytes        # the bytes meant as the stream's data
    gap: bytes         # between the data and the tail
    tail: bytes        # endstream / endobj framing
    length_value: int  # what object 5 holds, for an indirect /Length


@st.composite
def _stream_cases(draw: st.DrawFn) -> StreamCase:
    data = draw(_DATA)
    exact = len(data)
    length = draw(st.sampled_from([
        b"/Length %d" % exact, b"/Length %d" % exact, b"/Length %d" % exact,
        b"/Length %d" % max(exact - 1, 0), b"/Length %d" % (exact + 1),
        b"/Length %d" % (exact + 20), b"/Length 0", b"", b"/Length -1",
        b"/Length %d /Length %d" % (exact, exact), b"/Length %d /Length %d" % (exact, exact + 1),
        b"/Length 5 0 R", b"/Length 1.0", b"/Length (3)",
    ]))
    extra = draw(st.sampled_from([b"", b"/Filter /None", b"/Type /XObject"]))
    return StreamCase(
        header=b"<< " + length + b" " + extra + b" >>",
        eol=draw(_EOL),
        data=data,
        gap=draw(st.sampled_from([b"", b"\n", b"\r\n", b"\r", b" \n", b"\x00\n", b"\n\n"])),
        tail=draw(_TAIL),
        length_value=draw(st.sampled_from([exact, exact + 1, max(exact - 1, 0)])),
    )


def _build(case: StreamCase) -> tuple[bytes, dict[int, int]]:
    """A one-page PDF whose page content is the stream under test, with a
    correct xref table for every object's intended offset."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
    }
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num, body in objects.items():
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    offsets[STREAM_OBJ] = len(out)
    out += (b"4 0 obj\n" + case.header + b"\nstream" + case.eol + case.data + case.gap
            + case.tail + b"\n")
    offsets[LENGTH_OBJ] = len(out)
    out += b"5 0 obj\n%d\nendobj\n" % case.length_value
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for num in range(1, 6):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out), offsets


def _ours(data: bytes, offsets: dict[int, int]) -> IndirectObject:
    def resolve(num: int, gen: int) -> int | None:
        if (num, gen) != (LENGTH_OBJ, 0):
            return None
        held = ObjectParser(data).parse_indirect_at(offsets[LENGTH_OBJ], len(data))
        if held is None or held.flags or not isinstance(held.value, PdfInt):
            return None
        return held.value.value

    parsed = ObjectParser(data, resolve_length=resolve).parse_indirect_at(
        offsets[STREAM_OBJ], offsets[LENGTH_OBJ])
    assert parsed is not None
    return parsed


def _mupdf(data: bytes) -> tuple[bytes | None, bool, str]:
    pymupdf.TOOLS.mupdf_warnings()  # clear
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        raw = doc.xref_stream_raw(STREAM_OBJ)
        repaired = bool(doc.is_repaired)
    except Exception as exc:  # noqa: BLE001 - any failure is a disagreement
        return None, True, repr(exc)
    return raw, repaired, pymupdf.TOOLS.mupdf_warnings()


def _qpdf(path: Path) -> tuple[bytes | None, int, str]:
    result = subprocess.run(
        ["qpdf", f"--show-object={STREAM_OBJ}", "--raw-stream-data", str(path)],
        capture_output=True, timeout=30)
    return result.stdout, result.returncode, result.stderr.decode("utf-8", "replace")


@requires_qpdf
@settings(max_examples=EXAMPLES, suppress_health_check=[HealthCheck.too_slow])
@given(_stream_cases())
def test_an_unflagged_stream_reads_the_same_in_every_reader(
        tmp_path_factory: pytest.TempPathFactory, case: StreamCase) -> None:
    data, offsets = _build(case)
    ours = _ours(data, offsets)
    if ours.flags or ours.stream is None or not ours.complete:
        return  # flagged: readers may disagree, and we said so
    mine = data[ours.stream.data.start:ours.stream.data.end]
    path = tmp_path_factory.mktemp("diff") / "case.pdf"
    path.write_bytes(data)
    mu_raw, mu_repaired, mu_warnings = _mupdf(data)
    qp_raw, qp_code, qp_err = _qpdf(path)
    assert (mu_raw, mu_repaired) == (mine, False), (case, mu_warnings)
    assert (qp_raw, qp_code) == (mine, 0), (case, qp_err)


# ── Values: dictionaries of strings, names, numbers and nesting ───────────
# MuPDF and qpdf each re-serialize an object in their own canonical syntax;
# parsing that back with our parser compares values without needing any
# reader's decoding API.
_VALUE_PIECES = st.sampled_from([
    b"(abc)", b"(a\\q)", b"(\\400)", b"(\\0053)", b"(a\\\r\nb)", b"(a\\\nb)", b"(a\rb)",
    b"(a\r\nb)", b"(()())", b"(\\()", b"<41 42>", b"<414>", b"<>", b"< 4 1 >", b"<4g>",
    b"/N", b"/A#20B", b"/A#00B", b"/A#4", b"/#41", b"/", b"4.", b"-.5", b"+3", b"0",
    b"-0", b"00012", b"1.50", b"true", b"false", b"null", b"1 0 R", b"[1 (x) /y]", b"[]",
    b"<< /K 1 >>", b"<<>>", b"(\xfe\xff\x00A)", b"(\xff\xfeA\x00)", b"(\x80\x9f\xad)",
])


@st.composite
def _dict_bodies(draw: st.DrawFn) -> bytes:
    # Unique keys: a repeated key is flagged, so it would never reach the readers.
    entries = draw(st.lists(st.tuples(st.sampled_from([b"/K", b"/L", b"/M", b"/N#41", b"/O"]),
                                      _VALUE_PIECES), max_size=5, unique_by=lambda e: e[0]))
    sep = draw(st.sampled_from([b" ", b"\n", b"", b"%c\n"]))
    return b"<<" + b"".join(k + b" " + v + sep for k, v in entries) + b">>"


def _plain(value: object) -> object:
    """A parsed value as plain data (spans dropped); numbers as floats."""
    from redaction_verifier.inventory import objects as o
    match value:
        case o.PdfNull() | None:
            return None
        case o.PdfBool(value=v):
            return ("bool", v)
        case o.PdfInt(value=v) | o.PdfReal(value=v):
            return ("num", None if v is None else float(v))
        case o.PdfName(raw=raw):
            return ("name", raw)
        case o.PdfString(raw=raw):
            return ("str", raw)
        case o.PdfRef(num=num, gen=gen):
            return ("ref", num, gen)
        case o.PdfArray(items=items):
            return [_plain(item) for item in items]
        case o.PdfDict(entries=entries):
            # §7.3.7: an entry whose value is null is the same as no entry
            # (qpdf drops it, MuPDF keeps it): equivalent, not a disagreement.
            return sorted((k.raw, repr(_plain(v))) for k, v in entries
                          if not isinstance(v, o.PdfNull))
    return ("other", repr(value))


def _value_pdf(body: bytes) -> tuple[bytes, int]:
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        4: body,
    }
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num, text in objects.items():
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + text + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n"
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, 5))
    out += b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out), offsets[4]


def _reparse(serialized: bytes) -> object:
    parsed = ObjectParser(serialized).parse_value_at(0)
    return _plain(parsed.value)


@requires_qpdf
@settings(max_examples=EXAMPLES, suppress_health_check=[HealthCheck.too_slow])
@given(_dict_bodies())
def test_an_unflagged_value_reads_the_same_in_every_reader(
        tmp_path_factory: pytest.TempPathFactory, body: bytes) -> None:
    data, offset = _value_pdf(body)
    ours = ObjectParser(data).parse_indirect_at(offset, len(data))
    assert ours is not None
    if ours.flags or not ours.complete:
        return
    mine = _plain(ours.value)
    pymupdf.TOOLS.mupdf_warnings()
    doc = pymupdf.open(stream=data, filetype="pdf")
    mu_text = doc.xref_object(4, compressed=True).encode("latin-1")
    mu_warnings = pymupdf.TOOLS.mupdf_warnings()
    assert (_reparse(mu_text), mu_warnings, doc.is_repaired) == (mine, "", False), (
        body, mu_text)
    path = tmp_path_factory.mktemp("val") / "case.pdf"
    path.write_bytes(data)
    result = subprocess.run(["qpdf", "--show-object=4", str(path)], capture_output=True,
                            timeout=30)
    assert (_reparse(result.stdout), result.returncode) == (mine, 0), (
        body, result.stdout, result.stderr)


# ── The written allowlist of leniencies (ADR 0010) ────────────────────────
# Each input below is off the spec's strict path, yet our parser accepts it
# without a flag. That is allowed only because MuPDF and qpdf read it
# exactly as we do; this test is the evidence, and the list is the
# allowlist. Anything readers disagree on is flagged instead (a lone CR
# after `stream`, '#00' in a name, `endstreamendobj`, bad hex digits...).
ACCEPTED_LENIENCIES = {
    "unknown escape (backslash ignored)": b"(a\\qb)",
    "octal escape overflow (high bits dropped)": b"(\\400\\777)",
    "one- and two-digit octal escapes": b"(\\0053\\53)",
    "backslash CR LF continuation": b"(a\\\r\nb)",
    "backslash CR continuation": b"(a\\\rb)",
    "raw CR LF in a literal becomes LF": b"(a\r\nb)",
    "raw CR in a literal becomes LF": b"(a\rb)",
    "balanced unescaped parentheses": b"(a(b)c)",
    "odd final hex digit padded with 0": b"<414>",
    "whitespace inside a hex string": b"< 4 1\n42 >",
    "real with no fraction digits": b"4.",
    "real with no integer digits": b"-.5",
    "explicit plus sign": b"+3",
    "leading zeros": b"00012",
    "negative zero": b"-0",
    "empty name": b"/",
    "comment between tokens": b"[1 %c\n 2]",
}


@requires_qpdf
@pytest.mark.parametrize("value", ACCEPTED_LENIENCIES.values(), ids=ACCEPTED_LENIENCIES.keys())
def test_every_accepted_leniency_reads_the_same_in_every_reader(
        tmp_path: Path, value: bytes) -> None:
    data, offset = _value_pdf(b"<< /K " + value + b" >>")
    ours = ObjectParser(data).parse_indirect_at(offset, len(data))
    assert ours is not None and ours.flags == () and ours.complete
    mine = _plain(ours.value)
    pymupdf.TOOLS.mupdf_warnings()
    doc = pymupdf.open(stream=data, filetype="pdf")
    mu_text = doc.xref_object(4, compressed=True).encode("latin-1")
    assert (_reparse(mu_text), pymupdf.TOOLS.mupdf_warnings()) == (mine, "")
    path = tmp_path / "case.pdf"
    path.write_bytes(data)
    result = subprocess.run(["qpdf", "--show-object=4", str(path)], capture_output=True,
                            timeout=30)
    assert (_reparse(result.stdout), result.returncode) == (mine, 0), result.stderr
