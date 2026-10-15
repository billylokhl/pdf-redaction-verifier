"""Bomb and hang cases (Phase 3d's gate: hostile input exits 2, or answers
correctly within its caps). Each file is small on disk and costs a reader
time or memory: a decompression bomb, deep nesting, a page-tree cycle, an
absurd page count, an oversized image, an operator flood. None holds a
secret: they are clean files, so the correct verdict is a clean 0 where a
reader can answer within its limits, and 2 where it cannot.

They are never scanned in the test process (``Case.bomb``):
tests/test_bomb_cases.py runs the CLI out of process through
redaction_verifier.sandbox, killed at each case's caps, and judges the
answer -- or pins a hang, a memory kill or an error as a known gap's
outcome. All bytes are hand-assembled (eval/caselib/rawpdf.py's style);
the caps were sized on the owner's Mac (2026-10-15) with headroom.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from ..model import SSN, Bomb, KnownGap, case, expect

GB = 1 << 30


def _pdf(objects: list[bytes], root: int = 1) -> bytes:
    body = b"%PDF-1.7\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    body += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    body += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
             % (len(objects) + 1, root, xref))
    return body


FONT = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
SHOW = b"BT /F1 12 Tf 72 700 Td (SSN " + SSN.encode() + b") Tj ET\n"


def _page(extra: bytes = FONT) -> list[bytes]:
    return [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R %s >>"
            % extra]


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def _bomb_then(mib: int, tail: bytes) -> bytes:
    """*mib* MiB of spaces, then *tail*, Flate-compressed: a few hundred KB."""
    z = zlib.compressobj(9)
    chunk = b" " * (1 << 20)
    return b"".join(z.compress(chunk) for _ in range(mib)) + z.compress(tail) + z.flush()


BUSY = "Opening an untrusted PDF in any tool that does not bound its own work."
READ_IT = ("Read the file with a tool that bounds its work (or decompress the stream "
           "piecewise): the SSN is plain text behind the bomb.")


@case("page.bomb-flate-content", truth="leak", cells="hostile.flate-bomb", writer="raw",
      # Read whole (2.4 GB at peak on the owner's Mac): found, with the
      # object it could not also read flagged.
      bomb=Bomb(wall_clock_s=180, memory_bytes=4 * GB),
      expected=expect(1, findings=(("SSN", "live"),),
                      warnings=(("OBJECT_UNREADABLE", "orphaned"),)),
      story="A page whose content stream is 256 MiB of spaces and then the SSN, compressed "
            "to a few hundred KB: a decompression bomb in live content.",
      mistake=BUSY, recovery=READ_IT)
def flate_content(path: Path) -> None:
    path.write_bytes(_pdf(_page() + [_stream(_bomb_then(256, SHOW), b"/Filter /FlateDecode")]))


@case("leftover.bomb-flate-orphan", truth="leak", cells="hostile.flate-bomb", writer="raw",
      bomb=Bomb(wall_clock_s=120, memory_bytes=3 * GB),
      # Read only to 16 MiB: the rest, the SSN included, is flagged.
      expected=expect(2, warnings=(("PAYLOAD_TRUNCATED", "orphaned"),)),
      story="The same 256 MiB bomb, ending in the SSN, in a stream nothing references.",
      mistake=BUSY, recovery=READ_IT)
def flate_orphan(path: Path) -> None:
    path.write_bytes(_pdf(_page() + [_stream(b"BT ET"),
                                     _stream(_bomb_then(256, b"SSN " + SSN.encode()),
                                             b"/Filter /FlateDecode")]))


@case("file.bomb-deep-nesting", truth="leak", cells="hostile.deep-nesting", writer="raw",
      requires=("exiftool", "qpdf"), bomb=Bomb(wall_clock_s=400, memory_bytes=3 * GB),
      # The page dictionary does not parse, so no reachability walk is
      # trusted: the content stream's SSN is found as "unreferenced", and
      # what could not be read is flagged (exiftool times out at 120 s).
      expected=expect(1, findings=(("SSN", "unreferenced"),), warnings=(
          ("HIDDEN_ITEM_FAILED", "live"), ("OBJECT_UNREADABLE", "unreferenced"),
          ("TOOL_EXIT_NONZERO", None), ("TOOL_TIMEOUT", None))),
      story="A page showing the SSN, whose dictionary also holds an array nested 30,000 "
            "levels deep.",
      mistake=BUSY, recovery=READ_IT)
def deep_nesting(path: Path) -> None:
    deep = b"[" * 30000 + b"]" * 30000
    path.write_bytes(_pdf(_page(FONT + b" /Deep " + deep) + [_stream(SHOW)]))


@case("file.bomb-page-tree-cycle", truth="leak", cells="hostile.page-tree-cycle", writer="raw",
      requires=("qpdf",), bomb=Bomb(wall_clock_s=120, memory_bytes=3 * GB),
      # The page is read once (found); the cycle's repeat is flagged.
      expected=expect(1, findings=(("SSN", "live"),), warnings=(
          ("HIDDEN_ITEM_FAILED", "live"), ("PAGE_FAILED", "live"),
          ("TOOL_EXIT_NONZERO", None), ("TOOL_NO_OUTPUT", None))),
      story="A page showing the SSN, in a page tree whose /Kids lists the tree itself: a "
            "reader that walks it without remembering where it has been never stops.",
      mistake=BUSY, recovery=READ_IT)
def page_tree_cycle(path: Path) -> None:
    path.write_bytes(_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 2 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R " + FONT + b" >>",
        _stream(SHOW)]))


@case("file.bomb-huge-page-count", truth="leak", cells="hostile.huge-page-count", writer="raw",
      # Today an internal error (exit 2, failing closed: COVERAGE's ⚑
      # footnote); the flag 3d is to give instead is a failed page.
      bomb=Bomb(wall_clock_s=120, memory_bytes=3 * GB, today="error:INTERNAL_ERROR"),
      expected=expect(2, warnings=(("PAGE_FAILED", "live"),)),
      story="A page showing the SSN, in a page tree that claims two billion pages.",
      mistake=BUSY, recovery=READ_IT)
def huge_page_count(path: Path) -> None:
    path.write_bytes(_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 2000000000 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R " + FONT + b" >>",
        _stream(SHOW)]))


@case("page.bomb-oversized-image", truth="clean", features="live.pixels", writer="raw",
      bomb=Bomb(wall_clock_s=180, memory_bytes=3 * GB),
      expected=expect(2),
      known_gap=KnownGap("live.pixels.oversized-image", today=expect(0)),
      story="A page drawing a 60,000 x 60,000 pixel image: far more detail than the "
            "page's OCR render can resolve, read as if it were the render.",
      mistake=BUSY)
def oversized_image(path: Path) -> None:
    pixels = zlib.compress(b"\0" * (60000 // 8 * 60000), 9)
    path.write_bytes(_pdf(
        _page(b"/Resources << /XObject << /Im1 5 0 R >> >>")
        + [_stream(b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
           _stream(pixels, b"/Type /XObject /Subtype /Image /Width 60000 /Height 60000 "
                           b"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode")]))


@case("page.bomb-operator-flood", truth="leak", cells="hostile.operator-flood", writer="raw",
      bomb=Bomb(wall_clock_s=180, memory_bytes=2 * GB),
      expected=expect(1, findings=(("SSN", "live"),)),
      story="A page whose content is 10 MB of one-letter text operators, 130 KB on disk, "
            "with the SSN among them: a reader must still find it, within its limits.",
      mistake=BUSY, recovery=READ_IT)
def operator_flood(path: Path) -> None:
    ops = b"BT /F1 1 Tf (x) Tj ET\n" * 250_000 + SHOW + b"BT /F1 1 Tf (x) Tj ET\n" * 250_000
    path.write_bytes(_pdf(_page() + [_stream(zlib.compress(ops, 9), b"/Filter /FlateDecode")]))
