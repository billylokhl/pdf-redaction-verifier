# Coverage

Where a secret can be stored in a PDF, how it is encoded there, and what
this tool does about each combination. This is the map the layers are
checked against: a cell marked ✗ is a place a leaking document can still
be certified clean.

**Legend**

- ✓ **Read** — the content is searched; a match is a finding or a
  manual-review warning, per the reading layer's tier (see the README).
- ⚑ **Flagged** — the tool finds the content but cannot read it, and says
  so: a manual-review warning, so the run exits `2`, never `0`.
- ✗ **Not covered** — a secret here can pass as clean (exit `0`).
- — Not applicable.

## Where × how

| Where the content is | Plain text | Font-coded text¹ | Pixels | Container² |
| --- | --- | --- | --- | --- |
| **Live page content** — drawn on a page | ✓ Text, Objects, OCR | ✓ Text, OCR | ✓ OCR · ✗ under a box drawn over an image | — |
| **Off the page** — drawn outside the visible area | ✓ Text, Objects | ✓ Text | ✗ | — |
| **Orphaned objects** — still stored, referenced by nothing | ✓ Objects (`ORPHANED`) | ⚑ Objects | ⚑ Objects³ | — |
| **Superseded versions** — rewritten by an incremental update | ✓ Objects (`earlier revision N`)⁴ | ⚑ Objects | ⚑ Objects³ | — |
| **Document metadata** — Info dictionary, XMP | ✓ Metadata, Objects | — | — | — |
| **Orphaned XMP** | ✓ Objects | — | — | — |
| **Attachments** — listed, or attached to an annotation | ✓ Hidden (manual review) | — | ⚑ Hidden | ⚑ Hidden |
| **Orphaned attachments** | ✓ Objects (manual review) | — | ⚑ Objects | ⚑ Objects |
| **Annotation text, form fields, link targets, layer names** | ✓ Hidden, Objects | — | — | — |
| **JavaScript** | ✓ Hidden (document-level), Objects (scripts stored as strings), Binary (scripts stored as streams; manual review) | — | — | — |

¹ Fonts whose codes are not the characters shown: CID fonts with
Identity-H encoding (standard for Word, Chrome and embedded TrueType),
custom `/Differences` encodings, Type3 fonts. The Text layer applies the
font's Unicode mapping, so live text is read. For leftover content only
the raw codes are available; the tool flags them when they are visibly
not text (mostly control bytes). A font that maps *ordinary-looking* codes
to other glyphs is not detected — ✗.

² zip, Office documents, nested PDFs — anything that is not text once
decompressed.

³ Images big enough to hold legible text (at least 16×32 pixels); smaller
masks and icons are not flagged.

⁴ Each earlier revision is reopened by cutting the file at its `%%EOF`,
and every object that differs from the current version is scanned as
leftover content (up to the 50 most recent revisions).

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

## What ⚑ costs, and what comes next

A flag is the honest answer to "found it, could not read it", but it means
a clean document that merely *contains* such content exits `2` too. Each
flag is removed by teaching the tool to read that cell:

- **Font-coded leftover text** — decode through the font's Unicode
  mapping, e.g. by re-attaching the leftover stream to the page it came
  from.
- **Leftover images, and pixels under a drawn box** — OCR stored images
  directly, not only the rendered page.
- **Containers** — unpack zip and Office files, and scan nested PDFs as
  PDFs.

The Binary layer (qpdf) is a cross-check with a second PDF parser rather
than a row of its own: it sees only objects reachable from the file's
root, so it is not a backstop for orphaned or superseded content.
