"""ADR 0012's policy 1 in the case library (owner decisions 2026-10-15).

By default a built-in class match (`ssn`, `credit-card`, `email`,
`us-phone`) is an informational notice that never changes the exit code;
`"strict": true` on a class rule keeps it in the verdict. The library keeps
every layout tested both ways (the owner's decision 7):

- every case's class rules are marked `"strict": true`, so each case keeps
  the expectation it had before ADR 0012 (strict mode is exactly today's
  behaviour; the tool before ADR 0012 ignores the key, so the scorecard
  sees no difference on them);
- each case the owner signed off as reaching exit 0 under policy 1 gets one
  default-mode twin, `<id>-default-mode`: the same file and rules without
  `"strict"`, truth `clean` (the value is not a listed secret), expected
  exit 0. Each twin's difference from the reference tool is an accepted,
  `weaker: true` diff citing the sign-off.

`apply` runs once, from `caselib.load()`, after every family is imported.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .model import Case, expect

# The owner's per-case sign-off, 2026-10-15 (ADR 0012, "Cases that need the
# owner's sign-off"): every one an `ssn` class case, reason "the SSN is not
# a listed secret".
SIGNED_OFF_BY_EXIT = (  # 24: by the exit they give today
    # exit 1 today (9)
    "page.visible-pattern-rule", "layout.single-line-pattern-only",
    "layout.rotated-pattern-only", "layout.form-boxes-pattern-only",
    "layout.overlapping-values-pattern-only",
    "layout.overlapping-split-short-tail-pattern-only",
    "document.trailer-key-pattern-rule", "page.scan-image-sideways-90",
    "page.scan-image-sideways-270",
    # exit 2 today, class reviews only (14)
    "layout.split-lines-pattern-only", "layout.split-three-lines-pattern-only",
    "layout.split-pages-pattern-only", "layout.split-pages-needs-later-lines-pattern-only",
    "layout.split-three-pages-pattern-only", "layout.split-over-blank-page-pattern-only",
    "layout.two-page-breaks-pattern-only", "layout.overlapping-split-pages-pattern-only",
    "layout.repeated-line-wraps-pattern-only", "layout.several-values-pattern-only",
    "layout.table-rows-pattern-only", "layout.table-rows-split-pages-pattern-only",
    "layout.vertical-stack-pattern-only", "document.trailer-id-digits-pattern-rule",
    # the known gap K43
    "page.dense-sideways-scan",
)
SIGNED_OFF_BY_LABEL = (  # 11: known gaps whose target came from the class alone
    "document.af-pattern-rule", "document.js-stream-pattern-rule",
    "document.piece-info-pattern-rule", "page.hidden-sample-bytes-pattern-rule",
    "layout.form-boxes-split-pages-pattern-only", "layout.rotated270-split-pages-pattern-only",
    "layout.split-four-pages-pattern-only", "layout.split-pages-needs-earlier-lines-pattern-only",
    "layout.vertical-stack-split-pages-pattern-only", "layout.undashed-ssn-wrapped",
    "layout.rotated-split-pages-pattern-only",
)
SIGNED_OFF_OTHER_CLASSES = (  # 3: a second sign-off the same day, same reason
    # PR 3a's strict-coverage cases for the other classes (owner, 2026-10-15)
    "page.visible-us-phone-class-rule", "page.visible-credit-card-class-rule",
    "page.visible-email-class-rule",
)
SIGNED_OFF_CLEAN = (  # clean cases whose label the same decision covers
    "document.generated-id-digits", "page.digit-column",
)
SIGNED_OFF = (SIGNED_OFF_BY_EXIT + SIGNED_OFF_BY_LABEL + SIGNED_OFF_OTHER_CLASSES
              + SIGNED_OFF_CLEAN)
TWIN_SUFFIX = "-default-mode"

_applied = False


def _is_class(rule: dict[str, Any]) -> bool:
    return "class" in rule


def strict(rules: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """The rules with every class rule marked strict."""
    return tuple({**r, "strict": True} if _is_class(r) else r for r in rules)


def twin(original: Case, rules: tuple[dict[str, Any], ...]) -> Case:
    """The default-mode twin of a signed-off case: same file, *rules*
    without "strict", no listed secret in it, so it certifies."""
    return dataclasses.replace(
        original,
        id=original.id + TWIN_SUFFIX,
        truth="clean",
        cells=(),
        features=original.cells + original.features,
        expected=expect(0),
        story=(f"{original.story} Default mode (ADR 0012): the value is not one of the "
               "listed secrets, so the class match is an informational notice and the file "
               "certifies."),
        rules=rules,
        known_gap=None,
        older_macos=None,
        mistake="",
        recovery="",
    )


def apply(registry: dict[str, Case]) -> None:
    global _applied
    if _applied:
        return
    missing = [cid for cid in SIGNED_OFF if cid not in registry]
    if missing:
        raise ValueError(f"policy 1 sign-off names unknown case(s): {missing}")
    for cid, case in list(registry.items()):
        if any(_is_class(r) for r in case.rules):
            registry[cid] = dataclasses.replace(case, rules=strict(case.rules))
            if cid in SIGNED_OFF:
                tw = twin(case, case.rules)
                if tw.id in registry:
                    raise ValueError(f"duplicate case id {tw.id!r}")
                registry[tw.id] = tw
    _applied = True
