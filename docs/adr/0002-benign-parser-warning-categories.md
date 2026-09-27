# 0002. Benign parser-warning categories

Status: proposed

## Context

REDESIGN.md §4 defines parser agreement as: compare the inventory's
in-use object set and page tree against PyMuPDF and against qpdf (a
separate process) for the current revision, where "repair" means
parse-level warnings (`qpdf <file> --object-streams=disable /dev/null`
plus MuPDF warnings) -- explicitly **not** `qpdf --check`'s linearization
lint. The plan cites a measured flag rate of "~1.5%" but defers "benign
warning categories" to an ADR. This is that ADR.

**This is a substantial revision of the first version.** That version
called four qpdf warning categories benign based on qpdf's own reassuring
wording ("a common error handled correctly by qpdf and most other
applications") without independently verifying the claim, and without
checking it against REDESIGN §4's own "Ambiguity detection" bullet,
which already lists **duplicate dictionary keys** and **xref offsets not
landing on `N G obj`** as things the inventory must flag, not wave
through. Review found concrete counter-examples for two of the four
categories and an unverified assumption in a third. This version tightens
the rules, verifies each one against the file's own bytes rather than
trusting qpdf's wording, and re-measures.

## Decision

**Only two categories survive, each requiring a per-instance check --
not a blanket rule on the warning's wording:**

1. **A wrong/zero xref offset is benign only when the inventory's own
   byte scan finds no object header (`N G obj`) for that object number
   anywhere in the file.** qpdf's message ("object has offset 0 - a
   common error handled correctly...") is not sufficient on its own:
   review demonstrated a concrete case where an object's xref entry
   pointed at offset 0, qpdf treated it as null (it does **not** scan
   for the real body when the offset is exactly 0), and the object was a
   page's `/Contents` -- the page rendered blank, and the object was
   dropped entirely on rewrite. That is real data loss, not "nothing was
   lost." The corrected rule: only benign when there genuinely is no
   body anywhere for that number (nothing exists to lose); otherwise
   this is exactly REDESIGN §4's ambiguity-detection case and must flag.
2. **A duplicated dictionary key is benign only when both occurrences'
   values are textually identical.** ISO 32000 does not define a
   resolution order for a duplicate key ("keys shall be unique" -- a
   duplicate is a violation, not a resolvable case with a spec-mandated
   winner); "last occurrence overrides earlier ones" is qpdf's own
   implementation choice, and another reader is free to pick the first,
   or to error. When the two values are identical, no reader's choice
   can produce a different document, so the ambiguity is moot; when they
   differ, this is exactly REDESIGN §4's ambiguity-detection case.

**Two categories from the first version are dropped -- always flag:**

3. **"Expected endobj"** (a missing/misplaced `endobj`) is dropped. The
   first version claimed this was benign "when the next token is
   unambiguous," but that check was never actually implemented --
   `measure_corpus.py`'s regex matched the qpdf message text alone, with
   no verification of what followed. This is exactly the kind of
   ambiguity (padding between a stream's true end and the next object)
   the byte tiler exists to catch; it always flags pending a real
   per-object check in Phase 3a.
4. **"Input stream is complete but output may still be valid"** is
   dropped. This was mislabelled in the first version as a "single-object
   note" related to inline images. It is actually qpdf's warning for an
   **unterminated Flate stream** -- a truncated object or xref stream,
   the same filter-chain problem as K1/K2 (data after a stream's declared
   end, or a stream cut short). It always flags.

Both surviving checks are implemented as verified per-instance functions
in `eval/spikes/measure_corpus.py` (`_offset_warning_benign`,
`_dup_key_benign`), not a stderr-text pattern match -- when either check
cannot recover enough information to verify (the object number or key
can't be parsed, or the object's raw span can't be found), the result is
**not benign** by construction: fail closed.

**A known gap in rule 1, found on the second review: it is fail-open for
a body stored inside an object stream (`/ObjStm`).** `_offset_warning_benign`
looks for a literal `N G obj` header anywhere in the raw file bytes; an
object compressed into an `/ObjStm` (referenced via the cross-reference
stream's type-2 entries, per PDF 1.5+) has no such header at all, so the
check would wrongly conclude "no body exists anywhere" (benign) even
though the object is very much present, just compressed. **This corpus
has zero instances where that gap actually changes the verdict** (no
offset-warning object in this corpus is also an ObjStm member), but the
gap is real and the check must be extended to also search `/ObjStm`
contents before Phase 3c relies on it. Recommend: accept the gap for now
(it did not change any measured number here) and close it as part of
Phase 3c's real implementation, not this spike.

## Measurement

2,031 real files, 484 text-bearing (`eval/spikes/RESULTS.md`, full table
there; `eval/spikes/measure_corpus.py` is the script):

| | All files | Text-bearing |
| --- | --- | --- |
| qpdf raw flag rate (any warning) | 9.0% | 32.4% |
| qpdf refined flag rate (tightened categories excluded) | 2.8% | 7.0% |
| MuPDF flag rate | 0.3% | 1.4% |
| **Combined refined flag rate** | **2.9%** | **7.2%** |

The refined rate is markedly higher than the first version's reported
0.69%, because that number came from the four-category (unverified)
benign list; with only the two verified categories, roughly two-thirds
of what was called benign before is now correctly counted as a flag.
7.2% on the text-bearing stratum -- the population parser agreement
actually matters for -- is well above REDESIGN §4's original ~1.5%
estimate, and above what §5's scorecard gates ("false hard < 1%...
review rate" targets) would obviously tolerate without further work.

## Consequences

- This ADR does not close the affordability question REDESIGN §4 raised
  -- it corrects a wrong answer to it. A 7.2% review-rate contribution
  from parser agreement alone is a real cost the owner needs to weigh
  against the plan's Phase 3c gate ("Flag rate as measured in Phase 1").
- Category 1's fix requires the byte-tiler's own object-header scan to be
  available wherever parser-agreement is judged (already true in
  `inventory_lite.py`/the eventual Phase 3a inventory) -- it is not a
  qpdf-only check.
- REDESIGN §4's "Ambiguity detection" bullet (duplicate keys, `/Length`
  disagreeing with `endstream`, xref offsets not landing on `N G obj`,
  the same object defined twice) already treats these as flag-worthy at
  the *byte-tiling* level; this ADR does not contradict that -- it
  narrows only when the **parser-agreement cross-check specifically**
  (comparing qpdf/MuPDF's independent view against ours) treats an
  instance as *additional* corroborating evidence of a problem, versus
  when it is inert because provably nothing could have been lost. A
  future revision of §4 should cite this ADR next to that bullet rather
  than leave the two documents to be reconciled by inference.

- **This measurement is a proxy, not the real check.** REDESIGN §4
  defines parser agreement as comparing the *inventory's own* in-use
  object set and page tree against PyMuPDF and qpdf -- that comparison
  does not exist until Phase 3a builds the inventory. This spike measures
  "did qpdf or MuPDF warn at all" as a stand-in for it, which is not the
  same signal (a warning can fire with no effect on the object set or
  page tree PyMuPDF and the inventory would each derive, and the reverse
  is conceivable too). Phase 3c must re-measure against the real
  object-set/page-tree comparison before treating the rate below as
  settled -- this ADR's numbers size the *warning-based* proxy only.

**Deferred to Phase 3c, not fixed in this spike:** `_dup_key_benign`'s
value comparison captures only the first whitespace-delimited token
after the key, so an indirect reference like `5 0 R` and `5 1 R` (same
object number, different generation -- a genuinely different reference)
would compare equal on their captured "5" alone. This spike's measured
rate is not known to be affected (no duplicate-key case in this corpus
happened to have differing-generation indirect-reference values), but
the check itself is not sound as written and must compare the full
reference, not its first token, before Phase 3c trusts it.

## Owner confirmation needed

- Whether a 7.2% (text-bearing) / 2.9% (all-files) parser-agreement flag
  rate is affordable to ship as-is, or whether Phase 3c needs to narrow
  further (e.g. by implementing the deferred "expected endobj" per-object
  check rather than dropping the category outright) before this becomes
  a real gate -- keeping in mind the rate above is from the warning-based
  proxy, not the real object-set/page-tree comparison.
- Whether to accept the two known verification gaps (ObjStm offset
  bodies; duplicate keys compared on the first token) until Phase 3c.
  Both measured 0 instances on this corpus: no offset-warning object is
  also an `/ObjStm` member, and no duplicate-key pair is a pair of
  indirect references that differ only after the first token. Recommend: accept them now and close both
  in Phase 3c's real implementation, before that phase relies on either
  check.
