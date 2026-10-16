"""redaction_verifier.matching.policy — which rules decide the verdict.

[ADR 0012](../../docs/adr/0012-pattern-classes-inform.md) (accepted
2026-10-15): a built-in pattern class (`ssn`, `credit-card`, `email`,
`us-phone`) is a guess the tool supplies, not a secret the user listed, so
by default its matches are informational notices that never change the
exit code. A value rule, and a user-written `pattern` rule, always decide
the verdict. Strict mode (`--strict-classes`, or `"strict": true` on a
class rule) keeps a class's matches as findings and review warnings,
exactly as before ADR 0012.

Pure: it maps rule names and the kinds the rules loader recorded for them
(`RuleLabel.kind`) to the set of informational rule names; the report
model routes those rules' findings and warnings to its notices.
"""

from __future__ import annotations

from typing import Collection, Mapping

# The rule kinds that are built-in classes: "class" in this tool's own
# rules format. A redactor config's entity_types are not rules at all
# (owner decision 2026-10-16): they are never read as classes, so neither
# this policy nor --strict-classes ever applies to them.
CLASS_KINDS: frozenset[str] = frozenset({"class"})


def informational_rules(
    kinds: Mapping[str, str], *, strict: bool, strict_rules: Collection[str] = ()
) -> frozenset[str]:
    """The names of the rules whose matches are notices, not verdict.

    *kinds* maps each rule's name to its kind. With *strict* every rule
    decides the verdict; otherwise every built-in class rule is
    informational except those named in *strict_rules*.
    """
    if strict:
        return frozenset()
    keep = frozenset(strict_rules)
    return frozenset(name for name, kind in kinds.items()
                     if kind in CLASS_KINDS and name not in keep)
