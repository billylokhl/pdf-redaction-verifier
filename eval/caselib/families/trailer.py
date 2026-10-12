"""Secrets in the document trailer (docs/REDESIGN.md §8, K40 and K41).

A classic trailer dictionary is not an object: nothing in the file's
cross-reference table points at it. Until the trailer scan
(``verify.scan_trailers``), the Objects layer used it only to find the
root, and the Binary layer saw only qpdf's rewrite, where the file's first
/ID element comes back hex-encoded, the second is replaced and earlier
revisions' trailers are dropped, so every case below except the
cross-reference-stream one exited 0. A cross-reference *stream*'s
dictionary is an object and was always read, but labelled ORPHANED.

Tier (owner decision D): every trailer string is matched like any PDF
string, except that a pattern match in a /ID element made only of hex
digits and dashes (a generated identifier written as text) is manual
review."""

from __future__ import annotations

from pathlib import Path

from ..model import SSN, KnownGap, case, expect
from ..rawpdf import build, build_objstm, incremental_update, one_page, stream, text

SECRET = f"SSN {SSN}"
PAGE = stream(b"", text("Quarterly report"))
LIVE_SSN = expect(1, findings=(("SSN", "live"),))
PATTERN_RULE = ({"name": "Any SSN", "class": "ssn"},)
ID_REVIEW = expect(2, warnings=(("REVIEW_OBJECT_TEXT", "live"),))
MISTAKE = ("A producer that writes the document /ID (or a private trailer key) from "
           "document text, such as a title or case number, instead of a hash.")
RECOVERY = ("Open the file in a text editor and read the trailer dictionary after each "
            "cross-reference table; a hex string (<...>) needs decoding from hex first.")


def leak(id: str, cells: str, story: str, *, expected=LIVE_SSN, **kw):
    return case(id, truth="leak", cells=cells, expected=expected, story=story, writer="raw",
                mistake=kw.pop("mistake", MISTAKE), recovery=kw.pop("recovery", RECOVERY), **kw)


def _with_trailer(keys: bytes, pdf: bytes | None = None) -> bytes:
    """A one-page file (or *pdf*) with *keys* added to its only trailer."""
    raw = build(one_page(PAGE)) if pdf is None else pdf
    marker = b"/Root 1 0 R >>\nstartxref"
    assert raw.count(marker) == 1
    return raw.replace(marker, b"/Root 1 0 R " + keys + b" >>\nstartxref")


@leak("document.trailer-id-literal", "trailer.plain",
      "K40. The document /ID's first element is a literal string holding the SSN: the "
      "trailer dictionary is not an object, so the object walk never read it, and qpdf "
      "writes that element back hex-encoded, so the Binary layer's sweep never saw the "
      "digits either.")
def trailer_id_literal(path: Path) -> None:
    path.write_bytes(_with_trailer(b"/ID [(" + SECRET.encode() + b") <44556677>]"))


@leak("document.trailer-id-hex-second", "trailer.plain",
      "The SSN as hex-encoded text in the second element of the trailer's /ID: qpdf "
      "replaces that element with one of its own, so only a read of the file's own "
      "trailer finds it.")
def trailer_id_hex_second(path: Path) -> None:
    path.write_bytes(_with_trailer(b"/ID [<44556677> <" + SECRET.encode().hex().encode() + b">]"))


@leak("document.trailer-key-pattern-rule", "trailer.plain",
      "The SSN in a private (non-standard) trailer key, matched only by a pattern rule: "
      "qpdf keeps the key, but the Binary layer's sweep is value secrets only.",
      expected=expect(1, findings=(("Any SSN", "live"),)), rules=PATTERN_RULE)
def trailer_key_pattern_rule(path: Path) -> None:
    path.write_bytes(_with_trailer(b"/Note (" + SECRET.encode() + b")"))


@leak("document.trailer-key-value-rule", "trailer.plain",
      "The SSN in a private trailer key, matched by a value rule: the trailer scan "
      "reports it as found; before it, only the Binary layer's manual-review warning "
      "on qpdf's rewrite did.")
def trailer_key_value_rule(path: Path) -> None:
    path.write_bytes(_with_trailer(b"/Note (" + SECRET.encode() + b")"))


@leak("document.xref-stream-id", "trailer.plain",
      "The SSN as a literal /ID element in a cross-reference stream's dictionary (PDF "
      "1.5+), which is the file's trailer: that dictionary is an object, so it was always "
      "read, but labelled ORPHANED, since nothing references a cross-reference stream.")
def xref_stream_id(path: Path) -> None:
    raw = build_objstm(one_page(PAGE))
    marker = b"/Type /XRef /W [1 4 2]"
    assert raw.count(marker) == 1
    path.write_bytes(raw.replace(
        marker, b"/Type /XRef /ID [(" + SECRET.encode() + b") <44556677>] /W [1 4 2]"))


@leak("document.trailer-id-digits-pattern-rule", "trailer.plain",
      "The SSN alone, digits and dashes only, as the trailer's first /ID element, matched "
      "only by a pattern rule: an /ID element made only of hex digits and dashes looks "
      "like a generated identifier, which holds digit runs by coincidence, so the match "
      "is manual review (owner decision D), never certified clean.",
      expected=ID_REVIEW, rules=PATTERN_RULE)
def trailer_id_digits_pattern_rule(path: Path) -> None:
    path.write_bytes(_with_trailer(b"/ID [(" + SSN.encode() + b") <44556677>]"))


@leak("revision.earlier-trailer-id", "superseded.plain",
      "K41. An earlier revision's trailer holds the SSN as a literal second /ID element; "
      "the incremental update that follows writes a new trailer that keeps the first "
      "element and replaces the second, as PDF readers expect. The earlier-revision scan "
      "compares objects, not trailers, and qpdf writes only the current trailer.",
      expected=expect(1, findings=(("SSN", "superseded"),)),
      mistake=MISTAKE + " Then saving incrementally, which keeps the old trailer.")
def earlier_trailer_id(path: Path) -> None:
    first = _with_trailer(b"/ID [<44556677> (" + SECRET.encode() + b")]")
    updated = incremental_update(first, {4: stream(b"", text("Quarterly report, revised"))})
    marker = b" /Prev "
    assert updated.count(marker) == 1
    path.write_bytes(updated.replace(marker, b" /ID [<44556677> <8899aabb>] /Prev "))


@case("document.generated-id-digits", truth="clean", features="trailer.plain", writer="raw",
      expected=expect(0), rules=PATTERN_RULE,
      story="A clean document whose /ID elements are a generated identifier written as 32 "
            "hex characters of text that happen to contain a nine-digit run an SSN pattern "
            "accepts (about one such string in thirteen holds a run some built-in class "
            "accepts): manual review, by the owner's decision, rather than a false finding.",
      known_gap=KnownGap("false-alarm.generated-id-digits", ID_REVIEW))
def generated_id_digits(path: Path) -> None:
    generated = b"(0a1b2c123456789def0a1b2c3d4e5f60)"
    path.write_bytes(_with_trailer(b"/ID [" + generated + b" " + generated + b"]"))
