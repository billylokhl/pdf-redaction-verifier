"""redaction_verifier.ledger — the ledger types of docs/REDESIGN.md §4.

Closed enums and frozen records for the new path (Phase 3a on), kept in
their own module so mypy can check them strictly (the legacy model is
not strict); ``redaction_verifier.model`` re-exports every name. A Flag
carries only a closed reason, an optional byte span and named integers
-- never document bytes -- so it can cross the child→parent boundary
(§4, "Process boundary") and appear in any log or report as is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias


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
