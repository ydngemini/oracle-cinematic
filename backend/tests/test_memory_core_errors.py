"""A database or sealing failure answers 503 with a fixed sentence: the
exception text never reaches the browser, and only its type reaches the log.

What those handlers catch is RuntimeError from the pool or the crypto layer,
and its text names configuration ("ORACLE_ENCRYPTION_MASTER_KEY is not set",
the database user, the table that would not decrypt). Forty-one routes used to
send it as "Memory Core offline (<text>)"."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from memory_core_errors import memory_core_offline
from tenancy import Role, TenantContext

BACKEND = Path(__file__).resolve().parents[1]
SECRET_SHAPED = "ORACLE_ENCRYPTION_MASTER_KEY is not set for user neoh_app on table interaction_logs"
AGENT = TenantContext(agent_id="agent@offline.test", tenant_id="00000000-0000-0000-0000-0000000000a1",
                      role=Role.AGENT)


def test_the_detail_is_fixed_and_the_log_names_types_only(caplog):
    try:
        try:
            raise OSError("pool host db.internal:25060 refused")
        except OSError as inner:
            raise RuntimeError(SECRET_SHAPED) from inner
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="oracle.memory_core"):
            plain = memory_core_offline(exc)
            saved = memory_core_offline(exc, "note not persisted")
    assert (plain.status_code, plain.detail) == (503, "Memory Core offline.")
    assert (saved.status_code, saved.detail) == (503, "Memory Core offline — note not persisted.")
    logged = caplog.text
    assert "RuntimeError caused by OSError" in logged
    assert "note not persisted" in logged
    for fragment in ("ORACLE_ENCRYPTION_MASTER_KEY", "neoh_app", "interaction_logs", "db.internal"):
        assert fragment not in logged


def test_a_route_sends_no_exception_text(monkeypatch):
    """Through a real handler: the CRM touch history over a tenant_tx that
    raises the crypto layer's own message."""
    import crm

    @asynccontextmanager
    async def broken(_ctx):
        raise RuntimeError(SECRET_SHAPED)
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(crm, "tenant_tx", broken)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(crm.client_interactions("00000000-0000-0000-0000-0000000000c1", 50, AGENT))
    assert (exc.value.status_code, exc.value.detail) == (503, "Memory Core offline.")


def test_a_misconfigured_texting_provider_names_no_setting(monkeypatch):
    import messaging_api

    monkeypatch.setenv("ORACLE_MESSAGING_PROVIDER", "carrier-pigeon")
    owner = TenantContext(agent_id="owner@offline.test", tenant_id=AGENT.tenant_id, role=Role.BROKER_OWNER)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(messaging_api.refresh_registration(owner))
    assert (exc.value.status_code, exc.value.detail) == (503, "Texting is not configured on this server.")


def test_no_route_formats_an_exception_into_the_offline_detail():
    """The fixed sentence comes from memory_core_errors; a new handler that
    pastes `{exc}` into it would undo the point."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in ("venv", "node_modules", "pascal-editor", "tests") for part in path.parts):
            continue
        if path.name == "memory_core_errors.py":  # formats the route's outcome, never an exception
            continue
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if re.search(r"Memory Core offline[^\"\n]*\{", line):
                offenders.append(f"{path.relative_to(BACKEND)}:{n}")
    assert not offenders, offenders
