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

**Ship the four reasons exactly as proposed, and no others** (unchanged
from the first version of this ADR -- see the list and the three
rejected candidates below), **plus one governing rule this pass's review
found missing and that must be stated explicitly, because ADR 0004
(image-OCR envelope) would otherwise contradict it:**

> **`NOT_APPLICABLE` (and `DECODED`) never exempts a unit's decoded bytes
> from the raw matcher. A `NOT_APPLICABLE` reason excuses a decoder from
> needing to *further parse or interpret* bytes a structural parse has
> already fully accounted for -- it never excuses those bytes, or any
> text a decoder recovers from them, from being searched for secrets.**

The concrete case that makes this necessary: consider a 10×10 DeviceGray
image whose 100 raw sample bytes are literally the ASCII codes for
"Employee SSN 123-45-6789" (a value hidden in the sample data, not
rendered as visible glyphs -- this is steganographic with respect to a
human viewer, but trivial with respect to a byte scan). Today's
`verify.py` exits `2` for this file (a stored image it does not OCR is
flagged, never silently passed). Under ADR 0003's reason 4
("image data fully consumed by the image decoder") composed carelessly
with ADR 0004's envelope, the image decoder would OCR the (visually
blank/noisy) pixels, find no text, correctly report the image as fully
consumed by the decoder, and -- if `NOT_APPLICABLE` were read as "this
byte range is settled, move on" -- the file would wrongly exit `0`. The
rule above closes this: the same raw sample bytes must *also* always be
searched by the matcher (as a raw byte string, independent of what OCR
found), and only then, if nothing matches, does `NOT_APPLICABLE` apply.
This is not a new decoder or a new reason -- it is a restatement of
Principle 1's "closed enum" discipline applied to what `NOT_APPLICABLE`
is allowed to mean.

**The original four reasons, unchanged:**

1. **Xref-stream field data** -- the fixed-width binary columns inside a
   cross-reference stream's body (object type, offset, generation).
   Confirmed applicable only when the stream's own `/W` array and length
   agree exactly with its declared object range.
2. **Object-stream header table** -- the `N G` offset pairs at the start
   of an `/ObjStm`'s decompressed body, before `/First`. Never the
   objects themselves, which are always separately decoded.
3. **A font program whose parsed tables span the stream and whose
   strings were searched** -- sfnt/CFF/Type1 table parsing must account
   for every byte (padding aside; a gap becomes a `RESIDUE` child), and
   the name/metadata strings inside those tables must still go through
   the matcher.
4. **Image data fully consumed by the image decoder** -- only once
   docs/adr/0004's envelope accepts the image and OCR actually ran to
   completion, **and** the raw sample bytes have separately been
   searched per the governing rule above.

Three other candidates considered and rejected: encrypted stream padding
(overlaps the decryption cross-check, ADR 0001, rather than needing its
own reason); a linearized file's hint-stream payload (no case in the
corpus needed this; stays `UNREADABLE`/flagged until a decoder exists);
a `/Metadata` stream's XML prolog/whitespace (already fully searched as
text, nothing to excuse).

**The list stays closed.** Any future addition needs its own ADR with
the same bar: a structural parse that accounts for every byte, and
confirmation that the governing rule above still holds for it.

## Measurement

This is a design confirmation, not a rate. The steganographic-image case
above is a **planned case for the case library, not implemented in this
pass** -- it belongs in Phase 0b/3a as a new cell (a value hidden in raw
image sample bytes, undetectable by OCR, only found by a raw byte scan
of the decoded samples) and should be pinned there before Phase 4b ships
the image decoder, to make the governing rule a tested invariant rather
than only a documented one.

**Deferred, not implemented here:** xref-stream free-entry bytes (reason
1) still need to go through the raw matcher under the same governing
rule (a free-entry slot's bytes are not secret-bearing in any real xref
stream this pass saw, but the rule should not special-case them) --
tracked for Phase 3a's inventory implementation, not resolved by this
ADR.

## Consequences

- Every decoder that wants to mark a unit `NOT_APPLICABLE` for a reason
  not on this list must instead return `FLAGGED` and open a new ADR.
- The governing rule means `NOT_APPLICABLE`'s implementation is not just
  "return a status" -- it is "return a status *and* have already run the
  matcher over the bytes it excuses from further parsing." This is a
  concrete implementation requirement for every decoder on the registry,
  not only the image decoder; Phase 4's decoder registry entries should
  each note where the raw-byte matcher pass happens.

## Owner confirmation needed

None for the list itself. The steganographic-image case should be
confirmed as a Phase 0b/3a case-library addition (a scheduling question,
not a design trade-off).
