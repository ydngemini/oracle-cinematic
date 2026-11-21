"""The person approving a one-to-one text declares what it is for (external
audit 26.5: "add a purpose classification to approved outbound messaging").

Informational (a reply, an appointment or showing confirmation, a document or
deadline reminder; no promotion) needs prior express consent; marketing needs
documented written consent. Undeclared is marketing. Missions, sales
sequences, the AI sales agent and speed-to-lead stay marketing, and so do
emails and calls. The declaration is stored outside the hashed execution
payload and read back at send time only for the approval it was made for.
Behaviour against PostgreSQL: tests/test_sms_purpose_live.py.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import commands_api as ca
from outreach_compliance import Purpose
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="agent-a@brokerage.test", tenant_id="00000000-0000-0000-0000-0000000000aa",
                    role=Role.AGENT)
APPROVAL = "11111111-1111-1111-1111-111111111111"


def _row(command_type="SMS", context=None, created_by="agent-a@brokerage.test", declared=None,
         approval_id=APPROVAL, as_text=False, key="client-key-1234"):
    draft = {"content": {"body": "Your showing is confirmed for 3 pm."}, "context": context or {}}
    if declared is not None:
        draft["declared_purpose"] = declared
    return {"command_type": command_type, "created_by": created_by, "approval_id": approval_id,
            "idempotency_key": key, "draft": json.dumps(draft) if as_text else draft}


def test_an_approval_may_carry_a_purpose_and_nothing_else_new():
    assert ca.ApprovalDecision(reason="Reviewed the text.").purpose is None
    assert ca.ApprovalDecision(reason="Reviewed the text.", purpose="informational").purpose is Purpose.INFORMATIONAL
    with pytest.raises(ValidationError):
        ca.ApprovalDecision(reason="Reviewed the text.", purpose="transactional")
    with pytest.raises(ValidationError):
        ca.ApprovalDecision(reason="Reviewed the text.", evidence_status="documented")


@pytest.mark.parametrize("purpose, stored, declared", [
    (None, "marketing", False),                     # the strict default
    (Purpose.MARKETING, "marketing", True),
    (Purpose.INFORMATIONAL, "informational", True),
])
def test_a_one_to_one_text_records_the_approvers_declaration(purpose, stored, declared):
    record = ca._purpose_declaration(_row(), purpose, CTX)
    assert record == {"purpose": stored, "declared": declared, "declared_by": CTX.agent_id,
                      "approval_id": APPROVAL}


@pytest.mark.parametrize("context, created_by, origin", [
    ({"source": "speed-to-lead"}, "agent-a@brokerage.test", "speed-to-lead"),
    ({"source": "smart-plan"}, "agent-a@brokerage.test", "a sales sequence"),
    ({"source": "our-ai-sales-agent"}, "agent-a@brokerage.test", "the AI sales agent"),
    ({"mission_id": "m-1"}, "agent-a@brokerage.test", "a mission"),
    ({}, "mission:m-2", "a mission"),
])
def test_a_campaign_text_cannot_be_declared_informational(context, created_by, origin):
    row = _row(context=context, created_by=created_by)
    with pytest.raises(HTTPException) as refused:
        ca._purpose_declaration(row, Purpose.INFORMATIONAL, CTX)
    assert refused.value.status_code == 422 and origin in refused.value.detail
    assert ca._purpose_declaration(row, None, CTX)["purpose"] == "marketing"
    assert ca._purpose_declaration(row, Purpose.MARKETING, CTX)["purpose"] == "marketing"


def test_a_text_asked_of_neoh_in_chat_is_one_to_one():
    assert ca._campaign_origin(_row(context={"origin": "ai_chat", "message_id": "m"})) is None
    assert ca._purpose_declaration(_row(context={"origin": "ai_chat"}), Purpose.INFORMATIONAL, CTX)


@pytest.mark.parametrize("command_type", ["EMAIL", "CALL", "CALENDAR"])
def test_emails_and_calls_stay_marketing(command_type):
    with pytest.raises(HTTPException) as refused:
        ca._purpose_declaration(_row(command_type=command_type), Purpose.INFORMATIONAL, CTX)
    assert refused.value.status_code == 422
    assert ca._purpose_declaration(_row(command_type=command_type), Purpose.MARKETING, CTX) is None
    assert ca._purpose_declaration(_row(command_type=command_type), None, CTX) is None


def test_send_time_reads_the_declaration_only_for_its_own_approval():
    informational = {"purpose": "informational", "declared": True, "declared_by": CTX.agent_id,
                     "approval_id": APPROVAL}
    assert ca.declared_sms_purpose(_row(declared=informational)) is Purpose.INFORMATIONAL
    assert ca.declared_sms_purpose(_row(declared=informational, as_text=True)) is Purpose.INFORMATIONAL
    # Nothing declared, a marketing declaration, or one made for an earlier approval: marketing.
    assert ca.declared_sms_purpose(_row()) is Purpose.MARKETING
    assert ca.declared_sms_purpose(_row(declared={**informational, "purpose": "marketing"})) is Purpose.MARKETING
    assert ca.declared_sms_purpose(_row(declared={**informational, "approval_id": "other"})) is Purpose.MARKETING
    assert ca.declared_sms_purpose(_row(declared=informational, approval_id=None)) is Purpose.MARKETING
    assert ca.declared_sms_purpose(_row(declared="informational")) is Purpose.MARKETING
    assert ca.declared_sms_purpose({"command_type": "SMS", "state": "queued"}) is Purpose.MARKETING  # no columns
    # A campaign text is marketing whatever the stored record says.
    assert ca.declared_sms_purpose(_row(declared=informational, context={"source": "speed-to-lead"})) \
        is Purpose.MARKETING


def test_the_declaration_is_outside_the_approved_payload():
    """The job payload (hashed into the approval) is built from content and
    context only, so storing the declaration cannot change what was approved."""
    import inspect

    source = inspect.getsource(ca._queue_execution_in_tx)
    assert 'stored_draft.get("content")' in source and 'stored_draft.get("context")' in source
    assert "declared_purpose" not in source
    release = inspect.getsource(ca.release_command)
    assert release.index("_purpose_declaration(") < release.index("decide_approval_in_tx(")
    assert "extra_metadata=" in release


# ── review of PR #170: an edit cannot launder a campaign text ────────────────
EDITED_CAMPAIGNS = [   # the context an edit could empty; the key and created_by it cannot
    ("speed-to-lead:intake-1", "agent-a@brokerage.test", "speed-to-lead"),
    ("smart-plan:step-run-1", "agent-a@brokerage.test", "a sales sequence"),
    ("sales-agent:follow_up:contact-1:uuid", "agent-a@brokerage.test", "the AI sales agent"),
    ("mission:m-1:action:a-1", "mission:m-1", "a mission"),
    ("client-chosen-key", "mission:m-2", "a mission"),
]


@pytest.mark.parametrize("key, created_by, origin", EDITED_CAMPAIGNS)
def test_a_campaign_text_with_an_emptied_context_still_cannot_be_declared_informational(key, created_by, origin):
    row = _row(context={}, key=key, created_by=created_by)
    assert ca._campaign_origin(row) == origin
    with pytest.raises(HTTPException) as refused:
        ca._purpose_declaration(row, Purpose.INFORMATIONAL, CTX)
    assert refused.value.status_code == 422 and origin in refused.value.detail


@pytest.mark.parametrize("key, created_by, origin", EDITED_CAMPAIGNS)
def test_the_worker_reads_marketing_for_an_edited_campaign_text_even_with_a_stale_declaration(key, created_by, origin):
    stale = {"purpose": "informational", "declared": True, "declared_by": CTX.agent_id, "approval_id": APPROVAL}
    assert ca.declared_sms_purpose(_row(context={}, key=key, created_by=created_by, declared=stale)) \
        is Purpose.MARKETING


def test_an_edit_keeps_the_server_owned_context_and_cannot_add_it():
    stored = _row(context={"source": "speed-to-lead", "intake_event_id": "i-1", "contact_id": "c-1"})
    assert ca._edited_context(stored, {}) == {"source": "speed-to-lead", "intake_event_id": "i-1"}
    assert ca._edited_context(stored, {"source": "manual", "contact_id": "c-2", "note": "x"}) == {
        "source": "speed-to-lead", "intake_event_id": "i-1", "contact_id": "c-2", "note": "x"}
    mission = _row(context={"mission_id": "m-1", "objective": "o", "required_disclosures": ["AI voice"]})
    assert ca._edited_context(mission, {"mission_id": None})["required_disclosures"] == ["AI voice"]
    assert ca._edited_context(mission, {"mission_id": None})["mission_id"] == "m-1"
    one_to_one = _row(context={"client_id": "c-1"})
    assert ca._edited_context(one_to_one, {"client_id": "c-1", "source": "speed-to-lead",
                                           "mission_id": "m-9"}) == {"client_id": "c-1"}


def test_a_client_chosen_key_or_a_chat_request_is_one_to_one():
    assert ca._campaign_origin(_row(key="parse:abc")) is None
    assert ca._campaign_origin(_row(key="ai:msg:tool:anchor", context={"origin": "ai_chat"})) is None
