"""COVERAGE.md's "Known gaps" stays in step with the case library (#33).

Every pinned known gap (`Case.known_gap`) is a place where today's tool
gives the wrong answer, so a reader must find it in COVERAGE.md's Known
gaps section (the README keeps only a short plain-language list and links
there). Each bullet there, and the false-positives paragraph, carries an
HTML comment, `<!-- gap-cells: a b c -->`, naming the gap cells its prose
covers. The check is two-way: a new known gap without prose fails, and so
does prose left behind for a gap that was closed. Visible `(K39)`-style
tags let readers follow docs/REDESIGN.md's K-table; each must name a row
there that is not marked fixed.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from caselib import load

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMENT = re.compile(r"<!-- gap-cells: ([^>]*?) -->")


def _section() -> str:
    """COVERAGE.md's Known gaps section: from its heading to the next
    heading of the same or a higher level, or to the end of the file."""
    coverage = (REPO_ROOT / "COVERAGE.md").read_text(encoding="utf-8")
    start = coverage.index("\n## Known gaps\n")
    end = coverage.find("\n## ", start + 1)
    return coverage[start:] if end == -1 else coverage[start:end]


def _bullets(section: str) -> list[str]:
    """The section's top-level bullets, each with its continuation lines."""
    return [item for item in re.split(r"\n(?=- )", section)[1:]]


def test_every_known_gap_cell_is_listed_and_only_those() -> None:
    listed = [cell for found in COMMENT.findall(_section()) for cell in found.split()]
    duplicates = sorted(cell for cell, n in Counter(listed).items() if n > 1)
    assert not duplicates, f"cells listed under more than one entry: {duplicates}"
    gaps = {case.known_gap.cell for case in load().values() if case.known_gap}
    missing = sorted(gaps - set(listed))
    stale = sorted(set(listed) - gaps)
    assert not missing, (
        f"known_gap cells with no COVERAGE.md Known-gaps entry: {missing} -- describe each "
        "in the entry it belongs to and add it to that entry's gap-cells comment")
    assert not stale, (
        f"COVERAGE.md's Known gaps lists cells that are no longer known gaps: {stale} -- "
        "remove them, "
        "and the prose too if the gap is closed")


def test_every_gap_bullet_carries_one_comment() -> None:
    """A bullet without a comment could describe a gap the check never
    sees; one with two is ambiguous."""
    bullets = _bullets(_section())
    assert bullets, "COVERAGE.md's Known gaps section has no bullets"
    for bullet in bullets:
        bullet = bullet.split("\n\n", 1)[0]  # a bullet ends at the first blank line
        assert len(COMMENT.findall(bullet)) == 1, (
            f"bullet without exactly one gap-cells comment: {bullet[:80]!r}")


# K-table rows that say "**fixed**" for part of the gap while the rest is
# still open, so COVERAGE.md's Known gaps may still tag them. A row is added here only with
# the residue it still describes.
PARTLY_FIXED = {
    "34": "fixed at the manual-review tier; the hard tier is still open",
    "42": "fixed on macOS 27 only; CI's macOS 26 still exits 0",
}


def _k_rows() -> dict[str, str]:
    redesign = (REPO_ROOT / "docs" / "REDESIGN.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| K(\d+) \| (.*)$", redesign, flags=re.MULTILINE))


def _is_fixed(number: str, row: str) -> bool:
    """A row marks its gap fixed when it says **fixed** in bold, in any
    case and anywhere in the row (K8, K9 and K40 all write it differently)."""
    return number not in PARTLY_FIXED and re.search(r"\*\*fixed", row, re.IGNORECASE) is not None


def test_the_fixed_row_check_recognises_every_way_the_table_writes_it() -> None:
    rows = _k_rows()
    for number in ("8", "9", "12", "38", "40", "41"):
        assert _is_fixed(number, rows[number]), f"K{number} should read as fixed"
    for number in ("14", "39", "43", *PARTLY_FIXED):
        assert not _is_fixed(number, rows[number]), f"K{number} should read as open"


def test_k_tags_name_open_rows_of_the_k_table() -> None:
    rows = _k_rows()
    tags = set(re.findall(r"\bK(\d+)\b", COMMENT.sub("", _section())))
    assert tags, "COVERAGE.md's Known gaps section names no K-number"
    unknown = sorted((t for t in tags if t not in rows), key=int)
    fixed = sorted((t for t in tags if t in rows and _is_fixed(t, rows[t])), key=int)
    assert not unknown, f"K-numbers with no row in docs/REDESIGN.md's K-table: {unknown}"
    assert not fixed, f"K-numbers the K-table marks fixed: {fixed}"


def test_the_section_uses_only_dash_bullets() -> None:
    """The bullet check splits on "- "; another marker would slip past it."""
    assert not re.search(r"^\s*[*+] ", _section(), flags=re.MULTILINE)


def test_gap_cells_comments_appear_only_in_the_section() -> None:
    """A gap-cells comment anywhere else (elsewhere in COVERAGE.md, or left
    in the README the list moved from) is never checked, so it could vouch
    for a gap no entry here describes."""
    coverage = (REPO_ROOT / "COVERAGE.md").read_text(encoding="utf-8")
    assert len(COMMENT.findall(coverage)) == len(COMMENT.findall(_section())), (
        "gap-cells comment outside COVERAGE.md's Known gaps section")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert not COMMENT.findall(readme), (
        "README.md carries a gap-cells comment; known gaps are listed in COVERAGE.md")
