"""README's "Known gaps" stays in step with the case library (#33).

Every pinned known gap (`Case.known_gap`) is a place where today's tool
gives the wrong answer, so a reader must find it in README's Known gaps
section. Each bullet there, and the false-positives paragraph, carries
an HTML comment, `<!-- gap-cells: a b c -->`, naming the gap cells its
prose covers. The check is two-way: a new known gap without prose fails,
and so does prose left behind for a gap that was closed. Visible
`(K39)`-style tags let readers follow docs/REDESIGN.md's K-table; each
must name a row there that is not marked fixed.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from caselib import load

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMENT = re.compile(r"<!-- gap-cells: ([^>]*?) -->")


def _section() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("\n### Known gaps\n")
    end = readme.index("\n## ", start)
    return readme[start:end]


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
        f"known_gap cells with no README Known-gaps entry: {missing} -- describe each "
        "in the entry it belongs to and add it to that entry's gap-cells comment")
    assert not stale, (
        f"README lists cells that are no longer known gaps: {stale} -- remove them, "
        "and the prose too if the gap is closed")


def test_every_gap_bullet_carries_one_comment() -> None:
    """A bullet without a comment could describe a gap the check never
    sees; one with two is ambiguous."""
    bullets = _bullets(_section())
    assert bullets, "README's Known gaps section has no bullets"
    for bullet in bullets:
        bullet = bullet.split("\n\n", 1)[0]  # a bullet ends at the first blank line
        assert len(COMMENT.findall(bullet)) == 1, (
            f"bullet without exactly one gap-cells comment: {bullet[:80]!r}")


def test_k_tags_name_open_rows_of_the_k_table() -> None:
    redesign = (REPO_ROOT / "docs" / "REDESIGN.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| K(\d+) \| (.*)$", redesign, flags=re.MULTILINE))
    tags = set(re.findall(r"\bK(\d+)\b", COMMENT.sub("", _section())))
    assert tags, "README's Known gaps section names no K-number"
    unknown = sorted((t for t in tags if t not in rows), key=int)
    fixed = sorted((t for t in tags if t in rows and rows[t].startswith("**Fixed**")), key=int)
    assert not unknown, f"K-numbers with no row in docs/REDESIGN.md's K-table: {unknown}"
    assert not fixed, f"K-numbers the K-table marks fixed: {fixed}"
