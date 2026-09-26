# 0003. The `NOT_APPLICABLE` closed list

Status: accepted

## Context

REDESIGN.md Principle 1 requires `NOT_APPLICABLE` to "require a reason
from a closed list," and §4's decoder registry already proposes the
list: "xref-stream field data, object-stream header table, a font
program whose parsed tables span the stream and whose strings were
searched, image data fully consumed by the image decoder. Extended only
by ADR." Phase 1's job was to stress-test that list against the real
corpus and this pass's other findings before treating it as settled --
not to design it from scratch.

The stakes of getting this wrong run in one direction only: every
`NOT_APPLICABLE` reason is a place a unit's obligation is discharged
*without* a decoder having read its bytes for secrets. An overly generous
list silently reopens exactly the "absence is a negative claim, but the
tool is enumerative" problem §1 exists to close.

## Decision

**Ship the four reasons exactly as proposed, and no others:**

1. **Xref-stream field data** -- the fixed-width binary columns inside a
   cross-reference stream's body (object type, offset, generation).
   Confirmed applicable only when the stream's own `/W` array and length
   agree exactly with its declared object range (the same confirmation
   `_xref_section`-equivalent logic already performs to accept the
   section at all).
2. **Object-stream header table** -- the `N G` offset pairs at the start
   of an `/ObjStm`'s decompressed body, before `/First`. Never the
   objects themselves, which are always separately decoded.
3. **A font program whose parsed tables span the stream and whose
   strings were searched** -- sfnt/CFF/Type1 table parsing must account
   for every byte (padding aside; a gap becomes a `RESIDUE` child per the
   decoder registry, not silently accepted), and the name/metadata
   strings inside those tables must still go through the matcher. A font
   program that merely opens without erroring is not enough.
4. **Image data fully consumed by the image decoder** -- only once
   docs/adr/0004's envelope accepts the image and normalisation/OCR
   actually ran to completion; an image outside the envelope is
   `FLAGGED`, never this reason.

**No additional reason is added by this ADR.** Three candidates were
considered and rejected for now:

- *Encrypted stream padding/PKCS#7 padding bytes* -- tempting, but
  padding validity is exactly the kind of signal the decryption
  cross-check (docs/adr/0001) already uses; carving it out as
  `NOT_APPLICABLE` would remove a check rather than add a reason.
- *A `/Linearized` first-page hint-stream dictionary's own bytes* --
  no case in the corpus needed this; xref-stream field data already
  covers the hint stream's cross-reference role, and the hint table's
  own binary payload is exactly the kind of thing that should stay
  `UNREADABLE`/flagged until a decoder for it exists, not quietly
  excused.
- *A `/Metadata` stream's XML prolog/whitespace* -- already fully
  searched as text (XMP/Info decoder row); there is no sub-byte-range
  here that needs excusing.

**The list stays closed.** Any future addition needs its own ADR with
the same bar: a structural parse that accounts for every byte, not a
type declaration taken on the object's own word (Principle 3).

## Measurement

This is a design confirmation, not a rate; the corpus measurements that
bear on it are the unindexed-byte rate (docs/adr/0007's sibling
measurement, `eval/spikes/RESULTS.md` §d) and the orphaned-stream rate.
Neither surfaced a real file whose leftover bytes would need a fifth
`NOT_APPLICABLE` reason to explain -- the unindexed bytes found (ExifTool
update markers, a repeated linearization header comment, one file with
an unreachable xref table) are all either genuinely flag-worthy or
already covered by "header"/"xref chain," never a case of "this is
structurally inert and we should stop looking here."

## Consequences

- Every decoder that wants to mark a unit `NOT_APPLICABLE` for a reason
  not on this list must instead return `FLAGGED` (or a narrower status)
  and open a new ADR -- this is deliberately friction, not an oversight.
- Because the list is small and each reason requires a completed
  structural parse, `NOT_APPLICABLE` should be rare in practice compared
  to `DECODED` -- if Phase 4's implementation finds itself reaching for
  `NOT_APPLICABLE` often, that is a signal the decoder is guessing a
  kind rather than confirming it (Principle 3), not a signal the list is
  too short.

## Owner confirmation needed

None.
