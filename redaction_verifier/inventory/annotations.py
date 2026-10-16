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
references in a row (a longer chain makes MuPDF warn: the gate sees it)
-- but a stream counts only one reference away, as pdf_is_stream reads
it: an /N or state entry naming an object whose value is a reference to
a stream is no stream (an /N so given is read as the stream's dictionary,
an entry so given draws nothing; 3a-7e-2's review, fixing 3a-7d).
A direct null is an absent key. Inherited /FT and /V come from the
nearest of the widget and its /Parent chain that has the key; they are
memoized per field node, so a deep chain is walked once. A /Parent cycle
among fields flags FIELD_TREE (owner decision P5): MuPDF fails on it
(cycle in tree). An annotation MuPDF would synthesize but cannot -- its
/Subtype not one MuPDF has an appearance for, or a widget whose inherited
/FT is none it builds one for (/Tx, /Ch, /Sig, also through references;
/Btn only written as a name) -- flags APPEARANCE_UNSUPPORTED (issue #106;
coordinator decision, 2026-10-15): MuPDF warns, and draws the stored
appearance instead, or nothing. One flag per cause, in the first revision
it appears: the annotation (one written directly: its page), or for a
field type the field node that holds /FT (none: the chain's top) -- not
one per widget inheriting it.

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

from collections.abc import Callable, Sequence
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


# The subtypes MuPDF 1.28 builds an appearance for (pdf_write_appearance);
# any other, an absent /Subtype or one that is not a name makes it warn
# "cannot create appearance stream for ... annotations" (issue #106).
SYNTHESIZABLE: Final = frozenset({
    b"Text", b"FreeText", b"Line", b"Square", b"Circle", b"Polygon", b"PolyLine",
    b"Highlight", b"Underline", b"Squiggly", b"StrikeOut", b"Redact", b"Stamp", b"Caret",
    b"Ink", b"FileAttachment", b"Sound", b"Widget"})
# The field types it builds a widget's appearance for. MuPDF reads /Tx, /Ch
# and /Sig through references (up to MAX_HOPS), /Btn only written as a name:
# a reference to /Btn makes it warn "cannot create appearance stream for Btn
# widgets" (tests/test_inventory_annotations.py, every case against MuPDF).
FIELD_TYPES: Final = frozenset({b"Ch", b"Sig", b"Tx"})
FIELD_TYPES_DIRECT: Final = frozenset({b"Btn"})
UNLISTED: Final = frozenset({b"Link", b"Popup"})
# References MuPDF follows in a row (pdf_resolve_indirect_chain); one more
# and it warns "too many indirections" and reads null.
MAX_HOPS: Final = 9
# Work charged per reference followed: a body lookup through the xref
# history (build.py's callbacks), several steps, not one.
HOP_WORK: Final = 3
# Work charged per /Annots item, listed or not: its resolution and checks.
ITEM_WORK: Final = 2


@dataclass(frozen=True)
class Annotation:
    """An annotation MuPDF lists: its page (an index into the revision's
    page list), its position in that page's /Annots, its object number
    (None when written directly in /Annots), the body holding it (its own,
    or its page's), whether it is a widget (/Subtype /Widget, resolved),
    whether MuPDF synthesizes its appearance, and -- when it does not --
    the object number of the stored appearance stream MuPDF draws
    (``appearance``, 3a-7e-2: a stream /AP /N, or the stream /AS names in
    an /N state dictionary, each through up to MAX_HOPS references, as
    pdf_annot_ap reads it; None when synthesized or when that is not a
    stream: nothing drawn)."""

    page: int
    index: int
    number: int | None
    body: UnitRef
    widget: bool
    synthesized: bool
    appearance: int | None = None


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
    the number of the node that decides its field type: the one holding
    /FT, or with none in the chain its topmost numbered node (None: no
    numbered node) -- one cause, however many widgets inherit it."""

    ft: PdfValue | None
    v: PdfValue | None
    cause: int | None


_CYCLE: Final = _Field(None, None, None)  # a node on a /Parent cycle (memo marker, by identity)
_EMPTY: Final = _Field(None, None, None)  # nothing above: no /Parent


def build_annotations(
        revisions: int, changes: list[list[int]],
        body_of: Callable[[int, int], Body | None],
        home_of: Callable[[int, int], int | None],
        trailers: list[PdfDict | None],
        pages: tuple[tuple[Page, ...] | None, ...],
        budget: Budget, tokens: Sequence[object] | None = None,
        ) -> tuple[tuple[Annotations | None, ...],
                   tuple[tuple[Synthesized, ...], ...], list[Flag]]:
    """Each revision's annotations (None where its page list is, or the
    budget ran out), its SYNTHESIZED units, and the FIELD_TREE and
    APPEARANCE_UNSUPPORTED flags. *tokens*: each revision's reuse token
    (decrypted.reuse_tokens), part of the ident."""
    tokens = tokens if tokens is not None else (None,) * revisions
    results: list[Annotations | None] = [None] * revisions
    units: list[tuple[Synthesized, ...]] = [() for _ in range(revisions)]
    flags: list[Flag] = []
    seen: set[tuple[FlagReason, int, int]] = set()
    previous: tuple[set[int], tuple[object, ...], tuple[Page, ...],
                    Annotations | None, tuple[Synthesized, ...]] | None = None
    for revision in reversed(range(revisions)):
        listed = pages[revision] if revision < len(pages) else None
        if listed is None:
            # No walk here, so nothing records what this revision changed:
            # the next revision with a page list walks afresh (review of #140).
            previous = None
            continue
        trailer = trailers[revision]
        root = trailer.get(b"Root") if trailer is not None else None
        held = acroform_key(root, revision, body_of, budget)
        if held is SPENT:
            break
        ident = ((root.num, root.gen) if isinstance(root, PdfRef) else None,
                 tokens[revision], held)
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


SPENT: Final = object()  # acroform_key's answer when the budget refuses


def acroform_key(root: PdfValue | None, revision: int,
                 body_of: Callable[[int, int], Body | None], budget: Budget) -> object:
    """What the walk reads of the catalog: its /AcroForm value's written
    bytes (None without a catalog dictionary or /AcroForm); SPENT when
    the budget refuses. The catalog itself is not in the walk's read set:
    a revision rewriting it but not /AcroForm reuses the walk."""
    if not isinstance(root, PdfRef) or root.num == 0:
        return None
    body = body_of(root.num, revision)
    if body is None or body[2] != root.gen or not isinstance(body[1], PdfDict):
        return None
    catalog = body[1]
    if not budget.charge_work(len(catalog.entries) + 1):
        return SPENT
    value = catalog.get(b"AcroForm")
    if value is None:
        return None
    if not budget.charge_work(value.end - value.start + 1):
        return SPENT
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
        # An annotation's classification and drawn appearance, by number.
        self.classified: dict[int, tuple[bool, int | None]] = {}
        self.number = 0  # the number of the body resolve() last reached
        self.hops = 0    # and how many references it followed to get there

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
            if hops > MAX_HOPS or not self.charge(HOP_WORK):
                return None, None
            self.number = value.num
            body = self.body(value)
            if body is None:
                return None, None
            value = body[1]
        self.hops = hops
        return (None, None) if value is None or isinstance(value, PdfNull) else (value, body)

    def stream(self, value: PdfValue | None) -> int | None:
        """The number of the stream *value* names, as MuPDF's pdf_is_stream
        reads it: a reference whose object is itself a stream. A chain
        (`20 0 obj 6 0 R endobj`) is no stream there, though a dictionary or
        a name is read through one (tests/test_inventory_drawable.py, against
        MuPDF's drawing). None otherwise."""
        _, held = self.resolve(value)
        return self.number if held is not None and held[4] and self.hops == 1 else None

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
                if not self.charge(ITEM_WORK):
                    return None
                value, held = self.resolve(item)
                if not isinstance(value, PdfDict):
                    continue  # MuPDF lists dictionaries only
                subtype, _ = self.resolve(self.get(value, b"Subtype"))
                if isinstance(subtype, PdfName) and subtype.raw in UNLISTED:
                    continue
                number = item.num if isinstance(item, PdfRef) and held is not None else None
                if number is not None and number in self.classified:
                    synthesized, drawn = self.classified[number]
                else:
                    synthesized, drawn = self.classify(
                        value, number, subtype, need,
                        number if number is not None else page.number)
                    if number is not None:
                        self.classified[number] = synthesized, drawn
                unit = held[0] if held is not None else page.unit
                widget = isinstance(subtype, PdfName) and subtype.raw == b"Widget"
                found.append(Annotation(page_index, index, number, unit, widget, synthesized,
                                        drawn))
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
                 need: bool, holder: int) -> tuple[bool, int | None]:
        """ADR 0011 item 5: does MuPDF synthesize this annotation's
        appearance -- and if not, the stored stream it draws (None: none)?
        *holder*: the object a flag names -- the annotation's number, or its
        page's for one written directly."""
        widget = isinstance(subtype, PdfName) and subtype.raw == b"Widget"
        ft: PdfValue | None = None
        cause = holder
        if widget:
            field = self.field(value, number)
            ft = field.ft
            cause = field.cause if field.cause is not None else holder
            if isinstance(ft, PdfName) and ft.raw == b"Sig":
                # Whatever /AP or /NeedAppearances say: signed, the stored
                # appearance is drawn.
                if self.signed(field.v):
                    return False, self.stored(value)[1]
                return True, None
        stored, drawn = self.stored(value)
        synthesized = not stored or (widget and need)
        if synthesized:
            if not (isinstance(subtype, PdfName) and subtype.raw in SYNTHESIZABLE):
                self.flag_unsupported(AppearanceRule.SUBTYPE, holder)
            elif widget and not self.buildable(ft):
                self.flag_unsupported(AppearanceRule.FIELD_TYPE, cause)
            return True, None
        return False, drawn

    def buildable(self, ft: PdfValue | None) -> bool:
        """A field type MuPDF builds a widget's appearance for: /Tx, /Ch or
        /Sig, as written or through references; /Btn only as written."""
        if isinstance(ft, PdfName):
            return ft.raw in FIELD_TYPES or ft.raw in FIELD_TYPES_DIRECT
        kind, _ = self.resolve(ft)
        return isinstance(kind, PdfName) and kind.raw in FIELD_TYPES

    def flag_unsupported(self, rule: AppearanceRule, number: int) -> None:
        self.flags.append((FlagReason.APPEARANCE_UNSUPPORTED, int(rule), number))

    def stored(self, value: PdfDict) -> tuple[bool, int | None]:
        """Whether MuPDF draws an /AP /N as stored -- a stream one
        reference away (pdf_is_stream follows no chain), or else a
        dictionary (read through references: a stream's at the end of a
        chain is one) in which /AS names an entry (one that is not a stream
        one reference away draws nothing) -- and the stream's number (None:
        no stream drawn)."""
        ap, _ = self.resolve(self.get(value, b"AP"))
        if not isinstance(ap, PdfDict):
            return False, None
        normal_value = self.get(ap, b"N")
        drawn = self.stream(normal_value)
        if drawn is not None:
            return True, drawn
        normal, _ = self.resolve(normal_value)
        if not isinstance(normal, PdfDict):
            return False, None
        state, _ = self.resolve(self.get(value, b"AS"))
        if not isinstance(state, PdfName):
            return False, None
        entry = self.get(normal, state.raw)
        if entry is None:
            return False, None
        return True, self.stream(entry)

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
            if ft is not None:
                cause = n if n is not None else base.cause
            else:
                cause = base.cause if base.cause is not None else n
            base = _Field(ft if ft is not None else base.ft, v if v is not None else base.v,
                          cause)
            if n is not None:
                self.fields[n] = base
        return base
