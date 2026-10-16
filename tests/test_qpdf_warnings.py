"""qpdf exit 3: the warning-kind parser and its per-file byte checks.

Owner decision 2026-10-15 (ADR 0002's note): exit 3 stops forcing exit 2
only when every stderr line is one of five kinds and its check against
the file's own bytes passes. These tests feed ``_qpdf_warnings_unverified``
hand-written stderr and hand-assembled bytes, so they need no qpdf; the
case library (eval/caselib/families/qpdf_warnings.py) runs the real tool.
Every value is fabricated.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib

import pytest

import verify
from redaction_verifier.matching import SecretMatcher
from redaction_verifier.model import ScanReport

from .conftest import SSN

TARGET = "/fabricated/dir/doc.pdf"
TAIL = b"qpdf: operation succeeded with warnings\n"
CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
PAGES = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 6 0 R >> >> >>")
FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
CONTENT = b"BT /F1 12 Tf 72 700 Td (Quarterly report) Tj ET"


def warn(*bodies: bytes, target: str = TARGET) -> bytes:
    """qpdf's stderr: one WARNING line per body, then its closing line."""
    prefix = b"WARNING: " + target.encode() + b" "
    return b"".join(prefix + body + b"\n" for body in bodies) + TAIL


def stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


class Pdf:
    """Hand-assembled objects in order, then a classic xref and trailer.
    ``at(text)`` gives the offset of a marker in the result."""

    def __init__(self, *objects: tuple[int, bytes] | bytes, tail: bytes | None = None) -> None:
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        for item in objects:
            if isinstance(item, bytes):
                out += item
            else:
                number, body = item
                out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        if tail is None:
            tail = b"xref\n0 1\n0000000000 65535 f \ntrailer\n<< /Size 7 /Root 1 0 R >>\n" \
                   b"startxref\n%d\n%%%%EOF\n" % len(out)
        self.data = bytes(out) + tail

    def at(self, marker: bytes, after: bool = False) -> int:
        i = self.data.index(marker)
        return i + len(marker) if after else i


def base(*extra: tuple[int, bytes] | bytes, contents: bytes = stream(b"", CONTENT)) -> Pdf:
    return Pdf((1, CATALOG), (2, PAGES), (3, PAGE), (4, contents), (6, FONT), *extra)


def check(stderr: bytes | None, data: bytes | None) -> str | None:
    return verify._qpdf_warnings_unverified(stderr, TARGET, data)


def unfinished_flate(data: bytes) -> bytes:
    z = zlib.compressobj(0)
    return z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH)


# ── The stderr parser ──────────────────────────────────────────────────

class TestParser:
    REF = b"(object 3 0, offset 246): treating bad indirect reference (0 0 R) as null"

    def test_a_harmless_kind_passes(self) -> None:
        assert check(warn(self.REF), base().data) is None

    def test_without_the_closing_line_too(self) -> None:
        assert check(warn(self.REF)[:-len(TAIL)], base().data) is None

    @pytest.mark.parametrize("stderr", [
        b"", TAIL, b"\n",
        warn(b"(offset 417): xref not found"),
        warn(b"file is damaged"),
        warn(b": Attempting to reconstruct cross-reference table"),
        warn(b"(object 3 0, offset 242): treating object as null because of error during "
             b"parsing: overflow/underflow converting 18446744073386459286 to 64-bit integer"),
        warn(REF, b"(offset 9): something new in a later qpdf"),
        warn(REF) + b"WARNING: object 3 0: Pages tree includes non-dictionary object\n",
        warn(REF, target="/fabricated/other.pdf"),           # not this file's path
        warn(REF + b" "),                                      # trailing junk
        warn(REF).replace(b"\n", b"\r\n"),
        warn(b"(object 3 0, offset 246): treating bad indirect reference (1 0 R) as null"),
        warn(b"(object 3 0, offset 246): treating bad indirect reference (0 1 R) as null"),
    ], ids=lambda s: repr(s[:40]))
    def test_anything_else_fails_closed(self, stderr: bytes) -> None:
        assert check(stderr, base().data) is not None

    def test_unreadable_stderr_or_bytes_fail_closed(self) -> None:
        assert check(None, base().data) is not None
        assert check(warn(self.REF), None) is not None

    def test_a_name_with_a_newline_cannot_forge_a_line(self) -> None:
        # qpdf prints a duplicated key's name decoded: /Fi#0alter splits
        # its warning in two, and neither half is a recognised line.
        pdf = base((5, b"<< /Fi#0alter 1 /Fi#0alter 1 >>"))
        stderr = warn(b"(object 5 0, offset %d): dictionary has duplicated key /Fi\nlter; "
                      b"last occurrence overrides earlier ones" % pdf.at(b"5 0 obj\n<<", True))
        assert check(stderr, pdf.data) is not None

    def test_a_name_with_spaces_is_refused(self) -> None:
        pdf = base((5, b"<< /A#20B 1 /A#20B 1 >>"))
        o = pdf.data.index(b"<< /A#20B") + 2
        assert check(warn(b"(object 5 0, offset %d): dictionary has duplicated key /A B; "
                          b"last occurrence overrides earlier ones" % o), pdf.data) is not None

    def test_caps_fail_closed(self, monkeypatch) -> None:
        assert check(warn(self.REF) + b"x" * verify.QPDF_STDERR_CAP_BYTES, base().data)
        monkeypatch.setattr(verify, "QPDF_WARNING_LINES_CAP", 2)
        assert check(warn(self.REF, self.REF, self.REF), base().data) is not None
        monkeypatch.setattr(verify, "QPDF_CHECK_MAX_FILE_BYTES", 10)
        assert check(warn(self.REF), base().data) is not None

    def test_reasons_never_echo_qpdf_text(self) -> None:
        stderr = warn(b"(offset 1): " + SSN.encode())
        reason = check(stderr, base().data)
        assert reason and SSN not in reason and "offset" not in reason


# ── 2. offset 0 ────────────────────────────────────────────────────────

def offset0(number: int = 5) -> bytes:
    return warn(b"(object %d 0): object has offset 0 - a common error handled correctly by "
                b"qpdf and most other applications" % number)


class TestOffsetZero:
    def test_no_body_anywhere_passes(self) -> None:
        assert check(offset0(), base().data) is None

    @pytest.mark.parametrize("body", [
        b"5 0 obj\n<< /Note (" + SSN.encode() + b") >>\nendobj\n",
        b"5 1 obj\n(x)\nendobj\n",                        # any generation
        b"5 %c\n0 obj (x) endobj\n",                      # comment between the parts
        b"% 5 0 obj (x)\n",                               # even inside a comment
        b"x5\x000\x0cobj\n",                              # NUL and form feed are whitespace
        b"5 +0 obj (x) endobj\n",                         # a signed generation
        b"5 0obj (x) endobj\n",                           # generation glued to obj
        b"+5 -0 obj (x) endobj\n",
        b"(a%b) 5 %c\n0 obj (x)\n",                       # a '%' in a string first
        b"5\r\n%c1\r%c2\n0\n%c3\nobj (x)\n",              # several comments
    ])
    def test_a_stored_body_fails(self, body: bytes) -> None:
        assert check(offset0(), base(body).data) is not None

    def test_a_number_ending_in_n_counts_when_its_offset_is_written(self) -> None:
        # A reader sent to the '5' of '15 0 obj' reads object 5, but only
        # an offset written in the file (an xref entry) can send it there.
        pdf = base((15, b"(x)"))
        assert check(offset0(), pdf.data) is None
        five = pdf.at(b"15 0 obj") + 1
        pdf = base((15, b"(x)"), b"%% %010d\n" % five)
        assert check(offset0(), pdf.data) is not None

    def test_a_commented_header_reached_by_a_written_offset(self) -> None:
        pdf = base(b"%95 0 obj (x) endobj\n")
        assert check(offset0(), pdf.data) is None
        five = pdf.at(b"%95 0 obj") + 2
        pdf = base(b"%95 0 obj (x) endobj\n", b"%% xref entry %010d 00000 n\n" % five)
        assert check(offset0(), pdf.data) is not None

    def test_a_written_offset_counts_from_a_shifted_header(self) -> None:
        # Junk before '%PDF-': readers add its length to every offset.
        pdf = base(b"%95 0 obj (x) endobj\n")
        five = pdf.at(b"%95 0 obj") + 2
        shifted = b"JUNK\n" + pdf.data + b"%% xref entry %010d 00000 n\n" % five
        assert check(offset0(), shifted) is not None
        assert check(offset0(), b"JUNK\n" + pdf.data) is None

    def test_a_number_merely_containing_n_is_not_a_body(self) -> None:
        assert check(offset0(), base((51, b"(x)")).data) is None

    @pytest.mark.parametrize("name", [b"/ObjStm", b"/Obj#53tm", b"/XRef", b"/XR#65f"])
    def test_object_or_xref_streams_fail(self, name: bytes) -> None:
        # A body packed in an object stream has no header in the bytes.
        assert check(offset0(), base((7, b"<< /Type " + name + b" >>")).data) is not None

    def test_one_failing_instance_fails_the_file(self) -> None:
        pdf = base(b"9 0 obj\n(x)\nendobj\n")
        assert check(offset0(5)[:-len(TAIL)] + offset0(9), pdf.data) is not None


class TestBounded:
    """Crafted bytes make a check fail closed, never run for long."""

    @pytest.mark.parametrize("filler", [
        b"1 %" * 70_000,                 # a '%' per digit, no end of line
        b"1 " * 100_000 + b"obj ",
        b"9" * 200_000 + b" 0 obj",
        (b"%" * 50 + b"\n") * 4_000 + b"obj",
        b"/a#41" * 40_000,
    ])
    @pytest.mark.parametrize("stderr", [warn(TestParser.REF), offset0()])
    def test_adversarial_bytes(self, filler: bytes, stderr: bytes) -> None:
        import time
        pdf = base((7, stream(b"", filler)))
        start = time.monotonic()
        check(stderr, pdf.data)
        assert time.monotonic() - start < 10

    def test_long_lines_without_cr(self) -> None:
        import time
        pdf = base((7, stream(b"", b"\nobj" * 100_000)))
        start = time.monotonic()
        check(offset0(), pdf.data)
        assert time.monotonic() - start < 5

    def test_many_offset_zero_lines_scan_once(self) -> None:
        import time
        pdf = base((7, stream(b"", b"/a#41 " * 100_000)))
        lines = b"".join(offset0(n)[:-len(TAIL)] for n in range(100, 2100)) + TAIL
        start = time.monotonic()
        assert check(lines, pdf.data) is None
        assert time.monotonic() - start < 10

    def test_the_work_budget_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setattr(verify, "QPDF_HEADER_SCAN_WORK_PER_BYTE", 0)
        pdf = base((7, stream(b"", b"1 0 obj\n" * 200_000)))
        assert check(offset0(), pdf.data) is not None


# ── 3. duplicated keys ─────────────────────────────────────────────────

def dupkey(pdf: Pdf, obj: int, key: bytes, marker: bytes) -> bytes:
    return warn(b"(object %d 0, offset %d): dictionary has duplicated key %s; last "
                b"occurrence overrides earlier ones" % (obj, pdf.at(marker, after=True), key))


class TestDuplicatedKey:
    def test_identical_values_pass(self) -> None:
        pdf = base(contents=stream(b"/Filter /FlateDecode /Filter /FlateDecode",
                                   zlib.compress(CONTENT, 0)))
        assert check(dupkey(pdf, 4, b"/Filter", b"4 0 obj\n<<"), pdf.data) is None

    @pytest.mark.parametrize("a, b", [
        (b"/FlateDecode", b"/ASCIIHexDecode"),
        (b"5 0 R", b"5 1 R"),                 # same number, other generation
        (b"(a)", b"(\\141)"),                 # same string, written differently
        (b"[1 2]", b"[1 2 3]"),
        (b"<< /A 1 >>", b"<< /A 2 >>"),
    ])
    def test_differing_values_fail(self, a: bytes, b: bytes) -> None:
        pdf = base((5, b"<< /K " + a + b" /K " + b + b" >>"))
        assert check(dupkey(pdf, 5, b"/K", b"5 0 obj\n<<"), pdf.data) is not None

    def test_nested_dictionary(self) -> None:
        pdf = base((5, b"<< /A 1 /B << /K 1 /K 1 >> >>"))
        assert check(dupkey(pdf, 5, b"/K", b"/B <<"), pdf.data) is None
        pdf = base((5, b"<< /A 1 /B << /K 1 /K 2 >> >>"))
        assert check(dupkey(pdf, 5, b"/K", b"/B <<"), pdf.data) is not None

    def test_another_key_of_the_same_dictionary_differing_fails(self) -> None:
        pdf = base((5, b"<< /K 1 /K 1 /J 1 /J 2 >>"))
        assert check(dupkey(pdf, 5, b"/K", b"5 0 obj\n<<"), pdf.data) is not None

    def test_an_escaped_spelling_of_the_key_counts(self) -> None:
        pdf = base((5, b"<< /K 1 /#4B 2 >>"))
        assert check(dupkey(pdf, 5, b"/K", b"5 0 obj\n<<"), pdf.data) is not None

    def test_offset_or_object_that_does_not_match_fails(self) -> None:
        pdf = base((5, b"<< /K 1 /K 1 >>"))
        good = pdf.at(b"5 0 obj\n<<", after=True)
        line = b"(object %d 0, offset %d): dictionary has duplicated key /K; last occurrence " \
               b"overrides earlier ones"
        assert check(warn(line % (5, good)), pdf.data) is None
        assert check(warn(line % (5, good + 1)), pdf.data) is not None
        assert check(warn(line % (6, good)), pdf.data) is not None
        assert check(warn(line.replace(b"/K;", b"/J;") % (5, good)), pdf.data) is not None

    def test_a_dictionary_inside_a_string_is_not_one(self) -> None:
        pdf = base((5, b"<< /S (<< /K 1 /K 2 >>) >>"))
        assert check(dupkey(pdf, 5, b"/K", b"(<<"), pdf.data) is not None


# ── 4. expected endobj ─────────────────────────────────────────────────

def endobj(obj: int, offset: int, xref: bool = False) -> bytes:
    return warn(b"(%sobject %d 0, offset %d): expected endobj"
                % (b"xref stream: " if xref else b"", obj, offset))


class TestExpectedEndobj:
    def test_whitespace_then_the_next_object_passes(self) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b"\n \r\n5 0 obj\n1\nendobj\n")
        assert check(endobj(4, pdf.at(b"5 0 obj")), pdf.data) is None

    def test_after_a_plain_value_too(self) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n<< /A 1 >>\n5 0 obj\n1\nendobj\n")
        assert check(endobj(4, pdf.at(b"5 0 obj")), pdf.data) is None

    def test_before_xref_passes(self) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b"\n")
        assert check(endobj(4, pdf.at(b"xref")), pdf.data) is None

    @pytest.mark.parametrize("between", [
        b"(" + SSN.encode() + b")\n",
        b"% " + SSN.encode() + b"\n",                 # a comment is not whitespace here
        b"endstream\n",
    ])
    def test_anything_else_between_fails(self, between: bytes) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b"\n" + between
                  + b"5 0 obj\n1\nendobj\n")
        # Wherever qpdf points: at the stray token, or past a comment.
        for offset in (pdf.at(between[:1] if between[:1] != b"e" else b"endstream\n5"),
                       pdf.at(b"5 0 obj")):
            assert check(endobj(4, offset), pdf.data) is not None

    def test_a_forged_header_in_a_comment_cannot_relocate_the_end(self) -> None:
        # The real stream ends at the first endstream. A comment holds a
        # fake "4 0 obj" whose /Length lands on a second endstream; the
        # walk reads the comment as a comment, so the gap is not clean.
        fake = (b"% 4 0 obj << /Length 5 >> stream\r\n%SSN endstream\n")
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b" " + fake
                  + b"5 0 obj\n1\nendobj\n")
        assert check(endobj(4, pdf.at(b"5 0 obj")), pdf.data) is not None

    def test_object_or_offset_mismatch_fails(self) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b"\n5 0 obj\n1\nendobj\n")
        assert check(endobj(7, pdf.at(b"5 0 obj")), pdf.data) is not None
        assert check(endobj(4, pdf.at(b"5 0 obj") - 1), pdf.data) is not None

    def test_xref_stream_variant_needs_an_xref_stream(self) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b"\n")
        assert check(endobj(4, pdf.at(b"xref"), xref=True), pdf.data) is not None

    def test_a_walk_that_cannot_parse_the_file_fails_closed(self) -> None:
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + stream(b"", CONTENT) + b"\n5 0 obj\n1\nendobj\n",
                  tail=b"garbage ) here\n")
        assert check(endobj(4, pdf.at(b"5 0 obj")), pdf.data) is not None

    def test_an_indirect_length_is_resolved_only_when_unique(self) -> None:
        body = b"<< /Length 9 0 R >>\nstream\n" + CONTENT + b"\nendstream"
        length = b"9 0 obj\n%d\nendobj\n" % len(CONTENT)
        pdf = Pdf((1, CATALOG), b"4 0 obj\n" + body + b"\n" + length)
        assert check(endobj(4, pdf.at(b"9 0 obj")), pdf.data) is None
        twice = Pdf((1, CATALOG), b"4 0 obj\n" + body + b"\n" + length + length)
        assert check(endobj(4, twice.at(b"9 0 obj")), twice.data) is not None


# ── 5. Flate without its end marker ────────────────────────────────────

def flate_warning(offset: int) -> bytes:
    return warn(b"(offset %d): input stream is complete but output may still be valid" % offset)


def data_start(pdf: Pdf, obj: int) -> int:
    return pdf.data.index(b"stream\n", pdf.at(b"%d 0 obj" % obj)) + len(b"stream\n")


class TestTruncatedFlate:
    def test_a_content_stream_passes(self) -> None:
        pdf = base(contents=stream(b"/Filter /FlateDecode", unfinished_flate(CONTENT)))
        assert check(flate_warning(data_start(pdf, 4)), pdf.data) is None

    @pytest.mark.parametrize("dictionary", [
        b"/Type /ObjStm /N 1 /First 4",
        b"/Type /Obj#53tm /N 1 /First 4",
        b"/N 1 /First 4",                      # an object stream that does not say so
        b"/Type 9 0 R",                        # a /Type a reader must resolve
        b"/Type /XObject /Type /ObjStm",
    ])
    def test_an_object_stream_fails(self, dictionary: bytes) -> None:
        pdf = base((7, stream(b"/Filter /FlateDecode " + dictionary,
                              unfinished_flate(b"5 0 (" + SSN.encode() + b")"))))
        assert check(flate_warning(data_start(pdf, 7)), pdf.data) is not None

    def test_an_offset_that_is_not_a_stream_start_fails(self) -> None:
        pdf = base(contents=stream(b"/Filter /FlateDecode", unfinished_flate(CONTENT)))
        assert check(flate_warning(data_start(pdf, 4) + 1), pdf.data) is not None


def xref_stream_pdf(rows: int, declared: int, predictor: bool = False,
                    index: bytes | None = None) -> tuple[Pdf, int]:
    row = struct.pack(">BIH", 1, 15, 0)
    raw = b"".join((b"\x00" + row) if predictor else row for _ in range(rows))
    extra = b" /DecodeParms << /Predictor 12 /Columns 7 >>" if predictor else b""
    extra += b" /Index " + index if index else b""
    body = stream(b"/Type /XRef /W [1 4 2] /Size %d /Root 1 0 R /Filter /FlateDecode%s"
                  % (declared, extra), unfinished_flate(raw))
    head = Pdf((1, CATALOG), tail=b"")
    obj = b"7 0 obj\n" + body + b"\n"
    data = head.data + obj + b"startxref\n%d\n%%%%EOF\n" % len(head.data)
    pdf = Pdf(tail=b"")
    pdf.data = data
    return pdf, data_start(pdf, 7)


class TestTruncatedXrefStream:
    def test_every_row_decodes(self) -> None:
        pdf, start = xref_stream_pdf(rows=8, declared=8)
        assert check(flate_warning(start), pdf.data) is None
        both = flate_warning(start)[:-len(TAIL)] + endobj(7, pdf.at(b"startxref"), xref=True)
        assert check(both, pdf.data) is None

    def test_with_a_png_predictor(self) -> None:
        pdf, start = xref_stream_pdf(rows=8, declared=8, predictor=True)
        assert check(flate_warning(start), pdf.data) is None

    def test_rows_missing_fail(self) -> None:
        pdf, start = xref_stream_pdf(rows=7, declared=8)
        assert check(flate_warning(start), pdf.data) is not None
        pdf, start = xref_stream_pdf(rows=8, declared=8, index=b"[0 4 10 5]")
        assert check(flate_warning(start), pdf.data) is not None

    def test_index_counts_rows(self) -> None:
        pdf, start = xref_stream_pdf(rows=8, declared=20, index=b"[0 4 10 4]")
        assert check(flate_warning(start), pdf.data) is None


# ── The collector ──────────────────────────────────────────────────────

class TestCollector:
    """_collect_qpdf against a stand-in qpdf (this interpreter): its argv
    ends in "<target> -" as the real launch does, it writes a little QDF
    output and the stderr given, then exits 3."""

    def _run(self, stderr: bytes, data: bytes | None, tmp_path, code: int = 3) -> ScanReport:
        target = str(tmp_path / "doc.pdf")
        err = tmp_path / "stderr.bin"
        err.write_bytes(stderr)
        script = ("import sys; sys.stdout.buffer.write(b'%PDF-1.7 x' * 8); "
                  f"sys.stderr.buffer.write(open({str(err)!r}, 'rb').read()"
                  ".replace(b'@T', sys.argv[1].encode())); "
                  f"sys.exit({code})")
        proc = subprocess.Popen([sys.executable, "-c", script, target, "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report = ScanReport()
        verify._collect_qpdf(proc, SecretMatcher([]), report, None, data)
        return report

    def _stderr(self, *bodies: bytes) -> bytes:
        return b"".join(b"WARNING: @T " + b + b"\n" for b in bodies) + TAIL

    def test_harmless_exit_3_is_certified(self, tmp_path) -> None:
        report = self._run(self._stderr(TestParser.REF), base().data, tmp_path)
        assert not [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]

    def test_unverified_exit_3_still_flags(self, tmp_path) -> None:
        report = self._run(self._stderr(b"(offset 9): xref not found " + SSN.encode()),
                           base().data, tmp_path)
        (w,) = [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]
        assert w.fields["returncode"] == 3 and SSN not in w and "xref" not in w

    def test_no_file_bytes_still_flags(self, tmp_path) -> None:
        report = self._run(self._stderr(TestParser.REF), None, tmp_path)
        assert [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]

    def test_other_exit_codes_always_flag(self, tmp_path) -> None:
        report = self._run(self._stderr(TestParser.REF), base().data, tmp_path, code=2)
        assert [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]

    def test_stderr_past_the_cap_is_drained_and_flags(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(verify, "QPDF_STDERR_CAP_BYTES", 64)
        report = self._run(self._stderr(*[TestParser.REF] * 50), base().data, tmp_path)
        assert [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]

    def test_stdout_closed_and_stderr_left_open_times_out(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(verify, "SUBPROCESS_TIMEOUT_S", 2)
        target = str(tmp_path / "doc.pdf")
        script = ("import os, sys, time; sys.stdout.buffer.write(b'%PDF x' * 8); "
                  "sys.stdout.flush(); os.close(1); time.sleep(30)")
        proc = subprocess.Popen([sys.executable, "-c", script, target, "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        report = ScanReport()
        verify._collect_qpdf(proc, SecretMatcher([]), report, None, base().data)
        assert [w for w in report.warnings if w.code == "TOOL_TIMEOUT"]

    def test_a_large_stderr_does_not_stall_qpdf(self, tmp_path) -> None:
        # 1 MB of stderr would fill a pipe nobody reads and hang the tool.
        report = self._run(self._stderr(*[TestParser.REF] * 12_000), base().data, tmp_path)
        assert not [w for w in report.warnings if w.code == "TOOL_TIMEOUT"]
        assert [w for w in report.warnings if w.code == "TOOL_EXIT_NONZERO"]
