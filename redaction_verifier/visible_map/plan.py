"""redaction_verifier.visible_map.plan — what the map says.

Built from three inputs: the scan's own truth (the sink), the --json
report's findings and warnings, and the document (make_plan). Every
finding ends up drawn as a box, listed, or listed as "could not be
positioned"; every warning code either sets a flag or is an "outside"
code (headline.py). Nothing here writes a PDF.

Every string that comes from outside this module's own fixed wording — a
rule's label, a masked sample, the target's name, an object's role — goes
through Rules.clean (the map's checker) before it is kept, so it is
checked whole against every rule while it is still a whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import pymupdf as fitz

from redaction_verifier.matching import PatternRule, mask, normalize_string
from redaction_verifier.model import Secret
from redaction_verifier.report.text import _sanitize_report_text, rule_label
from redaction_verifier.rules import RuleLabel, RuleSet
from redaction_verifier.visible_map import headline, locate

MAX_BOXES_PER_PAGE = 200
MERGE_OVERLAP = 0.5

# Box styles, by how the content shows.
SOLID, HATCHED, DASHED, DOTTED = "solid", "hatched", "dashed", "dotted"
VISIBLE_STYLES = frozenset({SOLID, HATCHED})
# Boxes over an appearance that is itself what shows (the page, or print).
DRAWN_STYLES = frozenset({SOLID, HATCHED, DOTTED})
TOGGLED = "on hover (toggled view)"

# Listed sections, in the order the summary page shows them: (heading,
# the flag the section sets).
SECTIONS: dict[str, tuple[str, str]] = {
    "unpositioned": ("Could not be positioned (may be visible)", "I"),
    "not_scanned": ("Not scanned", "I"),
    "off_layer": ("On a switched-off layer", "I"),
    "layer_rule": ("Behind a layer rule (may be visible)", "I"),
    "ocr": ("Found by OCR, not positioned", "V"),
    "review": ("Possible (review)", "P"),
    "live": ("Live content, may be visible", "L"),
    "dict_copy": ("Also stored in a boxed annotation's or field's own objects", ""),
    "outside": ("Outside the crop, recoverable by uncropping", "X"),
    "panels": ("Panels", "X"),
    "scripts": ("When scripts run", "X"),
    "attachments": ("When an attachment is opened", "X"),
    "password": ("Stored; not shown by viewers that honour the password flag", "X"),
    "stored": ("Stored, not shown", ""),
    "not_visible": ("Not visible", ""),
}

# What the Hidden layer's item kinds are, when they are not on a page.
_PAGELESS: dict[str, tuple[str, str]] = {
    "attachment:filename": ("panels", "attachment name"),
    "attachment:ufilename": ("panels", "attachment name"),
    "attachment:description": ("panels", "attachment description"),
    "attachment:content": ("attachments", "embedded file"),
    "javascript": ("scripts", "JavaScript"),
    "layer:name": ("panels", "layer name"),
}
MARKUP_SUBTYPES = frozenset({
    "Text", "FreeText", "Line", "Square", "Circle", "Polygon", "PolyLine", "Highlight",
    "Underline", "Squiggly", "StrikeOut", "Stamp", "Caret", "Ink", "FileAttachment", "Sound",
    "Redact",
})
# Annotation flags (PDF /F): Hidden, Print, NoView, ToggleNoView.
_HIDDEN, _PRINT, _NO_VIEW, _TOGGLE_NO_VIEW = 2, 4, 32, 256
_LEGACY_KINDS = {"value": "value rule", "pattern": "pattern rule", "class": "built-in type rule"}
_ORDINAL = {1: "st", 2: "nd", 3: "rd"}


@dataclass
class Box:
    page: int
    rect: fitz.Rect
    style: str
    label: str              # checked variable text
    how: str                # fixed text
    approx: bool = False
    part: bool = False


@dataclass
class Listed:
    section: str
    page: int | None
    label: str              # checked variable text
    note: str = ""          # fixed text
    detail: str = ""        # checked variable text (an object's role)


@dataclass
class MapPlan:
    target_name: str
    target_sha256: str
    version: str
    exit_code: int
    json_unwritten: bool
    page_count: int
    page_sizes: dict[int, tuple[float, float]] = field(default_factory=dict)
    boxes: list[Box] = field(default_factory=list)
    listed: list[Listed] = field(default_factory=list)
    codes: list[tuple[str, int | None]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)   # why the map is incomplete (fixed)
    not_visible: dict[str, int] = field(default_factory=dict)
    off_layers: int = 0
    layer_rules: int = 0
    cropped: int = 0
    legend: list[str] = field(default_factory=list)    # legacy index labels, explained
    flags: headline.Flags = field(default_factory=headline.Flags)
    headline: str = ""
    row: int = 0

    @property
    def mapped_pages(self) -> list[int]:
        return sorted({b.page for b in self.boxes})


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else _ORDINAL.get(n % 10, 'th')}"


def page_ranges(pages: list[int]) -> str:
    """"pp. 2, 4-6" for [2, 4, 5, 6]."""
    pages = sorted(set(pages))
    runs: list[list[int]] = []
    for p in pages:
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    shown = ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)
    return f"p.{shown}" if len(pages) == 1 else f"pp. {shown}"


class Rules:
    """The rules as loaded in the parent, rebuilt from the header, with
    the map's label for each."""

    def __init__(self, data: dict[str, Any], clean: Callable[..., str]) -> None:
        from redaction_verifier.matching import BUILTIN_PATTERN_CLASSES
        self.secrets = {name: Secret(name, norm) for name, norm in data["secrets"]}
        self.patterns: dict[str, PatternRule] = {}
        for name, src, flags, cls in data["patterns"]:
            validator = BUILTIN_PATTERN_CLASSES[cls][1] if cls else None
            self.patterns[name] = PatternRule(name, re.compile(src, flags), validator)
        self.clean = clean
        self._labels: dict[str, str] = {}
        self._entry: dict[str, int] = {}
        self.legacy: dict[str, tuple[str, int, int]] = {}
        counts: dict[str, int] = {}
        shared = RuleSet(labels={name: RuleLabel(kind, text) for name, kind, text in data["labels"]
                                 if kind not in _LEGACY_KINDS})
        for entry, (name, kind, text) in enumerate(data["labels"], start=1):
            self._entry[name] = entry
            if kind in _LEGACY_KINDS:
                # A legacy JSON rule's name is the user's own words and
                # may be the secret itself: the map numbers it instead.
                counts[kind] = counts.get(kind, 0) + 1
                self.legacy[name] = (kind, counts[kind], entry)
                self._labels[name] = f"{_LEGACY_KINDS[kind]} #{counts[kind]}"
            else:
                self._labels[name] = rule_label(name, shared)
        self.shown_legacy: set[str] = set()
        self._by_shown = {_sanitize_report_text(n): n for n in [*self.secrets, *self.patterns]}

    def resolve(self, shown: str) -> str | None:
        """The rule a --json report name refers to."""
        if shown in self.secrets or shown in self.patterns:
            return shown
        return self._by_shown.get(shown)

    def label(self, name: str | None, sample: str = "", *, hide: bool = False) -> str:
        """The checked label for rule *name*, with the masked sample (only
        "****" when *hide*: the match lies on an exact value's glyphs)."""
        if name is None or name not in self._labels:
            return self.clean("an unlabelled rule")
        if name in self.legacy:
            self.shown_legacy.add(name)
        base = self._labels[name]
        entry = f"rule #{self._entry[name]}"
        if sample:
            masked = "****" if hide else self.masked(mask(sample), sample)
            return self.clean(f"{base} {masked}", f"{base} ****", f"{entry} ****")
        return self.clean(base, entry)

    def masked(self, shown: str, matched: str = "") -> str:
        """*shown* (mask()'s output), or a bare "****" when it could reveal
        part of an exact value: the match contains one, or any two
        consecutive revealed characters occur together in one (any longer
        shared run contains such a pair). A pattern that matches a value,
        or text that runs on past its start or end, would otherwise show
        some of the value's characters. Used for the map only: the human
        and --json reports keep mask() as it is."""
        values = [s.normalized for s in self.secrets.values()]
        if matched and any(v in normalize_string(matched) for v in values):
            return "****"
        tail = normalize_string(shown[4:])
        if len(tail) == 1 and any(tail in v for v in values):
            return "****"           # mask() of a short match shows one character
        if any(tail[i:i + 2] in v for i in range(len(tail) - 1) for v in values):
            return "****"
        return shown

    def legend(self) -> list[str]:
        out = []
        for name in sorted(self.shown_legacy, key=lambda n: self._entry[n]):
            kind, n, entry = self.legacy[name]
            noun = {"value": "value", "pattern": "pattern", "class": "built-in type"}[kind]
            out.append(f"{_LEGACY_KINDS[kind]} #{n} = {ordinal(n)} {noun} entry, "
                       f"rules-file entry {entry}")
        return out


def _roles(doc: fitz.Document) -> tuple[dict[int, str], dict[int, set[int]]]:
    """A plain role for objects a finding can name — what a page uses it
    for, or the document's own structures — and, for an annotation,
    widget or link and its appearance stream, the annotations it belongs
    to (one appearance can serve several widgets)."""
    roles: dict[int, str] = {}
    owners: dict[int, set[int]] = {}

    def put(xref: Any, role: str) -> None:
        if isinstance(xref, int) and xref > 0:
            roles.setdefault(xref, role)

    try:
        put(doc.pdf_catalog(), "document catalog")
        kind, value = doc.xref_get_key(-1, "Info")
        if kind == "xref":
            put(int(value.split()[0]), "document information")
    except Exception:
        pass
    for index in range(doc.page_count):
        n = index + 1
        try:
            page = doc[index]
            put(page.xref, f"page {n}")
            for x in page.get_contents():
                put(x, f"content p.{n}")
            for a in [*page.annots(), *page.widgets()]:
                put(a.xref, f"{'form field' if a.__class__.__name__ == 'Widget' else 'annotation'}"
                    f" p.{n}")
                owners.setdefault(a.xref, set()).add(a.xref)
                kind, value = doc.xref_get_key(a.xref, "AP/N")
                if kind == "xref":
                    put(int(value.split()[0]), f"appearance of an annotation p.{n}")
                    owners.setdefault(int(value.split()[0]), set()).add(a.xref)
            for link in page.get_links():
                put(link.get("xref"), f"link p.{n}")
                if isinstance(link.get("xref"), int):
                    owners.setdefault(link["xref"], set()).add(link["xref"])
            for img in page.get_images(full=True):
                put(img[0], f"image p.{n}")
            for xo in page.get_xobjects():
                put(xo[0], f"form XObject p.{n}")
            for font in page.get_fonts(full=True):
                put(font[0], f"font p.{n}")
        except Exception:
            continue
    return roles, owners


class Planner:
    def __init__(self, doc: fitz.Document, header: dict[str, Any], rules: Rules) -> None:
        self.doc = doc
        self.header = header
        self.rules = rules
        self.clean = rules.clean
        clean = self.clean
        self.sink = header["sink"]
        self.report = header["report"]
        self.plan = MapPlan(
            target_name=clean(header["target_name"], "target"),
            target_sha256=header["target_sha256"],
            version=header["version"],
            exit_code=header["exit_code"],
            json_unwritten=header["exit_code"] != header["scan_code"],
            page_count=doc.page_count,
        )
        self._texts: dict[int, locate.PageText | None] = {}
        self._roles: dict[int, str] | None = None
        self._owners: dict[int, set[int]] = {}
        self._facts: dict[tuple[int, Any], locate.AnnotFacts | None] = {}
        self.located_text: set[tuple[str, int | None]] = set()
        self.located_ocr: set[str] = set()
        self.located_hidden: set[tuple[str, str]] = set()
        self.off: set[int] = set()
        self._items: dict[int, locate.PageItems] = {}
        self._values_at: dict[int, set[int]] = {}
        # (xref, rule) pairs a hidden item was boxed for -- shown on the
        # page, on hover, when printed or opened. A live-object finding in
        # that annotation or field's own objects then adds nothing a reader
        # could not already see. Stored, hidden and password items are not
        # in here: the Objects layer may have matched a key the Hidden layer
        # never reads (a tooltip, say), which a reader can see.
        self.shown: set[tuple[int, str]] = set()
        # The subset whose box is the appearance being shown: only these
        # absorb a copy in the appearance stream.
        self.drawn: set[tuple[int, str]] = set()

    # ── helpers ───────────────────────────────────────────────────────
    def list(self, section: str, page: int | None, label: str, note: str = "",
             detail: str = "") -> None:
        self.plan.listed.append(Listed(section, page, label, note, detail))

    def _value_glyphs(self, page_no: int, pt: locate.PageText) -> set[int]:
        """Every glyph of every exact value's occurrence on the page."""
        if page_no not in self._values_at:
            self._values_at[page_no] = {g for secret in self.rules.secrets.values()
                                        for hit in locate.value_hits(pt, page_no, secret)
                                        for g in hit.glyphs}
        return self._values_at[page_no]

    def page_text(self, page_no: int) -> locate.PageText | None:
        if page_no not in self._texts:
            if len(self._texts) > 2:        # pages are visited in order; keep the last few
                self._texts.pop(min(self._texts))
            try:
                self._texts[page_no] = locate.page_text(self.doc[page_no - 1])
            except Exception:
                self._texts[page_no] = None
        return self._texts[page_no]

    def _load_roles(self) -> dict[int, str]:
        if self._roles is None:
            try:
                self._roles, self._owners = _roles(self.doc)
            except Exception:
                self._roles, self._owners = {}, {}
        return self._roles

    def owners(self, xref: Any) -> set[int]:
        """The annotations or widgets an object belongs to (itself, or
        those whose appearance it is); empty for anything else."""
        self._load_roles()
        return self._owners.get(xref, set()) if isinstance(xref, int) else set()

    def is_appearance(self, xref: Any) -> bool:
        """*xref* is an appearance stream, not an annotation's own
        dictionary."""
        return isinstance(xref, int) and xref not in self.owners(xref)

    def role(self, xref: Any) -> str:
        if self._roles is None:
            self._load_roles()
        assert self._roles is not None
        if not isinstance(xref, int):
            return "object"
        return self.clean(f"object {xref}, {self._roles.get(xref, 'other object')}",
                          f"object {xref}", "an object")

    def add_box(self, page_no: int, rect: fitz.Rect, style: str, label: str, how: str,
                **kw: Any) -> None:
        self.plan.boxes.append(Box(page_no, rect, style, label, how, **kw))

    # ── the Text and OCR layers ───────────────────────────────────────
    def text_layer(self) -> None:
        truth = {int(p): t for p, t in self.sink["truth"].get("Text", {}).items()}
        scanned = set(self.sink["scanned"].get("Text", []))
        missing = [p for p in range(1, self.plan.page_count + 1) if p not in scanned]
        if missing:
            self.list("not_scanned", None, self.clean("Text layer"), page_ranges(missing))
        for page_no in sorted(truth):
            entry = truth[page_no]
            for name in entry["hard"]:
                self._place_text(page_no, name, value=True)
            for name in entry["pattern_hard"]:
                self._place_text(page_no, name, value=False)
            for name in entry["seam"]:
                self._place_seam(page_no, name)
        # What the --json report's Text findings are checked against: a
        # value found on one page by (rule, page), anything else by rule.
        self.located_text = {(n, p) for p, t in truth.items()
                             for n in (*t["hard"], *t["pattern_hard"], *t["seam"])}
        self.located_text |= {(n, None) for n, _ in self.located_text}

    def _place_text(self, page_no: int, name: str, *, value: bool) -> None:
        pt = self.page_text(page_no)
        rule_hits: list[locate.TextHit] = []
        if pt is not None:
            if value and name in self.rules.secrets:
                rule_hits = locate.value_hits(pt, page_no, self.rules.secrets[name])
            elif not value and name in self.rules.patterns:
                rule_hits = locate.pattern_hits(pt, page_no, self.rules.patterns[name])
        # Each occurrence is accounted for on its own: one that cannot be
        # boxed is listed even when another copy on the page was.
        unplaced = not rule_hits
        page = self.doc[page_no - 1]
        for hit in rule_hits:
            # A pattern match on any glyph of an exact value shows no tail:
            # mask() would reveal some of that value's characters.
            hide = bool(hit.sample) and pt is not None and bool(
                set(hit.glyphs) & self._value_glyphs(page_no, pt))
            label = self.rules.label(name, hit.sample, hide=hide)
            assert pt is not None
            split = locate.split_visible(pt, hit)
            if split is None:
                unplaced = True
                continue
            # A hit wholly off the page is listed; one running past the
            # visible edge (readings 4-6) is boxed where it shows and its
            # off-page part listed.
            if split.off_page:
                self.list("outside", page_no, label,
                          "the part past the visible edge" if split.visible else "")
            if not split.visible:
                continue
            if not split.needle:
                continue        # only a separator shows: nothing of the value to box
            box = locate.text_box(page, pt, split.visible)
            if box is not None and locate.verify_text_box(page, pt, box, split.needle):
                if split.off_page:
                    self.add_box(page_no, box, SOLID, label,
                                 "part of a value running past the visible edge, "
                                 "selectable text", part=True)
                else:
                    self.add_box(page_no, box, SOLID, label, "selectable text",
                                 approx=hit.approx)
            else:
                unplaced = True
        if unplaced:
            self.list("unpositioned", page_no, self.rules.label(name), "Text layer")

    def _place_seam(self, page_no: int, name: str) -> None:
        prev, cur = self.page_text(page_no - 1), self.page_text(page_no)
        secret = self.rules.secrets.get(name)
        hits = locate.seam_hits(prev, cur, page_no, secret) if prev and cur and secret else []
        boxed: list[tuple[int, fitz.Rect]] = []
        outside: list[int] = []
        placed = 0
        for hit in hits:
            pt = prev if hit.page == page_no - 1 else cur
            assert pt is not None
            split = locate.split_visible(pt, hit)
            if split is None:
                break
            if split.off_page:
                outside.append(hit.page)
            if split.visible and split.needle:
                page = self.doc[hit.page - 1]
                box = locate.text_box(page, pt, split.visible)
                if box is None or not locate.verify_text_box(page, pt, box, split.needle):
                    break
                boxed.append((hit.page, box))
            placed += 1
        where = f"across pages {page_no - 1}-{page_no}"
        if placed == 2:
            for p, box in boxed:
                self.add_box(p, box, SOLID, self.rules.label(name),
                             f"part of a value {where}, selectable text", part=True)
            for p in outside:
                self.list("outside", p, self.rules.label(name),
                          f"part of a value {where}, past the visible edge")
        else:
            self.list("unpositioned", page_no, self.rules.label(name), f"Text layer, {where}")

    def ocr_layer(self) -> None:
        truth = {int(p): t for p, t in self.sink["truth"].get("OCR", {}).items()}
        for page_no in sorted(truth):
            entry = truth[page_no]
            for name in sorted({*entry["hard"], *entry["pattern_hard"], *entry["seam"]}):
                self.list("ocr", page_no, self.rules.label(name))
                self.located_ocr.add(name)

    # ── the Hidden layer ──────────────────────────────────────────────
    def hidden_layer(self) -> None:
        for entry in self.sink["hidden"]:
            for name in entry["rules"]:
                self.located_hidden.add((entry["location"], name))
            try:
                self._classify(entry)
            except Exception:
                for name in entry["rules"]:
                    self.list("unpositioned", entry["page"], self.rules.label(name),
                              "could not be classified")

    def _classify(self, entry: dict[str, Any]) -> None:
        kind, page_no, names = entry["kind"] or "", entry["page"], entry["rules"]
        if page_no is None or kind in _PAGELESS:
            section, what = _PAGELESS.get(kind, ("unpositioned", "not on a page"))
            for name in names:
                self.list(section, page_no, self.rules.label(name), what)
            return
        if kind == "field:name":
            for name in names:
                self.list("panels", page_no, self.rules.label(name), "form field name")
            return
        if page_no not in self._items:
            self._items[page_no] = locate.PageItems(self.doc[page_no - 1])
        items = self._items[page_no]
        page = items.page
        xref = entry["xref"]
        key = (page_no, entry["location"] if kind.startswith("link:") else xref)
        if key not in self._facts:
            if kind.startswith("link:"):
                m = re.match(r"link #(\d+) ", entry["location"])
                self._facts[key] = locate.link_facts(self.doc, page, xref,
                                                     int(m.group(1)) if m else None, self.off)
            elif isinstance(xref, int):
                self._facts[key] = locate.annotation_facts(self.doc, items, xref, self.off)
            else:
                self._facts[key] = None
        facts = self._facts[key]
        for name in names:
            self._classify_one(page, page_no, kind, name, facts, xref)

    def _clip_shows(self, page: fitz.Page, rect: Any, name: str) -> bool:
        """Row 4's test: the page text clipped to the raw rectangle holds
        the rule's match."""
        text = page.get_text("text", clip=fitz.Rect(rect))
        if name in self.rules.secrets:
            return self.rules.secrets[name].normalized in locate.normalize_string(text)
        rule = self.rules.patterns.get(name)
        if rule is None:
            return False
        from redaction_verifier.matching import match_patterns
        return bool(match_patterns(text, [rule]))

    def _classify_one(self, page: fitz.Page, page_no: int, kind: str, name: str,
                      facts: locate.AnnotFacts | None, xref: Any) -> None:
        label = self.rules.label(name)
        if facts is None:
            self.list("unpositioned", page_no, label, "annotation not found on its page")
            return
        flags = facts.flags
        hidden = bool(flags & _HIDDEN) or (bool(flags & _NO_VIEW) and not flags & _PRINT)
        if facts.oc_kind == "OCG" and facts.oc_on is False:
            # Row 1: on a switched-off layer.
            note = "may print" if facts.oc_may_print else ""
            self.list("off_layer", page_no, label, note)
            return
        rect = facts.rect
        box = locate.annot_box(page, rect, displayed=facts.displayed)
        core_ok = box is not None and isinstance(xref, int) and xref > 0 and (
            locate.verify_annot_box(page, self.doc, xref, box))
        if flags & _NO_VIEW and flags & _TOGGLE_NO_VIEW and not flags & _HIDDEN:
            # NoView toggled by the pointer: shown while hovered.
            self._annot_box(page_no, box, core_ok, DASHED, label, TOGGLED,
                            xref=xref, name=name)
            return
        if hidden:
            # Row 2: hidden flag, or NoView without Print.
            self.plan.not_visible["hidden annotation"] = (
                self.plan.not_visible.get("hidden annotation", 0) + 1)
            return
        if flags & _NO_VIEW and flags & _PRINT:
            # Row 3: NoView + Print.
            self._annot_box(page_no, box, core_ok, DOTTED, label, "when printed", xref=xref,
                            name=name)
            return
        unrotated = None if box is None else locate.to_glyph_frame(page, box)
        shows = unrotated is not None and self._clip_shows(page, unrotated, name)
        # An open note shows its /Contents, not its author, subject or file.
        opened = (facts.popup_open or facts.own_open) and kind == "annotation:content"
        if shows or opened:
            # Row 4: on the page (its appearance, or an open note).
            how = "on page" if shows else "on page, open note"
            self._annot_box(page_no, box, core_ok, SOLID, label, how, xref=xref, name=name)
            return
        if facts.oc_kind in ("OCMD", "?"):
            # Row 5: a visibility expression the map does not evaluate.
            self.list("layer_rule", page_no, label)
            return
        if facts.is_widget and kind == "field:value":
            password = bool(facts.field_flags & locate.PASSWORD)
            ftype = facts.field_type
            if (ftype == locate.TEXT and not password) or ftype == locate.COMBOBOX:
                # Row 6: a viewer that redraws form fields shows /V.
                self._annot_box(page_no, box, core_ok, HATCHED, label,
                                "on page in viewers that redraw form fields", xref=xref, name=name)
                return
            if ftype == locate.LISTBOX and facts.opt_plain:
                self._annot_box(page_no, box, core_ok, DASHED, label,
                                "in the field's list, on click or scroll", xref=xref, name=name)
                return
            if ftype in (locate.CHECKBOX, locate.RADIOBUTTON):
                # Row 8: an export value.
                self.list("stored", page_no, label, "checkbox or radio export value")
                return
            if ftype == locate.TEXT and password:
                self.list("password", page_no, label)
                return
        elif kind.startswith("annotation:") and facts.subtype in MARKUP_SUBTYPES:
            # Row 7.
            how = ("when opened" if kind == "annotation:file"
                   else "on hover / in the comments panel")
            self._annot_box(page_no, box, core_ok, DASHED, label, how, xref=xref, name=name)
            return
        elif kind.startswith("link:"):
            self._annot_box(page_no, box, core_ok, DASHED, label, "on hover", xref=xref,
                            name=name)
            return
        self.list("unpositioned", page_no, label, "not classified")

    def _annot_box(self, page_no: int, box: fitz.Rect | None, ok: bool, style: str, label: str,
                   how: str, *, xref: Any, name: str) -> None:
        if box is None or not ok:
            self.list("unpositioned", page_no, label, how)
            return
        self.add_box(page_no, locate.min_size(box), style, label, how)
        self.shown.add((xref, name))
        if style in DRAWN_STYLES or how == TOGGLED:
            # The box is where the appearance itself shows: on the page,
            # when printed, or while hovered. A dashed box is not -- its
            # annotation's appearance is drawn, but the box is about text
            # shown on hover or click, so an appearance holding the value
            # (in glyphs a font maps) stays live content.
            self.drawn.add((xref, name))

    # ── everything the --json report found ───────────────────────────
    def findings(self) -> None:
        for f in self.report.get("findings", []):
            name = self.rules.resolve(f.get("rule", ""))
            layer, page = f.get("layer"), f.get("page")
            sample = f.get("sample") or ""
            if layer == "Text":
                if (name, page) not in self.located_text:
                    self.list("unpositioned", page, self.rules.label(name), "Text layer")
            elif layer == "OCR":
                if name not in self.located_ocr:
                    self.list("ocr", page, self.rules.label(name))
            elif layer == "Hidden":
                if (f.get("location"), name) not in self.located_hidden:
                    self.list("unpositioned", page, self.rules.label(name), "Hidden layer")
            elif layer == "Metadata":
                self.list("panels", None, self._shown_label(name, sample), "document properties")
            elif layer in ("Objects", "Binary"):
                storage = f.get("storage")
                if storage in headline.OUTSIDE_STORAGE:
                    self.plan.not_visible[storage] = self.plan.not_visible.get(storage, 0) + 1
                else:
                    owners = self.owners(f.get("object"))
                    held = self.drawn if self.is_appearance(f.get("object")) else self.shown
                    shown = bool(owners) and all((o, name) in held for o in owners)
                    section = "dict_copy" if name is not None and shown else "live"
                    self.list(section, None, self._shown_label(name, sample),
                              detail=self.role(f.get("object")))
            else:
                self.list("unpositioned", page, self.rules.label(name), "unknown layer")

    def _shown_label(self, name: str | None, masked: str) -> str:
        """A label for a --json finding, whose sample is already masked."""
        base = self.rules.label(name)
        if masked and re.fullmatch(r"\*{4}.{0,4}", masked):
            return self.clean(f"{base} {self.rules.masked(masked)}", f"{base} ****")
        return base

    def warnings(self) -> None:
        soft = {layer: {int(p): t["pattern_soft"] for p, t in pages.items()}
                for layer, pages in self.sink["truth"].items()}
        for w in self.report.get("warnings", []):
            code, storage, page = w.get("code", ""), w.get("storage"), w.get("page")
            self.plan.codes.append((code, page))
            level = headline.code_level(code, storage)
            name = self.rules.resolve(w["rule"]) if w.get("rule") else None
            if level == "review":
                pages: list[int | None] = [page]
                if code == "REVIEW_FUSED_PATTERN" and page is None:
                    hits = [p for p, names in soft.get(w.get("layer"), {}).items()
                            if name in names]
                    pages = list(sorted(hits)) or [None]
                for p in pages:
                    self.list("review", p, self.rules.label(name), w.get("layer") or "")
            elif level == "live":
                self.list("live", None, self.rules.label(name), w.get("code", ""),
                          detail=self.role(w.get("object")))
            elif level == "outside":
                key = storage or "stored"
                self.plan.not_visible[key] = self.plan.not_visible.get(key, 0) + 1

    # ── the whole plan ────────────────────────────────────────────────
    def build(self) -> MapPlan:
        plan = self.plan
        for index in range(self.doc.page_count):
            r = self.doc[index].rect
            plan.page_sizes[index + 1] = (r.width, r.height)
        try:
            self.off = locate.switched_off(self.doc)
        except Exception:
            plan.reasons.append("optional-content groups unreadable")
        plan.off_layers = len(self.off)
        try:
            plan.layer_rules = locate.layer_rules(self.doc)
            plan.cropped = sum(1 for i in range(self.doc.page_count)
                               if locate.outside_crop(self.doc[i]))
        except Exception:
            plan.reasons.append("layer rules or page boxes unreadable")
        self.text_layer()
        self.ocr_layer()
        self.hidden_layer()
        self.findings()
        self.warnings()
        self.finish()
        return plan

    def _row_2_detail(self) -> str:
        """Row 2's reasons, and how many items show on click, hover,
        print or open, in panels or when uncropped."""
        plan = self.plan
        reasons = []
        if plan.flags.I:
            n = len(plan.reasons) + _flagged(plan, "I")
            reasons.append(f"{n} incomplete")
        if plan.flags.P:
            reasons.append(f"{_flagged(plan, 'P')} possible")
        if plan.flags.L:
            reasons.append(f"{_flagged(plan, 'L')} in live content")
        shown = _flagged(plan, "X") + sum(1 for b in plan.boxes if b.style not in VISIBLE_STYLES)
        if shown:
            return (f"{', '.join(reasons)}; {shown} shown on click / hover / print / open / in "
                    "panels / when uncropped")
        return ", ".join(reasons)

    def finish(self) -> None:
        plan = self.plan
        plan.boxes = _merge(plan.boxes)
        kept: list[Box] = []
        for page_no in plan.mapped_pages:
            on_page = [b for b in plan.boxes if b.page == page_no]
            if len(on_page) > MAX_BOXES_PER_PAGE:
                plan.reasons.append(f"box cap reached on p.{page_no} "
                                    f"({len(on_page) - MAX_BOXES_PER_PAGE} not drawn)")
                on_page = on_page[:MAX_BOXES_PER_PAGE]
            kept.extend(on_page)
        plan.boxes = kept
        if self.sink.get("failed"):
            plan.reasons.append("the scan's hand-over to the map failed")
        if plan.off_layers:
            plan.reasons.append(
                f"{plan.off_layers} switched-off layer{'s' if plan.off_layers != 1 else ''}: "
                "content on them not fully checked (images, font-coded text)")
        if plan.layer_rules:
            plan.reasons.append(
                f"{plan.layer_rules} layer rule{'s' if plan.layer_rules != 1 else ''}: content "
                "behind them not fully checked (images, font-coded text)")
        if plan.cropped:
            plan.reasons.append(
                f"{plan.cropped} page{'s have' if plan.cropped != 1 else ' has'} area outside "
                "the crop; images there not checked")
        warns = self.report.get("warnings", [])
        incomplete: dict[str, set[int]] = {}
        for w in warns:
            code = w.get("code", "")
            if headline.code_level(code, w.get("storage")) in ("page", "document", "scope"):
                pages = incomplete.setdefault(code, set())
                if w.get("page") is not None:
                    pages.add(w["page"])
        for code in sorted(incomplete):
            pages = incomplete[code]
            plan.reasons.append(f"{code} {page_ranges(sorted(pages))}" if pages else code)
        plan.listed = [Listed(*key) for key in dict.fromkeys(
            (i.section, i.page, i.label, i.note, i.detail) for i in plan.listed)]
        sections = {item.section for item in plan.listed}
        styles = {b.style for b in plan.boxes}
        flag = {name: False for name in "VIPLX"}
        flag["V"] = bool(styles & VISIBLE_STYLES)
        flag["X"] = bool(styles - VISIBLE_STYLES)
        for section in sections:
            mark = SECTIONS[section][1]
            if mark:
                flag[mark] = True
        if plan.reasons:
            flag["I"] = True
        plan.flags = headline.Flags(
            V=flag["V"], I=flag["I"], P=flag["P"], L=flag["L"], X=flag["X"],
            ocr_only=flag["V"] and not styles & VISIBLE_STYLES)
        codes = [c for c, _ in plan.codes]
        outside_only = bool(warns) and all(
            headline.code_level(w.get("code", ""), w.get("storage")) == "outside" for w in warns)
        plan.row = headline.row(plan.flags, exit_code=plan.exit_code,
                                json_unwritten=plan.json_unwritten, codes=codes,
                                outside_only=outside_only)
        plan.headline = headline.text(plan.flags, exit_code=plan.exit_code,
                                      json_unwritten=plan.json_unwritten, codes=codes,
                                      outside_only=outside_only, detail=self._row_2_detail())
        plan.legend = self.rules.legend()


def _flagged(plan: MapPlan, flag: str) -> int:
    return sum(1 for i in plan.listed if SECTIONS[i.section][1] == flag)


def _overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    common = a & b
    if common.is_empty:
        return 0.0
    smaller = min(a.width * a.height, b.width * b.height)
    return (common.width * common.height) / smaller if smaller > 0 else 0.0


def _merge(boxes: list[Box]) -> list[Box]:
    """Same-label boxes on a page that overlap by at least half of the
    smaller one become one box."""
    out: list[Box] = []
    for box in boxes:
        for kept in out:
            if (kept.page == box.page and kept.label == box.label and kept.style == box.style
                    and kept.how == box.how and _overlap(kept.rect, box.rect) >= MERGE_OVERLAP):
                kept.rect = kept.rect | box.rect
                kept.approx = kept.approx or box.approx
                break
        else:
            out.append(box)
    return out



# ── Entry points ──────────────────────────────────────────────────────
def rules_header(rules: Any) -> dict[str, Any]:
    """The rules as loaded (a RuleSet), in a form Rules rebuilds exactly:
    each built-in class by name (its validator is code), each regex by
    source and flags, labels in the rules file's own order."""
    from redaction_verifier.matching import BUILTIN_PATTERN_CLASSES

    patterns = []
    for rule in rules.patterns:
        cls = None
        if rule.validator is not None:
            matches = [name for name, (src, validator) in BUILTIN_PATTERN_CLASSES.items()
                       if validator is rule.validator and src == rule.regex.pattern]
            if not matches:
                raise ValueError("a pattern rule with an unknown validator")
            cls = matches[0]
        patterns.append([rule.name, rule.regex.pattern, rule.regex.flags, cls])
    return {
        "secrets": [[s.name, s.normalized] for s in rules.secrets],
        "patterns": patterns,
        "labels": [[name, label.kind, label.text] for name, label in rules.labels.items()],
    }


def make_plan(data: bytes | memoryview, target_sha256: str, header: dict[str, Any],
              rules: Rules) -> MapPlan:
    """The plan for the document *data*, from *header*: the scan's truth
    (sink.MapSink.to_header), the --json report's content, the exit code,
    the scan's own code, the target's name and the version. *rules* is
    Rules rebuilt from the header's rules_header, with the checker its
    labels go through."""
    fitz.TOOLS.mupdf_display_errors(False)
    doc = fitz.open("pdf", data)
    if doc.needs_pass:
        raise ValueError("the target needs a password")
    return Planner(doc, dict(header, target_sha256=target_sha256), rules).build()
