# 0004. Image-OCR envelope

Status: proposed

## Context

REDESIGN.md's decoder registry (§4) gives the image decoder's witness as
"`DECODED` only inside a **recall-validated** envelope (format, size,
mask type); outside it, or conversion failure -> flagged; per-image cap
in pixels, `>= 35 Mpx`." "Recall-validated" is doing real work in that
sentence: it means the envelope's boundaries come from *measuring* how
often OCR actually finds text inside them on a labelled sample, not from
geometry and a completed decode alone. §4b's own gate says exactly this:
"recall measured" -- a Phase 4b activity that has not happened yet.

**Correction to the first version of this ADR.** It granted `DECODED`
from geometry (dimensions, pixel cap, mask shape) plus "OCR completed
without error" alone, with no recall bound at all -- contradicting §4's
"recall-validated" requirement outright. It also claimed "no strictness
regression," which is false: a leftover image today that is `>=8x32`px
(`_MIN_TEXT_IMAGE_SIDE`/`_MIN_TEXT_IMAGE_LENGTH`) is flagged (exit `2`)
as a leftover the tool cannot read; under the first version's ADR, such
an image inside the new envelope would become `DECODED` and could exit
`0` -- if OCR finds nothing (because there is nothing to find, or
because OCR missed real text), a file that is uncertifiable today would
newly certify as clean. That is a real strictness *decrease*, exactly
what REDESIGN's "never becomes less strict" transition rule forbids.

`verify.py` today never OCRs a stored image at all -- OCR only rasterizes
whole rendered pages (`extract_ocr_text`, `OCR_DPI = 300`).

## Decision

**`DECODED` requires a recall bound Phase 4b has not measured yet, so
until that measurement exists, image-OCR evidence is `FLAGGED`, never
`DECODED`** -- geometry and a completed OCR pass narrow *which* images
are even attempted, they do not by themselves justify treating an
OCR-clean result as proof of absence. This is the one substantive change
from the first version: the envelope below still describes which images
are *worth attempting*, but attempting and succeeding at OCR is no
longer sufficient for `DECODED` on its own.

**The envelope** (unchanged in shape, corrected in wording):

- **Colour space / filter**: DeviceGray, DeviceRGB, DeviceCMYK, or
  ICC-based declaring one of those as its alternate -- after our own
  normalisation to a grey bitmap (stencil mask handling, `/Decode`
  applied). **Indexed images are in scope only when their base colour
  space is itself one of the above** (Gray/RGB/CMYK or a matching
  ICC-alternate) -- the first version said "Indexed (any base)," which
  is wrong: an Indexed image whose base is Separation/DeviceN needs a
  tint-transform function evaluated per palette entry, not a simple
  lookup, and is out of scope for Phase 1. JBIG2Decode and JPXDecode are
  in scope only once MuPDF decodes them to raw samples first -- the
  envelope check runs on the decoded bitmap, never the compressed bytes.
- **Dimensions**: the upper bound is `10,000` px per side. For the lower
  bound, **do not simply exclude a sub-8px image from the envelope** (see
  Measurement below: 248 such images exist in this corpus, 5.6% of
  text-bearing files) -- that would flag every icon, bullet, and checkbox
  glyph as unreadable, a real, avoidable review-rate cost. Instead,
  **reuse today's `_text_sized`/`_MIN_TEXT_IMAGE_SIDE` judgment** (too
  small for text to plausibly fit, so it is excused from `DECODED`
  scrutiny the same way it is excused from being flagged today) --
  **and** always run docs/adr/0003's raw-byte matcher pass over the
  decoded samples regardless of size, closing the actual gap (a value
  hidden in tiny sample data) without the size-floor cost. (An earlier
  version of this ADR called the floor "not keeping today's gate's
  intent" and treated flagging every sub-floor image as a deliberate
  strictness increase; measurement showed that increase is neither free
  nor obviously justified, so the recommendation is reversed here.)
- **Total pixels**: `width * height <= 35,000,000` (35 Mpx), taken as a
  fixed cap rather than the plan's floor.
- **Mask type**: an `/SMask` or `/Mask` is only accepted when its own
  dimensions equal the base image's, or are a clean integer downscale;
  a stencil mask (`/ImageMask true`) is OCR'd directly after `/Decode`.
  Any other mask shape is outside the envelope.

Outside the envelope, or on conversion failure, the unit is `FLAGGED`.
**Inside** the envelope, until a recall bound exists, an OCR-clean result
is *also* `FLAGGED` (not `DECODED`) -- the envelope narrows what gets
attempted; it does not yet certify a clean attempt.

**Known misses inside the envelope**, for the record ahead of Phase 4b's
recall measurement:

- **Post-downsample small glyphs.** Apple Vision's own internal
  processing can downsample a large input image before recognition;
  fine print that is legible at the image's native resolution can become
  illegible after Vision's own resize, with no error raised -- a false
  `DECODED` risk indistinguishable from "genuinely no text" without a
  recall measurement.
- **Isoluminant colours.** Two colours with the same luminance but
  different hue (e.g. red text on a green background) can map to the
  *same* grey value under our RGB/CMYK-to-grey normalisation, making
  real text structurally invisible to OCR after normalisation even
  though it is visually plain to a human viewer.

## Measurement

**The 35 Mpx / 10,000 px bounds are not independently re-verified
against Apple Vision's actual limits in this pass** -- they are cited
from REDESIGN.md's own decoder-registry text (attributed there to
earlier feasibility work). This pass's budget went to the corpus rate
measurements and spike S1b the plan explicitly asked for.

The local corpus contains no stored image anywhere close to 35 Mpx, so
the cap's exact value has no effect on this corpus's measured rates.

**The size floor is a different story: tiny images are common, and an
earlier version of this ADR's claim that "no images [are] near the size
floor" was false.** `eval/spikes/image_envelope_stats.py` walks every
image object's `/Width`/`/Height` directly (no decompression needed):

| | All files | Text-bearing |
| --- | --- | --- |
| Files with >= 1 image under 8 px (either dimension) | 1.9% (39/2,031) | 5.6% (27/484) |
| Total images under 8 px | 248 | 224 |

**Recommendation, corrected**: rather than flagging every one of these
248 images under the new envelope (a real, avoidable review-rate cost),
apply today's existing `_text_sized`/`_MIN_TEXT_IMAGE_SIDE` excusal
logic to decide when a sub-floor image is *worth flagging at all* --
i.e. keep today's judgment that a genuinely tiny image (an icon, a
bullet, a checkbox glyph) is implausible as a text carrier and excuse it
-- **and** always still run docs/adr/0003's raw-byte matcher pass over
its decoded samples regardless of size. This keeps the strictness
increase docs/adr/0003's steganographic-image case actually needs (byte
content is always searched) without also flagging 248 ordinary icons
that were never going to be `DECODED` as text anyway.

## Consequences

- Making image-OCR evidence `FLAGGED` rather than `DECODED` until Phase
  4b's recall measurement means Phase 4b's own listed gate ("Leftover-
  image cells closed inside envelope; recall measured") is a
  **precondition** for this unit kind's enforcement, not a follow-up
  activity that happens after enforcement ships.
- No strictness regression claim is made or needed: every leftover image
  that exits `2` today keeps exiting `2` (or worse) under this design,
  because nothing here grants `DECODED` yet.
- The Indexed-base restriction is an implementation-facing correction
  that does not change a measured rate in this pass (no
  Separation/DeviceN-based Indexed images in the corpus). The size-floor
  recommendation does change a real rate: reusing today's `_text_sized`
  excusal instead of flagging every sub-8px image avoids flagging up to
  248 images (5.6% of text-bearing files) that were never going to carry
  readable text anyway.

## Owner confirmation needed

- Confirm (or re-measure) the 35 Mpx pixel cap and the 10,000 px
  per-side ceiling against Apple Vision's actual request limits.
- Confirm the plan to run image-OCR as `FLAGGED`-only (never `DECODED`)
  until Phase 4b's recall measurement lands, rather than accepting a
  provisional, unmeasured `DECODED` grant sooner.
- Confirm excusing a sub-8px image via today's `_text_sized` logic
  (recommended) rather than flagging all 248 such images found in this
  corpus under the new envelope.
