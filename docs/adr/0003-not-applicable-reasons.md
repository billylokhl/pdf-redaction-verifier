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
> classes run over the same stages, at review tier (below). A stage
> counts as searched only if nothing was lost decoding it: after a
> decode error, or any warning that suggests lost data (truncation, a
> corrupt or premature end of a codestream, a zlib or flate error), the
> stage is only partly searched and the unit is `FLAGGED`. At a filter
> or codec stage, a warning nobody has reviewed also means `FLAGGED`,
> and the only warning that may be excused there is an image-decoder
> warning reviewed as harmless, and then only by the image's own witness
> from Phase 4b (owner decision C, below). Interpreter warnings raised
> while running a content stream are governed by docs/adr/0009.**

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

The unreferenced rows exit `0` today only because of the 8×32
`_text_sized` floor. In the new design a leftover image (one no reached
content stream or appearance draws, whether unreferenced or only listed
as a resource) is always `FLAGGED`, whatever its size or what OCR finds
(owner decision D, 2026-09-27; docs/adr/0004), and a drawn one has no
size excusal (owner decision A; see reason 4 and docs/adr/0004): a
10×10 image is
treated like any other image -- enlarged and OCR'd, `FLAGGED` until
Phase 4b's recall bound covers small images. Either way its bytes are
still raw-searched under the governing rule above, so none of the four
rows can exit `0`.

**Why there is no size excusal.** An earlier same-day decision excused a
sub-floor image once its bytes had been raw-searched. Its premise, that
an image under 8×32 cannot plausibly carry text, is wrong: a 5×7-pixel
bitmap font renders an SSN as a 66×7 image, OCR reads it once the image
is enlarged, and a byte search cannot find text drawn as pixels. The
excusal would have been an exit-`0` path; it is also a gap in today's
tool (REDESIGN §8, K21). Removing the excusal closes the single small
image. It does not close the same render cut into strips: OCR'd one at
a time, each strip reads as nothing, so leftover strips are closed by
decision D (every leftover image is `FLAGGED`), not by enlargement.
Strips that are drawn but covered by something painted over them, or
drawn apart, are not closed by either: they are a known miss of
per-image OCR and an open question for the owner before Phase 4b
(docs/adr/0004).

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

**An image that decodes to more than declared is `FLAGGED`** (owner
decision B, 2026-09-27). An image can carry data beyond the main
decoded picture: an EXIF or other thumbnail (a JPEG APP1 or APP13
segment), additional JPEG 2000 codestreams, extra JBIG2 pages, decoded
samples beyond `/Width`×`/Height`×components×bits per component, or a
codec frame larger than the dictionary declares. None of it is ever
drawn, so OCR never sees it, and text in it is pixels, so the raw
matcher cannot find it either. Any such data means the image is
`FLAGGED` (exit `2`), not discharged.

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
restated by the owner's decisions of 2026-09-27):

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
4. **Image data fully consumed by the image decoder.** One path: the
   image is used by the document, meaning drawn by some reached content
   stream or appearance (a leftover image, including one listed as a
   resource that nothing draws, is always `FLAGGED`, owner decision D),
   it is inside docs/adr/0004's
   recall-validated envelope and its recall bound is satisfied
   (docs/adr/0004, accepted: geometry and a
   completed OCR pass are *not* enough on their own -- REDESIGN §4
   requires a recall-validated envelope, which does not exist until
   Phase 4b measures it). The envelope has no size excusal: its lower
   bound is whatever Phase 4b validates with enlargement, so the recall
   bound must cover images under 8×32 before any of them can be
   discharged. In addition, every filter-chain stage of the image's
   bytes (encoded and decoded) must have been searched per the governing
   rule above (value rules, and pattern classes at review tier); no
   stage may have lost data (a decode error, a warning that suggests
   lost data, or an unrecognised warning means `FLAGGED`; a warning on
   the reviewed-harmless allowlist is excused only by the image's own
   witness -- owner decision C); and the image must not decode to more
   than declared (a thumbnail, an extra JPEG 2000 codestream or JBIG2
   page, samples beyond the declared frame, or a larger codec frame
   means `FLAGGED` -- owner decision B). Until Phase 4b's recall
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
than only a documented one. So are the JPEG-comment case (a value in a
JPEG's comment segment, found only by a raw byte scan of the encoded
`DCTDecode` stream, never in the decoded samples), the EXIF-thumbnail
case (a small JPEG whose EXIF thumbnail shows the SSN; the image must be
`FLAGGED`), the extra-rows case (a referenced 200×20 DeviceGray image
whose declared frame is blank but whose stream holds 20 more rows with
an SSN render; the image must be `FLAGGED`), and the pixel-text-under-the-floor
case (a leftover image under 8×32 holding pixel-drawn text; caselib's
`leftover.small-image`, K21, already expects `LEFTOVER_IMAGE`; the
strips variant is not yet a case of its own; Phase 3a adds it, with a
strips-under-a-box variant: both strips drawn, a box painted over
them).

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
  decoder, gated on docs/adr/0004's recall bound) and no others.
- **Adopt the governing rule**: no unit's bytes are ever exempt from the
  raw matcher -- `NOT_APPLICABLE` and `DECODED` excuse a decoder from
  further parsing, never from the search. This is a requirement on every
  decoder in the registry (each must run the raw matcher over the bytes
  it excuses, and say where), not only the image decoder.
- **Run pattern classes on raw image bytes at the manual-review
  tier**, the same tier `_scan_orphaned_payload` already uses for other
  raw-text contexts -- a real, currently unimplemented gap to close
  before Phase 4b.
- **Schedule the hidden-image case** (a 10x10 image whose 100 raw sample
  bytes spell "Employee SSN 123-45-6789") **in Phase 0b/3a, before Phase
  4b** ships the image decoder, so the governing rule is a tested
  invariant rather than only a documented one.

**Recorded after the approval** (wording and case scheduling, not new
decisions):

- The raw matcher runs over every filter-chain stage, including the
  encoded bytes an image codec consumes, not only decoded samples; the
  pattern-class pass covers the same stages. A JPEG comment segment, a
  JPEG 2000 metadata box, or a JBIG2 extension segment never reaches the
  decoded pixels.
- The JPEG-comment case (a JPEG whose comment segment holds
  `123-45-6789`, present in the encoded `DCTDecode` stream but not in
  the decoded pixels) is scheduled next to the hidden-image case.
- Cases for the owner decisions below, all in Phase 3a before Phase 4b:
  the strips variant of K21 (two leftover 90×7 strips of one SSN
  render), a strips-under-a-box case next to it (both strips drawn, a
  black box painted over them; today's `verify.py` exits `0`), the
  EXIF-thumbnail case (a small JPEG whose EXIF thumbnail
  shows the SSN) next to the JPEG-comment case, and beside it the
  extra-rows case (a referenced 200×20 DeviceGray image with a blank
  declared frame plus 20 extra rows holding an SSN render; today's
  `verify.py` exits `0` on it and MuPDF gives no warning).

An earlier revision of this section also recorded here, as wording,
that any filter or codec error or warning means `FLAGGED` (a
"clean-decode rule"). That was a new rule, not wording, and decision C
below replaces it.

**Owner decisions (2026-09-27), taken after the approval.** A and B
supersede an earlier same-day decision that excused an image under the
8×32 floor, without OCR or a recall bound, once its bytes had been
raw-searched. That decision's premise -- that such an image cannot
plausibly carry text -- was wrong. C replaces the clean-decode rule
above. D extends docs/adr/0007's rule for orphaned content streams to
images (recorded in full in docs/adr/0004).

- **A. No size excusal for tiny images.** An image under the 8×32 floor
  is treated like any other image: normalised, enlarged and OCR'd, and
  `DECODED` (or `NOT_APPLICABLE` under reason 4) only once Phase 4b has
  measured a recall bound that covers small images. Until then it is
  `FLAGGED`, like every other image. Its bytes are still raw-searched at
  every filter-chain stage. Reason: a 5×7-pixel bitmap font renders an
  SSN as a 66×7 image; OCR reads it after enlargement; a byte search
  cannot find text drawn as pixels. It is also today's tool's gap
  (REDESIGN §8, K21). A closes the single small image; the strips
  variant is closed by D, not by A.
- **B. An image that decodes to more than declared is `FLAGGED`.** In
  the owner's words: "Any image data in the file beyond the main decoded picture
  (EXIF/other thumbnails, extra JPEG 2000 codestreams, extra JBIG2
  pages) counts as 'decodes to more than declared', so the image is
  flagged." Decoded samples
  beyond `/Width`×`/Height`×components×bits per component, and a codec
  frame larger than declared, decode to more than declared, so they are
  within B itself. The image is `FLAGGED` (exit `2`).
- **C. Image-decoder warnings, split by kind.** A decode error, or any
  warning that suggests lost data (truncation, a corrupt or premature
  end of a codestream, a zlib or flate error), means the image is always
  `FLAGGED`. Warnings reviewed as harmless (for example JPEG 2000's
  `numcomps doesn't match color_space`, openjpeg's `misplaced cmap box`)
  go on a reviewed allowlist; from Phase 4b the image's own witness may
  vouch for them. Any unrecognised warning means `FLAGGED`. This
  replaces the clean-decode rule and agrees with docs/adr/0009's guard
  3.
- **D. Leftover images are always `FLAGGED`.** An image nothing in the
  document uses is `FLAGGED` whatever its size and whatever OCR finds,
  like docs/adr/0007's orphaned content streams. "Uses" is read as
  *draws*: an image is used only if some reached content stream or
  appearance draws it, so one referenced only as a resource and never
  drawn is leftover. Reason: an SSN image cut into 7 px strips cannot be
  read strip by strip, and nothing reassembles strips that nothing
  draws. See docs/adr/0004 for the evidence, for drawn strips (not
  covered by D; an open question before Phase 4b), and for the cost,
  which Phase 3a measures.

With docs/adr/0004 now also accepted, reason 4's dependency on 0004's
recall bound is a scheduling gate (it cannot actually be reached until
Phase 4b measures a recall bound, one that covers small images), not an
open decision -- the list itself (the four reasons, three rejected)
needed no separate decision beyond the ones recorded above.
