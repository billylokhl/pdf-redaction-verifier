"""The inventory with each revision's cross-checked decryption (3a-9b-1a):
the structural pass crypt.py locates bytes by, crypt.inventory's two-pass
pipeline, decryption flags passed through, and the reuse tokens that keep
one revision's reading from standing for another's under a different
decryption (coordinator decision 2: the identity joins every reuse key).

Mutation-checked (#9b-1a): removing the token from pages' whole-result
reuse, pages' attribute intern or trees' ident fails a test here. The
tokens in a Page record's own key, in annotations' and in labels' idents
are defensive and cannot fail one: a Page's key already holds its
interned /Resources and /MediaBox (every listed page has both), an
annotation reading is reused only over the same Page records, and labels
read references and names, never string contents, and intern their sets
by value (a walk under a new token reads the same)."""

from __future__ import annotations

import pytest

from caselib import rawpdf
from caselib.rawcrypt import Handler
from redaction_verifier import crypt
from redaction_verifier.budget import Budget
from redaction_verifier.inventory import build_inventory
from redaction_verifier.inventory.decrypted import Decryption, reuse_token, reuse_tokens
from redaction_verifier.ledger import Flag, FlagReason

CONTENT = b"BT /F1 12 Tf 72 700 Td (A line) Tj ET"
FILE_ID = bytes(range(16))


def _plain_two_revisions() -> bytes:
    """A page with a name tree and an annotation, then an update that
    rewrites only /Info: every stage reuses revision 1's reading."""
    base = rawpdf.build({
        1: b"<< /Type /Catalog /Pages 2 0 R /Names << /Dests 9 0 R >> >>",
        2: rawpdf.PAGES,
        3: rawpdf.page(extra=b"/Annots [8 0 R]"),
        4: rawpdf.stream(b"", CONTENT), 5: b"<< /Title (one) >>", 6: rawpdf.HELVETICA,
        8: b"<< /Type /Annot /Subtype /Text /Rect [0 0 9 9] >>",
        9: b"<< /Names [(a) 3 0 R] >>"}, info=5)
    return rawpdf.incremental_update(base, {5: b"<< /Title (two) >>"}, info=5)


def _identity(tag: bytes) -> tuple[bytes, bytes]:
    return (tag * 32, FILE_ID)


def _built(data: bytes, decryption: list[Decryption | None] | None = None):  # noqa: ANN202
    return build_inventory(data, budget=Budget(file_size=len(data)), decryption=decryption)


# ── Reuse tokens ───────────────────────────────────────────────────────
def test_tokens() -> None:
    plain = Decryption(0, None, {}, {}, ())
    clean = Decryption(0, _identity(b"a"), {}, {}, ())
    flagged = Decryption(1, _identity(b"a"), {}, {}, (Flag(FlagReason.DECRYPTION_UNVERIFIED),))
    assert reuse_token(None, 0) is None and reuse_token(plain, 0) is None
    assert reuse_token(clean, 0) == _identity(b"a")
    assert reuse_token(flagged, 1) == ("revision", 1) != reuse_token(flagged, 2)
    assert reuse_tokens(None, 2) == (None, None)
    assert reuse_tokens([clean, None], 2) == (_identity(b"a"), None)
    with pytest.raises(ValueError):                   # a caller's bug, never guessed at
        reuse_tokens([clean], 2)
    with pytest.raises(ValueError):
        reuse_tokens([], 2)
    # Flagged without an identity (a budget that ran out before the open):
    # still a token of its own, never an unencrypted revision's None.
    spent = Decryption(3, None, {}, {}, (Flag(FlagReason.BUDGET_EXHAUSTED),))
    assert reuse_token(spent, 3) == ("revision", 3)


def test_without_decryption_every_stage_reuses_the_earlier_reading() -> None:
    inv = _built(_plain_two_revisions())
    assert inv.revisions == 2
    assert inv.pages[0] is inv.pages[1] and inv.pages[0]
    assert inv.trees[0] is inv.trees[1] and inv.trees[0] is not None
    assert inv.annotations[0] is inv.annotations[1] and inv.annotations[0]
    assert inv.labels[0] is not None and inv.labels[1] is not None
    assert inv.labels[0].drawable is inv.labels[1].drawable


@pytest.mark.parametrize("second", [_identity(b"b"), None], ids=["other-identity", "unencrypted"])
def test_a_different_token_stops_every_reuse(second: tuple[bytes, bytes] | None) -> None:
    # One revision decrypted under one identity, the other under another
    # (or none): no stage's reading of one stands for the other's.
    data = _plain_two_revisions()
    inv = _built(data, [Decryption(0, _identity(b"a"), {}, {}, ()),
                        Decryption(1, second, {}, {}, ())])
    assert inv.pages[0] is not inv.pages[1]
    assert inv.pages[0] is not None and inv.pages[1] is not None
    assert inv.pages[0][0] is not inv.pages[1][0]               # the Page records too
    assert inv.trees[0] is not inv.trees[1]
    assert inv.annotations[0] is not inv.annotations[1]
    # The stage as a whole walks afresh (more work charged). This does not
    # isolate labels, whose tokens are defensive (see the module docstring).
    same = _built(data, [Decryption(r, _identity(b"a"), {}, {}, ()) for r in (0, 1)])
    assert inv.graph_work > same.graph_work


def test_one_identity_reuses_as_unencrypted_does() -> None:
    data = _plain_two_revisions()
    same = [Decryption(r, _identity(b"a"), {}, {}, ()) for r in (0, 1)]
    inv = _built(data, same)
    assert inv.pages[0] is inv.pages[1] and inv.trees[0] is inv.trees[1]


def test_a_flagged_revision_is_never_reused() -> None:
    data = _plain_two_revisions()
    flag = Flag(FlagReason.DECRYPTION_UNVERIFIED, None, (("rule", 7),))
    inv = _built(data, [Decryption(0, _identity(b"a"), {}, {}, ()),
                        Decryption(1, _identity(b"a"), {}, {}, (flag,))])
    assert inv.pages[0] is not inv.pages[1] and inv.trees[0] is not inv.trees[1]


# ── Flags and the structural pass ──────────────────────────────────────
def test_decryption_flags_are_the_inventorys_right_after_the_chains() -> None:
    data = _plain_two_revisions()
    flag = Flag(FlagReason.DECRYPTION_MISMATCH, None, (("object", 4), ("rule", 1)))
    inv = _built(data, [Decryption(0, _identity(b"a"), {}, {}, (flag,)), None])
    assert flag in inv.flags
    assert inv.decryption[0] is not None and inv.decryption[1] is None


def test_the_structural_pass_has_the_units_and_no_graph() -> None:
    data = _plain_two_revisions()
    full = _built(data)
    first = build_inventory(data, budget=Budget(file_size=len(data)), structure_only=True)
    assert first.units == full.units and first.stream_data == full.stream_data
    assert first.object_streams == full.object_streams and first.entries == full.entries
    assert first.pages == () and first.trees == () and first.labels == ()
    assert first.graph.edges == {}


# ── crypt.inventory ────────────────────────────────────────────────────
def test_an_unencrypted_file_is_built_once() -> None:
    data = _plain_two_revisions()
    inv = crypt.inventory(data)
    assert inv.decryption == () and inv.flags == _built(data).flags
    assert inv.pages[0] is inv.pages[1]


def test_an_encrypted_file_carries_its_decryption() -> None:
    h = Handler(3, b"owner", FILE_ID)
    data = rawpdf.build({1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
                         4: rawpdf.stream(b"", h.stream_data(4, CONTENT)), 6: rawpdf.HELVETICA,
                         7: h.encrypt_dict()}, trailer=h.trailer(7))
    inv = crypt.inventory(data)
    (decryption,) = inv.decryption
    assert decryption is not None and decryption.identity is not None
    assert decryption.streams == {(4, 0): CONTENT}
    assert not any(f.reason.value.startswith("decryption") for f in inv.flags)


def test_an_encrypted_files_flags_reach_the_inventory() -> None:
    h = Handler(4, b"owner", FILE_ID)
    data = rawpdf.build({1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
                         4: rawpdf.stream(b"/Filter /Crypt", CONTENT), 6: rawpdf.HELVETICA,
                         7: h.encrypt_dict()}, trailer=h.trailer(7))
    inv = crypt.inventory(data)
    assert any(f.reason is FlagReason.DECRYPTION_UNVERIFIED for f in inv.flags)


def test_the_attribute_intern_keys_on_the_token() -> None:
    # Equal bytes as written decrypt alike only under one identity: the
    # same /Resources written in both revisions interns apart.
    data = _plain_two_revisions()
    inv = _built(data, [Decryption(0, _identity(b"a"), {}, {}, ()),
                        Decryption(1, _identity(b"b"), {}, {}, ())])
    assert inv.pages[0] is not None and inv.pages[1] is not None
    now, before = inv.pages[0][0].attributes, inv.pages[1][0].attributes
    assert now[b"Resources"].content != before[b"Resources"].content
    # So the Page records differ too: every listed page has /Resources and
    # /MediaBox (or the tree is flagged ATTRIBUTE_MISSING), and its record's
    # key holds their interned bytes. The record's own token is defensive.
    assert inv.pages[0][0] is not inv.pages[1][0]



def test_any_encrypt_key_sends_a_file_through_decryption() -> None:
    # The one-build shortcut is taken only when no trailer holds an /Encrypt
    # key at all: a repeated key whose first value is null still reaches
    # decrypt(), whose TRAILER_SPLIT check decides (#181 round 1).
    h = Handler(3, b"owner", FILE_ID)
    data = rawpdf.build({1: rawpdf.CATALOG, 2: rawpdf.PAGES, 3: rawpdf.page(),
                         4: rawpdf.stream(b"", h.stream_data(4, CONTENT)), 6: rawpdf.HELVETICA,
                         7: h.encrypt_dict()},
                        trailer=b" /Encrypt null" + h.trailer(7))
    inv = crypt.inventory(data)
    assert len(inv.decryption) == 1
    assert any(f.reason is FlagReason.DECRYPTION_UNVERIFIED and dict(f.params)["rule"] == 15
               for f in inv.flags)
