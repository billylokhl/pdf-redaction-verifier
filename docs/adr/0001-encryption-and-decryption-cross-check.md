# 0001. Encryption and the decryption cross-check

Status: proposed

## Context

REDESIGN.md §2 puts "encrypted-with-owner-password" files in scope and
says "a file that needs a user password the tool does not have exits
`2`." §4's inventory section adds the part left undecided: "per-object
decryption using each revision's `/Encrypt`... Our decrypted strings and
streams are compared with MuPDF's for every object MuPDF can reach; any
mismatch is a flag (a wrong key yields garbage that still tokenises).
Details in an ADR." Three things were actually open: which encryption
constructs to support, how to derive the key without a user password,
and exactly what the cross-check compares and how it fails.

**Correction to the first version of this ADR.** It claimed a
user-password file moves from exit `0` to exit `2` under the new
design, as though this were new strictness. That is wrong: `verify.py`
already exits `2` for a password-protected file today
(`doc.needs_pass` → `PDF_PASSWORD`, `verify.py` around line 3850). This
ADR does not change that outcome; it only adds a decryption and
cross-check path for files that open with the empty user password
(the "owner-password-only" case a redaction pipeline actually produces),
which today's `verify.py` also already handles correctly via PyMuPDF's
own silent decryption -- what's missing today is only the cross-check
against a *second* decryption, since the new inventory reads objects
itself instead of trusting PyMuPDF's.

## Decision

This ADR proposes an approach and flags the parts that are the owner's
call, rather than settling all of them.

**Scope.** Support the PDF standard security handler only: RC4 (40- and
128-bit, R2-R4) and AES (128-bit R4, 256-bit R5/R6), selected per
`/CF`/`/StmF`/`/StrF`/`/EFF` the way the spec defines, including the
`/Identity` crypt filter (a stream explicitly *not* encrypted despite a
document-level `/Encrypt`, which is more often abused, not less, in
attachments). Non-standard security handlers (`/Filter` other than
`/Standard`) are `UNREADABLE` at the document level -- exit `2`, not a
best-effort skip.

**Key derivation.** Always attempt the empty user password first (per
Algorithm 2 in the spec). If that fails to authenticate against `/U`
(or `/UE` for R5/R6), the file needs a real user password we don't
have: exit `2` -- unchanged from today's behavior (see Correction
above). No password is ever logged, retried against a wordlist, or
otherwise brute-forced.

**Implementation surface -- proposed, needs the owner's decision.** The
first version of this ADR proposed `pikepdf` for decryption. That does
not actually work for this design: **pikepdf (like PyMuPDF) only ever
exposes the current revision's view of the file** -- there is no API to
open "the file as of an earlier incremental update" the way
`verify.scan_earlier_revisions` does today for the *unencrypted* case
(truncating the raw bytes just past an earlier xref section and
reopening). A superseded revision can carry its **own** `/Encrypt`
dictionary (a different key, possibly a different algorithm, than the
current revision's), and neither pikepdf nor PyMuPDF can be pointed at
it. The realistic options are:

1. **Hand-rolled key derivation and decryption** (RC4 is a few lines;
   AES via a crypto-primitives library such as `cryptography`, never a
   hand-rolled block cipher) that can be pointed at any revision's own
   `/Encrypt` dictionary and any object's raw bytes, with pikepdf/MuPDF
   used only as the cross-check on the current revision (recommended --
   it is the only option that actually covers superseded revisions).
2. Decrypt only the current revision (via pikepdf or hand-rolled) and
   treat every superseded revision of an encrypted file as `FLAGGED`
   outright, never attempting to read it. Simpler, but gives up on the
   exact place a pre-redaction original is most likely to still be
   sitting (§4's inventory section on superseded objects).

**The cross-check, corrected.** For every object with an xref entry --
**this includes an orphaned-but-indexed object**: `doc.xref_object`/
`doc.xref_stream` read any object by number regardless of reachability,
as `eval/spikes/inventory_lite.py`'s own `orphaned_content_streams`
already demonstrates by calling `doc.xref_stream` on exactly such
objects. The first version of this ADR was wrong to restrict the
cross-check to "reachable" objects only.

Byte-for-byte comparison against `xref_object`/`xref_stream`, as first
proposed, **cannot work**: `xref_object` returns MuPDF's own
re-serialization of the dictionary (key order and whitespace are not
preserved from the source bytes), and `xref_stream` returns fully
**filter-decoded** bytes, not merely decrypted ones -- comparing our
raw decrypted-but-still-compressed stream against it would never match
even with the correct key. The comparison must instead be:

- **Dictionaries**: parse both our decrypted dictionary and MuPDF's
  `xref_object` output and compare at the **value level** (same keys,
  semantically equal values), not as byte strings.
- **Streams**: compare our decrypted-but-not-yet-filtered bytes against
  `doc.xref_stream_raw` (decrypted, filters *not* applied -- confirmed
  present in the pinned PyMuPDF version), which is the comparable stage;
  save the filter-decode comparison, if wanted, for a second pass using
  `xref_stream`.

**Any key path this cross-check cannot reach is `FLAGGED`, not
`DECODED` on our own decryption's word alone**, per the fail-closed
default this whole review is built around. Concretely, that means:
a superseded revision (including one with its own `/Encrypt`, under
option 1 above), and any object MuPDF itself cannot read for an
unrelated reason, are `FLAGGED` unless some other independent check
covers that specific key path.

**Unindexed byte ranges** in an encrypted file have no object number,
so there is nothing to decrypt against a confirmed key and nothing to
cross-check against. They are always flagged -- this is a consequence
of the byte-tiling design generally (any unindexed range is flagged
regardless of encryption; there is no dedicated ADR "covering" this, a
citation the first version of this ADR got wrong).

## Measurement

None of the 2,031 files in the local corpus (`eval/spikes/RESULTS.md`)
are password-protected (`doc.needs_pass` was false for all of them).
This ADR's design is **not empirically validated against a real
encrypted file** in this pass. Phase 3a should add hand-built encrypted
fixtures (`caselib/rawpdf.py` can assemble `/Encrypt` dictionaries
directly) that exercise: empty-password RC4-128, AES-128, AES-256 R6, a
`/CF` dictionary using `/Identity` for one stream, an incrementally
updated file whose *earlier* revision carries its own `/Encrypt`, and a
deliberately wrong key to confirm the cross-check actually fires.

## Consequences

- Objects in an encrypted file get the same fail-closed treatment as
  unencrypted ones once decrypted; encryption adds one more thing that
  must succeed (authentication) before any other obligation can be
  discharged.
- The cross-check makes MuPDF's own decryption (current revision) part
  of the audit surface for encrypted files, and makes a
  crypto-primitives library (if option 1 is chosen) part of it too --
  this is a wider exit-`0` audit surface than an unencrypted file has,
  which is inherent to the problem, not a design flaw.
- No verdict currently exits `0` for an encrypted file that this ADR
  would move to `2`, or vice versa -- see Correction above. The
  verdict-change risk here is in the *other* direction: getting the
  cross-check wrong could newly `FLAG` files that decrypt correctly,
  which is why Phase 3a's fixtures must include a positive
  (correct-key, should-cross-check-clean) case, not only a negative one.

## Owner confirmation needed

- Hand-rolled key derivation on a crypto-primitives library (option 1,
  recommended) vs. pikepdf/current-revision-only with superseded
  revisions always flagged (option 2).
- If option 1: which crypto-primitives library to pin (e.g.
  `cryptography` for AES/SHA; RC4 is short enough to implement directly
  and review as part of the exit-0 audit surface).
