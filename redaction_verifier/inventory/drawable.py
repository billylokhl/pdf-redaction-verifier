"""The reference graph, part e, continued (Phase 3a-7e-2, ADR 0011 item
5): each revision's DRAWABLE streams -- the drawing closure from the
canonical page list over item 5's closed key lists, one per role.

The walk is a key-path automaton over the graph's edges (the shape of
scopes.roles_at): a node is (object number, role); each edge's key path,
read from the body's role one key at a time through ``_NEXT`` (a key
named in the role's list) or ``_ANY`` (every entry of a dictionary drawn
by name: a resource category, /CharProcs, a DeviceN /Colorants, a type 5
halftone), gives the role of the object it references -- or drops the
edge at the first key outside the list. A direct value is read in place
(its keys are part of the path), so a direct /Resources, font or
halftone is followed like one given by reference. Every stream the walk
reaches is DRAWABLE: an upper bound, listed where content may draw it,
not decision D's "used" (4a and 4b run content).

Start points, per revision with a page list:

- each page (PAGE): its /Contents (a stream, or an array of streams),
  its own /Resources or, inherited, the /Resources of the ancestor that
  holds it (RES_HOLDER), and its own /Group;
- the stream MuPDF draws for each annotation it does not synthesize
  (``Annotation.appearance``, as pdf_annot_ap reads it), as a form;
- the catalog's /AcroForm /DR, as a resource dictionary, when any
  annotation of the revision is synthesized (it draws them).

The roles and their lists (item 5; the colour-space elements by the
coordinator's decision Q2 of 2026-10-15, an ADR 0011 note):

- a resource dictionary: every entry of /XObject, /Font, /Pattern,
  /Shading, /ExtGState, /ColorSpace (/Properties: optional content,
  which draws nothing);
- an XObject: by its /Subtype -- a form's /Subtype2, when present,
  replacing it, as MuPDF's Do operator reads them -- /Form a form, /Image
  an image, either only when a stream; any other (/PS included) follows
  nothing (MuPDF draws nothing), the stream itself still DRAWABLE;
- a form (an appearance, a soft mask's /G whatever its /Subtype):
  /Resources, /Group; a group: /CS;
- an image (also its /SMask and /Mask): /SMask, /Mask, /ColorSpace,
  /DecodeParms /JBIG2Globals (the parameters directly, or in an array);
- a font: /FontDescriptor (its /FontFile, /FontFile2, /FontFile3,
  /CIDSet), /ToUnicode, /Encoding (and a CMap's /UseCMap),
  /DescendantFonts (each a CID font), /CIDToGIDMap; a Type3 font also
  /CharProcs (every glyph) and /Resources -- Type3 as MuPDF's
  pdf_load_font decides: /Subtype /Type3, or a /Subtype other than Type0,
  Type1, MMType1 and TrueType (or none) with /CharProcs;
- a colour space: every element of its array, each a colour space; in
  a dictionary or stream so reached only /Functions, /Colorants (every
  entry a colour space) and /Process (its /ColorSpace) -- so an ICC
  profile's /Alternate and a function's own keys are not followed;
- a function: /Functions, each a function (and every element when given
  as an array: a shading's /Function, a graphics state's /TR);
- a shading: /Function, /ColorSpace; a pattern: /Resources, /Shading,
  /ExtGState;
- a graphics state: /SMask (its /G, a form; /TR), /TR, /TR2, /BG, /BG2,
  /UCR, /UCR2 (functions), /HT (a halftone: /TransferFunction, and for
  /HalftoneType 5 every other entry a halftone), /Font (an array: its
  font).

Names and integers deciding a role are read through references as MuPDF
reads a name, up to nine in a row.

A key outside these lists leads nowhere drawable: its target is ORPHANED
unless another label holds, failing closed (ADR 0010 item 6). A role
decided by a value the walk cannot read -- past nine references, a
direct font or halftone inside an array -- takes the narrower reading,
failing closed the same way. A reference is
followed only when it resolves (live, the same generation; a start the
page or annotation walk found is taken as it resolved it). Iterative,
never raises, every step charged: a node a few steps, each edge its key
path's length, each dictionary a decision reads its size, once."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from ..budget import Budget
from ..ledger import UnitRef
from .graph import ARRAY, Body, Edge
from .annotations import MAX_HOPS
from .objects import PdfDict, PdfInt, PdfName, PdfNull, PdfRef, PdfValue

# The roles (small ints: the automaton's states).
PAGE: Final = 0          # a page of the canonical list
RES_HOLDER: Final = 1    # an ancestor holding a page's inherited /Resources
CATALOG: Final = 2       # the catalog: its /AcroForm (when an annotation is synthesized)
ACROFORM: Final = 3      # /AcroForm: its /DR
CONTENTS: Final = 4      # a page's /Contents: a stream, or an array of them
LEAF: Final = 5          # a stream drawn as it is: content, a glyph, a font program
RESOURCES: Final = 6
CAT_XOBJECT: Final = 7
CAT_FONT: Final = 8
CAT_PATTERN: Final = 9
CAT_SHADING: Final = 10
CAT_EXTGSTATE: Final = 11
CAT_COLOR_SPACE: Final = 12
XOBJECT: Final = 13      # decided by its /Subtype: FORM, IMAGE or DRAWN_ONLY
FORM: Final = 14
IMAGE: Final = 15
GROUP: Final = 16
DECODE_PARMS: Final = 17
DECODE_PARMS_ITEM: Final = 18
FONT: Final = 19         # decided by its /Subtype (and /CharProcs): FONT or TYPE3
TYPE3: Final = 20
DESCENDANTS: Final = 21
FONT_DESCRIPTOR: Final = 22
CMAP: Final = 23
CHARPROCS: Final = 24
COLOR_SPACE: Final = 25
COLORANTS: Final = 26
PROCESS: Final = 27
FUNCTION: Final = 28
SHADING: Final = 29
PATTERN: Final = 30
EXTGSTATE: Final = 31
GS_FONT: Final = 32
SOFT_MASK: Final = 33
HALFTONE: Final = 34     # decided by its /HalftoneType: HALFTONE or HALFTONE5
HALFTONE5: Final = 35
DRAWN_ONLY: Final = 36   # an XObject neither form nor image: drawable, follows nothing
CID_FONT: Final = 37     # a Type0 font's descendant: loaded as a CID font, never Type3
ROLES: Final = 38

_FONT_KEYS: Final = {b"FontDescriptor": FONT_DESCRIPTOR, b"ToUnicode": LEAF,
                     b"Encoding": CMAP, b"DescendantFonts": DESCENDANTS,
                     b"CIDToGIDMap": LEAF}
_FORM_KEYS: Final = {b"Resources": RESOURCES, b"Group": GROUP}
# Each role's closed list: (role, key) -> the role of what it references.
_NEXT: Final[dict[tuple[int, bytes], int]] = {
    (role, key): target for role, keys in {
        PAGE: {b"Contents": CONTENTS, b"Resources": RESOURCES, b"Group": GROUP},
        RES_HOLDER: {b"Resources": RESOURCES},
        CATALOG: {b"AcroForm": ACROFORM},
        ACROFORM: {b"DR": RESOURCES},
        CONTENTS: {ARRAY: LEAF},
        RESOURCES: {b"XObject": CAT_XOBJECT, b"Font": CAT_FONT, b"Pattern": CAT_PATTERN,
                    b"Shading": CAT_SHADING, b"ExtGState": CAT_EXTGSTATE,
                    b"ColorSpace": CAT_COLOR_SPACE},
        FORM: _FORM_KEYS,
        GROUP: {b"CS": COLOR_SPACE},
        IMAGE: {b"SMask": IMAGE, b"Mask": IMAGE, b"ColorSpace": COLOR_SPACE,
                b"DecodeParms": DECODE_PARMS},
        DECODE_PARMS: {ARRAY: DECODE_PARMS_ITEM, b"JBIG2Globals": LEAF},
        DECODE_PARMS_ITEM: {b"JBIG2Globals": LEAF},
        FONT: _FONT_KEYS,
        TYPE3: _FONT_KEYS | {b"CharProcs": CHARPROCS, b"Resources": RESOURCES},
        DESCENDANTS: {ARRAY: CID_FONT},
        CID_FONT: _FONT_KEYS,
        FONT_DESCRIPTOR: {b"FontFile": LEAF, b"FontFile2": LEAF, b"FontFile3": LEAF,
                          b"CIDSet": LEAF},
        CMAP: {b"UseCMap": CMAP},
        COLOR_SPACE: {ARRAY: COLOR_SPACE, b"Functions": FUNCTION, b"Colorants": COLORANTS,
                      b"Process": PROCESS},
        PROCESS: {b"ColorSpace": COLOR_SPACE},
        FUNCTION: {ARRAY: FUNCTION, b"Functions": FUNCTION},
        SHADING: {b"Function": FUNCTION, b"ColorSpace": COLOR_SPACE},
        PATTERN: {b"Resources": RESOURCES, b"Shading": SHADING, b"ExtGState": EXTGSTATE},
        EXTGSTATE: {b"SMask": SOFT_MASK, b"TR": FUNCTION, b"TR2": FUNCTION, b"BG": FUNCTION,
                    b"BG2": FUNCTION, b"UCR": FUNCTION, b"UCR2": FUNCTION, b"HT": HALFTONE,
                    b"Font": GS_FONT},
        GS_FONT: {ARRAY: FONT},
        SOFT_MASK: {b"G": FORM, b"TR": FUNCTION},
        HALFTONE: {b"TransferFunction": FUNCTION},
        HALFTONE5: {b"TransferFunction": FUNCTION},
    }.items() for key, target in keys.items()}
# Dictionaries drawn by name: every entry (a dictionary key, never an
# array position) takes the role.
_ANY: Final[dict[int, int]] = {
    CAT_XOBJECT: XOBJECT, CAT_FONT: FONT, CAT_PATTERN: PATTERN, CAT_SHADING: SHADING,
    CAT_EXTGSTATE: EXTGSTATE, CAT_COLOR_SPACE: COLOR_SPACE, CHARPROCS: LEAF,
    COLORANTS: COLOR_SPACE, HALFTONE5: HALFTONE}
# A type 5 halftone's own keys, not sub-halftones.
_HALFTONE5_OWN: Final = frozenset({b"Type", b"HalftoneType", b"HalftoneName"})
# Roles decided by a value of the object itself.
_DECIDED: Final = frozenset({XOBJECT, FONT, HALFTONE})

# Work charged per body looked up (build.py's callbacks: several steps).
BODY_WORK: Final = 3
# The font subtypes MuPDF loads by name (pdf_load_font); any other, with
# /CharProcs, it guesses Type3.
_FONT_SUBTYPES: Final = frozenset({b"Type0", b"Type1", b"MMType1", b"TrueType", b"Type3"})


def _name(value: PdfValue | None) -> bytes | None:
    return value.raw if isinstance(value, PdfName) else None


def _present(value: PdfValue | None) -> bool:
    """A key MuPDF finds (a direct null is an absent key)."""
    return value is not None and not isinstance(value, PdfNull)


def role_at(role: int, path: tuple[bytes, ...],
            decide: Callable[[int, tuple[bytes, ...]], int] | None = None) -> int | None:
    """The role a reference at *path* gives its target, in a body read
    in *role* (None: the edge leads nowhere drawable). *decide(role,
    prefix)* settles a role decided by the direct value at *prefix*
    (without it: the narrower reading)."""
    for i, key in enumerate(path):
        found = _NEXT.get((role, key))
        if found is None and key != ARRAY:
            found = _ANY.get(role)
            if found == HALFTONE and key in _HALFTONE5_OWN:
                found = None
        if found is None:
            return None
        role = found
        if role in _DECIDED and i + 1 < len(path):
            # A direct value deciding its own role (an XObject must be a
            # stream: a direct one draws nothing).
            role = decide(role, path[:i + 1]) if decide is not None else _narrow(role)
    return role


def _narrow(role: int) -> int:
    """A decided role's narrower reading: an XObject follows nothing, a
    font is not Type3, a halftone not type 5."""
    return DRAWN_ONLY if role == XOBJECT else role


# A start: (number, generation or None: any live one, role, read it -- False
# only for the catalog, compared by its /AcroForm as written instead).
Start = tuple[int, int | None, int, bool]


class Drawing:
    """One revision's drawing closure (see the module docstring)."""

    def __init__(self, revision: int, body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None],
                 edges: dict[UnitRef, tuple[Edge, ...]], budget: Budget) -> None:
        self.revision, self.body_of, self.home_of, self.edges, self.budget = (
            revision, body_of, home_of, edges, budget)
        self.read: set[int] = set()
        # The numbers a role was decided by: a body whose own value (or a
        # direct value in it) was read, and each reference target read.
        self.decided: set[int] = set()
        # Each dictionary a decision read, its entries by key (``get``).
        self.index: dict[int, dict[bytes, PdfValue | None]] = {}
        self.spent = False

    def charge(self, n: int = 1) -> bool:
        if not self.budget.charge_work(n):
            self.spent = True
        return not self.spent

    def body(self, number: int, gen: int | None, read: bool = True) -> Body | None:
        """The live body ``number gen R`` names (any generation when *gen*
        is None), recording what was read (unless *read* is False)."""
        if read:
            self.read.add(number)
            home = self.home_of(number, self.revision)
            if home is not None:
                self.read.add(home)
        if not self.charge(BODY_WORK):
            return None
        body = self.body_of(number, self.revision)
        if body is None or number == 0 or (gen is not None and body[2] != gen):
            return None
        return body

    def run(self, starts: list[Start]) -> set[UnitRef] | None:
        """The streams the closure reaches from *starts*; None when spent."""
        drawable: set[UnitRef] = set()
        seen: set[tuple[int, int]] = set()
        stack = list(reversed(starts))
        while stack:
            if not self.charge():
                return None
            number, gen, role, read = stack.pop()
            if (number, role) in seen:
                continue
            seen.add((number, role))
            body = self.body(number, gen, read)
            if self.spent:
                return None
            if body is None:
                continue
            if body[4]:
                drawable.add(body[0])
            found = self.edges.get(body[0])
            if found is None:
                self.spent = True  # the graph stopped short: the budget is spent
                return None
            if role in _DECIDED:
                self.decided.add(number)
                role = self.decide(role, body[1], (), body[4])
                if self.spent:
                    return None
            decided: dict[tuple[bytes, ...], int] = {}

            def decide(inner: int, prefix: tuple[bytes, ...]) -> int:
                if prefix not in decided:
                    self.decided.add(number)
                    decided[prefix] = self.decide(inner, body[1], prefix)
                return decided[prefix]
            for edge in found:
                if not self.charge(len(edge.path) + 1):
                    return None
                target = role_at(role, edge.path, decide)
                if self.spent:
                    return None
                if target is not None and (edge.num, target) not in seen:
                    stack.append((edge.num, edge.gen, target, True))
        return drawable

    def decide(self, role: int, value: PdfValue | None, prefix: tuple[bytes, ...],
               stream: bool = False) -> int:
        """*role* settled by the value at *prefix* in a body's *value*: an
        XObject's /Subtype (a form or an image only when a stream: MuPDF
        draws no other), a font's /Subtype, a halftone's /HalftoneType,
        each through one reference; the narrower reading when unread."""
        for key in prefix:
            if key == ARRAY or not isinstance(value, PdfDict):
                return _narrow(role)
            value = self.get(value, key)
        if not isinstance(value, PdfDict):
            return _narrow(role)
        if role == HALFTONE:
            kind = self.resolve(self.get(value, b"HalftoneType"))
            return HALFTONE5 if isinstance(kind, PdfInt) and kind.value == 5 else HALFTONE
        name = _name(self.resolve(self.get(value, b"Subtype")))
        if role == FONT:
            # pdf_load_font: by /Subtype; any other (or none) with /CharProcs
            # is guessed Type3 ("unknown font format, guessing type3").
            if name == b"Type3" or (name not in _FONT_SUBTYPES
                                    and _present(self.get(value, b"CharProcs"))):
                return TYPE3
            return FONT
        if name == b"Form":
            # The Do operator: a form's /Subtype2, when present, replaces it.
            second = self.get(value, b"Subtype2")
            if _present(second):
                name = _name(self.resolve(second))
        if not stream:
            return DRAWN_ONLY  # not a stream (a direct XObject included): nothing drawn
        return FORM if name == b"Form" else IMAGE if name == b"Image" else DRAWN_ONLY

    def get(self, value: PdfDict, key: bytes) -> PdfValue | None:
        """``value.get(key)``, through an index of *value*'s entries made
        once per walk and charged by its size (PdfDict.get scans them: a
        dictionary of n direct fonts would be read n times)."""
        found = self.index.get(id(value))
        if found is None:
            if not self.charge(len(value.entries) + 1):
                return None
            found = {}
            for name, item in value.entries:
                found.setdefault(name.raw, item)
            for repeated in value.ambiguous:
                found[repeated] = None
            self.index[id(value)] = found
        return found.get(key)

    def resolve(self, value: PdfValue | None) -> PdfValue | None:
        """*value* through references, as MuPDF resolves a name (up to
        MAX_HOPS in a row), each recorded as read and as deciding."""
        hops = 0
        while isinstance(value, PdfRef):
            hops += 1
            if hops > MAX_HOPS:
                return None
            self.decided.add(value.num)
            held = self.body(value.num, value.gen)
            value = held[1] if held is not None else None
        return value

    def same(self, numbers: set[int], decided: set[int]) -> bool:
        """Does the last walk's set still hold, though *numbers* (read by
        it) changed? Each must be, before and after, a body that is not a
        stream (a stream's body is the unit labelled, and an object stream
        home's members move with it) with the same generation and the same
        references (number, generation, key path), and no role was decided
        by it (*decided*: a /Subtype or /HalftoneType read there). Then
        nothing is reached or left: a page or resource dictionary rewritten
        with the same references -- an update's usual shape -- keeps it."""
        if not self.charge(len(numbers) + 1):
            return False
        for number in sorted(numbers):
            if number in decided:
                return False
            old = self.body_of(number, self.revision + 1)
            new = self.body_of(number, self.revision)
            if (not self.charge(2 * BODY_WORK) or old is None or new is None or old[4] or new[4]
                    or old[2] != new[2]):
                return False
            before, after = self.edges.get(old[0]), self.edges.get(new[0])
            if before is None or after is None or not self.charge(len(before) + len(after) + 1):
                return False
            if ({(e.num, e.gen, e.path) for e in before}
                    != {(e.num, e.gen, e.path) for e in after}):
                return False
        return True
