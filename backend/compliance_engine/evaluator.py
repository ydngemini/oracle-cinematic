"""
ConditionEvaluator — interprets JSON trigger_conditions against a transaction context.

Condition grammar
-----------------
Comparison operators:
    {"field": "year_built", "op": "lt", "value": 1978}
    {"field": "price", "op": "gte", "value": 500000}
    {"field": "state", "op": "eq", "value": "CA"}
    {"field": "property_type", "op": "in", "value": ["condo", "townhouse"]}
    {"field": "in_flood_zone", "op": "eq", "value": true}
    {"field": "hoa_fee", "op": "exists"}

Logical combinators:
    {"all": [condition, ...]}   — AND
    {"any": [condition, ...]}   — OR
    {"not": condition}          — NOT

Empty dict {} always evaluates to True (unconditional rule).
"""

from __future__ import annotations

from typing import Any, Dict

_NUMERIC_OPS = {"lt", "lte", "gt", "gte"}
_EQUALITY_OPS = {"eq", "neq"}
_MEMBERSHIP_OPS = {"in", "not_in"}


class ConditionEvaluator:
    """
    Evaluates a JSON condition tree against a transaction context dict.

    Usage::

        evaluator = ConditionEvaluator()
        fired = evaluator.evaluate(rule.trigger_conditions, context)
    """

    def evaluate(
        self,
        condition: Dict[str, Any],
        context: Dict[str, Any],
    ) -> bool:
        """
        Recursively evaluate condition against context.

        Args:
            condition: A condition dict (see module docstring for grammar).
            context:   The transaction context dict.

        Returns:
            True if the condition is satisfied, False otherwise.

        Raises:
            ValueError: If the condition structure is unrecognised.
        """
        if not condition:
            return True

        if "all" in condition:
            return all(self.evaluate(sub, context) for sub in condition["all"])
        if "any" in condition:
            return any(self.evaluate(sub, context) for sub in condition["any"])
        if "not" in condition:
            return not self.evaluate(condition["not"], context)

        if "field" not in condition or "op" not in condition:
            raise ValueError(
                f"Unrecognised condition structure: {condition}. "
                "Expected 'field'+'op' or 'all'/'any'/'not'."
            )

        field: str = condition["field"]
        op: str = condition["op"]
        ctx_value = self._resolve_field(field, context)

        if op == "exists":
            return ctx_value is not None and ctx_value is not False and ctx_value != ""

        if "value" not in condition:
            raise ValueError(f"Condition with op='{op}' requires a 'value' key.")

        expected = condition["value"]

        if op in _EQUALITY_OPS:
            return self._compare_equality(ctx_value, op, expected)
        if op in _NUMERIC_OPS:
            return self._compare_numeric(field, ctx_value, op, expected)
        if op in _MEMBERSHIP_OPS:
            return self._compare_membership(ctx_value, op, expected)

        raise ValueError(f"Unknown operator: '{op}'.")

    @staticmethod
    def _resolve_field(field: str, context: Dict[str, Any]) -> Any:
        """Support dot-notation for nested fields."""
        parts = field.split(".")
        node: Any = context
        for part in parts:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    @staticmethod
    def _compare_equality(actual: Any, op: str, expected: Any) -> bool:
        if op == "eq":
            if isinstance(actual, str) and isinstance(expected, str):
                return actual.lower() == expected.lower()
            return actual == expected
        if op == "neq":
            if isinstance(actual, str) and isinstance(expected, str):
                return actual.lower() != expected.lower()
            return actual != expected
        return False

    @staticmethod
    def _compare_numeric(field: str, actual: Any, op: str, expected: Any) -> bool:
        try:
            a = float(actual)
            e = float(expected)
        except (TypeError, ValueError):
            raise ValueError(
                f"Numeric comparison on field '{field}' requires numeric values. "
                f"Got: actual={actual!r}, expected={expected!r}."
            )
        return {"lt": a < e, "lte": a <= e, "gt": a > e, "gte": a >= e}[op]

    @staticmethod
    def _compare_membership(actual: Any, op: str, expected: Any) -> bool:
        if not isinstance(expected, list):
            raise ValueError(
                f"Operator '{op}' requires a list 'value'. Got: {expected!r}."
            )
        normalised_expected = [
            v.lower() if isinstance(v, str) else v for v in expected
        ]
        normalised_actual = actual.lower() if isinstance(actual, str) else actual
        if op == "in":
            return normalised_actual in normalised_expected
        return normalised_actual not in normalised_expected
