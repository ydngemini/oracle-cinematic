"""
Rule model and RuleSet collection with versioning support.

A Rule is the atomic unit of compliance knowledge. RuleSets are
versioned, ordered collections that can be queried by state or date.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

SEVERITY_REQUIRED = "required"
SEVERITY_RECOMMENDED = "recommended"
SEVERITY_OPTIONAL = "optional"
SEVERITY_PROHIBITED = "prohibited"

VALID_SEVERITIES = {
    SEVERITY_REQUIRED, SEVERITY_RECOMMENDED,
    SEVERITY_OPTIONAL, SEVERITY_PROHIBITED,
}


@dataclass
class Rule:
    """
    A single compliance rule applicable to a real estate transaction.

    Attributes:
        rule_id:            Unique stable identifier, e.g. "CA-LEAD-PAINT-001".
        state_code:         Two-letter US state code or "FEDERAL".
        category:           "disclosure" | "closing" | "agency" | "tax" | "environmental"
        title:              Short human-readable label.
        description:        Plain-language explanation.
        severity:           "required" | "recommended" | "optional" | "prohibited"
        trigger_conditions: JSON dict interpreted by ConditionEvaluator. {} = always fires.
        required_actions:   List of action dicts with at least {"action"}.
        dependencies:       rule_ids that must have fired before this rule is evaluated.
        effective_date:     Date from which this rule version is active.
        expiry_date:        Date after which this rule is inactive (None = indefinite).
        version:            Integer version number.
        citations:          Legal citation strings.
        metadata:           Arbitrary extra fields.
    """

    rule_id: str
    state_code: str
    category: str
    title: str
    description: str
    severity: str
    trigger_conditions: Dict[str, Any]
    required_actions: List[Dict[str, Any]]
    dependencies: List[str] = field(default_factory=list)
    effective_date: date = field(default_factory=lambda: date(2000, 1, 1))
    expiry_date: Optional[date] = None
    version: int = 1
    citations: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"Rule {self.rule_id}: invalid severity '{self.severity}'. "
                f"Must be one of {VALID_SEVERITIES}."
            )
        self.state_code = self.state_code.upper()

    def is_active(self, as_of: Optional[date] = None) -> bool:
        """Return True if this rule is in effect on as_of (default: today)."""
        check = as_of or date.today()
        if check < self.effective_date:
            return False
        if self.expiry_date and check > self.expiry_date:
            return False
        return True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Rule":
        """Deserialise a rule from a plain dict (e.g. loaded from JSON)."""
        def _parse_date(val: Optional[str]) -> Optional[date]:
            if val is None:
                return None
            return date.fromisoformat(val)

        return cls(
            rule_id=data["rule_id"],
            state_code=data["state_code"],
            category=data["category"],
            title=data["title"],
            description=data["description"],
            severity=data["severity"],
            trigger_conditions=data.get("trigger_conditions", {}),
            required_actions=data.get("required_actions", []),
            dependencies=data.get("dependencies", []),
            effective_date=_parse_date(data.get("effective_date")) or date(2000, 1, 1),
            expiry_date=_parse_date(data.get("expiry_date")),
            version=data.get("version", 1),
            citations=data.get("citations", []),
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "rule_id": self.rule_id,
            "state_code": self.state_code,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "trigger_conditions": self.trigger_conditions,
            "required_actions": self.required_actions,
            "dependencies": self.dependencies,
            "effective_date": self.effective_date.isoformat(),
            "expiry_date": self.expiry_date.isoformat() if self.expiry_date else None,
            "version": self.version,
            "citations": self.citations,
            "metadata": self.metadata,
        }


class RuleSet:
    """
    An ordered, versioned collection of Rules.

    Supports loading from JSON seed files, filtering by state/category/date,
    rule lookup by rule_id, and historical snapshots.
    """

    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        self._rules: List[Rule] = rules or []

    @classmethod
    def from_json_file(cls, path: Path) -> "RuleSet":
        """Load all rules from a JSON file (list of rule dicts at top level)."""
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        rules = [Rule.from_dict(r) for r in raw]
        return cls(rules)

    @classmethod
    def from_seed_directory(cls, seed_dir: Path) -> "RuleSet":
        """Load every *.json file in seed_dir and merge into one RuleSet."""
        all_rules: List[Rule] = []
        for json_file in sorted(seed_dir.glob("*.json")):
            rs = cls.from_json_file(json_file)
            all_rules.extend(rs._rules)
        return cls(all_rules)

    def add_rule(self, rule: Rule) -> None:
        self._rules.append(rule)

    def replace_rule(self, rule: Rule) -> None:
        """Replace an existing rule_id+version or append if new."""
        for i, existing in enumerate(self._rules):
            if existing.rule_id == rule.rule_id and existing.version == rule.version:
                self._rules[i] = rule
                return
        self._rules.append(rule)

    def get_by_id(self, rule_id: str, as_of: Optional[date] = None) -> Optional[Rule]:
        """Return the latest active version of a rule by its ID."""
        candidates = [
            r for r in self._rules
            if r.rule_id == rule_id and r.is_active(as_of)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.version)

    def for_state(
        self,
        state_code: str,
        as_of: Optional[date] = None,
        include_federal: bool = True,
    ) -> List[Rule]:
        """Return all active rules for a given state (plus federal rules).
        Only the highest-version active variant of each rule_id is returned."""
        code = state_code.upper()
        active: Dict[str, Rule] = {}

        for rule in self._rules:
            if rule.state_code not in (code, "FEDERAL"):
                continue
            if not include_federal and rule.state_code == "FEDERAL":
                continue
            if not rule.is_active(as_of):
                continue
            existing = active.get(rule.rule_id)
            if existing is None or rule.version > existing.version:
                active[rule.rule_id] = rule

        return list(active.values())

    def for_category(
        self,
        category: str,
        state_code: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> List[Rule]:
        """Filter by category, optionally scoped to a state."""
        results = self._rules
        if state_code:
            results = self.for_state(state_code, as_of=as_of)
        return [r for r in results if r.category == category and r.is_active(as_of)]

    def all_states(self) -> List[str]:
        """Return sorted list of distinct state codes (excluding FEDERAL)."""
        return sorted({r.state_code for r in self._rules if r.state_code != "FEDERAL"})

    def snapshot(self, as_of: date) -> "RuleSet":
        """Return a new RuleSet containing only rules active on as_of."""
        return RuleSet([r for r in self._rules if r.is_active(as_of)])

    def __len__(self) -> int:
        return len(self._rules)

    def __repr__(self) -> str:
        return f"RuleSet({len(self._rules)} rules)"
