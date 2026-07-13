import asyncio
from uuid import UUID

import pytest
from fastapi import HTTPException

import media_api
from tenancy import Role, TenantContext


CTX = TenantContext(
    agent_id="agent-1",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


def test_media_persist_rejects_unbounded_file_count():
    files = [object()] * (media_api.MAX_FILES_PER_UPLOAD + 1)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            media_api._persist(
                object(),
                tenant_id=CTX.tenant_id,
                lead_id=None,
                listing_id=None,
                files=files,
            )
        )

    assert exc.value.status_code == 413


def test_contract_generation_rate_limit_is_per_actor(monkeypatch):
    media_api._contract_generation_timestamps.clear()
    monkeypatch.setattr(media_api, "_CONTRACT_RATE_LIMIT", 2)
    monkeypatch.setattr(media_api.time, "monotonic", lambda: 100.0)

    media_api._check_contract_generation_rate(CTX)
    media_api._check_contract_generation_rate(CTX)
    with pytest.raises(HTTPException) as exc:
        media_api._check_contract_generation_rate(CTX)

    assert exc.value.status_code == 429


def test_contract_data_failure_does_not_expose_internal_error(monkeypatch):
    from ml_forge import synthetic_lawyer

    media_api._contract_generation_timestamps.clear()

    async def fail(*_args, **_kwargs):
        raise RuntimeError("database host and internal details")

    monkeypatch.setattr(synthetic_lawyer, "generate_assignment_contract_for_client", fail)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            media_api.generate_assignment_contract(
                UUID("22222222-2222-4222-8222-222222222222"),
                expiration_seconds=3600,
                ctx=CTX,
            )
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "Contract data service unavailable."
    assert "database host" not in exc.value.detail
