"""The PDF producers behind the real-redactor set's documents.

Five producers, so the inputs are not all written the way the redactor
and the verifier read (both use PyMuPDF): Chrome's Skia/PDF (subset CID
fonts, Identity-H), reportlab (AcroForms, image-only scan pages),
tesseract's searchable-PDF output (image plus an invisible text layer),
macOS Quartz via cupsfilter, and pikepdf (libqpdf) for post-processing
in ``post.py``. PyMuPDF writes nothing here.

Every external program is found on PATH or through an environment
variable; no path is hard-coded. Every producer is given a fabricated
title and author, so nothing from this machine (a file name, a URL, a
login name) reaches a document's metadata.
"""

from __future__ import annotations

import math
import os
import random
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CHROME_DEFAULT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCAN_DPI = 200
# The user name cupsfilter passes to its filters: fabricated, so the
# login name never reaches a Quartz document.
CUPS_USER = "household"


class ProducerError(RuntimeError):
    pass


def chrome_binary() -> str:
    path = os.environ.get("CHROME", CHROME_DEFAULT)
    if not Path(path).exists():
        raise ProducerError("Chrome not found: set CHROME to its binary")
    return path


def _require(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        raise ProducerError(f"{tool} not found on PATH")
    return found


# ── Chrome ─────────────────────────────────────────────────────────────

def chrome_pdf(html: str, out: Path, *, timeout: float = 60.0) -> None:
    """Print *html* to *out* with headless Chrome.

    ``--no-pdf-header-footer`` is mandatory: Chrome's default header and
    footer print the page's file URL (a home path) in glyph codes that a
    raw-byte privacy scrub cannot see. A throwaway profile keeps the
    user's own Chrome profile out of it. Headless Chrome on macOS can
    keep running after it has written the file, so this waits for the
    file to be complete and then ends the process group.
    """
    html_dir = Path(tempfile.mkdtemp(prefix="rs-html-"))
    profile = Path(tempfile.mkdtemp(prefix="rs-chrome-"))
    src = html_dir / "doc.html"
    src.write_text(html, encoding="utf-8")
    out = out.resolve()
    if out.exists():
        out.unlink()
    log = html_dir / "chrome.log"
    proc: subprocess.Popen[bytes] | None = None
    cmd = [
        chrome_binary(), "--headless", "--disable-gpu", "--no-pdf-header-footer",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-sync", "--use-mock-keychain", "--password-store=basic",
        "--disable-background-networking", "--disable-component-update",
        f"--user-data-dir={profile}", f"--print-to-pdf={out}", src.as_uri(),
    ]
    try:
        with open(log, "wb") as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None or b"bytes written to file" in log.read_bytes():
                break
            time.sleep(0.2)
        else:
            raise ProducerError("Chrome did not write the PDF in time")
        # A last check that the file is whole before the process is ended.
        for _ in range(50):
            if out.exists() and out.read_bytes().rstrip().endswith(b"%%EOF"):
                break
            time.sleep(0.1)
        else:
            raise ProducerError("Chrome's PDF is incomplete")
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                os.killpg(proc.pid, signal.SIGKILL)
        shutil.rmtree(html_dir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


def chrome_version() -> str:
    try:
        out = subprocess.run([chrome_binary(), "--version"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    except (ProducerError, subprocess.TimeoutExpired):
        return "unavailable"
    return out.rsplit(" ", 1)[-1] if out else "unknown"


# ── Rasterising (poppler) ──────────────────────────────────────────────

def rasterise(pdf: Path, dpi: int, out_dir: Path, *, gray: bool = True) -> list[Path]:
    """Every page of *pdf* as a PNG, by poppler's pdftoppm (not MuPDF)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    cmd = [_require("pdftoppm"), "-r", str(dpi), "-png"]
    if gray:
        cmd.append("-gray")
    subprocess.run([*cmd, str(pdf), str(prefix)], check=True, capture_output=True)
    return sorted(out_dir.glob("page-*.png"))


# ── Scans ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Skew:
    """The geometric part of a scan's degradation, to map boxes with."""
    angle: float        # degrees, counter-clockwise as seen
    width: int
    height: int

    def map_box(self, box: list[float], scale: float) -> list[float]:
        """A top-left-origin box in points -> the box (in points) that
        covers it after the page image is rotated by ``angle``."""
        cx, cy = self.width / 2, self.height / 2
        t = math.radians(self.angle)
        xs, ys = [], []
        for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
            px, py = x * scale - cx, y * scale - cy
            xs.append(cx + px * math.cos(t) + py * math.sin(t))
            ys.append(cy - px * math.sin(t) + py * math.cos(t))
        return [min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale]


def degrade(png: Path, out_jpg: Path, rng: random.Random, *, quality: int = 60) -> Skew:
    """Make a clean render look scanned: a small skew, sensor noise, a
    grey paper tone and JPEG compression."""
    from PIL import Image, ImageFilter

    img = Image.open(png).convert("L")
    angle = rng.uniform(-1.8, 1.8)
    rotated = img.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
    noise = Image.effect_noise(rotated.size, rng.uniform(8, 14))
    toned = Image.blend(rotated, noise, 0.06).point(lambda v: min(255, int(v * 0.97 + 4)))
    toned.filter(ImageFilter.GaussianBlur(0.4)).save(out_jpg, "JPEG", quality=quality)
    return Skew(angle, img.width, img.height)


def image_pages_pdf(images: list[Path], out: Path, *, title: str, dpi: int = SCAN_DPI) -> None:
    """An image-only PDF, one full-page image per page (reportlab)."""
    from PIL import Image
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(out), invariant=1)
    c.setTitle(title)
    c.setAuthor("")
    c.setCreator("")
    for img in images:
        with Image.open(img) as im:
            w, h = im.width * 72 / dpi, im.height * 72 / dpi
        c.setPageSize((w, h))
        c.drawImage(str(img), 0, 0, w, h)
        c.showPage()
    c.save()


def tesseract_pdf(images: list[Path], out: Path, *, dpi: int = SCAN_DPI) -> None:
    """A searchable scan: tesseract's own PDF renderer (each page an
    image plus an invisible text layer). Title is set by post.py."""
    tess = _require("tesseract")
    work = Path(tempfile.mkdtemp(prefix="rs-tess-"))
    try:
        listing = work / "images.txt"
        listing.write_text("".join(f"{p}\n" for p in images))
        base = work / "out"
        subprocess.run([tess, str(listing), str(base), "-l", "eng", "--dpi", str(dpi), "pdf"],
                       check=True, capture_output=True)
        shutil.move(str(base.with_suffix(".pdf")), out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tesseract_version() -> str:
    try:
        out = subprocess.run([_require("tesseract"), "--version"], capture_output=True,
                             text=True, timeout=30).stdout
    except ProducerError:
        return "unavailable"
    return out.splitlines()[0].split()[-1] if out else "unknown"


# ── Quartz (macOS) ─────────────────────────────────────────────────────

def quartz_text_pdf(text: str, out: Path, *, title: str) -> None:
    """Plain text printed to PDF by macOS's cupsfilter (Quartz
    PDFContext, the producer of "Save as PDF"). The fabricated user name
    and title replace the CUPS defaults (the login name, the job name)."""
    work = Path(tempfile.mkdtemp(prefix="rs-cups-"))
    try:
        src = work / "letter.txt"
        src.write_text(text, encoding="utf-8")
        with open(out, "wb") as fh:
            subprocess.run([_require("cupsfilter"), "-U", CUPS_USER, "-t", title,
                            "-i", "text/plain", "-m", "application/pdf", str(src)],
                           stdout=fh, stderr=subprocess.DEVNULL, check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def poppler_version() -> str:
    try:
        out = subprocess.run([_require("pdftoppm"), "-v"], capture_output=True, text=True)
    except ProducerError:
        return "unavailable"
    line = (out.stderr or out.stdout).splitlines()
    return line[0].split()[-1] if line else "unknown"
