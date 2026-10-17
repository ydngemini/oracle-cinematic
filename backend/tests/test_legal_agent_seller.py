"""The AGENT LEGAL package never names the public-record owner as Seller (F11.04).

A lead's ``owner_name`` is the owner of record from a public-record parcel
match nobody has confirmed. The live pipeline (real_leads -> WorkflowEngine ->
format_for_websocket -> LegalMatrix "Assignment Contract") used to copy it
into ``contract_fields["seller"]``, and when the payload had no owner the key
arrived as None and the panel rendered "Seller: null".
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import legal_agent
import workflow_engine
from legal_agent import format_for_websocket, generate_legal_package
from real_leads import record_from_lead_row
from workflow_engine import WorkflowEngine

OWNER = "John Q Smith"


def _property(**overrides):
    data = {"address": "42 Elkton Rd, Newark, DE 19711", "price": 150000}
    data.update(overrides)
    return data


def _is_unconfirmed(seller):
    return isinstance(seller, str) and "not confirmed" in seller


def test_owner_of_record_is_not_the_seller():
    package = generate_legal_package(_property(owner_name=OWNER), strategy="wholesale")

    assert package["contract_type"] == "Assignment Contract"
    # Nowhere in the package: not as Seller, not under another contract field.
    assert OWNER not in json.dumps(package)
    assert package["contract_fields"]["seller"] == legal_agent.UNCONFIRMED_SELLER


def test_missing_owner_never_renders_as_null():
    package = generate_legal_package(_property(owner_name=None))

    assert _is_unconfirmed(package["contract_fields"]["seller"])
    assert package["contract_fields"]["seller"] == legal_agent.UNCONFIRMED_SELLER


def test_a_stated_seller_fills_the_seller():
    package = generate_legal_package(_property(seller_name="  Confirmed Seller  ", owner_name=OWNER))

    assert OWNER not in json.dumps(package)
    assert package["contract_fields"]["seller"] == "Confirmed Seller"


def test_blank_or_non_text_stated_seller_is_unconfirmed():
    for stated in ("", "   ", None, 42, {"name": OWNER}):
        package = generate_legal_package(_property(seller_name=stated, owner_name=OWNER))
        assert _is_unconfirmed(package["contract_fields"]["seller"]), stated


def test_websocket_frame_carries_no_owner_as_seller():
    frame = json.loads(format_for_websocket(_property(owner_name=OWNER), strategy="wholesale"))

    assert frame["type"] == "LEGAL_PACKAGE"
    assert OWNER not in json.dumps(frame)
    assert _is_unconfirmed(frame["data"]["contract_fields"]["seller"])


class _Graph:
    def __init__(self, hit):
        self.hit = hit

    async def calculate_novelty_score(self):
        yield self.hit


class _NoCMA:
    async def analyze_property(self, _hit):
        return None


def _lead_row(payload):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "parcel_id": "DE-0001",
        "payload": json.dumps(payload),
        "underwriting": "{}",
        "asking_price": None,
        "motivation_score": 60,
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "address": "42 Elkton Rd, Newark, DE 19711",
        "sqft": 1400,
        "beds": 3,
        "baths": 2,
        "state": "DE",
    }


def _legal_frame(monkeypatch, payload):
    """Drive one real analysis cycle on a tenant lead; return its LEGAL_PACKAGE."""
    frames = []

    async def publish(frame):
        frames.append(frame)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(workflow_engine.asyncio, "sleep", no_wait)
    engine = WorkflowEngine(publish=publish, tenant_id="t")
    engine.graph = _Graph({**record_from_lead_row(_lead_row(payload)), "novelty_score": 91})
    engine.analyst = _NoCMA()
    asyncio.run(engine._run_analysis_cycle())

    legal = [f for f in frames if f.get("type") == "LEGAL_PACKAGE"]
    assert len(legal) == 1
    return legal[0]


def test_live_pipeline_contract_panel_has_no_owner_as_seller(monkeypatch):
    frame = _legal_frame(monkeypatch, {"owner_name": OWNER, "estimated_value": 200000})

    assert frame["data"]["contract_type"] == "Assignment Contract"
    # The engine still voices its simulated "Initiating contact with …" line,
    # which is not the contract; the LEGAL_PACKAGE frame carries no owner.
    assert OWNER not in json.dumps(frame)
    assert _is_unconfirmed(frame["data"]["contract_fields"]["seller"])


def test_live_pipeline_lead_without_owner_shows_unconfirmed_not_null(monkeypatch):
    frame = _legal_frame(monkeypatch, {"estimated_value": 200000})

    # real_leads maps a missing owner to None; the panel must not say "null".
    assert _is_unconfirmed(frame["data"]["contract_fields"]["seller"])
