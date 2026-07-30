"""Coverage for the 50-state contract and document selection catalog."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import state_compliance.routes_reference as references
from state_compliance._common import ALL_STATE_CODES
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)


class _LibraryConn:
    async def fetchrow(self, query, *args):
        assert args == ("TX",)
        assert "state_regulatory_profiles" in query
        return {
            "state_name": "Texas",
            "regulatory_url": "https://example.test/tx",
            "attorney_review_required": False,
        }

    async def fetch(self, query, *args):
        if "state_disclosure_forms" in query:
            assert args == ("TX",)
            return [{
                "id": "form-1",
                "form_name": "Texas Seller Disclosure",
                "form_type": "seller_disclosure",
                "required_when": "Before contract execution",
                "effective_date": None,
                "download_url": "https://example.test/tx/seller-disclosure",
                "notes": "Use the current state source.",
            }]
        if "state_contract_templates" in query:
            assert args == ("TX",)
            return [{
                "id": "contract-1",
                "template_name": "Texas Residential Contract",
                "association": "TREC",
                "property_types": ["residential"],
                "version": "2026.1",
                "effective_date": None,
                "download_url": None,
            }]
        if "tenant_contract_template_registrations" in query:
            assert args == (TENANT_ID, "TX")
            return [{
                "id": "tenant-template-1",
                "template_key": "seller-purchase-standard",
                "document_type": "seller_purchase",
                "jurisdiction": "US-GENERIC",
                "version": "1.0.0",
                "source_status": "approved",
                "source_ref": "main@abc123",
            }]
        raise AssertionError(f"Unexpected query: {query}")


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


def test_compliance_catalog_has_document_references_for_every_state():
    missing = [
        code
        for code in ALL_STATE_CODES
        if not references._compliance_reference_items(code, attorney_review_required=False)
    ]

    assert missing == []


def test_state_document_library_combines_tenant_state_and_cited_sources(monkeypatch):
    monkeypatch.setattr(references, "tenant_tx", _fake_tenant_tx(_LibraryConn()))

    library = asyncio.run(references.get_state_document_library("tx", CTX))

    assert library.state_name == "Texas"
    assert library.total_contracts >= 2
    assert library.total_documents >= 1
    assert any(item.item_id == "tenant-template:tenant-template-1" for item in library.items)
    assert any(item.item_id == "state-contract:contract-1" for item in library.items)
    assert any(item.item_id == "state-form:form-1" and item.download_url for item in library.items)
    assert any(
        item.item_id == "compliance:FEDERAL-LEAD-PAINT-001"
        and item.download_url
        for item in library.items
    )
    assert all(not hasattr(item, "body_template") for item in library.items)


def test_state_document_library_rejects_non_https_source_links(monkeypatch):
    conn = _LibraryConn()

    async def unsafe_fetch(query, *args):
        rows = await _LibraryConn().fetch(query, *args)
        if "state_disclosure_forms" in query:
            rows[0]["download_url"] = "javascript:alert(1)"
        return rows

    conn.fetch = unsafe_fetch
    monkeypatch.setattr(references, "tenant_tx", _fake_tenant_tx(conn))

    library = asyncio.run(references.get_state_document_library("TX", CTX))

    form = next(item for item in library.items if item.item_id == "state-form:form-1")
    assert form.download_url is None
    assert form.selection_status == "review_required"
