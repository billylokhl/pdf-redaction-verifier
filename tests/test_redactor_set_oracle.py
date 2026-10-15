"""The real-redactor set's oracle (eval/redactor_set/oracle*.py) and its
adjudication records: independence from the verifier, channel R1 on
hand-built files, matching and classification rules, the expected-verdict
table against the case library, and the lock."""

from __future__ import annotations

import ast
import json
import shutil
import sys
import zlib
from pathlib import Path

import pikepdf
import pytest

from redactor_set import adjudicate, oracle, oracle_raw, persona
from redactor_set.truth import DocTruth, Instance

PKG = Path(__file__).resolve().parents[1] / "eval" / "redactor_set"
ORACLE_MODULES = ("oracle_raw.py", "oracle.py", "adjudicate.py", "oracle_cli.py")
FORBIDDEN = ("redaction_verifier", "verify", "caselib", "scorecard", "pymupdf", "fitz")


def _imports(source: str) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


class TestIndependence:
    def test_oracle_modules_import_nothing_of_the_verifier_or_pymupdf(self) -> None:
        for name in ORACLE_MODULES:
            bad = [n for n in _imports((PKG / name).read_text())
                   if n.split(".")[0] in FORBIDDEN]
            assert bad == [], name

    def test_raw_channel_uses_the_standard_library_only(self) -> None:
        tree = ast.parse((PKG / "oracle_raw.py").read_text())
        relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
        assert relative == []
        top = {n.split(".")[0] for n in _imports((PKG / "oracle_raw.py").read_text())}
        assert top <= set(sys.stdlib_module_names) | {"__future__"}, top

    def test_the_guard_is_live(self) -> None:
        assert [n for n in _imports("import fitz\nfrom caselib import x")
                if n.split(".")[0] in FORBIDDEN] == ["fitz", "caselib"]


# ── R1 on hand-built files ─────────────────────────────────────────────

def _pdf_with(tmp_path: Path, content: bytes, *, info: dict[str, object] | None = None,
              compress: bool = True) -> Path:
    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    page.obj.Contents = pdf.make_stream(content)
    for k, v in (info or {}).items():
        pdf.docinfo[k] = v
    path = tmp_path / "f.pdf"
    pdf.save(path, compress_streams=compress,
             object_stream_mode=pikepdf.ObjectStreamMode.disable)
    return path


class TestRawChannel:
    def test_finds_text_in_a_compressed_orphaned_stream(self) -> None:
        body = zlib.compress(b"BT (SSN 123-45-6789) Tj ET")
        raw = (b"%%PDF-1.7\n9 0 obj << /Length %d /Filter /FlateDecode >> stream\n" % len(body)
               + body + b"\nendstream endobj\n%%EOF\n")
        assert b"123-45-6789" not in raw                      # compressed on disk
        hits = oracle_raw.scan(raw, {"123-45-6789": []}).hits
        assert any(h.kind == "content" and h.how == "ascii" for h in hits)

    def test_finds_strings_in_any_encoding(self, tmp_path) -> None:
        for value in (pikepdf.String("Jordan Q. Testperson"),
                      pikepdf.String(b"\xfe\xff" + "Jordan Q. Testperson".encode("utf-16-be"))):
            path = _pdf_with(tmp_path, b"q Q", info={"/Author": value})
            hits = oracle_raw.scan(path.read_bytes(), {"Jordan Q. Testperson": []}).hits
            assert hits, value

    def test_after_the_final_eof(self, tmp_path) -> None:
        path = _pdf_with(tmp_path, b"q Q")
        raw = path.read_bytes() + b"\n%% (731904628155)\n"
        assert any(h.kind == "raw" for h in oracle_raw.scan(raw, {"731904628155": []}).hits)

    def test_joins_text_operands_and_drops_kerning(self) -> None:
        content = b"BT /F1 9 Tf (12) Tj [(3-) -40 (45-)] TJ <363738> Tj (9) Tj ET"
        assert oracle_raw.text_blocks(content) == [b"123-45-6789"]

    def test_fingerprint_codes_match_across_operands(self, tmp_path) -> None:
        codes = "123-45-6789".encode("utf-16-be")
        content = b"BT /F1 9 Tf <" + codes[:6].hex().encode() + b"> Tj [<" + \
            codes[6:].hex().encode() + b">] TJ ET"
        path = _pdf_with(tmp_path, content)
        hits = oracle_raw.scan(path.read_bytes(), {"123-45-6789": [codes.hex()]}).hits
        assert any(h.how == "codes" for h in hits)

    def test_a_font_program_hit_never_counts_alone(self) -> None:
        raw = (b"%PDF-1.7\n1 0 obj << /Length1 20 /Length 20 >> stream\n"
               b"glyf 123-45-6789 xx\nendstream endobj\n%%EOF\n")
        (hit,) = oracle_raw.scan(raw, {"123-45-6789": []}, partial=False).hits
        assert hit.kind == "font" and "font" in oracle_raw.NOT_RECOVERABLE_ALONE

    def test_partial_residue_in_text_not_in_numbers(self, tmp_path) -> None:
        # The last four of an account number left in a text operand...
        path = _pdf_with(tmp_path, b"BT (acct ****8155) Tj ET 815.5 0 0 1 0 0 cm")
        hits = oracle_raw.scan(path.read_bytes(), {"731904628155": []}).hits
        assert [h.how for h in hits] == ["partial:8155"]
        # ...but operators' numbers are never read as residue.
        path = _pdf_with(tmp_path, b"8155 6281 m 1 1 l S")
        assert oracle_raw.scan(path.read_bytes(), {"731904628155": []}).hits == []


class TestResidueRule:
    @pytest.mark.parametrize("text", [
        "62,048.57", "03/14", "Fictional Federal Bank", "acct 7319", "TESTPERSON",
        "compensation", "Anytown 11/19 1,042.50", "Samplename spouse", "sample",
        "1600 Main St", "PO Box 0000"])
    def test_agrees_with_the_generators_filler_rule(self, text) -> None:
        """The oracle and the generator must use one definition of partial
        residue: filler the generator accepts is never a partial leak."""
        oracle_says = any(oracle_raw.residue(text, f) for f in persona.written_forms())
        assert oracle_says == bool(persona.residue_in(text)), text

    def test_digits_ignore_separators_inside_a_token(self) -> None:
        assert oracle_raw.residue("123-45", "123-45-6789") == "1234"
        assert oracle_raw.residue("12 3-4", "123-45-6789") is None


# ── Matching and classification ────────────────────────────────────────

class TestMatching:
    def test_ocr_tolerances(self) -> None:
        assert oracle.ocr_match("123-45-6789", "SSN l23-45-67B9") == "whole"   # look-alikes
        assert oracle.ocr_match("123-45-6789", "SSN 123-45-6780") == "whole"   # one edit, long
        assert oracle.ocr_match("03/14/1971", "03/14/1977") == "whole"
        assert oracle.ocr_match("1600 Fictional Ave", "1600 Fictnal Ave") == "near"
        assert oracle.ocr_match("Avery", "Avary") == "near"    # short: exact, else the adjudicator
        assert oracle.ocr_match("Avery", "Jordan") is None

    def test_classes_by_precedence(self) -> None:
        inst = Instance("SSN1", "123-45-6789", "T1", "page-text", 0, [0, 0, 1, 1])
        E = oracle.Evidence
        f = "123-45-6789"
        assert oracle.classify(inst, [E("R1", "x", "whole", form=f),
                                      E("V", "y", "whole", form=f)]) == "recoverable-visible"
        assert oracle.classify(inst, [E("R2", "x", "whole", "orphaned", form=f)]) \
            == "recoverable-hidden"
        assert oracle.classify(inst, [E("R1", "f", "whole", counts=False, form=f)]) == "removed"
        assert oracle.classify(inst, [E("V", "x", "partial:6789", form=f)]) == "partial"
        assert oracle.worst(["removed", "partial", "recoverable-hidden"]) == "recoverable-hidden"

    def test_one_surviving_part_of_a_split_value_is_partial(self) -> None:
        inst = Instance("NAME2", "Avery R. Samplename", "T3c", "page-text", 0, None,
                        parts=["Avery R.", "Samplename"])
        E = oracle.Evidence
        assert oracle.classify(inst, [E("R1", "x", "whole", form="Samplename")]) == "partial"
        assert oracle.classify(inst, [E("R1", "x", "whole", form="Samplename"),
                                      E("R1", "y", "whole", form="Avery R.")]) \
            == "recoverable-hidden"


# ── The expected-verdict table against the case library ────────────────

class TestExpectations:
    def test_every_case_exists_with_that_cell_and_exit(self) -> None:
        import caselib

        reg = caselib.load()
        for key, exp in adjudicate.EXPECTED.items():
            case = reg.get(exp.case)
            assert case is not None, (key, exp.case)
            assert exp.cell in case.cells, (key, exp.case, case.cells)
            assert case.expected.exit == exp.exit, (key, exp.case)

    def test_every_location_has_an_expectation(self) -> None:
        from redactor_set.truth import LOCATIONS

        covered = {loc for loc, _ in adjudicate.EXPECTED}
        assert set(LOCATIONS) - covered == set()

    def test_exit_order(self) -> None:
        assert adjudicate.worst_exit([0, 2]) == 2 and adjudicate.worst_exit([2, 1, 0]) == 1


# ── Attribution, R2 and the regressions the review found (no OCR) ─────

def _annotated_leak(tmp_path: Path) -> Path:
    """The page's SSN removed and boxed, but the value copied into an
    annotation's /Contents and the Info /Subject."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Contents = pdf.make_stream(b"0 g 72 690 190 24 re f")
    annot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text,
        Rect=pikepdf.Array([500, 700, 520, 720]), Contents=pikepdf.String("SSN 123-45-6789")))
    page.obj.Annots = pikepdf.Array([annot])
    pdf.docinfo["/Subject"] = "123-45-6789"
    path = tmp_path / "ann.pdf"
    pdf.save(path)
    return path


def _attribute(path: Path, truth: DocTruth, tmp_path: Path) -> list[oracle.InstanceResult]:
    ch = oracle.gather(path, truth, tmp_path, with_v=False)
    return oracle.attribute(truth, ch, tmp_path)


T1_SSN = Instance("SSN1", "123-45-6789", "T1", "page-text", 0, [72, 70, 260, 96],
                  fingerprints=[b"123-45-6789".hex()])


class TestAttribution:
    def test_evidence_no_instance_can_claim_is_kept_unattributed(self, tmp_path) -> None:
        rows = _attribute(_annotated_leak(tmp_path), DocTruth("X", 0, "t", 1, "t", [T1_SSN]),
                          tmp_path)
        (planted,) = [r for r in rows if r.index == 0]
        assert planted.cls == "removed"
        extra = [r for r in rows if r.index < 0]
        assert len(extra) == 1 and extra[0].secret == "SSN1" and extra[0].uncertain
        assert extra[0].cls == "recoverable-hidden"
        roles = {e.role for e in extra[0].evidence}
        assert {"annot", "info"} <= roles

    def test_a_text_layer_over_a_scan_is_not_lost(self, tmp_path) -> None:
        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(612, 792))
        font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                  BaseFont=pikepdf.Name.Helvetica,
                                  Encoding=pikepdf.Name.WinAnsiEncoding)
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(b"BT 3 Tr /F1 12 Tf 72 700 Td (SSN 123-45-6789) Tj ET")
        pdf.save(tmp_path / "s.pdf")
        pix = Instance("SSN1", "123-45-6789", "S1", "pixels", 0, [72, 80, 200, 96])
        rows = _attribute(tmp_path / "s.pdf", DocTruth("X", 0, "t", 1, "t", [pix]), tmp_path)
        assert oracle.worst([r.cls for r in rows]) == "recoverable-hidden"
        assert any(r.index < 0 for r in rows)

    def test_an_identity_h_text_layer_over_a_scan_is_not_lost(self, tmp_path) -> None:
        # The tool blanked the pixels and added a text layer in a CID font
        # whose codes do not spell the text: only pdfminer reads it, on
        # the scan's own page. It must not be explained away by the scan.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_redactor_set_generator import _identity_h_page

        _identity_h_page(tmp_path / "s.pdf", "SSN 123-45-6789")
        pix = Instance("SSN1", "123-45-6789", "S1", "pixels", 0, [72, 80, 200, 96])
        truth = DocTruth("X", 0, "t", 1, "t", [pix])
        ch = oracle.gather(tmp_path / "s.pdf", truth, tmp_path, with_v=False)
        assert any("123-45-6789" in t for t in ch.r3)
        rows = oracle.attribute(truth, ch, tmp_path)
        assert oracle.worst([r.cls for r in rows]) == "recoverable-hidden"
        assert any(r.index < 0 and r.secret == "SSN1" for r in rows)

    def test_a_unicode_hex_text_layer_is_found_in_stored_content(self) -> None:
        # A two-byte font whose codes are Unicode, shown as a hex string,
        # split across TJ operands: R1 reads it from the bytes alone.
        body = b"BT /F1 9 Tf [<003100320033002d> -20 <00340035002d0036003700380039>] TJ ET"
        stream = b"1 0 obj << /Length %d >> stream\n" % len(body) + body + \
            b"\nendstream endobj\n"
        rep = oracle_raw.scan(b"%PDF-1.7\n" + stream + b"%%EOF\n", {"123-45-6789": []})
        assert any(h.how == "codes" for h in rep.hits)
        whole = b"BT /F1 9 Tf <003100320033002d00340035002d0036003700380039> Tj ET"
        stream = b"1 0 obj << /Length %d >> stream\n" % len(whole) + whole + \
            b"\nendstream endobj\n"
        rep = oracle_raw.scan(b"%PDF-1.7\n" + stream + b"%%EOF\n", {"123-45-6789": []})
        assert any(h.how == "hex16" for h in rep.hits)

    def test_a_string_instance_never_takes_page_content(self, tmp_path) -> None:
        pdf = pikepdf.new()
        page = pdf.add_blank_page()
        page.obj.Contents = pdf.make_stream(b"BT /F1 9 Tf (731904628155) Tj ET")
        pdf.save(tmp_path / "c.pdf")
        a1 = Instance("ACCT", "731904628155", "A1", "string", 0, [0, 0, 1, 1])
        rows = _attribute(tmp_path / "c.pdf", DocTruth("X", 0, "t", 1, "t", [a1]), tmp_path)
        assert rows[0].cls == "removed" and rows[0].evidence == []
        assert [r.cls for r in rows if r.index < 0] == ["recoverable-hidden"]

    def test_r2_storage_live_and_orphaned(self, tmp_path) -> None:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "SSN 123-45-6789", fontsize=12)
        for r in page.search_for("123-45-6789"):
            page.add_redact_annot(r, fill=(0, 0, 0))
        page.apply_redactions()
        doc.save(tmp_path / "o.pdf", garbage=0)        # the old stream stays, orphaned
        hits, _ = oracle.r2_scan(tmp_path / "o.pdf", {"123-45-6789": [_helv_hex("123-45-6789")]})
        assert {h.storage for h in hits} == {"orphaned"}
        assert {h.role for h in hits} == {"content"}

    def test_dates_are_never_partial_residue(self, tmp_path) -> None:
        dob = Instance("DOB", "03/14/1971", "T1", "page-text", 0, [0, 0, 1, 1])
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.docinfo["/ModDate"] = "D:20261014141900Z"         # 14:19 -> "1419"
        pdf.save(tmp_path / "d.pdf")
        rows = _attribute(tmp_path / "d.pdf", DocTruth("X", 0, "t", 1, "t", [dob]), tmp_path)
        assert all(r.cls == "removed" for r in rows)


def _helv_hex(text: str) -> str:
    return text.encode("latin-1").hex()


class TestRawRobustness:
    def test_an_escaped_8_or_9_is_itself(self) -> None:
        assert oracle_raw.string_tokens(b"(a\\8b\\9)") == [b"a8b9"]
        assert oracle_raw.string_tokens(b"(\\101\\7)") == [b"A\x07"]

    def test_random_binary_never_crashes(self) -> None:
        import random

        rng = random.Random(0)
        for _ in range(300):
            blob = bytes(rng.randrange(256) for _ in range(3000))
            oracle_raw.string_tokens(blob)
            oracle_raw.text_blocks(blob)

    def test_superseded_and_unindexed_bodies(self) -> None:
        body = b"BT (123-45-6789) Tj ET"
        raw = (b"%PDF-1.7\n4 0 obj << /Length 22 >> stream\n" + body + b"\nendstream endobj\n"
               b"%%EOF\n4 0 obj << /Length 2 >> stream\nq \nendstream endobj\n%%EOF\n"
               b"9 0 obj << /Length 22 >> stream\n" + body + b"\nendstream endobj\n")
        storages = sorted(h.storage for h in oracle_raw.scan(raw, {"123-45-6789": []}).hits
                          if h.kind == "content")
        assert storages == ["superseded", "unindexed"]

    def test_a_dictionary_is_never_borrowed_from_the_previous_object(self) -> None:
        raw = (b"%PDF-1.7\n1 0 obj << /Subtype /Image /Length 3 >> stream\nabc\nendstream "
               b"endobj\n<< /Length 22 >> stream\nBT (123-45-6789) Tj ET\nendstream\n%%EOF\n")
        (_, text) = oracle_raw.bodies(raw)
        assert text.kind == "content"


# ── Adjudication records and the lock ──────────────────────────────────

def _write_record(root: Path, method: str, stem: str, out: Path, truth: DocTruth,
                  cls: str) -> None:
    ev = [oracle.Evidence("R2", "object 9 content", "whole", "orphaned", role="content",
                          form="123-45-6789")] if cls != "removed" else []
    result = oracle.FileResult(str(out.relative_to(root)), oracle_raw_sha(out), truth.doc,
                               method, [oracle.InstanceResult(0, "SSN1", "T1", cls, ev)],
                               {"SSN1": cls}, "leak" if cls != "removed" else "clean")
    odir = root / "oracle" / "inputs" / method
    odir.mkdir(parents=True, exist_ok=True)
    (odir / f"{stem}.json").write_text(oracle.dumps(result))
    rec = adjudicate.draft(result, json.loads(truth.to_json())["instances"],
                           oracle_raw_sha(odir / f"{stem}.json"), "chrome", stem)
    rec["adjudicator"] = "agent"
    adir = root / "adjudication" / "inputs" / method
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{stem}.json").write_text(json.dumps(rec))


def _add_doc(root: Path, stem: str = "D01-s0", seed: int = 0, cls: str = "recoverable-hidden",
             *, records: bool = True) -> None:
    """One document of the set: its truth, its m1 and m2 outputs in the
    run's run.json, their oracle results and (if *records*) drafted
    records (adjudicator filled in). m1 is ``work/<stem>.pdf``."""
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "work" / "inputs").mkdir(parents=True, exist_ok=True)
    truth = DocTruth(stem.split("-s")[0], seed, "chrome", 1, "t", [T1_SSN])
    (root / "inputs" / f"{stem}.truth.json").write_text(truth.to_json())
    m1, m2 = root / "work" / f"{stem}.pdf", root / "work" / f"{stem}.m2.pdf"
    m1.write_bytes(b"%PDF-1.7 fabricated\n")
    m2.write_bytes(b"%PDF-1.7 fabricated, collected\n")
    run_path = root / "work" / "inputs" / "run.json"
    run = json.loads(run_path.read_text()) if run_path.exists() else {"documents": {}}
    run["documents"][stem] = {"exit": 0, "m1": str(m1.relative_to(root)),
                              "m2": str(m2.relative_to(root))}
    run_path.write_text(json.dumps(run))
    if records:
        _write_record(root, "m1", stem, m1, truth, cls)
        _write_record(root, "m2", stem, m2, truth, "removed")


def _root_with_record(tmp_path: Path, cls: str = "recoverable-hidden") -> Path:
    """A local root holding one document of the set (see _add_doc)."""
    root = tmp_path / "root"
    _add_doc(root, cls=cls)
    return root


def oracle_raw_sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestAdjudication:
    def test_draft_maps_cell_and_exit_by_storage(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        rec = json.loads((root / "adjudication/inputs/m1/D01-s0.json").read_text())
        (row,) = rec["instances"]
        assert (row["where"], row["cell"], row["expected_exit"]) == (
            "orphaned", "orphaned.font", 2)

    def test_where_and_expectation(self) -> None:
        def row(*ev: tuple[str, str, str]) -> dict:
            return {"evidence": [{"channel": c, "how": h, "storage": s, "counts": True}
                                 for c, h, s in ev]}
        assert adjudicate.where(row(("R1", "whole", "")), "page-text") == "unknown"
        assert adjudicate.where(row(("R1", "whole", "superseded")), "page-text") == "superseded"
        assert adjudicate.where(row(("R2", "whole", "orphaned"), ("R2", "whole", "live")),
                                "string") == "live"
        assert adjudicate.where(row(("R2", "whole", "orphaned")), "pixels") == "pixels-orphaned"
        assert adjudicate.where(row(("V", "whole", "live")), "pixels") == "pixels-live"
        assert adjudicate.expectation("T1", "live", "quartz+pikepdf").cell == "live.plain"
        assert adjudicate.expectation("T1", "live", "chrome").cell == "live.font"
        assert adjudicate.expectation("T1", "unknown", "chrome") is None

    def test_finalize_derives_truth_and_exit(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        rec = json.loads((root / "adjudication/inputs/m1/D01-s0.json").read_text())
        final = adjudicate.finalize_record(rec)
        assert final["truth"] == "leak" and final["expected_exit"] == 2

    def test_unfinished_records_are_refused(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        rec = json.loads((root / "adjudication/inputs/m1/D01-s0.json").read_text())
        with pytest.raises(ValueError, match="no adjudicator"):
            adjudicate.finalize_record({**rec, "adjudicator": None})
        rec["instances"][0]["uncertain"] = True
        with pytest.raises(ValueError, match="unresolved"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"class": "removed"}
        with pytest.raises(ValueError, match="needs a reason"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"class": "gone", "reason": "x"}
        with pytest.raises(ValueError, match="unknown class"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"class": "removed", "reason": "crop is a box"}
        assert adjudicate.finalize_record(rec)["truth"] == "clean"

    def test_an_unattributed_row_needs_a_cell_and_exit(self) -> None:
        rec = {"adjudicator": "agent", "instances": [
            {"index": -1, "secret": "SSN1", "loc": "?", "class": "recoverable-hidden",
             "uncertain": True, "unattributed": True, "override": None, "confirmed": "seen"}]}
        with pytest.raises(ValueError, match="no expected exit"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"reason": "annotation /Contents",
                                           "cell": "annot-fields.plain", "expected_exit": 1}
        assert adjudicate.finalize_record(rec)["expected_exit"] == 1

    def test_lock_verify_and_relock(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        path, digest = adjudicate.lock(root, "inputs")
        assert len(digest) == 64
        assert adjudicate.verify(root, "inputs")[0] == []
        rec_path = root / "adjudication/inputs/m1/D01-s0.json"
        data = json.loads(rec_path.read_text())
        data["notes"] = "second look"
        rec_path.write_text(json.dumps(data))
        assert adjudicate.verify(root, "inputs")[0] == ["changed since the lock: m1/D01-s0.json"]
        with pytest.raises(ValueError, match="give a reason"):
            adjudicate.lock(root, "inputs")
        adjudicate.lock(root, "inputs", reason="note added")
        assert json.loads(path.read_text())["relocks"][0]["reason"] == "note added"

    def test_a_hand_edited_class_is_refused(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        rec_path = root / "adjudication/inputs/m1/D01-s0.json"
        data = json.loads(rec_path.read_text())
        data["instances"][0]["class"] = "removed"            # no override
        rec_path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="edited by hand"):
            adjudicate.lock(root, "inputs")

    def test_a_deleted_record_needs_a_reason(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        adjudicate.lock(root, "inputs")
        (root / "adjudication/inputs/m1/D01-s0.json").unlink()
        with pytest.raises(ValueError, match="no record for m1/D01-s0.json"):
            adjudicate.lock(root, "inputs")

    def test_a_changed_output_is_refused(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        (root / "work/D01-s0.pdf").write_bytes(b"%PDF-1.7 something else\n")
        with pytest.raises(ValueError, match="output file changed"):
            adjudicate.lock(root, "inputs")


class TestLockCompleteness:
    def test_a_record_deleted_before_the_first_lock_is_refused(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        (root / "adjudication/inputs/m2/D01-s0.json").unlink()
        with pytest.raises(ValueError, match="no record for m2/D01-s0.json"):
            adjudicate.lock(root, "inputs")

    def test_a_copied_record_is_refused(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        shutil.copy(root / "adjudication/inputs/m1/D01-s0.json",
                    root / "adjudication/inputs/m1/D01-s0-copy.json")
        with pytest.raises(ValueError, match="outside the set"):
            adjudicate.lock(root, "inputs")

    def test_a_record_under_another_method_is_refused(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        a = root / "adjudication/inputs"
        m1, m2 = (a / "m1/D01-s0.json").read_text(), (a / "m2/D01-s0.json").read_text()
        (a / "m1/D01-s0.json").write_text(m2)
        (a / "m2/D01-s0.json").write_text(m1)
        with pytest.raises(ValueError, match="its method and stem say"):
            adjudicate.lock(root, "inputs")

    def test_an_output_the_run_did_not_make_is_reported(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        run_path = root / "work/inputs/run.json"
        run = json.loads(run_path.read_text())
        run["documents"]["D01-s0"]["m2"] = None
        run_path.write_text(json.dumps(run))
        with pytest.raises(ValueError, match="the run has no output"):
            adjudicate.lock(root, "inputs")

    def test_a_record_added_after_the_lock_needs_a_reason(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        adjudicate.lock(root, "inputs")
        _add_doc(root, "D02-s0")
        with pytest.raises(ValueError, match="give a reason"):
            adjudicate.lock(root, "inputs")
        adjudicate.lock(root, "inputs", reason="D02 joined the set")
        assert adjudicate.verify(root, "inputs")[0] == []

    def test_seeds_scope_the_set_and_are_kept_in_the_lock(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        _add_doc(root, "D01-s7", seed=7, records=False)      # out of scope, no records
        with pytest.raises(ValueError, match="no record for m1/D01-s7.json"):
            adjudicate.lock(root, "inputs")
        path, _ = adjudicate.lock(root, "inputs", seeds=[0])
        assert json.loads(path.read_text())["seeds"] == [0]
        assert adjudicate.verify(root, "inputs")[0] == []
        with pytest.raises(ValueError, match="no record for m1/D01-s7.json"):
            adjudicate.lock(root, "inputs", seeds=[0, 7])     # widened, not adjudicated
        _add_doc(root, "D01-s7", seed=7)
        with pytest.raises(ValueError, match="give a reason"):
            adjudicate.lock(root, "inputs", seeds=[0, 7])
        adjudicate.lock(root, "inputs", seeds=[0, 7], reason="seed 7 joins (owner)")
        assert adjudicate.verify(root, "inputs")[0] == []

    def test_a_deleted_lock_file_needs_a_reason(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        path, _ = adjudicate.lock(root, "inputs")
        path.unlink()
        with pytest.raises(ValueError, match="lock file was missing"):
            adjudicate.lock(root, "inputs")
        adjudicate.lock(root, "inputs", reason="lock file lost; relabel none")
        assert json.loads(path.read_text())["relocks"][0]["reason"].startswith("lock file")

    def test_a_leaking_row_cannot_expect_exit_0(self, tmp_path) -> None:
        root = _root_with_record(tmp_path)
        rec = json.loads((root / "adjudication/inputs/m1/D01-s0.json").read_text())
        rec["instances"][0]["override"] = {"reason": "x", "expected_exit": 0,
                                           "cell": "orphaned.font"}
        with pytest.raises(ValueError, match="cannot expect exit 0"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"reason": "x", "expected_exit": 1}
        with pytest.raises(ValueError, match="must give its cell"):
            adjudicate.finalize_record(rec)


class TestCalibration:
    def test_m0_fails_when_an_instance_is_not_found_whole(self, tmp_path, monkeypatch) -> None:
        from redactor_set import oracle_cli

        root = tmp_path
        (root / "inputs").mkdir()
        (root / "inputs" / "D01-s0.pdf").write_bytes(b"%PDF-1.7\n")
        truth = DocTruth("D01", 0, "chrome", 1, "t", [T1_SSN])
        (root / "inputs" / "D01-s0.truth.json").write_text(truth.to_json())
        stored = oracle.Evidence("R2", "content", "whole", "live", role="content",
                                 form="123-45-6789")
        seen = oracle.Evidence("V", "page 1 box", "whole", "live", form="123-45-6789", page=0)
        for cls, ev, code in (("partial", [], 1),
                              # The render alone does not prove the stored channels.
                              ("recoverable-visible", [seen], 1),
                              ("recoverable-visible", [seen, stored], 0)):
            monkeypatch.setattr(oracle, "run", lambda *a, cls=cls, ev=ev, **k: oracle.FileResult(
                "inputs/D01-s0.pdf", "0", "D01", "m0",
                [oracle.InstanceResult(0, "SSN1", "T1", cls, ev)], {"SSN1": cls}, "leak"))
            assert oracle_cli.run_oracle(root, "inputs", ["m0"], []) == code


# ── End to end (needs poppler and tesseract) ───────────────────────────

@pytest.mark.skipif(not (shutil.which("pdftoppm") and shutil.which("tesseract")),
                    reason="needs poppler (pdftoppm) and tesseract")
class TestEndToEnd:
    def _orphaned(self, tmp_path: Path) -> Path:
        """A page whose drawn SSN was redacted and saved without garbage
        collection (PyMuPDF, as some redaction tools do): the original
        content stream stays in the file, referenced by nothing."""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "SSN 123-45-6789", fontsize=24)
        for r in page.search_for("123-45-6789"):
            page.add_redact_annot(r, fill=(0, 0, 0))
        page.apply_redactions()
        path = tmp_path / "orphan.pdf"
        doc.save(path, garbage=0)
        return path

    def _page(self, tmp_path: Path, text: str) -> Path:
        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(612, 792))
        font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                  BaseFont=pikepdf.Name.Helvetica,
                                  Encoding=pikepdf.Name.WinAnsiEncoding)
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode())
        path = tmp_path / "e.pdf"
        pdf.save(path)
        return path

    def _truth(self) -> DocTruth:
        return DocTruth("X", 0, "test", 1, "t", [
            Instance("SSN1", "123-45-6789", "T1", "page-text", 0, [72, 70, 260, 96],
                     fingerprints=[b"123-45-6789".hex()])])

    def test_visible_hidden_and_removed(self, tmp_path) -> None:
        visible = oracle.run(self._page(tmp_path, "SSN 123-45-6789"), self._truth(),
                             output="e", sha256="0", method="m0", work=tmp_path / "w1")
        assert visible.secrets == {"SSN1": "recoverable-visible"}
        hidden = oracle.run(self._orphaned(tmp_path), self._truth(), output="e", sha256="0",
                            method="m1", work=tmp_path / "w2")
        assert hidden.secrets == {"SSN1": "recoverable-hidden"}
        assert any(e.storage == "orphaned" for e in hidden.instances[0].evidence)
        removed = oracle.run(self._page(tmp_path, "SSN"), self._truth(), output="e",
                             sha256="0", method="m2", work=tmp_path / "w3")
        assert removed.truth == "clean"
