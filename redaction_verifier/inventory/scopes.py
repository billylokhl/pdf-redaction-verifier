"""The reference graph, part c (Phase 3a-7c, ADR 0011 item 3): resource
scopes.

A form XObject, a tiling pattern, a Type3 font and an annotation
appearance stream must carry their own /Resources: a dictionary, direct
or an indirect reference to a live one (not a stream). Without it MuPDF
borrows the caller's resources or a cached font's, and qpdf runs no
content: FlagReason.RESOURCE_SCOPE. Which bodies must carry one is found
two ways, and their union decides, so neither a misread role nor a
misdeclared type exempts one:

- by type, in every live body: /Subtype /Form or /Type3, or /PatternType
  1 (a value given by reference is resolved; one that does not resolve
  to a name -- an integer -- counts as the type, failing closed);
- by role, in every live body: a stream reached from a resource
  dictionary's /XObject (an image excepted, by its /Subtype) or /Pattern,
  or as an annotation appearance under /AP (/N, /R or /D, directly or by
  appearance state). A resource dictionary is the value of any /Resources
  or /DR key (a page's, a form's, a pattern's, a Type3 font's, an
  appearance's, /AcroForm's); every hop -- the resource dictionary, its
  category dictionary, /AP, an appearance's state dictionary -- may be
  direct or by reference. Roles are read from key paths, from every
  occurrence of a holder key along a path, so a resource or state named
  like a holder key (/Resources << /XObject << /AP 4 0 R >> >>) keeps
  both readings.

Each scope records only its innermost dictionary, as written: 4a
resolves names there and flags a miss (item 3), never through MuPDF.

Incremental, like the edges: a body's role-giving references are
derived once per (body, role it is read in) and kept by target; a role
appearing on or vanishing from a number re-derives that number's own
contributions (roles nest at most three deep: resource dictionary,
category, entry; /AP, state dictionary, stream). A revision checks only
the numbers whose body changed, those that gained a role, and those
whose check read a number that changed (a /Resources, /Subtype or
/PatternType target). Every step is charged to the budget; one flag per
body, whatever the number of revisions it persists through."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Final

from ..budget import Budget
from ..ledger import Flag, FlagReason, UnitRef
from .graph import ARRAY, Body, Edge
from .objects import PdfDict, PdfInt, PdfName, PdfRef, PdfValue


class Role(Enum):
    """How a number is reached, by the key path that reaches it."""

    RESOURCES = 1   # a resource dictionary (/Resources, /DR)
    CATEGORY = 2    # its /XObject or /Pattern dictionary
    DRAWN = 3       # an entry of one: a form XObject, an image or a pattern
    AP = 4          # an annotation's /AP dictionary
    STATE = 5       # /AP's /N, /R or /D: an appearance stream or a state dictionary
    APPEARANCE = 6  # a state dictionary's entry: an appearance stream


_HOLDERS: Final = frozenset({b"Resources", b"DR"})
_CATEGORIES: Final = frozenset({b"XObject", b"Pattern"})
_STATES: Final = frozenset({b"N", b"R", b"D"})
# The roles whose holder passes a role on to what it references.
_PASSING: Final = (Role.RESOURCES, Role.CATEGORY, Role.AP, Role.STATE)
# The roles that oblige a stream to carry /Resources.
_OBLIGING: Final = frozenset({Role.DRAWN, Role.STATE, Role.APPEARANCE})


def _below_holder(suffix: tuple[bytes, ...]) -> Role | None:
    """The role of a reference at *suffix* below a resource dictionary."""
    if not suffix:
        return Role.RESOURCES
    if suffix[0] not in _CATEGORIES:
        return None
    if len(suffix) == 1:
        return Role.CATEGORY
    return Role.DRAWN if len(suffix) == 2 and suffix[1] != ARRAY else None


def _below_ap(suffix: tuple[bytes, ...]) -> Role | None:
    """The role of a reference at *suffix* below an /AP dictionary."""
    if not suffix:
        return Role.AP
    if suffix[0] not in _STATES:
        return None
    if len(suffix) == 1:
        return Role.STATE
    return Role.APPEARANCE if len(suffix) == 2 and suffix[1] != ARRAY else None


def roles_at(via: Role | None, path: tuple[bytes, ...]) -> set[Role]:
    """The roles a reference at *path* gives its target, in a body read
    in role *via* (None: any live body, by its own keys)."""
    found: set[Role] = set()
    if via is None:
        for i, key in enumerate(path):
            role = (_below_holder(path[i + 1:]) if key in _HOLDERS
                    else _below_ap(path[i + 1:]) if key == b"AP" else None)
            if role is not None:
                found.add(role)
    elif via is Role.RESOURCES:
        role = _below_holder(path)
        if role is not None and role is not Role.RESOURCES:
            found.add(role)
    elif via is Role.AP:
        role = _below_ap(path)
        if role is not None and role is not Role.AP:
            found.add(role)
    elif len(path) == 1 and path[0] != ARRAY:
        found.add(Role.DRAWN if via is Role.CATEGORY else Role.APPEARANCE)
    return found


_Source = tuple[int, Role | None]  # a number, and the role its body is read in


class Scopes:
    """The resource scopes' state across revisions (oldest first), fed
    by build_graph with the numbers whose body changed in each."""

    def __init__(self, current: dict[int, Body | None],
                 edges: dict[UnitRef, tuple[Edge, ...]], budget: Budget,
                 flags: list[Flag]) -> None:
        self.current, self.edges, self.budget, self.flags = current, edges, budget, flags
        # (target, role) -> the sources giving it; a source's contributions.
        self.givers: dict[tuple[int, Role], set[_Source]] = {}
        self.given: dict[_Source, frozenset[tuple[int, Role]]] = {}
        # A check's reads: number -> numbers it read; number -> its readers.
        self.reads: dict[int, set[int]] = {}
        self.readers: dict[int, set[int]] = {}
        self.flagged: set[UnitRef] = set()
        # Each obligated body's own /Resources as written (direct, or the
        # reference), by body: its scope's innermost dictionary.
        self.scopes: dict[UnitRef, PdfValue] = {}

    def has(self, number: int, role: Role) -> bool:
        return bool(self.givers.get((number, role)))

    def revision(self, revision: int, moved: Iterable[int]) -> bool:
        """Update roles for the bodies that changed and check what that
        can change; False when the budget is spent."""
        candidates: set[int] = set()
        toggled: list[tuple[int, Role]] = []
        for number in moved:
            candidates.add(number)
            readers = self.readers.get(number, set())
            if not self.budget.charge_work(len(readers) + 1):
                return False
            candidates |= readers
            for via in (None, *(r for r in _PASSING if self.has(number, r))):
                if not self._derive((number, via), toggled):
                    return False
        while toggled:
            number, role = toggled.pop()
            if role in _OBLIGING:
                candidates.add(number)
            if role in _PASSING and not self._derive((number, role), toggled):
                return False
        if not self.budget.charge_work(2 * len(candidates) + 1):  # sorted
            return False
        for number in sorted(candidates):
            if not self._check(number, revision):
                return False
        return True

    def _derive(self, source: _Source, toggled: list[tuple[int, Role]]) -> bool:
        """Re-derive *source*'s contributions from its current body (none
        when it has no body, or no longer the role), appending each
        (target, role) that appears or vanishes to *toggled*."""
        number, via = source
        body = self.current.get(number)
        new: set[tuple[int, Role]] = set()
        if body is not None and (via is None or self.has(number, via)):
            for edge in self.edges.get(body[0], ()):
                if not self.budget.charge_work(len(edge.path) + 1):
                    return False
                for role in roles_at(via, edge.path):
                    new.add((edge.num, role))
        old = self.given.pop(source, frozenset())
        if new:
            self.given[source] = frozenset(new)
        if not self.budget.charge_work(len(old) + len(new) + 1):
            return False
        for key in old - new:
            holders = self.givers.get(key)
            if holders is not None:
                holders.discard(source)
                if not holders:
                    del self.givers[key]
                    toggled.append(key)
        for key in new - old:
            holders = self.givers.setdefault(key, set())
            if not holders:
                toggled.append(key)
            holders.add(source)
        return True

    def _resolve(self, value: PdfValue | None, read: set[int]) -> PdfValue | None:
        """A direct value, or what a reference names when it is live with
        its generation and not a stream (None otherwise)."""
        if not isinstance(value, PdfRef):
            return value
        read.add(value.num)
        body = self.current.get(value.num)
        if body is None or value.num == 0 or body[2] != value.gen or body[4]:
            return None
        return body[1]

    def _check(self, number: int, revision: int) -> bool:
        body = self.current.get(number)
        if body is not None and isinstance(body[1], PdfDict) and not self.budget.charge_work(
                len(body[1].entries) + 1):  # its keys looked up below
            return False
        read: set[int] = set()
        scope = self._obligation(body, number, read)
        for target in self.reads.pop(number, set()) - read:
            self.readers.get(target, set()).discard(number)
        for target in read:
            self.readers.setdefault(target, set()).add(number)
        if read:
            self.reads[number] = read
        if not self.budget.charge_work(2 * len(read) + 1):
            return False
        if scope is None:
            return True
        assert body is not None
        unit = body[0]
        if scope is _MISSING:
            if unit not in self.flagged:
                self.flagged.add(unit)
                self.flags.append(Flag(FlagReason.RESOURCE_SCOPE, None, (
                    ("object", number), ("revision", revision))))
        elif isinstance(scope, (PdfDict, PdfRef)):
            self.scopes[unit] = scope
        return True

    def _obligation(self, body: Body | None, number: int, read: set[int]) -> object:
        """None when *body* need not carry /Resources; else its own
        /Resources as written, or _MISSING."""
        if body is None or not isinstance(body[1], PdfDict):
            return None
        value, stream = body[1], body[4]
        subtype = value.get(b"Subtype")
        kind = self._resolve(subtype, read)
        typed = isinstance(subtype, PdfRef) and not isinstance(kind, PdfName)
        if isinstance(kind, PdfName) and kind.raw in (b"Form", b"Type3"):
            typed = True
        pattern = value.get(b"PatternType")
        tiling = self._resolve(pattern, read)
        if isinstance(pattern, PdfRef) and not isinstance(tiling, PdfInt):
            typed = True
        if isinstance(tiling, PdfInt) and tiling.value == 1:
            typed = True
        image = isinstance(kind, PdfName) and kind.raw == b"Image"
        roled = stream and (self.has(number, Role.STATE) or self.has(number, Role.APPEARANCE)
                            or (not image and self.has(number, Role.DRAWN)))
        if not (typed or roled):
            return None
        resources = value.get(b"Resources")
        if isinstance(resources, PdfDict):
            return resources
        if isinstance(resources, PdfRef) and isinstance(self._resolve(resources, read), PdfDict):
            return resources
        return _MISSING


_MISSING: Final = object()
