"""redaction_verifier.ledger — the ledger types of docs/REDESIGN.md §4.

Closed enums and frozen records for the new path (Phase 3a on), kept in
their own module so mypy can check them strictly (the legacy model is
not strict); ``redaction_verifier.model`` re-exports every name. A Flag
carries only a closed reason, an optional byte span and named integers
-- never document bytes -- so it can cross the child→parent boundary
(§4, "Process boundary") and appear in any log or report as is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, TypeAlias


class Status(Enum):
    """An obligation's state (Principle 1). Every obligation starts
    UNEXAMINED; only DECODED and NOT_APPLICABLE (with an NAReason) can
    ever discharge one."""

    UNEXAMINED = "unexamined"
    DECODED = "decoded"
    NOT_APPLICABLE = "not_applicable"
    FLAGGED = "flagged"
    UNREADABLE = "unreadable"
    FAILED = "failed"


class NAReason(Enum):
    """Exactly the four reasons of docs/adr/0003 -- extended only by ADR.
    None of them exempts a unit's bytes from the raw matcher (ADR 0003's
    governing rule)."""

    XREF_STREAM_FIELD_DATA = "xref_stream_field_data"
    OBJSTM_HEADER_TABLE = "objstm_header_table"
    FONT_PROGRAM_SPANNED = "font_program_spanned"
    IMAGE_DATA_CONSUMED = "image_data_consumed"


class FlagReason(Enum):
    """Why something was flagged. Closed: a member is added by the PR
    that first emits it, and tests pin that every member is emitted."""

    UNINDEXED_NON_WHITESPACE = "unindexed_non_whitespace"
    CONTESTED_SPAN = "contested_span"
    CLAIM_OUT_OF_RANGE = "claim_out_of_range"
    CLAIM_INVALID = "claim_invalid"
    SELF_OVERLAP = "self_overlap"
    BUDGET_EXHAUSTED = "budget_exhausted"
    # The lexer's (3a-2): a string without its closing delimiter, bad
    # bytes in a hex string or a name's #xx escape, a lone ')' or '>'.
    UNTERMINATED = "unterminated"
    INVALID_HEX_DIGIT = "invalid_hex_digit"
    INVALID_NAME_ESCAPE = "invalid_name_escape"
    STRAY_DELIMITER = "stray_delimiter"
    # The object parser's (3a-3).
    UNEXPECTED_TOKEN = "unexpected_token"      # a token where none fits
    MISSING_VALUE = "missing_value"            # a key or object with no value
    DUPLICATE_KEY = "duplicate_key"            # a dictionary key repeated
    NUMBER_OUT_OF_RANGE = "number_out_of_range"
    NESTING_LIMIT = "nesting_limit"            # containers nested too deep
    TOKEN_LIMIT = "token_limit"                # an object with too many tokens
    EXTRA_TOKENS = "extra_tokens"              # tokens after an object's value
    MISSING_ENDOBJ = "missing_endobj"
    STREAM_EOL = "stream_eol"                  # no end-of-line after `stream`
    ENDSTREAM_JOINED = "endstream_joined"      # `endstreamendobj`: readers disagree
    LENGTH_MISMATCH = "length_mismatch"        # /Length disagrees with endstream
    STREAM_SLACK = "stream_slack"              # bytes past /Length before endstream
    FLAGS_TRUNCATED = "flags_truncated"        # an object's flags past the cap
    # Capped Flate and predictors (3a-4a).
    FLATE_ERROR = "flate_error"                # zlib rejected the data (incl. checksum)
    FLATE_TRUNCATED = "flate_truncated"        # input ended before zlib's end marker
    AFTER_STREAM_END = "after_stream_end"      # bytes after zlib's end marker
    BAD_DECODE_PARMS = "bad_decode_parms"      # a predictor we cannot undo exactly
    PREDICTOR_ERROR = "predictor_error"        # bad row filter type, partial row
    # The cross-reference chain (3a-4b): canonical shapes only (ADR 0010).
    XREF_TAIL = "xref_tail"                    # the file does not end startxref N %%EOF
    XREF_NOT_FOUND = "xref_not_found"          # an offset holds no xref section
    XREF_TABLE_MALFORMED = "xref_table_malformed"
    XREF_STREAM_MALFORMED = "xref_stream_malformed"
    UNSUPPORTED_FILTER = "unsupported_filter"  # a filter the inventory does not decode
    XREF_CONFLICT = "xref_conflict"            # one object defined twice in a section
    XREF_OFFSET_MISMATCH = "xref_offset_mismatch"  # an entry not on its own N G obj
    PREV_CYCLE = "prev_cycle"                  # /Prev revisits a section, or too many
    MISSING_ROOT = "missing_root"              # a revision's trailer names no catalog
    MISSING_PAGE_TREE = "missing_page_tree"    # a revision's catalog /Pages is no dictionary
    XREF_SIZE_MISMATCH = "xref_size_mismatch"  # /Size is not 1 + the highest object
    HEADER_OFFSET = "header_offset"            # %PDF- is not at the file's first byte
    # The inventory (3a-5).
    XREF_EPILOGUE_MISMATCH = "xref_epilogue_mismatch"  # no startxref N %%EOF naming a revision
    REVISION_AMBIGUOUS = "revision_ambiguous"  # an object read differently across revisions
    OBJSTM_MALFORMED = "objstm_malformed"      # an object stream's /N, /First or header table
    OBJSTM_MEMBER_INVALID = "objstm_member_invalid"  # no value, a stream, an object stream
    OBJSTM_ENTRY_MISMATCH = "objstm_entry_mismatch"  # a compressed entry its home does not hold
    # The owner's pending decisions (3a-6c).
    COMMENT_LINE = "comment_line"  # a claimed comment line with control bytes or `N G obj`
    # The reference graph (3a-7, ADR 0011).
    REF_GENERATION = "ref_generation"  # `N G R` naming a live object of another generation
    REF_DANGLING = "ref_dangling"      # `N G R` naming no object (free, missing, object 0)
    PAGE_TREE = "page_tree"            # a page tree or page attribute off the canonical shape
    NAME_TREE = "name_tree"            # a name or number tree off the canonical shape
    RESOURCE_SCOPE = "resource_scope"  # a form, pattern, Type3 font or appearance without /Resources
    FIELD_TREE = "field_tree"          # a /Parent cycle among form fields (owner decision P5)
    APPEARANCE_UNSUPPORTED = "appearance_unsupported"  # MuPDF would synthesize it but cannot (#106)


Reason: TypeAlias = NAReason | FlagReason


class UnitKind(Enum):
    """What a unit of the file is. Top-level kinds partition the file's
    bytes (the tiling); OBJSTM_MEMBER and STREAM_SLACK sit inside another
    unit (``UnitRef.within``). WHITESPACE and UNINDEXED name the tiling's
    unclaimed gaps: no unit may claim bytes as either."""

    HEADER = "header"                  # %PDF-x.y and the binary-marker comment
    XREF_TABLE = "xref_table"          # classic xref section + trailer
    XREF_EPILOGUE = "xref_epilogue"    # startxref, its offset, %%EOF
    WHITESPACE = "whitespace"          # an unclaimed run of PDF whitespace
    UNINDEXED = "unindexed"            # an unclaimed run with other bytes
    OBJECT = "object"                  # N G obj ... endobj reached via xref
    DEAD_BODY = "dead_body"            # N G obj ... endobj no xref reaches
    OBJSTM_HEADER = "objstm_header"    # an /ObjStm's N G offset pairs
    OBJSTM_MEMBER = "objstm_member"    # an object inside an /ObjStm
    STREAM_SLACK = "stream_slack"      # bytes between /Length and endstream


_KIND_ORDER: dict[UnitKind, int] = {kind: i for i, kind in enumerate(UnitKind)}


def _is_count(value: object) -> bool:
    # Exactly int (no bool, IntEnum or other subclass), so every number a
    # Flag takes from a Span or UnitRef is a plain int too.
    return type(value) is int and value >= 0


@dataclass(frozen=True)
class UnitRef:
    """A unit's identity: its kind and start offset, within its parent
    unit's coordinates when nested. ``obj``/``gen`` are informational
    (a label for humans) and not part of identity."""

    kind: UnitKind
    start: int
    within: UnitRef | None = None
    obj: int | None = field(default=None, compare=False)
    gen: int | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, UnitKind):
            raise TypeError("UnitRef.kind must be a UnitKind")
        if not _is_count(self.start):
            raise ValueError("UnitRef.start must be an int >= 0")
        if self.within is not None and not isinstance(self.within, UnitRef):
            raise TypeError("UnitRef.within must be a UnitRef or None")
        # The label crosses the process boundary too: plain ints only.
        if not all(v is None or _is_count(v) for v in (self.obj, self.gen)):
            raise ValueError("UnitRef.obj and .gen must be ints >= 0 or None")

    def sort_key(self) -> tuple[tuple[int, int], ...]:
        """A total order consistent with ``==``: (start, kind) from the
        outermost unit inward. The walk itself is iterative, but ``==``
        and ``hash`` recurse through ``within``, so a deep chain must
        never be compared or hashed; ``tile`` refuses any nested ref
        before touching it."""
        path: list[tuple[int, int]] = []
        ref: UnitRef | None = self
        while ref is not None:
            path.append((ref.start, _KIND_ORDER[ref.kind]))
            ref = ref.within
        path.reverse()
        return tuple(path)

    def label_key(self) -> tuple[bool, int, bool, int]:
        """Orders equal refs by their informational label, smallest
        (obj, gen) first and unlabelled last, so a representative can be
        picked deterministically."""
        return (self.obj is None, self.obj or 0, self.gen is None, self.gen or 0)


@dataclass(frozen=True)
class Span:
    """A half-open byte range ``[start, end)``; empty when start == end."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (_is_count(self.start) and _is_count(self.end) and self.start <= self.end):
            raise ValueError("Span needs integers with 0 <= start <= end")

    def __len__(self) -> int:
        return self.end - self.start


def _is_param(pair: object) -> bool:
    if not (isinstance(pair, tuple) and len(pair) == 2):
        return False
    name, value = pair
    # Exact types, not subclasses: a str or int subclass could carry
    # document bytes across the process boundary in a custom repr.
    return (type(name) is str and name.isascii() and name.isidentifier()
            and type(value) is int)


@dataclass(frozen=True)
class Flag:
    """An anomaly: a closed reason, where (if anywhere), and named
    integers. Never document bytes."""

    reason: FlagReason
    span: Span | None = None
    params: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reason, FlagReason):
            raise TypeError("Flag.reason must be a FlagReason")
        if self.span is not None and not isinstance(self.span, Span):
            raise TypeError("Flag.span must be a Span or None")
        if not (isinstance(self.params, tuple) and all(_is_param(p) for p in self.params)):
            raise TypeError("Flag.params must be a tuple of (identifier, int) pairs")


@dataclass(frozen=True)
class Unit:
    """A unit of the file: its identity, the byte spans it covers (in
    its parent's coordinates) and anything flagged while finding it."""

    ref: UnitRef
    spans: tuple[Span, ...]
    flags: tuple[Flag, ...] = ()


# ──────────────────────────────────────────────────────────────────────────
# Phase 3b-1: obligations, witnesses and labels (docs/phase3b-plan.md §§2-4)
# ──────────────────────────────────────────────────────────────────────────
# One appended block, so other lanes' additions above rebase trivially.
# Every record here crosses the child→parent pipe or is built by the
# parent from what did: closed enums and plain integers only, never
# document bytes or text. (The protocol's one exception, `hello`'s
# SHA-256 digest of the file, is a frame of 3b-2's, not a ledger record.)

# PDF white-space characters (ISO 32000-1 §7.2.2, Table 1). Here, not in
# inventory/tiling.py (which re-exports it), so the parent re-checks a
# WHITESPACE region without importing the inventory.
WHITESPACE: Final = b"\0\t\n\f\r "
_NON_WHITESPACE: Final = re.compile(rb"[^\x00\t\n\x0c\r ]")


def first_non_whitespace(data: bytes, start: int = 0, end: int | None = None) -> int | None:
    """The offset of the first byte of ``data[start:end]`` that is not
    PDF whitespace, or None when there is none."""
    found = _NON_WHITESPACE.search(data, start, len(data) if end is None else end)
    return None if found is None else found.start()


def is_pdf_whitespace(data: bytes, start: int = 0, end: int | None = None) -> bool:
    """Is every byte of ``data[start:end]`` PDF whitespace? True when
    empty. The predicate a WHITESPACE region's bytes must meet."""
    return first_non_whitespace(data, start, end) is None


def check_tiling(size: int, spans: Iterable[Span]) -> bool:
    """Do *spans*, in order, tile ``[0, size)`` exactly: each non-empty
    and starting where the previous one ended? Pure; answers False to
    anything malformed. Geometry only: 3b's parent anchor must also
    re-check every WHITESPACE/UNINDEXED region against the raw bytes
    (``is_pdf_whitespace``), since a child that mislabels a gap passes
    this check. Moved here from inventory/tiling.py (which re-exports
    it) so the parent need not import the inventory."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return False
    pos = 0
    for span in spans:
        if not isinstance(span, Span) or span.start != pos or span.end <= span.start:
            return False
        pos = span.end
    return pos == size


class StructuralKind(Enum):
    """ADR 0011 item 5's closed STRUCTURAL list, one member per entry.
    Moved here from inventory/labels.py (which re-exports it)."""

    XREF_STREAM = "xref_stream"
    OBJECT_STREAM = "object_stream"
    HINT_STREAM = "hint_stream"
    XMP = "xmp"
    EMBEDDED_FILE = "embedded_file"
    JAVASCRIPT = "javascript"
    OUTPUT_INTENT = "output_intent"


class Label(Enum):
    """A stream body's labels across its revisions (ADR 0011 item 5;
    ``inventory.labels.BodyLabels``). A body may be both DRAWABLE and
    STRUCTURAL (then drawable: every label's decoder runs); ORPHANED only
    alone. SYNTHESIZED is not a label but a unit (``SynthesizedRef``)."""

    DRAWABLE = "drawable"
    STRUCTURAL = "structural"
    ORPHANED = "orphaned"


class PendingReason(Enum):
    """Why an obligation is still UNEXAMINED."""

    NO_DECODER = "no_decoder"      # the registry has no decoder for it
    NOT_IN_PHASE = "not_in_phase"  # its decoder's phase is later (page views until 4a)


class UnreadableReason(Enum):
    """Why an obligation is UNREADABLE."""

    HINT_PAYLOAD = "hint_payload"  # a hint stream: no hint-table decoder (ADR 0003, R6 (a))


class FailedReason(Enum):
    """Why an obligation is FAILED."""

    NOT_REPORTED = "not_reported"  # the child's report failed: no partial credit (§3)


class LedgerReason(Enum):
    """FLAGGED reasons the parent sets itself, from what it checks. Never
    a child's ``Flag``: a child's flag keeps its own FlagReason."""

    REVIEW_MATCH = "review_match"  # a RAW_MATCH with a review-tier match (§4, "Matching")


ObligationReason: TypeAlias = (NAReason | FlagReason | LedgerReason | PendingReason
                               | UnreadableReason | FailedReason)

# Which reasons each status takes: DECODED none, every other status one.
_REASONS: Final[dict[Status, tuple[type[Enum], ...]]] = {
    Status.UNEXAMINED: (PendingReason,),
    Status.DECODED: (),
    Status.NOT_APPLICABLE: (NAReason,),
    Status.FLAGGED: (FlagReason, LedgerReason),
    Status.UNREADABLE: (UnreadableReason,),
    Status.FAILED: (FailedReason,),
}


class Reading(Enum):
    """A page view's reading."""

    TEXT = "text"
    OCR = "ocr"


class Tool(Enum):
    """The external tools the ledger anchors on (3b-9: qpdf, run by the
    parent in its own process)."""

    QPDF = "qpdf"


class Stage(Enum):
    """A byte stage the raw matcher scans (§3, Q11 (iii)): the parent's
    own STORED bytes, and the decoded stages the child sends. Text
    strings are matched as text, binary strings as bytes, each in its
    own stage."""

    STORED = "stored"                    # [0, size) of the parent's descriptor; never sent
    XREF_FIELDS = "xref_fields"          # a cross-reference stream's field data, free entries too
    OBJSTM_PAYLOAD = "objstm_payload"    # an object stream's decoded payload
    STRING_TAGGED = "string_tagged"      # text strings, language tags kept (UTF-8, text)
    STRING_UNTAGGED = "string_untagged"  # text strings, language tags stripped (UTF-8, text)
    STRING_BINARY = "string_binary"      # non-text strings' bytes as written (bytes)
    READER_VIEW = "reader_view"          # a Decryption.views entry (ADR 0001's 3a-9a-1 note)


class FileScope(Enum):
    """Sentinel for a file-level stage: ``StageRef(FILE, ...)``."""

    FILE = "file"


FILE: Final = FileScope.FILE


class Check(Enum):
    """The file-level checks the parent makes. CHILD_COMPLETE,
    FILE_IDENTITY and TILING are the child-integrity checks, the only
    ones enforced in 3b (Q1); the rest are recorded."""

    CHILD_COMPLETE = "child_complete"              # a valid, complete report arrived
    FILE_IDENTITY = "file_identity"                # the child read the parent's bytes
    TILING = "tiling"                              # its regions tile the file
    PAGE_COUNT = "page_count"                      # its page list against qpdf's
    OBJECT_SET = "object_set"                      # its xref entries against qpdf's
    INVENTORY_FILE_FLAGS = "inventory_file_flags"  # file-level inventory flags
    PARSER_AGREEMENT = "parser_agreement"          # 3c's; UNEXAMINED in 3b
    RAW_MATCH = "raw_match"                        # one per StageRef (§4)


class Tier(Enum):
    """A finding's tier: HARD is exit 1, REVIEW exit 2. Every raw match
    is REVIEW in 3b (§4, "Matching"; Q13)."""

    HARD = "hard"
    REVIEW = "review"


def _require_counts(owner: str, **values: object) -> None:
    for name, value in values.items():
        if not _is_count(value):
            raise ValueError(f"{owner}.{name} must be an int >= 0")


@dataclass(frozen=True)
class SynthesizedRef:
    """A SYNTHESIZED unit (ADR 0011 item 5, R2): an appearance MuPDF
    synthesizes, one per (annotation body, revision). It claims no bytes,
    so it is not a UnitKind. ``index`` is the annotation's /Annots
    position when it is written directly in the page's array (``body``
    is then the page's), as ``inventory.annotations.Synthesized`` keeps
    it; None otherwise."""

    body: UnitRef
    revision: int
    index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.body, UnitRef):
            raise TypeError("SynthesizedRef.body must be a UnitRef")
        _require_counts("SynthesizedRef", revision=self.revision)
        if self.index is not None:
            _require_counts("SynthesizedRef", index=self.index)


@dataclass(frozen=True)
class ViewRef:
    """A page view: a page index of qpdf's count, and a reading."""

    page: int
    reading: Reading

    def __post_init__(self) -> None:
        _require_counts("ViewRef", page=self.page)
        if not isinstance(self.reading, Reading):
            raise TypeError("ViewRef.reading must be a Reading")


@dataclass(frozen=True)
class ToolRef:
    """An external tool's obligation."""

    tool: Tool

    def __post_init__(self) -> None:
        if not isinstance(self.tool, Tool):
            raise TypeError("ToolRef.tool must be a Tool")


@dataclass(frozen=True)
class RuleScopeRef:
    """A rule's scope obligation, by the rule's index in the rules file
    (the rule itself never leaves the parent)."""

    rule: int

    def __post_init__(self) -> None:
        _require_counts("RuleScopeRef", rule=self.rule)


@dataclass(frozen=True)
class StageRef:
    """A stage of a unit (by the report's unit index) or of the file
    (``FILE``). STORED is always ``StageRef(FILE, STORED)``: one stage
    over ``[0, size)``."""

    unit: int | FileScope
    stage: Stage

    def __post_init__(self) -> None:
        if not isinstance(self.unit, FileScope):
            _require_counts("StageRef", unit=self.unit)
        if not isinstance(self.stage, Stage):
            raise TypeError("StageRef.stage must be a Stage")
        if self.stage is Stage.STORED and self.unit is not FILE:
            raise ValueError("STORED is a file-level stage: StageRef(FILE, STORED)")


@dataclass(frozen=True)
class CheckRef:
    """A file-level check. RAW_MATCH is one obligation per stage, so it
    names its ``stage``; no other check has one."""

    check: Check
    stage: StageRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.check, Check):
            raise TypeError("CheckRef.check must be a Check")
        if self.stage is not None and not isinstance(self.stage, StageRef):
            raise TypeError("CheckRef.stage must be a StageRef or None")
        if (self.check is Check.RAW_MATCH) != (self.stage is not None):
            raise ValueError("CheckRef.stage is required for RAW_MATCH and only for it")


ObligationRef: TypeAlias = UnitRef | SynthesizedRef | ViewRef | ToolRef | RuleScopeRef | CheckRef
_OBLIGATION_REFS: Final = (UnitRef, SynthesizedRef, ViewRef, ToolRef, RuleScopeRef, CheckRef)


@dataclass(frozen=True)
class Witness:
    """What a decoder consumed (Principle 2, ADR 0008): byte spans, and
    the length they must tile. For a decoded stage, the spans are in its
    decoded coordinates and ``length`` is the stage's. For a stored unit
    (§3, B5), the coordinates are its claims concatenated in order, one
    span per claim, and ``length`` is their total; that the span count
    equals the claim count is checked where the claims are known (the
    protocol's validator), not here. An empty span is invalid (a
    protocol error, settling 3a-1's note). ``code_count`` and ``glyph_count`` are reserved for 4a's
    consumption witness (ADR 0008) and not checked before it."""

    spans: tuple[Span, ...]
    length: int
    code_count: int | None = None
    glyph_count: int | None = None

    def __post_init__(self) -> None:
        if not (isinstance(self.spans, tuple) and all(isinstance(s, Span) for s in self.spans)):
            raise TypeError("Witness.spans must be a tuple of Spans")
        if any(span.start == span.end for span in self.spans):
            raise ValueError("Witness.spans may not hold an empty span")
        _require_counts("Witness", length=self.length)
        if not all(v is None or _is_count(v) for v in (self.code_count, self.glyph_count)):
            raise ValueError("Witness.code_count and .glyph_count must be ints >= 0 or None")

    def balances(self) -> bool:
        """Do the spans, in order, tile ``[0, length)`` exactly?"""
        return check_tiling(self.length, self.spans)


def _check_outcome(owner: str, status: object, reason: object, witness: object) -> None:
    if not isinstance(status, Status):
        raise TypeError(f"{owner}.status must be a Status")
    allowed = _REASONS[status]
    if not allowed:
        if reason is not None:
            raise ValueError(f"{owner}: {status.name} takes no reason")
    elif not isinstance(reason, allowed):
        names = " or ".join(kind.__name__ for kind in allowed)
        raise ValueError(f"{owner}: {status.name} needs a {names}")
    if witness is not None and not isinstance(witness, Witness):
        raise TypeError(f"{owner}.witness must be a Witness or None")


@dataclass(frozen=True)
class DecodeResult:
    """A decoder's result for one (unit, label): its status, the reason
    every status but DECODED needs, and its witness. 3b's: no evidence
    or children yet (REDESIGN §4's sketch has both)."""

    status: Status
    reason: ObligationReason | None
    witness: Witness | None = None

    def __post_init__(self) -> None:
        _check_outcome("DecodeResult", self.status, self.reason, self.witness)


@dataclass(frozen=True)
class Obligation:
    """One thing the verdict needs discharged (Principle 1). The reason
    is required unless DECODED, and comes from that status's own closed
    set."""

    id: ObligationRef
    status: Status
    reason: ObligationReason | None
    witness: Witness | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, _OBLIGATION_REFS):
            raise TypeError("Obligation.id must be an obligation reference")
        _check_outcome("Obligation", self.status, self.reason, self.witness)


@dataclass(frozen=True)
class Finding:
    """A match: the rule's index, its tier and the stage it was found in.
    No text: the parent masks at report time."""

    rule: int
    tier: Tier
    stage: StageRef

    def __post_init__(self) -> None:
        _require_counts("Finding", rule=self.rule)
        if not isinstance(self.tier, Tier):
            raise TypeError("Finding.tier must be a Tier")
        if not isinstance(self.stage, StageRef):
            raise TypeError("Finding.stage must be a StageRef")
