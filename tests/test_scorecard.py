"""Tests for the scorecard harness (eval/scorecard): key normalisation,
diff detection, accepted-diffs matching, metrics on a synthetic set, and
timeout handling in the subprocess runner.

These never touch git, a worktree, or the real verifier CLI — they are
pure-logic tests plus a couple of stand-in-script subprocess tests, so
they run identically with or without qpdf/exiftool/OCR installed.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from scorecard.accepted import (
    AcceptedDiff,
    dump_accepted_diffs,
    find_accepted,
    is_weaker,
    load_accepted_diffs,
)
from scorecard.differential import (
    CaseDiff,
    CaseRun,
    load_reference_cache,
    normalization_have,
    save_reference_cache,
    stale_accepted_diffs,
)
from scorecard.keys import NormalizedKey, effective_exit, is_environmental, normalize
from scorecard.metrics import CaseMetricRow, compute_metrics
from scorecard.runner import CliResult, run_cli

# ── keys.normalize / NormalizedKey ──────────────────────────────────────


def _report(exit_code=0, error=None, findings=(), warnings=()):
    return {"exit_code": exit_code, "error": error, "findings": list(findings), "warnings": list(warnings)}


def test_normalize_findings_key_is_rule_tier_storage():
    report = _report(
        exit_code=1,
        findings=[
            {"rule": "SSN", "tier": "hard", "storage": "orphaned", "object": 7, "page": None},
        ],
    )
    key = normalize(report)
    assert key.exit == 1
    assert key.error is None
    assert key.findings == frozenset({(("SSN", "hard", "orphaned"), 1)})
    assert key.warnings == frozenset()


def test_normalize_review_warning_key_includes_adjacency():
    report = _report(
        exit_code=2,
        warnings=[
            {
                "code": "REVIEW_CROSS_LINE",
                "kind": "review",
                "layer": "Text",
                "rule": "SSN",
                "storage": "live",
                "adjacency": "JOINED_LINES",
            }
        ],
    )
    key = normalize(report)
    assert key.warnings == frozenset({(("SSN", "review", "live", "JOINED_LINES"), 1)})


def test_normalize_other_warning_key_is_code_layer_tool():
    report = _report(
        exit_code=2,
        warnings=[
            {
                "code": "TOOL_EXIT_NONZERO",
                "kind": "coverage",
                "layer": "Binary",
                "tool": "qpdf",
                "returncode": 3,  # not part of the key
            }
        ],
    )
    key = normalize(report)
    assert key.warnings == frozenset({(("TOOL_EXIT_NONZERO", "Binary", "qpdf"), 1)})


def test_normalize_ignores_message_and_sample_text():
    """Two reports that differ only in message wording / masked sample
    normalise to the same key — that's the whole point of the key."""
    findings_a = [{"rule": "SSN", "tier": "hard", "storage": "live", "sample": "****6789"}]
    findings_b = [{"rule": "SSN", "tier": "hard", "storage": "live", "sample": "****0000"}]
    assert normalize(_report(1, findings=findings_a)) == normalize(_report(1, findings=findings_b))


def test_normalize_error_uses_code_only():
    report = _report(exit_code=2, error={"code": "INTERNAL_ERROR", "message": "boom at line 42"})
    assert normalize(report).error == "INTERNAL_ERROR"


def test_normalize_findings_are_a_multiset_not_a_set():
    """The same (rule, tier, storage) twice — e.g. the SSN in two separate
    orphaned streams — must count as two findings, not collapse into one:
    losing one of them is a real regression the differential must catch."""
    report = _report(
        exit_code=1,
        findings=[
            {"rule": "SSN", "tier": "hard", "storage": "orphaned"},
            {"rule": "SSN", "tier": "hard", "storage": "orphaned"},
        ],
    )
    key = normalize(report)
    assert key.findings == frozenset({(("SSN", "hard", "orphaned"), 2)})


def test_diff_catches_a_finding_count_dropping_from_two_to_one():
    old = normalize(
        _report(
            1,
            findings=[
                {"rule": "SSN", "tier": "hard", "storage": "orphaned"},
                {"rule": "SSN", "tier": "hard", "storage": "orphaned"},
            ],
        )
    )
    new = normalize(_report(1, findings=[{"rule": "SSN", "tier": "hard", "storage": "orphaned"}]))
    assert old != new
    diff = old.diff(new)
    assert any("count changed" in p and "2 -> 1" in p for p in diff)


def test_diff_silent_on_identical_duplicate_findings():
    report = _report(
        1,
        findings=[
            {"rule": "SSN", "tier": "hard", "storage": "orphaned"},
            {"rule": "SSN", "tier": "hard", "storage": "orphaned"},
        ],
    )
    key = normalize(report)
    assert key.diff(key) == ()


def test_normalized_key_jsonable_roundtrip():
    report = _report(
        exit_code=1,
        findings=[{"rule": "SSN", "tier": "hard", "storage": "live"}],
        warnings=[
            {"code": "TOOL_MISSING", "kind": "coverage", "layer": "Binary", "tool": "qpdf"},
        ],
    )
    key = normalize(report)
    assert NormalizedKey.from_jsonable(key.to_jsonable()) == key


# ── environmental warnings (OCR/tool absent on this machine) ─────────────


def _ocr_unavailable(**extra):
    return {"code": "OCR_UNAVAILABLE", "kind": "coverage", "layer": "OCR", **extra}


def _tool_missing(tool, **extra):
    return {"code": "TOOL_MISSING", "kind": "coverage", "layer": "Binary", "tool": tool, **extra}


NO_OCR = frozenset({"qpdf", "exiftool", "no-ocr"})
FULL_ENV = frozenset({"qpdf", "exiftool", "ocr"})


def test_is_environmental_true_when_tool_missing_from_have():
    assert is_environmental(_ocr_unavailable(), NO_OCR)


def test_is_environmental_false_when_tool_present():
    assert not is_environmental(_ocr_unavailable(), FULL_ENV)


def test_is_environmental_depends_only_on_its_arguments(monkeypatch):
    """is_environmental must never consult the process environment itself
    — REQUIRE_FULL_ENV is applied by the orchestration layer
    (`differential.normalization_have`), which decides what `have` to
    pass in, not by this function noticing the variable on its own. A
    real host's ambient REQUIRE_FULL_ENV (e.g. set by the full-macos CI
    job) must never change this function's answer for a given `have`."""
    monkeypatch.setenv("REQUIRE_FULL_ENV", "1")
    assert is_environmental(_ocr_unavailable(), NO_OCR)
    monkeypatch.delenv("REQUIRE_FULL_ENV", raising=False)
    assert is_environmental(_ocr_unavailable(), NO_OCR)


def test_is_environmental_uses_warning_tool_field_for_tool_missing():
    assert is_environmental(_tool_missing("qpdf"), frozenset({"exiftool", "no-ocr"}))
    assert not is_environmental(_tool_missing("qpdf"), frozenset({"qpdf", "exiftool", "no-ocr"}))


def test_normalization_have_passes_through_by_default(monkeypatch):
    monkeypatch.delenv("REQUIRE_FULL_ENV", raising=False)
    assert normalization_have(NO_OCR) == NO_OCR


def test_normalization_have_is_none_under_require_full_env(monkeypatch):
    """This is where REQUIRE_FULL_ENV is actually read (docs/REDESIGN.md
    §5) — the orchestration layer, not keys.is_environmental — so a
    missing tool on a full-environment job shows up as a real difference
    instead of being dropped as this machine's own limitation."""
    monkeypatch.setenv("REQUIRE_FULL_ENV", "1")
    assert normalization_have(NO_OCR) is None


def test_effective_exit_drops_solely_environmental_warning():
    # A leftover/revision case with no findings and nothing but an
    # OCR_UNAVAILABLE warning: exit is reported as 2, but on a machine
    # that genuinely lacks OCR that's environmental noise, not a verdict.
    report = _report(exit_code=2, warnings=[_ocr_unavailable()])
    assert effective_exit(report, NO_OCR) == 0
    assert effective_exit(report) == 2  # unfiltered: reported exit stands


def test_effective_exit_keeps_exit_when_a_real_warning_remains():
    report = _report(
        exit_code=2,
        warnings=[_ocr_unavailable(), {"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}],
    )
    assert effective_exit(report, NO_OCR) == 2


def test_normalize_with_have_drops_environmental_warning_from_key():
    report = _report(exit_code=2, warnings=[_ocr_unavailable()])
    key = normalize(report, have=NO_OCR)
    assert key.exit == 0
    assert key.warnings == frozenset()


def test_normalize_reference_vs_candidate_on_a_no_ocr_machine():
    """Regression for the scorecard-diff CI failure: on Linux (no OCR),
    every scan carries an OCR_UNAVAILABLE warning, which used to bump
    both sides' exit to 2 unfiltered and made the K8-fix's real change
    (exit 0 -> 2, a new leftover warning) look like a totally different,
    unlisted diff (just a warning added, no exit change) from the one
    accepted_diffs.yaml records for the full (OCR-available) environment.
    With `have` applied, the two environments agree."""
    reference_report = _report(exit_code=2, warnings=[_ocr_unavailable()])
    candidate_report = _report(
        exit_code=2,
        warnings=[
            _ocr_unavailable(),
            {"code": "LEFTOVER_UNDECODABLE_TEXT", "kind": "coverage", "layer": "Objects"},
        ],
    )
    ref_key = normalize(reference_report, have=NO_OCR)
    cand_key = normalize(candidate_report, have=NO_OCR)
    assert ref_key.exit == 0
    assert cand_key.exit == 2
    diff = ref_key.diff(cand_key)
    assert any("exit 0 -> 2" in p for p in diff)
    assert any("LEFTOVER_UNDECODABLE_TEXT" in p for p in diff)
    # And it matches exactly what a full-environment (OCR available) run
    # of the very same reference/candidate pair produces:
    full_env_ref = normalize(_report(exit_code=0), have=FULL_ENV)
    full_env_cand = normalize(
        _report(
            exit_code=2,
            warnings=[{"code": "LEFTOVER_UNDECODABLE_TEXT", "kind": "coverage", "layer": "Objects"}],
        ),
        have=FULL_ENV,
    )
    assert ref_key == full_env_ref
    assert cand_key == full_env_cand


# ── diff detection ───────────────────────────────────────────────────────


def test_diff_reports_no_difference_when_keys_equal():
    key = normalize(_report(0))
    assert key.diff(key) == ()


def test_diff_reports_exit_change():
    old = normalize(_report(0))
    new = normalize(_report(2))
    assert any("exit 0 -> 2" in p for p in old.diff(new))


def test_diff_reports_finding_added_and_removed():
    old = normalize(_report(1, findings=[{"rule": "SSN", "tier": "hard", "storage": "live"}]))
    new = normalize(_report(1, findings=[{"rule": "Code", "tier": "hard", "storage": "orphaned"}]))
    diff = old.diff(new)
    assert any("finding removed" in p and "SSN" in p for p in diff)
    assert any("finding added" in p and "Code" in p for p in diff)


def test_diff_reports_warning_added():
    old = normalize(_report(0))
    new = normalize(
        _report(
            2,
            warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}],
        )
    )
    diff = old.diff(new)
    assert any("warning added" in p and "LEFTOVER_IMAGE" in p for p in diff)


# ── accepted_diffs.yaml ──────────────────────────────────────────────────


def test_accepted_diffs_round_trip(tmp_path):
    old = normalize(_report(0))
    new = normalize(
        _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
    )
    entry = AcceptedDiff(case="leftover.example", old=old, new=new, reason="a fix", cell="orphaned.pixels", pr="#1")
    path = tmp_path / "accepted_diffs.yaml"
    dump_accepted_diffs([entry], path)

    loaded = load_accepted_diffs(path)
    assert list(loaded) == ["leftover.example"]
    assert find_accepted(loaded, "leftover.example", old, new) == entry


def test_accepted_diffs_does_not_match_a_different_change(tmp_path):
    old = normalize(_report(0))
    new = normalize(
        _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
    )
    entry = AcceptedDiff(case="leftover.example", old=old, new=new, reason="a fix")
    path = tmp_path / "accepted_diffs.yaml"
    dump_accepted_diffs([entry], path)
    loaded = load_accepted_diffs(path)

    other_new = normalize(_report(1, findings=[{"rule": "SSN", "tier": "hard", "storage": "live"}]))
    assert find_accepted(loaded, "leftover.example", old, other_new) is None
    assert find_accepted(loaded, "some.other-case", old, new) is None


def test_load_accepted_diffs_missing_file_is_empty(tmp_path):
    assert load_accepted_diffs(tmp_path / "does-not-exist.yaml") == {}


def test_load_accepted_diffs_rejects_incomplete_entry(tmp_path):
    path = tmp_path / "accepted_diffs.yaml"
    path.write_text("- case: x\n  reason: missing old/new\n")
    with pytest.raises(ValueError):
        load_accepted_diffs(path)


# ── "never less strict" (weaker diffs, stale entries) ─────────────────────


def test_is_weaker_for_each_named_transition():
    assert is_weaker(1, 2)   # leak -> review: less certain
    assert is_weaker(1, 0)   # leak -> clean: less certain
    assert is_weaker(2, 0)   # review -> clean: less certain
    assert not is_weaker(0, 2)   # clean -> review: MORE cautious, not weaker
    assert not is_weaker(2, 1)   # review -> leak: MORE cautious, not weaker
    assert not is_weaker(0, 1)
    assert not is_weaker(1, 1)
    assert not is_weaker(0, 0)


def test_load_accepted_diffs_rejects_weaker_entry_without_the_flag(tmp_path):
    old = normalize(_report(1, findings=[{"rule": "SSN", "tier": "hard", "storage": "live"}]))
    new = normalize(_report(2))
    entry = AcceptedDiff(case="leak.now-review", old=old, new=new, reason="a downgrade")
    path = tmp_path / "accepted_diffs.yaml"
    dump_accepted_diffs([entry], path)
    with pytest.raises(ValueError, match="weaker"):
        load_accepted_diffs(path)


def test_load_accepted_diffs_accepts_weaker_entry_with_the_flag(tmp_path):
    old = normalize(_report(1, findings=[{"rule": "SSN", "tier": "hard", "storage": "live"}]))
    new = normalize(_report(2))
    entry = AcceptedDiff(case="leak.now-review", old=old, new=new, reason="a downgrade", weaker=True)
    path = tmp_path / "accepted_diffs.yaml"
    dump_accepted_diffs([entry], path)
    loaded = load_accepted_diffs(path)
    assert loaded["leak.now-review"][0].weaker is True


def test_stale_accepted_diffs_flags_an_entry_no_diff_used():
    old = normalize(_report(0))
    new = normalize(
        _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
    )
    entry = AcceptedDiff(case="leftover.example", old=old, new=new, reason="a fix")
    accepted = {"leftover.example": [entry]}
    # No CaseDiff in this run's results references the entry at all (the
    # case might have been removed, or no longer produces this change).
    unrelated = CaseDiff("other.case", _run("other.case", _cli_result(_report(0))),
                          _run("other.case", _cli_result(_report(0))), accepted=None)
    stale = stale_accepted_diffs(accepted, [unrelated])
    assert stale == (entry,)


def test_stale_accepted_diffs_empty_when_entry_is_used():
    old = normalize(_report(0))
    new = normalize(
        _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
    )
    entry = AcceptedDiff(case="leftover.example", old=old, new=new, reason="a fix")
    accepted = {"leftover.example": [entry]}
    used = CaseDiff(
        "leftover.example",
        _run("leftover.example", _cli_result(_report(0))),
        _run("leftover.example", _cli_result(
            _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
        )),
        accepted=entry,
    )
    assert stale_accepted_diffs(accepted, [used]) == ()


# ── CaseDiff (differential.py) ───────────────────────────────────────────


def _cli_result(report=None, timed_out=False, returncode=0, elapsed=0.1):
    return CliResult(returncode=returncode, report=report, elapsed=elapsed, timed_out=timed_out, stderr_tail="")


def _run(case_id, result):
    return CaseRun.from_result(case_id, result)


def test_case_diff_ok_when_keys_match():
    result = _cli_result(_report(0))
    diff = CaseDiff("c", _run("c", result), _run("c", result), accepted=None)
    assert diff.ok
    assert not diff.changed
    assert not diff.unlisted


def test_case_diff_unlisted_when_keys_differ_and_not_accepted():
    ref = _run("c", _cli_result(_report(0)))
    cand = _run(
        "c",
        _cli_result(
            _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
        ),
    )
    diff = CaseDiff("c", ref, cand, accepted=None)
    assert not diff.ok
    assert diff.changed
    assert diff.unlisted
    assert "UNLISTED" in diff.describe()


def test_case_diff_accepted_when_matching_entry_given():
    ref = _run("c", _cli_result(_report(0)))
    cand_result = _cli_result(
        _report(2, warnings=[{"code": "LEFTOVER_IMAGE", "kind": "coverage", "layer": "Objects"}])
    )
    cand = _run("c", cand_result)
    entry = AcceptedDiff(case="c", old=ref.key, new=cand.key, reason="a fix")
    diff = CaseDiff("c", ref, cand, accepted=entry)
    assert diff.ok
    assert diff.changed
    assert not diff.unlisted
    assert "a fix" in diff.describe()


def test_case_diff_crashed_is_never_ok_even_with_accepted():
    ref = _run("c", _cli_result(_report(0)))
    crashed_result = _cli_result(report=None, timed_out=True, elapsed=60.0)
    cand = CaseRun.from_result("c", crashed_result)
    diff = CaseDiff("c", ref, cand, accepted=None)
    assert diff.crashed
    assert not diff.ok
    assert "timed out" in diff.describe()


# ── reference result cache ────────────────────────────────────────────────


def test_reference_cache_round_trip(tmp_path):
    run_ok = CaseRun.from_result("case-a", _cli_result(_report(0)))
    save_reference_cache(tmp_path, "key-1", {"case-a": run_ok})

    loaded = load_reference_cache(tmp_path, "key-1")
    assert loaded is not None
    assert loaded["case-a"].key == run_ok.key
    assert loaded["case-a"].crashed is False
    assert loaded["case-a"].result is None  # nothing was actually re-run


def test_reference_cache_never_saves_a_crashed_or_timed_out_run(tmp_path):
    """A flaky reference run must not poison the cache for everyone after
    it: it is retried next time, not frozen in as the answer."""
    run_ok = CaseRun.from_result("case-a", _cli_result(_report(0)))
    run_crashed = CaseRun.from_result("case-b", _cli_result(report=None, timed_out=True, elapsed=9.0))
    save_reference_cache(tmp_path, "key-1", {"case-a": run_ok, "case-b": run_crashed})

    loaded = load_reference_cache(tmp_path, "key-1")
    assert loaded is not None
    assert "case-a" in loaded
    assert "case-b" not in loaded  # never trusted from cache


def test_reference_cache_load_ignores_a_crashed_entry_even_if_present(tmp_path):
    """Defence in depth: even a cache file written by an older version of
    this code (before crashed runs were excluded on save) must not be
    trusted for a crashed entry on load."""
    path = tmp_path / "key-1.json"
    path.write_text(
        json.dumps(
            {
                "key": "key-1",
                "cases": {
                    "case-b": {"key": None, "crashed": True, "timed_out": True, "elapsed": 9.0},
                },
            }
        )
    )
    loaded = load_reference_cache(tmp_path, "key-1")
    assert loaded == {}


def test_reference_cache_miss_on_wrong_key(tmp_path):
    run_ok = CaseRun.from_result("case-a", _cli_result(_report(0)))
    save_reference_cache(tmp_path, "key-1", {"case-a": run_ok})
    assert load_reference_cache(tmp_path, "key-2") is None


def test_reference_cache_missing_file_is_none(tmp_path):
    assert load_reference_cache(tmp_path, "no-such-key") is None


# ── metrics.compute_metrics ───────────────────────────────────────────────


def _row(case_id, truth, expected_exit, actual_exit, has_hard_finding=False, crashed=False,
         timed_out=False, elapsed=1.0):
    return CaseMetricRow(
        case_id=case_id, truth=truth, expected_exit=expected_exit, actual_exit=actual_exit,
        has_hard_finding=has_hard_finding, crashed=crashed, timed_out=timed_out, elapsed=elapsed,
    )


def test_compute_metrics_on_synthetic_set():
    rows = [
        _row("leak-silent-miss", "leak", 1, 0, elapsed=1.0),
        _row("leak-downgrade", "leak", 1, 2, elapsed=1.0),
        _row("leak-correct", "leak", 1, 1, elapsed=1.0),
        _row("clean-correct", "clean", 0, 0, elapsed=1.0),
        _row("clean-false-hard", "clean", 0, 1, has_hard_finding=True, elapsed=3.0),
        _row("clean-review", "clean", 0, 2, elapsed=3.0),
        _row("crashed", "leak", 1, None, crashed=True, elapsed=3.0),
        _row("timed-out", "leak", 1, None, crashed=True, timed_out=True, elapsed=3.0),
    ]
    scorecard = compute_metrics(rows)
    assert scorecard.total == 8
    assert scorecard.leak_cases == 5
    assert scorecard.clean_cases == 3
    assert scorecard.silent_miss == 1
    assert scorecard.downgrade == 1
    assert scorecard.false_hard == 1
    assert scorecard.review_rate == 1
    assert scorecard.crashes == 1
    assert scorecard.timeouts == 1
    # elapsed = [1,1,1,1,3,3,3,3] sorted; p50 interpolates to 2.0, p95 to 3.0.
    assert scorecard.runtime_p50 == pytest.approx(2.0)
    assert scorecard.runtime_p95 == pytest.approx(3.0)


def test_compute_metrics_empty_is_zero():
    scorecard = compute_metrics([])
    assert scorecard.total == 0
    assert scorecard.runtime_p50 == 0.0
    assert scorecard.runtime_p95 == 0.0


def test_compute_metrics_to_jsonable_has_expected_keys():
    scorecard = compute_metrics([_row("a", "leak", 1, 1)])
    data = scorecard.to_jsonable()
    assert set(data) == {
        "total", "leak_cases", "clean_cases", "silent_miss", "downgrade", "false_hard",
        "review_rate", "crashes", "timeouts", "runtime_p50_s", "runtime_p95_s",
    }


# ── corpus.py: local-only path guard, producer-family-only privacy ───────


def test_producer_family_never_stored_raw():
    """CorpusEntry has no field that could hold the raw /Producer string —
    only the coarse bucket producer_family() computes."""
    import dataclasses

    from scorecard.corpus import CorpusEntry

    field_names = {f.name for f in dataclasses.fields(CorpusEntry)}
    assert "producer_family" in field_names
    assert "producer" not in field_names


def test_producer_family_buckets_known_producers():
    from scorecard.corpus import producer_family

    assert producer_family("Adobe Acrobat 24.1") == "acrobat"
    assert producer_family("Microsoft: Word") == "office"
    assert producer_family("LibreOffice 7.6") == "libreoffice"
    assert producer_family(None) == "unknown"
    assert producer_family("Some Bespoke Tool 3.0") == "other"


def test_ensure_local_only_allows_a_path_inside_real_corpus_dir():
    from scorecard.corpus import REAL_CORPUS_DIR, ensure_local_only

    path = REAL_CORPUS_DIR / "manifest.json"
    assert ensure_local_only(path) == path


def test_ensure_local_only_refuses_a_path_outside(tmp_path):
    from scorecard.corpus import ensure_local_only

    with pytest.raises(SystemExit):
        ensure_local_only(tmp_path / "manifest.json")


def test_ensure_local_only_allows_outside_when_flagged(tmp_path):
    from scorecard.corpus import ensure_local_only

    path = tmp_path / "manifest.json"
    assert ensure_local_only(path, allow_outside=True) == path


# ── runner.run_cli: timeout handling ──────────────────────────────────────


_STAND_IN_OK = textwrap.dedent(
    """
    import argparse, json, sys
    p = argparse.ArgumentParser()
    p.add_argument("--target")
    p.add_argument("--secrets")
    p.add_argument("--json")
    args = p.parse_args()
    report = {"exit_code": 0, "error": None, "findings": [], "warnings": []}
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f)
    sys.exit(0)
    """
)

_STAND_IN_HANGS = "import time\ntime.sleep(5)\n"


def test_run_cli_parses_report_on_success(tmp_path):
    script = tmp_path / "stand_in.py"
    script.write_text(_STAND_IN_OK)
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    result = run_cli(
        sys.executable, script, target, [{"name": "SSN", "value": "123-45-6789"}],
        tmp_path / "work", "case-1", timeout=10,
    )
    assert not result.crashed
    assert not result.timed_out
    assert result.returncode == 0
    assert result.report == {"exit_code": 0, "error": None, "findings": [], "warnings": []}


def test_run_cli_times_out(tmp_path):
    script = tmp_path / "hangs.py"
    script.write_text(_STAND_IN_HANGS)
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    result = run_cli(
        sys.executable, script, target, [{"name": "SSN", "value": "123-45-6789"}],
        tmp_path / "work", "case-2", timeout=0.2,
    )
    assert result.timed_out
    assert result.crashed
    assert result.report is None


def test_run_cli_crashed_when_report_missing(tmp_path):
    """A process that exits 0 but never writes a report is still a crash
    the scorecard must not silently treat as a match."""
    script = tmp_path / "no_report.py"
    script.write_text("import sys\nsys.exit(0)\n")
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    result = run_cli(
        sys.executable, script, target, [], tmp_path / "work", "case-3", timeout=10,
    )
    assert result.report is None
    assert result.crashed
