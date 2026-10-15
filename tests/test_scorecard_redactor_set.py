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
SSN1 = f"exact_values[{EXACT.index('666-51-4073')}]"
NAME1 = f"exact_values[{EXACT.index('Jordan Q. Testperson')}]"
SSN_PATTERN = "patterns[0]"


def row(index: int, secret: str, loc: str = "T1", cls: str = "recoverable-hidden",
        where: str = "orphaned", exit_: int = 1, **extra: Any) -> dict[str, Any]:
    return {"index": index, "secret": secret, "loc": loc, "class": cls, "where": where,
            "cell": "orphaned.font", "expected_exit": exit_, "known_gap": False,
            "provisional": False, "override": None, **extra} if "cell" not in extra else {
        "index": index, "secret": secret, "loc": loc, "class": cls, "where": where,
        "expected_exit": exit_, "known_gap": False, "provisional": False, "override": None,
        **extra}


def truth(*instances: tuple[str, str, int], encoding: str = "page-text") -> dict[str, Any]:
    return {"instances": [{"secret": s, "written": w, "page": p, "encoding": encoding}
                          for s, w, p in instances]}


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
    assert rs.rules_for("666-51-4073", "666-51-4073", EXACT, PATTERNS_) == {SSN1, SSN_PATTERN}
    # The undashed variant matches neither pattern; only its exact value names it.
    ssn2 = f"exact_values[{EXACT.index('666-38-2914')}]"
    assert rs.rules_for("666-38-2914", "666382914", EXACT, PATTERNS_) == {ssn2}


def test_a_finding_with_the_rule_and_storage_is_a_hit():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "666-51-4073", 0)), [finding(SSN1)])
    assert s.outcome == rs.HIT


def test_a_pattern_finding_also_names_the_instance():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "666-51-4073", 0)), [finding(SSN_PATTERN)])
    assert s.outcome == rs.HIT


def test_a_finding_in_another_storage_does_not_name_it():
    # Found live, but the adjudicated leak is an orphaned stream: the
    # orphaned copy was missed even though the file exits 1.
    [s] = score([row(0, "SSN1")], truth(("SSN1", "666-51-4073", 0)), [finding(SSN1, "live")])
    assert s.outcome == rs.MISS


def test_unreferenced_names_an_orphaned_instance():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "666-51-4073", 0)),
                [finding(SSN1, "unreferenced")])
    assert s.outcome == rs.HIT


def test_a_finding_for_another_secret_does_not_name_it():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "666-51-4073", 0)), [finding(NAME1)])
    assert s.outcome == rs.MISS


def test_only_a_warning_where_a_finding_is_expected_is_a_downgrade():
    [s] = score([row(0, "SSN1")], truth(("SSN1", "666-51-4073", 0)), [], [warning(SSN1)])
    assert s.outcome == rs.DOWNGRADED


def test_a_warning_is_a_hit_where_review_is_expected():
    [s] = score([row(0, "SSN1", exit_=2)], truth(("SSN1", "666-51-4073", 0)), [],
                [warning(SSN1)])
    assert s.outcome == rs.HIT


def test_a_warning_without_a_rule_names_nothing_in_a_read_cell():
    [s] = score([row(0, "SSN1", exit_=2, cell="orphaned.plain")],
                truth(("SSN1", "666-51-4073", 0)), [], [warning(None)])
    assert s.outcome == rs.MISS


def test_a_live_instance_needs_its_page():
    rows = [row(0, "SSN1", where="live")]
    tr = truth(("SSN1", "666-51-4073", 1))   # the truth counts pages from 0
    assert score(rows, tr, [finding(SSN1, "live", page=1)])[0].outcome == rs.MISS
    assert score(rows, tr, [finding(SSN1, "live", page=2)])[0].outcome == rs.HIT
    # Drawn content needs an entry on its page.
    assert score(rows, tr, [finding(SSN1, "live", page=None)])[0].outcome == rs.MISS


def test_one_entry_cannot_vouch_for_two_instances():
    rows = [row(0, "SSN1"), row(1, "SSN1")]
    tr = truth(("SSN1", "666-51-4073", 0), ("SSN1", "666-51-4073", 1))
    outcomes = sorted(s.outcome for s in score(rows, tr, [finding(SSN1, obj=7)]))
    assert outcomes == [rs.AMBIGUOUS, rs.HIT]


def test_two_entries_cover_two_instances_whatever_the_order():
    rows = [row(0, "SSN1"), row(1, "SSN1")]
    tr = truth(("SSN1", "666-51-4073", 0), ("SSN1", "666-51-4073", 1))
    found = [finding(SSN1, obj=7), finding(SSN1, obj=9)]
    assert [s.outcome for s in score(rows, tr, found)] == [rs.HIT, rs.HIT]


def test_the_matching_reassigns_to_cover_every_instance():
    # Instance 0 (orphaned) can take either object's occurrence, instance
    # 1 only object 9's (object 7 holds NAME1's exact match, so SSN1 there
    # is only a pattern fit for... no: a pattern entry at a place where
    # another secret's exact rule fired is that secret's). Here: 7 has an
    # SSN1 exact finding, 9 too; both instances are covered whatever the
    # order the matching tries them in.
    rows = [row(0, "SSN1"), row(1, "SSN1")]
    tr = truth(("SSN1", "666-51-4073", 0), ("SSN1", "666-51-4073", 1))
    found = [finding(SSN_PATTERN, obj=7), finding(SSN1, obj=9)]
    assert [s.outcome for s in score(rows, tr, found)] == [rs.HIT, rs.HIT]


def test_a_pattern_finding_never_vouches_for_another_secret():
    # B1: only SSN2 survives, in object 7; the verifier reports its exact
    # rule and the shared SSN pattern. SSN1's orphaned instance is missed.
    ssn2 = f"exact_values[{EXACT.index('666-38-2914')}]"
    rows = [row(0, "SSN1"), row(1, "SSN2")]
    tr = truth(("SSN1", "666-51-4073", 0), ("SSN2", "666-38-2914", 0))
    found = [finding(ssn2, obj=7), finding(SSN_PATTERN, obj=7)]
    assert [s.outcome for s in score(rows, tr, found)] == [rs.MISS, rs.HIT]


def test_layers_reporting_one_occurrence_vouch_once():
    # B2: the same SSN drawn twice on page 1; one occurrence is found, by
    # both the Text and the OCR layer. One hit, one ambiguous.
    rows = [row(0, "SSN1", where="live"), row(1, "SSN1", where="live")]
    tr = truth(("SSN1", "666-51-4073", 0), ("SSN1", "666-51-4073", 0))
    text = {**finding(SSN1, "live", page=1, obj=None), "layer": "Text"}
    ocr = {**finding(SSN1, "live", page=1, obj=None), "layer": "OCR"}
    outcomes = sorted(s.outcome for s in score(rows, tr, [text, ocr]))
    assert outcomes == [rs.AMBIGUOUS, rs.HIT]


def test_drawn_content_is_not_vouched_for_by_an_entry_without_its_page():
    rows = [row(0, "SSN1", where="live")]
    tr = truth(("SSN1", "666-51-4073", 0))
    objects = finding(SSN1, "live", page=None, obj=12)
    assert score(rows, tr, [objects])[0].outcome == rs.MISS


def test_removed_rows_are_not_scored_and_overrides_apply():
    rows = [row(0, "SSN1", cls="removed"),
            row(1, "NAME1", override={"class": "partial", "cell": "orphaned.plain",
                                      "expected_exit": 1, "reason": "r"})]
    tr = truth(("SSN1", "666-51-4073", 0), ("NAME1", "Jordan Q. Testperson", 0))
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
            override={"class": "recoverable-hidden", "cell": "document.odd",
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
        r.truth["instances"] = [{"secret": "SSN1", "written": "666-51-4073", "page": 0}]
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
    assert all("output #" in p and "ab" * 6 not in p for p in problems)


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


def ev(obj: int) -> list[dict[str, Any]]:
    """Oracle evidence placing an instance in object *obj*."""
    return [{"channel": "R1", "where": f"content obj {obj} @100", "how": "whole",
             "storage": "orphaned", "counts": True}]


def place_warning(code: str, objects: str, storage: str = "orphaned") -> dict[str, Any]:
    return {"code": code, "rule": None, "storage": storage, "page": None,
            "message": f"Objects: 2 ORPHANED object(s) ...: objects {objects} — NOT scanned"}


def test_a_flagged_cell_is_caught_by_its_place_warning_under_the_owners_conditions():
    # Owner, 2026-10-15: in a ⚑ cell the place warning counts as caught
    # when its code fits the cell's column, the row expects exit 2, and the
    # row's object is in the warning's object list.
    rows = [row(0, "SSN1", exit_=2, evidence=ev(6)), row(1, "NAME1", exit_=2, evidence=ev(9))]
    tr = truth(("SSN1", "666-51-4073", 0), ("NAME1", "Jordan Q. Testperson", 0))
    text = place_warning("LEFTOVER_UNDECODABLE_TEXT", "6, 9")
    assert [s.outcome for s in score(rows, tr, [], [text])] == [rs.FLAGGED] * 2
    # Another object, another code, another storage, or no object known: not caught.
    assert score(rows[:1], tr, [], [place_warning("LEFTOVER_UNDECODABLE_TEXT", "12")]
                 )[0].outcome == rs.MISS
    assert score(rows[:1], tr, [], [place_warning("LEFTOVER_IMAGE", "6")])[0].outcome == rs.MISS
    assert score(rows[:1], tr, [], [place_warning("UNTERMINATED_STRING", "6")]
                 )[0].outcome == rs.MISS
    assert score(rows[:1], tr, [], [place_warning("LEFTOVER_UNDECODABLE_TEXT", "6", "live")]
                 )[0].outcome == rs.MISS
    assert score([row(0, "SSN1", exit_=2)], tr, [], [text])[0].outcome == rs.MISS
    # A row the adjudicator gave exit 1 is not caught by a review warning.
    assert score([row(0, "SSN1", exit_=1, evidence=ev(6))], tr, [], [text]
                 )[0].outcome == rs.MISS
    # A read (✓) cell never is.
    read = [row(0, "SSN1", exit_=2, cell="orphaned.plain", evidence=ev(6))]
    assert score(read, tr, [], [text])[0].outcome == rs.MISS


def test_a_flagged_cell_left_unmatched_falls_back_to_its_place_warning():
    # Two orphaned images hold NAME1; one value finding names an orphaned
    # object (it vouches for one), and the "unread images" warning lists
    # the other's object: that one is flagged, not ambiguous.
    rows = [row(0, "NAME1", loc="S2", where="pixels-orphaned", exit_=2, cell="orphaned.pixels",
                evidence=ev(8)),
            row(1, "NAME1", loc="S2", where="pixels-orphaned", exit_=2, cell="orphaned.pixels",
                evidence=ev(10))]
    tr = truth(("NAME1", "Jordan Q. Testperson", 0), ("NAME1", "Jordan Q. Testperson", 1),
               encoding="pixels")
    images = place_warning("LEFTOVER_IMAGE", "8, 10")
    outcomes = sorted(s.outcome for s in score(rows, tr, [finding(NAME1, obj=6)], [images]))
    assert outcomes == [rs.FLAGGED, rs.HIT]
    outcomes = sorted(s.outcome for s in score(rows, tr, [finding(NAME1, obj=6)], []))
    assert outcomes == [rs.AMBIGUOUS, rs.HIT]


KINDS = {s.id: s.kind for s in SECRETS}


def test_owner_accepted_name_gaps_are_listed_not_missed():
    # A reordered name (T2) and a name fragment (partial) the verifier
    # misses are listed gaps (owner, 2026-10-15); the same miss of an SSN
    # variant, or a hit name variant, is not.
    rows = [row(0, "NAME1", loc="T2", where="live", cell="live.font"),
            row(-1, "NAME1", loc="?", cls="partial", where="unknown",
                override={"class": "partial", "cell": "annot-fields.plain",
                          "expected_exit": 1, "reason": "r"}),
            row(1, "SSN2", loc="T2", where="live", cell="live.font")]
    tr = truth(("NAME1", "TESTPERSON, JORDAN Q", 0), ("SSN2", "666382914", 0))
    scores = rs.score_instances({"instances": rows}, tr, {"findings": [], "warnings": []},
                                EXACT, PATTERNS_, VALUES, KINDS)
    assert [(s.outcome, s.gap) for s in scores] == [
        (rs.LISTED_GAP, "name-variant"), (rs.LISTED_GAP, "name-fragment"), (rs.MISS, None)]
    hit = rs.score_instances({"instances": rows[:1]}, tr,
                             {"findings": [finding(NAME1, "live", page=1)], "warnings": []},
                             EXACT, PATTERNS_, VALUES, KINDS)
    assert hit[0].outcome == rs.HIT and hit[0].gap is None


def _main_args(tmp_path, **kw):
    import argparse

    rules = tmp_path / "redact.yaml"
    rules.write_text("exact_values: []\npatterns: []\n")
    return argparse.Namespace(root=tmp_path, allow_outside=True, rules=rules, set="inputs",
                              timeout=5.0, workers=1, seed=[0], json=tmp_path / "out.json",
                              lock_change_reason="", **kw)


def test_an_invalid_run_writes_no_json_and_no_lock_history(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "load_locked", lambda root, s: [])
    monkeypatch.setattr(rs, "lock_sha", lambda root, s: "c" * 64)
    monkeypatch.setattr(rs, "run", lambda *a, **k: [run_of("m2", "clean", 0, None)])
    assert rs.main(_main_args(tmp_path)) == 1
    assert not (tmp_path / "out.json").exists()
    assert not (tmp_path / "results" / "lock-history.json").exists()
    monkeypatch.setattr(rs, "run", lambda *a, **k: [run_of("m2", "clean", 0, 0)])
    assert rs.main(_main_args(tmp_path)) == 0
    import json
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["valid"] is True and out["seeds"] == [0] and out["lock_sha256"] == "c" * 64
    assert len(json.loads((tmp_path / "results" / "lock-history.json").read_text())) == 1


def test_a_lock_that_does_not_verify_is_refused_before_anything_is_recorded(tmp_path,
                                                                            monkeypatch):
    def refuse(root, s):
        raise SystemExit("adjudication lock does not verify")
    monkeypatch.setattr(rs, "load_locked", refuse)
    with pytest.raises(SystemExit):
        rs.main(_main_args(tmp_path, ))
    assert not (tmp_path / "results").exists()
