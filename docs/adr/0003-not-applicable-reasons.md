# 0003. The `NOT_APPLICABLE` closed list

Status: proposed (depends on docs/adr/0004, also proposed)

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
human viewer, but trivial with respect to a byte scan).

**Correction: an earlier version of this ADR claimed "today's `verify.py`
exits `2` for this file," which is only true for one of four cases.**
Verified directly against `verify.py` (not a claim taken on faith):

| Drawn on the page? | Rule kind | Today's exit |
| --- | --- | --- |
| Yes | value (`"123-45-6789"`) | `2` (flagged: stored image, not OCR'd) |
| Yes | class (`ssn`) | **`0`** -- pattern classes never run over image/binary bytes today |
| No (unreferenced) | value | `0` -- the documented ✗ gap: an unreferenced image is not read at all |
| No (unreferenced) | class | `0` |

So three of the four already exit `0` today, not one. Under ADR 0003's
reason 4 ("image data fully consumed by the image decoder") composed
carelessly with ADR 0004's envelope, the drawn+value case (today's one
correctly-flagged case) would ALSO newly exit `0`: the image decoder
would OCR the (visually blank/noisy) pixels, find no text, correctly
report the image as fully consumed, and -- if `NOT_APPLICABLE` were read
as "this byte range is settled, move on" -- the file would silently
regress from `2` to `0`. The rule above closes that regression: the same
raw sample bytes must *also* always be searched by the matcher (as a raw
byte string, independent of what OCR found), and only then, if nothing
matches, does `NOT_APPLICABLE` apply. This is not a new decoder or a new
reason -- it is a restatement of Principle 1's "closed enum" discipline
applied to what `NOT_APPLICABLE` is allowed to mean.

**Whether pattern classes should run over raw image sample bytes at all
is a separate, real gap this case exposes, not fixed by the rule above.**
The rule above covers *value* rules (a literal match against raw bytes);
it does not by itself make *pattern classes* (`ssn`, `credit-card`, etc.)
run over image samples, and today they do not run over any binary data
at all (by design -- see `verify.py`'s `_OPAQUE_STREAM_SUBTYPES`
exclusion, which exists because font-program bytes coincidentally match
pattern classes constantly). Recommend: the new inventory's raw-byte
matcher pass (the rule above) should run **value** rules unconditionally
over decoded image samples, but should run **pattern classes** over them
only behind the same manual-review tier `_scan_orphaned_payload` already
uses for other raw-text contexts today (a coincidental digit run in
noisy sample data is exactly the kind of fusion that tier exists for) --
this needs its own case-library coverage in Phase 0b/3a, not
implementation here.

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
   docs/adr/0004's envelope accepts the image **and its recall bound is
   satisfied** (docs/adr/0004, proposed: geometry and a completed OCR
   pass are *not* enough on their own -- REDESIGN §4 requires a
   recall-validated envelope, which does not exist until Phase 4b
   measures it), **and** the raw sample bytes have separately been
   searched per the governing rule above. Until Phase 4b's recall
   measurement exists, this reason cannot actually be reached -- image
   evidence stays `FLAGGED`, consistent with ADR 0004.

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

None for the list itself. Two items depend on other proposed ADRs and
should be confirmed together: reason 4 depends on docs/adr/0004's recall
bound (so 0003 is marked proposed, not accepted, purely because of that
dependency); and whether pattern classes should run over decoded image
samples at manual-review tier (recommended above) is a real, currently-
unimplemented gap the owner should confirm before Phase 4b. The
steganographic-image case itself should be confirmed as a Phase 0b/3a
case-library addition (a scheduling question, not a design trade-off).
