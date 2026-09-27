"""The gallery (eval/gallery): built from case metadata, never gating
anything (docs/REDESIGN.md §5's "Gallery"). Builds a small subset into
``tmp_path`` rather than the whole ~390-case registry."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from caselib import REGISTRY, load
from caselib.cells import parts as cell_parts
from gallery.build import REDESIGN_MD, build
from gallery.knumbers import CASE_TO_K, documented_k_numbers
from gallery.verdicts import REFERENCE_CRASHED_NOTE

load()

# A small, representative subset: a known-miss leak case, a leak case the
# tool actually catches, a leak case with no cell drawn on the page (for
# the "nothing visible here" caption), an ordinary clean case, and a
# false-alarm case (clean, but with its own known_gap — never a "miss":
# truth is "clean").
_SUBSET = (
    "page.pixels-under-box",       # leak, known_gap, today's tool misses it (exit 0)
    "page.box-over-text",          # leak, caught today, secret drawn on the page
    "document.xmp-thumbnail",      # leak, known_gap, secret in XMP metadata (not on the page)
    "page.raw-minimal",            # clean, no known_gap
    "page.binary-digit-collision", # clean, known_gap (false alarm) — not a "miss"
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("gallery")
    result = build(out, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    return out, result, html


def test_subset_is_what_it_claims() -> None:
    for case_id in _SUBSET:
        assert case_id in REGISTRY, case_id


def test_every_leak_case_gets_a_section_with_mistake_and_recovery(built) -> None:
    _out, _result, page_html = built
    for case_id in _SUBSET:
        case = REGISTRY[case_id]
        if case.truth != "leak":
            continue
        section = _section(page_html, case_id)
        assert case.mistake and case.mistake in section, f"{case_id}: missing mistake text"
        assert case.recovery and case.recovery in section, f"{case_id}: missing recovery text"


def test_known_miss_marker_is_exact(built) -> None:
    _out, _result, html = built
    for case_id in _SUBSET:
        case = REGISTRY[case_id]
        section_re = re.compile(
            rf'<section id="case-{re.escape(case_id)}".*?</section>', re.DOTALL)
        section = section_re.search(html).group(0)  # type: ignore[union-attr]
        should_be_miss = (
            case.truth == "leak" and case.known_gap is not None
            and case.known_gap.today.exit == 0
        )
        has_marker = "MISSES IT TODAY" in section
        assert has_marker == should_be_miss, (
            f"{case_id}: miss marker present={has_marker}, expected={should_be_miss}")


def test_known_miss_count_matches_result(built) -> None:
    _out, result, _html = built
    expected = sum(
        1 for cid in _SUBSET
        if REGISTRY[cid].truth == "leak" and REGISTRY[cid].known_gap is not None
        and REGISTRY[cid].known_gap.today.exit == 0
    )
    assert result.misses == expected


def test_writes_only_inside_out(tmp_path) -> None:
    out = tmp_path / "gallery-out"
    before = {p for p in tmp_path.rglob("*") if p != out and out not in p.parents}
    build(out, case_ids=_SUBSET)
    after = {p for p in tmp_path.rglob("*") if p != out and out not in p.parents}
    assert after == before, f"wrote outside --out: {sorted(str(p) for p in after - before)}"
    assert (out / "index.html").exists()


def test_no_absolute_local_path_leaks_into_the_html(built) -> None:
    _out, _result, html = built
    assert "/Users/" not in html
    assert "/home/" not in html


def test_images_rendered_for_every_leak_case(built) -> None:
    out, _result, _html = built
    for case_id in _SUBSET:
        if REGISTRY[case_id].truth != "leak":
            continue
        assert (out / "images" / f"{case_id}.png").exists(), case_id


def test_clean_and_false_alarm_cases_get_their_own_section(built) -> None:
    _out, _result, html = built
    idx = html.index('id="clean-cases"')
    clean_section = html[idx:]
    for case_id in _SUBSET:
        if REGISTRY[case_id].truth == "clean":
            assert f'case-{case_id}"' in clean_section, case_id
        else:
            assert f'case-{case_id}"' not in clean_section, case_id


def test_false_alarm_case_is_labelled(built) -> None:
    _out, _result, html = built
    section_re = re.compile(
        r'<section id="case-page\.binary-digit-collision".*?</section>', re.DOTALL)
    section = section_re.search(html)
    assert section and "false alarm" in section.group(0)


def _section(page_html: str, case_id: str) -> str:
    m = re.search(rf'<section id="case-{re.escape(case_id)}".*?</section>', page_html, re.DOTALL)
    assert m, f"{case_id}: no section in the gallery"
    return html.unescape(m.group(0))


# ── "nothing visible here" caption (derived from the cell's own row) ────

def test_caption_appears_only_when_the_secret_is_not_drawn_on_the_page(built) -> None:
    _out, _result, html = built
    # document.xmp-thumbnail's secret lives in the XMP metadata packet, not
    # on the rendered page (row "metadata", not "live"/"match").
    assert "Nothing visible here" in _section(html, "document.xmp-thumbnail")
    # page.box-over-text draws the secret directly on the page (row "live").
    assert "Nothing visible here" not in _section(html, "page.box-over-text")


def test_caption_matches_the_cells_own_row_taxonomy() -> None:
    # Sanity check on the derivation itself, independent of any one case:
    # every case in the subset agrees with caselib.cells.parts's row.
    for case_id in _SUBSET:
        case = REGISTRY[case_id]
        if case.truth != "leak" or not case.cells:
            continue
        row, _column, _qualifier = cell_parts(case.cells[0])
        expect_caption = row not in ("live", "match")
        # document.xmp-thumbnail is the only subset member with a
        # non-page-drawn cell; everything else is "live".
        assert expect_caption == (case_id == "document.xmp-thumbnail")


# ── "match" is not decisive on its own: the value is on SOME page, but ──
# ── page 1 may hold all of it on one line, all of it only across lines, ──
# ── part of it (the rest later), none of it (all on a later page), or  ──
# ── it may be in no page's text at all. The                             ──
# ── reviewer found 57 of ~150 match.* cases captioned wrongly; this     ──
# ── pins every one of them against a hand-checked table.                ──

_VISIBILITY_TABLE_PATH = Path(__file__).parent / "gallery_visibility.json"
_VISIBILITY_TABLE: dict[str, str] = json.loads(_VISIBILITY_TABLE_PATH.read_text())


def _match_leak_ids() -> set[str]:
    return {cid for cid, case in REGISTRY.items()
            if case.truth == "leak" and case.cells and cell_parts(case.cells[0])[0] == "match"}


def test_visibility_table_covers_every_match_leak_case() -> None:
    assert sorted(set(_VISIBILITY_TABLE) ^ _match_leak_ids()) == [], (
        f"{_VISIBILITY_TABLE_PATH.name} must list exactly the match.* leak cases")
    assert set(_VISIBILITY_TABLE.values()) <= {"full", "wrapped", "partial", "later", "none"}
    assert _VISIBILITY_TABLE["page.extreme-coordinates"] == "none"


def test_secret_visibility_matches_the_table_for_every_match_case(tmp_path) -> None:
    """Every entry was checked by hand against what page 1's extracted
    text holds: "full" (the whole value on one line: the overlapping
    short-tail grid, and the several-values grid's card number),
    "wrapped" (on page 1 but only across lines: split-lines, table-rows,
    repeated-line-wraps, vertical-stack, raw-line-wrap, wrap-boxed-lines,
    two-column-wrap, ...), "partial" (a prefix on page 1, the rest on a
    later page: every split-pages / form-boxes / split-four-pages /
    vertical-stack-split-pages variant, page-break, ...), "later" (none
    of it on page 1, but on a later page: the several-values variants
    whose only rules are for the SSN, which sits on pages 2-3), "none" (in
    no page's text: page.extreme-coordinates)."""
    from caselib.run import build as build_case
    from gallery.build import _secret_visibility
    wrong = []
    for case_id, expected in sorted(_VISIBILITY_TABLE.items()):
        pdf = tmp_path / f"{case_id}.pdf"
        build_case(REGISTRY[case_id], pdf)
        got = _secret_visibility(REGISTRY[case_id], pdf)
        if got != expected:
            wrong.append(f"{case_id}: expected {expected}, got {got}")
    assert wrong == []


_VISIBILITY_SUBSET = (
    "page.extreme-coordinates",  # row "match", but nothing of the value is on page 1 at all
    "layout.page-break",         # row "match", but only half the value is on page 1
    "layout.raw-line-wrap",      # row "match": all of it on page 1, split across two lines
    "layout.several-values-ssn", # row "match": the SSN is only on pages 2-3
    "page.box-over-text",        # row "live": uncaptioned by design
)


@pytest.fixture(scope="module")
def visibility_built(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("gallery-visibility")
    build(out, case_ids=_VISIBILITY_SUBSET)
    return (out / "index.html").read_text()


_ALL_CAPTIONS = ("Nothing visible here", "Only part of the value", "split across lines",
                 "Not on page 1")


@pytest.mark.parametrize(("case_id", "caption"), [
    ("page.extreme-coordinates", "Nothing visible here"),
    ("layout.page-break", "Only part of the value is on page 1"),
    ("layout.raw-line-wrap", "The value is on this page, split across lines"),
    ("layout.several-values-ssn", "Not on page 1: the value appears later in the document"),
    ("page.box-over-text", None),
])
def test_each_visibility_gets_its_own_caption(visibility_built, case_id: str,
                                              caption: str | None) -> None:
    section = _section(visibility_built, case_id)
    for other in _ALL_CAPTIONS:
        if caption is None or other not in caption:
            assert other not in section, (case_id, other)
    if caption:
        assert caption in section


# ── the badge follows the measured verdict, not just the pinned label ───

def _write_results(path: Path, entries: dict[str, int | None]) -> None:
    """A minimal synthetic ``scorecard diff --json`` report: *entries*
    maps case id -> measured exit code, or None for a crash."""
    cases = []
    for case_id, exit_code in entries.items():
        crashed = exit_code is None
        cases.append({
            "case": case_id,
            "crashed": crashed,
            "candidate": None if crashed else {"exit": exit_code, "findings": [], "warnings": []},
        })
    path.write_text(json.dumps({"cases": cases}))


def test_measured_result_can_surface_an_undocumented_miss(tmp_path) -> None:
    # page.box-over-text has no known_gap and is normally caught (exit 1);
    # a measured exit 0 here is a real, undocumented miss and must be
    # called out distinctly from an already-known gap.
    results = tmp_path / "results.json"
    _write_results(results, {"page.box-over-text": 0})
    out = tmp_path / "gallery"
    result = build(out, results_path=results, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    section = _section(html, "page.box-over-text")
    assert "NEW MISS (not a known gap)" in section
    assert "MISSES IT TODAY" not in section    # that badge is for known gaps only
    # This new undocumented miss, plus the subset's two unmeasured known
    # gaps (page.pixels-under-box, document.xmp-thumbnail) falling back to
    # their pinned labels.
    assert result.misses == 3


def test_measured_result_can_show_a_known_gap_as_closed(tmp_path) -> None:
    # page.pixels-under-box's label says exit 0 (a known, pinned miss);
    # a measured exit 1 this run means it was actually caught — the badge
    # must follow the measurement, not the stale label, and the page
    # should say the two disagree.
    results = tmp_path / "results.json"
    _write_results(results, {"page.pixels-under-box": 1})
    out = tmp_path / "gallery"
    result = build(out, results_path=results, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    section = _section(html, "page.pixels-under-box")
    assert "MISSES IT TODAY" not in section
    assert "NEW MISS" not in section
    assert "measured verdict caught it instead" in section
    # document.xmp-thumbnail is still an (unmeasured, label-based) miss.
    assert result.misses == 1


def test_measured_result_agreeing_with_a_known_gap_still_shows_the_badge(tmp_path) -> None:
    results = tmp_path / "results.json"
    _write_results(results, {"page.pixels-under-box": 0})
    out = tmp_path / "gallery"
    build(out, results_path=results, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    section = _section(html, "page.pixels-under-box")
    assert "MISSES IT TODAY" in section
    assert "measured this run" in section


def test_crashed_result_is_never_a_miss(tmp_path) -> None:
    results = tmp_path / "results.json"
    _write_results(results, {"page.pixels-under-box": None})
    out = tmp_path / "gallery"
    result = build(out, results_path=results, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    section = _section(html, "page.pixels-under-box")
    assert "MISSES IT TODAY" not in section
    assert "NEW MISS" not in section
    assert "crashed" in section
    # document.xmp-thumbnail's own (unmeasured) known-gap miss still counts.
    assert result.misses == 1


# ── a reference-only crash must never launder a real candidate result   ──
# ── into "crashed" (reviewer-confirmed: scorecard's own "crashed" is     ──
# ── reference-OR-candidate, differential.CaseDiff.crashed, but the old   ──
# ── code keyed load_results off that combined flag instead of off        ──
# ── whether the CANDIDATE itself crashed). ───────────────────────────────

def _write_reference_crash_row(path: Path, case_id: str, candidate_exit: int) -> None:
    """A synthetic ``scorecard diff --json`` report where the reference
    crashed but the candidate ran fine and actually missed (exit 0) — the
    reviewer's exact reproduction shape."""
    path.write_text(json.dumps({"cases": [{
        "case": case_id,
        "reference": None,       # None here means the reference crashed
        "candidate": {"exit": candidate_exit, "findings": [], "warnings": []},
        "crashed": True,         # reference.crashed or candidate.crashed — true either way
    }]}))


def test_load_results_uses_the_candidate_not_the_combined_crashed_flag(tmp_path) -> None:
    from gallery.verdicts import load_results
    results = tmp_path / "results.json"
    _write_reference_crash_row(results, "page.box-over-text", candidate_exit=0)
    loaded = load_results(results)
    assert loaded["page.box-over-text"] == {"exit": 0, "findings": [], "warnings": []}


def test_reference_only_crash_still_surfaces_the_candidates_real_miss(tmp_path) -> None:
    # page.box-over-text has no known_gap and is normally caught (exit 1);
    # a measured candidate exit 0 is a real, undocumented miss. The old
    # code hid this behind "crashed" just because the reference crashed.
    results = tmp_path / "results.json"
    _write_reference_crash_row(results, "page.box-over-text", candidate_exit=0)
    out = tmp_path / "gallery"
    result = build(out, results_path=results, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    section = _section(html, "page.box-over-text")
    assert 'class="verdict crashed"' not in section
    assert "NEW MISS (not a known gap)" in section
    assert REFERENCE_CRASHED_NOTE in section  # a note, not a substitute for the real verdict
    assert result.misses == 3  # same count as the ordinary undocumented-miss test above


def test_reference_only_crash_with_a_caught_candidate_is_not_a_miss_or_a_crash(tmp_path) -> None:
    # The candidate genuinely caught it (exit 1) even though the reference
    # crashed — must show as an ordinary caught verdict, not "crashed".
    results = tmp_path / "results.json"
    _write_reference_crash_row(results, "page.box-over-text", candidate_exit=1)
    out = tmp_path / "gallery"
    build(out, results_path=results, case_ids=_SUBSET)
    html = (out / "index.html").read_text()
    section = _section(html, "page.box-over-text")
    assert 'class="verdict crashed"' not in section
    assert "NEW MISS" not in section
    assert "MISSES IT TODAY" not in section
    assert REFERENCE_CRASHED_NOTE in section


# ── K-numbers: an explicit, hand-verified table, checked against §8 ─────

def test_every_documented_k_number_is_claimed_by_exactly_one_case() -> None:
    documented = documented_k_numbers(REDESIGN_MD.read_text())
    assert documented, "couldn't find docs/REDESIGN.md §8's table at all"
    claimed = list(CASE_TO_K.values())
    assert len(claimed) == len(set(claimed)), "a K-number is claimed by more than one case"
    assert set(claimed) == documented, (
        f"CASE_TO_K vs §8 disagree: only in CASE_TO_K={set(claimed) - documented}, "
        f"only in §8={documented - set(claimed)}")


def test_every_k_numbered_case_id_is_a_real_known_gap() -> None:
    for case_id, k in CASE_TO_K.items():
        assert case_id in REGISTRY, f"K{k}: {case_id!r} is not a registered case"
        assert REGISTRY[case_id].known_gap is not None, f"K{k}: {case_id!r} has no known_gap"


def test_k_number_renders_in_its_case_section(built) -> None:
    _out, _result, html = built
    section = _section(html, "page.pixels-under-box")
    assert "K12" in section
