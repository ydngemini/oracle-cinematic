"""
ComplianceEngine — main facade for the 50-state real estate compliance system.

Typical usage::

    from compliance_engine import ComplianceEngine

    engine = ComplianceEngine.from_seed_directory()
    result = engine.check({
        "state": "CA",
        "property_type": "single_family",
        "year_built": 1965,
        "in_flood_zone": True,
        "has_hoa": True,
        "transaction_type": "purchase",
        "representation": "dual_agency",
        "price": 850000,
    })
    print(result.summary())
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .closing import ClosingEngine, ClosingProfile
from .disclosures import DisclosureEngine, DisclosureRequirement
from .reciprocity import ReciprocityEngine, ReciprocityRecord
from .rules import Rule, RuleSet


@dataclass
class ComplianceResult:
    """Complete compliance determination for a single transaction."""

    context: Dict[str, Any]
    disclosures: List[DisclosureRequirement]
    closing_profile: ClosingProfile
    warnings: List[str] = field(default_factory=list)
    rules_evaluated: int = 0
    as_of_date: date = field(default_factory=date.today)

    @property
    def required_disclosures(self) -> List[DisclosureRequirement]:
        return [d for d in self.disclosures if d.severity == "required"]

    @property
    def recommended_disclosures(self) -> List[DisclosureRequirement]:
        return [d for d in self.disclosures if d.severity == "recommended"]

    @property
    def required_form_ids(self) -> List[str]:
        """Deduplicated list of required form IDs across required disclosures."""
        seen: set = set()
        result = []
        for d in self.required_disclosures:
            for fid in d.form_ids:
                if fid not in seen:
                    seen.add(fid)
                    result.append(fid)
        return result

    def summary(self) -> str:
        """Return a human-readable text summary."""
        lines = [
            f"=== Compliance Check — {self.context.get('state', '??')} ===",
            f"As of: {self.as_of_date}",
            f"Property type: {self.context.get('property_type', 'unknown')}",
            f"Transaction: {self.context.get('transaction_type', 'unknown')}",
            f"Rules evaluated: {self.rules_evaluated}",
            "",
            "--- Closing Process ---",
            f"Method:         {self.closing_profile.closing_method}",
            f"Funding:        {self.closing_profile.funding_type}",
            f"Transfer tax:   {self.closing_profile.transfer_tax_payer}",
            f"Requires atty:  {self.closing_profile.requires_attorney}",
            f"Dry funding:    {self.closing_profile.is_dry_funding}",
            f"Comm. property: {self.closing_profile.is_community_property_state}",
            "",
            f"--- Required Disclosures ({len(self.required_disclosures)}) ---",
        ]
        for d in self.required_disclosures:
            lines.append(f"  [{d.rule_id}] {d.title}")
            if d.form_ids:
                lines.append(f"    Forms: {', '.join(d.form_ids)}")
            if d.deadline_days is not None:
                lines.append(f"    Deadline: {d.deadline_days} days from contract")
            for note in d.notes:
                lines.append(f"    Note: {note}")

        if self.recommended_disclosures:
            lines.append(f"\n--- Recommended Disclosures ({len(self.recommended_disclosures)}) ---")
            for d in self.recommended_disclosures:
                lines.append(f"  [{d.rule_id}] {d.title}")

        if self.warnings:
            lines.append("\n--- Warnings ---")
            for w in self.warnings:
                lines.append(f"  ! {w}")

        if self.closing_profile.warnings:
            lines.append("\n--- Closing Warnings ---")
            for w in self.closing_profile.warnings:
                lines.append(f"  ! {w}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context,
            "as_of_date": self.as_of_date.isoformat(),
            "rules_evaluated": self.rules_evaluated,
            "closing_profile": self.closing_profile.to_dict(),
            "disclosures": [d.to_dict() for d in self.disclosures],
            "required_form_ids": self.required_form_ids,
            "warnings": self.warnings,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class ComplianceEngine:
    """
    Facade that orchestrates disclosure, closing, and reciprocity engines.

    Args:
        rule_set:           Populated RuleSet.
        closing_engine:     ClosingEngine (default: new instance).
        reciprocity_engine: ReciprocityEngine (default: new instance).
    """

    def __init__(
        self,
        rule_set: RuleSet,
        closing_engine: Optional[ClosingEngine] = None,
        reciprocity_engine: Optional[ReciprocityEngine] = None,
    ) -> None:
        self._rules = rule_set
        self._disclosure_engine = DisclosureEngine(rule_set)
        self._closing_engine = closing_engine or ClosingEngine()
        self._reciprocity_engine = reciprocity_engine or ReciprocityEngine()

    @classmethod
    def from_seed_directory(cls, seed_dir: Optional[Path] = None) -> "ComplianceEngine":
        """
        Create a ComplianceEngine pre-loaded with all seed JSON rule files.

        Args:
            seed_dir: Path to seed_data/. Defaults to compliance_engine/seed_data/
                      relative to this file's location.
        """
        if seed_dir is None:
            seed_dir = Path(__file__).parent / "seed_data"
        rule_set = RuleSet.from_seed_directory(seed_dir)
        return cls(rule_set)

    def check(
        self,
        context: Dict[str, Any],
        as_of: Optional[date] = None,
    ) -> ComplianceResult:
        """
        Run a full compliance check for the given transaction context.

        Args:
            context: Transaction context dict. Required key: "state".
            as_of:   Reference date for active-rule filtering (default: today).

        Returns:
            ComplianceResult with disclosures, closing profile, and warnings.
        """
        check_date = as_of or date.today()
        state = context.get("state", "").upper()
        candidate_rules = self._rules.for_state(state, as_of=check_date)

        disclosures = self._disclosure_engine.get_required_disclosures(
            context, as_of=check_date
        )
        closing_profile = self._closing_engine.get_closing_profile(context)

        warnings: List[str] = []

        rep = context.get("representation", "")
        if rep == "dual_agency":
            warnings.append(
                "Dual agency detected. Verify state-specific dual agency consent "
                "forms and disclosure obligations."
            )
            if state == "MD":
                warnings.append(
                    "MD: Dual agency requires written consent from ALL parties "
                    "(MD Real Property Article §17-530)."
                )

        if closing_profile.is_community_property_state:
            warnings.append(
                "Community property state: both spouses may need to sign "
                "transfer documents regardless of who is on title."
            )

        price = context.get("price", 0)
        if price >= 3_000_000 and state in {"NY", "CA", "FL"}:
            warnings.append(
                f"High-value transaction (${price:,.0f}) — verify FinCEN Geographic "
                "Targeting Orders (GTOs) for all-cash reporting requirements."
            )

        return ComplianceResult(
            context=context,
            disclosures=disclosures,
            closing_profile=closing_profile,
            warnings=warnings,
            rules_evaluated=len(candidate_rules),
            as_of_date=check_date,
        )

    def check_reciprocity(self, home_state: str, host_state: str) -> ReciprocityRecord:
        """Check license portability from home_state to host_state."""
        return self._reciprocity_engine.check(home_state, host_state)

    def list_states_with_rules(self) -> List[str]:
        """Return sorted list of state codes that have seed rules loaded."""
        return self._rules.all_states()

    def get_rules_for_state(
        self,
        state_code: str,
        as_of: Optional[date] = None,
        category: Optional[str] = None,
    ) -> List[Rule]:
        """Return all active rules for a state, optionally filtered by category."""
        rules = self._rules.for_state(state_code, as_of=as_of)
        if category:
            rules = [r for r in rules if r.category == category]
        return rules

    @property
    def rule_set(self) -> RuleSet:
        """Direct access to the underlying RuleSet."""
        return self._rules
