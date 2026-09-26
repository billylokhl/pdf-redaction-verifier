# 0004. Image-OCR envelope

Status: accepted, one item needs owner confirmation

## Context

REDESIGN.md's decoder registry (§4) gives the image decoder's witness as
"`DECODED` only inside a recall-validated envelope (format, size, mask
type); outside it, or conversion failure -> flagged; per-image cap in
pixels, `>= 35 Mpx`." The envelope's *shape* (format/size/mask) was
already decided by the plan; Phase 1's job was to pin the actual
parameter values and confirm the pixel cap, which the plan states as a
floor ("`>= 35 Mpx`") rather than an exact number.

`verify.py` today never OCRs a stored image at all -- OCR only rasterizes
whole rendered pages (`extract_ocr_text`, `OCR_DPI = 300`). The nearest
existing gate is `_MIN_TEXT_IMAGE_SIDE = 8` / `_MIN_TEXT_IMAGE_LENGTH =
32` px, used only to decide whether an *unreadable leftover* image is
worth flagging at all -- a bypass-shaped magic number REDESIGN §1 already
calls out for replacement, not a validated envelope.

## Decision

An image is inside the envelope, and therefore eligible for `DECODED`,
only when all of:

- **Colour space / filter**: DeviceGray, DeviceRGB, DeviceCMYK, Indexed
  (any base), or ICC-based declaring one of those as its alternate --
  after our own normalisation to a grey bitmap (stencil mask handling,
  `/Decode` array applied, Indexed resolved through its palette).
  JBIG2Decode and JPXDecode are in scope (the decoder registry lists them
  explicitly) but only once MuPDF decodes them to raw samples first --
  the envelope check runs on the decoded bitmap, never on the compressed
  bytes.
- **Dimensions**: both `/Width` and `/Height` in `[8, 10000]` px. The
  floor keeps today's `_MIN_TEXT_IMAGE_SIDE`/`_MIN_TEXT_IMAGE_LENGTH`
  gate's intent (below this, nothing is legible at any DPI) but as a
  single, named, symmetric bound rather than two different numbers for
  the two axes. The ceiling is a placeholder pending the owner
  confirmation below.
- **Total pixels**: `width * height <= 35,000,000` (35 Mpx), taken as a
  fixed cap rather than the plan's floor -- this pass found no reason to
  set it higher and one reason to keep it exact: it must be named and
  tested (Principle 9), and "at least 35 Mpx" is not by itself a
  decidable predicate.
- **Mask type**: an `/SMask` or `/Mask` is only accepted when its own
  dimensions equal the base image's, or are a clean integer downscale of
  it (common for a lower-resolution soft mask) -- normalised and
  composited before OCR. A stencil mask (`/ImageMask true`) is OCR'd
  directly as a 1-bit bitmap after `/Decode` is applied. Any other mask
  shape (mismatched non-integer ratio, a mask referencing a different
  colour space) is outside the envelope.

Outside the envelope, or on any conversion failure, the unit is
`FLAGGED` -- never `NOT_APPLICABLE`, never silently skipped, per the
decoder registry row's own wording.

## Measurement

**This ADR's pixel-cap and dimension bounds are not independently
re-derived from a Vision OCR probe in this pass.** The `35 Mpx` figure
comes from REDESIGN.md's own decoder-registry text, which the earlier
feasibility work already established as a floor; this pass's budget
went to S1b and the three corpus rate measurements the plan explicitly
asked for, not to re-running Apple Vision against synthetic oversized
bitmaps to find its actual ceiling. **This is the one item in this ADR
that needs the owner's confirmation**: either accept 35 Mpx as measured
previously and cited here, or commission a short follow-up spike (feed
`VNRecognizeTextRequest` progressively larger synthetic bitmaps until it
errors or the process's memory watchdog would trip) before Phase 4b
enforces it.

What this pass *did* confirm: the local corpus contains no stored image
anywhere close to 35 Mpx (macOS system/app resources skew toward small
icons and vector content), so the cap's exact value has no effect on
this corpus's measured rates -- it is a forward-looking bound for
Phase 4b's real-world traffic, not something the Phase 1 corpus could
validate either way.

## Consequences

- A leftover image outside the envelope moves from today's
  `LEFTOVER_IMAGE` warning (already flag-worthy) to the same outcome
  under the new path -- no strictness regression.
- An image *inside* the envelope that fails OCR for a transient reason
  (decode error, Vision request failure) must still resolve to `FLAGGED`
  under this ADR's "conversion failure -> flagged" rule, not silently
  retried into `NOT_APPLICABLE`.
- The mask-matching rule is the part most likely to need revisiting once
  Phase 4b runs against real scanned/soft-masked corpora (this pass's
  corpus had essentially none) -- expect a follow-up ADR narrowing or
  widening it once recall is actually measured, as the decoder registry
  itself anticipates ("recall measured" is 4b's own gate).

## Owner confirmation needed

- Confirm (or re-measure) the 35 Mpx pixel cap and the 10,000 px
  per-side ceiling against Apple Vision's actual request limits before
  Phase 4b enforces this envelope as a hard gate.
