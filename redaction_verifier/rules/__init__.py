"""redaction_verifier.rules — loading and validating verification rules.

Moved verbatim from verify.py as Phase 2, step 1 ("Move") of
docs/REDESIGN.md: the redactor-config mapping tables, the shared rule
builders, RuleSet and the JSON/YAML loaders. Behaviour is byte-identical
to the code this replaced.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from redaction_verifier.matching import BUILTIN_PATTERN_CLASSES, PatternRule, normalize_string
from redaction_verifier.model import Secret, VerifyError, Warn, WarnList

# Upstream (the redactor's) entity types this tool's rules format can be
# asked to verify. Kept separate from ENTITY_TYPE_TO_CLASS below: an entity
# type can be a real upstream type with no regex equivalent here yet.
UPSTREAM_ENTITY_TYPES: frozenset[str] = frozenset({
    "person_name", "ssn", "email", "phone", "address", "date_of_birth",
    "account_number", "credit_card", "drivers_license", "passport",
})

# The redactor's entity types that have a regex equivalent here. The value is
# one of THIS tool's built-in classes: verification deliberately uses its
# own regexes and validators rather than importing the redactor's, so a
# flaw in the redactor's detection cannot hide itself from the check.
ENTITY_TYPE_TO_CLASS: dict[str, str] = {
    "ssn": "ssn",
    "email": "email",
    "phone": "us-phone",
    "credit_card": "credit-card",
}

# Mapped types whose class covers only PART of what the redactor means by
# that name. Having a regex for a name is not the same as covering the
# name, so these raise the same scope warning an unmapped type does.
PARTIAL_ENTITY_COVERAGE: dict[str, str] = {
    "phone": "only North American (NANP) numbers are checked; "
             "international formats are not",
}

# Top-level keys the redactor itself understands. Anything else is a typo or
# an upstream addition; either way the section it names is not scanned, so
# it is surfaced rather than silently dropped.
UPSTREAM_CONFIG_KEYS: frozenset[str] = frozenset({
    "entity_types", "exact_values", "patterns",
    "backend", "model", "llm_url", "ollama_url", "scrub_metadata",
})

# These tables are edited for different reasons and live far apart; an
# unguarded subscript would turn a rename into a KeyError that exits 1,
# this tool's code for "secret detected".
assert set(ENTITY_TYPE_TO_CLASS) <= UPSTREAM_ENTITY_TYPES
assert set(ENTITY_TYPE_TO_CLASS.values()) <= set(BUILTIN_PATTERN_CLASSES)
assert set(PARTIAL_ENTITY_COVERAGE) <= set(ENTITY_TYPE_TO_CLASS)


def _make_value_rule(name: str, spec: str, label: str = "") -> Secret:
    """Build a value rule, shared by both rule formats so the guards
    cannot drift apart (they already did once: the JSON path rejected a
    non-string spec while the YAML path coerced it, which is how YAML
    implicit typing silently changed what was searched for)."""
    normalized = normalize_string(spec)
    if not normalized:
        raise VerifyError(
            f"{label or name} normalizes to an empty string — it would "
            "match everything or nothing; refusing to scan"
        )
    return Secret(name, normalized)


def _make_pattern_rule(
    name: str, spec: str, flags: int, label: str = ""
) -> PatternRule:
    """Build a custom-regex rule, shared by both rule formats."""
    try:
        regex = re.compile(spec, flags)
    except re.error as exc:
        raise VerifyError(f"{label or name}: invalid regex: {exc}")
    if regex.match(""):
        raise VerifyError(
            f"{label or name}: pattern matches the empty string; "
            "refusing to scan"
        )
    return PatternRule(name, regex)


def _make_class_rule(
    name: str, class_name: str, label: str = ""
) -> PatternRule:
    """Build a built-in class rule, shared by both rule formats."""
    if class_name not in BUILTIN_PATTERN_CLASSES:
        raise VerifyError(
            f"{label or name}: unknown class {class_name!r} — valid "
            f"classes: {', '.join(sorted(BUILTIN_PATTERN_CLASSES))}"
        )
    regex_src, validator = BUILTIN_PATTERN_CLASSES[class_name]
    return PatternRule(name, re.compile(regex_src), validator)


@dataclass
class RuleSet:
    """Rules to scan for, plus what the source config asked for that
    cannot be scanned for at all."""

    secrets: list[Secret] = field(default_factory=list)
    patterns: list[PatternRule] = field(default_factory=list)
    # Entity types a redactor was told to remove that have no regex
    # equivalent (LLM-detected categories). Recorded, never dropped:
    # they are a hole in verification scope and must be reported.
    unverifiable: list[str] = field(default_factory=list)
    # Problems with the rules file that do not stop the scan but make a
    # clean result untrustworthy (surfaced as warnings -> exit 2).
    warnings: list[str] = field(default_factory=WarnList)

    def warn(self, code: str, layer: str, message: str, **fields: Any) -> None:
        self.warnings.append(Warn(code, layer, message, **fields))


def load_rules(rules_path: Path) -> RuleSet:
    """Load verification rules from a JSON rules file or a YAML config.

    A '.yaml'/'.yml' suffix selects the redactor's redact_config format,
    so one file can drive both redaction and verification; anything else
    is parsed as this tool's native JSON rules array.
    """
    if rules_path.suffix.lower() in (".yaml", ".yml"):
        return _load_rules_yaml(rules_path)
    return _load_rules_json(rules_path)


def _yaml_section(raw: dict, key: str) -> list:
    """Read a list-valued section, distinguishing absent from malformed.

    `raw.get(key) or []` would collapse {} and '' into an empty section
    before any type check ran, silently narrowing the scan instead of
    refusing it.
    """
    if key not in raw:
        return []
    value = raw[key]
    if value is None:           # `key:` with nothing under it
        return []
    if not isinstance(value, list):
        raise VerifyError(f"{key} must be a list, got {type(value).__name__}")
    return value


def _yaml_coercion_kind(value: Any) -> str:
    """Name the TYPE a coercing YAML loader read a scalar as — never the
    value itself.

    Used only by the RULES_UNQUOTED_VALUE warning. That warning used to
    show mask() of both the coerced value and the literal spec; since the
    coerced value is a deterministic function of the whole spec, the two
    masked tails together narrow a short secret by orders of magnitude
    (see #2). Saying only the kind ("a number", "a boolean", ...) carries
    no digit or character of the value.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):        # bool is an int subclass: check first
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return "a date"
    return f"a {type(value).__name__}"


def _load_rules_yaml(rules_path: Path) -> RuleSet:
    """Load a redactor's redact_config.yaml as verification rules.

    Mapping:
      exact_values -> value rules (normalized matching)
      patterns     -> pattern rules, compiled IGNORECASE exactly as the
                      redactor compiles them
      entity_types -> this tool's own built-in classes; types with no
                      regex equivalent, or only partial coverage, are
                      recorded so the scope gap is reported

    Keys the redactor needs but verification does not (backend, model,
    llm_url, ollama_url, scrub_metadata) are ignored.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - declared dependency
        raise VerifyError("reading a YAML config needs PyYAML: pip install pyyaml")

    # YAML's implicit typing is actively dangerous for secrets: unquoted
    # 00123456 is octal int 42798, 1.50 is float 1.5, `yes` is True.
    # Coercing those back with str() makes the tool search for a string
    # the user never wrote. Drop the coercing scalar resolvers so plain
    # scalars stay literal — but KEEP the merge resolver, or `<<: *anchor`
    # silently stops merging and whole sections vanish from the scan.
    _MERGE = "tag:yaml.org,2002:merge"

    class RawScalars(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            # PyYAML keeps the LAST of duplicate keys without complaint,
            # which silently discards an entire earlier section.
            seen_keys: set[str] = set()
            for key_node, _ in node.value:
                key = key_node.value
                if isinstance(key, str) and key in seen_keys:
                    raise VerifyError(f"duplicate key {key!r} in {rules_path}")
                if isinstance(key, str):
                    seen_keys.add(key)
            return super().construct_mapping(node, deep)

    RawScalars.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in resolvers if tag == _MERGE]
        for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    try:
        text = rules_path.read_text(encoding="utf-8")
        raw: Any = yaml.load(text, RawScalars)
        coerced: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML's message quotes the offending line — which in a rules
        # file is a secret. Report the problem and position only.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or type(exc).__name__
        raise VerifyError(
            f"Cannot read rules file {rules_path}: invalid YAML{where}: " f"{problem}"
        )
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: a non-UTF-8 config is an
        # operational error (exit 2), never the leak code.
        raise VerifyError(f"Cannot read rules file {rules_path}: {exc}")
    if not isinstance(raw, dict):
        raise VerifyError("a YAML rules file must be a mapping")

    rules = RuleSet()

    unknown_keys = sorted(set(raw) - UPSTREAM_CONFIG_KEYS)
    if unknown_keys:
        rules.warn(
            "RULES_UNKNOWN_KEY",
            "Rules",
            f"Rules: unrecognized config key(s) {', '.join(unknown_keys)} — "
            "if one is a misspelled section its rules were NOT scanned",
        )

    def literal(section: str, i: int, value: Any) -> str:
        """Every rule spec must be literal text. A non-string survived
        YAML's typing (an explicit !!int tag, or a nested mapping), which
        means the tool would search for something other than what is
        written in the file."""
        if not isinstance(value, str):
            raise VerifyError(
                f"{section}[{i}] must be a quoted string, got "
                f"{type(value).__name__} — quote it in the config"
            )
        return value

    # A redactor reading this file with a plain safe_load sees coerced
    # values and removes THOSE strings, so divergence means the two tools
    # are working from different text. The warning names only the TYPE
    # YAML coerced the value to, never a masked form of either value: the
    # coerced value is a deterministic function of the whole literal spec,
    # so showing mask() of both together narrowed a short secret by orders
    # of magnitude (#2) — the warning must not itself leak what it is
    # warning about.
    coerced_values = (coerced or {}).get("exact_values") or []
    values = _yaml_section(raw, "exact_values")
    for i, value in enumerate(values):
        spec = literal("exact_values", i, value)
        if i < len(coerced_values) and str(coerced_values[i]) != spec:
            rules.warn(
                "RULES_UNQUOTED_VALUE",
                "Rules",
                f"Rules: exact_values[{i}] is unquoted, so YAML reads it as "
                f"{_yaml_coercion_kind(coerced_values[i])} rather than the "
                "literal text written — a redactor sharing this file may "
                "have removed the wrong string. Quote the value in the "
                "config.",
            )
        rules.secrets.append(_make_value_rule(f"exact_values[{i}]", spec))

    for i, value in enumerate(_yaml_section(raw, "patterns")):
        spec = literal("patterns", i, value)
        if f"{spec} #" in text:
            rules.warn(
                "RULES_TRUNCATED_PATTERN",
                "Rules",
                f"Rules: patterns[{i}] appears to be truncated at an "
                "unquoted '#' (YAML comment) — quote the pattern",
            )
        # IGNORECASE only, matching the redactor's own _compile_patterns;
        # adding MULTILINE here would make a shared rule mean different
        # things in the two tools.
        rule = _make_pattern_rule(f"patterns[{i}]", spec, re.IGNORECASE)
        if rule.regex.groups:
            rules.warn(
                "RULES_CAPTURING_GROUP",
                "Rules",
                f"Rules: patterns[{i}] has a capturing group — the redactor "
                "removes only the group text, so this rule verifies more "
                "than it removed; manual review recommended",
            )
        rules.patterns.append(rule)

    # Upstream defaults entity_types to the FULL roster when the key is
    # absent, so treating absent as empty would certify clean a document
    # whose ten redacted categories were never searched for.
    if "entity_types" in raw:
        entity_types = _yaml_section(raw, "entity_types")
    else:
        entity_types = sorted(UPSTREAM_ENTITY_TYPES)
    for i, entity in enumerate(dict.fromkeys(str(e) for e in entity_types)):
        if entity not in UPSTREAM_ENTITY_TYPES:
            # Not echoed: a value pasted under the wrong key is a secret.
            raise VerifyError(
                f"entity_types[{i}] is not a known entity type — valid types: "
                f"{', '.join(sorted(UPSTREAM_ENTITY_TYPES))}"
            )
        mapped = ENTITY_TYPE_TO_CLASS.get(entity)
        if mapped is None:
            rules.unverifiable.append(entity)
            continue
        rules.patterns.append(_make_class_rule(f"entity_types:{entity}", mapped))
        if entity in PARTIAL_ENTITY_COVERAGE:
            rules.warn(
                "SCOPE_PARTIAL_ENTITY",
                "Rules",
                f"Scope: entity type {entity!r} is only partly verifiable — "
                f"{PARTIAL_ENTITY_COVERAGE[entity]}",
            )

    # A config of only unverifiable entity types is allowed: it scans
    # nothing, warns loudly, and exits 2 — which is the honest answer.
    if not (rules.secrets or rules.patterns or rules.unverifiable):
        raise VerifyError("the rules file contains no rules")
    return rules


def _load_rules_json(rules_path: Path) -> RuleSet:
    """Parse the rules JSON file, validating its shape.

    Each entry carries a 'name' and exactly one of:
      'value'   — a known secret string, matched via normalization;
      'pattern' — a custom regex matched against raw extracted text;
      'class'   — a built-in pattern class (see BUILTIN_PATTERN_CLASSES).

    Raises VerifyError for every operational problem so main can exit 2.
    """
    try:
        payload: Any = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # ValueError covers UnicodeDecodeError: a non-UTF-8 rules file is
        # an operational error (exit 2), never the leak code.
        raise VerifyError(f"Cannot read rules file {rules_path}: {exc}")

    if not isinstance(payload, list):
        raise VerifyError("the rules file must be a JSON array of objects")

    secrets: list[Secret] = []
    patterns: list[PatternRule] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict) or "name" not in entry:
            raise VerifyError(f"rules entry {i} must be an object with 'name'")
        name = str(entry["name"])
        # Names are rule identity throughout the pipeline; a duplicate
        # would silently overwrite another rule's hits in the report.
        if name in seen_names:
            raise VerifyError(f"rules entry {i}: duplicate rule name {name!r}")
        seen_names.add(name)
        kind_keys = [k for k in ("value", "pattern", "class") if k in entry]
        if len(kind_keys) != 1:
            raise VerifyError(
                f"rules entry {i} ({name!r}) must have exactly one of "
                f"'value', 'pattern', or 'class' (found: {kind_keys or 'none'})"
            )
        kind = kind_keys[0]
        spec = entry[kind]
        if not isinstance(spec, str):
            raise VerifyError(
                f"rules entry {i} ({name!r}): {kind!r} must be a string, "
                f"got {type(spec).__name__}"
            )

        if kind == "value":
            secrets.append(_make_value_rule(name, spec, f"secret {name!r}"))
        elif kind == "pattern":
            # MULTILINE so grep-style ^/$ anchors match per line of the
            # extracted page text instead of silently never matching.
            patterns.append(
                _make_pattern_rule(name, spec, re.MULTILINE, f"rule {name!r}")
            )
        else:
            patterns.append(_make_class_rule(name, spec, f"rule {name!r}"))

    if not secrets and not patterns:
        raise VerifyError("the rules file contains no rules")
    return RuleSet(secrets=secrets, patterns=patterns)
