"""redaction_verifier.model — the pure data model.

Moved verbatim from verify.py as Phase 2, step 1 ("Move") of
docs/REDESIGN.md: VerifyError, the Secret/Finding/ScanReport data classes,
and the Warn/WarnList machinery that keeps every warning tied to a
registered code. Behaviour is byte-identical to the code this replaced;
verify.py re-exports every name here so existing imports, `verify.X`
references and the CLI keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class VerifyError(Exception):
    """Operational failure that must exit with code 2, never 1."""


# ──────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Secret:
    """A named sensitive value's normalized search key."""

    name: str
    normalized: str


@dataclass(frozen=True)
class Finding:
    """A single leak: which rule surfaced in which layer, and where.

    *sample* carries the raw matched text for pattern rules (empty for
    value secrets); it is masked and sanitized at render time only, and
    excluded from equality so dedup keys on (layer, rule, location).
    """

    layer: str          # Text | OCR | Metadata | Objects | Binary | Hidden
    secret_name: str
    location: str
    sample: str = field(default="", compare=False)
    # Where the matched content is stored (STORAGE_CLASSES) and, for the
    # Objects layer, which object and earlier revision; for page layers,
    # the page when the match is on one page. Informational: dedup keys
    # on (layer, rule, location) alone, which already encodes them.
    storage: str = field(default="live", compare=False)
    object: int | None = field(default=None, compare=False)
    revision: int | None = field(default=None, compare=False)
    page: int | None = field(default=None, compare=False)


# Every warning carries a stable code, so machine consumers (the --json
# report, the evaluation harness) never depend on message wording. The
# kind says why it blocks certification:
#   review   — a possible match a human must judge
#   coverage — content that was not (fully) scanned or read
#   scope    — the rules cannot express or verify what was asked
WARNING_CODES: dict[str, str] = {
    # review
    "REVIEW_CROSS_LINE": "review",
    "REVIEW_CROSS_PAGE": "review",
    "REVIEW_FUSED_PATTERN": "review",
    "REVIEW_HIDDEN_TEXT": "review",
    "REVIEW_METADATA_RAW": "review",
    "REVIEW_OBJECT_TEXT": "review",
    "REVIEW_ADJACENT_LITERALS": "review",
    "REVIEW_BINARY": "review",
    # coverage
    "PAGE_FAILED": "coverage",
    "LAYER_CRASHED": "coverage",
    "OCR_UNAVAILABLE": "coverage",
    "SKIPPED_FAIL_FAST": "coverage",
    "EMPTY_DOCUMENT": "coverage",
    "TOOL_MISSING": "coverage",
    "TOOL_START_FAILED": "coverage",
    "TOOL_TIMEOUT": "coverage",
    "TOOL_EXIT_NONZERO": "coverage",
    "TOOL_NO_OUTPUT": "coverage",
    "TOOL_OUTPUT_MALFORMED": "coverage",
    "XMP_UNREADABLE": "coverage",
    "XREF_UNREADABLE": "coverage",
    "OBJECT_UNREADABLE": "coverage",
    "UNTERMINATED_STRING": "coverage",
    "LEFTOVER_UNDECODABLE_TEXT": "coverage",
    "LEFTOVER_IMAGE": "coverage",
    "LEFTOVER_CONTAINER": "coverage",
    "PAYLOAD_TRUNCATED": "coverage",
    "REVISION_SCAN_FAILED": "coverage",
    "REVISION_UNREADABLE": "coverage",
    "REVISION_CAP": "coverage",
    "HIDDEN_ITEM_FAILED": "coverage",
    "ATTACHMENT_TOO_LARGE": "coverage",
    "ATTACHMENT_EMPTY": "coverage",
    "ATTACHMENT_NOT_TEXT": "coverage",
    # scope
    "RULES_UNKNOWN_KEY": "scope",
    "RULES_UNQUOTED_VALUE": "scope",
    "RULES_TRUNCATED_PATTERN": "scope",
    "RULES_CAPTURING_GROUP": "scope",
    "SCOPE_PARTIAL_ENTITY": "scope",
    "SCOPE_UNVERIFIABLE": "scope",
}


# Where the content a finding or warning is about is stored:
#   live         — content the current document uses
#   orphaned     — an object nothing references (reachability trusted)
#   unreferenced — not reached by a reachability walk that is not trusted
#   superseded   — an earlier revision's version of a rewritten object
STORAGE_CLASSES: frozenset[str] = frozenset({"live", "orphaned", "unreferenced", "superseded"})
# How the text a review match was found in was assembled — a match that
# needs joining, or comes from an arbitrary run of text, may be a
# coincidence (see the two-tier model in DESIGN.md).
ADJACENCY: frozenset[str] = frozenset(
    {"JOINED_LINES", "JOINED_PAGES", "JOINED_LITERALS", "NOISY_SOURCE"}
)
LAYERS: frozenset[str] = frozenset(
    {
        "Text",
        "OCR",
        "Metadata",
        "Objects",
        "Binary",
        "Hidden",
        "Metadata/Binary",
        "Rules",
        "Document",
    }
)
# Structured fields a warning may carry, beyond code, layer and message.
WARNING_FIELDS: tuple[str, ...] = (
    "storage",
    "rule",
    "adjacency",
    "tool",
    "page",
    "object",
    "revision",
    # The tool's own exit status on TOOL_EXIT_NONZERO, separated out from
    # the message text so a consumer can tell a real qpdf failure from its
    # benign (version-dependent) exit 3 "succeeded with warnings" without
    # parsing prose (see #3).
    "returncode",
)


class Warn(str):
    """A warning message with a stable code, the layer that raised it, and
    optional structured fields (see WARNING_FIELDS).

    A str subclass, so the human report and every existing comparison
    treat it as the message; the rest rides along for the JSON report.
    An unregistered code or field raises at construction, which a layer
    turns into a crash warning — never a silent pass.
    """

    code: str
    layer: str
    fields: dict[str, Any]

    def __new__(cls, code: str, layer: str, message: str, **fields: Any) -> "Warn":
        if code not in WARNING_CODES:
            raise ValueError(f"unregistered warning code {code!r}")
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}")
        unknown = set(fields) - set(WARNING_FIELDS)
        if unknown:
            raise ValueError(f"unknown warning field(s) {sorted(unknown)}")
        if fields.get("storage") not in STORAGE_CLASSES | {None}:
            raise ValueError(f"unknown storage class {fields['storage']!r}")
        if fields.get("adjacency") not in ADJACENCY | {None}:
            raise ValueError(f"unknown adjacency {fields['adjacency']!r}")
        self = super().__new__(cls, message)
        self.code = code
        self.layer = layer
        self.fields = fields
        return self

    @property
    def kind(self) -> str:
        return WARNING_CODES[self.code]


class WarnList(list):
    """A list that accepts only Warn items, so a warning cannot reach the
    report without a code — however it is added. Rejecting raises, which
    a layer turns into a crash warning: exit 2, never a silent pass."""

    @staticmethod
    def _check(items: Any) -> list[Warn]:
        items = list(items)
        for item in items:
            if not isinstance(item, Warn):
                raise TypeError(f"warnings must be Warn, not {type(item).__name__}")
        return items

    def append(self, item: Any) -> None:
        super().append(*self._check([item]))

    def extend(self, items: Any) -> None:
        super().extend(self._check(items))

    def insert(self, index: Any, item: Any) -> None:
        super().insert(index, *self._check([item]))

    def __iadd__(self, items: Any) -> "WarnList":  # type: ignore[misc]
        super().extend(self._check(items))
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            super().__setitem__(index, self._check(value))
        else:
            super().__setitem__(index, *self._check([value]))


@dataclass
class ScanReport:
    """Aggregated results across all layers. Findings are unique."""

    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=WarnList)
    _seen: set[Finding] = field(default_factory=set, repr=False)

    def warn(self, code: str, layer: str, message: str, **fields: Any) -> None:
        self.warnings.append(Warn(code, layer, message, **fields))

    def record(
        self, layer: str, secret_name: str, location: str, sample: str = "", **origin: Any
    ) -> None:
        finding = Finding(layer, secret_name, location, sample, **origin)
        if finding not in self._seen:
            self._seen.add(finding)
            self.findings.append(finding)

    @property
    def leaked(self) -> bool:
        return bool(self.findings)

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)
