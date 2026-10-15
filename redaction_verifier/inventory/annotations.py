"""The reference graph, part d (Phase 3a-7d, ADR 0011 item 5 with its
2026-10-11 amendment): the annotations MuPDF synthesizes an appearance
for.

Per revision, each page of the canonical page list (pages.py) lists its
annotations as MuPDF does (pdf_first_annot and pdf_first_widget): every
/Annots item that resolves to a dictionary, but a Link or a Popup (MuPDF
lists and draws neither), an item listed twice listed twice. Each is
classified, per ADR 0011 item 5 as MuPDF 1.28 reads it
(tests/test_inventory_annotations.py, against MuPDF's own drawing):

- a widget whose inherited /FT is the name /Sig (as written: MuPDF's
  test does not resolve a reference there) is synthesized exactly when
  it is unsigned -- its inherited /V does not resolve to a dictionary
  whose /Type is absent or /Sig -- whatever /AP or /NeedAppearances say;
- any other annotation is not synthesized only when its /AP /N is a
  stream, or a dictionary in which /AS (a name) names an entry -- an
  entry that is not a stream draws nothing, and is not synthesized
  either -- and, for a widget, the catalog's /AcroForm /NeedAppearances
  is not true; every other annotation is synthesized.

References are resolved as MuPDF resolves them, through up to nine
references in a row (a longer chain makes MuPDF warn: the gate sees it).
A direct null is an absent key. Inherited /FT and /V come from the
nearest of the widget and its /Parent chain that has the key; they are
memoized per field node, so a deep chain is walked once. A /Parent cycle
among fields flags FIELD_TREE (owner decision P5): MuPDF fails on it
(cycle in tree); so do more than 100 field nodes above a widget, past
which qpdf does not reach it from /AcroForm. An annotation MuPDF would synthesize but cannot -- its
/Subtype not one MuPDF has an appearance for, or a widget whose inherited
/FT is not a name it builds one for, written directly -- flags
APPEARANCE_UNSUPPORTED (issue #106): MuPDF warns, and draws the stored
appearance instead, or nothing.

Each synthesized annotation is a SYNTHESIZED unit per (annotation body,
revision): it claims no bytes (not a tiling claim), starts UNEXAMINED
(4a decodes it) and is charged to the budget's unit cap (max_units).

Revisions are read oldest first; a revision that changes nothing the
previous walk read -- the catalog's /AcroForm as written, the page list,
every object the walk resolved -- reuses its result (form filling
changes /V, /AS and /NeedAppearances while a widget's body stays: those
are read, so a change to any re-reads the revision). Iterative, never
raises, charged to the budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from ..budget import Budget
from ..ledger import Flag, FlagReason, Status, UnitRef
from .graph import Body
from .objects import PdfArray, PdfBool, PdfDict, PdfName, PdfNull, PdfRef, PdfValue
from .pages import Page


class AppearanceRule(IntEnum):
    """Why an annotation was flagged APPEARANCE_UNSUPPORTED (its ``rule``)."""

    SUBTYPE = 1     # synthesized, but its /Subtype is none MuPDF builds an appearance for
    FIELD_TYPE = 2  # a synthesized widget whose inherited /FT is not one MuPDF builds


class FieldTreeRule(IntEnum):
    """Why a widget's field chain was flagged FIELD_TREE (its ``rule``)."""

    CYCLE = 1  # a /Parent cycle (owner decision P5): MuPDF fails ("cycle in tree")
    DEPTH = 2  # more than MAX_FIELD_LEVELS field nodes above a widget: qpdf stops there


# Field nodes above a widget qpdf reaches from /AcroForm /Fields (deeper, qpdf
# --check warns the widget "is not reachable from /AcroForm"), measured on
# qpdf 12.4: 100 reach it, 101 do not -- like the page tree's 100 levels.
MAX_FIELD_LEVELS: Final = 100


# The subtypes MuPDF 1.28 builds an appearance for (pdf_write_appearance);
# any other, an absent /Subtype or one that is not a name makes it warn
# "cannot create appearance stream for ... annotations" (issue #106).
SYNTHESIZABLE: Final = frozenset({
    b"Text", b"FreeText", b"Line", b"Square", b"Circle", b"Polygon", b"PolyLine",
    b"Highlight", b"Underline", b"Squiggly", b"StrikeOut", b"Redact", b"Stamp", b"Caret",
    b"Ink", b"FileAttachment", b"Sound", b"Widget"})
# The field types it builds a widget's appearance for, written as a name:
# MuPDF reads some through a reference (/Tx, /Ch) and not others (/Btn).
FIELD_TYPES: Final = frozenset({b"Btn", b"Ch", b"Sig", b"Tx"})
UNLISTED: Final = frozenset({b"Link", b"Popup"})
# References MuPDF follows in a row (pdf_resolve_indirect_chain); one more
# and it warns "too many indirections" and reads null.
MAX_HOPS: Final = 9


@dataclass(frozen=True)
class Annotation:
    """An annotation MuPDF lists: its page (an index into the revision's
    page list), its position in that page's /Annots, its object number
    (None when written directly in /Annots), the body holding it (its own,
    or its page's), whether it is a widget (/Subtype /Widget, resolved) and
    whether MuPDF synthesizes its appearance."""

    page: int
    index: int
    number: int | None
    body: UnitRef
    widget: bool
    synthesized: bool


@dataclass(frozen=True)
class Synthesized:
    """A SYNTHESIZED unit (ADR 0011 item 5, owner decision R2): one per
    (annotation body, revision) -- the revision is the index of the
    per-revision tuple that holds it, so an unchanged revision shares its
    predecessor's units. ``body`` is the annotation's own body, or for an
    annotation written directly in /Annots its page's, with ``index`` its
    /Annots position (None otherwise). It claims no bytes and starts
    UNEXAMINED: 4a decodes it from MuPDF's per-annotation trace."""

    body: UnitRef
    index: int | None
    status: Status = Status.UNEXAMINED


Annotations = tuple[Annotation, ...]


@dataclass(frozen=True)
class _Field:
    """A field node's inherited /FT and /V, as written (None: absent), and
    the field nodes from it to the top of its chain, itself included."""

    ft: PdfValue | None
    v: PdfValue | None
    levels: int


_CYCLE: Final = _Field(None, None, -1)  # a node on a /Parent cycle (memo marker)
_EMPTY: Final = _Field(None, None, 0)   # nothing above: no /Parent


def build_annotations(
        revisions: int, changes: list[list[int]],
        body_of: Callable[[int, int], Body | None],
        home_of: Callable[[int, int], int | None],
        trailers: list[PdfDict | None],
        pages: tuple[tuple[Page, ...] | None, ...],
        budget: Budget) -> tuple[tuple[Annotations | None, ...],
                                 tuple[tuple[Synthesized, ...], ...], list[Flag]]:
    """Each revision's annotations (None where its page list is, or the
    budget ran out), its SYNTHESIZED units, and the FIELD_TREE and
    APPEARANCE_UNSUPPORTED flags."""
    results: list[Annotations | None] = [None] * revisions
    units: list[tuple[Synthesized, ...]] = [() for _ in range(revisions)]
    flags: list[Flag] = []
    seen: set[tuple[FlagReason, int, int]] = set()
    previous: tuple[set[int], tuple[object, ...], tuple[Page, ...],
                    Annotations | None, tuple[Synthesized, ...]] | None = None
    for revision in reversed(range(revisions)):
        listed = pages[revision] if revision < len(pages) else None
        if listed is None:
            continue
        trailer = trailers[revision]
        root = trailer.get(b"Root") if trailer is not None else None
        held = _catalog_key(root, revision, body_of, budget)
        if held is _SPENT:
            break
        ident = ((root.num, root.gen) if isinstance(root, PdfRef) else None, held)
        if not budget.charge_work(len(listed) + 1):
            break
        if (previous is not None and previous[1] == ident and _same_pages(previous[2], listed)
                and not previous[0].intersection(changes[revision])):
            found, synthesized = previous[3], previous[4]
        else:
            walk = _Walk(revision, body_of, home_of, budget)
            found = walk.run(root, listed)
            if walk.spent:
                break
            for reason, rule, number in walk.flags:
                if (reason, rule, number) not in seen:
                    seen.add((reason, rule, number))
                    flags.append(Flag(reason, None, (
                        ("rule", rule), ("object", number), ("revision", revision))))
            synthesized = _units(found) if found is not None else ()
            previous = (walk.read, ident, listed, found, synthesized)
        # One unit per (annotation body, revision): charged in every
        # revision, shared or not.
        if not budget.charge_units(len(synthesized)):
            break
        results[revision], units[revision] = found, synthesized
    return tuple(results), tuple(units), flags


def _same_pages(a: tuple[Page, ...], b: tuple[Page, ...]) -> bool:
    """The same page records (pages.py keeps an unchanged page's record:
    identity is enough; a page read again with equal values only costs a
    re-walk)."""
    return a is b or (len(a) == len(b) and all(x is y for x, y in zip(a, b)))


def _units(found: Annotations) -> tuple[Synthesized, ...]:
    """The SYNTHESIZED units of a revision: one per synthesized body (an
    annotation listed twice is one unit), one per position for those
    written directly."""
    out: list[Synthesized] = []
    kept: set[tuple[UnitRef, int | None]] = set()
    for annotation in found:
        if not annotation.synthesized:
            continue
        key = (annotation.body, None if annotation.number is not None else annotation.index)
        if key not in kept:
            kept.add(key)
            out.append(Synthesized(*key))
    return tuple(out)


_SPENT: Final = object()


def _catalog_key(root: PdfValue | None, revision: int,
                 body_of: Callable[[int, int], Body | None], budget: Budget) -> object:
    """What the walk reads of the catalog: its /AcroForm value's written
    bytes (None without a catalog dictionary or /AcroForm); _SPENT when
    the budget refuses. The catalog itself is not in the walk's read set:
    a revision rewriting it but not /AcroForm reuses the walk."""
    if not isinstance(root, PdfRef) or root.num == 0:
        return None
    body = body_of(root.num, revision)
    if body is None or body[2] != root.gen or not isinstance(body[1], PdfDict):
        return None
    catalog = body[1]
    if not budget.charge_work(len(catalog.entries) + 1):
        return _SPENT
    value = catalog.get(b"AcroForm")
    if value is None:
        return None
    if not budget.charge_work(value.end - value.start + 1):
        return _SPENT
    return body[3][value.start:value.end]


class _Walk:
    def __init__(self, revision: int, body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None], budget: Budget) -> None:
        self.revision, self.body_of, self.home_of, self.budget = (
            revision, body_of, home_of, budget)
        self.read: set[int] = set()
        self.flags: list[tuple[FlagReason, int, int]] = []
        self.spent = False
        self.fields: dict[int, _Field] = {}       # a field node's inherited /FT and /V
        self.classified: dict[int, bool] = {}     # an annotation's classification, by number
        self.number = 0  # the number of the body resolve() last reached

    def charge(self, n: int = 1) -> bool:
        if not self.budget.charge_work(n):
            self.spent = True
        return not self.spent

    def body(self, ref: PdfRef) -> Body | None:
        """The live body *ref* names (its generation included), recording
        what was read."""
        self.read.add(ref.num)
        home = self.home_of(ref.num, self.revision)
        if home is not None:
            self.read.add(home)
        body = self.body_of(ref.num, self.revision)
        return body if body is not None and body[2] == ref.gen and ref.num != 0 else None

    def resolve(self, value: PdfValue | None) -> tuple[PdfValue | None, Body | None]:
        """*value* with references followed as MuPDF follows them (at most
        MAX_HOPS in a row), and the body the last one named (its number in
        ``self.number``): (None, None) for an absent key, a direct null or
        a reference to nothing."""
        body: Body | None = None
        hops = 0
        while isinstance(value, PdfRef):
            hops += 1
            if hops > MAX_HOPS or not self.charge():
                return None, None
            self.number = value.num
            body = self.body(value)
            if body is None:
                return None, None
            value = body[1]
        return (None, None) if value is None or isinstance(value, PdfNull) else (value, body)

    def get(self, holder: PdfDict, key: bytes) -> PdfValue | None:
        """*holder*'s *key* as written, a direct null absent; charged by
        the holder's size (PdfDict.get scans its entries)."""
        if not self.charge(len(holder.entries) + 1):
            return None
        value = holder.get(key)
        return None if isinstance(value, PdfNull) else value

    def run(self, root: PdfValue | None, pages: tuple[Page, ...]) -> Annotations | None:
        need = self.need_appearances(root)
        found: list[Annotation] = []
        for page_index, page in enumerate(pages):
            if self.spent:
                return None
            body = self.body(PdfRef(0, 0, page.number, page.gen))
            if body is None or not isinstance(body[1], PdfDict):
                continue
            annots, _ = self.resolve(self.get(body[1], b"Annots"))
            if not isinstance(annots, PdfArray):
                continue
            for index, item in enumerate(annots.items):
                if not self.charge():
                    return None
                value, held = self.resolve(item)
                if not isinstance(value, PdfDict):
                    continue  # MuPDF lists dictionaries only
                subtype, _ = self.resolve(self.get(value, b"Subtype"))
                if isinstance(subtype, PdfName) and subtype.raw in UNLISTED:
                    continue
                number = item.num if isinstance(item, PdfRef) and held is not None else None
                if number is not None and number in self.classified:
                    synthesized = self.classified[number]
                else:
                    synthesized = self.classify(value, number, subtype, need)
                    if number is not None:
                        self.classified[number] = synthesized
                unit = held[0] if held is not None else page.unit
                widget = isinstance(subtype, PdfName) and subtype.raw == b"Widget"
                found.append(Annotation(page_index, index, number, unit, widget, synthesized))
        return None if self.spent else tuple(found)

    def need_appearances(self, root: PdfValue | None) -> bool:
        """The catalog's /AcroForm /NeedAppearances is true. The catalog is
        read without entering the read set (build_annotations compares its
        /AcroForm as written)."""
        if not isinstance(root, PdfRef) or root.num == 0:
            return False
        catalog = self.body_of(root.num, self.revision)
        if catalog is None or catalog[2] != root.gen or not isinstance(catalog[1], PdfDict):
            return False
        form, _ = self.resolve(self.get(catalog[1], b"AcroForm"))
        if not isinstance(form, PdfDict):
            return False
        need, _ = self.resolve(self.get(form, b"NeedAppearances"))
        return isinstance(need, PdfBool) and need.value

    def classify(self, value: PdfDict, number: int | None, subtype: PdfValue | None,
                 need: bool) -> bool:
        """ADR 0011 item 5: does MuPDF synthesize this annotation's appearance?"""
        widget = isinstance(subtype, PdfName) and subtype.raw == b"Widget"
        ft: PdfValue | None = None
        if widget:
            field = self.field(value, number)
            ft = field.ft
            if isinstance(ft, PdfName) and ft.raw == b"Sig":
                return not self.signed(field.v)  # whatever /AP or /NeedAppearances say
        synthesized = not self.stored(value) or (widget and need)
        if synthesized:
            if not (isinstance(subtype, PdfName) and subtype.raw in SYNTHESIZABLE):
                self.flag_unsupported(AppearanceRule.SUBTYPE, number)
            elif widget and not (isinstance(ft, PdfName) and ft.raw in FIELD_TYPES):
                self.flag_unsupported(AppearanceRule.FIELD_TYPE, number)
        return synthesized

    def flag_unsupported(self, rule: AppearanceRule, number: int | None) -> None:
        self.flags.append((FlagReason.APPEARANCE_UNSUPPORTED, int(rule),
                           -1 if number is None else number))

    def stored(self, value: PdfDict) -> bool:
        """An /AP /N MuPDF draws as stored: a stream, or a dictionary in
        which /AS names an entry (one that is not a stream draws nothing)."""
        ap, _ = self.resolve(self.get(value, b"AP"))
        if not isinstance(ap, PdfDict):
            return False
        normal, held = self.resolve(self.get(ap, b"N"))
        if held is not None and held[4]:
            return True  # a stream (resolve ends on a body only through a reference)
        if not isinstance(normal, PdfDict):
            return False
        state, _ = self.resolve(self.get(value, b"AS"))
        if not isinstance(state, PdfName):
            return False
        return self.get(normal, state.raw) is not None

    def signed(self, v: PdfValue | None) -> bool:
        """MuPDF's test: /V a dictionary (a stream's included) whose /Type
        is absent or /Sig."""
        found, _ = self.resolve(v)
        if not isinstance(found, PdfDict):
            return False
        kind, _ = self.resolve(self.get(found, b"Type"))
        return kind is None or (isinstance(kind, PdfName) and kind.raw == b"Sig")

    def field(self, value: PdfDict, number: int | None) -> _Field:
        """The widget's inherited /FT and /V: the nearest of it and its
        /Parent chain that has each, memoized per field node; a cycle
        flags FIELD_TREE (and reads as nothing inherited)."""
        path: list[tuple[PdfDict, int | None]] = []
        on_path: set[int] = set()
        node, at = value, number
        base = _EMPTY
        while True:
            if not self.charge():
                return _EMPTY
            if at is not None:
                if at in self.fields:
                    base = self.fields[at]
                    break
                if at in on_path:
                    self.flags.append((FlagReason.FIELD_TREE, int(FieldTreeRule.CYCLE), at))
                    base = _CYCLE
                    break
                on_path.add(at)
            path.append((node, at))
            parent = self.get(node, b"Parent")
            if isinstance(parent, PdfDict):  # direct: part of its holder's body
                node, at = parent, None
                continue
            found, held = self.resolve(parent)
            if not isinstance(found, PdfDict) or held is None:
                break  # no parent (MuPDF stops at anything but a dictionary)
            node, at = found, self.number
        if base is _CYCLE:
            for _, n in path:
                if n is not None:
                    self.fields[n] = _CYCLE
            return _CYCLE
        for held_value, n in reversed(path):
            ft = self.get(held_value, b"FT")
            v = self.get(held_value, b"V")
            base = _Field(ft if ft is not None else base.ft, v if v is not None else base.v,
                          base.levels + 1)
            if n is not None:
                self.fields[n] = base
        if base.levels - 1 > MAX_FIELD_LEVELS:  # field nodes above the widget
            self.flags.append((FlagReason.FIELD_TREE, int(FieldTreeRule.DEPTH),
                               -1 if number is None else number))
        return base
