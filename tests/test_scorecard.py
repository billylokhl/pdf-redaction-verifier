"""Tests for the scorecard harness (eval/scorecard): key normalisation,
diff detection, accepted-diffs matching, metrics on a synthetic set, and
timeout handling in the subprocess runner.

These never touch git, a worktree, or the real verifier CLI — they are
pure-logic tests plus a couple of stand-in-script subprocess tests, so
they run identically with or without qpdf/exiftool/OCR installed.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from scorecard.accepted import AcceptedDiff, dump_accepted_diffs, find_accepted, load_accepted_diffs
from scorecard.differential import CaseDiff, CaseRun, load_reference_cache, save_reference_cache
from scorecard.keys import NormalizedKey, normalize
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
    assert key.findings == frozenset({("SSN", "hard", "orphaned")})
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
    assert key.warnings == frozenset({("SSN", "review", "live", "JOINED_LINES")})


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
    assert key.warnings == frozenset({("TOOL_EXIT_NONZERO", "Binary", "qpdf")})


def test_normalize_ignores_message_and_sample_text():
    """Two reports that differ only in message wording / masked sample
    normalise to the same key — that's the whole point of the key."""
    findings_a = [{"rule": "SSN", "tier": "hard", "storage": "live", "sample": "****6789"}]
    findings_b = [{"rule": "SSN", "tier": "hard", "storage": "live", "sample": "****0000"}]
    assert normalize(_report(1, findings=findings_a)) == normalize(_report(1, findings=findings_b))


def test_normalize_error_uses_code_only():
    report = _report(exit_code=2, error={"code": "INTERNAL_ERROR", "message": "boom at line 42"})
    assert normalize(report).error == "INTERNAL_ERROR"


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
    run_crashed = CaseRun.from_result("case-b", _cli_result(report=None, timed_out=True, elapsed=9.0))
    save_reference_cache(tmp_path, "key-1", {"case-a": run_ok, "case-b": run_crashed})

    loaded = load_reference_cache(tmp_path, "key-1")
    assert loaded is not None
    assert loaded["case-a"].key == run_ok.key
    assert loaded["case-a"].crashed is False
    assert loaded["case-a"].result is None  # nothing was actually re-run
    assert loaded["case-b"].crashed is True
    assert loaded["case-b"].timed_out is True
    assert loaded["case-b"].key is None


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
