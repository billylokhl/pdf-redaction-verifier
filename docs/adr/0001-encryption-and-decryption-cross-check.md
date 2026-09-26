# 0001. Encryption and the decryption cross-check

Status: accepted, one item needs owner confirmation

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

`verify.py` today has no encryption handling at all -- PyMuPDF silently
decrypts on open when no user password is set (the common case for
"owner-password-only" files, which is what a redaction pipeline
produces: readable content, restricted permissions) and the tool never
notices. This ADR is for the *new* inventory, which reads objects itself
rather than trusting PyMuPDF's decryption.

## Decision

**Scope.** Support the PDF standard security handler only: RC4 (40- and
128-bit, R2-R4) and AES (128-bit R4, 256-bit R5/R6), selected per
`/CF`/`/StmF`/`/StrF`/`/EFF` the way the spec defines, including the
`/Identity` crypt filter (a stream explicitly *not* encrypted despite a
document-level `/Encrypt`, which is more often abused, not less, in
attachments). Non-standard security handlers (`/Filter` other than
`/Standard`) are `UNREADABLE` at the document level -- exit `2`, not a
best-effort skip.

**Key derivation.** Always attempt the empty user password first (per
Algorithm 2 in the spec), matching what a redaction pipeline actually
produces and what PyMuPDF/qpdf already do by default. If that fails to
authenticate against `/U` (or `/UE` for R5/R6), the file needs a real
user password we don't have: exit `2`, per REDESIGN §2's own scope
statement. No password is ever logged, retried against a wordlist, or
otherwise brute-forced -- that is out of scope by construction, not an
oversight.

**Implementation surface.** Decryption is implemented via `pikepdf`
(which wraps libqpdf's crypto) rather than a hand-rolled RC4/AES/SHA-256
implementation in the inventory module. The plan's own principles argue
for this: minimising our own trust surface (§4 lists exactly what must
be hand-audited for the exit-`0` audit surface) is better served by
reusing a widely-used, tested crypto implementation than adding one more
hand-rolled algorithm to that list. **This adds a new pinned dependency
(`pikepdf`) and is the one item in this ADR that needs the owner's
sign-off** -- the alternative (hand-rolled crypto reviewed as part of
the audit surface) is not rejected, only not recommended.

**The cross-check.** For every object MuPDF's own object table also
reaches (i.e. everything except unindexed ranges and objects a
trustworthy reachability walk calls orphaned -- MuPDF has no notion of
either), compare our decrypted bytes against `doc.xref_object`/
`doc.xref_stream` for that object **byte-for-byte**, not by a text
sniff. A wrong key still produces a string that tokenises (PDF string
syntax survives garbage bytes); only a byte comparison against a second,
independently-implemented decryption catches a key-derivation bug. Any
mismatch is a flag, unconditionally -- never `DECODED`, never silently
preferring one answer over the other.

**What cannot be cross-checked.** An unindexed byte range in an
encrypted file (docs/adr/0003, docs/adr/0007) has no object number, so
there is nothing for MuPDF to reach and nothing to compare against. It
is always flagged, per REDESIGN §4's inventory section, precisely
because there is no second source to confirm our decryption of it was
even attempted with the right key.

## Measurement

None of the 2,031 files in the local corpus (`eval/spikes/RESULTS.md`)
are password-protected (`doc.needs_pass` was false for all of them, per
the orphaned-stream sweep's `encrypted_skipped: 0`). This ADR's design
is therefore **not empirically validated against a real encrypted file**
in this pass -- it follows from the spec and from the earlier
feasibility summary's account of PyMuPDF/qpdf behavior, not from a
corpus measurement. Phase 3a should add hand-built encrypted fixtures
(`caselib/rawpdf.py` can assemble `/Encrypt` dictionaries directly) that
exercise: empty-password RC4-128, AES-128, AES-256 R6, a `/CF`
dictionary using `/Identity` for one stream, and a deliberately wrong
key to confirm the cross-check actually fires.

## Consequences

- Objects in an encrypted file get the same fail-closed treatment as
  unencrypted ones once decrypted; encryption adds one more thing that
  must succeed (authentication) before any other obligation can be
  discharged.
- The cross-check makes MuPDF's own decryption part of the audit
  surface for encrypted files specifically (already implied by
  Principle 2's "cross-checked against MuPDF for the objects both can
  see" -- this ADR just spells out what "both can see" excludes).
- A non-standard security handler or a real user password moves a file
  that exits `0` today (PyMuPDF silently decrypts it) to exit `2` under
  the new path -- this is a `CHANGELOG.md`-worthy strictness increase
  under Principle 11, expected and intentional (§2: "A file that needs a
  user password the tool does not have exits `2`").

## Owner confirmation needed

- Adding `pikepdf` as a pinned runtime dependency for decryption,
  instead of a hand-rolled implementation reviewed as part of the
  exit-0 audit surface.
