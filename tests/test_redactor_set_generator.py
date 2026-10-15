"""The real-redactor set's generator (eval/redactor_set): the fabricated
persona and config, the truth manifest, fingerprints, the preflight, the
local-root guard, the runner, and its independence from the verifier.

Generating real documents needs Chrome, poppler, tesseract and macOS's
cupsfilter, so it is never run here; the pieces it is built from are.
Everything here needs only the ``test`` extra (pdfminer.six is in it).
"""

from __future__ import annotations

import ast
import getpass
import json
import re
import signal
import sys
from pathlib import Path

import pikepdf
import pytest
import yaml

from redactor_set import generate as gen
from redactor_set import locate, persona, producers
from redactor_set.run import run_set
from redactor_set.truth import DocTruth, Instance

PKG = Path(__file__).resolve().parents[1] / "eval" / "redactor_set"


# ── Persona ────────────────────────────────────────────────────────────

class TestPersona:
    def test_no_two_secrets_share_a_digit_run(self) -> None:
        assert persona.shared_digit_runs() == []

    def test_the_shared_run_check_is_live(self, monkeypatch) -> None:
        # Guard against a vacuous check: a form sharing six of SSN1's
        # digits must be caught.
        monkeypatch.setitem(persona.VARIANTS, "000665140", "ACCT")
        assert persona.shared_digit_runs()

    def test_values_cannot_be_real(self) -> None:
        """Format checks only: each value uses a range that is never
        issued (or a published test number), so no real person or account
        can be named."""
        v = {s.id: s.value for s in persona.SECRETS}
        for ssn in (v["SSN1"], v["SSN2"]):
            assert ssn.startswith("666-")                       # area 666 is never issued
        assert v["EIN"].startswith("00-")                       # EIN prefix 00 is never issued
        rout = [int(c) for c in v["ROUT"]]
        assert v["ROUT"].startswith("99")                       # unassigned Fed prefix
        assert sum(d * w for d, w in zip(rout, [3, 7, 1] * 3)) % 10 == 0
        digits = [int(c) for c in v["CARD"].replace(" ", "")]
        assert v["CARD"].startswith("4111")                     # network test number
        assert sum(sum(divmod(d * 2, 10)) if i % 2 == len(digits) % 2 else d
                   for i, d in enumerate(digits)) % 10 == 0           # Luhn
        assert v["EMAIL"].endswith("@example.com")

    def test_variants_name_real_ids_and_differ_from_values(self) -> None:
        values = {s.value for s in persona.SECRETS}
        for form, sid in persona.VARIANTS.items():
            assert sid in persona.BY_ID and form not in values

    def test_no_ssn_is_sequential(self) -> None:
        # Owner decision 2026-10-15: a sequential SSN matches line labels
        # 1..9 read across lines, which no real SSN would.
        for sid in ("SSN1", "SSN2"):
            digits = persona.BY_ID[sid].value.replace("-", "")
            steps = {int(b) - int(a) for a, b in zip(digits, digits[1:])}
            assert steps not in ({1}, {-1}, {0})
            assert "1234" not in digits and "6789" not in digits


class TestConfig:
    def test_config_keys_and_values_round_trip(self) -> None:
        cfg = yaml.safe_load(persona.redactor_config_yaml())
        assert set(cfg) == {"entity_types", "exact_values", "patterns", "scrub_metadata"}
        assert cfg["entity_types"] == []                       # LLM detection off
        assert cfg["exact_values"] == [s.value for s in persona.SECRETS]
        assert all(isinstance(v, str) for v in cfg["exact_values"])   # quoted: no ints
        assert cfg["patterns"] == list(persona.PATTERNS)

    def test_ssn_pattern_is_dashed_only(self) -> None:
        ssn = re.compile(persona.PATTERNS[0], re.IGNORECASE)
        assert ssn.search(persona.BY_ID["SSN2"].value)
        assert not ssn.search("666382914")                    # the T2 variant it misses

    def test_write_config_refuses_a_different_file(self, tmp_path) -> None:
        path = gen.write_config(tmp_path)
        assert gen.write_config(tmp_path) == path              # same content: fine
        path.write_text("exact_values: []\n")
        with pytest.raises(SystemExit):
            gen.write_config(tmp_path)


# ── Truth manifest ─────────────────────────────────────────────────────

class TestTruth:
    def test_round_trip(self) -> None:
        inst = Instance("SSN1", "666-51-4073", "T1", "page-text", 0, [1, 2, 3, 4],
                        fingerprints=["31"])
        truth = DocTruth("D01", 0, "chrome", 1, "Title", [inst])
        assert DocTruth.from_dict(json.loads(truth.to_json())) == truth

    @pytest.mark.parametrize("kwargs", [
        {"secret": "NOPE"}, {"loc": "Z9"}, {"encoding": "smoke"}, {"bbox": [1, 2, 3]},
        {"written": "731904628155"},          # ACCT's value, filed under SSN1
    ])
    def test_rejects_bad_instances(self, kwargs) -> None:
        base = {"secret": "SSN1", "written": "666-51-4073", "loc": "T1",
                "encoding": "page-text"}
        with pytest.raises(ValueError):
            Instance(**{**base, **kwargs})


# ── Fingerprints ───────────────────────────────────────────────────────

CMAP = b"""/CIDInit /ProcSet findresource begin
begincmap
1 begincodespacerange <0000> <FFFF> endcodespacerange
2 beginbfchar
<0003> <0020>
<0010> <002D>
endbfchar
2 beginbfrange
<0014> <001C> <0031>
<0040> <0041> [<0041> <0042>]
endbfrange
endcmap
"""


def _type0_pdf(path: Path) -> None:
    pdf = pikepdf.new()
    tu = pdf.make_stream(CMAP)
    font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0,
                              BaseFont=pikepdf.Name("/AAAAAA+Fab"),
                              Encoding=pikepdf.Name("/Identity-H"), ToUnicode=tu)
    simple = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                BaseFont=pikepdf.Name.Helvetica,
                                Encoding=pikepdf.Name.WinAnsiEncoding)
    page = pdf.add_blank_page()
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font, F2=simple))
    pdf.save(path)


def _identity_h_page(path: Path, text: str) -> None:
    """A page drawing *text* in a font-program-less Identity-H font whose
    codes are 1..n (so the bytes never spell the text)."""
    pdf = pikepdf.new()
    codes = {c: i + 1 for i, c in enumerate(dict.fromkeys(text))}
    cmap = ("/CIDInit /ProcSet findresource begin begincmap\n"
            "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            f"{len(codes)} beginbfchar\n"
            + "".join(f"<{n:04X}> <{ord(c):04X}>\n" for c, n in codes.items())
            + "endbfchar endcmap\n").encode()
    cid = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2,
                             BaseFont=pikepdf.Name("/AAAAAA+Fab"), DW=500,
                             CIDSystemInfo=pikepdf.Dictionary(Registry=pikepdf.String("Adobe"),
                                                              Ordering=pikepdf.String("Identity"),
                                                              Supplement=0))
    font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0,
                              BaseFont=pikepdf.Name("/AAAAAA+Fab"),
                              Encoding=pikepdf.Name("/Identity-H"),
                              DescendantFonts=pikepdf.Array([pdf.make_indirect(cid)]),
                              ToUnicode=pdf.make_stream(cmap))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    hexed = "".join(f"{codes[c]:04X}" for c in text)
    page.obj.Contents = pdf.make_stream(f"BT /F1 12 Tf 72 700 Td <{hexed}> Tj ET".encode())
    pdf.save(path)


class TestFingerprints:
    def test_parse_tounicode(self) -> None:
        m = locate.parse_tounicode(CMAP)
        assert m[" "] == b"\x00\x03" and m["-"] == b"\x00\x10"
        assert m["1"] == b"\x00\x14" and m["9"] == b"\x00\x1c"     # incrementing range
        assert m["A"] == b"\x00\x40" and m["B"] == b"\x00\x41"     # array range

    def test_codes_for_a_cid_font_and_ascii_for_a_simple_one(self, tmp_path) -> None:
        _type0_pdf(tmp_path / "f.pdf")
        with pikepdf.open(tmp_path / "f.pdf") as pdf:
            # A fixture value the test CMap covers (digits 1-9), not a persona one.
            prints = locate.fingerprints(pdf, 0, "123-45-6789")
        assert sorted(prints) == sorted(["00140015001600100017001800100019001a001b001c",
                                         b"123-45-6789".hex()])

    def test_a_font_missing_a_character_gives_no_print(self, tmp_path) -> None:
        _type0_pdf(tmp_path / "f.pdf")
        with pikepdf.open(tmp_path / "f.pdf") as pdf:
            # "Z" is in no CMap: only the simple font's ASCII is left.
            assert locate.fingerprints(pdf, 0, "Z1") == [b"Z1".hex()]


# ── Scan geometry ──────────────────────────────────────────────────────

class TestSkew:
    def test_no_skew_is_identity(self) -> None:
        box = [10.0, 20.0, 30.0, 40.0]
        assert producers.Skew(0.0, 1700, 2200).map_box(box, 200 / 72) == pytest.approx(box)

    def test_skew_grows_the_box_and_keeps_its_centre_near(self) -> None:
        box = [300.0, 390.0, 312.0, 402.0]              # at the page centre
        out = producers.Skew(1.5, 1700, 2200).map_box(box, 200 / 72)
        assert out[0] < box[0] and out[2] > box[2]
        assert (out[0] + out[2]) / 2 == pytest.approx(306, abs=1)


# ── Preflight ──────────────────────────────────────────────────────────

def _truth(doc: str = "X") -> DocTruth:
    return DocTruth(doc, 0, "test", 1, "t", [Instance("SSN1", "666-51-4073", "M1", "string")])


class TestPreflight:
    def test_a_plain_file_passes(self, tmp_path) -> None:
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.docinfo["/Title"] = "Fabricated"
        pdf.save(tmp_path / "ok.pdf", deterministic_id=True)
        gen.preflight(tmp_path / "ok.pdf", _truth())

    def test_refuses_the_login_name(self, tmp_path) -> None:
        if len(getpass.getuser()) < 3:
            pytest.skip("login name too short to search for")
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.docinfo["/Author"] = getpass.getuser()
        pdf.save(tmp_path / "bad.pdf")
        with pytest.raises(gen.PreflightError, match="login name"):
            gen.preflight(tmp_path / "bad.pdf", _truth())

    def test_refuses_a_needle_drawn_in_glyph_codes(self, tmp_path, monkeypatch) -> None:
        """Chrome's default header prints the file URL in an Identity-H
        font: no raw or decoded byte holds the text, only the extracted
        page text does (and in another case than the needle's)."""
        monkeypatch.setattr(gen, "private_needles", lambda: ["fabuser"])
        _identity_h_page(tmp_path / "hdr.pdf", "FabUser")
        raw = (tmp_path / "hdr.pdf").read_bytes().lower()
        assert b"fabuser" not in raw and "fabuser".encode("utf-16-be") not in raw
        with pytest.raises(gen.PreflightError, match="login name"):
            gen.preflight(tmp_path / "hdr.pdf", _truth())

    @pytest.mark.parametrize("value", [
        pikepdf.String("FabUser".encode("utf-16-be")),     # UTF-16 (no BOM)
        pikepdf.String(b"\xfe\xff" + "FabUser".encode("utf-16-be")),
        pikepdf.String("Fab(User)"),                        # escaped literal
    ])
    def test_refuses_a_needle_in_an_encoded_string(self, tmp_path, monkeypatch, value) -> None:
        monkeypatch.setattr(gen, "private_needles", lambda: ["fabuser", "fab(user)"])
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.docinfo["/Author"] = value
        pdf.save(tmp_path / "s.pdf")
        with pytest.raises(gen.PreflightError, match="login name"):
            gen.preflight(tmp_path / "s.pdf", _truth())

    def test_refuses_a_needle_in_a_hex_or_octal_string_as_written(self, tmp_path,
                                                                 monkeypatch) -> None:
        monkeypatch.setattr(gen, "private_needles", lambda: ["fabuser"])
        for i, written in enumerate((b"<46616255736572>", b"(\\106ab\\125ser)")):
            path = tmp_path / f"w{i}.pdf"
            pdf = pikepdf.new()
            pdf.add_blank_page()
            pdf.docinfo["/Author"] = "PLACEHOLDER"
            pdf.save(path, compress_streams=False, object_stream_mode=pikepdf.ObjectStreamMode.disable)
            path.write_bytes(path.read_bytes().replace(b"(PLACEHOLDER)", written))
            assert b"fabuser" not in path.read_bytes().lower()
            with pytest.raises(gen.PreflightError, match="login name"):
                gen.preflight(path, _truth())

    def test_needles_cover_file_urls_and_this_account(self) -> None:
        if len(getpass.getuser()) < 3:
            pytest.skip("login name too short to search for")
        needles = gen.private_needles()
        assert "file://" in needles and getpass.getuser().lower() in needles
        assert all(n == n.lower() and len(n) >= 3 for n in needles)

    def test_refuses_a_secret_inside_a_font_program(self, tmp_path) -> None:
        pdf = pikepdf.new()
        page = pdf.add_blank_page()
        font_file = pdf.make_stream(b"\x00\x01glyf666-51-4073\x00")
        desc = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor,
                                                    FontFile2=font_file))
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(
            F1=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType,
                                  FontDescriptor=desc)))
        pdf.save(tmp_path / "font.pdf")
        with pytest.raises(gen.PreflightError, match="font stream"):
            gen.preflight(tmp_path / "font.pdf", _truth())

    def test_a_real_16_byte_static_id_passes(self, tmp_path) -> None:
        # qpdf's static /ID is 32 hex digits in a row: the config's card
        # pattern needs a word boundary after 16 digits, so it cannot match.
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.save(tmp_path / "static.pdf", static_id=True)
        assert b"<31415926535897932384626433832795>" in (tmp_path / "static.pdf").read_bytes()
        gen.preflight(tmp_path / "static.pdf", _truth())

    def test_refuses_an_id_a_card_pattern_matches(self, tmp_path) -> None:
        pdf = pikepdf.new()
        pdf.add_blank_page()
        # An 8-byte /ID whose hex text is 16 digits: the card pattern
        # matches it whole. (qpdf keeps an existing first /ID element.)
        pdf.trailer.ID = pikepdf.Array([pikepdf.String(bytes.fromhex("4111111111111111")),
                                        pikepdf.String(b"\xaa" * 8)])
        pdf.save(tmp_path / "id.pdf")
        with pytest.raises(gen.PreflightError, match="/ID"):
            gen.preflight(tmp_path / "id.pdf", _truth())


# ── Planting checks (needs pdfminer.six) ───────────────────────────────

class TestFinalize:
    def _page_with(self, path: Path, text: str) -> None:
        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(612, 792))
        font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                  BaseFont=pikepdf.Name.Helvetica,
                                  Encoding=pikepdf.Name.WinAnsiEncoding)
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode())
        pdf.save(path)

    def test_declared_value_is_located(self, tmp_path) -> None:
        from redactor_set.build import Built, TextSpec, finalize
        self._page_with(tmp_path / "a.pdf", "SSN 666-51-4073")
        truth = finalize("X", 0, Built(tmp_path / "a.pdf", "test", "t",
                                       text=[TextSpec("SSN1", "666-51-4073", "T1", 1)]))
        (inst,) = truth.instances
        assert inst.page == 0 and inst.fingerprints == [b"666-51-4073".hex()]
        # Baseline 700 pt up from the bottom: the top-left frame puts the
        # glyph tops a little above 792 - 700 = 92.
        assert inst.bbox is not None and 78 < inst.bbox[1] < 92 < inst.bbox[3]

    def test_undeclared_or_miscounted_values_are_refused(self, tmp_path) -> None:
        from redactor_set.build import Built, PlantError, TextSpec, finalize
        self._page_with(tmp_path / "b.pdf", "SSN 666-51-4073 acct 731904628155")
        with pytest.raises(PlantError, match="731904628155"):
            finalize("X", 0, Built(tmp_path / "b.pdf", "test", "t",
                                   text=[TextSpec("SSN1", "666-51-4073", "T1", 1)]))
        with pytest.raises(PlantError, match="drawn 1 times"):
            finalize("X", 0, Built(tmp_path / "b.pdf", "test", "t",
                                   text=[TextSpec("SSN1", "666-51-4073", "T1", 2),
                                         TextSpec("ACCT", "731904628155", "T1", 1)]))


    def test_filler_holding_a_secret_digit_run_is_refused(self, tmp_path) -> None:
        from redactor_set.build import Built, PlantError, finalize
        self._page_with(tmp_path / "c.pdf", "Wages 62,048.57")      # the EIN's 4857
        with pytest.raises(PlantError, match="piece of EIN"):
            finalize("X", 0, Built(tmp_path / "c.pdf", "test", "t", clean_control=True))

    def test_a_declared_value_is_not_residue(self, tmp_path) -> None:
        from redactor_set.build import Built, TextSpec, finalize
        self._page_with(tmp_path / "d.pdf", "EIN 00-4857261")
        finalize("X", 0, Built(tmp_path / "d.pdf", "test", "t",
                               text=[TextSpec("EIN", "00-4857261", "T1", 1)]))

    def test_an_undeclared_string_is_refused(self, tmp_path) -> None:
        from redactor_set.build import Built, PlantError, finalize
        self._page_with(tmp_path / "e.pdf", "Nothing here")
        with pikepdf.open(tmp_path / "e.pdf", allow_overwriting_input=True) as pdf:
            pdf.docinfo["/Author"] = "Jordan Q. Testperson"
            pdf.save(tmp_path / "e.pdf")
        with pytest.raises(PlantError, match="string store"):
            finalize("X", 0, Built(tmp_path / "e.pdf", "test", "t", clean_control=True))
        # Declared, it passes; declared but absent, it is refused.
        ok = Instance("NAME1", "Jordan Q. Testperson", "M1", "string")
        finalize("X", 0, Built(tmp_path / "e.pdf", "test", "t", other=[ok]))
        ghost = Instance("SSN1", "666-51-4073", "M1", "string")
        with pytest.raises(PlantError, match="not stored"):
            finalize("X", 0, Built(tmp_path / "e.pdf", "test", "t", other=[ok, ghost]))


    def _blank(self, path: Path) -> None:
        self._page_with(path, "Nothing here")

    def test_undeclared_xmp_attachment_and_stream_are_refused(self, tmp_path) -> None:
        from redactor_set.build import Built, PlantError, finalize
        cases = []
        self._blank(tmp_path / "x.pdf")
        with pikepdf.open(tmp_path / "x.pdf", allow_overwriting_input=True) as pdf:
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
                m["dc:creator"] = ["Jordan Q. Testperson"]
            pdf.save(tmp_path / "x.pdf")
        cases.append(("x.pdf", "xmp store"))
        self._blank(tmp_path / "a.pdf")
        with pikepdf.open(tmp_path / "a.pdf", allow_overwriting_input=True) as pdf:
            pdf.attachments["d.csv"] = pikepdf.AttachedFileSpec(pdf, b"ssn,666-51-4073\n")
            pdf.save(tmp_path / "a.pdf")
        cases.append(("a.pdf", "embedded store"))
        self._blank(tmp_path / "o.pdf")
        with pikepdf.open(tmp_path / "o.pdf", allow_overwriting_input=True) as pdf:
            pdf.Root.Junk = pdf.make_stream(b"acct 731904628155")
            pdf.save(tmp_path / "o.pdf")
        cases.append(("o.pdf", "other store"))
        for name, match in cases:
            with pytest.raises(PlantError, match=match):
                finalize("X", 0, Built(tmp_path / name, "test", "t", clean_control=True))

    def test_residue_in_a_string_store_is_refused(self, tmp_path) -> None:
        from redactor_set.build import Built, PlantError, finalize
        self._blank(tmp_path / "r.pdf")
        with pikepdf.open(tmp_path / "r.pdf", allow_overwriting_input=True) as pdf:
            pdf.docinfo["/Author"] = "Testperson"
            pdf.docinfo["/CreationDate"] = "D:20260314000000Z"      # dates are exempt
            pdf.save(tmp_path / "r.pdf")
        with pytest.raises(PlantError, match="Author holds 'Testperson'"):
            finalize("X", 0, Built(tmp_path / "r.pdf", "test", "t", clean_control=True))


class TestFiller:
    def test_money_and_dates_never_hold_secret_residue(self) -> None:
        import random

        from redactor_set.templates import _date, _money
        rng = random.Random(0)
        for _ in range(3000):
            assert not persona.residue_in(_money(rng, 0, 90000))
            assert not persona.residue_in(_date(rng))

    def test_residue_detection_is_live(self) -> None:
        assert persona.residue_in("62,048.57") and persona.residue_in("03/14")
        assert persona.residue_in("Fictional Federal Bank")
        assert not persona.residue_in("Anytown Federal Bank 11/19 1,042.50")


# ── Local root and runner ──────────────────────────────────────────────

class TestLocalRoot:
    def test_inside_is_accepted(self) -> None:
        assert gen.local_root(gen.LOCAL_ROOT / "x") == (gen.LOCAL_ROOT / "x").resolve()

    def test_outside_is_refused_unless_allowed(self, tmp_path) -> None:
        with pytest.raises(SystemExit):
            gen.local_root(tmp_path)
        assert gen.local_root(tmp_path, allow_outside=True) == tmp_path.resolve()

    def test_the_local_root_is_gitignored(self) -> None:
        ignore = (PKG.parents[1] / ".gitignore").read_text().splitlines()
        assert "/eval/scorecard/redactor_set_local/" in ignore


FAKE_TOOL = """import shutil, sys
from pathlib import Path
src = Path(sys.argv[1]); assert sys.argv[2] == "--config" and Path(sys.argv[3]).exists()
shutil.copyfile(src, src.with_name(src.stem + "_redacted.pdf"))
print("fake tool: done")
"""


class TestRunner:
    def test_both_methods_are_produced(self, tmp_path, monkeypatch) -> None:
        tool = tmp_path / "tool.py"
        tool.write_text(FAKE_TOOL)
        (tmp_path / "probes").mkdir()
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.save(tmp_path / "probes" / "T1.pdf")
        config = gen.write_config(tmp_path)
        monkeypatch.setenv("REDACTOR_CMD", f"{sys.executable} -I {tool}")
        monkeypatch.setenv("REDACTOR_PYTHON", sys.executable)   # has pymupdf
        record = run_set(tmp_path, "probes", config)
        entry = record["documents"]["T1"]                      # type: ignore[index]
        assert entry["exit"] == 0
        assert (tmp_path / entry["m1"]).exists() and (tmp_path / entry["m2"]).exists()
        assert json.loads((tmp_path / "work" / "probes" / "run.json").read_text())["gc_pymupdf"]

    def _setup(self, tmp_path, monkeypatch, tool_source: str) -> Path:
        tool = tmp_path / "tool.py"
        tool.write_text(tool_source)
        (tmp_path / "probes").mkdir()
        pdf = pikepdf.new()
        pdf.add_blank_page()
        pdf.save(tmp_path / "probes" / "T1.pdf")
        monkeypatch.setenv("REDACTOR_CMD", f"{sys.executable} -I {tool}")
        monkeypatch.setenv("REDACTOR_PYTHON", sys.executable)
        return gen.write_config(tmp_path)

    def test_a_timeout_is_recorded_and_the_tool_ended(self, tmp_path, monkeypatch) -> None:
        config = self._setup(tmp_path, monkeypatch,
                             "import time\nprint('started', flush=True)\ntime.sleep(60)\n")
        record = run_set(tmp_path, "probes", config, timeout=1.0)
        entry = record["documents"]["T1"]                      # type: ignore[index]
        assert entry["exit"] == "timeout" and entry["m1"] is None and entry["m2"] is None
        assert "started" in (tmp_path / "work/probes/m1/T1/stdout.txt").read_text()

    def test_a_group_left_with_unreaped_members_still_times_out(self, tmp_path,
                                                                 monkeypatch) -> None:
        # On macOS os.killpg reports EPERM when only unreaped members are
        # left in the group: the run is still recorded as a timeout, not a
        # crash that loses the whole run record.
        from redactor_set import run as run_mod

        config = self._setup(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
        real_killpg = run_mod.os.killpg
        calls: list[int] = []

        def killpg(pid: int, sig: int) -> None:
            calls.append(sig)
            real_killpg(pid, signal.SIGKILL)          # really end it, then report EPERM
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(run_mod.os, "killpg", killpg)
        record = run_set(tmp_path, "probes", config, timeout=1.0)
        assert record["documents"]["T1"]["exit"] == "timeout"     # type: ignore[index]
        assert calls

    def test_stale_outputs_are_removed_before_a_run(self, tmp_path, monkeypatch) -> None:
        config = self._setup(tmp_path, monkeypatch, "raise SystemExit(1)\n")
        for m in ("m1", "m2"):
            stale = tmp_path / "work" / "probes" / m / "T1.pdf"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b"%PDF-1.4 stale")
        record = run_set(tmp_path, "probes", config)
        assert record["documents"]["T1"]["exit"] == 1          # type: ignore[index]
        assert not (tmp_path / "work/probes/m1/T1.pdf").exists()
        assert not (tmp_path / "work/probes/m2/T1.pdf").exists()

    def test_needs_the_command_from_the_environment(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("REDACTOR_CMD", raising=False)
        with pytest.raises(SystemExit, match="REDACTOR_CMD"):
            run_set(tmp_path, "probes", tmp_path / "c.yaml")


# ── Independence and hygiene ───────────────────────────────────────────

# The verifier and the library it (and the redaction tool) reads with:
# the generator writes nothing with PyMuPDF, so its inputs are not all
# shaped the way both readers expect.
FORBIDDEN = ("redaction_verifier", "verify", "caselib", "scorecard", "pymupdf", "fitz")


def _forbidden_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        found += [n for n in names if n.split(".")[0] in FORBIDDEN]
    return found


class TestIndependence:
    def test_imports_nothing_from_the_verifier_or_pymupdf(self) -> None:
        for path in sorted(PKG.glob("*.py")):
            assert _forbidden_imports(path.read_text()) == [], path.name

    @pytest.mark.parametrize("source", [
        "import fitz", "import pymupdf as m", "from redaction_verifier.model import X",
        "def f():\n    import verify", "from caselib import model"])
    def test_the_guard_catches_each_form(self, source) -> None:
        assert _forbidden_imports(source)

    def test_no_local_paths_in_the_source(self) -> None:
        for path in sorted(PKG.glob("*.py")):
            text = path.read_text()
            for needle in ("/Users/", "/home/", "/opt/", "/envs/"):
                assert needle not in text, f"{path.name}: {needle}"
