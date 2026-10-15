#!/usr/bin/env python3
"""The verifier's CLI, unchanged, plus a record of the shape of every
built-in pattern-class match it makes (Phase 5's false-hard baseline;
`scorecard.pattern_baseline` runs it, eval/README.md documents it).

Run exactly like `verify.py` (same arguments, same exit status, same
`--json` report). Every pattern match in the tool, at every layer and
tier, goes through `redaction_verifier.matching.patterns.match_patterns`
(`PatternScanner` calls it too); `Recorder.install` wraps that function,
which still returns its own result object untouched, and for each hit
notes the match's shape from `scorecard.match_shapes` under a hash of
(rule, masked sample) -- the masked sample is the string the `--json`
report shows, so a finding can be joined to its record. When the verdict
is decided, the records go to the file named by the PATTERN_RECORDER_OUT
environment variable. Neither the matched text nor the masked sample is
ever written.

A record cannot change the verdict: the wrapper returns what the wrapped
function returned (or raises what it raised), and every step of
recording runs under a catch-all -- a failure while classifying is
recorded as the shape "classifier-error", any other failure drops the
record, never the hit. tests/test_pattern_baseline.py runs every
case-library case that has a pattern rule with and without the recorder
and requires identical reports.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]


class Recorder:
    """Wraps match_patterns wherever the tool looks it up (the patterns
    module, the matching package, verify's re-export) while installed."""

    def __init__(self) -> None:
        # Insertion-ordered set of (key, shape, collapsed).
        self.records: dict[tuple[str, str, bool], None] = {}
        self._saved: list[tuple[Any, Callable[..., Any]]] = []

    def install(self) -> None:
        import verify
        from redaction_verifier import matching
        from redaction_verifier.matching import patterns

        original = patterns.match_patterns

        def recording(raw_text, patterns_, already=(), *, collapse_separators=False):
            hits = original(raw_text, patterns_, already, collapse_separators=collapse_separators)
            if hits:
                self._record(raw_text, hits, collapse_separators)
            return hits

        for module in (patterns, matching, verify):
            self._saved.append((module, module.match_patterns))
            module.match_patterns = recording  # type: ignore[attr-defined]

    def uninstall(self) -> None:
        while self._saved:
            module, function = self._saved.pop()
            module.match_patterns = function

    def _record(self, raw_text: str, hits: dict[str, str], collapsed: bool) -> None:
        try:
            from redaction_verifier.matching import patterns
            from scorecard.match_shapes import classify, record_key

            try:
                # The text match_patterns itself searched (see its body).
                folded = patterns._fold_for_patterns(raw_text)
                text: str | None = (
                    re.sub(r"[-\s]{2,}", "-", folded) if collapsed else folded)
            except Exception:
                text = None
            for name, sample in list(hits.items()):
                try:
                    if text is None:
                        raise ValueError("text could not be folded")
                    start = text.find(sample)
                    if start < 0:
                        raise ValueError("match not found in its own text")
                    shape = classify(name, text, start, start + len(sample))
                except Exception:
                    shape = "classifier-error"
                self.records[(record_key(name, patterns.mask(sample)), shape, collapsed)] = None
        except Exception:
            pass  # a lost record shows up as an unjoined finding, never as a changed verdict

    def write(self, path: Path) -> None:
        payload = [
            {"key": key, "shape": shape, "collapsed": collapsed}
            for key, shape, collapsed in self.records
        ]
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)


def _run() -> None:
    # Never this script's own directory first: scorecard's modules (report,
    # keys, inventory, ...) must not shadow anything the verifier imports.
    sys.path[0] = str(REPO)
    sys.path.append(str(REPO / "eval"))
    import verify
    from scorecard.match_shapes import OUT_ENV

    recorder = Recorder()
    recorder.install()
    original_main = verify.main

    def main_and_record(argv=None):
        try:
            return original_main(argv)
        finally:
            out = os.environ.get(OUT_ENV)
            if out:
                try:
                    recorder.write(Path(out))
                except Exception:
                    pass  # no record file: every finding counts as unjoined

    verify.main = main_and_record
    verify.run_cli()


if __name__ == "__main__":
    _run()
