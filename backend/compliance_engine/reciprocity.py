"""
ReciprocityEngine — determines license portability between US states.

Reciprocity levels:
    full        — No additional education/exam required.
    partial     — Some requirements waived; state exam or course still needed.
    none        — Must complete full licensing requirements.
    portability — Transaction-specific cooperative agreement.

Data is current as of 2025. Always verify with the target state commission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ReciprocityRecord:
    """A reciprocity agreement between two states."""

    home_state: str
    host_state: str
    level: str
    conditions: List[str] = field(default_factory=list)
    application_url: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "home_state": self.home_state,
            "host_state": self.host_state,
            "level": self.level,
            "conditions": self.conditions,
            "application_url": self.application_url,
            "notes": self.notes,
        }


_RECIPROCITY_TABLE: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("CO", "GA"): {"level": "full",    "conditions": [],                                            "notes": "CO-GA cooperative agreement."},
    ("GA", "CO"): {"level": "full",    "conditions": [],                                            "notes": "GA-CO cooperative agreement."},
    ("CO", "AL"): {"level": "full",    "conditions": ["CO license must be active 1+ year"],         "notes": ""},
    ("CO", "AK"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("CO", "AR"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("CO", "DE"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("CO", "MS"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("CO", "OK"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("CA", "AZ"): {"level": "none",    "conditions": ["Must pass AZ exam; CA has no AZ agreement"], "notes": ""},
    ("CA", "TX"): {"level": "none",    "conditions": ["TX has no reciprocity with CA"],             "notes": ""},
    ("CA", "NV"): {"level": "partial", "conditions": ["Pass NV state portion of exam"],             "notes": ""},
    ("CA", "OR"): {"level": "none",    "conditions": [],                                            "notes": "OR offers no reciprocity."},
    ("NY", "PA"): {"level": "full",    "conditions": ["PA license active; NY exam not required"],   "notes": ""},
    ("NY", "NJ"): {"level": "partial", "conditions": ["Must pass NJ state exam"],                   "notes": ""},
    ("NY", "CT"): {"level": "partial", "conditions": ["Must pass CT state portion of exam"],        "notes": ""},
    ("TX", "AR"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("TX", "LA"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("TX", "MS"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("TX", "OK"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("TX", "NM"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("FL", "AL"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("FL", "AR"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("FL", "CT"): {"level": "partial", "conditions": ["Pass CT state law course + exam"],           "notes": ""},
    ("FL", "GA"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("FL", "IL"): {"level": "partial", "conditions": ["Pass IL state exam portion"],                "notes": ""},
    ("FL", "MS"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("FL", "NE"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("WA", "ID"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("WA", "OR"): {"level": "none",    "conditions": ["OR has no reciprocity"],                     "notes": ""},
    ("GA", "AL"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("GA", "FL"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("GA", "SC"): {"level": "full",    "conditions": [],                                            "notes": ""},
    ("AZ", "CA"): {"level": "none",    "conditions": ["CA has no reciprocity"],                     "notes": ""},
    ("AZ", "NV"): {"level": "partial", "conditions": ["Pass NV exam; AZ license active"],           "notes": ""},
    ("AZ", "TX"): {"level": "full",    "conditions": [],                                            "notes": ""},
}

_NO_RECIPROCITY_STATES = {"DE", "KY", "MI", "MN", "SD", "VT"}


class ReciprocityEngine:
    """
    Determines license portability between US states.

    Args:
        extra_records: Additional ReciprocityRecord objects to supplement
                       the built-in table.
    """

    def __init__(self, extra_records: Optional[List[ReciprocityRecord]] = None) -> None:
        self._extra: Dict[Tuple[str, str], ReciprocityRecord] = {}
        for rec in (extra_records or []):
            key = (rec.home_state.upper(), rec.host_state.upper())
            self._extra[key] = rec

    def check(self, home_state: str, host_state: str) -> ReciprocityRecord:
        """
        Return reciprocity status for agent licensed in home_state
        wanting to practice in host_state.
        """
        home = home_state.upper()
        host = host_state.upper()

        if home == host:
            return ReciprocityRecord(
                home_state=home, host_state=host, level="full",
                notes="Same state — no reciprocity needed.",
            )

        key = (home, host)
        if key in self._extra:
            return self._extra[key]

        if key in _RECIPROCITY_TABLE:
            data = _RECIPROCITY_TABLE[key]
            return ReciprocityRecord(
                home_state=home, host_state=host, level=data["level"],
                conditions=data.get("conditions", []),
                application_url=data.get("application_url", ""),
                notes=data.get("notes", ""),
            )

        if host in _NO_RECIPROCITY_STATES:
            return ReciprocityRecord(
                home_state=home, host_state=host, level="none",
                conditions=["Full licensing required — host state has no reciprocity."],
                notes=f"{host} does not offer reciprocity with any state.",
            )

        return ReciprocityRecord(
            home_state=home, host_state=host, level="none",
            conditions=["Reciprocity status unknown — contact host state commission."],
            notes="Not yet mapped. Verify with state commission.",
        )

    def reciprocal_states_for(self, home_state: str) -> List[ReciprocityRecord]:
        """Return all states where home_state has at least partial reciprocity."""
        home = home_state.upper()
        results = []
        for (h, t), data in _RECIPROCITY_TABLE.items():
            if h == home and data["level"] in ("full", "partial", "portability"):
                results.append(ReciprocityRecord(
                    home_state=home, host_state=t, level=data["level"],
                    conditions=data.get("conditions", []),
                    notes=data.get("notes", ""),
                ))
        for (h, t), rec in self._extra.items():
            if h == home and rec.level in ("full", "partial", "portability"):
                results.append(rec)
        return sorted(results, key=lambda r: r.host_state)
