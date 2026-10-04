# 0011. The reference graph: canonical form

Status: accepted (owner approval, 2026-10-04: R1-R7 all as recommended)

## Context

[ADR 0010](0010-readers-are-the-authority.md) item 6 requires the
reference graph's canonical-form rules in writing before its PR (3a-7,
[phase3a-plan.md](../phase3a-plan.md)): "an edge kind we do not model
makes its target an orphan, `FLAGGED`; where readers' resource
inheritance differs, decode under each and flag". Phase 3a runs no
content, so "decode under each" is not available yet: here, where the
readers' inheritance would differ, the graph flags; running content
under each scope is 4a's.

The graph is what content decoding needs (REDESIGN §4): per revision,
which object uses which, the page tree with its inherited attributes,
and the resource scope each content stream, form, pattern, Type3 glyph
and annotation appearance is read under. Under ADR 0010 the readers
decide how a file reads, so both were studied in their source (MuPDF
1.28.2; qpdf and libqpdf 12.4.2) and with fabricated files, by one
research pass and an independent fact-check:

| Shape | MuPDF | qpdf |
| --- | --- | --- |
| Page vs. page-tree node | by `/Type` (its fallback lookup: an untyped node with `/Kids` and no `/MediaBox` is a node) | by the presence of `/Kids`, then `/Type` rewritten to match |
| Page count | the root's `/Count`; a count that is low or missing hides pages (a high one is corrected once the tree loads) | walks the tree; `--show-npages` prints the root's `/Count` |
| A `/Kids` entry that is null, dangling or not a dictionary | a blank page or an error in that slot; a later page lost | skips it -- unless an inheritable attribute sits above it: then libqpdf fails and `--check` exits 2 |
| A page listed twice, or under two nodes | the same object twice | a copy with a new object number (dropped instead if the xref was reconstructed) |
| A cycle in the page tree; more than 100 `/Pages` levels | renders | fatal |
| `N G R` whose generation is not the object's | resolves by number | null, without a warning |
| An inherited attribute that is a reference to null or to nothing | stops there: no value | treats it as absent and inherits from above |
| An inherited `/MediaBox` that is not 4 numbers, or `/Resources` not a dictionary | takes what it can (a 5-number box: its first 4; an invalid box: 612x792) | repairs to letter size or `<< >>`, with a warning -- only when no valid value sits anywhere above; otherwise returns the invalid value |
| `/Parent` not the node that lists the page, missing, or cyclic | inherits along `/Parent` (repairing a cycle) | inherits along `/Parent` by default, along the traversal path when it pushes attributes |
| No `/Resources` or `/MediaBox` anywhere above a page | none; 612x792 | repairs: `<< >>`, letter size, with a warning |
| `/ArtBox`, `/TrimBox`, `/BleedBox` on a page-tree node | inherited (outside the spec) | not inherited |
| A form, tiling pattern or annotation appearance without its own `/Resources` | uses the caller's (the page's, the enclosing form's) | runs no content (its resource cleanup assumes the page's) |
| A name missing from the innermost `/Resources` | falls through to enclosing forms and the page; an unresolved font becomes a substitute font, without a warning | runs no content |
| A Type3 font without `/Resources` | the resources current when the font was first loaded (cached) | runs no content |
| An annotation without an appearance MuPDF can use (not a Link or Popup): `/AP` absent or empty, only `/D`, `/N` neither a stream nor a dictionary, or a dictionary whose `/AS` is missing or names no entry (an entry that is not a stream draws nothing, unsynthesized); a widget under `/NeedAppearances true` (its stored `/AP` is replaced); an unsigned signature field (`/V` not a dictionary) | builds and shows an appearance from the annotation's values: a field's `/V` (a string, a name, or a stream's text, inherited through the field's `/Parent`), a text field's rich text `/RV` (which overrides `/V`), a FreeText's rich text `/RC` (which overrides `/Contents`), `/Contents`; `/DS` is a style sheet. Rich text is laid out as HTML, character references decoded -- so the shown text can be in no string of the file -- and an `<img>` with a data URI is drawn as an image | shows nothing unless asked to generate appearances |
| Hidden or NoView annotations, Popups, optional content OFF | not drawn | no rendering (its flattening honours the annotation flags) |
| A `/Contents` array with a null or dangling item; with a dictionary or integer item | skips them | fails (exit 2); skips them, with a warning |
| A string split across two `/Contents` items | joined with a space ("SPL IT") | joined with a newline |
| A catalog without `/Type /Catalog` | reads it | warns |
| An empty name-tree root (neither `/Names` nor `/Kids`) | no entries | no entries (a warning for the trees `--check` walks: `/EmbeddedFiles`, `/Dests`, `/PageLabels`) |
| A name-tree child whose `/Limits` is wrong; duplicate keys; unsorted keys | `pdf_load_name_tree` ignores `/Limits` and keeps the last of duplicates; `pdf_lookup_name` falls back to a linear search (PyMuPDF's embedded-file listing reads only the root's `/Names`) | lists the entry without a warning, a lookup warns and still finds it (`--check` looks names up only under `/Dests`); keeps one of duplicates, with a warning; warns on unsorted keys |
| `/Templates`, `/JavaScript` name trees | not read (`/JavaScript` only with JavaScript enabled, which PyMuPDF does not do) | not read by `--check` |

Measured on the owner's corpus (2,073 macOS system and application PDFs,
counts only), over every revision of the 1,939 files the 3a-6 gate
leaves unflagged (2,118 revisions, 8,030 pages in the current ones):

- Page trees: **0** files with any page-tree shape above, at any
  revision; no tree is 10 levels deep. Inherited values: **0** pages with
  `/Resources` not a dictionary, a box that is not 4 numbers or has zero
  area, `/Rotate` not a multiple of 90, or a `/CropBox` outside the
  `/MediaBox` (a fabricated control shows the scan sees each).
- Resource scopes: **0** without their own `/Resources`, of 3,005 form
  XObjects (265 files), 42 tiling patterns, 2 Type3 fonts and 312
  appearance streams in the current revisions (older revisions: 448, 18,
  0 and 223 more, also 0).
- References: **0** generation mismatches; **6** files with one
  reference to object 0 each; **1** file with 3,417 references to numbers
  with no object.
- Synthesized appearances: **139** files, 400 annotations in the current
  revisions: 376 widgets with no `/AP` (115 files) and 24 unsigned
  signature fields (none under `/NeedAppearances`); older revisions add
  13 widgets in 8 files, none of them new.
- `/Contents` arrays: 70 files, none with a bad item. Optional content:
  147 files. Page thumbnails: 202. Name trees: **19** files with 22 empty
  roots (15 `/JavaScript`, 6 `/Templates`, 1 `/EmbeddedFiles`).
- Stream labels (item 5), from a prototype of the walk over the current
  revisions (deterministic; checked by an independent re-implementation):
  of 24,579 streams, 18,336 may be drawn, 1,836 are structural (498
  cross-reference and object streams, 416 linearization hint streams --
  one per file that has one -- 732 XMP packets, 190 JavaScript streams,
  no output-intent profiles), and 4,407 in **307 files** are orphaned:
  1,322 referenced by nothing (82 files), 1,013 page thumbnails (202),
  1,104 a design application's private data under `/PieceInfo` (203), and
  968 others (223), which may include gaps in the prototype's closure that
  the implementation closes and re-measures.
- qpdf's own check of the 416 hint streams' tables: 154 clean, 182 with
  linearization lint, and 80 in updated files qpdf no longer calls
  linearized (their `/L` is stale).

## Decision

### 1. Scope of 3a-7

Per revision, the inventory builds:

- **Edges** from every live object and the revision's trailer: each
  reference in a value, labelled with a use kind (item 4), resolved
  against that revision's object map.
- **The page tree** from the catalog's `/Pages`, in order -- only from a
  tree of the canonical shape (item 2).
- **Page attributes:** each page's `/Resources`, `/MediaBox`,
  `/CropBox` and `/Rotate`, nearest ancestor first, pushed down the tree
  once (item 2).
- **Resource scopes** for pages, forms, tiling patterns, Type3 fonts and
  annotation appearances (item 3).
- **Labels** on stream units: `DRAWABLE`, `STRUCTURAL` or `ORPHANED`, and
  `SYNTHESIZED` annotation units (item 5).

It runs no content: which XObjects a page draws (`Do`), names resolved
inside content, optional-content and annotation visibility and decision
D's geometry ([ADR 0004](0004-image-ocr-envelope.md)) are 4a's and 4b's;
item 6 lists what they inherit from here. Like the rest of the
inventory it never raises on input bytes, walks iteratively with a
seen-set ([ADR 0006](0006-recursion-and-decode-budget.md)), and is
charged to the required `Budget` (item 7).

### 2. The canonical page tree; anything else flags

The page tree reads the same in both readers only in this shape, so the
graph accepts exactly this and flags the rest (new `FlagReason.PAGE_TREE`
with a closed `PageTreeRule` parameter, like `NumberRule`):

- `/Root` is an indirect reference to a dictionary with `/Type
  /Catalog`, whose `/Pages` is an indirect reference to the root node,
  which has `/Type /Pages`, a `/Kids` array and no `/Parent` (a page as
  the root reads as 0 pages in MuPDF and fails in qpdf).
- Every node is a dictionary reached by exactly one indirect reference
  in exactly one `/Kids` array: no null, dangling, direct or
  non-dictionary kid, no node reached twice (which covers cycles and a
  page listed twice). `/Kids`, `/Count` and `/Type` may be indirect
  (both readers resolve them); a key whose value is a direct `null` is
  absent (both readers).
- A node with `/Kids` has `/Type /Pages`, `/Kids` an array and an
  integer `/Count` equal to the number of pages below it; a node without
  `/Kids` has `/Type /Page`.
- Every node's `/Parent` is an indirect reference to the node whose
  `/Kids` lists it.
- At most 100 `/Pages` levels, the root counted (qpdf's limit).
- Page attributes: the nearest of the page and its ancestors that has
  the key, and that value is not a reference to null or to a number with
  no object; `/Resources` is a dictionary and `/MediaBox` four numbers,
  both found; any box is four numbers with positive area, and `/CropBox`
  overlaps `/MediaBox`; `/Rotate` an integer multiple of 90; no page-tree
  node carries `/ArtBox`, `/TrimBox` or `/BleedBox`.
- A page's `/Contents` is absent, an indirect reference to a stream, or
  an array (direct or indirect) whose every item is an indirect
  reference to a stream.

Measured cost: **0** unflagged files. qpdf's page-tree repairs then never
run on an unflagged file, so the gate's page-tree substring exemptions
(`PAGE_TREE_SEMANTICS`) and the retyped-catalog exemption are removed
(item 8).

### 3. Resource scopes

- A page's scope is its `/Resources`. A form XObject, a tiling pattern,
  a Type3 font and an annotation appearance stream must carry their own
  `/Resources` dictionary (direct, or an indirect reference to one);
  without it MuPDF borrows the caller's or a cached one, so it flags
  (`FlagReason.RESOURCE_SCOPE`). Measured cost: **0** unflagged files.
- A scope exposes only its own, innermost dictionary. MuPDF falls
  through to enclosing scopes for a name missing there, and turns an
  unresolved font into a substitute without a warning; so 4a resolves
  every name with its own lookup in the innermost scope and flags a name
  it does not find -- never through MuPDF (REDESIGN §4 and the 4a row
  record this).

### 4. Edges and use kinds

An edge is (source object, use kind, target object); the use kind is
decided by the key path in the source, from a closed set:

- **Structure:** `ROOT`, `INFO`, `ENCRYPT` (trailer); `PAGES` (`/Pages`,
  `/Kids`; `/Parent` is a back edge, not a use).
- **Drawing:** `CONTENTS`, `RESOURCES`, and per resource category
  `XOBJECT`, `FONT`, `FONT_PROGRAM` (`/FontFile*`, a Type3 font's
  `/CharProcs`), `PATTERN`, `SHADING`, `EXTGSTATE`, `SOFT_MASK` (`/SMask`
  in a graphics state or an image, an image's `/Mask`), `COLOR_SPACE`,
  `PROPERTIES`; `ANNOTATION` (`/Annots`), `APPEARANCE` (`/AP`, by state).
- **Values a reader shows or a decoder must read:** `FIELD` (`/AcroForm`
  fields and their kids, a field's `/V`), `OUTLINE`, `NAME_TREE`
  (`/Names` and its trees, `/Dests`), `ACTION` (`/OpenAction`, `/AA`,
  `/A`), `EMBEDDED_FILE` (`/EF`, `/AF`), `METADATA`, `THUMBNAIL`
  (`/Thumb`), `ALTERNATE` (`/Alternates`), `OPTIONAL_CONTENT`
  (`/OCProperties`, `/OC`), `STRUCTURE` (`/StructTreeRoot` and below),
  `PIECE_INFO`.
- **`OTHER`:** any reference under a key not listed.

A reference resolves only to a live object with the same generation in
that revision. Otherwise:

- a **generation mismatch** flags (`FlagReason.REF_GENERATION`): MuPDF
  resolves it by number, libqpdf reads null. Measured cost: 0. The
  pinned test `test_a_generation_mismatch_is_unflagged_until_the_reference_graph`
  flips to assert the flag;
- a **dangling reference** (to a free, missing or offset-0 number, or to
  object 0) is owner decision **R1**.

Name and number trees (the catalog's `/Names` subtrees and
`/PageLabels`; the catalog's own `/Dests` is a plain dictionary): each
node is a dictionary with exactly one of `/Names` (or `/Nums`), an array
of key-value pairs with keys unique and sorted by bytes, or `/Kids`, an
array of indirect references to nodes, none reached twice; every node
below the root has a correct `/Limits`. Anything else flags
(`FlagReason.NAME_TREE`) -- except an empty root, owner decision **R5**.
The rule holds for every tree, read by a reader or not: our own decoders
walk them all (the `/JavaScript` tree leads to JavaScript streams).

### 5. Labels: drawable, synthesized, structural, orphaned

The labels decide what the verdict may certify, so they come from walks
over closed lists, never from a key path or a declared type alone. A
stream may carry several labels; every label's decoder runs on it and
the worst status wins. No label discharges a unit by itself. Per
revision:

- **Reachable**: every object a modeled edge (item 4) reaches from the
  trailer.
- **`DRAWABLE`**: a stream the drawing closure reaches from the
  canonical page list. It starts at each page's `CONTENTS`, its
  `RESOURCES`, its transparency `/Group`, and the appearance MuPDF draws
  for each annotation it does not synthesize (the `SYNTHESIZED`
  conditions below). From each object it follows a closed list of keys for that
  object's role:
  - a resource dictionary: every entry of `/XObject`, `/Font`,
    `/Pattern`, `/Shading`, `/ExtGState`, `/ColorSpace` (`/Properties`
    holds optional-content dictionaries, which draw nothing);
  - a form: `/Resources`, `/Group` (and the group's `/CS`);
  - an image: `/SMask`, `/Mask`, `/ColorSpace`, `/DecodeParms
    /JBIG2Globals`;
  - a font: `/FontDescriptor` (its `/FontFile`, `/FontFile2`,
    `/FontFile3`, `/CIDSet`), `/ToUnicode`, `/Encoding` (and a CMap's
    `/UseCMap`), `/DescendantFonts`, `/CIDToGIDMap`; a Type3 font's
    `/CharProcs` and `/Resources`;
  - a colour space: every element of its array, and a DeviceN
    `/Attributes` dictionary's `/Colorants` and `/Process`;
  - a function: `/Functions`; a shading: `/Function`, `/ColorSpace`; a
    pattern: `/Resources`, `/Shading`, `/ExtGState`;
  - a graphics state: `/SMask` (its `/G`, `/TR`), `/TR`, `/TR2`, `/BG`,
    `/BG2`, `/UCR`, `/UCR2`, `/HT` (a halftone's `/TransferFunction`, and
    a type 5 halftone's every sub-halftone), `/Font`.

  The `/AcroForm /DR` resources join when any annotation is synthesized
  (they draw it). A key outside these lists leads nowhere drawable: its
  target is orphaned, failing closed (ADR 0010 item 6). `DRAWABLE` is an
  upper bound -- listed where content may draw it -- not decision D's
  "used", which needs running content (the 4a and 4b rows).
- **`SYNTHESIZED`**: each annotation MuPDF synthesizes an appearance
  for is a ledger unit of its own, starting `UNEXAMINED`. A signature
  field is synthesized exactly when it is unsigned -- signed, in MuPDF's
  test, when its `/FT` is `/Sig` and its `/V` is a dictionary whose
  `/Type` is absent or `/Sig`, both inherited through the field `/Parent`
  chain -- whatever `/AP` or `/NeedAppearances` say. Any other annotation
  is *not* synthesized only when it is a Link or a Popup, or has an `/AP
  /N` that is a stream or a dictionary in which `/AS` (references
  resolved) names an entry -- and, for a widget, the form's
  `/NeedAppearances` is not true; every other annotation is synthesized.
  (An `/AS` entry that is not a stream draws nothing: not synthesized,
  and nothing drawable.) 4a decodes the unit from MuPDF's
  per-annotation text trace: every character of its shown inputs (`/V`
  as a string, name or stream's text, inherited through the field
  `/Parent` chain; `/RV`; `/RC`; `/Contents`; rich text read by our own
  XHTML text extraction, character references decoded, styles ignored;
  `/DS` is a style sheet, not text) must appear in that trace. The unit
  can be `DECODED` only if its rich text (`/RV`, `/RC`, `/DS`) uses only
  the elements `body`, `p`, `span`, `b`, `i` and `br`; the attributes
  `style`, `dir` and namespace declarations; and the style properties
  `font`, `font-family`, `font-size`, `font-style`, `font-weight`,
  `color`, `text-align`, `text-decoration`, `vertical-align`,
  `line-height`, `letter-spacing` and `margin` -- so no `img`, no `svg`,
  no `url(` -- and MuPDF's drawing of the annotation (its display list and
  its synthesized stream) shows no image of any kind, inline, XObject or
  SVG; otherwise it is `FLAGGED`. Tests: an `<img>` data URI and an inline
  `<svg>` holding text both flag. A widget's stored `/AP` under
  `/NeedAppearances` is not drawn. Owner decision **R2**.
- **`STRUCTURAL`**, a closed list. A stream is structural only when
  reached by its key from a reachable object, its dictionary carries no
  image or form keys (`/Subtype /Image` or `/Form`, `/BBox`, `/Width`),
  and its kind's decoder (REDESIGN's registry) parses the whole stream
  in its format; each kind keeps that decoder's status:
  - cross-reference and object streams: the inventory's own decoding (ADR
    0003 reasons 1-2 cover the field data and the header table; the
    members are objects);
  - the linearization hint stream -- an exception to the reachability
    condition, since nothing references it: the stream starting at the
    first byte offset `/H` gives in the canonical linearized pair's
    dictionary. No decoder: ADR 0003 keeps its payload `UNREADABLE`, so it
    is `FLAGGED` and never discharged (owner decision **R6**);
  - an XMP packet under `/Metadata`: the XMP decoder, the whole stream
    well-formed XML; an image inside it (a thumbnail, K22) is an image
    nothing draws -- unused under decision D, so `FLAGGED`. Images are
    found by content, not by property name: every base64 or hex run in
    the packet is unwrapped, as the embedded-file decoder does, and a run
    that decodes to an image signature, or to binary nothing accounts
    for, flags (a test puts a thumbnail under a custom namespace);
  - an embedded file under a file specification's `/EF`: the
    embedded-file decoder;
  - a JavaScript stream under an action's `/JS`: no decoder yet (K29),
    so `UNEXAMINED` until one exists;
  - an output-intent profile under the catalog's `/OutputIntents`: no
    decoder yet, so `UNEXAMINED`.
- **`ORPHANED`**: every stream with no other label, whatever its declared
  type: one nothing references, one reached only through edges outside
  the closed lists (thumbnails, `/PieceInfo` private data, an annotation
  state MuPDF does not draw, a page in `/Templates`), or one reached only
  from objects that are not reachable themselves.
- **Across revisions** ([ADR 0007](0007-orphaned-content-streams.md)):
  labels are per body, so they never pass between bodies sharing an
  object number. A body is orphaned for the verdict when no revision it
  lives in gives it another label, whatever kind of stream it is.
  Decision D's stricter "drawn by the current revision" for images is
  4b's, by running content.

`ORPHANED` is a **label for the ledger, not an inventory flag**:
inventory flags mean "the readers may read this file differently" and
drive the agreement gate; an orphan reads alike in both (neither draws
it). 3b's verdict function maps every `ORPHANED` stream to `FLAGGED`
whatever its decoder reports, with a test where a `DECODED` orphan still
yields `FLAGGED` (the 3b row). Owner decision **R3**.

### 6. What 4a inherits from here

Recorded in REDESIGN's registry and 4a row: resolve names only in the
innermost scope, flagging a miss (item 3); decode each `SYNTHESIZED`
unit from MuPDF's per-annotation trace under item 5's conditions; a token or string that
runs across two `/Contents` items reads differently in the two readers
and flags; optional-content and annotation visibility and decision D's
geometry use the page attributes and labels built here.

### 7. Cost

Each live body's edges are parsed once and shared by every revision it
lives in; each revision's walk then visits only that revision's live
objects and edges, and page attributes are pushed down the tree once
(O(nodes)). The total is the sum over revisions of live objects and
edges -- linear for a file with few revisions, and for many small
updates over a large base proportional to revisions x base. It is
charged to the budget's work counter, and the walk stops at the cap: the
file is flagged `BUDGET_EXHAUSTED`, never certified (ADR 0010 item 3). A
file with many revisions (signed or repeatedly filled forms) may reach
it; the corpus averages 1.09 revisions a file. The corpus's
largest sum is measured with the graph; the timing test scales revisions
and objects together (n vs 8n each, about 64 times the work) and asserts
either that the time stays within the budget's linear bound or that the
file is flagged `BUDGET_EXHAUSTED`, as issue #52 item 4 asks of the
oracle.

### 8. The agreement gate grows with the graph

The oracle (`eval/scorecard/inventory.py`) compares, per revision, for
every file the graph leaves unflagged:

- the page list: ours against MuPDF's (`page_xref` for every page after
  forcing the tree to load, then `page_count`) and libqpdf's (the pages'
  object numbers, read from a separate libqpdf instance after the value
  comparison, since listing pages runs qpdf's repairs; a copy with a new
  object number, or an exception, is a disagreement);
- each page's attributes: ours against MuPDF's effective values (its
  inherited `/Resources`, `page.mediabox`, `page.cropbox` converted from
  MuPDF's flipped coordinates, `page.rotation`) and libqpdf's page
  helper, without pushing attributes; a box's area is measured on its
  normalized rectangle;
- the `/EmbeddedFiles` name tree: its entries as MuPDF's
  `pdf_load_name_tree` and libqpdf's name-tree helper read them, against
  ours (canonical trees only, so the readers' differences on `/Limits`
  and duplicates never arise);
- each annotation's synthesized-or-drawn classification against
  MuPDF's own (whether the appearance it draws is the stored one), so a
  misread trigger fails the gate instead of passing review;
- the allowlist shrinks: `PAGE_TREE_SEMANTICS`, the retyped-catalog
  exemption and (with R1 as recommended) `OBJECT_ZERO_REFERENCE` go, so
  any qpdf page-tree message on an unflagged file is a disagreement; with
  R5, anchored entries are added for qpdf's empty-name-tree-root warnings. This
  closes issue #52 items 1 and 5 and part of 2 (the unanchored
  content-stream substring stays, Phase 4's), and #44's page-tree item; qpdf's
  "unexpected xref entry type" (#44) is gated like any other message.

## What it costs, by phase

On one denominator, all 2,073 corpus files (134 flagged, 6.46%, today).
Measured on the 1,939 unflagged files (current revisions):

| Source of exit `2` | Unflagged files |
| --- | --- |
| A dangling reference (R1, inventory flag) | 7 |
| An orphaned stream (R3): thumbnails 202, `/PieceInfo` private data 203, unreferenced 82, other 223 | 307 |
| A linearization hint stream (R6, ADR 0003) | 416 |
| A reachable XMP packet holding a thumbnail image (decision D: an image nothing draws) | 519 |
| A JavaScript stream (until a decoder exists) | 89 |
| A synthesized annotation (until 4a decodes it) | 139 |
| An output-intent profile (until a decoder exists) | 0 |

| When | Exit `2` |
| --- | --- |
| 3a-7, inventory flags (R1, R4, R5 as recommended) | 141 files (6.80%) |
| Once the ledger verdict is enforced (from 4a) | about 865 (42%): 134, plus 728 of the unflagged files from the table together, plus up to 7 |
| -- after 4a decodes synthesized annotations and a JavaScript decoder exists | about 865 (42%): orphans, hint streams and XMP images alone cover the same 728 |
| -- if hint streams were not flagged (R6 (b), at best) | about 840 (40-41%) |
| -- orphans and XMP images only | about 775 (37%) |
| -- orphans alone | about 441 (21%) |

The dominant costs follow from rules already accepted -- decision D
(images nothing draws: page and XMP thumbnails) and ADR 0003 (hint
streams) -- whose cost was not measured when they were decided; this is
the first measurement. Thumbnails are a known leak shape: a preview
made before redaction keeps the original. The counts come from a
prototype over the current revisions and are re-measured on the
implementation. In 3b the ledger verdict is computed but not shipped
(REDESIGN §7); before 4a no content decoder exists, so it exits `2` on
nearly every file anyway.

## Owner decisions

Decided 2026-10-04, every one as recommended.

**R1. Dangling references: flag every one, or only where used?** Both
readers read a reference to a free or missing object as null, so most
read alike, but not on every path: a dangling `/Kids` entry, `/Root` or
inherited attribute reads differently (table). The plan already commits
3a-7 to flag a reference to a free or missing object; flagging only the
used ones would amend it. Measured cost of flagging every one: **7
files (+0.34 points)**. *Recommendation: flag every one*
(`FlagReason.REF_DANGLING`); the `OBJECT_ZERO_REFERENCE` allowlist entry
and the oracle's dangling-reference normalization (#52 item 1) go,
instead of growing.

**R2. Annotations MuPDF synthesizes.** MuPDF shows text it builds from
an annotation's values -- with rich text, text no string in the file
holds, and possibly images. Options: (a) each becomes a `SYNTHESIZED`
ledger unit, `UNEXAMINED` until 4a decodes it, and `FLAGGED` if its rich
text could draw an image (item 5), not an inventory flag; (b) flag the
file at the inventory. Measured: **139 files**, 400 annotations (+6.7
points under (b)). *Recommendation: (a)*: the appearance is a reader's
reading, not an ambiguity between the readers, and a unit that starts
`UNEXAMINED` cannot be certified until it is decoded.

**R3. Orphans are a ledger label, not an inventory flag**, with 3b's
verdict mapping every `ORPHANED` stream to `FLAGGED` (item 5). This
applies ADR 0007 ("always `FLAGGED`") and decision D to every stream,
by walk. It adds no inventory flag, but once the ledger verdict is
enforced, **307 of the 1,939 unflagged files (15.8%)** exit `2` for an
orphan (ADR 0007 had estimated 11.4% of text-bearing files for content
streams alone). The largest classes are page thumbnails (202 files) and
a design application's private data under `/PieceInfo` (203 files),
which can hold an unredacted copy of a document's artwork.
*Recommendation: yes*, with the rate re-measured on the implementation
and brought back before the verdict is enforced (ADR 0010: measure
before enforcing).

**R4. Strict canonical page tree, page attributes and resource scopes**
(items 2 and 3): every deviation flags, no allowlist. Measured cost:
**0** files. *Recommendation: yes.*

**R5. An empty name-tree root.** A root with neither `/Names` nor
`/Kids` reads as empty in both readers where they read the tree
(`/EmbeddedFiles`, `/Dests`, `/PageLabels`: MuPDF lists nothing, qpdf
lists nothing and warns); neither reads `/Templates` or `/JavaScript`,
where our own decoders then find nothing. Options: accept it as empty,
with anchored allowlist entries for qpdf's warnings and a
reader-agreement test per tree kind; or flag it. Measured: **19 files**,
22 roots (15 `/JavaScript`, 6 `/Templates`, 1 `/EmbeddedFiles`; +0.92
points if flagged). *Recommendation: accept.*

**R6. The linearization hint stream** (one in every linearized file).
[ADR 0003](0003-not-applicable-reasons.md) (accepted) keeps its payload
`UNREADABLE`, so `FLAGGED` once the ledger is enforced: **416 of the
1,939 unflagged files (21.5%)**, not measured when ADR 0003 was decided. Options: (a) keep ADR 0003 as it
stands; (b) amend it later: a hint stream is `DECODED` when our own
hint-table decoder reads every byte of it and qpdf's linearization check
passes cleanly -- 154 of the 416 files would qualify (182 have
hint-table lint, 80 are updated files whose linearization is stale).
*Recommendation: (a) now*; (b) needs a decoder that does not exist, and
can be decided with the decoders (3c/4), measured then.

**R7. Images inside XMP packets** (thumbnails, K22). An image a packet
holds is drawn by nothing, so decision D makes it unused and `FLAGGED`:
**519 of the 1,939 unflagged files (26.8%)**, the largest single cost
here, likewise unmeasured when D was decided. Options: (a) apply D as
accepted; (b) decode and OCR them like drawn images (4b), certifying a
thumbnail whose text is clean. *Recommendation: (a)*: a thumbnail made
before redaction keeps the original, and nothing a reader shows tells
the user it is there.

## Consequences

- New closed flag reasons (`PAGE_TREE` with `PageTreeRule`,
  `RESOURCE_SCOPE`, `REF_GENERATION`, `NAME_TREE`, and with R1
  `REF_DANGLING`), each with an EMITTERS entry; new `UseKind`, the
  `DRAWABLE`/`STRUCTURAL`/`ORPHANED` labels and a `SYNTHESIZED` unit kind;
  `Inventory` gains per-revision page lists, page attributes, scopes and
  edges.
- REDESIGN changes with this note: the registry gains the synthesized
  appearance and names a decoder (or its absence) for each structural
  kind; the 3b row gains the orphan-to-`FLAGGED` mapping and its test;
  the 4a row gains the innermost-scope name rule, synthesized units and
  split `/Contents` tokens.
- The costs, by phase, are in "What it costs" above; each is re-measured
  on the implemented graph before merge.
- Left to later phases: running content (`Do`, names, split tokens),
  visibility and geometry (4a/4b), decoding synthesized appearances (4a),
  the orphan overlap with today's exit `2` (3a-12).
