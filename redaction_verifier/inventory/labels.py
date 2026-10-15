"""The reference graph, part e (Phase 3a-7e, ADR 0011 item 5): labels.
This first part (3a-7e-1) gives each revision its reachable objects and
its STRUCTURAL streams; DRAWABLE and ORPHANED follow (3a-7e-2).

- **Reachable**: every object a reference reaches from the revision's
  trailer, following every edge item 4 collects whatever its use kind
  (graph.py: reachability must not rest on a kind), /Parent included
  (coordinator decision 2026-10-15, ADR 0011's notes): both readers keep
  exactly that set when they write the file (the gate's Check.REACHABLE).
  A reference reaches an object only when it resolves -- live in the
  revision with the same generation; R1 and REF_GENERATION flag the rest.
- **STRUCTURAL**, item 5's closed list, as records per stream body:
  - by the chain, reached by no reference: each cross-reference stream
    of the revision's chain (its own sections and every older one's, a
    hybrid's /XRefStm included); each object stream home to a compressed
    entry live in the revision (an /ObjStm home to none gets no label);
    and the hint stream, the stream object starting at the first offset
    /H names in the accepted linearization dictionary (xref.Chain), in
    every revision, the pair being the base -- with a stale /L too
    (an updated file): the pair is canonical (owner decision 7);
  - by key, from a reachable object: /Metadata (XMP), each entry of /EF
    (EMBEDDED_FILE; /EF itself may be a reference), /JS (JAVASCRIPT), and
    /DestOutputProfile in an item of the catalog's /OutputIntents
    (OUTPUT_INTENT; the array and its items direct or by reference) --
    the target a stream whose dictionary carries no /Subtype /Image or
    /Form (resolved), no /BBox and no /Width.
  A record holds only its kind and what the walk established (the
  reachable source, -1 for the chain, and the key). Whether the kind's
  decoder parses the whole stream is checked where the decoder runs (3b's
  registry, REDESIGN), so a record is a candidate until then; no label
  discharges a unit, and no status is assigned here.

Labels add no flag (owner decision R3). Revisions are read oldest first.
A revision whose trailer references are unchanged and that changes no
object the previous walk read shares its reachable set and by-key records
(by identity; and its whole record when the chain's structural streams
are unchanged too). A revision without a page list gets no labels, and
the next walks afresh (the review of #140's rule: nothing records what
the skipped revision changed). Iterative, never raises, every step
charged to the budget; a walk the budget cuts short leaves that revision
and every newer one None, and the budget's flag stands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..budget import Budget
from ..ledger import UnitRef
from .graph import ARRAY, Body, Edge
from .objects import PdfDict, PdfName, PdfRef


class StructuralKind(Enum):
    """ADR 0011 item 5's closed STRUCTURAL list, one member per entry."""

    XREF_STREAM = "xref_stream"
    OBJECT_STREAM = "object_stream"
    HINT_STREAM = "hint_stream"
    XMP = "xmp"
    EMBEDDED_FILE = "embedded_file"
    JAVASCRIPT = "javascript"
    OUTPUT_INTENT = "output_intent"


CHAIN: Final = -1  # a record's source when the chain, not a key, makes it structural


@dataclass(frozen=True)
class Structural:
    """Why a stream is structural, as far as the walk can tell: its kind,
    the reachable object whose key reaches it (CHAIN for the chain's own
    streams) and that key (b"" for the chain). A candidate until the
    kind's decoder parses the whole stream (3b)."""

    kind: StructuralKind
    source: int
    key: bytes


@dataclass(frozen=True, eq=False)
class RevisionLabels:
    """One revision's labels: the object numbers reachable from its
    trailer, and its STRUCTURAL streams' records by body. A revision that
    reads nothing new shares its predecessor's record (by identity).
    3a-7e-2 adds the DRAWABLE streams."""

    reachable: frozenset[int]
    structural: dict[UnitRef, tuple[Structural, ...]]


# Work charged per body looked up: a body_of lookup through the xref
# history (build.py's callbacks), several steps, not one.
BODY_WORK: Final = 3
# Work charged per element (reachable number, structural record) of a
# revision's labels made again after the first: retained memory, about 30
# bytes a frozenset element, that the file did not pay for -- a file whose
# updates rewrite a reachable object in every revision runs out of budget,
# not of memory (pages.py's PAGE_RECORD_WORK).
RECORD_WORK: Final = 32

_EF: Final = 1        # a body reached as /EF: each of its entries is an embedded file
_INTENTS: Final = 2   # the catalog's /OutputIntents array
_INTENT: Final = 3    # an item of it: its /DestOutputProfile
_IMAGE_OR_FORM: Final = (b"Image", b"Form")

# A by-key candidate: (kind, source, key, target number, target generation).
_Candidate = tuple[StructuralKind, int, bytes, int, int]
_Records = dict[UnitRef, dict[Structural, None]]


def build_labels(revisions: int, changes: list[list[int]],
                 body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None],
                 edges: dict[UnitRef, tuple[Edge, ...]],
                 trailers: tuple[tuple[Edge, ...], ...],
                 pages: tuple[object, ...],
                 xref_streams: list[list[UnitRef]],
                 hint: UnitRef | None,
                 budget: Budget) -> tuple[RevisionLabels | None, ...]:
    """Each revision's labels (None where it has no page list, or the
    budget ran out). *edges* and *trailers* are the graph's (graph.py);
    *pages* each revision's page list (only whether it has one is read);
    *xref_streams[r]* the cross-reference streams of revision r's own
    sections; *hint* the hint stream's unit, if any. No flags."""
    results: list[RevisionLabels | None] = [None] * revisions
    # The chain's structural streams, kept as the revisions go: the
    # cross-reference streams so far, and each object stream's live members.
    chain_units: list[UnitRef] = []
    members: dict[int, set[int]] = {}
    home_now: dict[int, int] = {}
    chain: tuple[_Records, set[int]] | None = None  # records, numbers read
    # The last walk: (numbers read, trailer references, reachable, by-key
    # records, the revision's labels).
    previous: tuple[set[int], tuple[object, ...], frozenset[int], _Records,
                    RevisionLabels] | None = None
    made = False
    for revision in reversed(range(revisions)):
        changed = set(changes[revision])
        if not budget.charge_work(len(changed) + len(xref_streams[revision]) + 1):
            break
        moved = False  # a home gained its first live member or lost its last
        for number in changes[revision]:
            if not budget.charge_work(1):
                return tuple(results)
            home = home_of(number, revision)
            if home_now.get(number) == home:
                continue
            if number in home_now:
                old = home_now.pop(number)
                members[old].discard(number)
                if not members[old]:
                    del members[old]
                    moved = True
            if home is not None:
                home_now[number] = home
                if home not in members:
                    members[home] = set()
                    moved = True
                members[home].add(number)
        chain_units += xref_streams[revision]
        if (moved or xref_streams[revision]
                or (chain is not None and not chain[1].isdisjoint(changed))):
            chain = None  # made again below, when this revision has labels
        if revision >= len(pages) or pages[revision] is None:
            previous = None
            continue
        trailer = trailers[revision]
        if not budget.charge_work(len(trailer) + 1):
            break
        ident = tuple((e.num, e.gen, e.path) for e in trailer)
        walk = _Walk(revision, body_of, home_of, edges, budget)
        fresh = chain is None
        if chain is None:
            records = walk.chain(chain_units, members, hint)
            if records is None:
                break
            chain = (records, walk.read)
            walk.read = set()
        if previous is not None and previous[1] == ident and previous[0].isdisjoint(changed):
            if not fresh:
                results[revision] = previous[4]  # nothing new read: shared
                continue
            read, reachable, by_key = previous[0], previous[2], previous[3]
        else:
            walked = walk.run(trailer)
            if walked is None:
                break
            read, by_key = walk.read, walked[1]
            reachable = frozenset(walked[0])
        if not budget.charge_work(len(by_key) + len(chain[0]) + 1):
            break
        labels = RevisionLabels(reachable, _merge(by_key, chain[0]))
        retained = len(labels.structural) + (
            len(reachable) if previous is None or reachable is not previous[2] else 0)
        if made and not budget.charge_work(RECORD_WORK * retained):
            break
        made = True
        results[revision] = labels
        previous = (read, ident, reachable, by_key, labels)
    return tuple(results)


def _merge(by_key: _Records, chain: _Records) -> dict[UnitRef, tuple[Structural, ...]]:
    """The records by unit, chain first, each once."""
    merged: _Records = {}
    for records in (chain, by_key):
        for unit, found in records.items():
            merged.setdefault(unit, {}).update(found)
    return {unit: tuple(found) for unit, found in merged.items()}


class _Walk:
    def __init__(self, revision: int, body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None],
                 edges: dict[UnitRef, tuple[Edge, ...]], budget: Budget) -> None:
        self.revision, self.body_of, self.home_of, self.edges, self.budget = (
            revision, body_of, home_of, edges, budget)
        self.read: set[int] = set()
        self.spent = False

    def charge(self, n: int = 1) -> bool:
        if not self.budget.charge_work(n):
            self.spent = True
        return not self.spent

    def body(self, number: int, gen: int) -> Body | None:
        """The live body ``number gen R`` names, recording what was read."""
        self.read.add(number)
        home = self.home_of(number, self.revision)
        if home is not None:
            self.read.add(home)
        if not self.charge(BODY_WORK):
            return None
        body = self.body_of(number, self.revision)
        return body if body is not None and body[2] == gen and number != 0 else None

    def chain(self, units: list[UnitRef], members: dict[int, set[int]],
              hint: UnitRef | None) -> _Records | None:
        """The chain's structural streams in this revision."""
        records: _Records = {}
        if not self.charge(len(units) + len(members) + 1):
            return None
        for unit in units:
            records.setdefault(unit, {})[Structural(StructuralKind.XREF_STREAM, CHAIN, b"")] = None
        for home in sorted(members):
            body = self.body(home, 0)
            if self.spent:
                return None
            if body is not None and body[4]:
                records.setdefault(body[0], {})[
                    Structural(StructuralKind.OBJECT_STREAM, CHAIN, b"")] = None
        if hint is not None:
            records.setdefault(hint, {})[Structural(StructuralKind.HINT_STREAM, CHAIN, b"")] = None
        return records

    def run(self, trailer: tuple[Edge, ...]) -> tuple[set[int], _Records] | None:
        """The reachable numbers and the by-key records; None when spent."""
        reachable: set[int] = set()
        roots = [e.num for e in trailer if e.path == (b"Root",)]
        catalog = roots[0] if roots else None
        candidates: list[_Candidate] = []
        contexts: list[tuple[int, int, int]] = []  # (number, gen, context)
        stack = [(e.num, e.gen) for e in reversed(trailer)]
        while stack:
            if not self.charge():
                return None
            number, gen = stack.pop()
            if number in reachable:
                continue
            body = self.body(number, gen)
            if self.spent:
                return None
            if body is None:
                continue
            reachable.add(number)
            found = self.edges.get(body[0])
            if found is None or not self.charge(len(found) + 1):
                self.spent = True  # the graph stopped short: the budget is spent
                return None
            for edge in found:
                _match(number, edge, number == catalog, candidates, contexts)
                if edge.num not in reachable:
                    stack.append((edge.num, edge.gen))
        if not self.contexts(contexts, candidates):
            return None
        return reachable, self.records(candidates)

    def contexts(self, contexts: list[tuple[int, int, int]],
                 candidates: list[_Candidate]) -> bool:
        """The edges of bodies reached as /EF, the catalog's
        /OutputIntents or one of its items (each reachable already)."""
        seen: set[tuple[int, int]] = set()
        while contexts:
            if not self.charge():
                return False
            number, gen, context = contexts.pop()
            if (number, context) in seen:
                continue
            seen.add((number, context))
            body = self.body(number, gen)
            if self.spent:
                return False
            found = self.edges.get(body[0]) if body is not None else ()
            if found is None or not self.charge(len(found) + 1):
                self.spent = True
                return False
            for edge in found:
                if context == _EF and len(edge.path) == 1:
                    candidates.append((StructuralKind.EMBEDDED_FILE, number, b"EF",
                                       edge.num, edge.gen))
                elif context == _INTENTS:
                    _intents(number, edge, edge.path, candidates, contexts)
                elif context == _INTENT and edge.path == (b"DestOutputProfile",):
                    candidates.append((StructuralKind.OUTPUT_INTENT, number,
                                       b"DestOutputProfile", edge.num, edge.gen))
        return True

    def records(self, candidates: list[_Candidate]) -> _Records:
        """Each candidate whose target is a stream with no image or form
        keys. The caller checks ``spent``."""
        records: _Records = {}
        for kind, source, key, number, gen in candidates:
            body = self.body(number, gen)
            if self.spent:
                break
            if body is None or not body[4] or not self.plain(body):
                continue
            records.setdefault(body[0], {})[Structural(kind, source, key)] = None
        return records

    def plain(self, body: Body) -> bool:
        """The stream's dictionary carries no /Subtype /Image or /Form
        (resolved: one reference followed; anything still unread fails
        closed), no /BBox and no /Width."""
        value = body[1]
        if not isinstance(value, PdfDict) or not self.charge(2 * len(value.entries) + 1):
            return False
        keys = {name.raw for name, _ in value.entries}
        if b"BBox" in keys or b"Width" in keys:
            return False
        subtype = value.get(b"Subtype")
        if isinstance(subtype, PdfRef):
            held = self.body(subtype.num, subtype.gen)
            subtype = held[1] if held is not None else None
            if isinstance(subtype, PdfRef):
                return False
        return not (isinstance(subtype, PdfName) and subtype.raw in _IMAGE_OR_FORM)


def _match(number: int, edge: Edge, catalog: bool, candidates: list[_Candidate],
           contexts: list[tuple[int, int, int]]) -> None:
    """Record *edge* (held by reachable object *number*) when its key path
    makes its target a structural candidate, or a body whose own edges
    may (/EF by reference, the catalog's /OutputIntents)."""
    path = edge.path
    if not path:
        return
    last = path[-1]
    if last == b"Metadata":
        candidates.append((StructuralKind.XMP, number, last, edge.num, edge.gen))
    elif last == b"JS":
        candidates.append((StructuralKind.JAVASCRIPT, number, last, edge.num, edge.gen))
    elif last == b"EF":
        contexts.append((edge.num, edge.gen, _EF))
    if len(path) >= 2 and path[-2] == b"EF":
        candidates.append((StructuralKind.EMBEDDED_FILE, number, b"EF", edge.num, edge.gen))
    if catalog and path[0] == b"OutputIntents":
        _intents(number, edge, path[1:], candidates, contexts)


def _intents(number: int, edge: Edge, rest: tuple[bytes, ...], candidates: list[_Candidate],
             contexts: list[tuple[int, int, int]]) -> None:
    """An edge at *rest* inside the catalog's /OutputIntents array."""
    if rest == (ARRAY, b"DestOutputProfile"):
        candidates.append((StructuralKind.OUTPUT_INTENT, number, b"DestOutputProfile",
                           edge.num, edge.gen))
    elif rest == (ARRAY,):
        contexts.append((edge.num, edge.gen, _INTENT))
    elif rest == ():
        contexts.append((edge.num, edge.gen, _INTENTS))
