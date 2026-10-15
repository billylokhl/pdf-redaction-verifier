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
    every revision whose reachable objects do not include it (item 5:
    nothing references it), the pair being the base -- with a stale /L
    too (an updated file): the pair is canonical (owner decision 7);
  - by key, from a reachable object, each key only on the holder item 5
    names, so a stream drawn under a name that happens to be one of these
    keys is never labelled (the review of #153):
    - /Metadata (XMP) on a dictionary or stream whose /Type is one of
      METADATA_TYPES or whose /Subtype is /Image or /Form;
    - /EF's entries (EMBEDDED_FILE; /EF itself may be a reference) on a
      file specification, /Type /Filespec (required with /EF);
    - /JS (JAVASCRIPT) on an action whose /S is /JavaScript;
    - /DestOutputProfile in an item of the catalog's /OutputIntents
      (OUTPUT_INTENT; the array and its items direct or by reference);
    and never on a holder that is itself a resource-category or
    /CharProcs dictionary -- written under one of those keys, or a body
    some reachable reference names under one -- whose entries are drawn
    by name. /Type, /Subtype and /S are read through one reference. The
    target is a stream whose dictionary carries no /Subtype /Image or
    /Form (resolved), no /BBox and no /Width. When in doubt, no label:
    an unlabelled stream fails closed (ORPHANED, 3a-7e-2).
  A record holds only its kind and what the walk established (the
  reachable source, -1 for the chain, and the key). Whether the kind's
  decoder parses the whole stream is checked where the decoder runs (3b's
  registry, REDESIGN), so a record is a candidate until then; no label
  discharges a unit, and no status is assigned here.

Labels add no flag (owner decision R3). Revisions are read oldest first.
A revision whose trailer references are unchanged and that changes no
object the previous walk read -- or only reachable objects whose new
bodies hold the same references (number, generation, key path) and that
no by-key record read and no object stream is (a rewritten catalog, an
update's usual shape) -- keeps the last walk's reachable set and by-key
records, by identity, as does a revision walked again to the same ones.
The chain's records are shared too: the cross-reference streams are one
list for the whole file, each revision naming how many apply. A revision
without a page list gets no labels, and the next walks afresh (the review
of #140's rule: nothing records what the skipped revision changed).

Memory: whatever a revision retains that is new -- its labels record, a
new reachable set, new by-key records, a new object-stream set, each
cross-reference stream appended -- is charged to the work budget at least
its size in bytes (the *_WORK constants, measured with sys.getsizeof), so
labels retain at most one byte per unit of work: at most the budget's 64
per byte of the file (DESIGN.md, "Bounded resources"). Iterative,
never raises, every step charged; a walk the budget cuts short leaves that
revision and every newer one None, and the budget's flag stands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..budget import Budget
from ..ledger import UnitRef
from .graph import ARRAY, Body, Edge
from .objects import PdfArray, PdfDict, PdfName, PdfRef, PdfValue


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


@dataclass(frozen=True, slots=True)
class Structural:
    """Why a stream is structural, as far as the walk can tell: its kind,
    the reachable object whose key reaches it (CHAIN for the chain's own
    streams) and that key (b"" for the chain). A candidate until the
    kind's decoder parses the whole stream (3b)."""

    kind: StructuralKind
    source: int
    key: bytes


_XREF: Final = Structural(StructuralKind.XREF_STREAM, CHAIN, b"")
_OBJSTM: Final = Structural(StructuralKind.OBJECT_STREAM, CHAIN, b"")
_HINT: Final = Structural(StructuralKind.HINT_STREAM, CHAIN, b"")

ByKey = dict[UnitRef, tuple[Structural, ...]]


@dataclass(frozen=True, eq=False, slots=True)
class RevisionLabels:
    """One revision's labels: the object numbers reachable from its
    trailer, and its STRUCTURAL streams -- ``structural``, made on each
    call from parts shared across revisions: the by-key records, the
    file's cross-reference streams (``xref_streams``, oldest first; the
    first ``xref_count`` are this revision's chain), the object streams
    home to a live entry, and the hint stream. A revision that reads
    nothing new shares its predecessor's record (by identity). 3a-7e-2
    adds the DRAWABLE streams."""

    reachable: frozenset[int]
    by_key: ByKey
    xref_streams: list[UnitRef]
    xref_count: int
    object_streams: frozenset[UnitRef]
    hint: UnitRef | None

    @property
    def structural(self) -> dict[UnitRef, tuple[Structural, ...]]:
        """Every structural stream's records, the chain's first: made on
        each call (not kept), in time linear in the records."""
        found: dict[UnitRef, tuple[Structural, ...]] = {}
        for unit in self.xref_streams[:self.xref_count]:
            found[unit] = (_XREF,)
        for unit in sorted(self.object_streams, key=lambda u: u.sort_key()):
            found[unit] = (*found.get(unit, ()), _OBJSTM)
        if self.hint is not None:
            found[self.hint] = (*found.get(self.hint, ()), _HINT)
        for unit, records in self.by_key.items():
            found[unit] = (*found.get(unit, ()), *records)
        return found


# Work charged per body looked up: a body_of lookup through the xref
# history (build.py's callbacks), several steps, not one.
BODY_WORK: Final = 3
# Work charged per revision for its own bookkeeping (a revision that reads
# nothing new still costs a few dozen steps).
REVISION_WORK: Final = 8
# Retained memory, charged at least its size in bytes (sys.getsizeof,
# CPython 3.12, measured in tests/test_inventory_labels.py): a new set or
# dictionary (an empty frozenset is about 200 bytes), a reachable number or
# object stream in a frozenset (its slot; the ints are the graph's), a
# by-key record (its dict slot, tuple and Structural), a revision's labels
# record, a cross-reference stream in the shared list.
SET_WORK: Final = 256
RECORD_WORK: Final = 64
BY_KEY_WORK: Final = 384
LABELS_WORK: Final = 192
LIST_WORK: Final = 8

_EF: Final = 1        # a body reached as /EF: each of its entries is an embedded file
_INTENTS: Final = 2   # the catalog's /OutputIntents array
_INTENT: Final = 3    # an item of it: its /DestOutputProfile
_IMAGE_OR_FORM: Final = (b"Image", b"Form")
# Dictionaries whose entries are drawn by name: a key in one is a
# resource's name, never item 5's key.
_DRAWN_BY_NAME: Final = frozenset({
    b"XObject", b"Font", b"Pattern", b"Shading", b"ExtGState", b"ColorSpace", b"Properties",
    b"CharProcs"})
# The holders /Metadata is read on (item 5: "an XMP packet under /Metadata"):
# by /Type, or an image or form XObject by /Subtype. Any other holder: no
# label (fail closed).
METADATA_TYPES: Final = frozenset({
    b"Catalog", b"Pages", b"Page", b"Template", b"Font", b"FontDescriptor", b"EmbeddedFile",
    b"Filespec", b"StructTreeRoot", b"StructElem", b"OCG", b"XObject"})
_KEYS: Final = frozenset({b"Metadata", b"JS", b"EF"})

# A by-key candidate: (kind, source, key, target number, target generation,
# whether its holder is the source's top level).
_Candidate = tuple[StructuralKind, int, bytes, int, int, bool]
# A context body: (number, gen, context, whether its holder is the source's
# top level, source).
_Context = tuple[int, int, int, bool, int]


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
    # cross-reference streams so far (one list, shared by every revision's
    # labels), and each object stream's live members.
    chain_units: list[UnitRef] = []
    members: dict[int, set[int]] = {}
    homes: set[int] = set()  # every number that has been a home
    home_now: dict[int, int] = {}
    chain: tuple[frozenset[UnitRef], set[int]] | None = None  # object streams, numbers read
    last: _Last | None = None
    for revision in reversed(range(revisions)):
        changed = set(changes[revision])
        added = xref_streams[revision]
        if not budget.charge_work(len(changed) + LIST_WORK * len(added) + REVISION_WORK):
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
                homes.add(home)
                if home not in members:
                    members[home] = set()
                    moved = True
                members[home].add(number)
        chain_units += added
        if moved or (chain is not None and not chain[1].isdisjoint(changed)):
            chain = None  # made again below, when this revision has labels
        if revision >= len(pages) or pages[revision] is None:
            last = None
            continue
        trailer = trailers[revision]
        if not budget.charge_work(len(trailer) + 1):
            break
        ident = tuple((e.num, e.gen, e.path) for e in trailer)
        walk = _Walk(revision, body_of, home_of, edges, budget)
        if chain is None:
            units = walk.object_streams(members)
            if units is None:
                break
            if last is not None and units == last.labels.object_streams:
                units = last.labels.object_streams
            chain = (units, walk.read)
            walk.read = set()
        # The reuse rule's ident condition; the reachable condition in
        # same() is implied by the keyed and read ones (a number the walk
        # read but did not reach is an unresolved target: rewriting it to
        # resolve changes its references' targets' bodies only through
        # read numbers) -- an equivalent mutant, kept as a guard.
        same = last is not None and last.ident == ident and walk.same(
            last.read & changed, last, homes)
        if walk.spent:
            break
        if same:
            assert last is not None
            read, keyed, sources = last.read, last.keyed, last.sources
            reachable, by_key = last.labels.reachable, last.labels.by_key
        else:
            walked = walk.run(trailer)
            if walked is None:
                break
            read, keyed, sources = walk.read, walk.keyed, walk.sources
            reachable, by_key = frozenset(walked[0]), walked[1]
            if last is not None and reachable == last.labels.reachable:
                reachable = last.labels.reachable  # walked again, the same: kept once
            if last is not None and by_key == last.labels.by_key:
                by_key = last.labels.by_key
        here = hint if hint is not None and hint.obj not in reachable else None
        previous = last.labels if last is not None else None
        if (previous is not None and reachable is previous.reachable
                and by_key is previous.by_key and chain[0] is previous.object_streams
                and len(chain_units) == previous.xref_count):
            # (The hint stream follows: it depends on the reachable set only.)
            labels = previous  # nothing new: shared, nothing new retained
        else:
            labels = RevisionLabels(reachable, by_key, chain_units, len(chain_units),
                                    chain[0], here)
            retained = LABELS_WORK
            if previous is None or reachable is not previous.reachable:
                retained += SET_WORK + RECORD_WORK * len(reachable)
            if previous is None or by_key is not previous.by_key:
                retained += SET_WORK + BY_KEY_WORK * sum(len(r) for r in by_key.values())
            if previous is None or chain[0] is not previous.object_streams:
                retained += SET_WORK + RECORD_WORK * len(chain[0])
            if not budget.charge_work(retained):
                break
        results[revision] = labels
        last = _Last(read, keyed, sources, ident, labels)
    return tuple(results)


@dataclass(frozen=True)
class _Last:
    """The last revision labelled: the numbers its walk read (``keyed``:
    those read for a by-key record -- a target, a holder's /Type, /Subtype
    or /S through a reference, an /EF or /OutputIntents body), what each
    body holding one of item 5's keys made of them (``sources``: its
    candidates and contexts), its trailer references and its labels."""

    read: set[int]
    keyed: set[int]
    sources: dict[int, _Made]
    ident: tuple[object, ...]
    labels: RevisionLabels


class _Walk:
    def __init__(self, revision: int, body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None],
                 edges: dict[UnitRef, tuple[Edge, ...]], budget: Budget) -> None:
        self.revision, self.body_of, self.home_of, self.edges, self.budget = (
            revision, body_of, home_of, edges, budget)
        self.read: set[int] = set()
        self.keyed: set[int] = set()
        self.sources: dict[int, _Made] = {}
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

    def same(self, numbers: set[int], last: _Last, homes: set[int]) -> bool:
        """Does the last walk's reading still hold, though *numbers* (read
        by it) changed? Each must be a reachable object -- not a home, not
        read for a by-key record -- whose new body has the generation and
        the references (number, generation, key path) its old one had, and
        -- a body holding one of item 5's keys -- makes the same candidates
        of them (a holder's /Type or /S may change alone): then no number
        is reached or left, and no record changes. A
        catalog rewritten with the same references, an update's usual
        shape, is not walked again."""
        if not self.charge(len(numbers) + 1):
            return False
        for number in sorted(numbers):
            if (number not in last.labels.reachable or number in last.keyed
                    or number in homes):
                return False
            old = self.body_of(number, self.revision + 1)
            new = self.body_of(number, self.revision)
            if not self.charge(2 * BODY_WORK) or old is None or new is None or old[2] != new[2]:
                return False
            before, after = self.edges.get(old[0]), self.edges.get(new[0])
            if before is None or after is None or not self.charge(len(before) + len(after) + 1):
                return False
            if ({(e.num, e.gen, e.path) for e in before}
                    != {(e.num, e.gen, e.path) for e in after}):
                return False
            if number in last.sources and self.made(number, new, after) != last.sources[number]:
                return False
        return True

    def made(self, number: int, body: Body, found: tuple[Edge, ...]) -> _Made | None:
        """The candidates and contexts *body*'s edges with item 5's keys
        make (None if it has none, or the budget is spent)."""
        holders: _Holders | None = None
        candidates: list[_Candidate] = []
        contexts: list[_Context] = []
        for edge in found:
            path = edge.path
            if path and (path[-1] in _KEYS or (len(path) > 1 and path[-2] == b"EF")):
                if holders is None:
                    holders = self.holders(body)
                    if holders is None:
                        return None
                self.match(number, edge, holders, candidates, contexts)
        return (tuple(candidates), tuple(contexts)) if holders is not None else None

    def object_streams(self, members: dict[int, set[int]]) -> frozenset[UnitRef] | None:
        """The object streams home to a live compressed entry here."""
        if not self.charge(len(members) + 1):
            return None
        found: list[UnitRef] = []
        for home in sorted(members):
            body = self.body(home, 0)
            if self.spent:
                return None
            if body is not None and body[4]:
                found.append(body[0])
        return frozenset(found)

    def run(self, trailer: tuple[Edge, ...]) -> tuple[set[int], ByKey] | None:
        """The reachable numbers and the by-key records; None when spent."""
        reachable: set[int] = set()
        roots = [e.num for e in trailer if e.path == (b"Root",)]
        catalog = roots[0] if roots else None
        candidates: list[_Candidate] = []
        contexts: list[_Context] = []
        named: set[int] = set()  # bodies a reference names under a drawn-by-name key
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
            # Each edge is looked at twice: for item 5's keys, then followed.
            if found is None or not self.charge(2 * len(found) + 1):
                self.spent = True  # the graph stopped short: the budget is spent
                return None
            made = self.made(number, body, found)
            if self.spent:
                return None
            if made is not None:
                self.sources[number] = made
                candidates += made[0]
                contexts += made[1]
            for edge in found:
                path = edge.path
                if path and path[-1] in _DRAWN_BY_NAME:
                    named.add(edge.num)
                if number == catalog and path and path[0] == b"OutputIntents":
                    _intents(number, edge, path[1:], candidates, contexts)
                if edge.num not in reachable:
                    stack.append((edge.num, edge.gen))
        if not self.contexts(contexts, candidates, named):
            return None
        return reachable, self.records(candidates, named)

    def holders(self, body: Body) -> _Holders | None:
        """Each reference's holding dictionary in *body*'s value (by the
        reference's start), and each dictionary's holding one; None when
        spent."""
        refs: dict[int, PdfDict] = {}
        parents: dict[int, PdfDict] = {}
        stack: list[PdfValue] = [body[1]] if body[1] is not None else []
        while stack:
            item = stack.pop()
            size = (len(item.entries) if isinstance(item, PdfDict)
                    else len(item.items) if isinstance(item, PdfArray) else 0)
            if not self.charge(2 * size + 1):  # each entry looked at, a step or two
                return None
            if isinstance(item, PdfDict):
                for _, value in item.entries:
                    if isinstance(value, PdfRef):
                        refs[value.start] = item
                    elif isinstance(value, PdfDict):
                        parents[id(value)] = item
                        stack.append(value)
                    elif isinstance(value, PdfArray):
                        stack.append(value)
            elif isinstance(item, PdfArray):
                stack.extend(v for v in item.items if isinstance(v, (PdfDict, PdfArray)))
        return refs, parents

    def match(self, number: int, edge: Edge, holders: _Holders,
              candidates: list[_Candidate], contexts: list[_Context]) -> None:
        """Record *edge* (held by reachable object *number*, its last key
        one of item 5's) when its holder is the one item 5 names."""
        path = edge.path
        holder = holders[0].get(edge.start)
        if holder is None:
            return
        last = path[-1]
        if len(path) > 1 and path[-2] == b"EF":
            # /EF << /F ... >>: the file specification holds the /EF dictionary.
            spec = holders[1].get(id(holder))
            top = len(path) == 2
            if (spec is not None and (top or path[-3] not in _DRAWN_BY_NAME)
                    and self.name(spec.get(b"Type")) == b"Filespec"):
                candidates.append((StructuralKind.EMBEDDED_FILE, number, b"EF",
                                   edge.num, edge.gen, top))
            return
        top = len(path) == 1
        if not top and path[-2] in _DRAWN_BY_NAME:
            return
        if last == b"Metadata":
            kind = self.name(holder.get(b"Type"))
            subtype = self.name(holder.get(b"Subtype"))
            if kind in METADATA_TYPES or subtype in _IMAGE_OR_FORM:
                candidates.append((StructuralKind.XMP, number, last, edge.num, edge.gen, top))
        elif last == b"JS":
            if self.name(holder.get(b"S")) == b"JavaScript":
                candidates.append((StructuralKind.JAVASCRIPT, number, last, edge.num,
                                   edge.gen, top))
        elif last == b"EF":
            if self.name(holder.get(b"Type")) == b"Filespec":
                contexts.append((edge.num, edge.gen, _EF, top, number))

    def name(self, value: PdfValue | None) -> bytes | None:
        """*value* as a name, through one reference (recorded as keyed)."""
        if isinstance(value, PdfRef):
            self.keyed.add(value.num)
            held = self.body(value.num, value.gen)
            value = held[1] if held is not None else None
        return value.raw if isinstance(value, PdfName) else None

    def contexts(self, contexts: list[_Context], candidates: list[_Candidate],
                 named: set[int]) -> bool:
        """The edges of bodies reached as /EF, the catalog's
        /OutputIntents or one of its items (each reachable already) --
        none a dictionary drawn by name, nor reached from one's top level."""
        seen: set[tuple[int, int]] = set()
        while contexts:
            if not self.charge():
                return False
            number, gen, context, top, source = contexts.pop()
            if (number, context) in seen or number in named or (top and source in named):
                continue
            seen.add((number, context))
            self.keyed.add(number)
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
                                       edge.num, edge.gen, True))
                elif context == _INTENTS:
                    _intents(number, edge, edge.path, candidates, contexts)
                elif context == _INTENT and edge.path == (b"DestOutputProfile",):
                    candidates.append((StructuralKind.OUTPUT_INTENT, number,
                                       b"DestOutputProfile", edge.num, edge.gen, True))
        return True

    def records(self, candidates: list[_Candidate], named: set[int]) -> ByKey:
        """Each candidate whose holder is no dictionary drawn by name and
        whose target is a stream with no image or form keys. The caller
        checks ``spent``."""
        records: dict[UnitRef, dict[Structural, None]] = {}
        for kind, source, key, number, gen, top in candidates:
            if top and source in named:
                continue
            self.keyed.add(number)
            body = self.body(number, gen)
            if self.spent:
                break
            if body is None or not body[4] or not self.plain(body):
                continue
            records.setdefault(body[0], {})[Structural(kind, source, key)] = None
        return {unit: tuple(found) for unit, found in records.items()}

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
            self.keyed.add(subtype.num)
            held = self.body(subtype.num, subtype.gen)
            subtype = held[1] if held is not None else None
            if isinstance(subtype, PdfRef):
                return False
        return not (isinstance(subtype, PdfName) and subtype.raw in _IMAGE_OR_FORM)


# Each reference's holding dictionary (by its start) and each dictionary's
# holding one (by id), within one body's value.
_Holders = tuple[dict[int, PdfDict], dict[int, PdfDict]]
# What one body's edges with item 5's keys make: candidates, contexts.
_Made = tuple[tuple[_Candidate, ...], tuple[_Context, ...]]


def _intents(number: int, edge: Edge, rest: tuple[bytes, ...], candidates: list[_Candidate],
             contexts: list[_Context]) -> None:
    """An edge at *rest* inside the catalog's /OutputIntents array."""
    if rest == (ARRAY, b"DestOutputProfile"):
        candidates.append((StructuralKind.OUTPUT_INTENT, number, b"DestOutputProfile",
                           edge.num, edge.gen, False))
    elif rest == (ARRAY,):
        contexts.append((edge.num, edge.gen, _INTENT, False, number))
    elif rest == ():
        contexts.append((edge.num, edge.gen, _INTENTS, False, number))
