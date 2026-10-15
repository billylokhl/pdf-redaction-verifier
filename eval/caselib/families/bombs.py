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

from ..model import Bomb, KnownGap, case, expect

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


def _page(extra: bytes = b"") -> list[bytes]:
    return [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R %s >>"
            % extra]


def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def _zeros_flate(mib: int) -> bytes:
    z = zlib.compressobj(9)
    chunk = b"\0" * (1 << 20)
    return b"".join(z.compress(chunk) for _ in range(mib)) + z.flush()


BUSY = "Opening an untrusted PDF in any tool that does not bound its own work."


@case("page.bomb-flate-content", truth="clean", features="live.plain", writer="raw",
      bomb=Bomb(wall_clock_s=180, memory_bytes=3 * GB),
      expected=expect(2),
      story="A page whose content stream is 256 MiB of zeros compressed to 250 KB: a "
            "decompression bomb in live content.",
      mistake=BUSY)
def flate_content(path: Path) -> None:
    path.write_bytes(_pdf(_page() + [_stream(_zeros_flate(256), b"/Filter /FlateDecode")]))


@case("leftover.bomb-flate-orphan", truth="clean", features="orphaned.plain", writer="raw",
      bomb=Bomb(wall_clock_s=120, memory_bytes=3 * GB),
      expected=expect(2),
      story="The same 256 MiB decompression bomb in a stream nothing references.",
      mistake=BUSY)
def flate_orphan(path: Path) -> None:
    path.write_bytes(_pdf(_page() + [_stream(b"BT ET"),
                                     _stream(_zeros_flate(256), b"/Filter /FlateDecode")]))


@case("file.bomb-deep-nesting", truth="clean", features="live.plain", writer="raw",
      requires=("exiftool",), bomb=Bomb(wall_clock_s=400, memory_bytes=3 * GB),
      expected=expect(2),
      story="A page dictionary holding an array nested 30,000 levels deep.",
      mistake=BUSY)
def deep_nesting(path: Path) -> None:
    deep = b"[" * 30000 + b"]" * 30000
    path.write_bytes(_pdf(_page(b"/Deep " + deep) + [_stream(b"BT ET")]))


@case("file.bomb-page-tree-cycle", truth="clean", features="live.plain", writer="raw",
      bomb=Bomb(wall_clock_s=120, memory_bytes=3 * GB),
      expected=expect(2),
      story="A page tree whose /Kids lists the tree itself: a reader that walks it "
            "without remembering where it has been never stops.",
      mistake=BUSY)
def page_tree_cycle(path: Path) -> None:
    path.write_bytes(_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 2 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        _stream(b"BT ET")]))


@case("file.bomb-huge-page-count", truth="clean", features="live.plain", writer="raw",
      bomb=Bomb(wall_clock_s=120, memory_bytes=3 * GB),
      expected=expect(2),
      known_gap=KnownGap("live.plain", outcome="error:INTERNAL_ERROR"),   # cell: pending
      story="A one-page file whose page tree claims two billion pages.",
      mistake=BUSY)
def huge_page_count(path: Path) -> None:
    path.write_bytes(_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 2000000000 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        _stream(b"BT ET")]))


@case("page.bomb-oversized-image", truth="clean", features="live.pixels", writer="raw",
      bomb=Bomb(wall_clock_s=180, memory_bytes=3 * GB),
      expected=expect(2),
      known_gap=KnownGap("live.plain", today=expect(0)),                  # cell: pending
      story="A page drawing a 60,000 x 60,000 pixel image: far more detail than the "
            "page's OCR render can resolve.",
      mistake=BUSY)
def oversized_image(path: Path) -> None:
    pixels = zlib.compress(b"\0" * (60000 // 8 * 60000), 9)
    path.write_bytes(_pdf(
        _page(b"/Resources << /XObject << /Im1 5 0 R >> >>")
        + [_stream(b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
           _stream(pixels, b"/Type /XObject /Subtype /Image /Width 60000 /Height 60000 "
                           b"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode")]))


@case("page.bomb-operator-flood", truth="clean", features="live.font", writer="raw",
      bomb=Bomb(wall_clock_s=180, memory_bytes=2 * GB),
      expected=expect(0),
      story="A page whose content is 10 MB of one-letter text operators, 130 KB on disk: "
            "a reader answers, but should not need gigabytes to.",
      mistake=BUSY)
def operator_flood(path: Path) -> None:
    font = (b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
            b"/BaseFont /Helvetica >> >> >>")
    ops = b"BT /F1 1 Tf (x) Tj ET\n" * 500_000
    path.write_bytes(_pdf(_page(font) + [_stream(zlib.compress(ops, 9),
                                                 b"/Filter /FlateDecode")]))
