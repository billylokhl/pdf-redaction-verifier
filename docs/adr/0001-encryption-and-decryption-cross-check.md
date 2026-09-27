# 0001. Encryption and the decryption cross-check

Status: accepted (owner approval, 2026-09-27)

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

**Scope.** Support the PDF standard security handler only: RC4 (40- and
128-bit, R2-R4) and AES (128-bit R4, 256-bit R5/R6), selected per
`/CF`/`/StmF`/`/StrF`/`/EFF` the way the spec defines, including the
`/Identity` crypt filter (a stream explicitly *not* encrypted despite a
document-level `/Encrypt`, which is more often abused, not less, in
attachments). Non-standard security handlers (`/Filter` other than
`/Standard`) are `UNREADABLE` at the document level -- exit `2`, not a
best-effort skip.

**Key derivation.** Always attempt the empty user password first. For
R2-R4 the key comes from Algorithm 2 in the spec and is authenticated
against `/U`. For R5/R6 the password is authenticated against `/U`'s
validation salt (the hash of the password with that salt must equal
`/U`'s first 32 bytes); `/UE` only unwraps the file key once that check
passes, and the decrypted `/Perms` must then also check out. pikepdf
(qpdf) does not fail on a `/Perms` mismatch -- it opens the file and
emits a warning -- so a qpdf `/Perms`-mismatch warning means `FLAGGED`
(exit `2`). If authentication fails, the file needs a real user password we don't
have: exit `2` -- unchanged from today's behavior (see Correction
above). No password is ever logged, retried against a wordlist, or
otherwise brute-forced.

**Implementation surface: pikepdf, cross-checked against MuPDF (decided,
owner approval 2026-09-27).** Each revision is decrypted via pikepdf on
the same prefix cut `verify.scan_earlier_revisions` already produces for
that revision (`verify.py` around lines 2929-2931: the raw bytes cut
just past an earlier xref section, with a `startxref` appended); pikepdf
is pointed at that same prefix. Its decrypted stream bytes, before
filters, are cross-checked against MuPDF's `xref_stream_raw` for the
same prefix. No hand-rolled key derivation or crypto-primitives library
is added.

`pikepdf` is not a runtime dependency today and is **not added to
`pyproject.toml` now** -- it is pinned when Phase 3a actually adds it as
a dependency, the same way `pymupdf` is pinned. Phase 3a must also:

- add a fixture whose earlier revision carries a **different** `/Encrypt`
  from the current one -- nothing has exercised that case yet;
- confirm that pikepdf's decrypted-but-not-yet-filtered stream read
  actually exposes the stage this cross-check compares against
  (`xref_stream_raw`) -- this was not checked in this pass and is
  currently **unconfirmed**.

**The one thing option 1 (not chosen: hand-rolled key derivation) would
have added is searching dead bodies in an encrypted file. Those stay
`FLAGGED` under pikepdf too (no second decryption exists for them), so
the difference was exit `1` versus `2` when a secret sits in one --
never a false `0` either way.** See "Options considered" below for the
full comparison that led to this choice.

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
`DECODED` on one decryption's word alone**, per the fail-closed default
this whole review is built around. Concretely, that means: a dead body
outside every xref, a superseded revision whose prefix cut MuPDF opens
only by repair (today's tool already reports that as unread), and any
object MuPDF itself cannot read for an unrelated reason, are `FLAGGED`
unless some other independent check covers that specific key path.

**Unindexed byte ranges** in an encrypted file have no object number,
so there is nothing to decrypt against a confirmed key and nothing to
cross-check against. They are always flagged -- this is a consequence
of the byte-tiling design generally (any unindexed range is flagged
regardless of encryption; there is no dedicated ADR "covering" this, a
citation the first version of this ADR got wrong).

## Options considered

**Correction to the previous version of this ADR.** It claimed that
neither pikepdf nor PyMuPDF can open a superseded revision, so
hand-rolled key derivation was "the only option that actually covers
superseded revisions." That is false. `verify.scan_earlier_revisions`
already reaches them for encrypted files too: it cuts the raw bytes just
past an earlier xref section, appends a `startxref`, and reopens the
prefix (`verify.py` around lines 2929-2931). The cut copy carries the
earlier revision's own trailer and `/Encrypt`, and PyMuPDF opens it with
the empty user password. Checked directly: an owner-password-only file
(AES-256 R6, and separately RC4-128 R3) whose content stream showing a
value was overwritten by an incremental update exits `1` under today's
`verify.py`, with an `Objects` finding in `superseded` storage. pikepdf
could be pointed at the same prefix. So which implementation reads a
superseded revision was an open choice, not a forced one.

The options considered, and the differences between them:

1. **Hand-rolled key derivation and decryption** (RC4 is a few lines;
   AES via a crypto-primitives library such as `cryptography`, never a
   hand-rolled block cipher), applied per revision to that revision's
   own `/Encrypt`, with MuPDF's decryption of the same prefix as the
   cross-check.
2. **Library decryption per revision (chosen)**: pikepdf (qpdf) reads
   each revision's prefix cut, as `verify.py` already does with PyMuPDF,
   and its decrypted stream bytes, before filters, are cross-checked
   against MuPDF's `xref_stream_raw` for the same prefix.
3. Decrypt only the current revision and flag every superseded revision
   of an encrypted file outright. Rejected: today's tool already
   reads those revisions, so this would turn every incrementally updated
   encrypted file into a review case for no gain.

| | Option 1 (hand-rolled) | Option 2 (pikepdf, chosen) |
| --- | --- | --- |
| Superseded revisions | Reached by the prefix cut, key derived from that revision's `/Encrypt` | Reached by the same prefix cut |
| Dead bodies (`N G obj` bytes no xref indexes) | Can be decrypted and searched, but no second decryption exists to cross-check, so still `FLAGGED` | Cannot be decrypted; `FLAGGED` unsearched. A secret in one exits `2` rather than `1` -- never `0` either way |
| An earlier revision whose `/Encrypt` differs from the current one | Untested | Untested |
| Cross-check independence | Our code vs MuPDF | qpdf vs MuPDF: two independent implementations, neither ours |
| Exit-`0` audit surface | Our key derivation and RC4, plus the crypto library | pikepdf/qpdf's decryption |
| New dependency to pin | `cryptography` (or another crypto-primitives library) | `pikepdf` (not a runtime dependency today) |

**Why option 2 was chosen.** It reaches superseded revisions the way
today's tool already does, keeps hand-written cryptography off the
exit-`0` audit surface, and its cross-check compares two implementations
that share no code. The one thing option 1 would have added is
searching dead bodies in an encrypted file, and those stay `FLAGGED`
under either option (no second decryption exists for them), so the
difference is exit `1` versus `2` when a secret sits in one -- never a
false `0`.

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
- The cross-check makes MuPDF's own decryption part of the audit surface
  for encrypted files, and makes pikepdf/qpdf part of it too -- this is
  a wider exit-`0` audit surface than an unencrypted file has, which is
  inherent to the problem, not a design flaw.
- No verdict currently exits `0` for an encrypted file that this ADR
  would move to `2`, or vice versa -- see Correction above. The
  verdict-change risk here is in the *other* direction: getting the
  cross-check wrong could newly `FLAG` files that decrypt correctly,
  which is why Phase 3a's fixtures must include a positive
  (correct-key, should-cross-check-clean) case, not only a negative one.

## Owner decision (2026-09-27)

Owner decision: "approve all recommendations."

- **Implementation: pikepdf (option 2)**, decrypting each revision's
  prefix cut and cross-checking its decrypted-but-unfiltered stream
  bytes against MuPDF's `xref_stream_raw` for the same prefix. Option 1
  (hand-rolled key derivation) and option 3 (current revision only) are
  rejected.
- **Library to pin: `pikepdf`**, when Phase 3a adds it as a dependency
  -- not added to `pyproject.toml` now.
- **Phase 3a adds a fixture** whose earlier revision carries a
  **different** `/Encrypt` from the current one.
- **Confirm in Phase 3a** that pikepdf's decrypted-but-unfiltered read
  actually exposes the stage this cross-check compares against; this is
  currently unconfirmed.
