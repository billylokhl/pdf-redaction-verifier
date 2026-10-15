#!/usr/bin/env python3
"""The verifier's CLI, unchanged, plus a record of the shape of every
built-in pattern-class match it makes (Phase 5's false-hard baseline;
`scorecard.pattern_baseline` runs it, eval/README.md documents it).

Run exactly like `verify.py` (same arguments, same exit status, same
`--json` report). Every pattern match in the tool, at every layer and
tier, goes through `redaction_verifier.matching.patterns.match_patterns`
(`PatternScanner` calls it too); `Recorder.install` wraps that function,
which still returns its own result object untouched. For each call that
could yield a hard finding (no `collapse_separators`) it records the shape
(`scorecard.match_shapes`) of every validated match, at its exact span,
and, for each hit the call returns, a salted hash of (rule, masked sample)
with that hit's shape -- the masked sample is what the `--json` report
shows, so a finding can be joined to its shape. When the verdict is
decided, the records go to the file named by PATTERN_RECORDER_OUT; the
salt comes from PATTERN_RECORDER_SALT and is never written. Neither the
matched text nor the masked sample is ever written.

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
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

REPO = Path(__file__).resolve().parents[2]


class Recorder:
    """Wraps match_patterns wherever the tool looks it up (the patterns
    module, the matching package, verify's re-export) while installed.

    Only calls without `collapse_separators` are recorded: those are the
    only calls a hard finding can come from (a collapsed call is the
    manual-review tier). Two records per call:

    - `joins`: for each hit the call returns, a salted key of (rule, masked
      sample) and the shape of that exact match -- how a --json finding,
      which shows only its masked sample, finds its shape;
    - `shapes`: per rule, the shape of *every* validated match in the call's
      text, including rules `already` hit and matches after the first. The
      tool stops at a rule's first match per page; a demotion would not,
      so this is what `pattern_baseline`'s residual estimate reads.
    """

    def __init__(self, salt: str = "") -> None:
        self.salt = salt
        self.joins: dict[tuple[str, str], None] = {}  # insertion-ordered set of (key, shape)
        self.shapes: dict[str, set[str]] = {}
        self._saved: list[tuple[Any, Callable[..., Any]]] = []

    def install(self) -> None:
        import verify
        from redaction_verifier import matching
        from redaction_verifier.matching import patterns

        original = patterns.match_patterns

        def recording(raw_text, patterns_, already=(), *, collapse_separators=False):
            hits = original(raw_text, patterns_, already, collapse_separators=collapse_separators)
            if not collapse_separators:
                self._record(raw_text, patterns_, hits)
            return hits

        for module in (patterns, matching, verify):
            self._saved.append((module, module.match_patterns))
            module.match_patterns = recording  # type: ignore[attr-defined]

    def uninstall(self) -> None:
        while self._saved:
            module, function = self._saved.pop()
            module.match_patterns = function

    def _record(self, raw_text: str, rules: Any, hits: dict[str, str]) -> None:
        try:
            from redaction_verifier.matching import patterns
            from scorecard.match_shapes import classify, record_key

            # The text match_patterns itself searched (see its body).
            text = patterns._fold_for_patterns(raw_text)
            for rule in rules:
                first: tuple[str, str] | None = None
                cls = _builtin_class(rule)
                for start, end in _validated_matches(rule, text):
                    try:
                        shape = classify(cls, text, start, end)
                    except Exception:
                        shape = "classifier-error"
                    self.shapes.setdefault(rule.name, set()).add(shape)
                    if first is None:
                        first = (text[start:end], shape)
                if rule.name in hits:
                    # match_patterns' hit is the first validated match of the
                    # same loop; anything else would be a bug here, recorded
                    # as such rather than guessed.
                    sample = hits[rule.name]
                    shape = first[1] if first and first[0] == sample else "classifier-error"
                    self.joins[(record_key(self.salt, rule.name, patterns.mask(sample)), shape)] = None
        except Exception:
            pass  # a lost record shows up as an unjoined finding, never as a changed verdict

    def write(self, path: Path) -> None:
        payload = {
            "joins": [{"key": key, "shape": shape} for key, shape in self.joins],
            "shapes": {rule: sorted(shapes) for rule, shapes in sorted(self.shapes.items())},
        }
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)


def _builtin_class(rule: Any) -> str:
    """The built-in class a rule is (its regex is that class's own), so a
    rule named otherwise ("Any SSN") is classified as its class; a custom
    pattern gets the empty class, which no class-specific shape fits."""
    from redaction_verifier.matching.patterns import BUILTIN_PATTERN_CLASSES

    for name, (source, _validator) in BUILTIN_PATTERN_CLASSES.items():
        if rule.regex.pattern == source:
            return name
    return ""


def _validated_matches(rule: Any, text: str) -> Iterator[tuple[int, int]]:
    """Every match of *rule* in *text* that match_patterns would accept, in
    order: its own search loop (a rejected candidate retries one character
    on, a zero-width match is skipped), continued past each accepted one."""
    pos = 0
    while pos <= len(text):
        m = rule.regex.search(text, pos)
        if m is None:
            return
        if not m.group(0):
            pos = m.end() + 1
            continue
        if rule.validator is None or rule.validator(m.group(0)):
            yield m.start(), m.end()
            pos = m.end()
        else:
            pos = m.start() + 1


def _run() -> None:
    # Never this script's own directory first: scorecard's modules (report,
    # keys, inventory, ...) must not shadow anything the verifier imports.
    sys.path[0] = str(REPO)
    sys.path.append(str(REPO / "eval"))
    import verify
    from scorecard.match_shapes import OUT_ENV, SALT_ENV

    recorder = Recorder(os.environ.get(SALT_ENV, ""))
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
