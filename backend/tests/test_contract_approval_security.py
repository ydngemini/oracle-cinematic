"""Security regressions for tenant-bound contract drafts and approvals."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import contracts_api
from automation_jobs import payload_hash
from contracts_api import ReviewDecision, SignatureRecord
from platform_policy import ActionRisk
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
DOCUMENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
APPROVAL_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
CLIENT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
TRANSACTION_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
ANCHOR_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
CTX = TenantContext(
    agent_id="broker@tenant.test",
    tenant_id=TENANT_ID,
    role=Role.BROKER_OWNER,
)


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(received_ctx):
        assert received_ctx == CTX
        yield conn

    return tx


class _MissingAnchorConn:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        assert "tenant_id=$2::uuid" in query
        assert args == (ANCHOR_ID, TENANT_ID)
        assert OTHER_TENANT_ID not in args
        return None


@pytest.mark.parametrize(
    "anchor_key",
    ["client_id", "lead_id", "listing_id", "property_id", "transaction_id"],
)
def test_cross_tenant_or_missing_draft_anchors_share_one_tenant_scoped_denial(
    anchor_key,
):
    conn = _MissingAnchorConn()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            contracts_api._validate_draft_anchors(
                conn,
                CTX,
                {
                    "tenant_id": OTHER_TENANT_ID,
                    "nested": {anchor_key: str(ANCHOR_ID)},
                },
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Contract draft anchor not found."
    assert len(conn.queries) == 1


def _approval_payload(content_sha256: str, revision: int) -> dict:
    return {
        "document_id": str(DOCUMENT_ID),
        "content_sha256": content_sha256,
        "revision": revision,
        "template_key": "assignment-standard",
        "template_version": "1.0.0",
        "vault_client_id": str(CLIENT_ID),
        "attorney_reviewer": "Reviewer Attestation",
    }


def _locked_document_row(
    *,
    content: str,
    metadata_hash: str,
    metadata_revision: int,
    approval_hash: str,
    approval_revision: int,
    approval_status: str = "pending",
) -> dict:
    payload = _approval_payload(approval_hash, approval_revision)
    return {
        "id": DOCUMENT_ID,
        "tenant_id": uuid.UUID(TENANT_ID),
        "transaction_id": TRANSACTION_ID,
        "lead_id": None,
        "template_key": "assignment-standard",
        "template_version": "1.0.0",
        "content_ciphertext": content.encode("utf-8"),
        "status": "review_required",
        "approval_id": APPROVAL_ID,
        "metadata": {
            "content_sha256": metadata_hash,
            "revision": metadata_revision,
            "vault_client_id": str(CLIENT_ID),
        },
        "locked_approval_id": APPROVAL_ID,
        "approval_action_type": "contract.vault_and_approve",
        "approval_risk_class": ActionRisk.LEGAL_DOCUMENT.value,
        "approval_target_type": "contract_document",
        "approval_target_id": str(DOCUMENT_ID),
        "draft_payload": payload,
        "approval_payload_hash": payload_hash(payload),
        "approval_status": approval_status,
    }


class _StaleDecisionConn:
    def __init__(self, row):
        self.row = row
        self.update_attempts = 0

    async def fetchrow(self, query, *_args):
        if "SELECT d.*" in query:
            assert "d.tenant_id=$2::uuid" in query
            assert "FOR UPDATE OF d,a" in query
            return self.row
        self.update_attempts += 1
        raise AssertionError(f"Stale approval must not mutate state: {query}")


@pytest.mark.parametrize("stale_part", ["content_hash", "revision"])
def test_stale_content_hash_or_revision_cannot_transition_approval(
    monkeypatch,
    stale_part,
):
    content = "Current contract body"
    current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    old_hash = hashlib.sha256(b"Old contract body").hexdigest()
    row = _locked_document_row(
        content=content,
        metadata_hash=current_hash,
        metadata_revision=2,
        approval_hash=old_hash if stale_part == "content_hash" else current_hash,
        approval_revision=1 if stale_part == "revision" else 2,
    )
    conn = _StaleDecisionConn(row)
    monkeypatch.setattr(contracts_api, "tenant_tx", _fake_tenant_tx(conn))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            contracts_api._transition_document_approval(
                DOCUMENT_ID,
                ReviewDecision(decision="approved", reason="Reviewed and approved."),
                CTX,
            )
        )

    assert exc.value.status_code == 409
    assert "stale" in exc.value.detail.lower()
    assert conn.update_attempts == 0


class _ConcurrentDecisionStore:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.content = "Concurrency-safe contract body"
        self.content_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        self.revision = 3
        self.payload = _approval_payload(self.content_sha256, self.revision)
        self.approval_status = "pending"
        self.approval_updates = 0
        self.metadata = {
            "content_sha256": self.content_sha256,
            "revision": self.revision,
            "vault_client_id": str(CLIENT_ID),
        }

    @asynccontextmanager
    async def transaction(self, received_ctx):
        assert received_ctx == CTX
        async with self.lock:
            yield self

    async def fetchrow(self, query, *args):
        if "SELECT d.*" in query:
            return {
                **_locked_document_row(
                    content=self.content,
                    metadata_hash=self.content_sha256,
                    metadata_revision=self.revision,
                    approval_hash=self.content_sha256,
                    approval_revision=self.revision,
                    approval_status=self.approval_status,
                ),
                "metadata": dict(self.metadata),
            }
        if "SELECT id FROM clients" in query:
            assert args == (CLIENT_ID, TENANT_ID)
            return {"id": CLIENT_ID}
        if "SELECT id FROM transactions" in query:
            assert args == (TRANSACTION_ID, TENANT_ID)
            return {"id": TRANSACTION_ID}
        if "UPDATE action_approvals" in query:
            assert "status='pending'" in query
            if self.approval_status != "pending":
                return None
            self.approval_status = args[2]
            self.approval_updates += 1
            return {
                "id": APPROVAL_ID,
                "tenant_id": uuid.UUID(TENANT_ID),
                "action_type": "contract.vault_and_approve",
                "risk_class": ActionRisk.LEGAL_DOCUMENT.value,
                "target_type": "contract_document",
                "target_id": str(DOCUMENT_ID),
                "payload_hash": payload_hash(self.payload),
                "draft_payload": dict(self.payload),
                "requested_by": "author@tenant.test",
                "requested_at": datetime.now(timezone.utc) - timedelta(minutes=1),
                "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
                "status": self.approval_status,
                "decided_by": CTX.agent_id,
                "decided_at": datetime.now(timezone.utc),
                "reason": args[4],
            }
        if "UPDATE contract_documents" in query:
            assert args[1] == TENANT_ID
            self.metadata.update(
                {
                    "approval_content_sha256": self.content_sha256,
                    "approval_revision": self.revision,
                }
            )
            return {"id": DOCUMENT_ID}
        raise AssertionError(f"Unexpected query: {query}")


async def _fake_decrypt(_conn, ciphertext, _key):
    return ciphertext.decode("utf-8")


def test_concurrent_and_replayed_approval_transitions_apply_once(monkeypatch):
    store = _ConcurrentDecisionStore()
    queued: list[dict] = []

    async def fake_enqueue(_ctx, **kwargs):
        queued.append(kwargs)
        return {"id": str(uuid.uuid4()), "state": "queued"}, True

    async def fake_audit(**_kwargs):
        return None

    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-only-key")
    monkeypatch.setattr(contracts_api, "tenant_tx", store.transaction)
    monkeypatch.setattr(contracts_api, "decrypt_pii", _fake_decrypt)
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)
    monkeypatch.setattr(contracts_api, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(contracts_api, "ledger", SimpleNamespace(record=fake_audit))
    body = ReviewDecision(
        decision="approved",
        reason="Reviewed and approved.",
        approval_id=APPROVAL_ID,
        content_sha256=store.content_sha256,
        revision=store.revision,
    )

    async def race():
        return await asyncio.gather(
            contracts_api.review_document(DOCUMENT_ID, body, CTX),
            contracts_api.review_document(DOCUMENT_ID, body, CTX),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, HTTPException)]

    assert len(successes) == 1
    assert successes[0]["queued"] is True
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert store.approval_updates == 1
    assert len(queued) == 1

    with pytest.raises(HTTPException) as replay:
        asyncio.run(contracts_api.review_document(DOCUMENT_ID, body, CTX))
    assert replay.value.status_code == 409
    assert store.approval_updates == 1
    assert len(queued) == 1


class _RevokeConn:
    def __init__(self):
        self.status = "approved"
        self.transitions = 0

    async def fetchrow(self, query, *args):
        assert "tenant_id=$2::uuid" in query
        assert "status IN ('pending','approved')" in query
        assert args[:2] == (APPROVAL_ID, TENANT_ID)
        if self.status not in {"pending", "approved"}:
            return None
        self.status = "revoked"
        self.transitions += 1
        return {"id": APPROVAL_ID}


def test_content_revision_revokes_current_approval_exactly_once():
    conn = _RevokeConn()

    async def revoke_twice():
        first = await contracts_api._revoke_document_approval(
            conn, CTX, APPROVAL_ID
        )
        second = await contracts_api._revoke_document_approval(
            conn, CTX, APPROVAL_ID
        )
        return first, second

    assert asyncio.run(revoke_twice()) == (True, False)
    assert conn.transitions == 1


class _SignatureConn:
    async def fetchrow(self, query, *args):
        assert "SET status='signed'" not in query
        assert "tenant_id=$5::uuid" in query
        assert args[4] == TENANT_ID
        return {
            "id": DOCUMENT_ID,
            "tenant_id": uuid.UUID(TENANT_ID),
            "status": "approved",
            "metadata": {
                "signature_reference": args[1],
                "signature_recorded_by": args[2],
                "signature_reason": args[3],
                "signature_verification_status": "unverified",
                "execution_status": "not_verified",
            },
        }


def test_self_attested_signature_does_not_create_executed_state(monkeypatch):
    conn = _SignatureConn()
    monkeypatch.setattr(contracts_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)

    result = asyncio.run(
        contracts_api.record_signature(
            DOCUMENT_ID,
            SignatureRecord(
                signature_reference="self-reported-envelope-123",
                reason="Signature reported by broker.",
            ),
            CTX,
        )
    )

    assert result["status"] == "approved"
    assert result["metadata"]["signature_verification_status"] == "unverified"
    assert result["metadata"]["execution_status"] == "not_verified"
