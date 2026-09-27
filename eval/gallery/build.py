"""Build the static gallery: one self-contained ``index.html`` plus one
rendered PNG per case, generated fresh every time into ``--out`` (nothing
here is committed — see eval/README.md's "Gallery" section)."""

from __future__ import annotations

import functools
import html
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import fitz

from caselib import REGISTRY, load
from caselib.cells import parts as cell_parts
from caselib.model import Case
from caselib.run import available, build as build_case

from .knumbers import CASE_TO_K
from .render import render_page1
from .style import CSS
from .verdicts import (REFERENCE_CRASHED_NOTE, Verdict, gap_disagreement, is_miss,
                       is_undocumented_miss, load_results, reference_crashed_cases, verdict_for)

# A cell's row (caselib.cells.parts) says where the secret is. Everything
# the row can name outright other than "live" and "match" (off-page, a
# switched-off layer, an unused resource, an orphaned/superseded object,
# metadata, an attachment, a script, private data, unindexed bytes, ...)
# never reaches the rendered page at all: "Nothing visible here". "live"
# rows are left uncaptioned by design; the value may be covered (a box
# drawn over it, a font encoding it) — the caption says where the value
# is, not whether a reader can make it out. "match" (a layout-splitting
# limit) is NOT settled by the row alone: the value is somewhere on a
# page, but not necessarily on *page 1*, and not necessarily in one
# piece there — see _secret_visibility, which reads the pages' own
# extracted text instead.
_LIVE_ROW = "live"
_MATCH_ROW = "match"

# Captions for the render, keyed by _secret_visibility's answer ("full"
# gets none). They say where the value is, never whether it is legible.
_CAPTIONS = {
    "none": "Nothing visible here: the secret is elsewhere in the file (see above).",
    "later": "Not on page 1: the value appears later in the document.",
    "partial": "Only part of the value is on page 1; the rest is on a later page.",
    "wrapped": "The value is on this page, split across lines.",
}
# Best first: a case with several pinned values is captioned by the one
# page 1 shows most of.
_VISIBILITY_RANK = ("full", "wrapped", "partial", "later", "none")
# The fewest characters of a value page 1 must show to count as "part"
# of it (a value's 3-digit SSN area number, say).
_MIN_PART = 3
# Pages read for the rest of a split value; a value split further than
# this is "none" as far as the caption is concerned.
_MAX_PAGES_READ = 10


def _norm(text: str) -> str:
    """*text* without anything but letters and digits: separators,
    spaces and line breaks all vanish, so "123-45-\\n6789" and the value
    "123-45-6789" compare equal."""
    return re.sub(r"[\W_]+", "", text)


def _page_texts(pdf_path: Path) -> list[str]:
    """The extracted text of the first pages of *pdf_path* (up to
    _MAX_PAGES_READ), or [] if it cannot be opened."""
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    try:
        return [doc[i].get_text() for i in range(min(doc.page_count, _MAX_PAGES_READ))]
    except Exception:
        return []
    finally:
        doc.close()


def _lines(text: str) -> list[str]:
    return [line for line in (_norm(raw) for raw in text.splitlines()) if line]


def _readings(value: str, lines: list[tuple[bool, str]]) -> set[tuple[int, bool]]:
    """Every way to read *value* (normalised) off *lines* — ``(on page 1,
    normalised line)`` in reading order — in two or more pieces: the end
    of one line, then zero or more whole lines, then the start of a later
    line, with any lines in between skipped (a column beside it, a
    footer). Returns ``(characters read off page 1, capped at _MIN_PART;
    whether any piece came from a later page)`` for each."""
    length = len(value)
    # For each offset into the value, the lines that could continue it there.
    candidates = [[j for j, (_p1, text) in enumerate(lines)
                   if text.startswith(value[pos:]) or value.startswith(text, pos)]
                  for pos in range(length)]

    @functools.lru_cache(maxsize=None)
    def rest(pos: int, after: int) -> frozenset[tuple[int, bool]]:
        out: set[tuple[int, bool]] = set()
        remaining = length - pos
        for j in candidates[pos]:
            if j <= after:
                continue
            on_page1, text = lines[j]
            if text.startswith(value[pos:]):          # the last piece
                out.add((min(remaining, _MIN_PART) if on_page1 else 0, not on_page1))
            elif len(text) < remaining:               # a whole line in the middle
                for p1, later in rest(pos + len(text), j):
                    out.add((min(p1 + (len(text) if on_page1 else 0), _MIN_PART),
                             later or not on_page1))
        return frozenset(out)

    found: set[tuple[int, bool]] = set()
    for i, (on_page1, text) in enumerate(lines):
        for k in range(1, length):
            if text.endswith(value[:k]):
                for p1, later in rest(k, i):
                    found.add((min(p1 + (k if on_page1 else 0), _MIN_PART),
                               later or not on_page1))
    return found


def _value_visibility(value: str, pages: list[str]) -> str:
    v = _norm(value)
    if not v or not pages:
        return "none"
    page1 = [(True, line) for line in _lines(pages[0])]
    if any(v in line for _p1, line in page1):
        return "full"
    if _readings(v, page1):
        return "wrapped"
    later = [(False, line) for page in pages[1:] for line in _lines(page)]
    if later:
        # A prefix of the value on page 1 and the rest after it, or (read
        # the other way round) a suffix on page 1 and the rest on a later page.
        for order in (page1 + later, later + page1):
            if any(p1 >= _MIN_PART and used_later for p1, used_later in _readings(v, order)):
                return "partial"
        if any(v in line for _p1, line in later) or _readings(v, later):
            return "later"
    return "none"


def _joins(text: str) -> tuple[str, str, str]:
    """*text*'s lines joined three ways — with nothing, with one space,
    and with nothing after dropping separators at each break — since a
    wrapped value reads back one of those ways ("123-45-" + "6789", "123
    45" + "6789", "123" + "45-" + "6789"). A regex can then match across
    a line break without every line on the page being fused together."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    trimmed = [re.sub(r"^[\W_]+|[\W_]+$", "", line) for line in lines]
    return "".join(lines), " ".join(lines), "".join(trimmed)


def _pattern_visibility(regex: re.Pattern[str], valid: Callable[[str], bool] | None,
                        pages: list[str]) -> str:
    """The same answers for a rule with no literal value (a built-in
    class like "ssn", or a regex), judged by where the rule's own regex
    finds a match its validator (if any) accepts: on one line of page 1
    ("full"); across page 1's line breaks ("wrapped"); starting on page 1
    and running on into the next page ("partial"); only on later pages
    ("later"). Matching runs line by line or across joined lines, never
    over text with every separator stripped, so digits from unrelated
    lines are not fused into a false match."""
    def found(text: str) -> list[re.Match[str]]:
        return [m for m in regex.finditer(text) if valid is None or valid(m.group())]

    if not pages:
        return "none"
    if any(found(line) for line in pages[0].splitlines()):
        return "full"
    later_text = "\n".join(pages[1:])
    heads, tails = _joins(pages[0]), _joins(later_text)
    if any(found(head) for head in heads):
        return "wrapped"
    if len(pages) > 1:
        for sep, head, tail in zip(("", " ", ""), heads, tails):
            if not (head and tail):
                continue
            if any(m.start() <= len(head) - _MIN_PART and m.end() > len(head)
                   for m in found(head + sep + tail)):
                return "partial"
            if any(m.start() < len(tail) and m.end() >= len(tail) + len(sep) + _MIN_PART
                   for m in found(tail + sep + head)):
                return "partial"
        if any(found(tail) for tail in tails):
            return "later"
    return "none"


def _rule_regex(rule: dict[str, str]) -> tuple[re.Pattern[str], Callable[[str], bool] | None] | None:
    """A pattern rule's regex and validator (a built-in class has both,
    as verify.py applies them; a custom regex has no validator)."""
    if "class" in rule:
        import verify
        spec = verify.BUILTIN_PATTERN_CLASSES.get(rule["class"])
        return (re.compile(spec[0]), spec[1]) if spec else None
    if "pattern" in rule:
        try:
            return re.compile(rule["pattern"]), None
        except re.error:
            return None
    return None


def _secret_visibility(case: Case, pdf_path: Path) -> str:
    """"full", "wrapped", "partial", "later" or "none": where a leak
    case's pinned secret is relative to page 1, for captioning the render
    (never for gating anything). This reports where the value is in the
    page's text, not whether a reader can make it out: a value under a
    box, drawn white on white, or in invisible render mode 3 is still
    "full" and gets no caption. The cell's row settles everything but
    "match"; a "match" case is judged from the pages' own extracted text,
    rule by rule (a literal value, or the rule's regex and validator when
    it has none), and captioned by the rule page 1 shows most of:

    - "full": the whole value on one line of page 1 (no caption);
    - "wrapped": the whole value on page 1, but only reading across
      lines (match.line-wrap and friends);
    - "partial": at least _MIN_PART characters of it on page 1 and the
      rest on a later page (match.page-break and friends);
    - "later": none of it on page 1, but the value is on later pages;
    - "none": in no page's text at all (match.extreme-coordinates, K35,
      draws its text where PyMuPDF's extraction never returns it, so
      page 1 is blank)."""
    if not case.cells:
        return "full"   # nothing to judge (shouldn't happen for a leak case)
    row, _column, _qualifier = cell_parts(case.cells[0])
    if row == _LIVE_ROW:
        return "full"
    if row != _MATCH_ROW:
        return "none"
    pages = _page_texts(pdf_path)
    results = []
    for rule in case.rules:
        if "value" in rule:
            results.append(_value_visibility(rule["value"], pages))
        else:
            pattern = _rule_regex(rule)
            if pattern is not None:
                results.append(_pattern_visibility(*pattern, pages))
    return min(results, key=_VISIBILITY_RANK.index, default="none")


REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_MD = REPO_ROOT / "COVERAGE.md"
REDESIGN_MD = REPO_ROOT / "docs" / "REDESIGN.md"

# Display order; families the registry doesn't use are skipped.
_FAMILY_ORDER = ("page", "layout", "document", "attachment", "leftover", "revision", "file")


@dataclass(frozen=True)
class BuildResult:
    out: Path
    leak_cases: int
    clean_cases: int
    skipped_perf: int
    skipped_unavailable: int
    misses: int


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _safe_relative_link(target: Path, out_dir: Path) -> str | None:
    """A relative href from *out_dir* to *target*, or None when that path
    would have to climb out past a recognisable local home directory to
    get there (a gallery built far from the repo — e.g. a test's
    ``tmp_path`` — must never embed a developer's absolute path in its
    HTML just because a relative path technically exists)."""
    if not target.exists():
        return None
    rel = os.path.relpath(target, out_dir)
    if "/Users/" in rel or "/home/" in rel or rel.startswith("Users/") or rel.startswith("home/"):
        return None
    return rel


def _pinned_secrets(case: Case) -> list[str]:
    out = []
    for rule in case.rules:
        name = rule.get("name", "?")
        if "value" in rule:
            out.append(f"{name}: {rule['value']}")
        else:
            out.append(f"{name}: (pattern class {rule.get('class', '?')})")
    return out


def _verdict_html(verdict: Verdict, highlight: bool = False) -> str:
    if verdict.crashed:
        return '<p class="verdict crashed"><strong>Today\'s verdict:</strong> crashed</p>'
    bits = [f"exit {verdict.exit}"]
    if verdict.findings:
        bits.append("findings: " + ", ".join(_esc(f) for f in verdict.findings))
    if verdict.warnings:
        bits.append("warnings: " + ", ".join(_esc(w) for w in verdict.warnings))
    css_class = "verdict miss" if highlight else "verdict"
    return (f'<p class="{css_class}"><strong>Today\'s verdict</strong> '
            f'(<span class="source">{_esc(verdict.source)}</span>): {"; ".join(bits)}</p>')


def _links_html(case: Case, out_dir: Path, k_numbers: dict[str, int]) -> str:
    parts = []
    coverage_cells = case.cells or ((case.known_gap.cell,) if case.known_gap else ())
    if coverage_cells:
        cell_text = ", ".join(f"<code>{_esc(c)}</code>" for c in coverage_cells)
        href = _safe_relative_link(COVERAGE_MD, out_dir)
        if href:
            parts.append(f'COVERAGE.md: <a href="{_esc(href)}">{cell_text}</a>')
        else:
            parts.append(f"COVERAGE.md: {cell_text}")
    k = k_numbers.get(case.id)
    if k is not None:
        href = _safe_relative_link(REDESIGN_MD, out_dir)
        label = f"K{k}"
        if href:
            parts.append(f'REDESIGN.md §8: <a href="{_esc(href)}">{label}</a>')
        else:
            parts.append(f"REDESIGN.md §8: {label}")
    if not parts:
        return ""
    return f'<p class="links">{" &middot; ".join(parts)}</p>'


def _case_section(case: Case, out_dir: Path, verdict: Verdict, k_numbers: dict[str, int],
                  image_ok: bool, visibility: str, reference_crashed: bool = False) -> str:
    miss = is_miss(case, verdict)
    undocumented = is_undocumented_miss(case, verdict)
    if undocumented:
        badge = ' <span class="badge new-miss">NEW MISS (not a known gap)</span>'
    elif miss:
        badge = ' <span class="badge miss">MISSES IT TODAY</span>'
    else:
        badge = ""
    css_classes = "case"
    if miss:
        css_classes += " known-miss"
    if undocumented:
        css_classes += " undocumented-miss"
    if image_ok:
        img_block = (f'<img src="images/{_esc(case.id)}.png" '
                    f'alt="page 1 of {_esc(case.id)}.pdf" loading="lazy">')
        caption = _CAPTIONS.get(visibility)
        if caption:
            img_block += f'<p class="no-visible-secret">{_esc(caption)}</p>'
    else:
        img_block = '<p class="no-render">(page could not be rendered)</p>'
    secrets = _pinned_secrets(case)
    secrets_html = "".join(f"<code>{_esc(s)}</code>" for s in secrets)
    notes = []
    disagreement = gap_disagreement(case, verdict)
    if disagreement:
        notes.append(disagreement)
    if reference_crashed:
        notes.append(f"Note: {REFERENCE_CRASHED_NOTE}.")
    note_html = "".join(f'<p class="note">{_esc(n)}</p>' for n in notes)
    return f"""
<section id="case-{_esc(case.id)}" class="{css_classes}">
  <h4><code>{_esc(case.id)}</code>{badge}</h4>
  <p class="story">{_esc(case.story)}</p>
  <dl>
    <dt>Mistake</dt><dd>{_esc(case.mistake)}</dd>
    <dt>Recovery</dt><dd>{_esc(case.recovery)}</dd>
  </dl>
  <div class="render">{img_block}</div>
  <p class="secret"><strong>Pinned secret(s):</strong> {secrets_html}</p>
  {_verdict_html(verdict, highlight=miss)}
  {note_html}
  {_links_html(case, out_dir, k_numbers)}
</section>
"""


def _clean_row(case: Case, verdict: Verdict) -> str:
    tag = ' <span class="badge false-alarm">false alarm</span>' if case.known_gap else ""
    return f"""
<section id="case-{_esc(case.id)}" class="case clean">
  <h4><code>{_esc(case.id)}</code>{tag}</h4>
  <p class="story">{_esc(case.story)}</p>
  {_verdict_html(verdict)}
</section>
"""


def _page(body: str, leak_count: int, clean_count: int, miss_count: int) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Redaction Verifier &mdash; Gallery</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>PDF Redaction Verifier &mdash; Gallery</h1>
  <p class="tagline">Generated from the case library (eval/caselib). Illustrative only
  &mdash; this page never gates anything (docs/REDESIGN.md &sect;5).
  Every value shown is fabricated (the canonical example SSN is 123-45-6789).</p>
  <p class="counts">{leak_count} leak cases &middot; {clean_count} clean cases &middot;
  <span class="miss-count">{miss_count} misses today</span></p>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def build(out: Path, results_path: Path | None = None,
         case_ids: Iterable[str] | None = None) -> BuildResult:
    """Build the gallery into *out*. *case_ids* restricts it to a subset of
    ``caselib.REGISTRY`` (tests only — the CLI always builds every case)."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(parents=True, exist_ok=True)

    load()
    have = available()
    results = load_results(results_path) if results_path else None
    ref_crashed = reference_crashed_cases(results_path) if results_path else frozenset()
    selected = sorted(case_ids) if case_ids is not None else sorted(REGISTRY)
    k_numbers = CASE_TO_K

    skipped_perf = 0
    skipped_unavailable = 0
    leak_by_family: dict[str, list[Case]] = {}
    clean_cases: list[Case] = []

    with tempfile.TemporaryDirectory(prefix="gallery-build-") as tmp:
        tmpdir = Path(tmp)
        for case_id in selected:
            case = REGISTRY[case_id]
            if getattr(case, "perf", False):
                skipped_perf += 1
                continue
            if case.requires - have:
                skipped_unavailable += 1
                continue
            if case.truth == "leak":
                leak_by_family.setdefault(case.family, []).append(case)
            else:
                clean_cases.append(case)

        misses = 0
        sections: list[str] = []
        for family in _FAMILY_ORDER:
            cases = leak_by_family.get(family)
            if not cases:
                continue
            sections.append(f'<h2 id="family-{_esc(family)}">{_esc(family)}</h2>')
            by_cell: dict[str, list[Case]] = {}
            for case in cases:
                cell = case.cells[0] if case.cells else "(uncategorised)"
                by_cell.setdefault(cell, []).append(case)
            for cell in sorted(by_cell):
                sections.append(f'<h3><code>{_esc(cell)}</code></h3>')
                for case in sorted(by_cell[cell], key=lambda c: c.id):
                    pdf = tmpdir / f"{case.id}.pdf"
                    build_case(case, pdf)
                    image_ok = render_page1(pdf, out / "images" / f"{case.id}.png")
                    visibility = _secret_visibility(case, pdf)
                    verdict = verdict_for(case, results)
                    if is_miss(case, verdict):
                        misses += 1
                    sections.append(_case_section(case, out, verdict, k_numbers, image_ok,
                                                  visibility, reference_crashed=case.id in ref_crashed))

        sections.append('<h2 id="clean-cases">Clean &amp; false-alarm cases</h2>')
        for case in sorted(clean_cases, key=lambda c: c.id):
            verdict = verdict_for(case, results)
            sections.append(_clean_row(case, verdict))

    total_leak = sum(len(v) for v in leak_by_family.values())
    html_doc = _page("\n".join(sections), total_leak, len(clean_cases), misses)
    (out / "index.html").write_text(html_doc)

    return BuildResult(
        out=out, leak_cases=total_leak, clean_cases=len(clean_cases),
        skipped_perf=skipped_perf, skipped_unavailable=skipped_unavailable, misses=misses,
    )
