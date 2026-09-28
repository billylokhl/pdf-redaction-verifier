"""The 3a-6 inventory agreement gate (eval/scorecard/inventory_gate.py)
and its oracle (eval/scorecard/inventory.py): every check the oracle
names fires; the gate fails on an unflagged disagreement, a crash, a
timeout or an unverified file, refuses a drifted corpus, and its
aggregate output never carries a file name, path, SHA-256, document
string or reader message. Every file here is generated in tmp_path;
fabricated content only."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from scorecard import cli
from scorecard import corpus as corpus_mod
from scorecard import inventory as oracle
from scorecard import inventory_gate as gate
from scorecard.inventory import Check, Status, compare, scrub
from scorecard.pdfgen import Spec, Update, build_pdf

from redaction_verifier.ledger import FlagReason, UnitKind

from .conftest import requires_qpdf

PLANTED = "PLANTED-FABRICATED-7Q"
SSN = "123-45-6789"
OBJSTM = Spec(xref="stream", objstm=True, updates=(Update("stream", objstm=True),))


# ── The oracle: each check, by name ───────────────────────────────────────
@requires_qpdf
def test_agreement_and_flags_are_reported(tmp_path: Path) -> None:
    found = compare(build_pdf(OBJSTM), tmp_path)
    assert found.status is Status.AGREES and found.check is None
    assert found.streams_compared > 0 and found.members_compared > 0
    flagged = compare(build_pdf(Spec(junk=b"junk")), tmp_path)
    assert flagged.flagged and flagged.reasons == (FlagReason.UNINDEXED_NON_WHITESPACE,)


@requires_qpdf
def test_each_reader_disagreement_names_its_check(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = build_pdf(OBJSTM)

    def check_with(target: object, name: str, value: object) -> Check | None:
        with monkeypatch.context() as patch:
            patch.setattr(target, name, value)
            found = compare(data, tmp_path)
        assert found.disagrees, found
        return found.check

    assert check_with(pymupdf.Document, "xref_stream_raw",
                      lambda self, n: b"tampered") is Check.STREAM_DATA
    assert check_with(pymupdf.Document, "xref_object",
                      lambda self, n, compressed=False: "<< /Other 1 >>") is Check.MEMBER_VALUE
    assert check_with(oracle, "qpdf_map", lambda path: ({}, 0)) is Check.OBJECT_SET
    assert check_with(oracle, "qpdf_map", lambda path: ({}, 2)) is Check.OBJECT_SET
    assert check_with(oracle, "qpdf_warnings",
                      lambda path: [b"WARNING: x.pdf: a structural problem"]) is Check.QPDF_CHECK
    assert check_with(oracle, "_mupdf_templates", lambda: ("warning",)) is Check.MUPDF_WARNING

    def times_out(path: Path) -> list[bytes]:
        raise subprocess.TimeoutExpired(["qpdf"], 60)
    assert check_with(oracle, "qpdf_warnings", times_out) is Check.ORACLE_ERROR


@requires_qpdf
def test_dead_bodies_and_tiling_are_checked(tmp_path: Path) -> None:
    data = build_pdf(Spec(dead=True))
    inv = oracle.inventory(data)
    assert compare(data, tmp_path, inv=inv).agrees
    hidden = replace(inv, units=tuple(u for u in inv.units
                                      if u.ref.kind is not UnitKind.DEAD_BODY))
    assert compare(data, tmp_path, inv=hidden).check is Check.DEAD_BODIES
    # Not tiling is a disagreement even for a flagged file.
    flagged = oracle.inventory(build_pdf(Spec(junk=b"junk")))
    assert compare(data, tmp_path, inv=replace(flagged, tiles=False)).check is Check.TILING


def test_a_reader_message_keeps_its_shape_not_its_content() -> None:
    line = (f"WARNING: /tmp/{PLANTED}/r0.pdf (object 5 0, offset 12): unknown token "
            f"{PLANTED} (SSN {SSN}) /Secret{PLANTED} 'Quoted' <414243> [1 2] while reading")
    template = scrub(line.encode())
    assert template.startswith("WARNING: (_) unknown token") and "while reading" in template
    for needle in (PLANTED, SSN, "Secret", "Quoted", "414243", "tmp"):
        assert needle not in template


# ── The gate over a tmp corpus ────────────────────────────────────────────
def _corpus(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, Path]:
    root = tmp_path / f"corpus-{PLANTED}"
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    manifest = tmp_path / "manifest.json"
    corpus_mod.build_manifest(root, manifest_path=manifest)
    return root, manifest


def _run(root: Path, manifest: Path, tmp_path: Path, *extra: str) -> int:
    return cli.main(["inventory", "run", "--root", str(root), "--manifest", str(manifest),
                     "--json", str(tmp_path / "aggregate.json"),
                     "--detail", str(tmp_path / "detail.jsonl"), "--allow-outside", *extra])


@requires_qpdf
def test_the_gate_reports_aggregates_only(tmp_path: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        f"agree-{PLANTED}-SSN-{SSN}.pdf": build_pdf(OBJSTM),
        f"flagged-{PLANTED}.pdf": build_pdf(Spec(junk=f"({PLANTED} {SSN})".encode())),
        f"dead-{PLANTED}.pdf": build_pdf(Spec(dead=True, comment=True, updates=(Update(),))),
    }
    root, manifest = _corpus(tmp_path, files)
    assert _run(root, manifest, tmp_path) == 0
    captured = capsys.readouterr()
    shared = captured.out + captured.err + (tmp_path / "aggregate.json").read_text()
    agg = json.loads((tmp_path / "aggregate.json").read_text())
    assert (agg["files"], agg["unflagged_agree"], agg["flagged"]) == (3, 2, 1)
    assert agg["gate"] == {"passed": True, "unflagged_disagree": 0, "crashes": 0,
                           "timeouts": 0, "unverified": 0, "inconsistent": 0}
    assert agg["flags"]["files_by_reason"] == {"unindexed_non_whitespace": 1}
    assert agg["pending_decisions"]["comment_lines"]["files_after_eof"] == 1
    entries = corpus_mod.load_manifest(manifest)
    needles = [PLANTED, SSN, str(root), str(tmp_path), *files,
               *(e.sha256 for e in entries), *(e.sha256[:16] for e in entries)]
    for needle in needles:
        assert needle not in shared, needle
    # The local-only detail is keyed by SHA-256; no path, no document string.
    detail = (tmp_path / "detail.jsonl").read_text()
    assert {json.loads(line)["sha256"] for line in detail.splitlines()} == {
        e.sha256 for e in entries}
    for needle in (PLANTED, SSN, str(root), *files):
        assert needle not in detail, needle


def _wrapper(tmp_path: Path, body: str) -> tuple[str, ...]:
    """An oracle child with *body* run first: an injected disagreement."""
    path = tmp_path / "wrapped_oracle.py"
    path.write_text("import sys\nimport scorecard.inventory as o\n" + body
                    + "\nsys.exit(o.main(sys.argv[1:]))\n")
    return (sys.executable, str(path))


@requires_qpdf
def test_an_injected_disagreement_fails_the_gate_without_leaking(tmp_path: Path) -> None:
    root = tmp_path / "files"
    root.mkdir()
    (root / "a.pdf").write_bytes(build_pdf(Spec()))
    (root / "b.pdf").write_bytes(build_pdf(Spec(junk=b"junk")))
    message = f"WARNING: x.pdf (object 1 0, offset 9): bad {PLANTED} (SSN {SSN}) /N{PLANTED}"
    command = _wrapper(tmp_path, f"o.qpdf_warnings = lambda path: [{message.encode()!r}]")
    jobs = [gate.Job(name, root / name) for name in ("a.pdf", "b.pdf")]
    results = gate.run_gate(jobs, workdir=tmp_path, oracle_command=command)
    assert [r.verdict for r in results] == [gate.DISAGREE, gate.FLAGGED]
    agg = gate.aggregate(results)
    assert agg["gate"]["passed"] is False and agg["gate"]["unflagged_disagree"] == 1
    assert agg["unflagged_disagree_by_check"] == {"qpdf_check": 1}
    text = json.dumps(agg) + gate.render(agg)
    assert PLANTED not in text and SSN not in text and "a.pdf" not in text
    gate.write_detail(results, tmp_path / "detail.jsonl")
    detail = (tmp_path / "detail.jsonl").read_text()
    assert "qpdf_check" in detail and "bad" in detail
    assert PLANTED not in detail and SSN not in detail


def test_a_crash_and_a_timeout_fail_the_gate(tmp_path: Path) -> None:
    path = tmp_path / "f.pdf"
    path.write_bytes(build_pdf(Spec()))
    job = gate.Job("0" * 64, path)
    crash = gate.run_file(job, workdir=tmp_path, build_command=(
        sys.executable, "-c", f"raise ValueError('{PLANTED}')"))
    assert (crash.verdict, crash.build.error) == (gate.CRASH, "ValueError")
    exited = gate.run_file(job, workdir=tmp_path,
                           build_command=(sys.executable, "-c", "import sys; sys.exit(3)"))
    assert (exited.verdict, exited.build.error) == (gate.CRASH, "exit 3")
    slow = gate.run_file(job, workdir=tmp_path, build_timeout=0.5,
                         build_command=(sys.executable, "-c", "import time; time.sleep(60)"))
    assert slow.verdict == gate.TIMEOUT and slow.build.elapsed < 30
    agg = gate.aggregate([crash, exited, slow])
    assert (agg["gate"]["passed"], agg["gate"]["crashes"], agg["gate"]["timeouts"]) == (
        False, 2, 1)
    assert PLANTED not in json.dumps(agg)


def test_an_oracle_that_cannot_finish_never_counts_as_agreement(tmp_path: Path) -> None:
    clean, flagged = tmp_path / "clean.pdf", tmp_path / "flagged.pdf"
    clean.write_bytes(build_pdf(Spec()))
    flagged.write_bytes(build_pdf(Spec(junk=b"junk")))
    sleeper = (sys.executable, "-c", "import time; time.sleep(60)")
    results = [gate.run_file(gate.Job(p.name, p), workdir=tmp_path, oracle_timeout=0.5,
                             oracle_command=sleeper) for p in (clean, flagged)]
    assert [r.verdict for r in results] == [gate.UNVERIFIED, gate.FLAGGED]
    agg = gate.aggregate(results)
    assert agg["gate"]["passed"] is False and agg["gate"]["unverified"] == 1
    assert agg["oracle"]["timeouts"] == 2 and agg["oracle"]["failed_on_flagged"] == 1


def test_children_that_disagree_on_flags_are_inconsistent() -> None:
    build = gate.ChildRun("ok", 0.1, {"flags": {}, "tiles": True, "work": 1})
    said_flagged = {"agreement": {"status": "flagged", "reasons": ["xref_tail"], "check": None,
                                  "revision": None, "obj": None, "templates": [],
                                  "encrypted": False, "streams_compared": 0,
                                  "members_compared": 0},
                    "flags": ["xref_tail"], "measures": {}, "regions": {}}
    job = gate.Job("0" * 64, Path("unused"))
    result = gate.judge(job, 10, build, gate.ChildRun("ok", 0.1, said_flagged))
    assert result.verdict == gate.INCONSISTENT
    garbled = gate.judge(job, 10, build, gate.ChildRun("ok", 0.1, {"agreement": 1}))
    assert garbled.verdict == gate.UNVERIFIED
    assert gate.aggregate([result, garbled])["gate"]["passed"] is False
    assert gate.aggregate([])["gate"]["passed"] is False  # 0 files is never a pass


def test_a_drifted_corpus_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    names = [f"{PLANTED}-{i}.pdf" for i in range(3)]
    root, manifest = _corpus(tmp_path, {name: build_pdf(Spec(pages=1 + i))
                                        for i, name in enumerate(names)})
    (root / names[0]).write_bytes(build_pdf(Spec(pages=3)))  # changed
    (root / names[1]).unlink()                               # missing
    assert _run(root, manifest, tmp_path) == 2
    err = capsys.readouterr().err
    assert "1 missing, 1 changed" in err
    assert PLANTED not in err and str(root) not in err
    assert not (tmp_path / "aggregate.json").exists()
    empty = tmp_path / "empty.json"
    empty.write_text('{"entries": []}')
    assert _run(root, empty, tmp_path) == 2


def test_the_detail_file_is_local_only(tmp_path: Path) -> None:
    root, manifest = _corpus(tmp_path, {"a.pdf": build_pdf(Spec())})
    with pytest.raises(SystemExit, match="outside the local-only"):
        cli.main(["inventory", "run", "--root", str(root), "--manifest", str(manifest),
                  "--detail", str(tmp_path / "detail.jsonl")])


@requires_qpdf
def test_the_fuzz_gate_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["inventory", "fuzz", "--count", "6", "--seed", "3",
                     "--json", str(tmp_path / "agg.json"),
                     "--detail", str(tmp_path / "detail.jsonl"), "--allow-outside"]) == 0
    agg = json.loads((tmp_path / "agg.json").read_text())
    assert agg["files"] == 6 and agg["gate"]["passed"]
    assert agg["unflagged_agree"] + agg["flagged"] == 6


def test_a_harness_failure_is_a_crash_not_a_pass(tmp_path: Path) -> None:
    missing = gate.run_file(gate.Job("0" * 64, tmp_path / f"{PLANTED}.pdf"), workdir=tmp_path)
    assert (missing.verdict, missing.build.error) == (gate.CRASH, "harness FileNotFoundError")


@requires_qpdf
@pytest.mark.parametrize("probe", [b"<< /D << /  true >> >>", b"<< /L [/ 2.5 /n <41>] >>"])
def test_an_empty_name_member_agrees(tmp_path: Path, probe: bytes) -> None:
    # Found by the fuzz gate: MuPDF's tight printer writes `/ 2.5` as `/2.5`
    # (one name) though MuPDF reads two tokens, as qpdf and we do. The
    # oracle compares MuPDF's pretty print, which separates every token.
    from scorecard import pdfgen
    data = build_pdf(Spec(xref="stream", objstm=True, probe=True,
                          mutate=lambda packed: packed.replace(pdfgen.PROBE, probe.ljust(
                              len(pdfgen.PROBE)))))
    assert compare(data, tmp_path).agrees


# ── The pending-decision measurements ─────────────────────────────────────
def _ambiguous_length(new: bytes) -> bytes:
    """Stream 4's /Length is 5 0 R (12); revision 0 rewrites object 5."""
    from scorecard.pdfgen import Writer
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[1] = (1, w.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"), 0)
    table[2] = (1, w.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), 0)
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9]"
                          b" /Resources << >> /Contents 4 0 R >>"), 0)
    table[4] = (1, w.obj(4, b"<< /Length 5 0 R >>\nstream\nBT (x) Tj ET\nendstream"), 0)
    table[5] = (1, w.obj(5, b"12"), 0)
    section = w.table(table, b"/Size 6 /Root 1 0 R")
    w.epilogue(section)
    w.epilogue(w.table({5: (1, w.obj(5, new), 0)}, b"/Size 6 /Root 1 0 R /Prev %d" % section))
    return bytes(w.out)


def _measured(data: bytes) -> dict[str, int]:
    return oracle.measure(data, oracle.inventory(data))


def test_revision_ambiguous_is_measured_equal_or_not() -> None:
    assert (_measured(_ambiguous_length(b"12"))["ambiguous_equal"],
            _measured(_ambiguous_length(b"11"))["ambiguous_differ"]) == (1, 1)


def test_a_length_in_an_object_stream_and_a_dead_object_stream_are_counted() -> None:
    from scorecard.pdfgen import Writer
    w = Writer()
    table: dict[int, tuple[int, int, int]] = {0: (0, 0, 65535)}
    table[3] = (1, w.obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9]"
                          b" /Resources << >> /Contents 5 0 R >>"), 0)
    table[5] = (1, w.obj(5, b"<< /Length 6 0 R >>\nstream\nBT (x) Tj ET\nendstream"), 0)
    table[4] = (1, w.objstm(4, [(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
                                (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"), (6, b"12")]), 0)
    w.objstm(9, [(10, b"(dead)")])  # listed by no xref: a dead object stream
    table |= {1: (2, 4, 0), 2: (2, 4, 1), 6: (2, 4, 2)}
    w.epilogue(w.xref_stream(7, table, b"/Size 8 /Root 1 0 R"))
    measured = _measured(bytes(w.out))
    assert (measured["length_in_objstm"], measured["dead_objstms"]) == (1, 1)


@requires_qpdf
def test_an_updated_linearized_file_is_measured(tmp_path: Path) -> None:
    doc = pymupdf.open()
    for i in range(2):
        doc.new_page().insert_text((50, 50), f"page {i}")
    doc.save(tmp_path / "in.pdf")
    subprocess.run(["qpdf", "--linearize", str(tmp_path / "in.pdf"), str(tmp_path / "lin.pdf")],
                   check=True)
    linear = (tmp_path / "lin.pdf").read_bytes()
    assert (_measured(linear)["linearized"], _measured(linear)["linearized_updated"]) == (1, 0)
    doc = pymupdf.open(tmp_path / "lin.pdf")
    doc[0].insert_text((50, 80), "update")
    doc.save(tmp_path / "lin.pdf", incremental=True, encryption=0)
    updated = (tmp_path / "lin.pdf").read_bytes()
    assert _measured(updated)["linearized_updated"] == 1
    assert oracle.inventory(updated).flags  # flagged today (#44 item 1)
