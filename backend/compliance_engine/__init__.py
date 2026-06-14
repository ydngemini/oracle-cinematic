"""
compliance_engine — 50-state US real estate compliance rules engine.
"""
from .engine import ComplianceEngine
from .rules import Rule, RuleSet
from .evaluator import ConditionEvaluator
from .disclosures import DisclosureEngine
from .closing import ClosingEngine
from .reciprocity import ReciprocityEngine

__all__ = [
    "ComplianceEngine", "Rule", "RuleSet",
    "ConditionEvaluator", "DisclosureEngine",
    "ClosingEngine", "ReciprocityEngine",
]
__version__ = "1.0.0"
