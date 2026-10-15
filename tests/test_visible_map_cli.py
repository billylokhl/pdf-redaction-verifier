"""--visible-map at the CLI: the flag changes nothing when it is off and no
verdict when it is on; a fault in the map costs only the map; the output
file is only ever a new file or one of this tool's own maps.

"Identical" below means: the exit code, every byte of stdout, and the
--json report (findings with their locations, warnings, verdict). The only
differences the flag may make are a stderr line about the map and the
documented pre-scan refusals (MAP_PATH_CONFLICT / MAP_PATH_UNWRITABLE,
exit 2 before scanning).
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pymupdf as fitz
import pytest

import verify
from redaction_verifier.visible_map import paths, runner
from redaction_verifier.visible_map.sink import MapSink

from .conftest import FULL_ENV, HAS_OCR, REPO_ROOT, requires_full_env
from .visible_map_support import SSN, SSN_RULES, map_text, write_rules
from .visible_map_support import CliRun as Run
from .visible_map_support import run_cli as run_main

RUN_ALL = os.environ.get("RUN_MAP_IDENTITY") == "1"


def _leaky(tmp_path: Path, name: str = "leaky.pdf") -> Path:
    """A leak in the Text and Hidden layers, plus a clean second page."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), f"SSN {SSN}")
    sq = page.add_rect_annot(fitz.Rect(100, 300, 200, 350))
    sq.set_info(content=f"note {SSN}")
    sq.update()
    doc.new_page().insert_text((72, 72), "Nothing here")
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


def _same(a: Run, b: Run) -> None:
    assert a.code == b.code
    assert a.stdout == b.stdout
    assert a.report == b.report


def _no_map(run: Run) -> None:
    assert not run.out.exists()
    assert not [p for p in run.out.parent.iterdir() if p.name.startswith(f".{run.out.name}.")]


def _baseline(monkeypatch, target: Path, rules: Path, *extra: str, ocr: bool = False) -> Run:
    return run_main(monkeypatch, target, rules, *extra, flag=False, ocr=ocr)


def _marked_map(path: Path) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "an old map", fontname="helv")
    data = paths.insert_markers(doc.tobytes(garbage=4, deflate=True))
    path.write_bytes(data)
    return data


# ── The flag off, and on (design §9 test 1) ───────────────────────────
def test_flag_off_never_evaluates_the_map(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(verify, "_offer", lambda *a: calls.append(a))
    monkeypatch.setattr(verify, "MapSink", lambda: pytest.fail("a sink was made"))
    pdf = _leaky(tmp_path)
    run = _baseline(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
    assert run.code == 1 and calls == []


def _identity_cases() -> list[str]:
    from caselib import REGISTRY, load
    from caselib.cells import parts
    from caselib.run import available

    load()
    have = available()
    picked: dict[tuple[str, str, str], str] = {}
    for case_id, case in sorted(REGISTRY.items()):
        if case.perf or case.requires - have or (case.grid and not RUN_ALL):
            continue
        if RUN_ALL:
            picked[(case_id, "", "")] = case_id
            continue
        row = parts(case.cells[0])[0] if case.cells else "-"
        picked.setdefault((case_id.split(".")[0], row, case.truth), case_id)
    return sorted(picked.values())


@pytest.mark.parametrize("case_id", _identity_cases())
def test_flag_on_changes_no_verdict_on_the_case_library(case_id, tmp_path, monkeypatch) -> None:
    """A representative of every (family, row, truth) cell of the case
    library (RUN_MAP_IDENTITY=1: every buildable case)."""
    from caselib import REGISTRY
    from caselib.run import build

    case = REGISTRY[case_id]
    pdf = build(case, tmp_path / f"{case_id}.pdf")
    rules = write_rules(tmp_path, list(case.rules))
    off = _baseline(monkeypatch, pdf, rules, ocr=HAS_OCR)
    on = run_main(monkeypatch, pdf, rules, ocr=HAS_OCR)
    _same(off, on)
    assert on.written and "[MAP] MAP_WRITTEN" in on.stderr, on.stderr
    data = on.out.read_bytes()
    assert paths.markers_in(data[:1024], data[-64:]) == (True, True)


def _local_corpus() -> list:
    from .test_corpus import REAL_CORPUS_DIR

    pdfs = sorted(REAL_CORPUS_DIR.glob("*.pdf")) if REAL_CORPUS_DIR.is_dir() else []
    return pdfs or [pytest.param(None, marks=pytest.mark.skip(
        reason="no real vendor PDFs in tests/corpus_pdfs/"))]


@pytest.mark.parametrize("pdf", _local_corpus(), ids=lambda p: "" if p is None else "real")
def test_flag_on_changes_no_verdict_on_the_local_corpus(pdf, tmp_path, monkeypatch) -> None:
    """The same over tests/corpus_pdfs/ (git-ignored real files, when
    present), with each file's own sidecar as its rules."""
    sidecar = pdf.with_suffix(".pdf.secrets.json")
    pairs = json.loads(sidecar.read_text()) if sidecar.exists() else [["ssn", SSN]]
    rules = write_rules(tmp_path, [{"name": name, "value": value} for name, value in pairs])
    target = tmp_path / "target.pdf"
    target.write_bytes(pdf.read_bytes())
    _same(_baseline(monkeypatch, target, rules, ocr=HAS_OCR),
          run_main(monkeypatch, target, rules, ocr=HAS_OCR))


def test_flag_on_changes_no_verdict_with_fail_fast(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    _same(_baseline(monkeypatch, pdf, rules, "--fail-fast"),
          run_main(monkeypatch, pdf, rules, "--fail-fast"))


def test_hidden_item_locations_are_unchanged(tmp_path) -> None:
    """HiddenItem's new fields are plain values beside an unchanged
    location string."""
    pdf = _leaky(tmp_path)
    doc = fitz.open(pdf)
    items = [i for i in verify._hidden_objects(doc) if isinstance(i, verify.HiddenItem)]
    assert [i.location for i in items] == [
        f"annotation {items[0].xref} on page 1 (content)"]
    assert (items[0].page, items[0].kind) == (0, "annotation:content")


# ── Faults: the map's own failures cost only the map (test 2) ─────────
# Every fault runs against a clean target (exit 0: needs OCR, exiftool and
# qpdf), a degraded one (exit 2: OCR off) and a leaky one (exit 1), so a
# map failure that moved any exit code -- 0 up to 2, or 2 down to 0 --
# fails here.
TARGETS = [
    pytest.param("clean", marks=pytest.mark.skipif(not FULL_ENV, reason=(
        "a certifiable clean run needs Apple Vision, exiftool and qpdf"))),
    "degraded",
    "leaky",
]
EXPECTED_EXIT = {"clean": 0, "degraded": 2, "leaky": 1}


def _target(tmp_path: Path, kind: str) -> tuple[Path, bool]:
    """(the PDF, whether OCR runs) for a run expected to exit 0, 2 or 1."""
    if kind == "leaky":
        return _leaky(tmp_path), False
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Nothing to see here")
    path = tmp_path / f"{kind}.pdf"
    doc.save(path)
    doc.close()
    return path, kind == "clean"


def _fault_run(tmp_path: Path, monkeypatch, kind: str, inject) -> Run:
    pdf, ocr = _target(tmp_path, kind)
    rules = write_rules(tmp_path, SSN_RULES)
    base = _baseline(monkeypatch, pdf, rules, ocr=ocr)
    assert base.code == EXPECTED_EXIT[kind]
    inject()
    run = run_main(monkeypatch, pdf, rules, ocr=ocr)
    _same(base, run)
    return run


@pytest.mark.parametrize("kind", TARGETS)
@pytest.mark.parametrize("target, name", [
    (runner.MapJob, "make"),
    (runner.MapJob, "_open_target"),
    (runner.MapJob, "_check_published_bytes"),
    (runner, "rules_header"),
    (runner, "child_sys_path"),
    (paths, "publish"),
    (MapSink, "to_header"),
    (MapSink, "take"),
])
def test_a_raising_map_accessor_changes_nothing(target, name, kind, tmp_path,
                                                monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("injected")

    run = _fault_run(tmp_path, monkeypatch, kind, lambda: monkeypatch.setattr(target, name, boom))
    if name != "take":          # a failed hand-over still maps, as incomplete
        _no_map(run)
        assert "visible map not written" in run.stderr
    else:
        assert "the scan's hand-over to the map failed" in map_text(run.out)


@pytest.mark.parametrize("kind", TARGETS)
def test_a_mutating_sink_changes_nothing(kind, tmp_path, monkeypatch) -> None:
    original = MapSink.take

    def mutate(self, offered, refs):
        original(self, offered, refs)
        for ref in refs:
            if isinstance(ref, list):
                ref.clear()

    _fault_run(tmp_path, monkeypatch, kind, lambda: monkeypatch.setattr(MapSink, "take", mutate))


@pytest.mark.parametrize("kind", TARGETS)
@pytest.mark.parametrize("raiser", ["make", "make+abort"])
def test_ctrl_c_during_the_map_changes_nothing(raiser, kind, tmp_path, monkeypatch) -> None:
    def interrupt(*a, **k):
        raise KeyboardInterrupt

    def inject():
        monkeypatch.setattr(runner.MapJob, "make", interrupt)
        if raiser == "make+abort":
            monkeypatch.setattr(runner.MapJob, "abort", interrupt)

    _no_map(_fault_run(tmp_path, monkeypatch, kind, inject))


CHILDREN = {
    "segfault": "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n",
    "hang": "import time\ntime.sleep(600)\n",
    "stderr-flood": "import sys\nfor _ in range(4000):\n    sys.stderr.write('x' * 65536)\n",
    "killed-after-writing": (
        "import json, os, signal, sys\nsys.stdin.buffer.read()\nout = sys.stdout.buffer\n"
        "out.write(json.dumps({'ok': True, 'length': 100000, 'map_sha256': '0' * 64, "
        "'target_sha256': '0' * 64}).encode() + b'\\n')\n"
        "out.write(b'%PDF' * 1000)\nout.flush()\nos.kill(os.getpid(), signal.SIGKILL)\n"),
    "writes-forever": (
        "import json, sys\nsys.stdin.buffer.read()\nout = sys.stdout.buffer\n"
        "out.write(json.dumps({'ok': True, 'length': 1000, 'map_sha256': '0' * 64, "
        "'target_sha256': '0' * 64}).encode() + b'\\n')\n"
        "while True:\n    out.write(b'x' * 65536)\n"),
    "no-record-line": "import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536)\n",
    # A newline does come, after 64 KB: the 4 KB record cap holds even when
    # the whole line arrives in one chunk.
    "long-record-line": (
        "import sys\nsys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(b'{' + b' ' * 65536 + b'}\\n')\n"),
    "huge-length": (
        "import json, sys\nsys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write(json.dumps({'ok': True, 'length': 1 << 40, "
        "'map_sha256': '0' * 64, 'target_sha256': '0' * 64}).encode() + b'\\n')\n"),
    "never-reads": "import time\ntime.sleep(600)\n",
}


@pytest.mark.parametrize("kind", TARGETS)
@pytest.mark.parametrize("child", sorted(CHILDREN))
def test_a_failing_child_costs_only_the_map(child, kind, tmp_path, monkeypatch) -> None:
    def inject():
        monkeypatch.setattr(runner, "BOOTSTRAP", CHILDREN[child])
        monkeypatch.setattr(runner, "TIMEOUT_BASE_S", 3.0)
        monkeypatch.setattr(runner, "TIMEOUT_PER_PAGE_S", 0.0)
        if child == "never-reads":
            # A header larger than any pipe buffer, to a child that never reads.
            monkeypatch.setattr(runner, "child_sys_path", lambda: ["/nonexistent"] * 50000)

    started = time.monotonic()
    run = _fault_run(tmp_path, monkeypatch, kind, inject)
    assert time.monotonic() - started < 60
    _no_map(run)
    assert "visible map not written" in run.stderr


@requires_full_env
def test_a_json_report_that_cannot_be_written_changes_nothing(tmp_path, monkeypatch) -> None:
    """The one exit code main() itself moves (0 to 2: the --json report
    could not be written) moves the same with the flag on."""
    pdf, _ = _target(tmp_path, "clean")
    rules = write_rules(tmp_path, SSN_RULES)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        runs = [run_main(monkeypatch, pdf, rules, "--json", str(locked / "r.json"), ocr=True,
                         flag=flag, json_report=False) for flag in (False, True)]
    finally:
        locked.chmod(0o700)
    off, on = runs
    assert off.code == on.code == 2
    assert off.stdout == on.stdout
    assert "JSON report not written (exit 2)" in map_text(on.out)


# ── The output file (test 3) ──────────────────────────────────────────
@pytest.mark.parametrize("path_kind", ["JSON_PATH_CONFLICT", "JSON_PATH_UNWRITABLE",
                                       "TARGET_NOT_FOUND", "RULES_INVALID", "PDF_UNREADABLE",
                                       "PDF_PASSWORD", "INTERNAL_ERROR", "MAP_CHILD_FAILED"])
def test_a_stale_map_never_survives_a_run(path_kind, tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    out = tmp_path / "out.pdf"
    _marked_map(out)
    extra: list[str] = []
    if path_kind == "JSON_PATH_CONFLICT":
        extra = ["--json", str(rules)]
    elif path_kind == "JSON_PATH_UNWRITABLE":
        extra = ["--json", str(_unremovable_report(tmp_path))]
    elif path_kind == "TARGET_NOT_FOUND":
        pdf = tmp_path / "missing.pdf"
    elif path_kind == "RULES_INVALID":
        rules.write_text("not json")
    elif path_kind == "PDF_UNREADABLE":
        pdf.write_bytes(b"not a pdf at all")
    elif path_kind == "PDF_PASSWORD":
        doc = fitz.open(pdf)
        doc.save(tmp_path / "locked.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw",
                 owner_pw="owner")
        pdf = tmp_path / "locked.pdf"
    elif path_kind == "INTERNAL_ERROR":
        monkeypatch.setattr(verify, "print_report", lambda *a, **k: 1 / 0)
    else:
        monkeypatch.setattr(runner, "BOOTSTRAP", "raise SystemExit(3)\n")
    try:
        run = run_main(monkeypatch, pdf, rules, *extra, out=out, json_report=not extra)
    finally:
        if (tmp_path / "locked").exists():
            (tmp_path / "locked").chmod(0o700)
    if path_kind.startswith("JSON_"):
        assert run.code == 2 and "Scanning" not in run.stdout
    elif path_kind != "MAP_CHILD_FAILED":
        assert run.report is not None and run.report["error"]["code"] == path_kind
    assert not out.exists()


def _unremovable_report(tmp_path: Path) -> Path:
    """An old --json report that cannot be removed: inside a directory the
    user may not write (EACCES on Linux and macOS alike)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can remove a file from any directory")
    locked = tmp_path / "locked"
    locked.mkdir()
    report = locked / "report.json"
    report.write_text("{}")
    locked.chmod(0o500)
    return report


def test_a_json_refusal_never_removes_an_input(tmp_path, monkeypatch) -> None:
    """--target and --visible-map naming the same earlier map, with a
    --json report that cannot be removed: the stale-map cleanup on that
    early return (_drop_stale_map, not the map's own path check, which
    never runs) must check its inputs first, or it deletes the target."""
    target = tmp_path / "tmap.pdf"
    data = _marked_map(target)
    report = _unremovable_report(tmp_path)
    try:
        run = run_main(monkeypatch, target, write_rules(tmp_path, SSN_RULES), "--json",
                       str(report), out=target, json_report=False)
    finally:
        report.parent.chmod(0o700)
    assert run.code == 2 and "cannot replace JSON report" in run.stderr
    assert target.read_bytes() == data


def test_an_unmarked_file_is_refused_untouched(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    out = tmp_path / "original_unredacted.pdf"
    out.write_bytes(b"%PDF-1.7\nsomeone else's file\n")
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), out=out)
    assert run.code == 2 and run.report["error"]["code"] == "MAP_PATH_CONFLICT"
    assert out.read_bytes() == b"%PDF-1.7\nsomeone else's file\n"
    assert "Scanning" not in run.stdout          # refused before scanning


def test_a_partial_map_is_refused_by_name(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    out = tmp_path / "out.pdf"
    whole = _marked_map(out)
    partial = whole[:len(whole) // 2]
    out.write_bytes(partial)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), out=out)
    assert run.code == 2 and run.report["error"]["code"] == "MAP_PATH_CONFLICT"
    assert ("out.pdf: line-2 marker %PRV-VISIBLE-MAP-1 present, tail marker missing"
            in run.stderr)
    assert out.read_bytes() == partial


@pytest.mark.parametrize("what", ["target", "rules", "json"])
def test_the_map_never_names_an_input(what, tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    before = {p: p.read_bytes() for p in (pdf, rules)}
    out = {"target": pdf, "rules": rules, "json": pdf.with_name("leaky.on.json")}[what]
    run = run_main(monkeypatch, pdf, rules, out=out)
    assert run.code == 2
    assert run.report is None or run.report["error"]["code"] == "MAP_PATH_CONFLICT"
    assert {p: p.read_bytes() for p in (pdf, rules)} == before


def test_a_symlink_is_refused(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    out = tmp_path / "out.pdf"
    out.symlink_to(victim)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), out=out)
    assert run.code == 2 and run.report["error"]["code"] == "MAP_PATH_CONFLICT"
    assert out.is_symlink() and victim.read_text() == "keep me"


def test_an_unwritable_directory_is_refused(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        run = run_main(monkeypatch, pdf, rules, out=locked / "out.pdf")
    finally:
        locked.chmod(0o700)
    assert run.code == 2 and run.report["error"]["code"] == "MAP_PATH_UNWRITABLE"
    assert list(locked.iterdir()) == []


def test_a_crashing_path_check_is_a_path_error(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(paths, "check_out", boom)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
    assert run.code == 2 and run.report["error"]["code"] == "MAP_PATH_UNWRITABLE"


@pytest.mark.parametrize("kind", TARGETS)
def test_a_file_that_appears_before_publishing_is_left_alone(kind, tmp_path,
                                                             monkeypatch) -> None:
    pdf, ocr = _target(tmp_path, kind)
    rules = write_rules(tmp_path, SSN_RULES)
    base = _baseline(monkeypatch, pdf, rules, ocr=ocr)
    out = tmp_path / "out.pdf"
    original = runner.MapJob._check_published_bytes

    def race(self, record):
        original(self, record)
        out.write_bytes(b"arrived first")

    monkeypatch.setattr(runner.MapJob, "_check_published_bytes", race)
    run = run_main(monkeypatch, pdf, rules, out=out, ocr=ocr)
    _same(base, run)
    assert out.read_bytes() == b"arrived first"
    assert "MAP_PATH_CONFLICT" in run.stderr
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".out.pdf.")]


def test_a_failure_after_publishing_still_reports_the_map(tmp_path, monkeypatch) -> None:
    # Once OUT holds the map, a later failure (here: an interrupt as the
    # job notes it) must not be reported as "not written".
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    real = paths.publish

    def publish_then_interrupt(tmp, out, expect):
        real(tmp, out, expect)
        raise KeyboardInterrupt

    monkeypatch.setattr(paths, "publish", publish_then_interrupt)
    run = run_main(monkeypatch, pdf, rules)
    assert run.written and "not written" not in run.stderr
    assert "[MAP] MAP_WRITTEN" in run.stderr and "a later step failed" in run.stderr


def test_an_old_map_is_replaced(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    out = tmp_path / "out.pdf"
    _marked_map(out)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), out=out)
    assert run.code == 1 and "VISIBLE SECRETS FOUND" in map_text(out)
    assert "[MAP] MAP_WRITTEN" in run.stderr


def test_without_hard_links_the_tail_marker_lands_last(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    out = tmp_path / "out.pdf"
    writes: list[bytes] = []
    real_write = paths.write_all

    def no_link(src, dst):
        raise OSError(errno.EPERM, "no hard links here")

    def record(fd, data):
        if out.exists() and os.fstat(fd).st_ino == os.stat(out).st_ino:
            writes.append(bytes(data))
        real_write(fd, data)

    monkeypatch.setattr(paths.os, "link", no_link)
    monkeypatch.setattr(paths, "write_all", record)
    monkeypatch.setattr(paths, "_COPY_CHUNK", 1024)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), out=out)
    data = out.read_bytes()
    assert run.code == 1 and len(writes) > 2 and b"".join(writes) == data
    assert paths.markers_in(data[:1024], data[-64:]) == (True, True)
    # Every prefix a killed copy could leave lacks the tail marker.
    for n in range(1, len(writes)):
        assert not b"".join(writes[:n]).endswith(paths.MARKER_LINE)


# ── The child process (test 10) ───────────────────────────────────────
def _decoys(where: Path, tripwire: Path) -> None:
    """A redaction_verifier package and a json module, each of which trips
    *tripwire* if imported: json is the first module the bootstrap
    imports, before it sets sys.path."""
    (where / "redaction_verifier").mkdir(parents=True, exist_ok=True)
    trip = f"open({str(tripwire)!r}, 'a').write(__name__)\nraise ImportError('decoy')\n"
    (where / "redaction_verifier" / "__init__.py").write_text(trip)
    (where / "json.py").write_text(trip)


@pytest.mark.parametrize("planted", ["child-cwd", "child-env"])
def test_decoys_beside_the_child_cannot_load(planted, tmp_path, monkeypatch) -> None:
    """In the child's own working directory (-I keeps it off sys.path), and
    on a PYTHONPATH in its environment (-I ignores PYTHON* variables)."""
    decoy, tripwire = tmp_path / "decoy", tmp_path / "tripwire"
    _decoys(decoy, tripwire)
    original = runner.MapJob._exchange

    def planted_exchange(self, data, *, env, cwd, deadline, cap):
        if planted == "child-cwd":
            _decoys(Path(cwd), tripwire)
        else:
            env = dict(env, PYTHONPATH=str(decoy))
        return original(self, data, env=env, cwd=cwd, deadline=deadline, cap=cap)

    monkeypatch.setattr(runner.MapJob, "_exchange", planted_exchange)
    run = run_main(monkeypatch, _leaky(tmp_path), write_rules(tmp_path, SSN_RULES))
    assert run.written and not tripwire.exists(), run.stderr


def test_a_decoy_package_on_the_parents_path_cannot_load(tmp_path) -> None:
    """On the parent's own PYTHONPATH and as its working directory: the
    child gets the parent's absolute sys.path without its cwd, and this
    checkout's package first."""
    decoy = tmp_path / "decoy"
    tripwire = tmp_path / "tripwire"
    _decoys(decoy, tripwire)
    (decoy / "json.py").unlink()        # the parent itself needs the real json
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    out = tmp_path / "out.pdf"
    env = dict(os.environ, PYTHONPATH=str(decoy))
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "verify.py"), "--target", str(pdf), "--secrets",
         str(rules), "--visible-map", str(out)],
        capture_output=True, text=True, cwd=decoy, env=env, timeout=300)
    assert proc.returncode == 1, proc.stderr
    assert out.exists() and not tripwire.exists(), proc.stderr


def test_a_closed_stdin_cannot_swallow_the_target(tmp_path) -> None:
    """With stdin closed, a plain open() would put the target on fd 0 —
    the child's own stdin. The descriptor is kept at 3 or above."""
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    out = tmp_path / "out.pdf"
    proc = subprocess.run(
        [sys.executable, "-c", "import os, runpy, sys; os.close(0); sys.argv = sys.argv[1:]; "
         "runpy.run_path(sys.argv[0], run_name='__main__')", str(REPO_ROOT / "verify.py"),
         "--target", str(pdf), "--secrets", str(rules), "--visible-map", str(out)],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 1 and out.exists(), proc.stderr


def test_a_killed_parent_leaves_no_child_and_its_temp_is_swept(tmp_path) -> None:
    """SIGKILL the parent mid-map: the child notices its parent is gone and
    exits, and the next run removes the marked temp file left behind."""
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    out = tmp_path / "out.pdf"
    pidfile = tmp_path / "child.pid"
    script = (
        "import os, signal, sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import verify\n"
        "from redaction_verifier.visible_map import paths, runner\n"
        "verify._OCR_IMPORTS_OK = False\n"
        "write = paths.write_all\n"
        "def stall(fd, data):\n"
        "    write(fd, data)\n"
        "    proc = runner_job[0].proc\n"
        f"    open({str(pidfile)!r}, 'w').write(str(proc.pid))\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "paths.write_all = stall\n"
        "runner_job = []\n"
        "make = runner.MapJob.make\n"
        "def keep(self, header, **kw):\n"
        "    runner_job.append(self)\n"
        "    return make(self, header, **kw)\n"
        "runner.MapJob.make = keep\n"
        f"verify.main(['--target', {str(pdf)!r}, '--secrets', {str(rules)!r}, "
        f"'--visible-map', {str(out)!r}])\n")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == -9, proc.stderr
    left = [p for p in tmp_path.iterdir() if p.name.startswith(".out.pdf.")]
    assert left and paths.markers_in(left[0].read_bytes()[:1024], b"")[0]
    stale = time.time() - paths.STALE_AFTER_S - 60   # long enough ago to be swept
    for path in left:
        os.utime(path, (stale, stale))
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        os.kill(child, 9)
        pytest.fail("the orphaned child outlived its parent")
    paths.check_out(out, [pdf, rules])
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".out.pdf.")]


def test_the_sweep(tmp_path) -> None:
    """Only a dead run's own leftovers go: marked or empty, owned, older
    than STALE_AFTER_S. A live pid's file, a fresh one and an unmarked
    file with content stay."""
    out = tmp_path / "out.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "map", fontname="helv")
    partial = paths.insert_markers(doc.tobytes(garbage=4, deflate=True))[:200]
    dead = int(subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True).stdout)
    old = time.time() - paths.STALE_AFTER_S - 60
    files = {
        "live": (paths.sibling(out, "0a0a0a0a.tmp", os.getppid()), partial, old),
        "dead-marked": (paths.sibling(out, "1b1b1b1b.tmp", dead), partial, old),
        "dead-empty": (paths.sibling(out, "2c2c2c2c.tmp", dead), b"", old),
        "dead-probe": (paths.sibling(out, "3d3d3d3d.probe", dead), b"", old),
        "dead-fresh": (paths.sibling(out, "4e4e4e4e.tmp", dead), partial, time.time()),
        "dead-unmarked": (paths.sibling(out, "5f5f5f5f.tmp", dead), b"not a map", old),
    }
    for path, data, mtime in files.values():
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
    paths.sweep_temps(out)
    left = {name for name, (path, _, _) in files.items() if path.exists()}
    assert left == {"live", "dead-fresh", "dead-unmarked"}


def test_the_sweep_waits_out_the_childs_deadline() -> None:
    assert paths.STALE_AFTER_S == 2 * runner.TIMEOUT_CAP_S


def test_a_map_is_written_on_fat(tmp_path, monkeypatch) -> None:
    """On a real FAT volume (no hard links; an empty file's inode number
    changes once it has data) the map is still written, front to back."""
    if not shutil.which("hdiutil"):
        pytest.skip("needs hdiutil (macOS) to mount a FAT image")
    image, mount = tmp_path / "fat.dmg", tmp_path / "fat"
    mount.mkdir()
    made = subprocess.run(["hdiutil", "create", "-size", "8m", "-fs", "MS-DOS FAT16",
                           "-volname", "MAPTEST", str(image)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"hdiutil create failed: {made.stderr.strip()[:200]}")
    attached = subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount),
                               str(image)], capture_output=True, text=True)
    if attached.returncode != 0:
        pytest.skip(f"hdiutil attach failed: {attached.stderr.strip()[:200]}")
    try:
        out = mount / "out.pdf"
        run = run_main(monkeypatch, _leaky(tmp_path), write_rules(tmp_path, SSN_RULES), out=out)
        assert run.written, run.stderr
        data = out.read_bytes()
        assert paths.markers_in(data[:1024], data[-64:]) == (True, True)
        assert not [p for p in mount.iterdir() if p.name.startswith(".out.pdf.")]
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)


def test_a_long_output_name_fits(tmp_path, monkeypatch) -> None:
    out = tmp_path / ("m" * 240 + ".pdf")
    run = run_main(monkeypatch, _leaky(tmp_path), write_rules(tmp_path, SSN_RULES), out=out)
    assert run.written, run.stderr
    assert all(len(p.name) <= 255 for p in tmp_path.iterdir())


def test_the_flag_is_never_abbreviated(capsys) -> None:
    """--v used to be an error; it must not now silently mean
    --visible-map. Other options' abbreviations are untouched."""
    for arg in ("--v", "--vis", "--visible-ma=x.pdf"):
        with pytest.raises(SystemExit) as stop:
            verify.main(["--target", "t.pdf", "--secrets", "r.json", arg, "x.pdf"])
        assert stop.value.code == 2
    assert "spell out --visible-map" in capsys.readouterr().err
    # Help is answered first, as it always was.
    for argv in (["--help", "--v"], ["-h", "--vi", "x"]):
        with pytest.raises(SystemExit) as stop:
            verify.main(argv)
        assert stop.value.code == 0


def test_the_child_loads_this_checkouts_package_first() -> None:
    # Whatever is installed, the child imports the package beside the
    # verify.py that started it (an uninstalled checkout works as is).
    assert runner.child_sys_path()[0] == str(REPO_ROOT)
    assert "" not in runner.child_sys_path() and os.getcwd() not in runner.child_sys_path()[1:]


def test_nothing_is_left_in_scratch_after_a_sigkill(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    monkeypatch.setattr(runner, "BOOTSTRAP", runner.BOOTSTRAP.replace(
        "from redaction_verifier.visible_map.child import run\nrun(header)\n",
        "from redaction_verifier.visible_map import child\n"
        "data, digest = child._read_target(header['fd'])\n"
        "child.build_map(data, digest, header)\n"
        "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"))
    seen: list[list[str]] = []
    original = runner.MapJob._exchange

    def watch(self, data, *, env, cwd, deadline, cap):
        try:
            return original(self, data, env=env, cwd=cwd, deadline=deadline, cap=cap)
        finally:
            seen.append(sorted(os.listdir(cwd)))

    monkeypatch.setattr(runner.MapJob, "_exchange", watch)
    run = run_main(monkeypatch, pdf, rules)
    assert seen == [[]]
    _no_map(run)


@pytest.mark.parametrize("kind", TARGETS)
def test_the_target_changed_after_the_scan(kind, tmp_path, monkeypatch) -> None:
    pdf, ocr = _target(tmp_path, kind)
    rules = write_rules(tmp_path, SSN_RULES)
    base = _baseline(monkeypatch, pdf, rules, ocr=ocr)
    original = runner.MapJob.make

    def edit_then_make(self, header, **kw):
        with open(pdf, "r+b") as f:       # same inode, new bytes
            f.seek(0, os.SEEK_END)
            f.write(b"\n% appended after the scan\n")
        return original(self, header, **kw)

    monkeypatch.setattr(runner.MapJob, "make", edit_then_make)
    run = run_main(monkeypatch, pdf, rules, ocr=ocr)
    _same(base, run)
    _no_map(run)
    assert "MAP_TARGET_CHANGED" in run.stderr


def test_a_same_size_rewrite_is_caught_by_its_hash(tmp_path, monkeypatch) -> None:
    """Same inode, same size, mtime put back: only the child's hash of what
    it read can tell the target changed after the scan."""
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    original = runner.MapJob.make

    def rewrite_then_make(self, header, **kw):
        st = os.stat(pdf)
        data = bytearray(pdf.read_bytes())
        data[-20] ^= 0x01
        with open(pdf, "r+b") as f:
            f.write(data)
        os.utime(pdf, ns=(st.st_atime_ns, st.st_mtime_ns))
        return original(self, header, **kw)

    monkeypatch.setattr(runner.MapJob, "make", rewrite_then_make)
    run = run_main(monkeypatch, pdf, rules)
    _no_map(run)
    assert "MAP_TARGET_CHANGED" in run.stderr


def test_a_file_slipped_in_before_the_link_survives(tmp_path, monkeypatch) -> None:
    """Between publish's re-check of OUT and the link, a file appears: the
    link must refuse to replace it (os.rename would not)."""
    pdf = _leaky(tmp_path)
    out = tmp_path / "out.pdf"
    real = paths.remove_stale
    publishing = []
    original = runner.MapJob._check_published_bytes

    def check(self, record):
        original(self, record)
        publishing.append(True)

    def then_appear(path):
        real(path)
        if publishing:
            path.write_bytes(b"slipped in")

    monkeypatch.setattr(runner.MapJob, "_check_published_bytes", check)
    monkeypatch.setattr(paths, "remove_stale", then_appear)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), out=out)
    assert out.read_bytes() == b"slipped in"
    assert "MAP_PATH_CONFLICT" in run.stderr


def _signal_run(tmp_path: Path, signals: list[int]) -> tuple[int, str]:
    """A leaky run whose map child just sleeps; *signals* are sent to the
    whole process group once that child is up."""
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import verify\n"
        "from redaction_verifier.visible_map import runner\n"
        "verify._OCR_IMPORTS_OK = False\n"
        "runner.BOOTSTRAP = 'import sys, time\\nsys.stderr.write(\"CHILD UP\\\\n\")\\n"
        "sys.stderr.flush()\\ntime.sleep(60)\\n'\n"
        "make = runner.MapJob.make\n"
        "def announce(self, header, **kw):\n"
        "    sys.stderr.write('MAKE\\n'); sys.stderr.flush()\n"
        "    return make(self, header, **kw)\n"
        "runner.MapJob.make = announce\n"
        f"verify.run_cli(['--target', {str(pdf)!r}, '--secrets', {str(rules)!r}, "
        f"'--visible-map', {str(tmp_path / 'out.pdf')!r}])\n")
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    assert proc.stderr is not None
    for line in proc.stderr:
        if line.startswith("MAKE"):
            break
    time.sleep(0.5)                 # the child is started and sleeping
    for signum in signals:          # as a terminal's Ctrl-C: to the whole group
        try:
            os.killpg(proc.pid, signum)
        except (ProcessLookupError, PermissionError):
            break                   # the run already finished
        time.sleep(0.002)
    out, err = proc.communicate(timeout=120)
    return proc.returncode, err


@pytest.mark.parametrize("signals", [
    [signal.SIGINT], [signal.SIGINT] * 3, [signal.SIGINT] * 10, [signal.SIGTERM]],
    ids=["one-sigint", "three-sigints", "ten-sigints", "sigterm"])
def test_signals_during_the_map_keep_the_verdict(signals, tmp_path) -> None:
    """The leak was confirmed before the map began: Ctrl-C (once or in a
    burst) or SIGTERM during the map ends the map, never the verdict."""
    code, err = _signal_run(tmp_path, signals)
    assert code == 1, err[-2000:]
    assert not (tmp_path / "out.pdf").exists()


@requires_full_env
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_a_signal_after_the_map_in_the_cleanup_keeps_the_verdict(signum, tmp_path,
                                                                 monkeypatch) -> None:
    """A burst that outlasts the map: a signal arriving in main()'s
    cleanup, after the map is done, on a clean target. It must not turn
    the decided 0 into the crash code; the handlers the map replaced are
    back once main() returns."""
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Nothing to see")
    pdf = tmp_path / "clean.pdf"
    doc.save(pdf)
    doc.close()
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    real_close = runner.MapJob.close

    def close_then_signal(self: runner.MapJob) -> None:
        real_close(self)
        os.kill(os.getpid(), signum)
        time.sleep(0.05)            # let the handler run here, in the cleanup

    monkeypatch.setattr(runner.MapJob, "close", close_then_signal)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), ocr=True)
    assert run.code == 0 and run.written, run.stderr
    assert {s: signal.getsignal(s) for s in before} == before


def test_a_leftover_with_an_impossible_process_id_is_left_alone(tmp_path) -> None:
    """A junk name whose id overflows os.kill must not make every later
    run refuse the flag: the sweep leaves it and goes on."""
    out = tmp_path / "o1.pdf"
    junk = out.with_name(f".{out.name}.99999999999999999999999.tmp")
    junk.write_bytes(b"")
    os.utime(junk, (0, 0))
    paths.sweep_temps(out)
    assert junk.exists()


@pytest.mark.parametrize("cjk", ["文書文書文書文書", "書文"])
def test_a_tail_drawn_with_escapes_shows_no_digit_of_a_value(cjk, tmp_path, monkeypatch) -> None:
    """A CJK tail is drawn as \\u{6587}...: hex digits an exact value may
    hold. The map shows only "****", on the page and in the panels list
    (the --json sample, re-checked as drawn), while the reports keep
    mask()'s tail (no character of the value in it)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Ref 65871234 noted.")
    page.insert_text((72, 120), cjk, fontname="china-s")
    doc.set_metadata({"title": cjk})
    target = tmp_path / "cjk.pdf"
    doc.save(target)
    rules = write_rules(tmp_path, [{"name": "ref", "value": "65871234"},
                                   {"name": "cjk", "pattern": "[\\u4e00-\\u9fff]{%d}" % len(cjk)}])
    run = run_main(monkeypatch, target, rules)
    assert run.code == 1 and run.written, run.stderr
    tail = cjk[-min(4, len(cjk) // 2):]
    assert run.report is not None
    assert {f["sample"] for f in run.report["findings"] if f["sample"]} == {"****" + tail}
    text = map_text(run.out)
    assert "pattern rule #1 ****, selectable text" in text            # the box
    assert "pattern rule #1 **** -- object" in text                   # the --json sample
    assert "\\u{" not in text
    assert re.findall(r"\*{4}(\S*)", text) and not any(re.findall(r"\*{4}([^\s,]+)", text))


def test_closed_stderr_keeps_map_status_off_stdout(tmp_path) -> None:
    """With fd 2 closed, print(file=sys.stderr) would fall back to stdout:
    the map's status must vanish instead, so stdout stays identical."""
    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    outs = []
    for flag in ([], ["--visible-map", str(tmp_path / "out.pdf")]):
        proc = subprocess.run(
            ["/bin/sh", "-c", 'exec "$@" 2>&-', "sh", sys.executable,
             str(REPO_ROOT / "verify.py"), "--target", str(pdf), "--secrets", str(rules), *flag],
            capture_output=True, text=True, timeout=300)
        outs.append((proc.returncode, proc.stdout))
    assert outs[0] == outs[1]
    assert "[MAP]" not in outs[1][1] and (tmp_path / "out.pdf").exists()


def test_a_fifo_target_never_blocks(tmp_path) -> None:
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    job = runner.MapJob(tmp_path / "out.pdf")
    started = time.monotonic()
    job.open_target(fifo)
    assert time.monotonic() - started < 5
    assert job.fd is None and job.skip is not None and job.skip.code == "MAP_TARGET_UNREADABLE"


# ── Memory (test 11) ──────────────────────────────────────────────────
# Runs verify.main in a fresh interpreter (OCR off) and reports its own
# peak RSS. Children are not counted: RUSAGE_SELF.
_PEAK = (
    "import resource, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import verify\n"
    "verify._OCR_IMPORTS_OK = False\n"
    "unit = 1 if sys.platform == 'darwin' else 1024\n"
    "code = verify.main(sys.argv[2:])\n"
    "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit\n"
    "sys.stderr.write(f'PEAK {peak} {code}\\n')\n"
)


def _peak(target: Path, rules: Path, *extra: str) -> tuple[int, int]:
    proc = subprocess.run(
        [sys.executable, "-c", _PEAK, str(REPO_ROOT), "--target", str(target), "--secrets",
         str(rules), *extra],
        capture_output=True, text=True, timeout=1200)
    line = [ln for ln in proc.stderr.splitlines() if ln.startswith("PEAK ")][-1]
    _, peak, code = line.split()
    return int(peak), int(code)


def _big_image_pdf(path: Path, megabytes: int) -> Path:
    """One page drawing one uncompressed grey image of *megabytes* MB,
    written straight to disk (never held whole in this process)."""
    width = 4096
    height = megabytes * (1 << 20) // width
    content = f"q {width / 10} 0 0 {height / 10} 0 0 cm /Im1 Do Q".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {width / 10} {height / 10}]"
         "/Resources<</XObject<</Im1 5 0 R>>>>/Contents 4 0 R>>").encode(),
    ]
    with open(path, "wb") as f:
        f.write(b"%PDF-1.7\n")
        offsets = []
        for n, body in enumerate(objects, start=1):
            offsets.append(f.tell())
            f.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
        offsets.append(f.tell())
        f.write(b"4 0 obj\n<</Length %d>>\nstream\n" % len(content) + content
                + b"\nendstream\nendobj\n")
        offsets.append(f.tell())
        f.write(f"5 0 obj\n<</Type/XObject/Subtype/Image/Width {width}/Height {height}"
                f"/ColorSpace/DeviceGray/BitsPerComponent 8/Length {width * height}>>\n"
                "stream\n".encode())
        row = b"\x80" * width             # no byte a rule could read as a digit
        for _ in range(height):
            f.write(row)
        f.write(b"\nendstream\nendobj\n")
        xref = f.tell()
        f.write(b"xref\n0 6\n0000000000 65535 f \n")
        for off in offsets:
            f.write(b"%010d 00000 n \n" % off)
        f.write(b"trailer\n<</Size 6/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % xref)
    return path


def _dense_pdf(path: Path, pages: int) -> Path:
    doc = fitz.open()
    line = "Lorem ipsum dolor sit amet, consectetur adipiscing elit 4 " * 2
    for _ in range(pages):
        page = doc.new_page()
        for i in range(40):
            page.insert_text((20, 20 + i * 18), line, fontsize=8)
    doc.save(path, garbage=1, deflate=True)
    doc.close()
    return path


MB = 1 << 20


def _median(values: list[int]) -> int:
    return sorted(values)[len(values) // 2]


def _overhead(target: Path, rules: Path, out: Path, runs: int = 3) -> int:
    """The flag's cost to the parent's whole-run peak RSS: the median of
    *runs* runs with it, less the median of *runs* without (same exit
    code)."""
    off = [_peak(target, rules) for _ in range(runs)]
    on = [_peak(target, rules, "--visible-map", str(out)) for _ in range(runs)]
    assert {code for _, code in off} == {code for _, code in on}
    assert out.exists()
    return _median([p for p, _ in on]) - _median([p for p, _ in off])


def test_the_parent_holds_no_more_memory_however_large_the_target(tmp_path) -> None:
    """The flag's cost to the parent's whole-run peak does not grow with
    the target: the target reaches the child by descriptor and the map
    comes back in 64 KB chunks. Measured (2026-10-14): about 4 MB at a
    25 MB target and at a 200 MB one -- the map step's own fixed cost
    (the planner's imports, the header, spawning the child, the stream
    buffers), stacked on the scan's memory MuPDF still holds when the map
    starts, so it shows above the scan's peak. A parent that read the
    target would grow by its size. Medians of three runs each, because a
    scan's own peak varies by several MB from run to run on a loaded
    machine; test_the_map_step_holds_no_more_memory_however_large_the_target
    bounds the step itself.

    The overhead is the whole-run peak with the flag less the peak
    without: max(scan peak, memory kept after the scan + the map step's
    fixed cost) - scan peak. So it lies between 0 (the scan's own peak
    absorbs the step) and the fixed cost, whichever the target gives:
    macOS measured 3.4 and 3.2 MB (2026-10-15); Linux CI 0 and 5.2 MB
    (3.13), where the old 5 MB bound on the difference failed. The bound
    is the fixed-cost budget, 16 MB, on each and on their difference;
    a parent that read the target would add at least 175 MB here."""
    rules = write_rules(tmp_path, SSN_RULES)
    small = _overhead(_big_image_pdf(tmp_path / "small.pdf", 25), rules, tmp_path / "s.pdf")
    large = _overhead(_big_image_pdf(tmp_path / "large.pdf", 200), rules, tmp_path / "l.pdf")
    assert small < 16 * MB and large < 16 * MB, (small, large)
    assert large - small < 16 * MB, (small, large)


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("RUN_PERF") != "1",
                    reason="2,000 dense pages; set RUN_PERF=1 to run")
def test_the_parent_holds_no_more_memory_with_many_pages(tmp_path) -> None:
    """The same fixed cost on 2,000 dense pages (OCR off; nothing to
    find): the flag adds no per-page memory to the parent."""
    rules = write_rules(tmp_path, SSN_RULES)
    target = _dense_pdf(tmp_path / "dense.pdf", 2000)
    assert _overhead(target, rules, tmp_path / "out.pdf") < 10 * MB


# The map step alone, in a fresh interpreter with no scan to hide behind:
# the job is prepared, opened and made from a header built by hand, and the
# parent's peak RSS must not grow by more than a few MB however large the
# target. (A parent that read the PDF into memory would grow by its size.)
_STEP = (
    "import resource, sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import pymupdf, verify\n"
    "from redaction_verifier.model import ScanReport\n"
    "from redaction_verifier.report import build_json_report\n"
    "from redaction_verifier.rules import load_rules\n"
    "from redaction_verifier.visible_map import runner\n"
    "from redaction_verifier.visible_map.sink import MapSink, offer\n"
    "target, rules, out, scratch = map(Path, sys.argv[2:6])\n"
    "unit = 1 if sys.platform == 'darwin' else 1024\n"
    "def peak():\n"
    "    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit\n"
    "sink = MapSink()\n"
    "offer(sink, 'document', 1)\n"
    "offer(sink, 'page', 'Text', 1, set(), set(), {}, {}, set(), set())\n"
    "header = {'rules': runner.rules_header(load_rules(rules)), 'sink': sink.to_header(),\n"
    "          'report': build_json_report(ScanReport(), 0, None, target=target,\n"
    "                                      tool_version='test', ocr_available=True),\n"
    "          'exit_code': 0, 'scan_code': 0, 'target_name': target.name, 'version': 'test'}\n"
    "warm = pymupdf.open(); warm.new_page(); pymupdf.open('pdf', warm.tobytes()).close()\n"
    "job = runner.prepare(out, [target, rules])\n"
    "job.open_target(target)\n"
    "before = peak()\n"
    "job.make(header, env=verify._minimal_subprocess_env(), cwd=str(scratch), pages=1, items=1)\n"
    "job.close()\n"
    "sys.stderr.write(f'STEP {peak() - before}\\n')\n"
)


def test_the_map_step_holds_no_more_memory_however_large_the_target(tmp_path) -> None:
    target = _big_image_pdf(tmp_path / "large.pdf", 200)
    rules = write_rules(tmp_path, SSN_RULES)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _STEP, str(REPO_ROOT), str(target), str(rules),
         str(tmp_path / "out.pdf"), str(scratch)],
        capture_output=True, text=True, timeout=600)
    line = [ln for ln in proc.stderr.splitlines() if ln.startswith("STEP ")]
    assert line, proc.stderr
    assert (tmp_path / "out.pdf").exists()
    assert int(line[-1].split()[1]) < 4 * MB, line


def test_the_child_timeout_scales_with_pages() -> None:
    assert runner.timeout_for(0) == 30.0
    assert runner.timeout_for(100) == 55.0
    assert runner.timeout_for(10 ** 6) == 600.0
    assert runner.length_cap(10, 5) < runner.length_cap(10_000, 5) <= runner.LENGTH_CAP


def test_help_mentions_the_flag(capsys) -> None:
    with pytest.raises(SystemExit):
        verify.main(["--help"])
    assert "--visible-map OUT.pdf" in capsys.readouterr().out


def test_stderr_carries_the_status_and_stdout_does_not(tmp_path, monkeypatch) -> None:
    pdf = _leaky(tmp_path)
    run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES))
    assert "[MAP] MAP_WRITTEN: visible map written to leaky.map.pdf: " in run.stderr
    assert "MAP" not in run.stdout


# ── Old maps and publishing ───────────────────────────────────────────
class TestOldMaps:
    @staticmethod
    def _old_map(path: Path) -> bytes:
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "map", fontname="helv")
        data = paths.insert_markers(doc.tobytes(garbage=4, deflate=True))
        path.write_bytes(data)
        return data

    def test_a_file_swapped_in_after_the_check_is_put_back(self, tmp_path, monkeypatch) -> None:
        out = tmp_path / "out.pdf"
        self._old_map(out)
        real = paths._read_markers
        calls = []

        def swap_after_checking(path, st):
            found = real(path, st)
            if not calls:
                calls.append(path)
                out.unlink()
                out.write_bytes(b"someone else's file")     # swapped in after the check
            return found

        monkeypatch.setattr(paths, "_read_markers", swap_after_checking)
        with pytest.raises(paths.PathRefused) as refused:
            paths.remove_stale(out)
        assert refused.value.code == "MAP_PATH_CONFLICT"
        assert out.read_bytes() == b"someone else's file"
        assert [p.name for p in tmp_path.iterdir()] == ["out.pdf"]

    def test_publishing_refuses_a_replaced_temp_file(self, tmp_path) -> None:
        # The temp file at publish time is not the one written (here: the
        # written one is still elsewhere, so the inodes cannot coincide).
        written, tmp, out = tmp_path / "written", tmp_path / ".out.pdf.1.tmp", tmp_path / "out.pdf"
        self._old_map(written)
        tmp.write_bytes(b"not the map")
        st = written.stat()
        with pytest.raises(paths.PathRefused):
            paths.publish(tmp, out, (st.st_dev, st.st_ino))
        assert not out.exists()


# ── The header the CLI hands the child ────────────────────────────────
def test_the_cli_header_is_the_engine_harness_header(tmp_path, monkeypatch) -> None:
    """The engine's tests plan from a header the in-process harness
    builds; the CLI must hand its child the same one."""
    from .visible_map_support import run_main as run_scan

    pdf = _leaky(tmp_path)
    rules = write_rules(tmp_path, SSN_RULES)
    cli = run_main(monkeypatch, pdf, rules)
    scan = run_scan(monkeypatch, pdf, rules)
    assert cli.header is not None and scan.header is not None
    assert cli.header == scan.header


@requires_full_env
class TestHeadlineRow6:
    def test_row_6_json_not_written(self, tmp_path, monkeypatch) -> None:
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "Nothing to see")
        pdf = tmp_path / "r6.pdf"
        doc.save(pdf)
        doc.close()
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)          # the report cannot be written there
        try:
            run = run_main(monkeypatch, pdf, write_rules(tmp_path, SSN_RULES), "--json",
                           str(locked / "r6.json"), ocr=True, json_report=False)
        finally:
            locked.chmod(0o700)
        assert run.code == 2
        assert "every check ran; JSON report not written (exit 2)" in map_text(run.out)

