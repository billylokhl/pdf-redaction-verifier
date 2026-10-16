"""The reference graph, part b (Phase 3a-7b, ADR 0011 item 2): each
revision's page tree, in the one shape both readers read alike, and each
page's inherited attributes.

The tree starts at the catalog's /Pages and must be canonical: the root
an indirect reference to a /Type /Pages node with a /Kids array and no
/Parent; every node a dictionary (not a stream) reached by exactly one
indirect reference in exactly one /Kids array; a node with /Kids is
/Type /Pages with an integer /Count equal to the pages below it, a node
without /Kids is /Type /Page; every /Parent an indirect reference to the
node that lists it; at most 100 /Pages levels (qpdf's limit). /Kids,
/Count and /Type may be indirect; a direct null value is an absent key.

Each page's /Resources, /MediaBox, /CropBox and /Rotate come from the
nearest of the page and its ancestors that has the key; that value must
not be a reference to null or to nothing. /Resources is a dictionary and
/MediaBox four numbers, both found; every box is four numbers with
positive area -- in MuPDF's 32-bit floats, and for /MediaBox and its
intersection with /CropBox (the page MuPDF shows) at least one unit wide
and tall: MuPDF replaces a smaller one with a 1x1 page and an empty one
with 612x792, where qpdf keeps the box as written; /Rotate is an integer
multiple of 90; no /UserUnit other than 1 on the page itself (MuPDF scales
the page by it and does not inherit it; qpdf ignores it); the tree holds at least one page (qpdf --check fails on
none); no /Pages node carries /ArtBox, /TrimBox or /BleedBox
(MuPDF inherits them, qpdf does not). A page's /Contents is absent, a
reference to a stream, or an array (direct or by reference) of
references to streams. Anything else is FlagReason.PAGE_TREE with a
closed PageTreeRule.

Revisions are read oldest first; a revision that changes no object the
previous walk read (nor the object stream one lives in) reuses its
result, so updates that leave the tree alone cost nothing here. The walk
is iterative, never raises, and is charged to the budget."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from ..budget import Budget
from ..ledger import Flag, FlagReason, UnitKind, UnitRef
from .graph import Body
from .objects import PdfArray, PdfDict, PdfInt, PdfName, PdfNull, PdfReal, PdfRef, PdfValue


class PageTreeRule(IntEnum):
    """Why a page tree was flagged PAGE_TREE (its ``rule`` param)."""

    ROOT = 1               # /Pages not a reference to a /Pages root with /Kids, no /Parent
    KID = 2                # a /Kids item not a reference to a live dictionary
    REACHED_TWICE = 3      # a node listed twice (a cycle, a page listed twice)
    NODE_TYPE = 4          # /Type against /Kids, or /Kids not an array
    COUNT = 5              # /Count not an integer equal to the pages below
    PARENT = 6             # /Parent not a reference to the node that lists it
    DEPTH = 7              # more than 100 /Pages levels
    ATTRIBUTE_MISSING = 8  # no /Resources or /MediaBox on the page or above
    ATTRIBUTE_NULL = 9     # the nearest value a reference to null or to nothing
    ATTRIBUTE_INVALID = 10  # not a dictionary, four numbers, positive area, a multiple of 90
    CROPBOX = 11           # /CropBox does not overlap /MediaBox
    NODE_BOX = 12          # /ArtBox, /TrimBox or /BleedBox on a /Pages node
    CONTENTS = 13          # /Contents not absent, a stream, or an array of streams
    NO_PAGES = 14          # a tree with no page (qpdf --check: an error)


INHERITED: Final = (b"Resources", b"MediaBox", b"CropBox", b"Rotate")
_PAGE_BOXES: Final = (b"BleedBox", b"TrimBox", b"ArtBox")
MAX_LEVELS: Final = 100
# Work charged for each page record made again in a later revision (retained
# memory, a few hundred bytes): at most one per 16 bytes of the file.
PAGE_RECORD_WORK: Final = 1024


@dataclass(frozen=True)
class Attribute:
    """An inherited attribute as found: its value as written (a reference
    or a direct value), the bytes it indexes, the object holding it, and
    its written bytes' identity in place (equal bytes, from a holder of the
    same generation read from the same kind of place -- file or object
    stream, as decryption keys on them -- read alike, whichever parse)."""

    value: PdfValue
    text: bytes
    source: int
    content: int


@dataclass(frozen=True)
class Page:
    number: int
    gen: int
    unit: UnitRef
    attributes: dict[bytes, Attribute]


_UNRESOLVED: Final = object()


def build_pages(revisions: int, changes: list[list[int]],
                body_of: Callable[[int, int], Body | None],
                home_of: Callable[[int, int], int | None],
                trailers: list[PdfDict | None],
                budget: Budget, tokens: Sequence[object] | None = None,
                ) -> tuple[tuple[tuple[Page, ...] | None, ...], list[Flag]]:
    """Each revision's page list (None where the tree is not canonical or
    the budget ran out), and the PAGE_TREE flags. *tokens* are each
    revision's reuse token (decrypted.reuse_tokens): a reading stands for
    another revision's only under the same token."""
    tokens = tokens if tokens is not None else (None,) * revisions
    results: list[tuple[Page, ...] | None] = [None] * revisions
    flags: list[Flag] = []
    seen: set[tuple[int, int]] = set()
    previous: tuple[set[int], PdfValue | None, tuple[Page, ...] | None, object] | None = None
    # A page read again unchanged is the same Page: retained memory grows
    # with the pages that change, not revisions x pages.
    kept: dict[tuple[object, ...], Page] = {}
    made: set[tuple[int, int]] = set()  # pages with a record already
    # Each attribute as written -- its bytes, its holder's generation and
    # whether it is read from the file or an object stream -- interned.
    written: dict[tuple[bytes, int, UnitKind, object], int] = {}
    for revision in reversed(range(revisions)):
        trailer = trailers[revision]
        root = trailer.get(b"Root") if trailer is not None else None
        if (previous is not None and _same_ref(previous[1], root)
                and previous[3] == tokens[revision]
                and not previous[0].intersection(changes[revision])):
            results[revision] = previous[2]
            continue
        walk = _Walk(revision, body_of, home_of, budget, kept, made, written, tokens[revision])
        pages = walk.run(root)
        if walk.spent:
            return tuple(results), flags
        for rule, number in walk.flags:
            if (rule, number) not in seen:
                seen.add((rule, number))
                flags.append(Flag(FlagReason.PAGE_TREE, None, (
                    ("rule", int(rule)), ("object", number), ("revision", revision))))
        results[revision] = pages
        previous = (walk.read, root, pages, tokens[revision])
    return tuple(results), flags


def _same_ref(a: PdfValue | None, b: PdfValue | None) -> bool:
    return (isinstance(a, PdfRef) and isinstance(b, PdfRef)
            and (a.num, a.gen) == (b.num, b.gen))


def _number(value: PdfValue | None) -> float | None:
    if isinstance(value, PdfInt) and value.value is not None:
        return float(value.value)
    if isinstance(value, PdfReal) and value.value is not None:
        return value.value
    return None


_INF: Final = float("inf")


def f32(value: float) -> float:
    """*value* as MuPDF holds it: rounded once, to nearest with ties to
    even, to a 32-bit float (infinite past its range) -- in exact integer
    arithmetic: the child process imports no math library."""
    if value != value or value - value != 0 or value == 0:  # NaN, infinite, zero
        return value
    sign = -1.0 if value < 0 else 1.0
    num, den = abs(value).as_integer_ratio()
    exponent = num.bit_length() - den.bit_length()
    if (num << max(0, -exponent)) < (den << max(0, exponent)):
        exponent -= 1  # now 2**exponent <= |value| < 2**(exponent + 1)
    if exponent > 127:
        return sign * _INF
    shift = 23 - max(exponent, -126)  # 24 significant bits; subnormals below 2**-126
    if shift >= 0:
        mantissa, rest = divmod(num << shift, den)
        half = den
    else:
        mantissa, rest = divmod(num, den << -shift)
        half = den << -shift
    if 2 * rest > half or (2 * rest == half and mantissa & 1):
        mantissa += 1
    result = mantissa / (1 << shift) if shift >= 0 else float(mantissa << -shift)
    return sign * (_INF if result >= 2.0 ** 128 else result)


Box = tuple[float, float, float, float]


def box_of(value: PdfValue | None, least: float = 0.0) -> Box | None:
    """Four finite numbers, in 32-bit floats, normalized (x0 <= x1,
    y0 <= y1), more than *least* wide and tall (at least 1 when *least*
    is 1: MuPDF's own threshold)."""
    if not isinstance(value, PdfArray) or len(value.items) != 4:
        return None
    n = [_number(item) for item in value.items]
    if any(v is None for v in n):
        return None
    x0, y0, x1, y1 = (f32(v) for v in n if v is not None)
    box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if not all(v - v == 0 for v in box):  # finite
        return None
    return box if _sized(box, least) else None


def _sized(box: Box, least: float) -> bool:
    w, h = box[2] - box[0], box[3] - box[1]
    return (w >= least and h >= least) if least else (w > 0 and h > 0)


def intersect(a: Box, b: Box) -> Box:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


class _Walk:
    def __init__(self, revision: int, body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None], budget: Budget,
                 kept: dict[tuple[object, ...], Page], made: set[tuple[int, int]],
                 written: dict[tuple[bytes, int, UnitKind, object], int],
                 token: object = None) -> None:
        self.revision, self.body_of, self.home_of, self.budget = (
            revision, body_of, home_of, budget)
        self.kept, self.made, self.written = kept, made, written
        self.token = token  # the revision's reuse token: part of every interned key
        self.read: set[int] = set()
        self.flags: list[tuple[PageTreeRule, int]] = []
        self.spent = False

    def flag(self, rule: PageTreeRule, number: int) -> None:
        self.flags.append((rule, number))

    def body(self, ref: PdfRef) -> Body | None:
        """The live body *ref* names in this revision (its generation
        included), recording what was read."""
        self.read.add(ref.num)
        home = self.home_of(ref.num, self.revision)
        if home is not None:
            self.read.add(home)
        body = self.body_of(ref.num, self.revision)
        return body if body is not None and body[2] == ref.gen and ref.num != 0 else None

    def value(self, value: PdfValue | None) -> PdfValue | None | object:
        """*value* with one reference resolved: _UNRESOLVED for a
        reference to nothing; None for an absent key or a direct null."""
        if isinstance(value, PdfRef):
            body = self.body(value)
            return _UNRESOLVED if body is None else body[1]
        return None if isinstance(value, PdfNull) else value

    def charge(self, n: int = 1) -> bool:
        if not self.budget.charge_work(n):
            self.spent = True
        return not self.spent

    def run(self, root: PdfValue | None) -> tuple[Page, ...] | None:
        before = len(self.flags)
        catalog = self.body(root) if isinstance(root, PdfRef) else None
        top = catalog[1].get(b"Pages") if catalog and isinstance(catalog[1], PdfDict) else None
        node = self.body(top) if isinstance(top, PdfRef) else None
        if (node is None or node[4] or not isinstance(node[1], PdfDict)
                or not isinstance(self.value(node[1].get(b"Kids")), PdfArray)
                or not _name_is(self.value(node[1].get(b"Type")), b"Pages")
                or self.value(node[1].get(b"Parent")) is not None):
            self.flag(PageTreeRule.ROOT, top.num if isinstance(top, PdfRef) else -1)
            return None
        assert isinstance(top, PdfRef)
        pages: list[Page] = []
        visited = {top.num}
        # (number, gen, body, depth, inherited) to enter; ("exit", number,
        # expected count, pages before) to check /Count on the way out.
        stack: list[tuple[object, ...]] = [(top.num, top.gen, node, 1, {})]
        while stack:
            if not self.charge():
                return None
            item = stack.pop()
            if item[0] == "exit":
                _, closing, count, start = item
                assert isinstance(closing, int) and isinstance(start, int)
                if count != len(pages) - start:
                    self.flag(PageTreeRule.COUNT, closing)
                continue
            number, gen, body, depth, inherited = item
            assert isinstance(number, int) and isinstance(gen, int) and isinstance(depth, int)
            assert isinstance(body, tuple) and isinstance(inherited, dict)
            value, text = body[1], body[3]
            assert isinstance(value, PdfDict)
            kids = self.value(value.get(b"Kids"))
            kind = self.value(value.get(b"Type"))
            here = inherited  # shared down the tree until a node sets its own
            for key in INHERITED:
                found = value.get(key)
                if found is not None and not isinstance(found, PdfNull):
                    if here is inherited:
                        here = dict(inherited)
                    written = text[found.start:found.end]
                    if not self.charge(len(written) + 1):
                        return None
                    # Equal bytes read alike only in the same place: an
                    # encrypted file's strings are decrypted by the holder's
                    # number and generation, and only outside object streams.
                    where = (written, gen, body[0].kind, self.token)
                    content = self.written.setdefault(where, len(self.written))
                    here[key] = Attribute(found, text, number, content)
            if kids is not None:  # a /Pages node
                if not isinstance(kids, PdfArray) or not _name_is(kind, b"Pages"):
                    self.flag(PageTreeRule.NODE_TYPE, number)
                    continue
                if depth > MAX_LEVELS:
                    self.flag(PageTreeRule.DEPTH, number)
                    continue
                if any(self.value(value.get(k)) is not None for k in _PAGE_BOXES):
                    self.flag(PageTreeRule.NODE_BOX, number)
                count = self.value(value.get(b"Count"))
                stack.append(("exit", number,
                              count.value if isinstance(count, PdfInt) else None, len(pages)))
                for kid in reversed(kids.items):
                    if not self.charge():
                        return None
                    child = self.body(kid) if isinstance(kid, PdfRef) else None
                    if child is None or child[4] or not isinstance(child[1], PdfDict):
                        self.flag(PageTreeRule.KID, number)
                        continue
                    assert isinstance(kid, PdfRef)
                    if kid.num in visited:
                        self.flag(PageTreeRule.REACHED_TWICE, kid.num)
                        continue
                    visited.add(kid.num)
                    parent = child[1].get(b"Parent")
                    if not (isinstance(parent, PdfRef) and (parent.num, parent.gen) == (number, gen)):
                        self.flag(PageTreeRule.PARENT, kid.num)
                    stack.append((kid.num, kid.gen, child, depth + 1, here))
                continue
            if not _name_is(kind, b"Page"):  # a page
                self.flag(PageTreeRule.NODE_TYPE, number)
                continue
            self.page(number, value, here)
            # The page's own body and each attribute as written (its bytes,
            # interned) name the reading: a rewritten ancestor whose
            # attributes read the same keeps every page's record.
            ident = (number, gen, body[0], self.token,
                     *((k, a.content, a.source) for k, a in sorted(here.items())))
            if ident not in self.kept:
                # A page's first record is as big as its own object in the
                # file; one made again in a later revision (an ancestor or an
                # attribute rewritten) is retained memory the file did not
                # pay for, so it is charged: a file whose updates rewrite an
                # ancestor of many pages runs out of budget, not of memory.
                if (number, gen) in self.made and not self.charge(PAGE_RECORD_WORK):
                    return None
                self.made.add((number, gen))
                self.kept[ident] = Page(number, gen, body[0], here)
            pages.append(self.kept[ident])
        # Kids are pushed reversed: pages come off the stack in document order.
        if not pages:
            self.flag(PageTreeRule.NO_PAGES, top.num)
        return tuple(pages) if len(self.flags) == before else None

    def page(self, number: int, value: PdfDict, attributes: dict[bytes, Attribute]) -> None:
        resolved: dict[bytes, PdfValue | None | object] = {}
        for key in INHERITED:
            found = attributes.get(key)
            if not self.charge():
                return
            resolved[key] = self.value(found.value) if found is not None else None
            if found is not None and (resolved[key] in (_UNRESOLVED, None)
                                      or isinstance(resolved[key], PdfNull)):
                self.flag(PageTreeRule.ATTRIBUTE_NULL, number)
        resources, media = resolved[b"Resources"], resolved[b"MediaBox"]
        if b"Resources" not in attributes or b"MediaBox" not in attributes:
            self.flag(PageTreeRule.ATTRIBUTE_MISSING, number)
        if b"Resources" in attributes and not isinstance(resources, PdfDict):
            self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
        elif isinstance(resources, PdfDict) and self._is_stream(attributes[b"Resources"].value):
            self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
        media_box = box_of(media, least=1.0) if isinstance(media, PdfArray) else None
        if b"MediaBox" in attributes and media_box is None:
            self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
        crop = resolved[b"CropBox"]
        if b"CropBox" in attributes:
            crop_box = box_of(crop) if isinstance(crop, PdfArray) else None
            if crop_box is None:
                self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
            elif media_box is not None and not _sized(intersect(crop_box, media_box), 1.0):
                self.flag(PageTreeRule.CROPBOX, number)
        unit = self.value(value.get(b"UserUnit"))
        if unit is not None and not (isinstance(unit, (PdfInt, PdfReal))
                                     and _number(unit) == 1.0):
            self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
        rotate = resolved[b"Rotate"]
        if b"Rotate" in attributes and not (
                isinstance(rotate, PdfInt) and rotate.value is not None
                and rotate.value % 90 == 0):
            self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
        for key in _PAGE_BOXES:
            box = self.value(value.get(key))
            if box is not None and (not isinstance(box, PdfArray) or box_of(box) is None):
                self.flag(PageTreeRule.ATTRIBUTE_INVALID, number)
        self.contents(number, value.get(b"Contents"))

    def _is_stream(self, value: PdfValue) -> bool:
        if not isinstance(value, PdfRef):
            return False
        body = self.body(value)
        return body is not None and body[4]

    def contents(self, number: int, contents: PdfValue | None) -> None:
        if contents is None or isinstance(contents, PdfNull):
            return
        items: tuple[PdfValue, ...]
        if isinstance(contents, PdfRef):
            body = self.body(contents)
            if body is not None and body[4]:
                return
            if body is None or not isinstance(body[1], PdfArray):
                self.flag(PageTreeRule.CONTENTS, number)
                return
            items = body[1].items
        elif isinstance(contents, PdfArray):
            items = contents.items
        else:
            self.flag(PageTreeRule.CONTENTS, number)
            return
        for item in items:
            if not self.charge():
                return
            body = self.body(item) if isinstance(item, PdfRef) else None
            if body is None or not body[4]:
                self.flag(PageTreeRule.CONTENTS, number)
                return


def _name_is(value: object, name: bytes) -> bool:
    return isinstance(value, PdfName) and value.raw == name
