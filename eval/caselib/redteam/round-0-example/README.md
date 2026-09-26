# round-0-example

**Status:** worked example, not a real blind round.

- **Author:** caselib maintainers
- **Date:** 2026-09-26
- **COVERAGE.md commit given to the author:** `e2e67ff`
- **No-code-access statement:** this round ships with the red-team
  machinery itself, so it is not blind — it exists solely so the loader,
  the label hash lock and the adjudication path have something to run
  against in CI. A real round is written by someone who has read
  [COVERAGE.md](../../../../COVERAGE.md) and the threat model
  (`docs/REDESIGN.md`, `docs/THREAT_MODEL.md` if present) but not
  `verify.py` itself, and that round's own README must say so in its own
  words, plus name the actual commit they were handed.

## What's here

One case, `page.redteam-round0-visible`: an SSN typed directly onto a
page next to unrelated filler text (`example.pdf`). It plants the secret
in `live.plain`, a cell COVERAGE.md already lists — not the `new.*`
placeholder namespace (see the top-level [README](../README.md)) —
because this round's job is to prove the pipeline works, not to report a
new finding.

## Files

- `example.pdf` — the committed case file.
- `labels.json` — the frozen labels: one entry per case, matching the
  schema in `eval/caselib/families/redteam.py`.
- `round.json` — round metadata plus `initial_labels_sha256` (set once,
  at this round's first commit, and never edited again) and
  `labels_sha256` (the hash `labels.json` must currently match).
