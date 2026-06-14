"""
DisclosureEngine — determines which disclosure forms are required
for a given transaction context.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .evaluator import ConditionEvaluator
from .rules import Rule, RuleSet


@dataclass
class DisclosureRequirement:
    """A single disclosure obligation that applies to a transaction."""

    rule_id: str
    title: str
    description: str
    severity: str
    form_ids: List[str]
    deadline_days: Optional[int]
    notes: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "form_ids": self.form_ids,
            "deadline_days": self.deadline_days,
            "notes": self.notes,
            "citations": self.citations,
            "category": self.category,
        }


class DisclosureEngine:
    """
    Determines which disclosure requirements apply to a transaction.

    Args:
        rule_set:  A RuleSet loaded with seed data.
        evaluator: A ConditionEvaluator instance (shared or dedicated).
    """

    def __init__(
        self,
        rule_set: RuleSet,
        evaluator: Optional[ConditionEvaluator] = None,
    ) -> None:
        self._rules = rule_set
        self._evaluator = evaluator or ConditionEvaluator()

    def get_required_disclosures(
        self,
        context: Dict[str, Any],
        as_of: Optional[date] = None,
    ) -> List[DisclosureRequirement]:
        """
        Return every disclosure requirement that applies to context.

        Context fields (all optional except 'state'):
            state           str   e.g. "CA"
            property_type   str   "single_family"|"condo"|"multi_family"|"land"|"commercial"
            year_built      int
            in_flood_zone   bool
            has_hoa         bool
            transaction_type str  "purchase"|"lease"|"exchange"
            representation  str   "seller_only"|"buyer_only"|"dual_agency"|"none"
            price           float
            is_foreclosure  bool
            is_new_build    bool
            has_well        bool
            has_septic      bool
            near_military   bool
            known_mold      bool
            death_on_premises bool
            seller_is_married bool
            in_mud          bool
            has_split_estate bool
            was_meth_lab    bool
            has_mello_roos  bool
            has_window_security_bars bool
            has_underground_tank bool
            in_nyc          bool
            has_flooded_previously bool
        """
        state = context.get("state", "").upper()
        if not state:
            raise ValueError("context must include 'state'.")

        candidate_rules = self._rules.for_state(
            state, as_of=as_of, include_federal=True
        )
        disclosure_rules = [r for r in candidate_rules if r.category == "disclosure"]

        ordered = self._dependency_sort(disclosure_rules)

        fired_ids: set = set()
        results: List[DisclosureRequirement] = []

        for rule in ordered:
            if not all(dep in fired_ids for dep in rule.dependencies):
                continue

            try:
                if not self._evaluator.evaluate(rule.trigger_conditions, context):
                    continue
            except ValueError:
                continue

            fired_ids.add(rule.rule_id)

            form_ids = [
                a.get("form_id", "")
                for a in rule.required_actions
                if "form_id" in a
            ]
            deadline_days = next(
                (a.get("deadline_days") for a in rule.required_actions
                 if "deadline_days" in a),
                None,
            )
            notes = [
                a.get("note", "")
                for a in rule.required_actions
                if "note" in a
            ]

            results.append(
                DisclosureRequirement(
                    rule_id=rule.rule_id,
                    title=rule.title,
                    description=rule.description,
                    severity=rule.severity,
                    form_ids=[f for f in form_ids if f],
                    deadline_days=deadline_days,
                    notes=[n for n in notes if n],
                    citations=rule.citations,
                    category=rule.category,
                )
            )

        return results

    def _dependency_sort(self, rules: List[Rule]) -> List[Rule]:
        """Topological sort of rules by dependency chains."""
        id_to_rule = {r.rule_id: r for r in rules}
        sorted_rules: List[Rule] = []
        visited: set = set()

        def visit(rule: Rule, ancestors: set) -> None:
            if rule.rule_id in visited:
                return
            if rule.rule_id in ancestors:
                print(
                    f"WARNING: circular dependency at rule {rule.rule_id}",
                    file=sys.stderr,
                )
                return
            for dep_id in rule.dependencies:
                dep = id_to_rule.get(dep_id)
                if dep:
                    visit(dep, ancestors | {rule.rule_id})
            sorted_rules.append(rule)
            visited.add(rule.rule_id)

        for rule in rules:
            visit(rule, set())

        return sorted_rules
