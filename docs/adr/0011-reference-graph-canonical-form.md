# 0011. The reference graph: canonical form

Status: accepted (owner approval, 2026-10-04: R1-R7 all as recommended); amended 2026-10-11 (items 4, 5 and 8 and the flag reasons; Amendment below), 2026-10-12 (item 4's text rule, measured in 3a-7c) and 2026-10-14 (item 8's annotation gate, P1)

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
- **Labels** on stream units: one or more of `DRAWABLE`, `STRUCTURAL`, `ORPHANED`, and
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
  it does not find -- never through MuPDF (REDESIGN §4's reference-graph
  bullet and the 4a row record this).

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
  object 0) flags (`FlagReason.REF_DANGLING`, owner decision R1).

Name and number trees (the catalog's `/Names` subtrees and
`/PageLabels`; the catalog's own `/Dests` is a plain dictionary): each
node is a dictionary with exactly one of `/Names` (or `/Nums`), an array
of key-value pairs with keys unique and sorted by bytes (and as text; a non-empty tree in an encrypted revision flags until 3a-9: amended 2026-10-11, below), or `/Kids`, an
array of indirect references to nodes, none reached twice; every node
below the root has a correct `/Limits`. Anything else flags
(`FlagReason.NAME_TREE`) -- except an empty root, which reads as empty
(owner decision R5).
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
  chain (a cycle flags `FIELD_TREE`: amended 2026-10-11, below) -- whatever `/AP` or `/NeedAppearances` say. Any other annotation
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
  and, for a kind that has a decoder (REDESIGN's registry), that decoder
  parses the whole stream in its format; each kind keeps its decoder's
  status, and a kind with none the status listed here:
  - cross-reference and object streams: the inventory's own decoding (ADR
    0003 reasons 1-2 cover the field data and the header table; the
    members are objects);
  - the linearization hint stream -- an exception to the reachability
    condition, since nothing references it: the stream starting at the
    first byte offset `/H` gives in the canonical linearized pair's
    dictionary. No decoder: ADR 0003 keeps its payload `UNREADABLE`, so it
    is `FLAGGED`, not discharged until a hint-table decoder exists (owner
    decision R6);
  - an XMP packet under `/Metadata`: the XMP decoder, the whole stream
    well-formed XML; an image inside it (a thumbnail, K22) is an image
    nothing draws -- unused under decision D, so `FLAGGED`. Images are
    found by content, not by property name: every base64, hex, ASCII85 or
    quoted-printable run in the packet is unwrapped, as the embedded-file decoder does, and a run
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
  helper, without pushing attributes (amended 2026-10-11, below: pushed in a separate instance); a box's area is measured on its
  normalized rectangle;
- the `/EmbeddedFiles` name tree: its entries as MuPDF's
  `pdf_load_name_tree` and libqpdf's name-tree helper read them, against
  ours (canonical trees only, so the readers' differences on `/Limits`
  and duplicates never arise);
- each annotation's synthesized-or-drawn classification against
  MuPDF's own (whether the appearance it draws is the stored one), so a
  misread trigger fails the gate instead of passing review (by drawing: amended 2026-10-14, below);
- the allowlist shrinks: `PAGE_TREE_SEMANTICS`, the retyped-catalog
  exemption and (R1) `OBJECT_ZERO_REFERENCE` go, so
  any qpdf page-tree message on an unflagged file is a disagreement; for
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
| 3a-7, inventory flags (R1, R4, R5) | 141 files (6.80%) |
| Once every kind is enforced -- each from its decoder's phase, all of it when the shadow verdict ships (Phase 6), since a kind is enforced only once its decoder lands (REDESIGN §6) | about 865 (42%): 134, plus 728 of the unflagged files from the table together, plus up to 7 |
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

## Amendment (2026-10-11, the 3a-7 plan): owner decision 9

Decided with the 3a-7 plan ([phase3a-plan.md](../phase3a-plan.md), decision 9), on findings
of its review:

- **Item 4, name-tree keys (P3).** Keys must be unique and sorted as text, under both
  readers' decodings (qpdf compares UTF-8 text, MuPDF `pdf_to_text_string`), as well as by
  bytes: `(a)` and `<FEFF0061>` are distinct bytes but one name, and the readers then
  resolve it to different objects (as the gate's accessors read it, MuPDF's
  `pdf_load_name_tree` the last, pikepdf's `NameTree` iteration the first; their lookups,
  `pdf_lookup_name` and `NameTree[key]`, return the opposite entries). Expected cost 0,
  measured in 3a-7c.
- **Item 8, page attributes (P2).** pikepdf has no accessor that reads an inherited
  `/Resources` without writing it into the page (its `Page.resources` returns an empty
  dictionary for an inherited one and stores it). The gate compares against libqpdf's
  attributes pushed by pikepdf in its separate, throwaway instance -- qpdf's own
  inheritance -- rather than "without pushing attributes": a second instance opened with
  `inherit_page_attributes=True`, whose libqpdf warnings are gated like the first's.
- **Item 4, encrypted revisions (P4).** A name tree's keys are strings, encrypted until
  3a-9 decrypts them, so their order and uniqueness cannot be checked: a non-empty name tree
  in an encrypted revision flags `NAME_TREE` until 3a-9 (number-tree keys are integers,
  checked as usual). Corpus cost 0 (no encrypted file).
- **Item 5, field inheritance (P5).** A `/Parent` cycle among AcroForm fields leaves the
  inherited `/FT` and `/V`, so a synthesized appearance's input, undefined: it flags a new
  `FlagReason.FIELD_TREE`. Expected cost 0, measured in 3a-7d.

### Amendment (2026-10-12, measured in 3a-7c): owner decision 10

P3's expected cost of 0 did not hold: on the corpus it flagged 85 files
(84 otherwise unflagged). 72 of them have plain-encoded keys ending in
one NUL byte; 10 sort by bytes but not as text (9 mix UTF-16 and
plain-encoded keys); 3 hold bytes 0x7F-0xA0. The owner allowed the first
class only, on the condition that both readers resolve such a key to the
same object, which a reader-agreement test shows
(`tests/test_inventory_trees.py`): MuPDF reads the key as a C string,
the NUL cut off, libqpdf keeps it, and each finds the same object for
the key as written. A printable-ASCII key may therefore end in one NUL
byte (with a byte at or above 0x80 it flags: MuPDF keeps the NUL when it
reads the key as PDFDocEncoding and cuts it when it reads UTF-8 -- found
in 3a-7c's second and third reviews); keys must
be unique and in order under each reader's reading -- with it cut off
(MuPDF's) and with it kept (libqpdf's): with keys of mixed encodings one
does not imply the other (`(a\000)` and `<FEFF00610000>` are one name to
libqpdf, two to MuPDF); two NULs, a NUL inside, U+0000 in a UTF-16 key,
or any other control byte still flag. 3a-7c's reviews also found byte
0xAD, which MuPDF's PDFDocEncoding maps to U+0000 and qpdf does not, and
U+001B in a UTF-16 key (MuPDF strips a language escape byte-wise, ESC,
2 or 4 bytes of any value, ESC; libqpdf strips none): both flag. A
UTF-16 key is read alike only without U+0000 and U+001B. The third
review found that MuPDF reads a BOM-less key as UTF-8 whenever it parses
as such (leniently: overlong forms and surrogates included) and libqpdf
as PDFDocEncoding, so `<C3A9>` is one character to MuPDF and two to
libqpdf: a key that holds a lead byte 0xC0-0xFF followed by a
continuation byte 0x80-0xBF flags (failing closed: every UTF-8 reading
of a high byte holds one). A BOM-less key is read alike only in
printable ASCII and 0xA1-0xFF but 0xAD, and only when MuPDF cannot read
it as UTF-8. The other 13 files stay flagged. Known asymmetry, outside
the key itself: MuPDF also finds the name without its NUL (its lookup
compares C strings), libqpdf does not. Measured after: 13 files
`NAME_TREE` (12 otherwise unflagged); corpus 1,920 agree, 153 flagged
(7.38%). Item 3 (`RESOURCE_SCOPE`) stays strict (0 corpus files); 20
case-library cases whose annotation appearance streams draw only paths
and carry no `/Resources` are flagged, and the rule is revisited with
real redactor output (plan §5, Lane B) before it is enforced.

### Amendment (2026-10-14, 3a-7d): owner decision 11

**Item 8, the annotation gate's method (P1 = (a)).** MuPDF 1.28 has no
object-level signal that tells a synthesized appearance from the stored
one (`pdf_annot_ap`, before or after drawing, and
`pdf_annot_needs_resynthesis` name the stored one; the 3a-7d spike,
`eval/spikes/annotation_appearance.py`). The gate therefore compares
drawings: per revision, a copy with every page's `/Rotate` removed and
every annotation's `/F` and `/OC` cleared, against the same copy with
every annotation that has `/AP` replaced by a stand-in `/Square` with the
same `/Rect`, `/AP` and `/AS`, which MuPDF draws from `/AP`. Each
annotation is drawn alone in both, by MuPDF's trace device and as pixels
at 4x, and read as NOTHING (no mark), STORED (trace and pixels equal) or
SYNTHESIZED; ours synthesized matches SYNTHESIZED or NOTHING, ours not
synthesized STORED or NOTHING, and MuPDF's annotation list (annotations
and widgets) is compared with ours page by page as a multiset keyed by
object number -- by `/Annots` position for an annotation written
directly. Not exact: two drawings equal in trace and in 4x pixels read as
the same, so content that differs only below that resolution -- an
image's samples, a glyph program -- is not told apart. Item 5 already
flags a synthesized unit whose rich text could draw an image.

### Notes on item 5 (2026-10-15, 3a-7d; not an amendment)

Recorded with the implementation; item 5's rule is unchanged.

- **MuPDF-precise readings.** Item 5's conditions are read as MuPDF 1.28
  reads them, each pinned by a test against MuPDF's own drawing
  (`tests/test_inventory_annotations.py`):
  - `/FT` is read as written: a reference to `/Sig` is no signature
    field. `/V` and its `/Type` are resolved.
  - References are followed up to nine in a row; past that MuPDF warns
    and reads null.
  - A direct null is an absent key.
  - `/NeedAppearances` counts only as the boolean `true` (`1` does not),
    resolved.
  - `/NeedAppearances`, the signature test and the `/Parent` climb apply
    to `/Subtype /Widget` (resolved) only.
- **A flag reason, `APPEARANCE_UNSUPPORTED`** (coordinator decision,
  2026-10-15; owner informed). An annotation item 5 calls synthesized
  but MuPDF cannot synthesize is flagged:
  - a `/Subtype` outside the 18 MuPDF builds an appearance for -- absent,
    not a name, `/Movie`, `/Screen`, `/3D` and the like (issue #106);
  - a widget whose inherited `/FT` is missing, an unknown name, or a
    reference to `/Btn` (MuPDF reads `/Tx`, `/Ch` and `/Sig` through a
    reference, `/Btn` only written as a name).

  MuPDF warns "cannot create appearance stream" and draws the stored
  appearance instead -- under `/NeedAppearances`, STORED where item 5
  says synthesized -- or nothing. Corpus cost: 0 files.
- **`FIELD_TREE`** flags a `/Parent` cycle only (owner decision P5).
- **The `SYNTHESIZED` unit is a record, not a `UnitKind`.** It is a
  `Synthesized` record in `Inventory.synthesized`, per revision, rather
  than the `UnitKind` member the Consequences below name. This is
  intended for 3a: `UnitKind` members are byte-tiling kinds, and these
  units claim no bytes. Whether the ledger gives them a kind of their
  own is 3b's, which holds unit statuses -- not 3a-7e's, which adds only
  labels.

### Notes on items 5 and 8 (2026-10-15, 3a-7e-1; not an amendment)

Recorded with the implementation of reachable and `STRUCTURAL`
(`redaction_verifier/inventory/labels.py`); item 5's rule is unchanged.

- **Reachable follows `/Parent`** (coordinator decision 2026-10-15; owner
  informed). Item 4 calls `/Parent` a back edge, not a use, and item 5's
  Reachable says "every object a modeled edge reaches". Reachable follows
  every edge item 4 collects, `/Parent` included, whatever its use kind:
  both readers do (a field reached only through a widget's `/Parent` is
  kept by both writers), so Reachable is exactly the readers' reading.
- **The gate compares it** (coordinator decision Q5, 2026-10-15): the
  oracle's `Check.REACHABLE`, per revision of an unflagged file, ours
  exactly against what each reader keeps when it writes the revision with
  unused objects dropped -- MuPDF `tobytes(garbage=1)` (numbers kept; the
  written table's in-use entries) and libqpdf in QDF mode (its `%%
  Original object ID` comments; opened without pushing page attributes,
  which makes new objects; stream data and metadata left as they are). The
  3a-7e spike found both keep exactly every object a reference reaches
  from the trailer -- `/Info` and keys no reader names included -- and
  drop cross-reference and object streams, the linearization dictionary,
  the hint stream, free entries and dead bodies; and that a writer drops
  what it rewrites although its reader reads it: a stream's indirect
  `/Length` (both write it direct), the trailer's `/Encrypt` (both write
  the revision unencrypted), the trailer's `/ID` (qpdf writes a new one,
  MuPDF a new second element; two corpus files hold it by reference), and
  in qpdf a stream's `/DecodeParms` without `/Filter` -- which libqpdf
  reads as written (it is in the stream's dictionary as libqpdf reads it,
  its reference resolved, and the value comparison checks it), but its
  writer drops, since no filter uses it. The oracle adds those references back as each
  reader reads them; a target that holds references of its own makes the
  reader's set inexact there, and the check fails closed rather than
  allowing it. A reference to an object whose value is null (both writers
  drop it) already disagrees on the object's value. Known gap, failing
  closed: MuPDF's writer never writes an object stream, so an object
  stream a reference reaches (the catalog naming one, say) is in our set
  and not MuPDF's -- a disagreement on an unflagged file (no corpus or
  case-library file has one). Measured: both
  readers' sets equal ours on every unflagged revision of the corpus
  (2,080) and the case library (516).
- **An in-use `/ObjStm` home to no live compressed entry gets no label**
  (coordinator decision Q4): it is not structural by the chain, so it is
  orphaned once 3a-7e-2 labels orphans (the dead object streams the
  corpus has are in flagged files).
- **The hint stream of an updated linearized file whose `/L` is stale is
  `STRUCTURAL`** as item 5 says (coordinator decision Q8): the pair is
  canonical (owner decision 7); the measurement counts these files
  separately. The hint stream is the one `/H`'s first offset names, and
  only in a revision where nothing reachable references it (item 5's
  "nothing references it": an `/H` naming a page's content stream labels
  nothing); an overflow hint stream (`/H`'s second pair) gets no label,
  failing closed.
- **Labels' memory is bounded** (the review of #153). What a revision's
  labels keep that is new -- a reachable set, by-key records, an
  object-stream set, the revision's record; the cross-reference streams
  are one list shared by every revision -- is charged to the work budget
  at least its size in bytes, so labels keep at most one byte per unit of
  work, at most the budget's 64 per byte of the file (DESIGN.md,
  "Bounded resources"; measured 0.30 to 0.86 bytes per unit of the labels' own work, at most 52.5 bytes per byte of the file).
- **Item 5's keys only on the holders it names** (the review of #153;
  coordinator-requested, an interpretation of item 5). `/JS` only on an
  action whose `/S` is `/JavaScript`; `/EF` only on a file specification
  (`/Type /Filespec`, required with `/EF`); `/Metadata` only on a holder
  whose `/Type` is one of `Catalog`, `Pages`, `Page`, `Template`, `Font`,
  `FontDescriptor`, `EmbeddedFile`, `Filespec`, `StructTreeRoot`,
  `StructElem`, `OCG`, `XObject`, or an image or form XObject (the
  allowlist is kept as is: coordinator decision, round 2); and never on a
  holder drawn by name -- a resource-category or `/CharProcs` dictionary,
  or an appearance (`/AP`'s `/N`, `/D`, `/R`, the state dictionary `/AS`
  names entries of, and each state), written at such a place or named at
  one by any reachable reference (a Type 3 glyph, a shading or an
  appearance state named `/JS` is drawn, not a script). Any other holder:
  no label, failing closed.
- **What a `STRUCTURAL` record holds.** Its kind, the reachable object
  whose key reached it and that key (or the chain): the walk's conditions
  only. The condition that the kind's decoder parses the whole stream is
  checked where the decoder runs (3b's registry), so a record is a
  candidate until then. `/EF` and the catalog's `/OutputIntents` (the
  array and its items) may be references; `/Subtype` is read through one
  reference, and anything still unread fails closed.

### Notes on item 5 (2026-10-15, 3a-7e-2; not an amendment)

Recorded with the implementation of `DRAWABLE`, `ORPHANED` and per-body
labels (`redaction_verifier/inventory/drawable.py`, `labels.py`); item 5's
rule is unchanged.

- **A colour-space array's elements** (coordinator decision 2026-10-15;
  owner informed; measured gaps go to notes, never silent widening).
  Item 5's "every element of its array" names no role for an element.
  Each is followed as a colour space; in a dictionary or stream reached
  that way, only `/Functions` (each a function), `/Colorants` (each entry
  a colour space) and `/Process` (its `/ColorSpace`) are followed. So an
  ICC profile's `/Alternate` and `/Metadata`, and a function's other keys,
  lead nowhere drawable and fail closed.
  **Measured gap** (the review of #158; recorded, not widened): MuPDF
  draws an ICC profile's `/Alternate` whenever it cannot use the profile
  -- one it cannot read ("ignoring broken ICC profile"), or a profile
  type it rejects -- not only when the profile is broken. A Separation's
  tint function there,
  or an Indexed lookup there, is drawn by MuPDF -- its drawing changes
  with the stream -- while we label it `ORPHANED`. That fails closed (an
  orphan is `FLAGGED`), so it costs a verdict, never a miss.
- **The drawn appearance** (coordinator decision Q3).
  `Annotation.appearance` records the stream MuPDF draws for an
  annotation it does not synthesize: a stream `/AP /N`, or the stream
  that `/AS` names in an `/N` state dictionary. A stream counts only one
  reference away: MuPDF's `pdf_is_stream` follows no chain, while
  dictionaries and names (`/AP`, an `/N` dictionary, `/AS`) are read
  through up to nine references (the review of #158). An `/N` naming an
  object whose value is a reference to a stream is no stream: MuPDF
  synthesizes that annotation (3a-7d called it stored, its unit missing;
  fixed here) -- when `/AS` is absent, or names nothing in that stream's
  dictionary. When `/AS` names an entry there, MuPDF reads the stream's
  dictionary as the state dictionary: with `/AS /On` and the dictionary
  holding `/On 7 0 R`, MuPDF draws 7 (3a-7e-3, from the review of #158). A state entry so given draws nothing ("content stream is
  not a stream"): not synthesized, no appearance. The field is pinned
  against `pdf_annot_ap` with `pdf_is_stream` on every 3a-7d
  classification fixture and on chains at `/AS`, `/AP`, `/N` and a state
  entry, a signed signature field's stored appearance included; the
  classification of chains at `/N`, a state entry and `/AS`, with and
  without `/NeedAppearances`, against MuPDF's drawing. A synthesized
  annotation, and an `/AS` entry that is not a stream, have none. The
  walk starts there, so it never re-derives MuPDF's `/AS` and reference
  rules.
- **Roles decided by a value**, as MuPDF reads them (each pinned by
  MuPDF's drawing; names read through up to nine references, as MuPDF
  resolves a name).
  - An XObject is a form or an image by its `/Subtype`; on a `/Form`, a
    `/Subtype2`, when present, replaces it (MuPDF's `Do` operator:
    `/Subtype2 /Image` draws an image, `/Subtype2 /PS` nothing, "ignoring
    XObject with subtype PS"; on an `/Image` it is ignored). It is either
    only when it is a stream, since MuPDF draws no other. An XObject that
    is neither is itself `DRAWABLE` (the upper bound) and follows
    nothing.
  - A font is Type3 as MuPDF's `pdf_load_font` decides: `/Subtype
    /Type3`, or a `/Subtype` other than `Type0`, `Type1`, `MMType1` and
    `TrueType` (or none) with `/CharProcs` ("unknown font format,
    guessing type3"). Only a Type3 font follows `/CharProcs` and
    `/Resources`. A Type0 font's descendant is a CID font, never Type3.
    (The scopes' side, a guessed Type3 needing `/Resources`, is issue
    #159.)
  - A halftone is type 5 by its `/HalftoneType`.
  - A value the walk cannot read gets the narrower reading, failing
    closed: one past nine references, or a direct font or halftone inside
    an array. That is an XObject that follows nothing, a font that is not
    Type3, a halftone that is not type 5.
- **Wider than MuPDF** (the review of #158; for 3b and 4a/4b: `DRAWABLE`
  is item 5's upper bound, not "used"). MuPDF 1.28 never draws with, and
  but for `/TR` never loads: a graphics state's `/TR`, `/TR2`, `/BG`,
  `/UCR`, `/HT` (type 6 halftone streams included); a DeviceN
  `/Colorants` and `/Process`; a shading pattern's `/ExtGState`;
  `/JBIG2Globals` without `/JBIG2Decode`; a font descriptor's `/CIDSet`;
  and the `/AcroForm /DR` fonts of a `/NeedAppearances` text widget (it
  draws built-in Helvetica). The lists stay as item 5 gives them; a
  decoder or a content run must not take `DRAWABLE` for drawn.
- **How the lists are read.**
  - A direct value is read in place.
  - A function given as an array (a shading's `/Function`, `/TR`) is
    each element; an image's `/DecodeParms` array is each element; a
    graphics state's `/Font` array `[font size]` is its font.
  - `/Group` is the page's own, never inherited, as MuPDF reads it.
  - A page's inherited `/Resources` comes from the ancestor holding it.
  - `/AcroForm /DR` joins a revision's closure when any annotation in that
    revision is synthesized.
  - Glyphs, content streams and font programs are drawn as they are:
    their own keys are not followed.
- **A stream both `STRUCTURAL` and `DRAWABLE`** keeps both labels, and is
  drawable for 3b: every label's decoder runs and the worst status wins
  (the plan's 3a-7e entry).
- **Per body.** `Inventory.body_labels` holds each stream body's labels
  across the revisions it lives in, by body, never by number. A body is
  `ORPHANED` for the verdict when no revision it lives in gives it
  another label. That includes a dead body, a body no live entry names,
  and a body living only in revisions without labels (no page list).
  Per revision, `ORPHANED` is derived (`Inventory.orphaned`), not stored.
- **Found while implementing (measured on the 1,920 unflagged corpus
  files; each fails closed, none widens a list).**
  - The closure gap, as measured, is 0. MuPDF lists 5,875 streams on the
    current revisions' pages: contents, images and their soft masks, form
    XObjects, and drawn appearances. Every one is `DRAWABLE`. The case
    library and the fuzz set have no gap either. The measure does not see
    colour spaces, fonts, patterns or shadings (PyMuPDF lists none of
    their streams), so it cannot show the ICC `/Alternate` gap above.
  - Orphans in the current revisions: 294 files, 3,056 streams. Each is
    classed by the use kind of the path that first reaches it from the
    trailer:
    - thumbnails: 194 files;
    - `/PieceInfo` private data: 195;
    - unreferenced, or referenced only by objects nothing reaches: 82
      (the prototype's 82);
    - an appearance not drawn (an `/AP` state, or a widget on no page):
      24;
    - object streams that are home to no live entry now: 24. Each was
      structural in an older revision, so none is an orphan for the
      verdict;
    - `/Properties` entries holding forms or `/Contents`: 16.
      Marked-content property lists, which item 5 excludes;
    - `/AcroForm /DR` XObjects and fonts with no annotation synthesized:
      4;
    - an image's soft mask inside `/PieceInfo`: 1.
  - For the verdict, per body across revisions: 242 files, 2,513 bodies.
    In 52 of the 294 files every current orphan is labelled in an older
    revision it lives in; over the corpus that is 544 bodies in 86 files
    (item 5's "Across revisions"; decision D's stricter reading is 4b's).
  - No `/Metadata` stream is orphaned on an annotation or an ICC profile
    (corpus, case library and fuzz). This is a number for 3a-7e-3's
    briefing.

### Notes on items 5 and 7 (2026-10-15, 3a-7e-3; not an amendment)

Item 5's and item 7's rules are unchanged. The owner's cost briefing is
in [RESULTS.md](../../eval/spikes/RESULTS.md), "Phase 3a-7e".

- **Item 7's timing test** (`tests/test_inventory_stage.py`). The whole
  stage -- edges, page lists, name trees, annotations, labels -- runs
  through one seam (`build._reference_graph`). Revisions and objects are
  scaled together, n against 8n, every revision walked again: at n the
  work is within the file's limit, at 8n it would be about 64 times as
  much and the file is flagged `BUDGET_EXHAUSTED`, its newest revisions
  unlabelled. Lines executed per charged unit (walks' own charges) are
  10.3 to 10.6 against a bound of 15, not growing from n to 8n; a
  wall-clock backstop for work inside C (time per unit at 2,000 against
  16,000 objects: clean 1.23-1.33x, bound 2x; a reachable set kept as a
  list, 4.6x).
- **The cost table, re-measured** (unflagged files, 1,920 of 2,073;
  the table above counted 1,939): orphans per body 242 files (294 in the
  current revision's sense; the table's 307), hint streams 400 (416),
  XMP packets holding an image 516 (519; scanned in eval only), JavaScript
  86 (89), synthesized annotations 130 (139 over the whole corpus), output
  intents 0 (0); any of them 707 (about 728). Whole corpus once every
  kind is enforced: 860 files, 41.5% (about 865, 42%). No figure moves the
  owner's picture; orphans are lower under item 5's per-body definition.
- **Item 7's sum on the corpus.** The largest graph work is 322,346 units,
  for a sum of 6,062 live objects and edges over the file's revisions; the
  largest sum is 6,381. Units per object or edge are 18 to 71 on files
  whose sum is 500 or more, up to 243 on small files, which pay the fixed
  charges for what the labels keep.

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
  `RESOURCE_SCOPE`, `REF_GENERATION`, `NAME_TREE`, `REF_DANGLING`, and `FIELD_TREE` from the
  2026-10-11 amendment), each with an EMITTERS entry; new `UseKind`, the
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
