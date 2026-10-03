# Coverage

Where a secret can be stored in a PDF, how it is encoded there, and what
this tool does about each combination. This is the current **known** map,
built by probing each cell with a planted secret; adversarial review keeps
finding new places, so a missing row means "not yet known", never "safe".
A cell marked ✗ is a place a leaking document can still be certified
clean.

**Legend**

- ✓ **Read** — the content is searched; a match is a finding or a
  manual-review warning, per the reading layer's tier (see the README).
- ⚑ **Flagged** — the tool finds the content but cannot read it, and says
  so: a manual-review warning, so the run exits `2`, never `0`.
- ✗ **Not covered** — a secret here can pass as clean (exit `0`).
- — Not applicable.

A cell that relies on OCR assumes the OCR layer ran. When it cannot run —
off macOS, without the Vision bridge, or on a page Vision fails to read
(every page on macOS 27 until the fix in the CHANGELOG) — the run exits
`2`, never `0`.

Each cell has a stable id, `<row id>.<column>` (columns `plain`, `font`,
`pixels`, `container`), used by the case library in `eval/caselib` —
every ✓ and ⚑ cell has at least one case planting a secret there
(`eval/caselib/cells.py` lists them, with the ids of the ✗ parts of mixed
cells).

## Where × how

| Row id | Where the content is | Plain text | Font-coded text¹ | Pixels | Container² |
| --- | --- | --- | --- | --- | --- |
| `live` | **Live page content** — drawn on a page | ✓ Text, Objects, OCR · ✗ after a stream's end marker⁹ | ✓ Text, OCR · ✗ overprinted at the same point as other text | ✓ OCR, Binary (a value hidden in raw sample or codec bytes¹⁰) · ✗ under a box drawn over an image, a pattern rule over those same hidden bytes, extra rows beyond the declared height, or an EXIF/other thumbnail beyond the main decoded picture | — |
| `off-page` | **Off the page** — outside the visible crop/media box | ✓ Text, Objects | ✓ Text if the font has a Unicode map · ✗ otherwise, or running across the page edge | ✗ | — |
| `oc-off` | **Switched-off optional-content layer** | ✓ Objects | ✗ | ✗ | — |
| `annot-appearance` | **Annotation appearance** — a hidden annotation's drawing | ✓ Objects | ✗ | ✗ | — |
| `unused-resource` | **Referenced but never drawn** — an unused page resource | ✓ Objects | ✗ | ✗ | — |
| `orphaned` | **Orphaned objects** — still stored, referenced by nothing | ✓ Objects (`ORPHANED`)³ · ✗ after a stream's end marker⁹, or labelled as a font or image | ⚑ Objects⁴ · ✗ ordinary-looking codes⁴, one glyph per show operator | ⚑ Objects⁵ · ✗ small images⁵, text as outlines | ⚑ Objects |
| `superseded` | **Superseded versions** — rewritten by an incremental update | ✓ Objects (`earlier revision N`)³ ⁶ | ⚑ Objects⁴ | ⚑ Objects⁵ | ⚑ Objects |
| `metadata` | **Document metadata** — Info dictionary, XMP | ✓ Metadata (Info, XMP), Objects (Info only) | — | ✗ XMP thumbnails | — |
| `leftover-xmp` | **Orphaned / superseded XMP** | ✓ Objects⁷ | — | ✗ | — |
| `thumbnail` | **Page thumbnails** (`/Thumb`) | — | — | ✗ | — |
| `attachment` | **Attachments** — listed, or attached to an annotation | ✓ Hidden (manual review) | — | ⚑ Hidden | ⚑ Hidden⁸ · ✗ encoded as text² |
| `embedded-other` | **Other embedded files** — PDF 2.0 `/AF`, rich media | ✓ Binary: known values only (manual review) · ✗ pattern rules | — | ✗ | ✗ |
| `orphaned-attachment` | **Orphaned attachments** — typed or untyped | ✓ Objects (manual review) · ✗ untyped text with three stand-alone words that are content operators (`n`, `m`, `q` …) | — | ⚑ Objects | ⚑ Objects⁸ |
| `annot-fields` | **Annotation text, form fields, link targets, layer names** | ✓ Hidden, Objects | — | — | — |
| `javascript` | **JavaScript** | ✓ Hidden (catalog `/OpenAction`, named scripts), Objects (scripts stored as strings), Binary (scripts stored as streams: known values only, manual review) · ✗ pattern rules on scripts stored as streams on links, fields or pages | — | — | — |
| `private-data` | **Private application data** (`/PieceInfo`) | live: ✓ Binary known values only · ✗ pattern rules; leftover: ✓ Objects (manual review) | — | — | — |
| `unindexed` | **Unindexed bytes** — after the final `%%EOF`, in comments, or in an object whose table entry is marked free | ✗ | ✗ | ✗ | ✗ |

¹ Fonts whose codes are not the characters shown: CID fonts with
Identity-H encoding (Word, Chrome and embedded TrueType fonts commonly use
it), custom `/Differences` encodings, Type3 fonts. The Text layer applies
the font's Unicode map, so live text is read; OCR reads the rendered page.

² zip, Office documents, nested PDFs, gzip, rar, 7z — recognised by file
signature. A container encoded as text (an `.eml` with a base64 part, an
HTML file with a `data:` URI) is neither unpacked nor flagged — ✗.

³ Text in PDF string syntax is decoded; a leftover stream that is neither
page content nor binary (an untyped attachment, a script, private data)
is searched as raw text at the manual-review tier.

⁴ Flagged when any string a leftover stream shows is mostly non-text
codes (NUL-interleaved glyph numbers, control bytes). A font that maps
*ordinary-looking* codes to other glyphs is not detected — ✗.

⁵ Image XObjects and inline images at least 8 px on the short side and
32 px on the long side (a line of text); smaller masks and icons are not
flagged, and a secret in one is ✗.

⁶ Earlier revisions are located through the file's own cross-reference
chain (each `startxref` and trailer `/Prev`), so a `%%EOF` inside a stream
cannot invent one and a missing `%%EOF` cannot hide one. Only objects a
newer revision redefines are compared. Up to 50 revisions: the original
and the latest 49.

⁷ A known value is a hard finding; a pattern-class match is manual review
(IDs and dates in XMP assemble digit runs by coincidence).

⁸ Leftover payloads are read up to 16 MB decompressed; a warning says
when a larger one was cut.

⁹ Data inside a compressed stream's declared length but after its end
marker: both parsers stop at the marker, so it is never read.

¹⁰ A value's literal bytes in a drawn image's own raw samples (never
rendered as glyphs) or in a byte range a codec consumes but never
decodes to pixels (a JPEG comment segment): the Binary (qpdf) layer's
raw byte sweep sees them because it reads the file's stream bytes
directly, not through the image decoder. A pattern rule (rather than a
known value) over the same hidden bytes is not — the Binary layer's
sweep is value secrets only, the same limit as `embedded-other.plain`.

The ✗ parts of mixed cells and the gaps above have their own ids and,
where one exists, a case pinning today's wrong answer (`known_gap` in the
case library; `docs/REDESIGN.md` §8).

Two image gaps used to have no case (`docs/REDESIGN.md` §8, below the
K-table); Phase 3a added both, now K37 and K38. **Image data beyond the
declared size**: rows past an image's declared `/Height` are never
drawn, so a secret rendered there is never OCR'd, and MuPDF gives no
warning. **One render cut into strips**, each too thin to read alone:
the page's OCR reads them only when they are drawn next to each other
and nothing covers them; under a drawn box, drawn apart, or listed as a
resource but never drawn, they are not detected.

## Matching limits

These are not storage places but ways a value can be split so that no
matcher sees it whole. Each is ✗:

- A value split across a page break with a header, footer or page number
  between its halves, or where a page's last line is not its
  reading-order last line (two-column layouts, text rotated 270°).
- A value wrapped inside one column of a multi-column page — joining a
  page's lines interleaves the columns.
- Pattern-class numbers written without dashes (bare or space-separated)
  and wrapped at a line or page break.
- A pattern-rule value split at a page break inside form boxes, a
  one-character-per-line stack or rotated text, or with mixed separators —
  no reading joins it in a form the pattern accepts, so without a value
  rule for the same number it passes silently.
- Text placed at extreme coordinates (around 10⁹ points and beyond), which
  PyMuPDF does not return.

## What ⚑ costs, and what comes next

A flag is the honest answer to "found it, could not read it", but it means
a clean document that merely *contains* such content exits `2` too — on
real-world PDFs this fires rarely (about 1.5% in a corpus of 1,224, all
genuine leftover glyph-coded text from Adobe tools). Each flag is removed
by teaching the tool to read that cell, and each ✗ closed by reading or
flagging it:

- **Font-coded leftover text** — decode through the font's Unicode map,
  e.g. by re-attaching the leftover stream to the page it came from.
- **Pixels: leftover images, pixels under a drawn box, thumbnails** —
  OCR stored images directly, not only the rendered page.
- **Hidden layers, hidden annotations, unused resources** — extract and
  render them explicitly.
- **Containers** — unpack zip and Office files, scan nested PDFs as PDFs.
- **Other embedded files and scripts on actions** — walk every `/EF` and
  every action in the Hidden layer, with pattern rules.

The Binary layer (qpdf) is a cross-check with a second PDF parser rather
than a row of its own: it sees only objects reachable from the file's
root, so it is not a backstop for orphaned or superseded content.
