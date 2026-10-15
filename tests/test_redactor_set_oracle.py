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
        "compensation", "Anytown 11/19 1,042.50", "Samplename spouse", "sample"])
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
        assert oracle.classify_instance(inst, [E("R1", "x", "whole"), E("V", "y", "whole")],
                                        near=False)[0] == "recoverable-visible"
        assert oracle.classify_instance(inst, [E("R2", "x", "whole", "orphaned")],
                                        near=False)[0] == "recoverable-hidden"
        assert oracle.classify_instance(inst, [E("R1", "f", "whole", counts=False)],
                                        near=False)[0] == "removed"
        assert oracle.classify_instance(inst, [E("V", "x", "partial:6789")],
                                        near=True) == ("partial", True)
        assert oracle.worst(["removed", "partial", "recoverable-hidden"]) == "recoverable-hidden"


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


# ── Adjudication records and the lock ──────────────────────────────────

def _record(**over: object) -> dict:
    rec = {"output": "work/x.pdf", "sha256": "0" * 64, "doc": "D01-s0", "method": "m2",
           "oracle_sha256": "1" * 64, "adjudicator": "agent", "date": "2026-10-14",
           "notes": "", "secrets": {}, "truth": "clean",
           "instances": [{"index": 0, "secret": "SSN1", "loc": "T1", "class": "recoverable-hidden",
                          "uncertain": False, "evidence": [], "override": None,
                          "expected_exit": 2}]}
    rec.update(over)
    return rec


class TestAdjudication:
    def test_finalize_derives_truth_and_exit(self) -> None:
        final = adjudicate.finalize_record(_record())
        assert final["truth"] == "leak" and final["expected_exit"] == 2
        assert final["secrets"] == {"SSN1": "recoverable-hidden"}

    def test_unfinished_records_are_refused(self) -> None:
        with pytest.raises(ValueError, match="no adjudicator"):
            adjudicate.finalize_record(_record(adjudicator=None))
        rec = _record()
        rec["instances"][0]["uncertain"] = True
        with pytest.raises(ValueError, match="unresolved"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"class": "removed"}
        with pytest.raises(ValueError, match="needs a reason"):
            adjudicate.finalize_record(rec)
        rec["instances"][0]["override"] = {"class": "removed", "reason": "crop is a box"}
        assert adjudicate.finalize_record(rec)["truth"] == "clean"

    def test_lock_and_verify(self, tmp_path) -> None:
        (tmp_path / "m2").mkdir()
        rec_path = tmp_path / "m2" / "D01-s0.json"
        rec_path.write_text(json.dumps(_record()))
        adjudicate.lock(tmp_path)
        assert adjudicate.verify(tmp_path) == []
        data = json.loads(rec_path.read_text())
        data["instances"][0]["expected_exit"] = 1
        rec_path.write_text(json.dumps(data))
        assert adjudicate.verify(tmp_path) == ["changed since the lock: m2/D01-s0.json"]
        with pytest.raises(ValueError, match="give a reason"):
            adjudicate.lock(tmp_path)
        adjudicate.lock(tmp_path, reason="cell corrected after review")
        assert adjudicate.verify(tmp_path) == []
        lock = json.loads((tmp_path / "adjudication.lock.json").read_text())
        assert lock["relocks"][0]["reason"] == "cell corrected after review"


# ── End to end (needs poppler and tesseract) ───────────────────────────

@pytest.mark.skipif(not (shutil.which("pdftoppm") and shutil.which("tesseract")),
                    reason="needs poppler (pdftoppm) and tesseract")
class TestEndToEnd:
    def _orphaned(self, tmp_path: Path) -> Path:
        """A page whose drawn SSN was redacted and saved without garbage
        collection (PyMuPDF, as a redaction tool does): the original
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
