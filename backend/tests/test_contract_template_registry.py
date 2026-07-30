"""Regression coverage for tenant contract-source registration."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import contracts_api
from contracts_api import list_templates
from ml_forge.synthetic_lawyer import BUILTIN_CONTRACT_TEMPLATES, template_sha256
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
MIGRATION = Path(__file__).parents[1] / "db" / "migrations" / "0031_tenant_contract_template_registry.sql"


class _RegistryConn:
    def __init__(self):
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return [
            {
                "id": "template-registration-id",
                "template_key": "assignment-standard",
                "version": "1.0.0",
                "document_type": "assignment",
                "jurisdiction": "US-GENERIC",
                "status": "registered",
                "template_sha256": "8f99726faf31ebba04aa13ee48fae7858b0823e280063a5c4ddcfffc993287fb",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "source_control": json.dumps(
                    {"status": "approved", "kind": "version_controlled"}
                ),
            }
        ]


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


def test_contract_source_migration_registers_every_builtin_for_all_tenants():
    migration = MIGRATION.read_text()
    assert "CROSS JOIN contract_template_sources" in migration
    assert "AFTER INSERT ON tenants" in migration
    assert "attorney approval" in migration
    for key, template in BUILTIN_CONTRACT_TEMPLATES.items():
        assert key in migration
        assert template["version"] in migration
        assert template_sha256(template["body_template"]) in migration


def test_template_list_is_scoped_to_current_tenant_and_exposes_source_control(monkeypatch):
    conn = _RegistryConn()
    monkeypatch.setattr(contracts_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)

    result = asyncio.run(list_templates(CTX))

    assert conn.args == (TENANT_ID,)
    assert "registration.tenant_id = $1::uuid" in conn.query
    assert result["registry"]["registered_for_tenant"] is True
    assert result["registry"]["source_control"] == "approved"
    assert result["templates"][0]["status"] == "registered"
    assert result["templates"][0]["source_control"]["status"] == "approved"
