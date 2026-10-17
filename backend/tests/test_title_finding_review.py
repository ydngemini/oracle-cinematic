"""A person confirms or dismisses one title finding (T11.03, F11.04, L11-12).

`title_findings` has carried review_status/reviewed_by/reviewed_at since 0027,
and nothing ever wrote them: every matched or released finding read "Not
verified" to the homeowner, and the portal's dismissed filter only guarded rows
written outside the app. `POST /api/intelligence/title/findings/{id}/review`
is the reviewer action: a broker owner (the brokerage-level reviewer, as for
contract templates and model activation) decides one finding still awaiting
review, with a reason. The reason often names an owner, so it is stored only
sealed (0167, CONTRACT C5.1), under the key of the finding's own tenant. The
audit ledger gets the finding id, the decision and the actor, never free text,
and the record lands in the tenant whose finding changed.

Driven with a fake connection; tests/test_title_finding_review_live.py proves
the same under FORCE RLS as the application login.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import intelligence_api
import sealed_text
from audit_ledger import AuditCategory
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
# A platform admin works from their own context tenant; the finding may not be in it.
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
FINDING = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OWNER = TenantContext(agent_id="owner@brokerage.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@brokerage.test", tenant_id=TENANT_ID, role=Role.AGENT)
ADMIN = TenantContext(agent_id="ops@platform.test", tenant_id=TENANT_ID, role=Role.PLATFORM_ADMIN)
DECIDED_AT = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
MASTER = "title-review-test-master-key-0123456789abcdef"
# What a real reviewer writes: the owner's name and the record they compared.
REASON = "Judgment names Jane Q. Homeowner at 88 Oak St, not this parcel"


class _Conn:
    """The locking SELECT returns the finding's tenant and `current` review
    status (None = invisible under RLS); the UPDATE returns the decided row."""

    def __init__(self, *, current: str | None = "required", row_tenant=TENANT_ID):
        self.current = current
        self.row_tenant = uuid.UUID(str(row_tenant))
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "UPDATE title_findings" in query:
            if self.current != "required":
                return None
            return {
                "id": FINDING,
                "tenant_id": self.row_tenant,
                "property_key": "DE-NCC-0142",
                "finding_type": "judgment v. Jane Q. Homeowner",
                "match_status": "possible_match",
                "review_status": args[1],
                "reviewed_by": args[2],
                "reviewed_at": DECIDED_AT,
            }
        if "FROM title_findings" in query and "FOR UPDATE" in query:
            if self.current is None:
                return None
            return {"tenant_id": self.row_tenant, "review_status": self.current}
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        raise AssertionError(f"unexpected fetchval: {query}")


class _Ledger:
    def __init__(self):
        self.calls: list[dict] = []

    async def record(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


def _review(monkeypatch, ctx, *, decision="confirmed", reason=REASON, current="required",
            row_tenant=TENANT_ID, master=MASTER):
    if master:
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", master)
    else:
        monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    conn = _Conn(current=current, row_tenant=row_tenant)
    ledger = _Ledger()

    @asynccontextmanager
    async def fake_tx(received):
        assert received == ctx
        yield conn

    monkeypatch.setattr(intelligence_api, "tenant_tx", fake_tx)
    monkeypatch.setattr(intelligence_api, "ledger", ledger)
    body = intelligence_api.TitleFindingReview(decision=decision, reason=reason)
    try:
        result = asyncio.run(
            intelligence_api.review_title_finding(finding_id=FINDING, body=body, ctx=ctx)
        )
    except (HTTPException, sealed_text.SealError) as exc:
        return exc, conn, ledger
    return result, conn, ledger


def _flat(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def _predicate(query: str) -> str:
    """The WHERE clause only: a select or RETURNING list may name tenant_id."""
    where = _flat(query).split(" WHERE ", 1)[1]
    return re.split(r" RETURNING | FOR UPDATE", where)[0]


def test_an_agent_cannot_decide_a_title_finding(monkeypatch):
    error, conn, ledger = _review(monkeypatch, AGENT)

    assert isinstance(error, HTTPException) and error.status_code == 403
    assert conn.queries == [] and ledger.calls == []


def test_a_broker_owner_confirms_a_finding_awaiting_review(monkeypatch):
    result, conn, ledger = _review(monkeypatch, OWNER)

    assert result["id"] == str(FINDING)
    assert result["review_status"] == "confirmed"
    assert result["reviewed_by"] == OWNER.agent_id
    assert result["reviewed_at"] == DECIDED_AT.isoformat()
    [(lock, lock_args), (query, args)] = conn.queries
    # The finding is read and locked first: its own tenant names the sealing key.
    assert _flat(lock).startswith("SELECT tenant_id,review_status FROM title_findings")
    assert lock_args == (str(FINDING),)
    query = _flat(query)
    # Only a finding still awaiting review is decided, in one guarded write.
    assert "SET review_status=$2,reviewed_by=$3,reviewed_at=now()," in query
    assert "WHERE id=$1::uuid AND review_status='required'" in query
    # Tenancy is RLS's job, never a duplicated predicate here.
    assert "tenant_id" not in _predicate(lock) and "tenant_id" not in _predicate(query)
    assert args[:4] == (str(FINDING), "confirmed", OWNER.agent_id, REASON)


def test_the_reason_is_written_only_sealed_under_the_findings_tenant_key(monkeypatch):
    """CONTRACT C5.1: the reason may name an owner and an encrypted path
    exists, so no plaintext copy is written (0167 has no plaintext column)."""
    _result, conn, _ledger = _review(monkeypatch, OWNER)

    query, args = conn.queries[-1]
    query = _flat(query)
    assert "review_reason_ciphertext=pgp_sym_encrypt($4::text,$5::text,'s2k-mode=1')" in query
    assert "review_reason=" not in query
    assert args[4] == sealed_text.key_for(TENANT_ID)


def test_without_the_master_key_nothing_is_decided(monkeypatch):
    error, conn, ledger = _review(monkeypatch, OWNER, master="")

    assert isinstance(error, sealed_text.SealError)
    assert not any("UPDATE title_findings" in q for q, _ in conn.queries)
    assert ledger.calls == []


def test_a_broker_owner_dismisses_a_finding(monkeypatch):
    result, _conn, ledger = _review(monkeypatch, OWNER, decision="dismissed")

    assert result["review_status"] == "dismissed"
    assert ledger.calls[0]["action"] == "title_finding_dismissed"


def test_a_platform_admin_may_decide(monkeypatch):
    result, _conn, _ledger = _review(monkeypatch, ADMIN)

    assert result["review_status"] == "confirmed"


def test_a_platform_admins_decision_is_sealed_and_audited_in_the_findings_tenant(monkeypatch):
    """RLS lets a platform admin decide another tenant's finding. That tenant's
    client sees the change, so its audit trail records it and its key seals the
    reason: never the admin's context tenant."""
    _result, conn, ledger = _review(monkeypatch, ADMIN, decision="dismissed", row_tenant=OTHER_TENANT)

    [call] = ledger.calls
    assert call["tenant_id"] == str(OTHER_TENANT)
    _query, args = conn.queries[-1]
    assert args[4] == sealed_text.key_for(OTHER_TENANT) != sealed_text.key_for(TENANT_ID)


def test_a_second_decision_is_a_409_naming_the_current_status(monkeypatch):
    error, conn, ledger = _review(monkeypatch, OWNER, decision="dismissed", current="confirmed")

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert "confirmed" in error.detail
    assert not any("UPDATE title_findings" in q for q, _ in conn.queries)
    assert ledger.calls == []


def test_a_finding_the_caller_cannot_see_is_a_404(monkeypatch):
    error, conn, ledger = _review(monkeypatch, OWNER, current=None)

    assert isinstance(error, HTTPException) and error.status_code == 404
    [(lookup, _args)] = conn.queries
    assert "tenant_id" not in _predicate(lookup)
    assert ledger.calls == []


def test_the_reason_is_required():
    with pytest.raises(ValidationError):
        intelligence_api.TitleFindingReview(decision="confirmed")


@pytest.mark.parametrize("reason", ["", "        ", "fine ok"])
def test_a_blank_or_token_reason_is_refused_before_any_write(monkeypatch, reason):
    error, conn, ledger = _review(monkeypatch, OWNER, reason=reason)

    assert isinstance(error, HTTPException) and error.status_code == 422
    assert conn.queries == [] and ledger.calls == []


@pytest.mark.parametrize("decision", ["required", "verified", "approved"])
def test_only_confirmed_or_dismissed_is_a_decision(decision):
    with pytest.raises(ValidationError):
        intelligence_api.TitleFindingReview(decision=decision, reason=REASON)


def test_the_audit_record_carries_the_decision_and_actor_but_no_free_text(monkeypatch):
    _result, _conn, ledger = _review(monkeypatch, OWNER)

    [call] = ledger.calls
    assert call["category"] is AuditCategory.USER_STATE_CHANGE
    assert call["action"] == "title_finding_confirmed"
    assert call["tenant_id"] == TENANT_ID
    assert call["user_id"] == OWNER.agent_id
    assert call["target_id"] == str(FINDING)
    assert call["metadata"] == {"finding_id": str(FINDING), "decision": "confirmed"}
    # Nothing a person typed, and no owner name, reaches the plaintext ledger.
    blob = json.dumps(call, default=str)
    for text in (REASON, "Jane", "Homeowner", "88 Oak", "judgment v.", "DE-NCC-0142"):
        assert text not in blob


def test_the_brokerage_export_opens_the_sealed_reason():
    """bytea is never exported, so a sealed column the export cannot open
    would silently vanish from the brokerage's own export."""
    import privacy_export

    assert privacy_export.DECRYPT["title_findings"] == {"review_reason_ciphertext": "sealed_review_reason"}


def test_0167_adds_only_the_sealed_reason():
    sql = (Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0167_title_finding_review_reason_sealed.sql").read_text()
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "ADD COLUMN IF NOT EXISTS review_reason_ciphertext bytea" in body
    assert "review_reason text" not in body and "ADD COLUMN IF NOT EXISTS review_reason " not in body
