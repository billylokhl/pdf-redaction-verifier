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
  so: a manual-review warning, so the run exits `2`, never `0`. (Under
  Hostile input, ⚑ also marks a value found, exit `1`, but filed under
  the wrong storage¹⁵.)
- ✗ **Not covered** — a secret here can pass as clean (exit `0`).
- — Not applicable.

A cell that relies on OCR assumes the OCR layer ran. When it cannot run —
off macOS, without the Vision bridge, on a page where Vision raises an
error (every page on macOS 27 until the fix in the CHANGELOG), or on a
page where Vision reports success without delivering both readings, on
a page where a Vision call does not return within its deadline, or on a
page too large for Vision to read whole (¹² below) —
the run never exits `0` (`2`, or `1` if another layer found a leak). If
Vision delivers a reading without error but misses text, nothing flags
it: the OCR cells' ✓ assumes Vision's reading (¹² below).

Each cell has a stable id, `<row id>.<column>` (columns `plain`, `font`,
`pixels`, `container`), used by the case library in `eval/caselib` —
every ✓ and ⚑ cell has at least one case planting a secret there
(`eval/caselib/cells.py` lists them, with the ids of the ✗ parts of mixed
cells).

## Where × how

| Row id | Where the content is | Plain text | Font-coded text¹ | Pixels | Container² |
| --- | --- | --- | --- | --- | --- |
| `live` | **Live page content** — drawn on a page | ✓ Text, Objects, OCR · ✗ after a stream's end marker⁹ | ✓ Text, OCR · ✗ overprinted at the same point as other text | ✓ OCR, Binary (a value hidden in raw sample or codec bytes¹⁰) · ⚑ OCR on a page too large for Vision¹² · ✓ OCR under a box drawn over an image (⚑ without OCR)¹⁷ · ✗ a pattern rule over those same hidden bytes, extra rows beyond the declared height, an EXIF/other thumbnail beyond the main decoded picture, a one-character-per-line stack Vision does not report as a line, searched with a value rule (see Matching limits), or a line OCR silently leaves out of its reading¹², or an image with far more detail than the page's OCR render resolves (`live.pixels.oversized-image`) | — |
| `off-page` | **Off the page** — outside the visible crop/media box | ✓ Text, Objects | ✓ Text if the font has a Unicode map, also written vertically or running across the page edge · ✗ if the font has no Unicode map | ✗ | — |
| `oc-off` | **Switched-off optional-content layer** | ✓ Objects | ✗ | ✗ | — |
| `annot-appearance` | **Annotation appearance** — a hidden annotation's drawing | ✓ Objects | ✗ | ✗ | — |
| `unused-resource` | **Referenced but never drawn** — an unused page resource | ✓ Objects | ✗ | ✗ | — |
| `orphaned` | **Orphaned objects** — still stored, referenced by nothing | ✓ Objects (`ORPHANED`)³ · ✗ after a stream's end marker⁹, or labelled as a font or image | ⚑ Objects⁴ · ✗ ordinary-looking codes⁴, one glyph per show operator | ⚑ Objects⁵ · ✗ small images⁵, text as outlines | ⚑ Objects |
| `superseded` | **Superseded versions** — rewritten by an incremental update | ✓ Objects (`earlier revision N`)³ ⁶, trailers¹¹ | ⚑ Objects⁴ | ⚑ Objects⁵ | ⚑ Objects |
| `metadata` | **Document metadata** — Info dictionary, XMP | ✓ Metadata (Info, XMP), Objects (Info only) | — | ✗ XMP thumbnails | — |
| `leftover-xmp` | **Orphaned / superseded XMP** | ✓ Objects⁷ | — | ✗ | — |
| `thumbnail` | **Page thumbnails** (`/Thumb`) | — | — | ⚑ Hidden¹³ | — |
| `trailer` | **Document trailer** — the `/ID` and any other strings in the trailer dictionary | ✓ Objects (`trailer`)¹¹ | — | — | — |
| `attachment` | **Attachments** — listed, or attached to an annotation | ✓ Hidden (manual review) | — | ⚑ Hidden | ⚑ Hidden⁸ · ✗ encoded as text² |
| `embedded-other` | **Other embedded files** — PDF 2.0 `/AF`, rich media | ✓ Binary: known values only (manual review) · ✗ pattern rules | — | ✗ | ✗ |
| `orphaned-attachment` | **Orphaned attachments** — typed or untyped | ✓ Objects (manual review) · ✗ untyped text with three stand-alone words that are content operators (`n`, `m`, `q` …) | — | ⚑ Objects | ⚑ Objects⁸ |
| `annot-fields` | **Annotation text, form fields, link targets, layer names** | ✓ Hidden, Objects | — | — | — |
| `javascript` | **JavaScript** | ✓ Hidden (catalog `/OpenAction`, named scripts), Objects (scripts stored as strings), Binary (scripts stored as streams: known values only, manual review) · ✗ pattern rules on scripts stored as streams on links, fields or pages | — | — | — |
| `private-data` | **Private application data** (`/PieceInfo`) | live: ✓ Binary known values only · ✗ pattern rules; leftover: ✓ Objects (manual review) | — | — | — |
| `unindexed` | **Unindexed bytes** — after the final `%%EOF`, in comments, or in an object whose table entry is marked free | ✗ · ⚑ Binary when qpdf warns about the bytes¹⁶ | ✗ | ✗ | ✗ |

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
(IDs and dates in XMP assemble digit runs by coincidence) in strict mode,
and an informational notice by default ([ADR 0012](docs/adr/0012-pattern-classes-inform.md)).

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

¹¹ Every trailer dictionary is read from the file's own bytes: the
current one, each earlier revision's (`earlier revision N, trailer`),
and any other `trailer << ... >>` in the file (`trailer at byte N`); a
dictionary with no `trailer` keyword before it, or with junk between the
two, is unindexed bytes and not read (`unindexed.plain`). A
cross-reference stream's dictionary (PDF 1.5+) is the trailer too, and
is labelled as such. The Binary layer cannot stand in here: qpdf writes
only the current trailer, with the file's first `/ID` element
hex-encoded and the second replaced. Every trailer string is matched at
the hard tier, except a pattern-rule match in a `/ID` element made only
of hex digits and dashes — a generated identifier written as text, which
holds digit runs by coincidence (about one 32-character string in
thirteen) — which is manual review. A known value is a hard finding
anywhere in the trailer. A section's trailer is the `trailer` keyword
right after its table, in any line layout. Only the dictionary's own top-level `/ID` key
counts as `/ID`. Comments inside a trailer are skipped when reading its
strings and read themselves as raw text, at manual review; a trailer the
chain reaches but this tool cannot parse is a warning (exit `2`).

¹² Vision can report success and return fewer lines than the page holds.
On macOS 27, a scanned page of 40 lines of 8 pt text turned 90° comes
back without eight of them (14–21), the SSN's line among them
(`page.dense-sideways-scan`, [#67](https://github.com/billylokhl/pdf-redaction-verifier/issues/67)); with 20 or 30 lines it is read
whole. A page with any line read vertically is now also read turned 90°
and 270°, which found the SSN's line on 21 of 24 generated sideways
pages (macOS 27), but only as manual review (exit `2`):
turned, a one-character-per-line stack becomes a row Vision misreads as
a number. A page whose dropped lines are horizontal, or where Vision
reports no vertical line at all, can still pass as clean; nothing yet
compares Vision's reading with the ink on the page ([#82](https://github.com/billylokhl/pdf-redaction-verifier/issues/82)).
How much Vision reads also depends on the size of the print against the
render's longest side. On macOS 27, a 2400 × 3200 pt page (10000 ×
13334 px at OCR's 300 dpi) came back without any of its ten lines of
8 pt text ([#128](https://github.com/billylokhl/pdf-redaction-verifier/issues/128)); on renders 3600 px long some whole blocks of 8 pt
print were lost, from 3900 px more, and of 10 pt from 4250 px. A page
whose render is longer than 3510 px on a side (about 11.7 in: letter and
A4 pages are below it, legal pages are not) is therefore still read,
and whatever OCR finds on it counts, but it is also `PAGE_FAILED` (⚑,
exit `2` when nothing is found, `page.oversized-scan`). The cap is a
heuristic, not a guarantee: the losses do not grow steadily with size
and depend on the content, and below it Vision still drops part of a
block now and then (8 of 20 lines once at 3400 px, 6 of 20 on a
landscape letter page).

¹³ Any page carrying a `/Thumb` preview image — on the page itself, or
on a Pages node above it — gets a `PAGE_THUMBNAIL` warning (exit `2`),
whatever the image shows: the thumbnail is not decoded or OCR'd yet, and
a clean document with thumbnails cannot certify as `0` until it is. A
thumbnail only an earlier revision still holds is covered as that
revision's superseded or orphaned image (⁵: flagged if big enough to
hold text, ✗ below the size gate).

¹⁷ An image with an opaque box drawn over it (K12; ADR 0004's drawn
strips under a box, K38): OCR reads the rendered page, so it sees the
box. Since the legacy hotfix of 2026-10-15 (owner decision), a page with
an image that one opaque fill drawn after it covers by at least a
quarter of its area gets `IMAGE_COVERED` (⚑, exit `2`, with or without
OCR), and with OCR the page is rebuilt from its images alone, each at
its drawn place, and read again: a listed value there is a finding
(exit `1`); strips drawn apart in one render rejoin as drawn. Limits:
one fill's coverage counts (boxes adding up are not summed), a fill's
bounding rectangle stands for its area, an image covered by another
image, by text or by clipping is not a cover, and inline images and
images over 35 Mpx are flagged but not rebuilt. A blank image under a
box is flagged too (`page.box-over-blank-image`, review). Phase 4b's
per-image evidence is to subsume this.

¹⁶ Some unindexed bytes make qpdf exit 3 ("succeeded with warnings"):
an in-use cross-reference entry at offset 0 while that object's body is
still stored in the file, tokens between an object's value and a missing
`endobj`, or an earlier body of an object that a damaged cross-reference
table's rebuild passes over. No layer reads those bytes, but the Binary
layer's `TOOL_EXIT_NONZERO` flags the file (exit `2`). Since the owner
decision of 2026-10-15 (ADR 0002's note), exit 3 flags only when some
qpdf warning is not one of five kinds or fails its check against the
file's own bytes: a `(0 0 R)` reference; offset 0 with no stored body of
that object anywhere (no possible `N G obj` header, read generously, and
no object or xref stream in the file); a duplicated key whose values are identical; a missing
`endobj` followed by nothing but whitespace and then the next object,
`xref` or `startxref`; and a Flate stream without its end marker that is
not an object stream (an xref stream only when every declared row
decodes). Any other warning, an unparseable line or a check that cannot
decide keeps the flag (`file.offset-zero-entry-stored-body`,
`file.offset-zero-entry-page-content`, `file.offset-zero-entry-shifted-earlier-body`, `file.stray-bytes-before-endobj`,
`file.damaged-xref-earlier-body`, `file.duplicated-filter-first-wins`).
The same bytes behind a *free* entry, after `%%EOF` or in a comment give
qpdf no warning and stay ✗.

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

## Hostile input

Not storage places but files made to cost a reader time or memory
(Phase 3d). Each case plants a value where the bomb is; the case library
runs them out of process, under per-case caps
(`eval/caselib/families/bombs.py`, `tests/test_bomb_cases.py`):

- ⚑ A value behind a decompression bomb (`hostile.flate-bomb`): in an
  orphaned stream, read only to its first 16 MiB and the rest flagged; in
  live content, read whole (found), with the stream's object flagged¹⁵.
- ⚑ A value in a file with deeply nested objects
  (`hostile.deep-nesting`): found, with what could not be parsed
  flagged¹⁵.
- ✓ A value on a page in a cyclic page tree (`hostile.page-tree-cycle`):
  the page is read once, the cycle flagged.
- ⚑ A value in a file whose page tree claims an absurd page count
  (`hostile.huge-page-count`)¹⁴.
- ✓ A value among a flood of content operators
  (`hostile.operator-flood`): found, within its memory cap.

¹⁴ Exit `2` today comes from a caught internal error, not a flag: it
fails closed, but names no reason. Phase 3d turns it into a limit flag.

¹⁵ Found (exit `1`), filed under the wrong storage: the live flate bomb's
unreadable object is reported as orphaned, and the deeply nested file's
page — live, and drawn by other viewers — as unreferenced. The verdict is
right, but the report misleads about where the value is. A legacy
mislabel, pinned by each case; the redesign's ledger fixes it
([#169](https://github.com/billylokhl/pdf-redaction-verifier/issues/169)).

## Matching limits

These are not storage places but ways a value can be split so that no
matcher sees it whole. Each is ✗:

- A value split around other text into more than three pieces, or with
  its pieces more than five lines apart (see the review net below).
- A name written in a form the rules do not list: a reordered name
  ("TESTPERSON, JORDAN Q" against "Jordan Q. Testperson") or a fragment
  of one, such as a first name alone inside an email address. A value
  rule is matched whole after normalization (case, punctuation, spacing);
  the tool generates no reorderings and does not look for parts of a
  listed value. Not detected unless each form is listed as its own value
  (see README, Rules file).
- A letter-spaced value in fine print that the page's reading breaks into
  one-character lines around other text — more than three pieces
  (`match.letter-spaced-stack`).
- A value wrapped over two fine-print lines closer together than the
  line tolerance, set by the page's larger glyphs, which merges them into
  one interleaved line (`match.tight-fine-print`). OCR, where it runs,
  flags this one (exit `2`); it does not catch the letter-spaced stack.
- A page-break split whose halves are written in different directions,
  one horizontal and one vertical: no reading joins text of two
  directions (`match.mixed-direction-split`).
- Pattern-class numbers written without dashes (bare or space-separated)
  and wrapped at a line or page break.
- A pattern-rule value split at a page break inside form boxes, a
  one-character-per-line stack or rotated text, over four pages, or where
  the split needs more than the last line before the break, or with
  mixed separators —
  no reading joins it in a form the pattern accepts, so without a value
  rule for the same number it passes silently.
- Text placed at extreme coordinates (around 10⁹ points and beyond), which
  PyMuPDF does not return.
- A one-character-per-line stack in a scanned page (pixels only) that
  Vision does not report as a line, searched with a value rule ([#81](https://github.com/billylokhl/pdf-redaction-verifier/issues/81)).
  With text on the page, the Text layer reads the stack (manual review).
  In pixels only, OCR on macOS 27 reads a stack as one sideways line,
  usually misread; OCR now unstacks such a line, laying its glyphs out
  as one line and reading that, so the value is manual review
  (`layout.scanned-vertical-stack`, [#61](https://github.com/billylokhl/pdf-redaction-verifier/issues/61)). In 25 of 48 generated stack
  pages, though, Vision reported no vertical line, only some of the
  single glyphs or nothing, and those stacks still pass as clean. On
  macOS 26 (CI's runner) `layout.scanned-vertical-stack` itself still
  passes as clean; what Vision returns there was not looked at.

A value split around other text is read at the manual-review tier
(`match.page-break-furniture`, `match.columns`): the Text layer's review
net assembles a known value from two or three pieces of different lines
within five consecutive lines, in any order — the end of one line,
optionally a whole line, the start of another — so a value split across
a page break with a header, footer or page number between its halves, a
value wrapped inside one column of a two-column page, or one whose
halves come in reverse order is a manual-review warning (exit `2`),
never a finding (owner decision, 2026-10-11). Phase 5's geometry
matching remains the fix for the hard tier. Five lines allow a header
and a footer between the halves of a value in three pieces; the bound
keeps unrelated lines of a page from being pooled into a value. Cost,
measured with the Text layer alone: 3 of 6,000 files of a fabricated
fuzz of clean off-page layouts and none of a 2,072-file corpus of real
PDFs gain a review warning.

One page-break split is read at the manual-review tier only
(`match.page-break-off-page-half`): a value one of whose halves lies
wholly off the page — the first half below or beside page 1's last line,
or the second half above the next page's top edge. Text wholly off a
page reads like a slug, which does not run on from or into the visible
text, so the join is a manual-review warning (exit `2`), not a finding.
A first half that is the visible text's last line, running on past the
edge, is a hard page seam.

Before #68, on a rotated page whose crop box does not sit on the media
box, the old visible area misplaced some off-page text onto the page,
and some of these layouts were findings (`1`) by that accident; they
are manual review (`2`) now: a page-break split with either half wholly
off the page (owner decisions, 2026-10-10 and 2026-10-11), a value
wrapped from a visible line onto a line wholly off the page (2026-10-11),
and vertically written text split at a page break with a half off the
page, since the vertical readings across the edge feed no hard seam.

One known false alarm comes with it (`false-alarm.slug-beside-visible-line`):
a slug just past the page edge, only a word space from visible text on
its baseline ('Page 7' then 'Job 123-45'), reads as one line with that
text, exactly like a value running past the edge. If that line and the
next page's first line together form a value, the page seam makes it a
finding (`1`) on a clean document. A slug a column break away from
visible text (a page number at the margin, the slug well past the edge)
is read only as off-page text and gives manual review at most.

A value formed by visible and off-page parts is a manual-review
warning (exit `2`), never a finding and never silent, at every rotation
(owner decisions, 2026-10-11): a visible part and an off-page part a
column break apart on one line (`REVIEW_CROSS_LINE`, pinned by the
clean `page.raw-font-coded-baseline-off-page-*` cases), a value wrapped
from the last visible line onto a line wholly below the page
(`REVIEW_CROSS_LINE`), or carried from a page's last line onto text
wholly above the next page's top edge (`REVIEW_CROSS_PAGE`; both
pinned by `page.raw-font-coded-carried-off-page-*`). The same joins
flag, by design, the odd clean page whose visible text and an off-page
slug happen to form a value: review-tier noise, 3 of 6,000 files of a fabricated fuzz of clean off-page layouts (0.05%) and none of a 2,072-file corpus of real PDFs, read by the Text layer alone.

## What ⚑ costs, and what comes next

A flag is the honest answer to "found it, could not read it", but it means
a clean document that merely *contains* such content exits `2` too — on
real-world PDFs the leftover flags fire rarely (about 1.5% in a corpus of
1,224, all genuine leftover glyph-coded text from Adobe tools). The page
thumbnail flag does not: 244 of the 2,073 files in the local corpus
(11.8%) carry a `/Thumb` (1,149 pages), and 190 of them (9.2% of the
corpus) that exited `0` before now exit `2` (exact-value rules, OCR on,
macOS 27). The owner accepted this review-rate cost on 2026-10-15, with a follow-up planned to flag only thumbnails that differ from the current page. Each flag is removed
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
root, so it is not a backstop for orphaned or superseded content. The
document `/ID` elements qpdf generates in its output (they are not in the
file) are blanked before that scan whenever they can be told apart from
the file's own; the file's own `/ID` element that qpdf keeps is still
scanned, as qpdf writes it (hex).
