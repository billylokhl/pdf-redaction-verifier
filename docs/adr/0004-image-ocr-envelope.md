# 0004. Image-OCR envelope

Status: accepted (owner approval, 2026-09-27)

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
regression," which is false: a leftover image today that is at least 8×32 px
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
  (The raw matcher is a different matter: per docs/adr/0003's governing
  rule it runs over every filter-chain stage, the compressed bytes the
  codec consumes included.)
- **Dimensions**: the upper bound is `10,000` px per side. For the lower
  bound, **do not simply exclude every image below some floor from the
  envelope and flag it** (see Measurement below: 248 images under 8 px
  on a side, in 39 files -- 1.9% of all files; 224 of them in 27 files,
  5.6% of text-bearing files) -- that would flag every icon, bullet, and
  checkbox glyph as unreadable, a real, avoidable review-rate cost.
  Instead, **reuse today's 8×32 `_text_sized` judgment**
  (`verify.py`'s `_MIN_TEXT_IMAGE_SIDE` = 8 and
  `_MIN_TEXT_IMAGE_LENGTH` = 32: an image under 8 px on its short side
  *or* under 32 px on its long side is too small for text to plausibly
  fit). **Such an image is outside the envelope but is not `FLAGGED`:
  it is discharged as `NOT_APPLICABLE` under docs/adr/0003's reason 4,
  without OCR and without a recall bound** (owner decision, 2026-09-27),
  on two conditions: (a) every filter-chain stage of its bytes, encoded
  and decoded, has been raw-searched (value rules, and pattern classes
  at review tier), which closes the actual gap (a value hidden in small
  sample data or in codec metadata) without the size-floor cost; and
  (b) its **actual decoded** sample dimensions and sample count, not
  merely the declared `/Width` and `/Height`, are under the floor. A
  declared-small image whose payload decodes to more samples than
  declared is `FLAGGED`. (An earlier version of this ADR called the
  floor "not keeping today's gate's intent" and treated flagging every
  sub-floor image as a deliberate strictness increase; measurement
  showed that increase is neither free nor obviously justified, so that
  version's position was reversed.)
- **Total pixels**: `width * height <= 35,000,000` (35 Mpx), taken as a
  fixed cap rather than the plan's floor.
- **Mask type**: an `/SMask` or `/Mask` is only accepted when its own
  dimensions equal the base image's, or are a clean integer downscale;
  a stencil mask (`/ImageMask true`) is OCR'd directly after `/Decode`.
  Any other mask shape is outside the envelope.

Outside the envelope, or on conversion failure, the unit is `FLAGGED`
-- except an image under the size floor that meets both conditions
above, which is `NOT_APPLICABLE`.
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

**An earlier version of this ADR said the local corpus contains no
stored image anywhere close to 35 Mpx. That is false**:
`eval/spikes/image_envelope_stats.py` finds two stored images of
4464×8579 px (38.3 Mpx) in one text-bearing file, over the 35 Mpx cap.
Under this envelope both would be outside it and `FLAGGED` -- a
one-file cost on this corpus, but it means the cap's exact value does
bite on real files.

**The size floor is a different story: small images are common, and an
earlier version of this ADR's claim that "no images [are] near the size
floor" was false.** `eval/spikes/image_envelope_stats.py` walks every
image object's `/Width`/`/Height` directly (no decompression needed):

| | All files (2,031) | Text-bearing (484) |
| --- | --- | --- |
| Images under 8 px on either side | 248 images in 39 files (1.9%) | 224 images in 27 files (5.6%) |
| Images today's 8×32 `_text_sized` rule excuses (under 8 px short side or under 32 px long side) | 299 images in 51 files (2.5%) | 235 images in 27 files (5.6%) |
| ...of which not under 8 px (the 8×32 rule's addition) | 51 images in 13 files | 11 images in 1 file |
| Images over the 35 Mpx cap | 2 images in 1 file | 2 images in 1 file |

**Decision, corrected**: rather than flagging every one of these
images under the new envelope (a real, avoidable review-rate cost),
apply today's 8×32 `_text_sized` excusal to decide when a small image
is *worth flagging at all* -- i.e. keep today's judgment that a
genuinely small image (an icon, a bullet, a checkbox glyph) is
implausible as a text carrier and excuse it -- **and** always still run
docs/adr/0003's raw-byte matcher pass over every filter-chain stage of
its bytes regardless of size. These counts use the declared
`/Width`/`/Height`; the discharge itself checks the actual decoded
sample dimensions (Decision, above). This keeps the strictness increase docs/adr/0003's
hidden-image case actually needs (byte content is always searched)
without also flagging 299 ordinary icons (235 in text-bearing files)
that were never going to be `DECODED` as text anyway.

## Consequences

- Making image-OCR evidence `FLAGGED` rather than `DECODED` until Phase
  4b's recall measurement means Phase 4b's own listed gate ("Leftover-
  image cells closed inside envelope; recall measured") is a
  **precondition** for this unit kind's enforcement, not a follow-up
  activity that happens after enforcement ships.
- No strictness regression claim is made or needed: every leftover image
  that exits `2` today keeps exiting `2` (or worse) under this design,
  because nothing here grants `DECODED` yet, and the one `NOT_APPLICABLE`
  discharge (a sub-floor image) covers only images whose declared size
  today's tool already excuses (`verify._is_text_sized_image` reads the
  declared `/Width`/`/Height`) -- now also requiring the decoded size to
  be under the floor, and with all their bytes searched.
- The Indexed-base restriction is an implementation-facing correction
  that does not change a measured rate in this pass (no
  Separation/DeviceN-based Indexed images in the corpus). The size-floor
  decision does change a real rate: reusing today's 8×32
  `_text_sized` excusal instead of flagging every small image avoids
  flagging 299 images in 51 files (2.5% of all files) -- 235 images in
  27 files (5.6% of text-bearing files) -- that were never going to
  carry readable text anyway.

## Owner decision (2026-09-27)

Owner decision: "approve all recommendations."

- **Image OCR stays `FLAGGED`-only (never `DECODED`) until Phase 4b
  measures recall** -- no provisional, unmeasured `DECODED` grant
  sooner.
- **Keep today's 8x32 `_text_sized` excusal** for the envelope's lower
  size bound (299 images in 51 files, 2.5% of all files; 235 in 27,
  5.6% of text-bearing) **while still always raw-searching every
  filter-chain stage of the image's bytes** (encoded and decoded) per
  docs/adr/0003's governing rule, rather than flagging every small image
  under the new envelope.
- **Small images (owner decision, 2026-09-27, taken after the approval
  above).** An image under the 8×32 floor is discharged under
  docs/adr/0003's `NOT_APPLICABLE` reason 4 without OCR and without a
  recall bound, but only once all its bytes (every filter-chain stage,
  encoded and decoded) have been raw-searched (value rules, and pattern
  classes at review tier), and only if its **actual decoded** sample
  dimensions and count, not merely its declared `/Width` and `/Height`,
  are under the floor. A declared-small image whose payload decodes to
  more samples than declared is `FLAGGED`.
- **The 35 Mpx per-image cap stands** as specified -- it already
  excludes two real 38.3 Mpx images in one text-bearing file. It is
  **not yet independently verified against Apple Vision's actual request
  limits**; re-check it (and the 10,000 px per-side ceiling) in Phase
  4b.
