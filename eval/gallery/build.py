"""Build the static gallery: one self-contained ``index.html`` plus one
rendered PNG per case, generated fresh every time into ``--out`` (nothing
here is committed — see eval/README.md's "Gallery" section)."""

from __future__ import annotations

import html
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from caselib import REGISTRY, load
from caselib.cells import parts as cell_parts
from caselib.model import Case
from caselib.run import available, build as build_case

from .knumbers import CASE_TO_K
from .render import render_page1
from .style import CSS
from .verdicts import Verdict, gap_disagreement, is_miss, is_undocumented_miss, load_results, verdict_for

# A cell's row (caselib.cells.parts) says where the secret is, which tells
# us whether the page-1 render could plausibly show it at all: "live" is
# drawn on a page, "match" is a layout-splitting limit (the value IS on
# the page, just split awkwardly) — everything else (off-page, a
# switched-off layer, an unused resource, an orphaned/superseded object,
# metadata, an attachment, a script, private data, unindexed bytes, ...)
# never reaches the rendered page at all.
_VISIBLE_ROWS = frozenset({"live", "match"})


def _secret_visible_on_page(case: Case) -> bool:
    if not case.cells:
        return True   # nothing to judge (shouldn't happen for a leak case)
    row, _column, _qualifier = cell_parts(case.cells[0])
    return row in _VISIBLE_ROWS

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
                  image_ok: bool) -> str:
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
        if not _secret_visible_on_page(case):
            img_block += ('<p class="no-visible-secret">Nothing visible here: the secret '
                          'is elsewhere in the file (see above).</p>')
    else:
        img_block = '<p class="no-render">(page could not be rendered)</p>'
    secrets = _pinned_secrets(case)
    secrets_html = "".join(f"<code>{_esc(s)}</code>" for s in secrets)
    disagreement = gap_disagreement(case, verdict)
    note_html = f'<p class="note">{_esc(disagreement)}</p>' if disagreement else ""
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
                    verdict = verdict_for(case, results)
                    if is_miss(case, verdict):
                        misses += 1
                    sections.append(_case_section(case, out, verdict, k_numbers, image_ok))

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
