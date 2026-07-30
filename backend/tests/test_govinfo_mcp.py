"""Tests for the server-side-only official GovInfo MCP bridge."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import govinfo_mcp
from govinfo_mcp import GovInfoSearchRequest, get_govinfo_document, govinfo_status, search_govinfo
from tenancy import Role, TenantContext


CTX = TenantContext(
    agent_id="broker@tenant.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.BROKER_OWNER,
)


def _search_payload():
    return {
        "results": [
            {
                "title": "<b>Official</b> housing regulation",
                "packageId": "CFR-2026-title24-vol1",
                "granuleId": "CFR-2026-title24-vol1-sec35-92",
                "collectionCode": "CFR",
                "dateIssued": "2026-01-01",
                "teaser": "This must never be returned to the browser.",
                "detailsLink": "https://www.govinfo.gov/app/details/CFR-2026-title24-vol1/CFR-2026-title24-vol1-sec35-92",
            },
            {
                "title": "Untrusted destination",
                "packageId": "CFR-2026-title24-vol1",
                "detailsLink": "https://example.test/redirect",
            },
        ]
    }


def test_status_never_exposes_the_government_api_key(monkeypatch):
    monkeypatch.setenv("GOVINFO_API_KEY", "private-key")
    monkeypatch.setattr(govinfo_mcp, "require_feature", lambda _feature: None)

    result = asyncio.run(govinfo_status(CTX))

    assert result["available"] is True
    assert "private-key" not in str(result)
    assert result["endpoint"] == "https://api.govinfo.gov/mcp"


def test_search_uses_a_fixed_tool_and_returns_only_safe_metadata(monkeypatch):
    calls = []

    async def fake_call(name, arguments):
        calls.append((name, arguments))
        return _search_payload()

    monkeypatch.setattr(govinfo_mcp, "require_feature", lambda _feature: None)
    monkeypatch.setattr(govinfo_mcp, "_call_tool", fake_call)

    result = asyncio.run(search_govinfo(GovInfoSearchRequest(query="  housing\nregulation  ", page_size=3), CTX))

    assert calls == [("searchGovInfo", {"userQuery": "housing regulation", "pageSize": "3"})]
    assert result["results"] == [
        {
            "access_id": "CFR-2026-title24-vol1-sec35-92",
            "title": "Official housing regulation",
            "collection_code": "CFR",
            "date_issued": "2026-01-01",
            "details_url": "https://www.govinfo.gov/app/details/CFR-2026-title24-vol1/CFR-2026-title24-vol1-sec35-92",
        }
    ]
    assert "teaser" not in str(result)


def test_document_resolution_accepts_only_an_official_govinfo_pdf(monkeypatch):
    async def fake_call(name, arguments):
        assert name == "describePackageOrGranule"
        assert arguments == {"accessId": "CFR-2026-title24-vol1-sec35-92"}
        return {
            "Title": "Official regulation",
            "packageid": "CFR-2026-title24-vol1",
            "granuleid": "CFR-2026-title24-vol1-sec35-92",
            "pdfurl": "https://www.govinfo.gov/content/pkg/CFR-2026-title24-vol1/pdf/CFR-2026-title24-vol1-sec35-92.pdf",
        }

    monkeypatch.setattr(govinfo_mcp, "require_feature", lambda _feature: None)
    monkeypatch.setattr(govinfo_mcp, "_call_tool", fake_call)

    result = asyncio.run(get_govinfo_document("CFR-2026-title24-vol1-sec35-92", CTX))

    assert result["pdf_url"].startswith("https://www.govinfo.gov/content/pkg/")
    assert result["details_url"].startswith("https://www.govinfo.gov/app/details/")


def test_document_resolution_drops_a_non_govinfo_pdf(monkeypatch):
    async def fake_call(_name, _arguments):
        return {"Title": "Bad link", "pdfurl": "https://example.test/document.pdf"}

    monkeypatch.setattr(govinfo_mcp, "require_feature", lambda _feature: None)
    monkeypatch.setattr(govinfo_mcp, "_call_tool", fake_call)

    result = asyncio.run(get_govinfo_document("CFR-2026-title24-vol1", CTX))

    assert result["pdf_url"] is None


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)

    with pytest.raises(HTTPException) as error:
        asyncio.run(govinfo_mcp._call_tool("searchGovInfo", {"userQuery": "housing"}))

    assert error.value.status_code == 503
