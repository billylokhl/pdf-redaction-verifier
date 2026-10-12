"""The reference graph (Phase 3a-7, ADR 0011), part a: edges and
references (item 4).

Each live body's references are collected once, with their key path and
a use kind, and shared by every revision the body lives in. Per revision
a reference resolves only to a body live in that revision with the same
generation (a compressed target's generation is 0); otherwise it is
flagged -- REF_GENERATION when the number has a body of another
generation (MuPDF resolves it by number, libqpdf reads null),
REF_DANGLING when the number has no body at all (free, missing, an
offset-0 entry, object 0, or a body not found): owner decision R1, every
one. One flag per (source body, target number, generation), whatever the
number of revisions it persists through.

Resolution is incremental, oldest revision first: a revision re-resolves
the references of a number whose body changes in it (its entry changes,
or a compressed member's object stream is replaced), and the references
into a number whose live generation changes in it (a body appears, goes,
or changes generation) -- found through a reverse index. A revision's
trailer (its newest section's, as build reads /Root) is resolved in
every revision. The walk is iterative, never raises, and is charged to
the budget: when it is spent the graph stops, and the budget's own flag
keeps the file from being certified.

Use kinds are informational -- decided by the key path from a closed
table, OTHER for any key not listed; no label is derived from a key path
alone (ADR 0011 item 5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from ..budget import Budget
from ..ledger import Flag, FlagReason, Span, UnitKind, UnitRef
from .objects import PdfArray, PdfDict, PdfRef, PdfValue


class UseKind(Enum):
    """What a reference is used for, by the key path that holds it
    (ADR 0011 item 4). PARENT is a back edge (/Parent), not a use."""

    ROOT = "root"
    INFO = "info"
    ENCRYPT = "encrypt"
    PAGES = "pages"
    PARENT = "parent"
    CONTENTS = "contents"
    RESOURCES = "resources"
    XOBJECT = "xobject"
    FONT = "font"
    FONT_PROGRAM = "font_program"
    PATTERN = "pattern"
    SHADING = "shading"
    EXTGSTATE = "extgstate"
    SOFT_MASK = "soft_mask"
    COLOR_SPACE = "color_space"
    PROPERTIES = "properties"
    ANNOTATION = "annotation"
    APPEARANCE = "appearance"
    FIELD = "field"
    OUTLINE = "outline"
    NAME_TREE = "name_tree"
    ACTION = "action"
    EMBEDDED_FILE = "embedded_file"
    METADATA = "metadata"
    THUMBNAIL = "thumbnail"
    ALTERNATE = "alternate"
    OPTIONAL_CONTENT = "optional_content"
    STRUCTURE = "structure"
    PIECE_INFO = "piece_info"
    OTHER = "other"


# A resource dictionary's categories: a reference below one of these keys
# inside /Resources is used as that category.
_RESOURCE_CATEGORIES: Final = {
    b"XObject": UseKind.XOBJECT, b"Font": UseKind.FONT, b"Pattern": UseKind.PATTERN,
    b"Shading": UseKind.SHADING, b"ExtGState": UseKind.EXTGSTATE,
    b"ColorSpace": UseKind.COLOR_SPACE, b"Properties": UseKind.PROPERTIES,
}
# Every other listed key, matched outermost first along the path.
_KEYS: Final = {
    b"Root": UseKind.ROOT, b"Info": UseKind.INFO, b"Encrypt": UseKind.ENCRYPT,
    b"Pages": UseKind.PAGES, b"Kids": UseKind.PAGES, b"Parent": UseKind.PARENT,
    b"Contents": UseKind.CONTENTS, b"Resources": UseKind.RESOURCES,
    b"FontFile": UseKind.FONT_PROGRAM, b"FontFile2": UseKind.FONT_PROGRAM,
    b"FontFile3": UseKind.FONT_PROGRAM, b"CharProcs": UseKind.FONT_PROGRAM,
    b"SMask": UseKind.SOFT_MASK, b"Mask": UseKind.SOFT_MASK,
    b"Annots": UseKind.ANNOTATION, b"AP": UseKind.APPEARANCE,
    b"AcroForm": UseKind.FIELD, b"Fields": UseKind.FIELD, b"V": UseKind.FIELD,
    b"Outlines": UseKind.OUTLINE, b"Names": UseKind.NAME_TREE, b"Dests": UseKind.NAME_TREE,
    b"OpenAction": UseKind.ACTION, b"AA": UseKind.ACTION, b"A": UseKind.ACTION,
    b"EF": UseKind.EMBEDDED_FILE, b"AF": UseKind.EMBEDDED_FILE,
    b"Metadata": UseKind.METADATA, b"Thumb": UseKind.THUMBNAIL,
    b"Alternates": UseKind.ALTERNATE, b"OCProperties": UseKind.OPTIONAL_CONTENT,
    b"OC": UseKind.OPTIONAL_CONTENT, b"StructTreeRoot": UseKind.STRUCTURE,
    b"PieceInfo": UseKind.PIECE_INFO,
}
ARRAY: Final = b"[]"  # an array position in a key path


def use_kind(path: tuple[bytes, ...]) -> UseKind:
    """The use kind of a reference at *path* (dictionary keys, ARRAY for
    an array position): a resource category directly inside /Resources
    first, else the outermost listed key, else OTHER."""
    for i, key in enumerate(path[:-1]):
        if key == b"Resources" and path[i + 1] in _RESOURCE_CATEGORIES:
            return _RESOURCE_CATEGORIES[path[i + 1]]
    for key in path:
        kind = _KEYS.get(key)
        if kind is not None:
            return kind
    return UseKind.OTHER


@dataclass(frozen=True)
class Edge:
    """A reference: its use kind, target number and generation, and
    where it is (its key path; its span, in the coordinates of the body
    that holds it)."""

    kind: UseKind
    num: int
    gen: int
    path: tuple[bytes, ...]
    start: int
    end: int


@dataclass(frozen=True)
class Graph:
    """``edges``: each body's references, by its unit (every body live in
    some revision, collected once). ``trailers``: each revision's
    trailer references, by revision (0 = current)."""

    edges: dict[UnitRef, tuple[Edge, ...]]
    trailers: tuple[tuple[Edge, ...], ...]


# A number's body in a revision: its unit, its value, its generation.
Body = tuple[UnitRef, PdfValue | None, int]
_TRAILER: Final = -1  # a trailer reference's source in a flag


def edges_of(value: PdfValue | None, budget: Budget) -> tuple[Edge, ...] | None:
    """Every reference in *value*, in order, iteratively; None when the
    budget refuses the walk."""
    found: list[Edge] = []
    stack: list[tuple[PdfValue, tuple[bytes, ...]]] = []
    if value is not None:
        stack.append((value, ()))
    while stack:
        item, path = stack.pop()
        if not budget.charge_work(1):
            return None
        if isinstance(item, PdfRef):
            found.append(Edge(use_kind(path), item.num, item.gen, path, item.start, item.end))
        elif isinstance(item, PdfDict):
            stack.extend((v, (*path, k.raw)) for k, v in reversed(item.entries))
        elif isinstance(item, PdfArray):
            stack.extend((v, (*path, ARRAY)) for v in reversed(item.items))
    return tuple(found)


def build_graph(revisions: int, changes: list[list[int]],
                body_of: Callable[[int, int], Body | None],
                home_of: Callable[[int, int], int | None],
                trailers: list[PdfDict | None],
                budget: Budget) -> tuple[Graph, list[Flag]]:
    """The edges and the reference flags of a file with *revisions*
    revisions. *changes[r]* lists the numbers whose xref entry changes in
    revision r; *body_of(n, r)* is number n's body in revision r;
    *home_of(n, r)* its object stream's number when it is compressed
    there; *trailers[r]* revision r's trailer."""
    edges: dict[UnitRef, tuple[Edge, ...]] = {}
    trailer_edges: list[tuple[Edge, ...]] = [() for _ in range(revisions)]
    flags: list[Flag] = []
    seen: set[tuple[UnitRef | None, int, int, FlagReason]] = set()
    current: dict[int, Body | None] = {}
    sources: dict[int, set[int]] = {}   # target number -> numbers whose body references it
    members: dict[int, set[int]] = {}   # object stream number -> its live members
    homes: dict[int, int] = {}          # member number -> its object stream number

    def generation(number: int) -> int | None:
        body = current.get(number)
        return body[2] if body is not None and number != 0 else None

    def resolve(source: UnitRef | None, number: int, edge: Edge, revision: int) -> None:
        live = generation(edge.num)
        if live == edge.gen:
            return
        reason = FlagReason.REF_DANGLING if live is None else FlagReason.REF_GENERATION
        key = (source, edge.num, edge.gen, reason)
        if key in seen:
            return
        seen.add(key)
        span = (Span(edge.start, edge.end)
                if source is None or source.kind is UnitKind.OBJECT else None)
        flags.append(Flag(reason, span, (("object", number), ("target", edge.num),
                                         ("generation", edge.gen), ("revision", revision))))

    def collect(body: Body) -> tuple[Edge, ...] | None:
        unit, value, _ = body
        if unit not in edges:
            found = edges_of(value, budget)
            if found is None:
                return None
            edges[unit] = found
        return edges[unit]

    for revision in reversed(range(revisions)):
        touched = set(changes[revision])
        for number in changes[revision]:
            touched |= members.get(number, set())  # a replaced home moves its members
        moved: list[int] = []      # numbers whose body changed
        regenerated: list[int] = []  # numbers whose live generation changed
        for number in sorted(touched):
            if not budget.charge_work(1):
                return Graph(edges, tuple(trailer_edges)), flags
            home = home_of(number, revision)
            if homes.get(number) != home:
                if number in homes:
                    members.get(homes.pop(number), set()).discard(number)
                if home is not None:
                    homes[number] = home
                    members.setdefault(home, set()).add(number)
            body = body_of(number, revision)
            before = current.get(number)
            if (before[0] if before else None) != (body[0] if body else None):
                before_gen = generation(number)
                current[number] = body
                moved.append(number)
                if generation(number) != before_gen:
                    regenerated.append(number)
        # A moved body's references, all of them, join the reverse index (an
        # older body's entries stay: a lookup checks the current body's edges).
        for number in moved:
            body = current[number]
            found = collect(body) if body is not None else ()
            if found is None:
                return Graph(edges, tuple(trailer_edges)), flags
            for edge in found:
                sources.setdefault(edge.num, set()).add(number)
        for number in moved:
            body = current[number]
            if body is None:
                continue
            for edge in edges[body[0]]:
                if not budget.charge_work(1):
                    return Graph(edges, tuple(trailer_edges)), flags
                resolve(body[0], number, edge, revision)
        moved_set = set(moved)
        for target in regenerated:  # references into a number whose generation changed
            for number in sorted(sources.get(target, set()) - moved_set):
                body = current.get(number)
                if body is None:
                    continue
                for edge in edges.get(body[0], ()):
                    if edge.num != target:
                        continue
                    if not budget.charge_work(1):
                        return Graph(edges, tuple(trailer_edges)), flags
                    resolve(body[0], number, edge, revision)
        trailer = trailers[revision]
        found = edges_of(trailer, budget)
        if found is None:
            return Graph(edges, tuple(trailer_edges)), flags
        trailer_edges[revision] = found
        for edge in found:
            resolve(None, _TRAILER, edge, revision)
    return Graph(edges, tuple(trailer_edges)), flags
