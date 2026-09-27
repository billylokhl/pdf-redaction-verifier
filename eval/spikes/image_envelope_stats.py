#!/usr/bin/env python3
"""Measures stored-image sizes against docs/adr/0004's envelope bounds,
on the real corpus -- behind docs/adr/0004's claims that "no tiny
images" exist and that no stored image comes near the 35 Mpx cap, both
of which review found false.

Walks every object in every file (doc.xref_length()), checks
/Subtype /Image, and reads /Width and /Height directly via
doc.xref_get_key -- cheap, no decompression needed. Three counts:

* "under 8 px": either dimension below 8 px;
* "not text-sized": today's `verify._text_sized` (imported, not
  reimplemented) is false -- under 8 px on the short side OR under
  32 px on the long side (the 8x32 rule `verify.py` uses to decide a
  leftover image is too small to be worth flagging); a superset of the
  first count, reported with the difference;
* "over 35 Mpx": width * height above docs/adr/0004's per-image cap.

Reported on both denominators; aggregate counts only.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import fitz  # noqa: E402

import corpus  # noqa: E402
import verify  # noqa: E402

TINY_FLOOR = 8
PIXEL_CAP = 35_000_000

_COUNTS = ("tiny", "not_text_sized", "not_text_sized_but_not_tiny", "over_cap")


def _empty_bucket() -> dict[str, Any]:
    bucket: dict[str, Any] = {"files_measured": 0, "largest_image_px": 0,
                              "largest_image_dims": None}
    for name in _COUNTS:
        bucket[f"files_with_{name}_image"] = 0
        bucket[f"total_{name}_images"] = 0
    return bucket


def measure(files: list[Path]) -> dict[str, Any]:
    buckets = {"all_files": _empty_bucket(), "text_bearing": _empty_bucket()}
    encrypted = errors = 0
    for f in files:
        try:
            doc = fitz.open(str(f))
        except Exception:
            errors += 1
            continue
        if doc.needs_pass:
            encrypted += 1
            doc.close()
            continue
        counts = dict.fromkeys(_COUNTS, 0)
        largest = (0, None)
        try:
            n = doc.xref_length()
        except Exception:
            n = 0
        for xref in range(1, n):
            try:
                if doc.xref_get_key(xref, "Subtype") != ("name", "/Image"):
                    continue
                w = int(doc.xref_get_key(xref, "Width")[1])
                h = int(doc.xref_get_key(xref, "Height")[1])
            except Exception:
                continue
            tiny = w < TINY_FLOOR or h < TINY_FLOOR
            small = not verify._text_sized(w, h)
            counts["tiny"] += tiny
            counts["not_text_sized"] += small
            counts["not_text_sized_but_not_tiny"] += small and not tiny
            counts["over_cap"] += w * h > PIXEL_CAP
            if w * h > largest[0]:
                largest = (w * h, (w, h))
        text_bearing = corpus.is_text_bearing(doc)
        doc.close()
        for key in (["all_files"] + (["text_bearing"] if text_bearing else [])):
            b = buckets[key]
            b["files_measured"] += 1
            for name, count in counts.items():
                if count:
                    b[f"files_with_{name}_image"] += 1
                    b[f"total_{name}_images"] += count
            if largest[0] > b["largest_image_px"]:
                b["largest_image_px"], b["largest_image_dims"] = largest

    out: dict[str, Any] = {"encrypted_skipped": encrypted, "errors": errors,
                            "tiny_floor_px": TINY_FLOOR,
                            "text_sized_rule_px": [verify._MIN_TEXT_IMAGE_SIDE,
                                                   verify._MIN_TEXT_IMAGE_LENGTH],
                            "pixel_cap": PIXEL_CAP}
    for key, b in buckets.items():
        n = b["files_measured"]
        out[key] = {"files_measured": n,
                    "largest_image_px": b["largest_image_px"],
                    "largest_image_dims": b["largest_image_dims"]}
        for name in _COUNTS:
            files_with = b[f"files_with_{name}_image"]
            out[key][name] = {"files_with": files_with,
                              "file_rate": files_with / n if n else None,
                              "images": b[f"total_{name}_images"]}
    return out


if __name__ == "__main__":
    t0 = time.time()
    files = corpus.discover()
    result = measure(files)
    result["elapsed_s"] = round(time.time() - t0, 2)
    print(json.dumps(result, indent=2))
