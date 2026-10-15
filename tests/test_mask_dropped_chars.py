"""#66, review finding B6: a masked sample must not show a character the
matcher ignores when that character belongs to an exact value.

The matcher's normalizer keeps only ASCII letters and digits, so the exact
value "93Ж8" is searched as "938" and found in "x93ΩЖ8" across the Greek
and Cyrillic letters. The wide fold keeps those letters, so before the fix
the pattern sample "x93ΩЖ" showed its tail "ΩЖ", and "Ж" is a character
of the value. All data here is fabricated.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pymupdf as fitz
import pytest

import verify

from redaction_verifier.matching import mask, normalize_string
from redaction_verifier.model import MaskIndex, fold_wide

from .conftest import run_verify


def _pdf(path: Path, text: str) -> Path:
    # The bundled CJK font also covers Greek and Cyrillic, so the text
    # layer reads back exactly what was written.
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontbuffer=fitz.Font("cjk").buffer)
    page.insert_text((72, 72), text, fontname="F0")
    doc.save(path)
    doc.close()
    return path


def _rules(path: Path, value: str, pattern: str) -> Path:
    path.write_text(json.dumps([
        {"name": "v", "value": value},
        {"name": "p", "pattern": pattern},
    ]))
    return path


def _index(*values: str) -> MaskIndex:
    # Both forms verify.py indexes per value: the searched one and the wide fold.
    return MaskIndex(f for v in values for f in (normalize_string(v), fold_wide(v)) if f)


class TestThroughTheCli:
    CASES = [
        # (document text, exact value, pattern, characters that must not appear)
        ("Ref x93ΩЖ8 end", "93Ж8", r"x93\S\S", ("Ж", "ж")),
        ("Ref ab1жΩж2 end", "1жж2", r"ab1\S{3}", ("ж", "Ж")),
    ]

    @pytest.mark.parametrize("text, value, pattern, hidden", CASES)
    def test_json_sample(self, tmp_path, text, value, pattern, hidden) -> None:
        pdf = _pdf(tmp_path / "doc.pdf", text)
        rules = _rules(tmp_path / "r.json", value, pattern)
        out = tmp_path / "report.json"
        code = verify.main(["--target", str(pdf), "--secrets", str(rules), "--json", str(out)])
        data = json.loads(out.read_text())
        assert code == 1
        by_rule = {(f["layer"], f["rule"]): f["sample"] for f in data["findings"]}
        # The exact value was found and the pattern matched on the text layer.
        assert any(layer == "Text" and s == "" for (layer, _), s in by_rule.items())
        assert any(layer == "Text" and s == "****" for (layer, _), s in by_rule.items())
        samples = {f["sample"] for f in data["findings"]}
        assert samples <= {"", "****"}
        dumped = json.dumps(data, ensure_ascii=False)
        assert not any(c in dumped for c in hidden)

    @pytest.mark.parametrize("text, value, pattern, hidden", CASES)
    def test_text_report(self, tmp_path, text, value, pattern, hidden) -> None:
        pdf = _pdf(tmp_path / "doc.pdf", text)
        rules = _rules(tmp_path / "r.json", value, pattern)
        result = run_verify(pdf, rules)
        assert result.returncode == 1, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if "STILL PRESENT" in ln]
        assert any("Text" in ln and "****" in ln for ln in lines)
        assert not any(c in result.stdout for c in hidden)


@pytest.mark.parametrize("sample, values, shown", [
    ("x93ΩЖ", ("93Ж8",), "****"),          # the reported leak
    ("x9Ωж", ("93Ж8",), "****"),           # its direct form, lower case
    ("ab1жΩж", ("1жж2",), "****"),         # a tail whose pairs are all split by junk
    ("x1жΩ", ("1жж2",), "****"),           # two characters, one of them the value's
    ("x9७३", ("9३8",), "****"),  # Devanagari digit in the tail and the value
    ("x93ΩΩ", ("93Ж8",), "****ΩΩ"),        # control: no tail character is the value's
    ("ab-Ж", ("12",), "****-Ж"),           # control: the value has no such letter
])
def test_mask_dropped_characters(sample, values, shown) -> None:
    assert mask(sample, _index(*values)) == shown


# ── Seeded property test, drawn from the review's fuzz ───────────────────
_ALNUM = "0123456789abcAB" + "жØé"
# Separators, soft hyphen, zero-width space, a combining mark, non-ASCII
# letters, digits of other scripts (Arabic-Indic, Devanagari).
_JUNK = "-. /_­​́жΩ٣३"
_FULLWIDTH = {c: chr(ord(c) + 0xFEE0) for c in "0123456789abcAB"}


def _leaks(seed: int, n: int) -> list[tuple[str, list[str], str, str]]:
    """Render an exact value with junk between its characters and a random
    surrounding, cut a random sample, and report every mask whose revealed
    tail holds a glyph of the value."""
    rng = random.Random(seed)
    found: list[tuple[str, list[str], str, str]] = []
    for _ in range(n):
        value = "".join(rng.choice(_ALNUM) for _ in range(rng.randint(1, 8)))
        if not normalize_string(value):
            continue
        others = ["".join(rng.choice(_ALNUM) for _ in range(rng.randint(2, 8)))
                  for _ in range(rng.randint(0, 2))]
        exact = [value, *others]
        doc: list[str] = []
        value_at: list[int] = []
        doc.extend(rng.choice(_ALNUM + _JUNK) for _ in range(rng.randint(0, 8)))
        for i, c in enumerate(value):
            if i and rng.random() < 0.3:
                doc.append(rng.choice(_JUNK))
            r = rng.random()
            if r < 0.15 and c in _FULLWIDTH:
                c = _FULLWIDTH[c]
            elif r < 0.3 and c.isalpha():
                c = c.upper() if c.islower() else c.lower()
            value_at.append(len(doc))
            doc.append(c)
        doc.extend(rng.choice(_ALNUM + _JUNK) for _ in range(rng.randint(0, 8)))
        a = rng.randint(0, len(doc) - 1)
        b = rng.randint(a + 1, len(doc))
        sample = "".join(doc[a:b])
        out = mask(sample, _index(*exact))
        reveal = len(out) - 4
        if reveal and any(b - reveal <= p < b for p in value_at):
            found.append((value, exact, sample, out))
    return found


@pytest.mark.parametrize("seed", [0, 1, 66])
def test_no_value_glyph_in_a_revealed_tail(seed) -> None:
    assert _leaks(seed, 20_000) == []


def test_property_test_catches_the_old_rule(monkeypatch) -> None:
    # Without the dropped-character check the same draw finds leaks, so the
    # property test above can fail.
    import redaction_verifier.matching.patterns as patterns

    monkeypatch.setattr(patterns, "_shows_a_dropped_value_char", lambda tail, index: False)
    assert _leaks(0, 20_000)
