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
- **Dimensions**: both `/Width` and `/Height` in `[8, 10000]` px.
  **This floor does not "keep today's gate's intent" -- it inverts it.**
  Today, `_MIN_TEXT_IMAGE_SIDE`/`_MIN_TEXT_IMAGE_LENGTH` exist to
  **excuse** a tiny image from being flagged at all (too small for text
  to plausibly fit, so silence about it is acceptable). Under this
  envelope, the same floor instead **excludes** a tiny image from
  `DECODED` eligibility -- meaning a tiny image now gets `FLAGGED`
  where today's gate would pass it over in silence. That is a
  deliberate strictness increase (a tiny image previously ignored is
  now held to account), not a continuation of the old gate's purpose,
  and should be described that way.
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

## Consequences

- Making image-OCR evidence `FLAGGED` rather than `DECODED` until Phase
  4b's recall measurement means Phase 4b's own listed gate ("Leftover-
  image cells closed inside envelope; recall measured") is a
  **precondition** for this unit kind's enforcement, not a follow-up
  activity that happens after enforcement ships.
- No strictness regression claim is made or needed: every leftover image
  that exits `2` today keeps exiting `2` (or worse) under this design,
  because nothing here grants `DECODED` yet.
- The Indexed-base restriction and the corrected floor wording are both
  implementation-facing corrections; neither changes a measured rate in
  this pass (the corpus has no Separation/DeviceN-based Indexed images
  and no images near the size floor either way).

## Owner confirmation needed

- Confirm (or re-measure) the 35 Mpx pixel cap and the 10,000 px
  per-side ceiling against Apple Vision's actual request limits.
- Confirm the plan to run image-OCR as `FLAGGED`-only (never `DECODED`)
  until Phase 4b's recall measurement lands, rather than accepting a
  provisional, unmeasured `DECODED` grant sooner.
