"""The real-redactor set (docs/REDESIGN.md §5, real-redactor tier): fabricated
documents mirroring real document types, generated locally, run through a
redaction tool, and later judged by an oracle independent of the verifier.

Nothing here imports the verifier (``redaction_verifier`` or ``verify``):
tests/test_redactor_set.py enforces it. Everything generated lives under the
gitignored ``eval/scorecard/redactor_set_local/``; see eval/README.md, "The
real-redactor set".
"""
