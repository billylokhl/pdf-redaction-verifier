#!/usr/bin/env python3
"""The verifier's CLI, unchanged, plus a record of the shape of every
built-in pattern-class match it makes (Phase 5's false-hard baseline;
`scorecard.pattern_baseline` runs it, eval/README.md documents it).

Run exactly like `verify.py` (same arguments, same exit status, same
`--json` report). Every pattern match in the tool, at every layer and
tier, goes through `redaction_verifier.matching.patterns.match_patterns`
(`PatternScanner` calls it too); this wraps that function, returns its
result untouched, and for each hit notes the rule, the masked sample (the
same string the `--json` report shows, so a finding can be joined to its
record) and the match's shape from `scorecard.match_shapes`. When the
verdict is decided, the records go to the file named by the
PATTERN_RECORDER_OUT environment variable. The matched text itself is
never written anywhere.

A record cannot change the verdict: the wrapper returns what the wrapped
function returned, and a failure while classifying is recorded as the
shape "classifier-error" instead of raised.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Never this script's own directory first: scorecard's modules (report,
# keys, inventory, ...) must not shadow anything the verifier imports.
sys.path[0] = str(REPO)
sys.path.append(str(REPO / "eval"))

import verify  # noqa: E402
from redaction_verifier import matching as _matching  # noqa: E402
from redaction_verifier.matching import patterns as _patterns  # noqa: E402
from scorecard.match_shapes import OUT_ENV, classify  # noqa: E402

_records: dict[tuple[str, str, str, bool], None] = {}  # insertion-ordered set
_original_match_patterns = _patterns.match_patterns


def _recording_match_patterns(raw_text, patterns, already=(), *, collapse_separators=False):
    hits = _original_match_patterns(
        raw_text, patterns, already, collapse_separators=collapse_separators
    )
    if hits:
        try:
            # The text match_patterns itself searched (see its body).
            text = _patterns._fold_for_patterns(raw_text)
            if collapse_separators:
                text = re.sub(r"[-\s]{2,}", "-", text)
        except Exception:
            text = None
        for name, sample in hits.items():
            try:
                start = text.find(sample) if text is not None else -1
                if start < 0:
                    raise ValueError("match not found in its own text")
                shape = classify(name, text, start, start + len(sample))
            except Exception:
                shape = "classifier-error"
            _records[(name, _patterns.mask(sample), shape, collapse_separators)] = None
    return hits


def _write_records() -> None:
    out = os.environ.get(OUT_ENV)
    if not out:
        return
    path = Path(out)
    payload = [
        {"rule": rule, "sample": sample, "shape": shape, "collapsed": collapsed}
        for rule, sample, shape, collapsed in _records
    ]
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


_original_main = verify.main


def _main_and_record(argv=None):
    try:
        return _original_main(argv)
    finally:
        _write_records()


for _module in (_patterns, _matching, verify):
    _module.match_patterns = _recording_match_patterns  # type: ignore[attr-defined]
verify.main = _main_and_record

if __name__ == "__main__":
    verify.run_cli()
