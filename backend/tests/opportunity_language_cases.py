"""Fixed inputs for every opportunity_engine detector card (L05-06).
Imported by test_opportunity_language.py.

`english_outputs()` is what the detectors produced at 26e06e1, before their
sentences were routed through the en/es catalogue. Its result is frozen in
`opportunity_en_golden.json`; the test holds today's English to it byte for
byte. `outputs(lang)` calls the detectors with a language, an argument they
did not take before the change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import opportunity_engine as oe
from tenancy import Role, TenantContext

GOLDEN = Path(__file__).with_name("opportunity_en_golden.json")

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def _at(days: float, hours: float = 3) -> datetime:
    return datetime.fromtimestamp(NOW.timestamp() + days * 86400 + hours * 3600, tz=timezone.utc)


CONTRACT_ROWS = [
    {"id": "l-1", "address": "12 Oak St", "state": "DE", "contract_expires_at": _at(1),
     "dossier_status": "under_contract", "client_name": "Sarah Johnson", "client_id": "c-1"},
    {"id": "l-2", "address": "40 Elm Ave", "state": "DE", "contract_expires_at": _at(10),
     "dossier_status": None, "client_name": None, "client_id": None},
    {"id": "l-3", "address": None, "state": "PA", "contract_expires_at": _at(0),
     "dossier_status": "draft", "client_name": None, "client_id": None},
]

INTENT_ROWS = [
    {"client_id": "c-1", "full_name": "Sarah Johnson", "lead_score": 72, "stage": "active",
     "summary": None, "score_breakdown": None,
     "next_actions": [{"code": "verify-contact", "title": "Verify contact information",
                       "reason": "A valid email or phone number is required before outreach can be reviewed."}],
     "data_gaps": [{"code": "contact", "label": "No verified contact method"},
                   {"code": "source", "label": "Lead source not recorded"}]},
    {"client_id": "c-2", "full_name": "Luis Ortega", "lead_score": None, "stage": "lead",
     "summary": None, "score_breakdown": None,
     "next_actions": json.dumps([{"code": "confirm-property",
                                  "title": "Confirm seller property address or parcel"}]),
     "data_gaps": json.dumps([{"code": "property", "label": "No verified seller property"},
                              {"code": "contact-history", "label": "No recorded contact"},
                              {"code": "property-lookup",
                               "label": "Public-record property lookup timed out — not checked this pass"}])},
    {"client_id": "c-3", "full_name": None, "lead_score": 55, "stage": "nurture",
     "summary": "Asked twice about schools near the lake.", "score_breakdown": None,
     "next_actions": [{"code": "review-property-match",
                       "title": "Review public-record property candidates"}],
     "data_gaps": [{"code": "property-lookup",
                    "label": "Public-record property lookup unavailable — not checked this pass"}]},
    {"client_id": "c-4", "full_name": "Ana Ruiz", "lead_score": 80, "stage": "active",
     "summary": "Pre-approved; wants to see the lake house again.", "score_breakdown": None,
     "next_actions": [{"code": "model-suggestion", "title": "Book a second showing at 9 Lake Rd"}],
     "data_gaps": []},
    {"client_id": "c-5", "full_name": "Ben Cole", "lead_score": 40, "stage": "lead",
     "summary": None, "score_breakdown": None,
     "next_actions": [{"code": "something-new"}], "data_gaps": [{"code": "unlabelled"}]},
    {"client_id": "c-6", "full_name": "Cara Diaz", "lead_score": 40, "stage": "lead",
     "summary": None, "score_breakdown": None,
     "next_actions": ["Call her back"], "data_gaps": None},
]

DISTRESS_ROWS = [
    {"id": "l-9", "address": "5 Pine St", "state": "NJ", "motivation_score": 91,
     "dossier_status": "draft", "updated_at": _at(-30)},
    {"id": "l-8", "address": "77 Bay Rd", "state": "MD", "motivation_score": 84,
     "dossier_status": "new", "updated_at": None},
]

LISTING_ROWS = [{"listing_id": "li-1", "lead_id": "l-20"}, {"listing_id": "li-2", "lead_id": "l-21"},
                {"listing_id": "li-3", "lead_id": "l-22"}]


def _match(name: Optional[str], criteria: list[tuple[str, str]], **kw) -> dict[str, Any]:
    return {
        "client_id": "c-9", "name": name, "verdict": "strong",
        "matched_criteria": [note for _dim, note in criteria],
        "matched_dimensions": [dim for dim, _note in criteria],
        "unknown": [], "matched_signals": len(criteria),
        "stated_needs": kw.get("needs", []), "recent_showings": kw.get("showings", []),
    }


BUYER_MATCHES = {
    "li-1": {
        "property": {"lead_id": "l-20", "address": "9 Lake Rd, Dover, DE 19901"},
        "matches": [_match("Sarah Johnson", [("location", "looking in 19901"),
                                              ("budget", "budget up to $450,000"),
                                              ("beds", "wants 3+ bedrooms, this has 3")],
                           needs=["a yard for the dog", "near good schools", "garage"],
                           showings=[{"address": "12 Oak St", "shown_at": "2026-10-01", "outcome": "liked"},
                                     {"address": "40 Elm Ave", "shown_at": "2026-09-20", "outcome": None}])],
    },
    "li-2": {
        "property": {"lead_id": None, "address": "unparseable"},
        "matches": [_match("Luis Ortega", [("budget", "budget up to $300,000"),
                                            ("property_type", "wants condo")]),
                    _match("Ana Ruiz", [("location", "looking in Dover"),
                                        ("budget", "$310,000 is 3% over their $300,000 budget")])],
    },
    "li-3": {
        "property": {"lead_id": "l-22", "address": "1 Bay Ct, Lewes, DE 19958"},
        "matches": [_match("", [("location", "looking in 19958"), ("beds", "wants 2+ bedrooms, this has 4")]),
                    _match("Ben Cole", [("location", "looking in Lewes"), ("budget", "budget up to $900,000")]),
                    _match("Cara Diaz", [("location", "looking in 19958"), ("beds", "wants 3+ bedrooms, this has 4")])],
    },
}


class FakeConn:
    """Answers each detector's one query with its fixture rows."""

    async def fetch(self, sql: str, *args):
        if "contract_expires_at BETWEEN" in sql:
            return CONTRACT_ROWS
        if "FROM client_ai_state" in sql:
            return INTENT_ROWS
        if "DISTINCT ON (state)" in sql:
            return DISTRESS_ROWS
        if "FROM listings" in sql:
            return LISTING_ROWS
        raise AssertionError(f"unexpected query: {sql[:60]}")

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield


async def _buyer_matches(_conn, _ctx, *, listing_id=None, lead_id=None, limit=5):
    return BUYER_MATCHES.get(listing_id)


DETECTORS = ("_contract_deadline_opportunities", "_intent_model_opportunities",
             "_listing_buyer_opportunities", "_distress_opportunities")


def _run(name: str, lang: Optional[str]) -> list[dict[str, Any]]:
    detector = getattr(oe, name)
    args = (FakeConn(), CTX) if lang is None else (FakeConn(), CTX, lang)
    with mock.patch.object(oe, "datetime", _FixedDatetime), \
            mock.patch("buyer_matching.property_buyer_matches", _buyer_matches):
        return [o.as_dict() for o in asyncio.run(detector(*args))]


def outputs(lang: Optional[str]) -> dict[str, Any]:
    return {name: _run(name, lang) for name in DETECTORS}


def english_outputs() -> dict[str, Any]:
    return outputs(None)


if __name__ == "__main__":
    GOLDEN.write_text(json.dumps(english_outputs(), indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN}")
