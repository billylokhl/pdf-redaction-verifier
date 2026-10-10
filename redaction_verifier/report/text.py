"""redaction_verifier.report.text — the human-readable console report.

Moved verbatim from verify.py as Phase 2, step 2 ("Move") of
docs/REDESIGN.md §4/§6 ("report/ human, JSON"; "Move, don't wrap"), then
rewritten in plain language at the owner's request: every finding says,
explicitly, that a secret is STILL IN THE FILE (a problem after
redaction) and what kind of problem it is, and a "What this means"
section explains, once each, every kind of finding that occurs. The
verdict, findings, warnings and the --json report are unchanged; the
report reveals no more of any secret than before (values never, pattern
matches only through mask()).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from redaction_verifier.matching import mask
from redaction_verifier.model import Finding, ScanReport

if TYPE_CHECKING:
    from redaction_verifier.rules import RuleSet

_BAR = "=" * 70
_NAME_CAP = 40  # characters of a user-chosen rule name shown, after sanitizing


def _sanitize_report_text(text: str) -> str:
    """Strip control characters so PDF-derived bytes (ANSI escapes,
    newlines) cannot inject into or spoof the terminal report."""
    return "".join(ch if ch.isprintable() or ch == " " else "�" for ch in text)


def _wrap(text: str, indent: str, width: int = 78) -> list[str]:
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


# ── Where a finding is: a short tag on its line, and an explanation ────────
# Hidden-layer locations start with one of these prefixes (verify.py).
_HIDDEN_PREFIXES = (
    ("annotation", "comment or note"),
    ("form field", "form field"),
    ("link #", "link"),
    ("embedded file", "attachment"),
    ("JavaScript", "script"),
    ("optional-content group", "layer name"),
)
_OBJECT_TAGS = {"live": "stored", "orphaned": "left over", "unreferenced": "left over?",
                "superseded": "earlier version"}


def _where(finding: Finding) -> tuple[str, str]:
    """(the tag shown on the finding's line, the key of its explanation)."""
    layer = finding.layer
    if layer in ("Text", "OCR"):
        return "on a page", layer
    if layer == "Objects":
        tag = _OBJECT_TAGS.get(finding.storage, finding.storage)
        if finding.storage == "orphaned" and "(XMP metadata)" in finding.location:
            return tag, "Objects:xmp-orphaned"
        return tag, f"Objects:{finding.storage}"
    if layer == "Metadata":
        return "properties", "Metadata"
    if layer == "Hidden":
        for prefix, tag in _HIDDEN_PREFIXES:
            if finding.location.startswith(prefix):
                return tag, f"Hidden:{tag}"
        return "other", "Hidden:other"
    return finding.storage, f"{layer}:{finding.storage}"


# The explanation of each kind of place, in the order they are shown.
_WHERE_EXPLAINED: dict[str, tuple[str, str]] = {
    "Text": ("Text, on a page", "IN THE PAGE'S TEXT. The page(s) shown contain it as real text that "
             "can be selected, copied or searched -- even if it is covered by a box, coloured "
             "white, or outside the page edge."),
    "OCR": ("OCR, on a page", "VISIBLE ON THE PAGE. The page(s) shown display it when viewed or "
            "printed (it was read from the page the way a person sees it)."),
    "Objects:live": ("Objects, stored", "STORED IN THE FILE'S OBJECTS. Often the page's own drawing "
                     "instructions (so it may also be on the page), the document information, or a "
                     "form or annotation. Any PDF tool can extract it."),
    "Objects:orphaned": ("Objects, left over (ORPHANED object)", "HIDDEN, BUT NOT REMOVED. No page "
                         "shows it, but it is still stored inside the file, where PDF tools can "
                         "extract it. Usual cause, for page content: the page was covered or "
                         "replaced, but the original was kept in the file. Usual fix: save the "
                         "file with \"garbage collection\" (which deletes unused objects), then "
                         "run this check again."),
    "Objects:xmp-orphaned": ("Objects, left over (ORPHANED object ... XMP metadata)", "HIDDEN, BUT "
                             "NOT REMOVED. An older copy of the document's metadata was left in "
                             "the file; nothing shows it, but PDF tools can extract it. Usual "
                             "fix: save the file with \"garbage collection\", then run this "
                             "check again."),
    "Objects:unreferenced": ("Objects, left over? (object)", "PROBABLY HIDDEN, BUT NOT REMOVED. As "
                             "for a left-over object, except this tool could not confirm that "
                             "nothing uses it."),
    "Objects:superseded": ("Objects, earlier version", "IN AN OLDER VERSION OF THE FILE. The file "
                           "was edited and saved on top of itself; the earlier version, with the "
                           "secret, is still inside and can be restored. Usual fix: save a full "
                           "new copy (not an incremental save), then run this check again."),
    "Metadata": ("Metadata, properties", "IN THE DOCUMENT'S PROPERTIES (title, author, keywords, "
                 "XMP or other embedded metadata). Document-information panels or metadata tools "
                 "show it."),
    "Hidden:comment or note": ("Hidden, comment or note", "IN A COMMENT OR NOTE on the page "
                               "shown. Some are drawn on the page; others appear when clicked, "
                               "hovered, or in the comments panel."),
    "Hidden:form field": ("Hidden, form field", "IN A FORM FIELD on the page shown: its "
                          "value (usually drawn on the page, and shown in the form panel) or "
                          "its name (not drawn, but any PDF tool can read it)."),
    "Hidden:link": ("Hidden, link", "IN A LINK on the page shown: its address or action."),
    "Hidden:attachment": ("Hidden, attachment", "IN AN ATTACHMENT'S NAME OR DESCRIPTION, shown in "
                          "the attachments panel."),
    "Hidden:script": ("Hidden, script", "IN THE DOCUMENT'S JAVASCRIPT, which a viewer can run and "
                      "any PDF tool can read."),
    "Hidden:layer name": ("Hidden, layer name", "IN A LAYER NAME, shown in the layers panel."),
    "Hidden:other": ("Hidden, other", "IN A PART OF THE FILE THAT IS NOT PAGE TEXT; PDF tools can "
                     "read it."),
}


# ── Which rule matched: the plain label, and how the kinds are named ───────
_KIND_NAMES = {"exact_values": "exact values", "patterns": "patterns",
               "entity_types": "built-in types", "value": "values", "pattern": "patterns",
               "class": "built-in types"}
_KIND_ORDER = ("exact_values", "value", "patterns", "pattern", "entity_types", "class")


def _label(finding: Finding, rules: RuleSet | None) -> str:
    label = rules.labels.get(finding.secret_name) if rules is not None else None
    text = repr(finding.secret_name) if label is None else label.text
    # "|" separates the columns; a rule name must not be able to fake one.
    text = _sanitize_report_text(text).replace("|", "¦")
    if len(text) > _NAME_CAP:
        text = text[:_NAME_CAP - 1] + "…"
    if finding.sample:
        text += f" {mask(finding.sample)}"
    return _sanitize_report_text(text)


def _counts(report: ScanReport, rules: RuleSet) -> str:
    totals: dict[str, int] = {}
    for label in rules.labels.values():
        totals[label.kind] = totals.get(label.kind, 0) + 1
    found: dict[str, set[str]] = {}
    for finding in report.findings:
        hit = rules.labels.get(finding.secret_name)
        if hit is not None:
            found.setdefault(hit.kind, set()).add(finding.secret_name)
    parts = []
    for kind in _KIND_ORDER:
        if kind not in totals:
            continue
        n, total = len(found.get(kind, ())), totals[kind]
        if kind == "entity_types" and rules.entity_types_by_default:
            parts.append(f"{n} of the {total} built-in types checked by default")
        elif kind in ("entity_types", "class"):
            parts.append(f"{n} of your {total} built-in types")
        else:
            parts.append(f"{n} of your {total} {_KIND_NAMES[kind]}")
    return "Found: " + ", ".join(parts) + "."


def _found_explained(report: ScanReport, rules: RuleSet | None) -> list[str]:
    if rules is None:
        return []
    kinds = {rules.labels[f.secret_name].kind for f in report.findings
             if f.secret_name in rules.labels}
    out = []
    if "exact_values" in kinds:
        out.append(("exact value #N", "The Nth item under exact_values in your secrets file, "
                    "counting from 1. In brackets: the same item as warnings and the --json "
                    "report name it (counting from 0). Its characters are never shown."))
    if "value" in kinds:
        out.append(("'name' (a value)", "A value from your rules file, by the name you gave it. "
                    "Its characters are never shown."))
    if "patterns" in kinds:
        out.append(("pattern #N", "Text matching the Nth item under patterns in your secrets "
                    "file, counting from 1 (in brackets: counting from 0, as in warnings). Only "
                    "the last characters of the matched text are shown."))
    if "pattern" in kinds:
        out.append(("'name' (a pattern)", "Text matching a pattern from your rules file, by the "
                    "name you gave it. Only the last characters of the match are shown."))
    if "entity_types" in kinds:
        why = ("They are checked by default because your secrets file does not list "
               "entity_types." if rules.entity_types_by_default
               else "Your secrets file lists them under entity_types.")
        out.append(("built-in type", "Text that looks like the kind of data named (for "
                    "example an SSN or an email address). " + why + " Only the last "
                    "characters of the match are shown."))
    if "class" in kinds:
        out.append(("'name' (a built-in type)", "Text that looks like the kind of data the "
                    "rule's class names (for example an SSN). Only the last characters of the "
                    "match are shown."))
    lines = ["", "  What was found"]
    for head, body in out:
        lines.append(f"    {head}")
        lines += _wrap(body, "        ")
    return lines


def print_report(report: ScanReport, pdf_path: Path, *, rules: RuleSet | None = None,
                 json_unwritten: bool = False) -> None:
    """Render the verdict banner, the findings and what they mean.

    This is the single choke point where pattern samples are masked and
    all document- and rules-derived text is sanitized. *json_unwritten*:
    the --json report could not be written, so a clean run is NOT
    certified (exit 2).
    """
    name = _sanitize_report_text(pdf_path.name)
    print(f"\n{_BAR}")
    if report.leaked:
        print(f"  [FAIL]  SECRETS ARE STILL IN THIS FILE: {name}")
        if rules is not None:
            for line in _wrap(_counts(report, rules), "          "):
                print(_sanitize_report_text(line))
        n = len(report.findings)
        for line in _wrap(
                f"({n} finding{'s' if n != 1 else ''} -- one spot can be found by several "
                "checks.) If this is a redacted copy, the redaction did not remove them. Do not "
                "share this file until each one is removed or checked.", "          "):
            print(line)
        if any(getattr(w, "code", "") == "SKIPPED_FAIL_FAST" for w in report.warnings):
            print("          Later checks were skipped (--fail-fast); there may be more.")
        print(_BAR)
        for f in report.findings:
            tag, _ = _where(f)
            line = (f"  ✖ STILL PRESENT | {_label(f, rules):<36} | {f.layer:<8} | {tag:<15} | "
                    f"{f.location}")
            print(_sanitize_report_text(line))
        _print_meaning(report, rules)
    elif report.degraded or json_unwritten:
        print(f"  [NOT CERTIFIED]  No secret was confirmed in: {name}")
        why = ("But the checks below did not finish, need a person to review them, or your "
               "rules ask for something this tool cannot check" if report.degraded
               else "But the JSON report could not be written")
        for line in _wrap(f"{why} -- so this is NOT a certified clean result (exit code 2). "
                          "Resolve this first.", "                   "):
            print(line)
    else:
        print(f"  [PASS]  No target secrets detected in: {name}")
    print(_BAR)

    if report.degraded:
        if report.leaked:
            print("\n  ⚠ ATTENTION — warnings were raised during the scan "
                  "(these are additional possible problems):")
        else:
            print("\n  ⚠ ATTENTION — warnings were raised during the scan:")
        for w in report.warnings:
            print(_sanitize_report_text(f"    - {w}"))
    print()


def _print_meaning(report: ScanReport, rules: RuleSet | None) -> None:
    lines = ["", "  WHAT THIS MEANS"]
    lines += _wrap("Each ✖ line is text matching one of your rules that is STILL IN THIS FILE. "
                   "Anyone who receives the file can get it back as described below. A pattern "
                   "or built-in type can match text you did not mean -- check each one.", "  ")
    lines += _found_explained(report, rules)
    keys = {_where(f)[1] for f in report.findings}
    lines += ["", "  Where it is, and why that is a problem"]
    for key, (head, body) in _WHERE_EXPLAINED.items():
        if key in keys:
            lines.append(f"    {head}")
            lines += _wrap(body, "        ")
    for key in sorted(keys - set(_WHERE_EXPLAINED)):
        lines.append(f"    {key}")
        lines += _wrap("Stored in the file; PDF tools can read it.", "        ")
    for line in lines:
        print(_sanitize_report_text(line))
