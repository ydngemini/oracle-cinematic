"""Read-only bridge to the U.S. Government Publishing Office GovInfo MCP.

The upstream endpoint is the official GPO public-preview service at
``https://api.govinfo.gov/mcp``.  This router deliberately exposes only its
search and document-retrieval capabilities.  It never sends tenant records,
draft content, or Personal AI prompts upstream, and it does not return the
government API key to the browser.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from platform_policy import Feature, require_feature
from tenancy import TenantContext, require_context


router = APIRouter(prefix="/api/contracts/govinfo", tags=["contracts", "govinfo"])
logger = logging.getLogger("oracle.govinfo_mcp")

# The endpoint and protocol version are published by the U.S. Government
# Publishing Office.  Keeping the host fixed prevents an environment setting
# from quietly turning this public-data bridge into an SSRF proxy.
_GOVINFO_MCP_URL = "https://api.govinfo.gov/mcp"
_GOVINFO_MCP_PROTOCOL_VERSION = "2025-03-26"
_GOVINFO_DOCS_URL = "https://github.com/usgpo/api/blob/main/docs/mcp.md"
_HTTP_TIMEOUT_SECONDS = 15.0
_MAX_UPSTREAM_RESPONSE_BYTES = 1_500_000
_MAX_RESULTS = 10
_ACCESS_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,220}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_GOVINFO_HOSTS = frozenset({"govinfo.gov", "www.govinfo.gov"})


class GovInfoSearchRequest(BaseModel):
    """A deliberately small request surface for a public federal search."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=2, max_length=320)
    page_size: int = Field(default=8, ge=1, le=_MAX_RESULTS)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        cleaned = " ".join(_CONTROL_CHARS_RE.sub(" ", value).split())
        if len(cleaned) < 2:
            raise ValueError("query must include at least two visible characters")
        return cleaned


def _govinfo_api_key() -> str:
    """Use a dedicated key when present, otherwise the existing data.gov key."""
    return (
        os.getenv("GOVINFO_API_KEY", "").strip()
        or os.getenv("DATA_GOV_API_KEY", "").strip()
    )


def _configuration_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Federal source search is not configured.",
    )


def _safe_govinfo_url(value: Any, *, pdf: bool = False) -> str | None:
    """Allow only HTTPS links returned by the official GovInfo host."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname not in _GOVINFO_HOSTS:
        return None
    if pdf:
        if not parsed.path.startswith("/content/pkg/") or not parsed.path.lower().endswith(".pdf"):
            return None
    elif not parsed.path.startswith("/app/details/"):
        return None
    return raw


def _clean_text(value: Any, *, limit: int) -> str:
    text = html.unescape(_HTML_TAG_RE.sub(" ", str(value or "")))
    return " ".join(text.split())[:limit]


def _extract_mcp_json(payload: Any) -> dict[str, Any]:
    """Decode the official server's text-wrapped JSON tool responses."""
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo returned an invalid response.")
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo could not complete the request.")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            nested = decoded.get("structuredContent")
            return nested if isinstance(nested, dict) else decoded
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo returned no usable document data.")


async def _call_tool(name: str, arguments: dict[str, str]) -> dict[str, Any]:
    """Issue one authenticated, read-only JSON-RPC MCP tool call.

    GovInfo's documented Streamable HTTP endpoint accepts stateless tool calls.
    This avoids retaining sessions or request content across tenants.
    """
    api_key = _govinfo_api_key()
    if not api_key:
        raise _configuration_error()

    request_id = str(uuid.uuid4())
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _GOVINFO_MCP_PROTOCOL_VERSION,
        "x-api-key": api_key,
    }
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            response = await client.post(_GOVINFO_MCP_URL, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        logger.info("GovInfo MCP request timed out.")
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "GovInfo did not respond in time.") from exc
    except httpx.HTTPError as exc:
        logger.info("GovInfo MCP request failed: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo is unavailable.") from exc

    if response.status_code in {401, 403}:
        logger.warning("GovInfo MCP rejected the configured API key (status=%d).", response.status_code)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Federal source search is unavailable.")
    if response.status_code == 429:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Federal source search is busy. Try again shortly.")
    if response.status_code >= 400:
        logger.info("GovInfo MCP returned status=%d.", response.status_code)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo could not complete the request.")

    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_UPSTREAM_RESPONSE_BYTES:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo returned an oversized response.")
    if len(response.content) > _MAX_UPSTREAM_RESPONSE_BYTES:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo returned an oversized response.")
    try:
        return _extract_mcp_json(response.json())
    except json.JSONDecodeError as exc:
        logger.info("GovInfo MCP returned non-JSON content.")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GovInfo returned an invalid response.") from exc


def _normalize_search_results(payload: dict[str, Any], *, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for candidate in payload.get("results") or []:
        if not isinstance(candidate, dict):
            continue
        package_id = _clean_text(candidate.get("packageId"), limit=220)
        granule_id = _clean_text(candidate.get("granuleId"), limit=220)
        access_id = granule_id or package_id
        details_url = _safe_govinfo_url(candidate.get("detailsLink"))
        if not access_id or not _ACCESS_ID_RE.fullmatch(access_id) or not details_url:
            continue
        title = _clean_text(candidate.get("title"), limit=320) or access_id
        result = {
            "access_id": access_id,
            "title": title,
            "collection_code": _clean_text(candidate.get("collectionCode"), limit=64),
            "date_issued": _clean_text(candidate.get("dateIssued"), limit=32),
            "details_url": details_url,
        }
        results.append(result)
        if len(results) >= limit:
            break
    return results


@router.get("/status")
async def govinfo_status(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    """Expose configuration state without disclosing any upstream credential."""
    require_feature(Feature.CONTRACTS)
    return {
        "available": bool(_govinfo_api_key()),
        "provider": "GovInfo MCP",
        "authority": "U.S. Government Publishing Office",
        "endpoint": _GOVINFO_MCP_URL,
        "documentation_url": _GOVINFO_DOCS_URL,
        "scope": "Official federal publications and metadata.",
        "form_notice": "Federal source research only; it is not a state transaction-form provider.",
    }


@router.post("/search")
async def search_govinfo(
    body: GovInfoSearchRequest,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Search official federal publications through the GPO's MCP server."""
    require_feature(Feature.CONTRACTS)
    payload = await _call_tool(
        "searchGovInfo",
        {"userQuery": body.query, "pageSize": str(body.page_size)},
    )
    return {
        "provider": "GovInfo MCP",
        "results": _normalize_search_results(payload, limit=body.page_size),
    }


@router.get("/documents/{access_id}")
async def get_govinfo_document(
    access_id: str,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Resolve one official package/granule to its GovInfo PDF, when offered."""
    require_feature(Feature.CONTRACTS)
    if not _ACCESS_ID_RE.fullmatch(access_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid GovInfo document identifier.")
    payload = await _call_tool("describePackageOrGranule", {"accessId": access_id})
    package_id = _clean_text(payload.get("packageid") or payload.get("packageId"), limit=220)
    granule_id = _clean_text(payload.get("granuleid") or payload.get("granuleId"), limit=220)
    if package_id and not _ACCESS_ID_RE.fullmatch(package_id):
        package_id = ""
    if granule_id and not _ACCESS_ID_RE.fullmatch(granule_id):
        granule_id = ""
    details_url = (
        f"https://www.govinfo.gov/app/details/{package_id}/{granule_id}"
        if package_id and granule_id
        else None
    )
    return {
        "provider": "GovInfo MCP",
        "title": _clean_text(payload.get("Title") or payload.get("title"), limit=320) or access_id,
        "access_id": granule_id or package_id or access_id,
        "details_url": _safe_govinfo_url(details_url),
        "pdf_url": _safe_govinfo_url(payload.get("pdfurl") or payload.get("pdfUrl"), pdf=True),
    }
