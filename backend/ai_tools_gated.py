"""Agent tools that request an action instead of performing one.

Every tool here is in ``ai_tool_policy.GATED_TOOLS``. None of them sends an
email, dials a number, writes a calendar entry, executes a contract, or lists a
property. Each builds the same request a human would build in the UI, attaches
an approval, and returns its identifiers. The action happens later, in the path
a human decision triggers — which is also where the legal controls live:
``guard_outreach`` for outreach, ``review_document`` (broker-owner only) for
contracts, ``publish_publication`` for the marketplace.

Two rules hold across the module, and the tests check both statically:

**Nothing here calls a provider.** Not a mail sender, not Twilio, not the vault
writer. A gated tool that could act would make the approval decorative.

**Facts come from records, not from the model.** The phone number, the email
address, the purchase price, the closing date — all read from the row the agent
selected. The model chooses *who* and *why*; it does not get to supply *what*.
An earlier version of ``call_contact`` accepted a model-supplied phone string,
which is one fabricated digit away from dialling a stranger.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from record_json import clean as _clean
from tenancy import TenantContext


TOOLS_HANDLED = frozenset({
    "request_property_reconstruction",
    "request_listing_video",
    "draft_email",
    "draft_sms",
    "call_contact",
    "draft_contract",
    "schedule_event",
    "publish_to_marketplace",
})

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# Contact resolution
# ---------------------------------------------------------------------------

def _to_e164(raw: object) -> tuple[Optional[str], Optional[str]]:
    """Normalise a stored phone number, or say why it cannot be.

    ``clients.phone`` is free text and the command layer requires E.164, so the
    choice is between normalising here under a stated rule and refusing. A bare
    ten-digit number is taken as +1 — this platform is US-only throughout
    (state licensing, TCPA, quiet hours) — and the result goes into the draft
    the approver reads, so the assumption is visible before anyone is contacted.
    Anything more ambiguous is refused rather than guessed.
    """
    digits = re.sub(r"[^\d+]", "", str(raw or ""))
    if not digits:
        return None, "the client record has no phone number"
    if digits.startswith("+"):
        candidate = digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        return None, (
            f"the stored number {str(raw)!r} is not in a recognisable format; "
            f"add a country code in the CRM"
        )
    if not _E164_RE.match(candidate):
        return None, f"the stored number {str(raw)!r} is not a valid phone number"
    return candidate, None


async def _outreach_target(conn, ctx: TenantContext, client_id: str):
    """Contact details for an outreach command, read from the record."""
    client = await conn.fetchrow(
        """SELECT id,full_name,email,phone FROM clients
            WHERE id=$1::uuid AND tenant_id=$2::uuid AND archived_at IS NULL""",
        client_id, ctx.tenant_id,
    )
    if not client:
        return None, _err("The selected client no longer exists.")
    # Quiet hours and consent rules are per state, so guard_outreach needs one
    # at execution. It lives on the linked property, not on the contact.
    state_code = await conn.fetchval(
        """SELECT state FROM leads
            WHERE seller_client_id=$1::uuid AND tenant_id=$2::uuid AND state IS NOT NULL
            ORDER BY updated_at DESC LIMIT 1""",
        client_id, ctx.tenant_id,
    )
    return {
        "client_id": client_id,
        "full_name": client["full_name"],
        "email": (client["email"] or "").strip(),
        "phone_raw": client["phone"],
        "state_code": (state_code or "").upper(),
    }, None


def _staged(tool_name: str, staged: dict, *, channel: str, to: str, detail: str) -> dict:
    """Receipt for a staged command. Nothing was sent."""
    command = staged["command"]
    approval = staged.get("approval") or {}
    return {
        "ok": True,
        "action_type": tool_name,
        "approval_id": str(approval.get("id") or command.get("approval_id") or ""),
        "command_id": str(command["id"]),
        "state": command["state"],
        "channel": channel,
        "to": to,
        "sent": False,
        "detail": detail,
        "reused_existing_request": staged.get("created") is False,
    }


async def _stage(ctx, *, command_type, target, draft, message_id, tool_name,
                 anchor_id, user_id, recipient, detail):
    from commands_api import stage_command

    try:
        staged = await stage_command(
            ctx,
            command_type=command_type,
            target=target,
            draft=draft,
            context={"origin": "ai_chat", "message_id": message_id},
            # One request per tool call per message: a model that loops
            # requeues the same row instead of stacking approvals.
            idempotency_key=f"ai:{message_id}:{tool_name}:{anchor_id}",
            created_by=user_id,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        return _err(f"The request was rejected: {exc}")
    return _staged(tool_name, staged, channel=command_type.value, to=recipient,
                   detail=detail)


# ---------------------------------------------------------------------------
# Outreach — EMAIL, SMS, CALL
# ---------------------------------------------------------------------------

async def _outreach(conn, ctx, *, tool_name, tool_input, client_id, user_id,
                    message_id) -> dict:
    from commands_api import CommandType

    contact, error = await _outreach_target(conn, ctx, client_id)
    if error:
        return error

    if tool_name == "draft_email":
        subject = str(tool_input.get("subject") or "").strip()
        body_text = str(tool_input.get("body") or "").strip()
        if not subject or len(subject) > 200:
            return _err("subject must be 1-200 characters.")
        if not body_text or len(body_text) > 20_000:
            return _err("body must be 1-20000 characters.")
        if not contact["email"]:
            return _err(
                f"{contact['full_name']} has no email address on record, so "
                f"there is nowhere to send this."
            )
        return await _stage(
            ctx, command_type=CommandType.EMAIL,
            target={"email": contact["email"], "client_id": client_id,
                    "state_code": contact["state_code"] or None},
            draft={"subject": subject, "body": body_text},
            message_id=message_id, tool_name=tool_name, anchor_id=client_id,
            user_id=user_id, recipient=contact["email"],
            detail=(f"Email drafted to {contact['email']} and queued for "
                    f"approval. Nothing has been sent."),
        )

    phone, phone_error = _to_e164(contact["phone_raw"])
    if phone_error:
        return _err(f"Cannot contact {contact['full_name']}: {phone_error}.")
    if not contact["state_code"]:
        # guard_outreach reads the state to apply quiet hours and per-state
        # consent. Guessing a state would be guessing which law applies.
        return _err(
            f"No state is recorded for {contact['full_name']}. Quiet hours and "
            f"consent rules are per state, so the request cannot be staged "
            f"without one — link the client to a property or set the state in "
            f"the CRM."
        )
    target = {"phone": phone, "client_id": client_id,
              "state_code": contact["state_code"]}

    if tool_name == "draft_sms":
        message = str(tool_input.get("body") or "").strip()
        if not message or len(message) > 1_600:
            return _err("body must be 1-1600 characters.")
        return await _stage(
            ctx, command_type=CommandType.SMS, target=target,
            draft={"body": message}, message_id=message_id, tool_name=tool_name,
            anchor_id=client_id, user_id=user_id, recipient=phone,
            detail=(f"Text drafted to {phone} and queued for approval. "
                    f"Nothing has been sent."),
        )

    reason = str(tool_input.get("reason") or "").strip()
    if not reason or len(reason) > 2_000:
        return _err("reason must be 1-2000 characters.")
    return await _stage(
        ctx, command_type=CommandType.CALL, target=target,
        draft={"reason": reason}, message_id=message_id, tool_name=tool_name,
        anchor_id=client_id, user_id=user_id, recipient=phone,
        detail=(f"Call to {phone} queued for approval. No call has been placed "
                f"and none will be until a human approves it."),
    )


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _iso_datetime(value: object, field: str):
    raw = str(value or "").strip()
    if not raw:
        return None, _err(f"{field} is required.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, _err(
            f"{field} must be an ISO-8601 timestamp such as "
            f"2026-08-20T14:00:00-04:00."
        )
    if parsed.tzinfo is None:
        # A naive time means a different moment in every timezone, and the
        # people in this calendar entry are not all in one.
        return None, _err(f"{field} must include a UTC offset.")
    return parsed, None


async def _schedule_event(conn, ctx, *, tool_input, client_id, user_id,
                          message_id) -> dict:
    from commands_api import CommandType

    summary = str(tool_input.get("summary") or "").strip()
    if not summary or len(summary) > 200:
        return _err("summary must be 1-200 characters.")
    start, error = _iso_datetime(tool_input.get("start"), "start")
    if error:
        return error
    end, error = _iso_datetime(tool_input.get("end"), "end")
    if error:
        return error
    if end <= start:
        return _err("end must be after start.")
    if start < datetime.now(timezone.utc):
        return _err("start is in the past; a meeting cannot be scheduled backwards.")

    contact, contact_error = await _outreach_target(conn, ctx, client_id)
    if contact_error:
        return contact_error

    description = str(tool_input.get("description") or "").strip()[:5_000]
    return await _stage(
        ctx, command_type=CommandType.CALENDAR,
        target={"client_id": client_id, "email": contact["email"] or None},
        draft={"event": {
            "summary": summary,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "description": description or None,
            # The invitee is whoever the record says, not a name the model
            # recalled from the conversation.
            "attendee": contact["email"] or None,
        }},
        message_id=message_id, tool_name="schedule_event", anchor_id=client_id,
        user_id=user_id, recipient=contact["email"] or contact["full_name"],
        detail=(
            f"Calendar entry drafted for {start.isoformat()} and queued for "
            f"approval. Nothing has been written to a calendar."
        ),
    )


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

# Where each template variable comes from. Nothing in this table is a value the
# model may supply: a purchase price or a closing date that an assistant
# remembered rather than read is a fabricated term inside a legal instrument,
# and the broker reviewing it has no way to tell which is which.
_CONTRACT_FIELD_SOURCES: dict[str, str] = {
    "current_date": "today's date",
    "seller_name": "the seller party on the transaction, or the linked client",
    "buyer_name": "the buyer party on the transaction",
    "assignor_name": "the buyer party on the transaction (the assignor)",
    "assignee_name": "the assignee party on the transaction",
    "party_a_name": "the first party on the transaction",
    "party_b_name": "the second party on the transaction",
    "property_address": "transactions.property_address, or leads.address",
    "purchase_price": "transactions.purchase_price",
    "wholesale_buy_price": "transactions.purchase_price",
    "investor_buy_price": "the accepted offer amount on the transaction",
    "earnest_money_deposit": "transactions.earnest_money",
    "closing_date": "transactions.closing_deadline",
    "original_contract_date": "leads.contract_execution_date",
}

_PARTY_ROLE_FIELDS = {
    "seller_name": "seller",
    "buyer_name": "buyer",
    "assignor_name": "buyer",
    "assignee_name": "assignee",
    "party_a_name": "buyer",
    "party_b_name": "seller",
}


def _money(value) -> Optional[str]:
    return None if value is None else f"{float(value):,.2f}"


async def _contract_inputs(conn, ctx: TenantContext, deal_id: str,
                           required: list[str]) -> tuple[dict, dict]:
    """Fill what the records hold and leave the rest visibly empty."""
    lead = await conn.fetchrow(
        """SELECT id,address,state,contract_execution_date,seller_client_id
             FROM leads WHERE id=$1::uuid AND tenant_id=$2::uuid""",
        deal_id, ctx.tenant_id,
    )
    transaction = await conn.fetchrow(
        """SELECT id,property_address,purchase_price,earnest_money,
                  closing_deadline,accepted_offer_id
             FROM transactions
            WHERE lead_id=$1::uuid AND tenant_id=$2::uuid
            ORDER BY updated_at DESC LIMIT 1""",
        deal_id, ctx.tenant_id,
    )
    parties: dict[str, str] = {}
    if transaction:
        rows = await conn.fetch(
            """SELECT party_role,display_name FROM transaction_parties
                WHERE transaction_id=$1::uuid AND tenant_id=$2::uuid
                  AND display_name IS NOT NULL""",
            transaction["id"], ctx.tenant_id,
        )
        parties = {row["party_role"]: row["display_name"] for row in rows}
    seller_fallback = None
    if lead and lead["seller_client_id"]:
        seller_fallback = await conn.fetchval(
            "SELECT full_name FROM clients WHERE id=$1::uuid AND tenant_id=$2::uuid",
            lead["seller_client_id"], ctx.tenant_id,
        )
    accepted_amount = None
    if transaction and transaction["accepted_offer_id"]:
        accepted_amount = await conn.fetchval(
            """SELECT amount FROM transaction_offers
                WHERE id=$1::uuid AND tenant_id=$2::uuid""",
            transaction["accepted_offer_id"], ctx.tenant_id,
        )

    resolved: dict[str, Any] = {}
    for field in required:
        value = None
        if field == "current_date":
            value = date.today().isoformat()
        elif field in _PARTY_ROLE_FIELDS:
            value = parties.get(_PARTY_ROLE_FIELDS[field])
            if value is None and field == "seller_name":
                value = seller_fallback
        elif field == "property_address":
            value = (transaction["property_address"] if transaction else None) or (
                lead["address"] if lead else None)
        elif field in ("purchase_price", "wholesale_buy_price"):
            value = _money(transaction["purchase_price"]) if transaction else None
        elif field == "investor_buy_price":
            value = _money(accepted_amount)
        elif field == "earnest_money_deposit":
            value = _money(transaction["earnest_money"]) if transaction else None
        elif field == "closing_date":
            value = (transaction["closing_deadline"].isoformat()
                     if transaction and transaction["closing_deadline"] else None)
        elif field == "original_contract_date":
            value = (lead["contract_execution_date"].isoformat()
                     if lead and lead["contract_execution_date"] else None)
        if value not in (None, ""):
            resolved[field] = str(value)

    missing = {
        field: _CONTRACT_FIELD_SOURCES.get(
            field,
            "no record on this platform stores this; it is a term someone has "
            "to decide and enter in Contract Vault",
        )
        for field in required if field not in resolved
    }
    return resolved, missing


async def _draft_contract(conn, ctx, *, tool_input, deal_id, user_id) -> dict:
    from contracts_api import BUILTIN_CONTRACT_TEMPLATES

    template_key = str(tool_input.get("template_key") or "").strip()
    if template_key not in BUILTIN_CONTRACT_TEMPLATES:
        return _err(
            "template_key must be one of: "
            + ", ".join(sorted(BUILTIN_CONTRACT_TEMPLATES))
            + ". Use list_contract_templates to see which are approved here."
        )
    lead = await conn.fetchrow(
        """SELECT id,address,seller_client_id FROM leads
            WHERE id=$1::uuid AND tenant_id=$2::uuid""",
        deal_id, ctx.tenant_id,
    )
    if not lead:
        return _err("That deal is not in this workspace.")

    template = await conn.fetchrow(
        """SELECT id,template_key,version,document_type,required_fields,status,
                  attorney_reviewed_by
             FROM contract_templates
            WHERE tenant_id=$1::uuid AND template_key=$2 AND status='approved'
            ORDER BY updated_at DESC LIMIT 1""",
        ctx.tenant_id, template_key,
    )
    if not template:
        # An unapproved template is not a template. Rendering the built-in body
        # anyway would produce a document nobody vetted for this brokerage.
        return _err(
            f"No approved {template_key!r} template exists in this workspace. "
            f"A contract can only be drafted from a template an attorney has "
            f"approved in Contract Vault."
        )

    required = list(template["required_fields"] or [])
    resolved, missing = await _contract_inputs(conn, ctx, deal_id, required)
    if missing:
        # Deliberately not an error: naming the gaps is the useful answer, and
        # it is the one thing the old flat refusal could not give.
        return {
            "ok": True,
            "action_type": "draft_contract",
            "drafted": False,
            "template_key": template_key,
            "document_type": template["document_type"],
            "resolved_fields": resolved,
            "missing_fields": missing,
            "detail": (
                f"No document was created. {len(missing)} of {len(required)} "
                f"required terms are not recorded on this deal, and a contract "
                f"drafted with invented terms is worse than no contract. Fill "
                f"them on the transaction, or draft this in Contract Vault "
                f"where a human enters them."
            ),
        }

    # The reviewer of record is the attorney who approved this template for
    # this brokerage — a name read from the row, not one the model produced.
    # An invented reviewer on a legal document is a forged attestation.
    reviewer = str(template["attorney_reviewed_by"] or "").strip()
    if len(reviewer) < 3:
        return _err(
            f"The approved {template_key!r} template records no reviewing "
            f"attorney, and a draft cannot name one that is not on file."
        )

    from contracts_api import DocumentDraft, draft_document

    try:
        result = await draft_document(
            DocumentDraft(
                template_id=template["id"],
                inputs=resolved,
                # The document belongs to the client behind the deal when there
                # is one; the lead itself is the fallback anchor.
                vault_client_id=lead["seller_client_id"] or lead["id"],
                lead_id=deal_id,
                attorney_reviewer=reviewer,
            ),
            ctx,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "rendering failed"
        return _err(f"The contract could not be drafted: {detail}")

    # `editable_draft` — the rendered contract body — is deliberately dropped.
    # Putting a legal instrument's text into the model's context invites it to
    # quote, summarise or subtly restate terms that only the vault copy is
    # authoritative for.
    document = result.get("document") or {}
    approval = result.get("approval") or {}
    return {
        "ok": True,
        "action_type": "draft_contract",
        "drafted": True,
        "document_id": str(document.get("id") or ""),
        "approval_id": str(approval.get("id") or ""),
        "status": document.get("status"),
        "attorney_review_required": True,
        "executed": False,
        "template_key": template_key,
        "resolved_fields": resolved,
        "contract_text_withheld": True,
        "detail": (
            "A draft was created in Contract Vault and is awaiting attorney "
            "review. It is not executed, not signed, and not binding; a "
            "broker-owner must review it before it goes anywhere."
        ),
    }


# ---------------------------------------------------------------------------
# Marketplace
# ---------------------------------------------------------------------------

async def _publish_to_marketplace(ctx, *, tool_input) -> dict:
    from marketplace_api import PublicationCreate, create_publication_from_contract

    contract_id = str(tool_input.get("contract_id") or "").strip()
    try:
        import uuid as _uuid
        contract_uuid = _uuid.UUID(contract_id)
    except (ValueError, AttributeError):
        return _err("contract_id must be a UUID.")
    asking_price = tool_input.get("asking_price")
    if asking_price not in (None, ""):
        try:
            asking_price = float(str(asking_price).replace(",", "").replace("$", ""))
        except ValueError:
            return _err("asking_price must be a number.")
    else:
        asking_price = None

    try:
        result = await create_publication_from_contract(
            contract_uuid,
            # 'tenant' visibility, always: putting a property in front of every
            # brokerage on the platform is a disposition decision, and the
            # approver can widen it. The model does not get to choose the
            # audience.
            PublicationCreate(visibility="tenant", asking_price=asking_price),
            ctx,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "the request was refused"
        return _err(f"The publication could not be created: {detail}")

    publication = result.get("publication") or {}
    approval = result.get("approval") or {}
    return {
        "ok": True,
        "action_type": "publish_to_marketplace",
        "publication_id": str(publication.get("id") or ""),
        "approval_id": str(approval.get("id") or ""),
        "state": publication.get("state"),
        "visibility": publication.get("visibility"),
        "published": False,
        "detail": (
            "A draft listing was created and is awaiting approval. It is not "
            "visible to any buyer yet, and its visibility is limited to this "
            "workspace until a human widens it."
        ),
    }


# ---------------------------------------------------------------------------
# Spatial capture and marketing video — jobs that spend money
# ---------------------------------------------------------------------------

async def _property_anchor(conn, ctx: TenantContext, tool_input: dict):
    """Resolve lead_id/listing_id and prove the row is in this workspace.

    Checked before anything is staged: reconstruction_jobs FKs would otherwise
    raise on a bogus or cross-tenant id, and an approval pointing at a property
    the approver cannot see is worse than no approval.
    """
    import uuid as _uuid

    lead_raw = str(tool_input.get("lead_id") or "").strip()
    listing_raw = str(tool_input.get("listing_id") or "").strip()
    if not lead_raw and not listing_raw:
        return None, _err("Provide lead_id or listing_id.")

    field, raw, table = (
        ("lead_id", lead_raw, "leads") if lead_raw else ("listing_id", listing_raw, "listings")
    )
    try:
        anchor = _uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None, _err(f"{field} must be a UUID.")

    # RLS scopes this to the caller's tenant, so a hit proves visibility.
    if not await conn.fetchval(f"SELECT 1 FROM {table} WHERE id = $1", anchor):
        return None, _err(f"No {table[:-1]} with that id in this workspace.")
    return {"field": field, "id": str(anchor), "target_type": table[:-1]}, None


async def _request_property_reconstruction(conn, ctx, *, tool_input, user_id) -> dict:
    """Ask for a 3D reconstruction of a property. Rents a GPU; does not start one.

    This spends real money — a pod-based reconstruction is roughly $0.25-0.35 —
    so it stages an approval and returns. The GPU is rented later, on the path a
    human decision triggers, which is the same rule every other gated tool
    follows: a tool that could act would make its own approval decorative.

    Readiness is checked *before* staging. Queuing an approval for a job that
    cannot run — no credits, no provider configured — spends a human's attention
    on a decision that has no effect, and the provider's own reason says exactly
    what to fix.
    """
    from approval_service import create_approval
    from platform_policy import ActionRisk

    anchor, error = await _property_anchor(conn, ctx, tool_input)
    if error:
        return error

    # `available()` only reports readiness; it neither reconstructs nor rents.
    from reconstruction_providers import get_provider

    provider = get_provider()
    ready, reason = provider.available()
    if not ready:
        return _err(
            f"A reconstruction cannot be run right now: {reason}. "
            f"Nothing was requested."
        )

    approval = await create_approval(
        ctx,
        action_type="property_reconstruction",
        risk=ActionRisk.FINANCIAL,
        target_type=anchor["target_type"],
        target_id=anchor["id"],
        draft_payload={
            anchor["field"]: anchor["id"],
            "provider": provider.name,
            # What the resulting media will be allowed to claim. Recorded in the
            # approval so the approver sees it, and so a later change of
            # provider cannot quietly upgrade the claim.
            "provenance": getattr(provider, "produces", "captured"),
            "requested_by": user_id,
        },
    )
    return {
        "ok": True,
        "action_type": "request_property_reconstruction",
        "approval_id": str(approval.get("id") or ""),
        "target_id": anchor["id"],
        "provider": provider.name,
        "started": False,
        "detail": (
            "A 3D reconstruction was requested and is awaiting approval. No GPU "
            "has been rented and nothing has been charged. Approving it starts "
            "the job; it takes roughly 30-60 minutes."
        ),
    }


async def _request_listing_video(conn, ctx, *, tool_input, user_id) -> dict:
    """Ask for a marketing video. Bills a video provider; does not generate one.

    The source imagery is deliberately not chosen here. Listing photos scraped
    from a portal are copyrighted works owned by the photographer or brokerage,
    and docs/data-access-tiers.md records that gate as one that stays shut — so
    the approver picks from media this workspace holds rather than the model
    naming a URL it found.
    """
    from approval_service import create_approval
    from platform_policy import ActionRisk

    anchor, error = await _property_anchor(conn, ctx, tool_input)
    if error:
        return error

    brief = str(tool_input.get("brief") or "").strip()[:1_000]
    if not brief:
        return _err("brief is required: say what the video should show.")

    # Aliased: reconstruction_providers exports a get_provider too, and the
    # two factories return entirely different things.
    from video_providers import get_provider as get_video_provider

    provider = get_video_provider()
    ready, reason = provider.available()
    if not ready:
        return _err(f"Video generation is not available: {reason}. Nothing was requested.")

    approval = await create_approval(
        ctx,
        action_type="listing_video",
        risk=ActionRisk.FINANCIAL,
        target_type=anchor["target_type"],
        target_id=anchor["id"],
        draft_payload={
            anchor["field"]: anchor["id"],
            "brief": brief,
            "provider": getattr(provider, "name", "unknown"),
            # Always. A generated walkthrough is not footage of the home, and
            # the label travels with the media rather than being decided later.
            "provenance": "ai_generated",
            "requested_by": user_id,
        },
    )
    return {
        "ok": True,
        "action_type": "request_listing_video",
        "approval_id": str(approval.get("id") or ""),
        "target_id": anchor["id"],
        "generated": False,
        "detail": (
            "A marketing video was requested and is awaiting approval. Nothing "
            "has been generated or charged. The finished video is labelled "
            "AI-generated; it is not footage of the property."
        ),
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def execute(conn, ctx: TenantContext, tool_name: str, tool_input: dict, *,
                  user_id: str, message_id: str, context_type: Optional[str],
                  context_id: Optional[str]) -> dict:
    """Run one gated tool. Every path returns a request, never a result."""
    tool_input = tool_input or {}

    if tool_name == "publish_to_marketplace":
        return await _publish_to_marketplace(ctx, tool_input=tool_input)

    if tool_name == "request_property_reconstruction":
        return await _request_property_reconstruction(
            conn, ctx, tool_input=tool_input, user_id=user_id
        )

    if tool_name == "request_listing_video":
        return await _request_listing_video(
            conn, ctx, tool_input=tool_input, user_id=user_id
        )

    from ai_chat_store import _selected_uuid

    if tool_name == "draft_contract":
        deal_id, error = _selected_uuid(
            tool_input=tool_input, input_name="deal_id",
            context_type=context_type, context_id=context_id,
            expected_context="lead",
        )
        if error:
            return error
        return await _draft_contract(conn, ctx, tool_input=tool_input,
                                     deal_id=deal_id, user_id=user_id)

    client_id, error = _selected_uuid(
        tool_input=tool_input, input_name="client_id",
        context_type=context_type, context_id=context_id,
        expected_context="client",
    )
    if error:
        return error
    if tool_name == "schedule_event":
        return await _schedule_event(conn, ctx, tool_input=tool_input,
                                     client_id=client_id, user_id=user_id,
                                     message_id=message_id)
    return await _outreach(conn, ctx, tool_name=tool_name, tool_input=tool_input,
                           client_id=client_id, user_id=user_id,
                           message_id=message_id)
