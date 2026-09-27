# 0003. The `NOT_APPLICABLE` closed list

Status: accepted (owner approval, 2026-09-27)

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

**Adopt the four reasons exactly as listed, and no others** (unchanged
from the first version of this ADR -- see the list and the three
rejected candidates below), **plus one governing rule this pass's review
found missing and that must be stated explicitly, because ADR 0004
(image-OCR envelope) would otherwise contradict it:**

> **`NOT_APPLICABLE` (and `DECODED`) never exempts any of a unit's bytes
> from the raw matcher. A `NOT_APPLICABLE` reason excuses a decoder from
> needing to *further parse or interpret* bytes a structural parse has
> already fully accounted for -- it never excuses those bytes, or any
> text a decoder recovers from them, from being searched for secrets.
> "A unit's bytes" means every stage of its filter chain: the raw
> matcher runs over the output of each filter, including the encoded
> bytes an image codec consumes (`DCTDecode`, `JPXDecode`,
> `JBIG2Decode`), not only the decoded samples it produces. Pattern
> classes run over the same stages, at review tier (below).**

The concrete case that makes the rule necessary: consider a 10×10 DeviceGray
image whose 100 raw sample bytes are literally the ASCII codes for
"Employee SSN 123-45-6789" (a value hidden in the sample data, not
rendered as visible glyphs -- this is steganographic with respect to a
human viewer, but trivial with respect to a byte scan).

**Correction: an earlier version of this ADR claimed "today's `verify.py`
exits `2` for this file," which is only true for one of four cases.**
Verified directly against `verify.py` (not a claim taken on faith):

| Drawn on the page? | Rule kind | Today's exit |
| --- | --- | --- |
| Yes | value (`"123-45-6789"`) | `2` -- `REVIEW_BINARY`: qpdf's raw byte sweep matched the value in the image's bytes. (The drawn image *is* OCR'd, as part of the page render; OCR finds nothing, since the value is sample data, not glyphs.) |
| Yes | class (`ssn`) | **`0`** -- pattern classes never run over image/binary bytes today |
| No (unreferenced) | value | `0` -- only because a 10×10 image is under today's 8×32 `_text_sized` floor (`verify.py` `_MIN_TEXT_IMAGE_SIDE`/`_MIN_TEXT_IMAGE_LENGTH`), so it is not flagged as a leftover; the same unreferenced image at 10×40 exits `2` (`LEFTOVER_IMAGE`) |
| No (unreferenced) | class | `0` (same reason; `2` at 10×40) |

The unreferenced rows depend on docs/adr/0004's size excusal: that ADR
keeps today's 8×32 `_text_sized` judgment (accepted), so a small image
like this one stays excused from being flagged -- and only the governing
rule above (all its bytes still go through the raw matcher) keeps the
value case from passing silently. The exact conditions under which a
small image is discharged are reason 4 below.

So three of the four already exit `0` today, not one. Under ADR 0003's
reason 4 ("image data fully consumed by the image decoder") composed
carelessly with ADR 0004's envelope, the drawn+value case (today's one
correctly-flagged case) would ALSO newly exit `0`: the image decoder
would OCR the (visually blank/noisy) pixels, find no text, correctly
report the image as fully consumed, and -- if `NOT_APPLICABLE` were read
as "this byte range is settled, move on" -- the file would silently
regress from `2` to `0` once the legacy path (and with it today's
qpdf raw byte sweep, the source of that `2`) retires in Phase 6. The rule above closes that regression: the same
raw sample bytes (and every earlier filter-chain stage of the image)
must *also* always be searched by the matcher (as a raw byte string,
independent of what OCR found), and only then, if nothing
matches, does `NOT_APPLICABLE` apply. This is not a new decoder or a new
reason -- it is a restatement of Principle 1's "closed enum" discipline
applied to what `NOT_APPLICABLE` is allowed to mean.

**Why "every filter-chain stage" is part of the rule.** Searching only
the decoded samples would leave a false exit-`0` path. A
JPEG whose comment (`COM`) segment holds `123-45-6789` carries the value
in the `DCTDecode` stream, but the value never reaches the decoded
pixels; a JPEG 2000 metadata box (XML, UUID, comment) and a JBIG2
extension segment are the same shape. Today's `verify.py` exits `2`
(`REVIEW_BINARY`) on the JPEG-comment case, because qpdf's raw byte
sweep sees the encoded stream; a rewrite that searched only decoded
samples would regress it to `0` once that sweep retires in Phase 6.

**Whether pattern classes should run over raw image sample bytes at all
is a separate, real gap the hidden-image case exposes, not fixed by the
rule above.**
The rule above covers *value* rules (a literal match against raw bytes);
it does not by itself make *pattern classes* (`ssn`, `credit-card`, etc.)
run over image samples, and today they do not run over any binary data
at all (by design -- see `verify.py`'s `_OPAQUE_STREAM_SUBTYPES`
exclusion, which exists because font-program bytes coincidentally match
pattern classes constantly). **Decided:** the new inventory's raw-byte
matcher pass (the rule above) runs value rules unconditionally
over every filter-chain stage of an image's bytes (the encoded bytes
the codec consumes as well as the decoded samples), and runs pattern
classes over the same stages only behind the same manual-review tier
`_scan_orphaned_payload` already uses for other raw-text contexts today (a coincidental digit run in
noisy sample data is exactly the kind of fusion that tier exists for) --
this needs its own case-library coverage in Phase 0b/3a, not
implementation here.

**The four reasons** (1-3 unchanged from the first version; reason 4
now also states the owner's small-image decision of 2026-09-27):

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
4. **Image data fully consumed by the image decoder.** Two ways to
   reach it, and in both, every filter-chain stage of the image's bytes
   (encoded and decoded) must first have been searched per the governing
   rule above -- value rules, and pattern classes at review tier:
   - **An image inside docs/adr/0004's envelope**, only once its recall
     bound is satisfied (docs/adr/0004, accepted: geometry and a
     completed OCR pass are *not* enough on their own -- REDESIGN §4
     requires a recall-validated envelope, which does not exist until
     Phase 4b measures it). Until then this path cannot actually be
     reached -- image evidence stays `FLAGGED`, consistent with ADR 0004.
   - **An image under the 8×32 `_text_sized` floor** (owner decision,
     2026-09-27), **without OCR and without a recall bound**, when its
     **actual decoded sample dimensions and sample count** -- not merely
     the declared `/Width` and `/Height` -- are under the floor. A
     declared-small image whose payload decodes to more samples than
     its declared dimensions allow is `FLAGGED`, never discharged here.

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
than only a documented one. So is the JPEG-comment case (a value in a
JPEG's comment segment, found only by a raw byte scan of the encoded
`DCTDecode` stream, never in the decoded samples).

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

## Owner decision (2026-09-27)

Owner decision: "approve all recommendations."

- **Adopt the four reasons** (xref-stream field data; object-stream
  header table; a font program whose parsed tables span the stream and
  whose strings were searched; image data fully consumed by the image
  decoder, gated on docs/adr/0004's recall bound, or for an image under
  the 8×32 floor on the small-image conditions below) and no others.
- **Adopt the governing rule**: no unit's bytes are ever exempt from the
  raw matcher -- `NOT_APPLICABLE` and `DECODED` excuse a decoder from
  further parsing, never from the search. This is a requirement on every
  decoder in the registry (each must run the raw matcher over the bytes
  it excuses, and say where), not only the image decoder. The matcher
  runs over every filter-chain stage, including the encoded bytes an
  image codec consumes, not only decoded samples (a wording fix within
  this rule, recorded after the approval: a JPEG comment segment, a
  JPEG 2000 metadata box, or a JBIG2 extension segment never reaches the
  decoded pixels).
- **Run pattern classes on raw image bytes at the manual-review
  tier** (every filter-chain stage, as above), the same tier
  `_scan_orphaned_payload` already uses for other raw-text contexts -- a
  real, currently unimplemented gap to close before Phase 4b.
- **Small images (owner decision, 2026-09-27, taken after the approval
  above).** An image under the 8×32 `_text_sized` floor is discharged
  under reason 4 without OCR and without a recall bound, but only once
  all its bytes (every filter-chain stage, encoded and decoded) have
  been raw-searched (value rules, and pattern classes at review tier),
  and only if its **actual decoded** sample dimensions and count, not
  merely its declared `/Width` and `/Height`, are under the floor. A
  declared-small image whose payload decodes to more samples than
  declared is `FLAGGED`. docs/adr/0004 records the same decision.
- **Schedule two cases in Phase 0b/3a, before Phase 4b** ships the image
  decoder, so the governing rule is a tested invariant rather than only
  a documented one: the hidden-image case (a 10x10 image whose 100 raw
  sample bytes spell "Employee SSN 123-45-6789"), and the JPEG-comment
  case (a JPEG whose comment segment holds `123-45-6789`, present in the
  encoded `DCTDecode` stream but not in the decoded pixels).

With docs/adr/0004 now also accepted, reason 4's dependency on 0004's
recall bound is a scheduling gate for images inside the envelope (that
path cannot actually be reached until Phase 4b measures recall), not an
open decision; the small-image path is reachable as soon as the
inventory can decode and raw-search an image's bytes. The list itself
(the four reasons, three rejected) needed no separate decision beyond
the ones recorded above.
