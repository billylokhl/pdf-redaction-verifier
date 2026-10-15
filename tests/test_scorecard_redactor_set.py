"""`scorecard redactor-set run` (eval/scorecard/redactor_set.py): the
per-instance U2 scoring, U3, and the guards around the run. Fabricated
values only (eval/redactor_set/persona.py)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from redactor_set.persona import PATTERNS, SECRETS  # noqa: E402
from scorecard import corpus, redactor_set as rs  # noqa: E402
from scorecard.runner import CliResult  # noqa: E402

EXACT = [s.value for s in SECRETS]
PATTERNS_ = list(PATTERNS)
VALUES = {s.id: s.value for s in SECRETS}
SSN1 = f"exact_values[{EXACT.index('123-45-6789')}]"
NAME1 = f"exact_values[{EXACT.index('Jordan Q. Testperson')}]"
SSN_PATTERN = "patterns[0]"


def row(index: int, secret: str, loc: str = "T1", cls: str = "recoverable-hidden",
        where: str = "orphaned", exit_: int = 1, **extra: Any) -> dict[str, Any]:
    return {"index": index, "secret": secret, "loc": loc, "class": cls, "where": where,
            "cell": "orphaned.font", "expected_exit": exit_, "known_gap": False,
            "provisional": False, "override": None, **extra}


def truth(*instances: tuple[str, str, int]) -> dict[str, Any]:
    return {"instances": [{"secret": s, "written": w, "page": p} for s, w, p in instances]}


def finding(rule: str, storage: str = "orphaned", page: int | None = None,
            obj: int | None = 7) -> dict[str, Any]:
    return {"layer": "Objects", "rule": rule, "tier": "hard", "storage": storage,
            "page": page, "object": obj, "location": f"object {obj}"}


def warning(rule: str | None, storage: str | None = "orphaned", code: str = "W",
            page: int | None = None) -> dict[str, Any]:
    return {"code": code, "rule": rule, "storage": storage, "page": page}


def score(rows: list[dict[str, Any]], tr: dict[str, Any], findings: list[dict[str, Any]] = (),
          warnings: list[dict[str, Any]] = ()) -> list[rs.InstanceScore]:
    report = {"findings": list(findings), "warnings": list(warnings)}
    return rs.score_instances({"instances": rows}, tr, report, EXACT, PATTERNS_, VALUES)


def test_rules_for_names_the_exact_value_and_every_matching_pattern():
    assert rs.rules_for("123-45-6789", "123-45-6789", EXACT, PATTERNS_) == {SSN1, SSN_PATTERN}
    # The undashed variant matches neither pattern; only its exact value names it.
    ssn2 = f"exact_values[{EXACT.index('666-38-2914')}]"
    assert rs.rules_for("666-38-2914", "666382914", EXACT, PATTERNS_) == {ssn2}


def test_a_finding_with_the_rule_and_storage_is_a_hit():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "123-45-6789", 0)), [finding(SSN1)])
    assert s.outcome == rs.HIT


def test_a_pattern_finding_also_names_the_instance():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "123-45-6789", 0)), [finding(SSN_PATTERN)])
    assert s.outcome == rs.HIT


def test_a_finding_in_another_storage_does_not_name_it():
    # Found live, but the adjudicated leak is an orphaned stream: the
    # orphaned copy was missed even though the file exits 1.
    [s] = score([row(0, "SSN1")], truth(("SSN1", "123-45-6789", 0)), [finding(SSN1, "live")])
    assert s.outcome == rs.MISS


def test_unreferenced_names_an_orphaned_instance():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "123-45-6789", 0)),
                [finding(SSN1, "unreferenced")])
    assert s.outcome == rs.HIT


def test_a_finding_for_another_secret_does_not_name_it():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "123-45-6789", 0)), [finding(NAME1)])
    assert s.outcome == rs.MISS


def test_only_a_warning_where_a_finding_is_expected_is_a_downgrade():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "123-45-6789", 0)), [], [warning(SSN1)])
    assert s.outcome == rs.DOWNGRADED


def test_a_warning_is_a_hit_where_review_is_expected():
    [s] = score([row(0, "SSN1", exit_=2)], truth(("SSN1", "123-45-6789", 0)), [],
                [warning(SSN1)])
    assert s.outcome == rs.HIT


def test_a_warning_without_a_rule_names_nothing():
    [s] = score([row(0, "SSN1", exit_=2)], truth(("SSN1", "123-45-6789", 0)), [],
                [warning(None)])
    assert s.outcome == rs.MISS


def test_a_live_instance_needs_its_page():
    rows = [row(0, "SSN1", where="live")]
    tr = truth(("SSN1", "123-45-6789", 1))   # the truth counts pages from 0
    assert score(rows, tr, [finding(SSN1, "live", page=1)])[0].outcome == rs.MISS
    assert score(rows, tr, [finding(SSN1, "live", page=2)])[0].outcome == rs.HIT
    # A report entry that names no page still names it.
    assert score(rows, tr, [finding(SSN1, "live", page=None)])[0].outcome == rs.HIT


def test_one_entry_cannot_vouch_for_two_instances():
    rows = [row(0, "SSN1"), row(1, "SSN1")]
    tr = truth(("SSN1", "123-45-6789", 0), ("SSN1", "123-45-6789", 1))
    outcomes = sorted(s.outcome for s in score(rows, tr, [finding(SSN1, obj=7)]))
    assert outcomes == [rs.AMBIGUOUS, rs.HIT]


def test_two_entries_cover_two_instances_whatever_the_order():
    rows = [row(0, "SSN1"), row(1, "SSN1")]
    tr = truth(("SSN1", "123-45-6789", 0), ("SSN1", "123-45-6789", 1))
    found = [finding(SSN1, obj=7), finding(SSN1, obj=9)]
    assert [s.outcome for s in score(rows, tr, found)] == [rs.HIT, rs.HIT]


def test_the_matching_reassigns_to_cover_every_instance():
    # Entry A (no page) can name either instance; entry B (page 1) only
    # instance 0. A greedy pick of A for instance 0, tried first, would
    # leave instance 1 ambiguous; the matching moves instance 0 to B.
    rows = [row(0, "SSN1", where="live"), row(1, "SSN1", where="live")]
    tr = truth(("SSN1", "123-45-6789", 0), ("SSN1", "123-45-6789", 1))
    a = finding(SSN1, "live", page=None, obj=None)
    b = finding(SSN_PATTERN, "live", page=1, obj=None)
    scores = score(rows, tr, [a, b])
    assert scores[0].named_by[0] == rs._entry_id(a)    # A is tried first
    assert [s.outcome for s in scores] == [rs.HIT, rs.HIT]


def test_removed_rows_are_not_scored_and_overrides_apply():
    rows = [row(0, "SSN1", cls="removed"),
            row(1, "NAME1", override={"class": "partial", "cell": "orphaned.plain",
                                      "expected_exit": 1, "reason": "r"})]
    tr = truth(("SSN1", "123-45-6789", 0), ("NAME1", "Jordan Q. Testperson", 0))
    [s] = score(rows, tr, [finding(NAME1)])
    assert (s.secret, s.cls, s.cell, s.outcome) == ("NAME1", "partial", "orphaned.plain", rs.HIT)


def test_an_unattributed_row_uses_the_storage_its_cell_names():
    r = row(-1, "NAME1", where="unknown",
            override={"class": "recoverable-hidden", "cell": "leftover-xmp.plain",
                      "expected_exit": 1, "reason": "r"})
    assert rs.storage_of(r) == "orphaned"
    [s] = score([r], truth(), [finding(NAME1, "live")])
    assert s.outcome == rs.MISS
    [s] = score([r], truth(), [finding(NAME1, "orphaned")])
    assert s.outcome == rs.HIT


def test_an_unplaceable_storage_accepts_any_storage():
    r = row(-1, "NAME1", where="unknown",
            override={"class": "recoverable-hidden", "cell": "annot-fields.plain",
                      "expected_exit": 1, "reason": "r"})
    assert rs.storage_of(r) == "unknown"
    assert score([r], truth(), [finding(NAME1, "live")])[0].outcome == rs.HIT


def run_of(method: str, truth_: str, expected: int, exit_: int | None, *,
           control: bool = False, warnings: list[dict[str, Any]] = (),
           findings: list[dict[str, Any]] = (), rows: list[dict[str, Any]] = (),
           ocr: bool = True, error: Any = None) -> rs.FileRun:
    report = None if exit_ is None else {
        "exit_code": exit_, "error": error, "findings": list(findings),
        "warnings": list(warnings), "environment": {"ocr_available": ocr}}
    record = {"method": method, "stem": "X", "doc": "X", "sha256": "ab" * 32,
              "truth": truth_, "expected_exit": expected, "instances": list(rows)}
    result = CliResult(returncode=exit_, report=report, elapsed=0.1, timed_out=False,
                       stderr_tail="")
    return rs.FileRun(record, {"clean_control": control, "instances": []}, result)


def test_u3_counts_clean_m2_outputs_and_keeps_the_control_apart():
    runs = [run_of("m2", "clean", 0, 0), run_of("m2", "clean", 0, 2,
                                                  warnings=[warning(None, code="OCR_X")]),
            run_of("m2", "clean", 0, 0, control=True), run_of("m2", "leak", 1, 1),
            run_of("m1", "clean", 0, 0)]
    u = rs.u3(runs)
    assert (u["n"], u["exit_0"], u["exit_2"], u["share_exit_0"]) == (2, 1, 1, 0.5)
    assert u["exit_2_warning_codes"] == {"OCR_X": 1}
    assert u["clean_control_exits"] == {0: 1}


def test_summary_counts_file_and_instance_misses_per_method():
    leak_rows = [row(0, "SSN1", where="live")]
    m2_hit = run_of("m2", "leak", 1, 1, findings=[finding(SSN1, "live", page=None)],
                    rows=leak_rows)
    m2_silent = run_of("m2", "leak", 1, 0, rows=leak_rows)
    m1_other = run_of("m1", "leak", 1, 1, findings=[finding(NAME1, "live")], rows=leak_rows)
    for r in (m2_hit, m2_silent, m1_other):
        r.truth["instances"] = [{"secret": "SSN1", "written": "123-45-6789", "page": 0}]
    counts, detail = rs.summarise([m2_hit, m2_silent, m1_other], EXACT, PATTERNS_, VALUES)
    assert counts["m2"]["files"]["silent_miss"] == 1
    assert counts["m2"]["instances"][rs.HIT] == 1 and counts["m2"]["instances"][rs.MISS] == 1
    # m1 exits 1, but for another secret: the instance is still a miss.
    assert counts["m1"]["files"]["silent_miss"] == 0
    assert counts["m1"]["instances"][rs.MISS] == 1
    assert len(detail) == 3


def test_a_run_without_a_verdict_or_without_ocr_is_unmeasured():
    runs = [run_of("m2", "clean", 0, 0), run_of("m2", "clean", 0, None),
            run_of("m2", "clean", 0, 0, ocr=False),
            run_of("m2", "clean", 0, 2, error={"code": "PDF_UNREADABLE"})]
    problems = rs.unmeasured(runs)
    assert len(problems) == 3
    assert all("ab" * 6 in p for p in problems)


def test_the_counts_name_no_document():
    runs = [run_of("m2", "clean", 0, 0)]
    runs[0].record["stem"] = runs[0].record["doc"] = "D99-s0"
    counts, _ = rs.summarise(runs, EXACT, PATTERNS_, VALUES)
    assert "D99" not in rs.render(counts, "f" * 64)
    assert "D99" not in repr(counts)


def test_ensure_local_only_takes_the_redactor_set_boundary(tmp_path):
    inside = rs.REDACTOR_SET_DIR / "results"
    assert corpus.ensure_local_only(inside, boundary=rs.REDACTOR_SET_DIR) == inside
    with pytest.raises(SystemExit):
        corpus.ensure_local_only(tmp_path, boundary=rs.REDACTOR_SET_DIR)
    with pytest.raises(SystemExit):   # the real corpus is not the set's root
        corpus.ensure_local_only(corpus.REAL_CORPUS_DIR, boundary=rs.REDACTOR_SET_DIR)


def test_run_cli_passes_a_secrets_file_as_it_is(tmp_path, monkeypatch):
    from scorecard import runner

    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv

        class P:
            returncode, stderr = 0, ""
        return P()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    rules = tmp_path / "redact.yaml"
    rules.write_text("exact_values: []\n")
    runner.run_cli("py", Path("verify.py"), tmp_path / "x.pdf", [{"name": "n", "value": "v"}],
                   tmp_path / "w", "c", secrets_file=rules)
    assert seen["argv"][seen["argv"].index("--secrets") + 1] == str(rules)
    assert not list((tmp_path / "w").glob("*.rules.json"))


def test_the_redactor_set_root_is_gitignored():
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    assert "/eval/scorecard/redactor_set_local/" in gitignore.splitlines()


def test_a_changed_lock_needs_a_reason_and_is_kept(tmp_path):
    assert rs.check_lock_history(tmp_path, "a" * 64, "") is None
    assert rs.check_lock_history(tmp_path, "a" * 64, "") is None      # unchanged
    assert "lock changed" in rs.check_lock_history(tmp_path, "b" * 64, "")
    assert rs.check_lock_history(tmp_path, "b" * 64, "owner relabel, D07") is None
    import json
    history = json.loads((tmp_path / "results" / "lock-history.json").read_text())
    assert [h["reason"] for h in history] == ["", "", "owner relabel, D07"]
