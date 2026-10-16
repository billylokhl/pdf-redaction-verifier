"""redaction_verifier.visible_map — the visible map, a picture of a scan.

The map pictures the scan; it is not a verdict. Three rules govern it:

- it never changes the verdict (it will be built after the report is
  out, in a child process, and nothing it does can reach the exit code);
- it is never more complete than the scan (every box answers a hard
  finding the scan already made, and anything it cannot place is listed
  as "could not be positioned", never dropped);
- it never implies "not visible" for content a reader could click, hover,
  print, open, uncrop or switch on.

Modules so far (the command-line flag that runs the map in a child
process comes later):

- sink.py:     what the scan hands over (copies of its own per-page truth);
- locate.py:   positions — text glyphs and annotation rectangles;
- plan.py:     what the map says: boxes, lists and flags, from the scan;
- headline.py: the one-line summary and the incompleteness-code table;
- latin.py:    text as the map draws it (Latin-1, escapes);
- render.py:   drawing the map, and the self-check of what was drawn;
- markers.py:  the map file's own mark (line 2 and the last line).

Nothing here is imported by a scan except sink.py, whose `offer`
verify.py calls only when a sink exists (never, today). This package
never imports verify (one-way dependency, docs/REDESIGN.md §6).
"""
