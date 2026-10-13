"""The reference graph, part c (Phase 3a-7c, ADR 0011 item 4 with its
2026-10-11 amendment): name and number trees.

Every subtree of the catalog's /Names (all of them, whether a reader
reads them or not: our own decoders walk them all) is a name tree, and
the catalog's /PageLabels a number tree. Each node is a dictionary (not
a stream) with exactly one of /Names (/Nums) -- an array of key-value
pairs -- or /Kids -- an array of indirect references to nodes, none
reached twice; every node below the root has /Limits naming its least
and greatest key. Keys are strings (integers) unique and in order across
the whole tree: by bytes, and (owner decision 9, P3) as text, which is
what both readers compare -- so a name key must decode the same in both:
lossless, without a language tag, and in PDFDocEncoding only from the
bytes both readers map alike (printable ASCII and 0xA1-0xFF but 0xAD;
one trailing NUL after printable ASCII allowed, owner decision 10) or in
UTF-16BE without U+0000 or U+001B -- unique and in order under each
reader's reading. An empty root (neither /Names nor /Kids, or an empty array)
reads as empty in both readers (owner decision R5). A non-empty name tree
in an encrypted revision is flagged until 3a-9 decrypts its keys (P4).
Anything else is FlagReason.NAME_TREE with a closed NameTreeRule.

Revisions are read oldest first, and a revision that changes nothing the
previous walk read reuses its result. Iterative, never raises, charged
to the budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Literal

from ..budget import Budget
from ..ledger import Flag, FlagReason
from .graph import Body
from .objects import PdfArray, PdfDict, PdfInt, PdfNull, PdfRef, PdfString, PdfValue
from .strings import TextEncoding, text_string


class NameTreeRule(IntEnum):
    """Why a tree was flagged NAME_TREE (its ``rule`` param)."""

    NODE = 1           # a node not a dictionary, or not exactly one of /Names and /Kids
    ENTRIES = 2        # /Names (/Nums) not key-value pairs with string (integer) keys
    ORDER = 3          # keys not unique and in order by bytes (numbers: by value)
    TEXT = 4           # a key both readers may not read as the same text, or out of order as text
    KIDS = 5           # /Kids not an array of references to dictionaries
    REACHED_TWICE = 6  # a node listed twice (a cycle)
    LIMITS = 7         # a node below the root without /Limits [least greatest]
    ENCRYPTED = 8      # a non-empty name tree in an encrypted revision (until 3a-9)
    NAMES = 9          # the catalog's /Names not a dictionary


@dataclass(frozen=True)
class Entry:
    """A tree entry as written: its key's bytes (a string's, or an
    integer's value), its value, and the bytes the value indexes."""

    key: bytes | int
    value: PdfValue
    text: bytes


# Each revision's trees: (the catalog's key, e.g. b"EmbeddedFiles", or
# b"PageLabels") -> entries in order; None where flagged.
Trees = dict[bytes, tuple[Entry, ...]]


def build_trees(revisions: int, changes: list[list[int]],
                body_of: Callable[[int, int], Body | None],
                home_of: Callable[[int, int], int | None],
                trailers: list[PdfDict | None],
                budget: Budget) -> tuple[tuple[Trees | None, ...], list[Flag]]:
    results: list[Trees | None] = [None] * revisions
    flags: list[Flag] = []
    seen: set[tuple[int, int]] = set()
    previous: tuple[set[int], tuple[object, ...], Trees | None] | None = None
    for revision in reversed(range(revisions)):
        trailer = trailers[revision]
        root = trailer.get(b"Root") if trailer is not None else None
        encrypted = trailer is not None and trailer.get(b"Encrypt") is not None
        ident = ((root.num, root.gen) if isinstance(root, PdfRef) else None, encrypted)
        if (previous is not None and previous[1] == ident
                and not previous[0].intersection(changes[revision])):
            results[revision] = previous[2]
            continue
        walk = _Walk(revision, body_of, home_of, budget, encrypted)
        trees = walk.run(root)
        if walk.spent:
            return tuple(results), flags
        for rule, number in walk.flags:
            if (rule, number) not in seen:
                seen.add((rule, number))
                flags.append(Flag(FlagReason.NAME_TREE, None, (
                    ("rule", int(rule)), ("object", number), ("revision", revision))))
        results[revision] = trees
        previous = (walk.read, ident, trees)
    return tuple(results), flags


def key_text(raw: bytes) -> str | None:
    """A name key as MuPDF reads it as text, or None when the readers may
    not read it alike.

    UTF-16BE without U+0000 (MuPDF handles text as C strings) and without
    U+001B (MuPDF strips a language escape byte-wise -- ESC, 2 or 4 bytes
    of any value, ESC -- where libqpdf strips nothing and our own tag rule
    matches only ASCII letters). Or PDFDocEncoding from the bytes both
    readers map alike: printable ASCII and 0xA1-0xFF but 0xAD, which MuPDF
    maps to U+0000 and qpdf does not. A printable-ASCII key may end in one
    NUL byte (owner decision 10, 2026-10-12, amending P3): MuPDF cuts it
    off, libqpdf keeps it (qpdf_key_text), and both resolve the key as
    written to the same object (tests/test_inventory_trees.py); MuPDF keeps
    the NUL of a key holding any byte at or above 0x80, so there it flags.
    Keys are checked unique and in order under both readings (_order):
    with keys of mixed encodings, order under one does not imply order
    under the other."""
    decoded = text_string(raw)
    if not decoded.lossless or decoded.tags_removed:
        return None
    if decoded.encoding is TextEncoding.UTF16BE:
        return None if "\0" in decoded.text or "\x1b" in decoded.text else decoded.text
    if decoded.encoding is not TextEncoding.PDFDOC:
        return None
    if raw.endswith(b"\0"):
        body = raw[:-1]
        return decoded.text[:len(body)] if all(0x20 <= b <= 0x7E for b in body) else None
    if all((0x20 <= b <= 0x7E or b >= 0xA1) and b != 0xAD for b in raw):
        return decoded.text
    return None


def qpdf_key_text(raw: bytes) -> str | None:
    """The key as libqpdf reads it as text: key_text, with the trailing NUL
    kept."""
    text = key_text(raw)
    if text is None or text_string(raw).encoding is TextEncoding.UTF16BE:
        return text
    return text + "\0" if raw.endswith(b"\0") else text


_LEAF: Final = {False: b"Names", True: b"Nums"}


class _Walk:
    def __init__(self, revision: int, body_of: Callable[[int, int], Body | None],
                 home_of: Callable[[int, int], int | None], budget: Budget,
                 encrypted: bool) -> None:
        self.revision, self.body_of, self.home_of, self.budget = (
            revision, body_of, home_of, budget)
        self.encrypted = encrypted
        self.read: set[int] = set()
        # Every node reached in this revision, across all its trees: a
        # node two trees share is reached twice, and no node is walked
        # twice (one per tree would cost trees x nodes).
        self.visited: set[int] = set()
        self.flags: list[tuple[NameTreeRule, int]] = []
        self.spent = False

    def flag(self, rule: NameTreeRule, number: int) -> None:
        self.flags.append((rule, number))

    def charge(self, n: int = 1) -> bool:
        if not self.budget.charge_work(n):
            self.spent = True
        return not self.spent

    def body(self, ref: PdfRef) -> Body | None:
        self.read.add(ref.num)
        home = self.home_of(ref.num, self.revision)
        if home is not None:
            self.read.add(home)
        body = self.body_of(ref.num, self.revision)
        return body if body is not None and body[2] == ref.gen and ref.num != 0 else None

    def node(self, value: PdfValue | None, text: bytes,
             visited: set[int]) -> tuple[PdfDict, bytes, int] | None | Literal[False]:
        """A node: (dictionary, its bytes, its number or -1); None when it
        is no dictionary; False when it was reached before."""
        if isinstance(value, PdfRef):
            if value.num in visited:
                return False
            visited.add(value.num)
            body = self.body(value)
            if body is None or body[4] or not isinstance(body[1], PdfDict):
                return None
            return body[1], body[3], value.num
        return (value, text, -1) if isinstance(value, PdfDict) else None

    def resolve(self, value: PdfValue | None) -> PdfValue | None:
        if isinstance(value, PdfRef):
            body = self.body(value)
            return body[1] if body is not None and not body[4] else None
        return None if isinstance(value, PdfNull) else value

    def run(self, root: PdfValue | None) -> Trees | None:
        before = len(self.flags)
        catalog = self.body(root) if isinstance(root, PdfRef) else None
        if catalog is None or not isinstance(catalog[1], PdfDict):
            return {}  # MISSING_ROOT's (the chain), not a tree's
        trees: Trees = {}
        names = catalog[1].get(b"Names")
        if names is not None and not isinstance(names, PdfNull):
            found = self.node(names, catalog[3], self.visited)
            if not isinstance(found, tuple):
                self.flag(NameTreeRule.NAMES, names.num if isinstance(names, PdfRef) else -1)
            else:
                for key, tree in found[0].entries:
                    if not self.charge():
                        return None
                    if isinstance(tree, PdfNull):  # a direct null is an absent key
                        continue
                    entries = self.tree(tree, found[1], number=False)
                    if entries is None:
                        continue
                    if entries and self.encrypted:
                        self.flag(NameTreeRule.ENCRYPTED,
                                  tree.num if isinstance(tree, PdfRef) else -1)
                    trees[key.raw] = entries
        labels = catalog[1].get(b"PageLabels")
        if labels is not None and not isinstance(labels, PdfNull):
            entries = self.tree(labels, catalog[3], number=True)
            if entries is not None:
                trees[b"PageLabels"] = entries
        return trees if len(self.flags) == before else None

    def tree(self, root: PdfValue, text: bytes, *, number: bool) -> tuple[Entry, ...] | None:
        """One tree's entries in order, or None when flagged."""
        before = len(self.flags)
        visited = self.visited
        entries: list[Entry] = []
        # An encrypted name tree's keys (and /Limits) are ciphertext here:
        # its shape is checked, its keys are not -- it is flagged ENCRYPTED
        # when it holds any (P4).
        ciphered = self.encrypted and not number
        # (value, bytes, is_root) to enter; ("exit", node number, limits,
        # first entry index) to check /Limits on the way out.
        stack: list[tuple[object, ...]] = [(root, text, True)]
        leaf = _LEAF[number]
        while stack:
            if not self.charge():
                return None
            item = stack.pop()
            if item[0] == "exit":
                _, holder, limits, start = item
                assert isinstance(holder, int) and isinstance(start, int)
                # The subtree's least and greatest keys, if its entries are
                # in order (else ORDER flags it): its first and last, never
                # a copy (a deep tree would copy each leaf once per level).
                # Comparing passes over the keys' bytes: charged by their
                # length (a /Limits array many nodes share by reference
                # would otherwise compare a long key once per node, free).
                if isinstance(limits, tuple) and not self.charge(
                        sum(len(k) if isinstance(k, bytes) else 1 for k in limits) + 1):
                    return None
                if len(entries) <= start or (not ciphered and limits != (
                        entries[start].key, entries[-1].key)):
                    self.flag(NameTreeRule.LIMITS, holder)
                continue
            value, here, is_root = item
            assert isinstance(here, bytes)
            found = self.node(value, here, visited)  # type: ignore[arg-type]
            holder = value.num if isinstance(value, PdfRef) else -1
            if found is False:
                self.flag(NameTreeRule.REACHED_TWICE, holder)
                continue
            if found is None:
                self.flag(NameTreeRule.NODE, holder)
                continue
            node, node_text, holder = found
            pairs, kids = self.resolve(node.get(leaf)), self.resolve(node.get(b"Kids"))
            empty_pairs = isinstance(pairs, PdfArray) and not pairs.items
            if is_root and (pairs is None or empty_pairs) and kids is None:
                continue  # an empty root reads as empty (R5)
            if (pairs is None) == (kids is None):
                self.flag(NameTreeRule.NODE, holder)
                continue
            if not is_root:
                limits = self._limits(self.resolve(node.get(b"Limits")), number)
                stack.append(("exit", holder, limits, len(entries)))
            if pairs is not None:
                if not isinstance(pairs, PdfArray) or not pairs.items or len(pairs.items) % 2:
                    self.flag(NameTreeRule.ENTRIES, holder)
                    continue
                for key, paired in zip(pairs.items[::2], pairs.items[1::2]):
                    if not self.charge():
                        return None
                    if number and isinstance(key, PdfInt) and key.value is not None:
                        entries.append(Entry(key.value, paired, node_text))
                    elif not number and isinstance(key, PdfString):
                        entries.append(Entry(key.raw, paired, node_text))
                    else:
                        self.flag(NameTreeRule.ENTRIES, holder)
                        break
                continue
            if not isinstance(kids, PdfArray) or not kids.items:
                self.flag(NameTreeRule.KIDS, holder)
                continue
            for kid in reversed(kids.items):
                if not isinstance(kid, PdfRef):
                    self.flag(NameTreeRule.KIDS, holder)
                    continue
                stack.append((kid, node_text, False))
        if not ciphered:
            self._order(entries, number, root.num if isinstance(root, PdfRef) else -1)
        return tuple(entries) if len(self.flags) == before else None

    def _limits(self, value: PdfValue | None, number: bool) -> tuple[object, object] | None:
        if not isinstance(value, PdfArray) or len(value.items) != 2:
            return None
        low, high = value.items
        if number:
            if isinstance(low, PdfInt) and isinstance(high, PdfInt):
                return low.value, high.value
            return None
        if isinstance(low, PdfString) and isinstance(high, PdfString):
            return low.raw, high.raw
        return None

    def _order(self, entries: list[Entry], number: bool, root: int) -> None:
        """Keys unique and in order across the tree (*root*: its number,
        -1 when direct), by bytes (numbers: by value), and name keys as
        text too. Comparing and decoding pass over each key's bytes at most
        a few times: charged by their length."""
        keys = [e.key for e in entries]
        size = sum(len(k) if isinstance(k, bytes) else 1 for k in keys)
        if not self.charge(6 * size + 2 * len(keys) + 1):  # bytes, then two readings
            return
        if any(not a < b for a, b in zip(keys, keys[1:])):  # type: ignore[operator]
            self.flag(NameTreeRule.ORDER, root)
        if number:
            return
        for read in (key_text, qpdf_key_text):  # MuPDF's reading, then libqpdf's
            texts = [read(k) for k in keys if isinstance(k, bytes)]
            if any(t is None for t in texts) or any(
                    not a < b for a, b in zip(texts, texts[1:])):  # type: ignore[operator]
                self.flag(NameTreeRule.TEXT, root)
                return
