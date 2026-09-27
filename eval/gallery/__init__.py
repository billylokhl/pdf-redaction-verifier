"""A static, self-contained HTML gallery generated from the case library
(``caselib.REGISTRY``): what a reader sees, the recovered secret, and
today's verdict for every leak case, plus a section for clean and
false-alarm cases.

Built at the end of Phase 0 (docs/REDESIGN.md §5's "Gallery"); it never
gates anything — see eval/README.md.

    PYTHONPATH=eval:. python -m gallery build --out DIR [--results FILE]

Nothing here is committed: the gallery is built fresh (PDFs into a
scratch directory, PNGs and HTML into ``--out``) every time.
"""

from __future__ import annotations
