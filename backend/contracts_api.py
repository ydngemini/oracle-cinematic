"""Versioned, reviewed legal drafts stored only as encrypted content and PDFs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from approval_service import approval_dict
from audit_ledger import AuditCategory, ledger
from automation_jobs import (
    JobApprovalError,
    canonical_json,
    enqueue_job,
    payload_hash,
    register_handler,
)
from crypto import CryptoError, decrypt_pii, derive_tenant_key, encrypt_pii
from db.connection import tenant_tx
from ml_forge.synthetic_lawyer import (
    BUILTIN_CONTRACT_TEMPLATES,
    defensive_redline,
    render_approved_contract_template,
    render_contract_workspace_draft,
    template_sha256,
    validate_contract_template,
    write_contract_pdf,
)
from policy_contract import account_security_esa_pdf_text, ACCOUNT_SECURITY_ESA_VERSION
from platform_policy import (
    ActionRisk,
    Feature,
    enforce_public_property_data,
    require_feature,
    validate_approval_reason,
)
from tenancy import Role, TenantContext, require_context, require_role

router = APIRouter(prefix="/api/contracts", tags=["contracts"])

DocumentType = Literal[
    "assignment",
    "seller_purchase",
    "buyer_purchase",
    "joint_venture",
    "redline",
    "account_security_esa",
    "buyer_representation",
    "buyer_offer",
    "inspection_repair_request",
    "financing_contingency_addendum",
    "listing_agreement",
    "seller_disclosure",
    "counteroffer_addendum",
    "termination_release",
]
_WORKSPACE_INPUT_MAX_BYTES = 160_000
_REGISTERED_PDF_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
_SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,119}$")
_GOVERNMENT_PDF_USER_AGENT = "Mozilla/5.0 (compatible; NEOH/1.0; +https://neoh.com)"
_CONTENT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRAFT_ANCHOR_NOT_FOUND = "Contract draft anchor not found."
_ANCHOR_SUFFIXES = (
    ("transaction_id", "transaction"),
    ("property_id", "property"),
    ("listing_id", "listing"),
    ("client_id", "client"),
    ("lead_id", "lead"),
)
logger = logging.getLogger(__name__)

_PDF_TEMPLATE_TITLES = {
    "assignment-standard": "Assignment Agreement",
    "buyer-purchase-standard": "Buyer Purchase Agreement",
    "defensive-redline-standard": "Defensive Redline Review",
    "joint-venture-standard": "Joint Venture Agreement",
    "seller-purchase-standard": "Seller Purchase Agreement",
    "account-security-esa": "NEOH™ Account Security ESA",
}

_ACCOUNT_SECURITY_ESA_TEMPLATE_KEY = "account-security-esa"
_ACCOUNT_SECURITY_ESA_TEMPLATE_VERSION = "1.0.0"


def _account_security_esa_template() -> str:
    return account_security_esa_pdf_text()

# Keep the compact PDF picker complete even during a rolling database
# migration.  These are the 50 U.S. states only; District of Columbia remains
# available through the state-compliance API but is intentionally not folded
# into a control labelled "50 states".
_FIFTY_STATE_OPTIONS = (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
)

# Direct, public PDFs only. The browser receives these links; the server never
# fetches arbitrary user-supplied URLs on behalf of a tenant.
_OFFICIAL_PDF_SOURCES = (
    {
        "id": "official:epa-lead-seller-en",
        "source_key": "epa-lead-seller-disclosure-en",
        "authority_scope": "federal",
        "group": "Official public PDFs",
        "kind": "document",
        "title": "Lead-Based Paint Seller Disclosure",
        "subtitle": "US federal · Form 9600-040 · English",
        "source_name": "U.S. Environmental Protection Agency",
        "source_url": "https://www.epa.gov/lead/lead-based-paint-disclosure-rule-section-1018-title-x",
        "pdf_url": "https://www.epa.gov/sites/default/files/documents/selr_eng.pdf",
        "download_url": "/api/contracts/pdf-library/registered/epa-lead-seller-disclosure-en/download",
        "delivery": "external_pdf",
    },
    {
        "id": "official:epa-lead-lessor-en",
        "source_key": "epa-lead-lessor-disclosure-en",
        "authority_scope": "federal",
        "group": "Official public PDFs",
        "kind": "document",
        "title": "Lead-Based Paint Lessor Disclosure",
        "subtitle": "US federal · Form 9600-041 · English",
        "source_name": "U.S. Environmental Protection Agency",
        "source_url": "https://www.epa.gov/lead/lead-based-paint-disclosure-rule-section-1018-title-x",
        "pdf_url": "https://www.epa.gov/sites/default/files/documents/lesr_eng.pdf",
        "download_url": "/api/contracts/pdf-library/registered/epa-lead-lessor-disclosure-en/download",
        "delivery": "external_pdf",
    },
)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _public_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("content_ciphertext", None)
    result.pop("body_template", None)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key in {"metadata", "required_fields", "source_control"}:
            result[key] = _json(value)
    return result


def _tenant_key(ctx: TenantContext) -> str:
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Legal draft encryption is not configured.",
        )
    return derive_tenant_key(ctx.tenant_id, master)


def _anchor_kind(key: Any) -> Optional[str]:
    normalized = str(key or "").strip().lower()
    for suffix, kind in _ANCHOR_SUFFIXES:
        if normalized == suffix or normalized.endswith(f"_{suffix}"):
            return kind
    return None


def _collect_draft_anchors(*sources: Any) -> dict[str, set[uuid.UUID]]:
    """Collect every recognized record anchor, including nested draft inputs."""
    anchors: dict[str, set[uuid.UUID]] = {
        "client": set(),
        "lead": set(),
        "listing": set(),
        "property": set(),
        "transaction": set(),
    }

    def add(kind: str, value: Any) -> None:
        if value is None:
            return
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for candidate in values:
            try:
                anchors[kind].add(uuid.UUID(str(candidate)))
            except (AttributeError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_DRAFT_ANCHOR_NOT_FOUND,
                ) from exc

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, candidate in value.items():
                kind = _anchor_kind(key)
                if kind is not None:
                    add(kind, candidate)
                else:
                    visit(candidate)
        elif isinstance(value, (list, tuple)):
            for candidate in value:
                visit(candidate)

    for source in sources:
        visit(source)
    return anchors


async def _validate_draft_anchors(
    conn: Any,
    ctx: TenantContext,
    *sources: Any,
) -> dict[str, set[uuid.UUID]]:
    """Resolve supplied anchors only inside the authenticated tenant.

    A platform-admin database session can bypass RLS, so every lookup carries
    the authenticated tenant explicitly.  Missing and cross-tenant UUIDs share
    one response to avoid turning this endpoint into a record oracle.
    """
    anchors = _collect_draft_anchors(*sources)
    queries = {
        "client": """
            SELECT id FROM clients
             WHERE id=$1::uuid AND tenant_id=$2::uuid
             FOR KEY SHARE
        """,
        "lead": """
            SELECT id FROM leads
             WHERE id=$1::uuid AND tenant_id=$2::uuid
             FOR KEY SHARE
        """,
        "listing": """
            SELECT id FROM listings
             WHERE id=$1::uuid AND tenant_id=$2::uuid
             FOR KEY SHARE
        """,
        "transaction": """
            SELECT id FROM transactions
             WHERE id=$1::uuid AND tenant_id=$2::uuid
             FOR KEY SHARE
        """,
        "property": """
            SELECT $1::uuid AS id
             WHERE EXISTS (
                       SELECT 1 FROM leads
                        WHERE id=$1::uuid AND tenant_id=$2::uuid
                   )
                OR EXISTS (
                       SELECT 1 FROM listings
                        WHERE id=$1::uuid AND tenant_id=$2::uuid
                   )
                OR EXISTS (
                       SELECT 1 FROM transactions
                        WHERE property_id=$1::uuid AND tenant_id=$2::uuid
                   )
        """,
    }
    for kind, identifiers in anchors.items():
        for identifier in identifiers:
            row = await conn.fetchrow(queries[kind], identifier, ctx.tenant_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_DRAFT_ANCHOR_NOT_FOUND,
                )
    return anchors


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
    document_type: DocumentType
    jurisdiction: str = Field(min_length=2, max_length=80)
    body_template: str = Field(min_length=20, max_length=100_000)
    required_fields: list[str] = Field(min_length=1, max_length=50)


class TemplateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approved", "rejected"]
    attorney_reviewed_by: str = Field(min_length=3, max_length=200)
    reason: str = Field(min_length=8, max_length=500)


class DocumentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_id: uuid.UUID
    client_id: Optional[uuid.UUID] = None
    lead_id: Optional[uuid.UUID] = None
    listing_id: Optional[uuid.UUID] = None
    property_id: Optional[uuid.UUID] = None
    transaction_id: Optional[uuid.UUID] = None
    vault_client_id: uuid.UUID
    attorney_reviewer: str = Field(min_length=3, max_length=200)
    inputs: dict[str, Any]

    @model_validator(mode="after")
    def anchor_required(self) -> "DocumentDraft":
        if not self.lead_id and not self.transaction_id:
            raise ValueError("lead_id or transaction_id is required")
        enforce_public_property_data(self.inputs)
        return self


class DocumentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revised_text: str = Field(min_length=20, max_length=200_000)
    attorney_reviewer: str = Field(min_length=3, max_length=200)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=8, max_length=500)
    approval_id: Optional[uuid.UUID] = None
    content_sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: Optional[int] = Field(default=None, ge=1)


class SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    signature_reference: str = Field(min_length=4, max_length=500)
    reason: str = Field(min_length=8, max_length=500)


class DraftWorkspaceCreate(BaseModel):
    """A tenant-private, editable draft before it becomes a legal document."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inputs(self) -> "DraftWorkspaceCreate":
        enforce_public_property_data(self.inputs)
        _validate_workspace_input_size(self.inputs)
        return self


class DraftWorkspaceCompletion(BaseModel):
    """Known values that Personal AI may merge into its saved workspace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    inputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inputs(self) -> "DraftWorkspaceCompletion":
        enforce_public_property_data(self.inputs)
        _validate_workspace_input_size(self.inputs)
        return self


def _validate_workspace_input_size(inputs: dict[str, Any]) -> None:
    try:
        encoded = canonical_json(inputs).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Draft inputs must be JSON-serializable.") from exc
    if len(encoded) > _WORKSPACE_INPUT_MAX_BYTES:
        raise ValueError("Draft inputs are too large.")


def _workspace_required_fields(candidate: dict[str, Any]) -> list[str]:
    if candidate["document_type"] == "redline":
        return ["original_text", "proposed_text"]
    return list(candidate["required_fields"])


def _builtin_workspace_template(template_key: str) -> dict[str, Any]:
    if template_key == _ACCOUNT_SECURITY_ESA_TEMPLATE_KEY:
        body_template = _account_security_esa_template()
        return {
            "template_key": template_key,
            "document_type": "account_security_esa",
            "jurisdiction": "NEOH™",
            "version": _ACCOUNT_SECURITY_ESA_TEMPLATE_VERSION,
            "body_template": body_template,
            "required_fields": [],
            "template_sha256": template_sha256(body_template),
        }
    candidate = BUILTIN_CONTRACT_TEMPLATES.get(template_key)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Built-in contract form not found.")
    return {
        "template_key": template_key,
        **candidate,
        "template_sha256": template_sha256(candidate["body_template"]),
    }


def _pdf_template_title(template_key: str) -> str:
    return _PDF_TEMPLATE_TITLES.get(
        template_key,
        template_key.replace("-", " ").title(),
    )


def _safe_direct_pdf_url(value: Any) -> Optional[str]:
    """Keep only legacy URLs whose direct-PDF type can be inferred by suffix."""
    url = _safe_https_url(value)
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.path.lower().endswith(".pdf"):
        return None
    return url


def _safe_https_url(value: Any) -> Optional[str]:
    """Keep only absolute HTTPS URLs that are safe to offer as browser links."""
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return url


def _safe_government_pdf_url(value: Any) -> Optional[str]:
    """Keep only direct HTTPS PDFs hosted on a government domain.

    This is intentionally stricter than the browser-link helper because this
    URL is fetched by the backend for a device download.  It prevents the
    source registry from becoming a general-purpose server-side request proxy.
    """
    url = _safe_https_url(value)
    if not url:
        return None
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname != "gov" and not hostname.endswith(".gov"):
        return None
    return url


async def _approved_pdf_library_items(ctx: TenantContext) -> list[dict[str, Any]]:
    """Read manually verified government PDF registrations.

    The registry stores the verified MIME type, so a direct PDF endpoint that
    does not use a ``.pdf`` filename (for example, New York DOS) remains valid
    without weakening the legacy filename-based fallback.
    """
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT source_key, authority_scope, state_code, document_kind, title,
                       subtitle, source_name, source_url, pdf_url, version, effective_date
                FROM authorized_document_sources
                WHERE approval_status = 'approved'
                  AND media_type = 'application/pdf'
                ORDER BY authority_scope, state_code NULLS FIRST, title
                """
            )
    except Exception as exc:  # Supports deployments that have not applied 0033 yet.
        logger.debug("Authorized PDF registry lookup unavailable: %s", exc)
        return []

    items: list[dict[str, Any]] = []
    for source_row in rows:
        row = dict(source_row)
        authority_scope = str(row.get("authority_scope") or "state")
        if authority_scope not in {"federal", "state"}:
            continue
        pdf_url = _safe_https_url(row.get("pdf_url"))
        source_url = _safe_https_url(row.get("source_url"))
        if not pdf_url or not source_url:
            continue
        items.append(
            {
                "id": f"authorized-source:{row['source_key']}",
                "group": (
                    "Official public PDFs"
                    if authority_scope == "federal"
                    else "Verified state PDFs"
                ),
                "authority_scope": authority_scope,
                "kind": str(row["document_kind"]),
                "state_code": row.get("state_code"),
                "title": str(row["title"]),
                "subtitle": str(row.get("subtitle") or row.get("state_code") or "Official PDF"),
                "source_name": str(row["source_name"]),
                "source_url": source_url,
                "pdf_url": pdf_url,
                "download_url": (
                    f"/api/contracts/pdf-library/registered/{row['source_key']}/download"
                    if _safe_government_pdf_url(pdf_url)
                    else None
                ),
                "delivery": "external_pdf",
            }
        )
    return items


async def _approved_form_source_link_items(ctx: TenantContext) -> list[dict[str, Any]]:
    """Return approved government and provider form portals without copying text.

    Many state transaction forms are distributed by a REALTOR association or
    other licensed provider.  Those sources can be presented in the library,
    but the platform must not scrape, proxy, or rehost the form body unless a
    separate provider agreement explicitly authorizes that delivery.
    """
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT source_key, authority_scope, state_code, document_kind, title, subtitle,
                       source_name, source_url, access_mode, access_note
                FROM authorized_form_source_links
                WHERE approval_status = 'approved'
                ORDER BY authority_scope, state_code NULLS FIRST, title
                """
            )
    except Exception as exc:  # Supports deployments that have not applied 0034 yet.
        logger.debug("Approved form-source registry lookup unavailable: %s", exc)
        return []

    items: list[dict[str, Any]] = []
    for source_row in rows:
        row = dict(source_row)
        source_url = _safe_https_url(row.get("source_url"))
        if not source_url:
            continue
        access_mode = str(row.get("access_mode") or "")
        if access_mode not in {"public_portal", "licensed_association"}:
            continue
        state_code = str(row["state_code"]) if row.get("state_code") else None
        authority_scope = str(
            row.get("authority_scope") or ("federal" if state_code is None else "state")
        )
        if authority_scope not in {"federal", "state"}:
            continue
        items.append(
            {
                "id": f"form-source:{row['source_key']}",
                "group": (
                    "Federal form portals"
                    if authority_scope == "federal"
                    else (
                        "Licensed association forms"
                        if access_mode == "licensed_association"
                        else "Official form portals"
                    )
                ),
                "authority_scope": authority_scope,
                "kind": str(row["document_kind"]),
                "state_code": state_code,
                "title": str(row["title"]),
                "subtitle": str(row.get("subtitle") or state_code or "US federal"),
                "source_name": str(row["source_name"]),
                "source_url": source_url,
                "access_mode": access_mode,
                "access_note": str(row.get("access_note") or ""),
                "delivery": "source_link",
            }
        )
    return items


async def _registered_pdf_source(ctx: TenantContext, source_key: str) -> dict[str, str]:
    """Resolve one approved government PDF registration for a safe download."""
    if not _SOURCE_KEY_RE.fullmatch(source_key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registered PDF source not found.")
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                SELECT source_key, title, pdf_url
                FROM authorized_document_sources
                WHERE source_key = $1
                  AND approval_status = 'approved'
                  AND media_type = 'application/pdf'
                """,
                source_key,
            )
    except Exception as exc:  # A rolling deployment may not have 0033 applied yet.
        logger.debug("Registered PDF source lookup unavailable: %s", exc)
        row = None

    if row is not None:
        candidate = dict(row)
        pdf_url = _safe_government_pdf_url(candidate.get("pdf_url"))
        if pdf_url:
            return {
                "source_key": str(candidate["source_key"]),
                "title": str(candidate["title"]),
                "pdf_url": pdf_url,
            }
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registered PDF source not found.")

    # Preserve the two federal PDFs during a migration rollout.  The fallback
    # is still constrained to the same static, government-hosted registrations.
    for candidate in _OFFICIAL_PDF_SOURCES:
        if candidate.get("source_key") != source_key:
            continue
        pdf_url = _safe_government_pdf_url(candidate.get("pdf_url"))
        if pdf_url:
            return {
                "source_key": source_key,
                "title": str(candidate["title"]),
                "pdf_url": pdf_url,
            }
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Registered PDF source not found.")


async def _download_registered_pdf_bytes(pdf_url: str) -> bytes:
    """Fetch a bounded, content-typed government PDF for a device download."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
        ) as client:
            current_url = pdf_url
            for _ in range(4):
                async with client.stream(
                    "GET",
                    current_url,
                    headers={
                        "Accept": "application/pdf",
                        "User-Agent": _GOVERNMENT_PDF_USER_AGENT,
                    },
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        next_url = _safe_government_pdf_url(
                            urljoin(current_url, response.headers.get("location", ""))
                        )
                        if not next_url:
                            raise HTTPException(
                                status.HTTP_502_BAD_GATEWAY,
                                "The government PDF redirected outside its authorized source.",
                            )
                        current_url = next_url
                        continue
                    if response.status_code != status.HTTP_200_OK:
                        raise HTTPException(
                            status.HTTP_502_BAD_GATEWAY,
                            "The government PDF could not be retrieved.",
                        )
                    content_type = response.headers.get("content-type", "").lower().split(";", 1)[0].strip()
                    if content_type != "application/pdf":
                        raise HTTPException(
                            status.HTTP_502_BAD_GATEWAY,
                            "The registered source did not return a PDF.",
                        )
                    content_length = response.headers.get("content-length", "")
                    if content_length.isdigit() and int(content_length) > _REGISTERED_PDF_DOWNLOAD_MAX_BYTES:
                        raise HTTPException(
                            status.HTTP_502_BAD_GATEWAY,
                            "The government PDF is too large to download.",
                        )
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > _REGISTERED_PDF_DOWNLOAD_MAX_BYTES:
                            raise HTTPException(
                                status.HTTP_502_BAD_GATEWAY,
                                "The government PDF is too large to download.",
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            else:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "The government PDF redirected too many times.",
                )
    except httpx.TimeoutException as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "The government PDF did not respond in time.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The government PDF is unavailable.") from exc


def _registered_pdf_filename(source_key: str) -> str:
    return f"{source_key}.pdf"


async def _state_pdf_library_items(ctx: TenantContext) -> list[dict[str, Any]]:
    """Return only registered, direct-PDF state sources.

    State association pages and HTML reference pages are intentionally omitted:
    a picker labelled "PDF" must not send someone to a non-PDF placeholder.
    """
    try:
        async with tenant_tx(ctx) as conn:
            form_rows = await conn.fetch(
                """
                SELECT id, state_code, form_name, form_type, effective_date, download_url
                FROM state_disclosure_forms
                WHERE download_url IS NOT NULL AND btrim(download_url) <> ''
                ORDER BY state_code, form_name
                """
            )
            contract_rows = await conn.fetch(
                """
                SELECT id, state_code, template_name, association, version, effective_date, download_url
                FROM state_contract_templates
                WHERE download_url IS NOT NULL AND btrim(download_url) <> ''
                ORDER BY state_code, template_name
                """
            )
    except Exception as exc:  # Built-in and federal PDF choices remain available.
        logger.warning("PDF source lookup failed: %s", exc)
        return []

    items: list[dict[str, Any]] = []
    for source_row in form_rows:
        row = dict(source_row)
        pdf_url = _safe_direct_pdf_url(row["download_url"])
        if not pdf_url:
            continue
        subtitle_parts = [str(row["state_code"]), str(row.get("form_type") or "document")]
        if row.get("effective_date"):
            subtitle_parts.append(f"effective {row['effective_date']}")
        items.append(
            {
                "id": f"state-form:{row['id']}",
                "group": "Verified state PDFs",
                "kind": "document",
                "state_code": str(row["state_code"]),
                "title": str(row["form_name"]),
                "subtitle": " · ".join(subtitle_parts),
                "source_name": "State regulatory source",
                "pdf_url": pdf_url,
                "delivery": "external_pdf",
            }
        )

    for source_row in contract_rows:
        row = dict(source_row)
        pdf_url = _safe_direct_pdf_url(row["download_url"])
        if not pdf_url:
            continue
        subtitle_parts = [str(row["state_code"]), str(row.get("association") or "state contract")]
        if row.get("version"):
            subtitle_parts.append(f"v{row['version']}")
        if row.get("effective_date"):
            subtitle_parts.append(f"effective {row['effective_date']}")
        items.append(
            {
                "id": f"state-contract:{row['id']}",
                "group": "Verified state PDFs",
                "kind": "contract",
                "state_code": str(row["state_code"]),
                "title": str(row["template_name"]),
                "subtitle": " · ".join(subtitle_parts),
                "source_name": str(row.get("association") or "State contract source"),
                "pdf_url": pdf_url,
                "delivery": "external_pdf",
            }
        )

    return items


def _workspace_public_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result.pop("payload_ciphertext", None)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key == "metadata":
            result[key] = _json(value)
    return result


def _workspace_metadata(
    *,
    rendered: dict[str, Any],
    candidate: dict[str, Any],
    revision: int,
    completion_count: int,
) -> dict[str, Any]:
    return {
        "content_sha256": rendered["content_sha256"],
        "template_sha256": candidate["template_sha256"],
        "missing_fields": list(rendered.get("missing_variables") or []),
        "assistant_status": "ready" if rendered["status"] == "READY" else "needs_inputs",
        "revision": revision,
        "completion_count": completion_count,
    }


def _workspace_storage_payload(
    *,
    inputs: dict[str, Any],
    rendered: dict[str, Any],
) -> str:
    return canonical_json(
        {
            "schema_version": 1,
            "inputs": inputs,
            "draft_text": rendered["final_contract_text"],
        }
    )


async def _decrypt_workspace_payload(
    conn: Any,
    row: Any,
    ctx: TenantContext,
) -> dict[str, Any]:
    try:
        raw = await decrypt_pii(conn, row["payload_ciphertext"], _tenant_key(ctx))
        payload = json.loads(raw)
    except (CryptoError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Contract draft workspace could not be decrypted.",
        ) from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("inputs"), dict)
        or not isinstance(payload.get("draft_text"), str)
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Contract draft workspace payload is invalid.",
        )
    return payload


def _render_workspace(
    candidate: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    rendered = render_contract_workspace_draft(
        document_type=candidate["document_type"],
        body_template=candidate["body_template"],
        required_fields=list(candidate["required_fields"]),
        transaction_data=inputs,
    )
    if rendered.get("status") == "FATAL_ERROR":
        raise HTTPException(status_code=422, detail=rendered)
    return rendered


def _workspace_response(row: Any, rendered: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace": _workspace_public_row(row),
        "editable_draft": rendered["final_contract_text"],
        "assistant": {
            "status": "ready" if rendered["status"] == "READY" else "needs_inputs",
            "missing_fields": list(rendered.get("missing_variables") or []),
            "warnings": list(rendered.get("warnings") or []),
        },
    }


async def _get_template(
    conn: Any,
    ctx: TenantContext,
    template_id: str,
    *,
    approved: bool = False,
) -> Any:
    clause = " AND status='approved'" if approved else ""
    row = await conn.fetchrow(
        f"""
        SELECT * FROM contract_templates
         WHERE id=$1::uuid AND tenant_id=$2::uuid{clause}
        """,
        template_id,
        ctx.tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Contract template not found.")
    if template_sha256(row["body_template"]) != row["template_sha256"]:
        raise HTTPException(status_code=409, detail="Contract template checksum mismatch.")
    return row


@router.get("/policy")
async def contract_policy(ctx: TenantContext = Depends(require_context)):
    require_feature(Feature.CONTRACTS)
    return {
        "approved_templates_only": True,
        "attorney_review_required": True,
        "storage": "private-s3-aes256",
        "draft_storage": "tenant-key-encrypted",
        "execution_requires_verified_signature": True,
        "self_attested_signature_changes_status": False,
        "legal_advice": False,
    }


@router.get("/templates")
async def list_templates(ctx: TenantContext = Depends(require_context)):
    """List the tenant's registered sources and any tenant-only templates.

    Source approval proves that a template came from the version-controlled
    registry. It never substitutes for the attorney review required before a
    template can be used to generate a legal document.
    """
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            WITH registered_sources AS (
                SELECT
                    COALESCE(template.id, registration.id) AS id,
                    source.template_key,
                    source.version,
                    source.document_type,
                    source.jurisdiction,
                    COALESCE(template.status, 'registered') AS status,
                    source.source_sha256 AS template_sha256,
                    COALESCE(template.created_at, registration.registered_at) AS created_at,
                    COALESCE(template.updated_at, registration.updated_at) AS updated_at,
                    jsonb_build_object(
                        'status', source.source_status,
                        'kind', 'version_controlled',
                        'reference', source.source_ref,
                        'registered_at', registration.registered_at,
                        'checksum', source.source_sha256
                    ) AS source_control
                FROM tenant_contract_template_registrations AS registration
                JOIN contract_template_sources AS source ON source.id = registration.source_id
                LEFT JOIN contract_templates AS template
                  ON template.tenant_id = registration.tenant_id
                 AND template.template_key = source.template_key
                 AND template.version = source.version
                WHERE registration.tenant_id = $1::uuid
                  AND registration.status = 'registered'
            ),
            tenant_only AS (
                SELECT
                    template.id,
                    template.template_key,
                    template.version,
                    template.document_type,
                    template.jurisdiction,
                    template.status,
                    template.template_sha256,
                    template.created_at,
                    template.updated_at,
                    jsonb_build_object(
                        'status', 'tenant_registered',
                        'kind', 'tenant_managed',
                        'reference', 'tenant contract template'
                    ) AS source_control
                FROM contract_templates AS template
                WHERE template.tenant_id = $1::uuid
                  AND NOT EXISTS (
                      SELECT 1
                      FROM tenant_contract_template_registrations AS registration
                      JOIN contract_template_sources AS source ON source.id = registration.source_id
                      WHERE registration.tenant_id = template.tenant_id
                        AND source.template_key = template.template_key
                        AND source.version = template.version
                  )
            )
            SELECT * FROM registered_sources
            UNION ALL
            SELECT * FROM tenant_only
            ORDER BY template_key, version
            """,
            ctx.tenant_id,
        )
    templates = [_public_row(row) for row in rows]
    return {
        "templates": templates,
        "registry": {
            "registered_for_tenant": True,
            "registered_template_count": len(templates),
            "source_control": "approved",
            "attorney_review_required": True,
        },
    }


@router.get("/templates/library")
async def template_library(ctx: TenantContext = Depends(require_context)):
    """Expose every built-in form for preview and encrypted draft work.

    The response intentionally identifies these as source-controlled draft
    forms, not executable legal instruments.  It does not depend on a tenant
    bootstrapping a duplicate ``contract_templates`` row first.
    """
    require_feature(Feature.CONTRACTS)
    templates = []
    for template_key in sorted(BUILTIN_CONTRACT_TEMPLATES):
        candidate = _builtin_workspace_template(template_key)
        templates.append(
            {
                "template_key": template_key,
                "version": candidate["version"],
                "document_type": candidate["document_type"],
                "jurisdiction": candidate["jurisdiction"],
                "template_sha256": candidate["template_sha256"],
                "required_fields": _workspace_required_fields(candidate),
                "preview_text": candidate["body_template"],
                "availability": "draft_ready",
                "source_control": {
                    "kind": "version_controlled",
                    "status": "approved_source",
                    "reference": "backend/ml_forge/synthetic_lawyer.py:BUILTIN_CONTRACT_TEMPLATES",
                },
            }
        )
    templates.append(
        {
            "template_key": _ACCOUNT_SECURITY_ESA_TEMPLATE_KEY,
            "version": _ACCOUNT_SECURITY_ESA_TEMPLATE_VERSION,
            "document_type": "account_security_esa",
            "jurisdiction": "NEOH™",
            "template_sha256": template_sha256(_account_security_esa_template()),
            "required_fields": [],
            "preview_text": _account_security_esa_template(),
            "availability": "draft_ready",
            "source_control": {
                "kind": "version_controlled",
                "status": "approved_source",
                "reference": "backend/policy_acceptance.py:ACCOUNT_SECURITY_ESA_DOCUMENT",
                "agreement_version": ACCOUNT_SECURITY_ESA_VERSION,
            },
        }
    )
    return {
        "templates": templates,
        "draft_workflow": {
            "preview": True,
            "encrypted_backend_save": True,
            "device_download": True,
            "personal_ai_completion": "known_values_only",
        },
    }


@router.get("/pdf-library")
async def pdf_library(ctx: TenantContext = Depends(require_context)):
    """List direct PDFs and approved state form sources for the compact picker.

    Direct public PDFs can be previewed and saved from NEOH.  Association and
    other licensed sources are represented as approved outbound links only;
    their protected form text is never copied into the platform registry.
    """
    require_feature(Feature.CONTRACTS)
    items: list[dict[str, Any]] = []
    for template_key in sorted(BUILTIN_CONTRACT_TEMPLATES):
        candidate = _builtin_workspace_template(template_key)
        items.append(
            {
                "id": f"source-template:{template_key}",
                "group": "Source-controlled PDFs",
                "authority_scope": "platform",
                "kind": "contract" if candidate["document_type"] != "redline" else "document",
                "state_code": None,
                "title": _pdf_template_title(template_key),
                "subtitle": f"{candidate['jurisdiction']} · v{candidate['version']}",
                "source_name": "NEOH source control",
                "pdf_url": f"/api/contracts/templates/library/{template_key}/pdf",
                "delivery": "authenticated_pdf",
            }
        )

    approved_sources = await _approved_pdf_library_items(ctx)
    items.extend(approved_sources or _OFFICIAL_PDF_SOURCES)

    registered_pdf_urls = {item["pdf_url"] for item in items if item.get("pdf_url")}
    items.extend(
        item
        for item in await _state_pdf_library_items(ctx)
        if item["pdf_url"] not in registered_pdf_urls
    )
    items.extend(await _approved_form_source_link_items(ctx))
    state_counts = {}
    for code, _name in _FIFTY_STATE_OPTIONS:
        state_items = [item for item in items if item.get("state_code") == code]
        state_counts[code] = {
            "document_count": len(state_items),
            "public_pdf_count": sum(
                1 for item in state_items
                if item.get("delivery") in {"authenticated_pdf", "external_pdf"}
            ),
            "licensed_source_count": sum(
                1 for item in state_items
                if item.get("access_mode") == "licensed_association"
            ),
        }
    federal_items = [item for item in items if item.get("authority_scope") == "federal"]
    return {
        "items": items,
        "states": [
            {
                "state_code": code,
                "state_name": name,
                **state_counts[code],
            }
            for code, name in _FIFTY_STATE_OPTIONS
        ],
        "federal_sources": {
            "document_count": len(federal_items),
            "public_pdf_count": sum(
                1 for item in federal_items
                if item.get("delivery") in {"authenticated_pdf", "external_pdf"}
            ),
            "official_portal_count": sum(
                1 for item in federal_items
                if item.get("delivery") == "source_link"
            ),
        },
        "source_note": (
            "Direct public government PDFs can be opened and saved. Official portals open at the "
            "issuing authority; licensed association form libraries are not copied or rehosted by NEOH."
        ),
        "state_coverage_note": (
            "All 50 states plus federal sources are selectable. Direct PDFs, official portals, and "
            "licensed association sources are shown separately."
        ),
    }


@router.get("/templates/library/{template_key}/pdf")
async def download_template_library_pdf(
    template_key: str,
    ctx: TenantContext = Depends(require_context),
) -> Response:
    """Render an unfilled, versioned source template as a device-downloadable PDF."""
    require_feature(Feature.CONTRACTS)
    if template_key == _ACCOUNT_SECURITY_ESA_TEMPLATE_KEY:
        source_text = "\n".join(
            [
                "SOURCE-CONTROLLED FORM REFERENCE",
                "NEOH™ Account Security ESA",
                f"NEOH Account Security Agreement · {ACCOUNT_SECURITY_ESA_VERSION}",
                "Complete and review this form for your organization and workspace.",
                "",
                _account_security_esa_template(),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "account-security-esa.pdf"
            write_contract_pdf(source_text, output_path)
            pdf = output_path.read_bytes()
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="neoh-account-security-esa.pdf"'},
        )

    candidate = _builtin_workspace_template(template_key)
    source_text = "\n".join(
        [
            "SOURCE-CONTROLLED FORM REFERENCE",
            f"{_pdf_template_title(template_key)} · {candidate['jurisdiction']} · v{candidate['version']}",
            "Complete and review this form for the applicable jurisdiction before use.",
            "",
            candidate["body_template"],
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        output_path = Path(tmp) / "source-template.pdf"
        write_contract_pdf(source_text, output_path)
        pdf = output_path.read_bytes()
    filename = f"neoh-{template_key}-v{candidate['version']}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/pdf-library/registered/{source_key}/download")
async def download_registered_pdf(
    source_key: str,
    ctx: TenantContext = Depends(require_context),
) -> Response:
    """Download an approved government PDF as an attachment for the device."""
    require_feature(Feature.CONTRACTS)
    source = await _registered_pdf_source(ctx, source_key)
    pdf = await _download_registered_pdf_bytes(source["pdf_url"])
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_registered_pdf_filename(source["source_key"])}"'},
    )


@router.post("/templates/bootstrap", status_code=201)
async def bootstrap_templates(ctx: TenantContext = Depends(require_context)):
    """Install checksum-pinned draft candidates; never auto-approve them."""
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    created: list[dict[str, Any]] = []
    async with tenant_tx(ctx) as conn:
        for key, candidate in BUILTIN_CONTRACT_TEMPLATES.items():
            row = await conn.fetchrow(
                """
                INSERT INTO contract_templates (
                    tenant_id,template_key,version,document_type,jurisdiction,
                    body_template,required_fields,template_sha256,created_by
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::text[],$8,$9)
                ON CONFLICT (tenant_id,template_key,version) DO NOTHING
                RETURNING *
                """,
                ctx.tenant_id,
                key,
                candidate["version"],
                candidate["document_type"],
                candidate["jurisdiction"],
                candidate["body_template"],
                candidate["required_fields"],
                template_sha256(candidate["body_template"]),
                ctx.agent_id,
            )
            if row:
                created.append(_public_row(row))
    return {"templates": created, "status": "draft", "attorney_review_required": True}


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateCreate,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    validation = validate_contract_template(
        body.document_type, body.body_template, body.required_fields
    )
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"issues": validation["issues"]})
    async with tenant_tx(ctx) as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO contract_templates (
                    tenant_id,template_key,version,document_type,jurisdiction,
                    body_template,required_fields,template_sha256,created_by
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::text[],$8,$9)
                RETURNING *
                """,
                ctx.tenant_id,
                body.template_key,
                body.version,
                body.document_type,
                body.jurisdiction,
                body.body_template,
                validation["required_fields"],
                validation["template_sha256"],
                ctx.agent_id,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Template version already exists.") from exc
            raise
    return _public_row(row)


@router.post("/templates/{template_id}/decision")
async def decide_template(
    template_id: uuid.UUID,
    body: TemplateDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    reason = validate_approval_reason(body.reason)
    async with tenant_tx(ctx) as conn:
        await _get_template(conn, ctx, str(template_id))
        row = await conn.fetchrow(
            """
            UPDATE contract_templates
               SET status=$2,attorney_reviewed_by=$3,
                   attorney_reviewed_at=now(),approval_notes=$4
             WHERE id=$1::uuid AND tenant_id=$5::uuid AND status='draft'
            RETURNING *
            """,
            str(template_id),
            body.decision,
            body.attorney_reviewed_by,
            reason,
            ctx.tenant_id,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="Only draft templates can be decided.")
    return _public_row(row)


def _render_document(template: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    if template["document_type"] == "redline":
        return defensive_redline(
            str(inputs.get("original_text") or ""),
            str(inputs.get("proposed_text") or ""),
        )
    return render_approved_contract_template(
        document_type=template["document_type"],
        body_template=template["body_template"],
        required_fields=list(template["required_fields"]),
        transaction_data=inputs,
    )


def _document_draft_anchors(body: DocumentDraft) -> dict[str, Optional[uuid.UUID]]:
    return {
        "client_id": body.client_id,
        "lead_id": body.lead_id,
        "listing_id": body.listing_id,
        "property_id": body.property_id,
        "transaction_id": body.transaction_id,
        "vault_client_id": body.vault_client_id,
    }


def _stored_document_anchors(row: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    values = dict(row)
    anchors = dict(metadata.get("anchors") or {})
    anchors.update(
        {
            "lead_id": values.get("lead_id"),
            "transaction_id": values.get("transaction_id"),
            "vault_client_id": metadata.get("vault_client_id"),
        }
    )
    return anchors


def _document_approval_payload(
    *,
    document_id: str,
    content_sha256: str,
    revision: int,
    template: Any,
    vault_client_id: str,
    attorney_reviewer: str,
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "content_sha256": content_sha256,
        "revision": revision,
        "template_key": template["template_key"],
        "template_version": template["version"],
        "vault_client_id": vault_client_id,
        "attorney_reviewer": attorney_reviewer,
    }


async def _approval_for_document(
    conn: Any,
    ctx: TenantContext,
    *,
    document_id: str,
    content_sha256: str,
    revision: int,
    template: Any,
    vault_client_id: str,
    attorney_reviewer: str,
) -> dict[str, Any]:
    payload = _document_approval_payload(
        document_id=document_id,
        content_sha256=content_sha256,
        revision=revision,
        template=template,
        vault_client_id=vault_client_id,
        attorney_reviewer=attorney_reviewer,
    )
    digest = payload_hash(payload)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    row = await conn.fetchrow(
        """
        INSERT INTO action_approvals (
            tenant_id,action_type,risk_class,target_type,target_id,
            payload_hash,draft_payload,requested_by,expires_at
        ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)
        RETURNING *
        """,
        ctx.tenant_id,
        "contract.vault_and_approve",
        ActionRisk.LEGAL_DOCUMENT.value,
        "contract_document",
        document_id,
        digest,
        canonical_json(payload),
        ctx.agent_id,
        expires_at,
    )
    return approval_dict(row)


async def _audit_approval_requested(
    ctx: TenantContext,
    approval: dict[str, Any],
) -> None:
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="approval_requested",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(approval["id"]),
        metadata={
            "action_type": "contract.vault_and_approve",
            "risk_class": ActionRisk.LEGAL_DOCUMENT.value,
            "payload_hash": approval["payload_hash"],
        },
    )


async def _revoke_document_approval(
    conn: Any,
    ctx: TenantContext,
    approval_id: Any,
) -> bool:
    if not approval_id:
        return False
    row = await conn.fetchrow(
        """
        UPDATE action_approvals
           SET status='revoked',decided_by=$3,decided_at=now(),
               reason='Draft revised; prior content approval revoked.'
         WHERE id=$1::uuid AND tenant_id=$2::uuid
           AND status IN ('pending','approved')
        RETURNING id
        """,
        approval_id,
        ctx.tenant_id,
        ctx.agent_id,
    )
    return row is not None


@router.post("/draft-workspaces/preview")
async def preview_draft_workspace(
    body: DraftWorkspaceCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Render a non-persistent preview with visible gaps rather than guesses."""
    require_feature(Feature.CONTRACTS)
    candidate = _builtin_workspace_template(body.template_key)
    rendered = _render_workspace(candidate, body.inputs)
    return {
        "template": {
            "template_key": candidate["template_key"],
            "version": candidate["version"],
            "document_type": candidate["document_type"],
            "template_sha256": candidate["template_sha256"],
        },
        "editable_draft": rendered["final_contract_text"],
        "assistant": {
            "status": "ready" if rendered["status"] == "READY" else "needs_inputs",
            "missing_fields": list(rendered.get("missing_variables") or []),
            "warnings": list(rendered.get("warnings") or []),
        },
    }


@router.post("/draft-workspaces", status_code=201)
async def create_draft_workspace(
    body: DraftWorkspaceCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Persist an encrypted, tenant-scoped working draft without approval flow."""
    require_feature(Feature.CONTRACTS)
    candidate = _builtin_workspace_template(body.template_key)
    rendered = _render_workspace(candidate, body.inputs)
    metadata = _workspace_metadata(
        rendered=rendered,
        candidate=candidate,
        revision=1,
        completion_count=0,
    )
    payload = _workspace_storage_payload(inputs=body.inputs, rendered=rendered)
    async with tenant_tx(ctx) as conn:
        await _validate_draft_anchors(conn, ctx, body.inputs)
        ciphertext = await encrypt_pii(conn, payload, _tenant_key(ctx))
        row = await conn.fetchrow(
            """
            INSERT INTO contract_draft_workspaces (
                tenant_id,document_type,template_key,template_version,
                template_sha256,input_hash,payload_ciphertext,status,
                metadata,created_by,completed_at
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,
                CASE WHEN $8='ready' THEN now() ELSE NULL END)
            RETURNING *
            """,
            ctx.tenant_id,
            candidate["document_type"],
            candidate["template_key"],
            candidate["version"],
            candidate["template_sha256"],
            rendered["input_sha256"],
            ciphertext,
            "ready" if rendered["status"] == "READY" else "draft",
            canonical_json(metadata),
            ctx.agent_id,
        )
    return _workspace_response(row, rendered)


@router.get("/draft-workspaces")
async def list_draft_workspaces(
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    """List metadata for saved drafts without exposing their encrypted content."""
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM contract_draft_workspaces
            WHERE tenant_id=$1::uuid
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            ctx.tenant_id,
            limit,
        )
    return {"workspaces": [_workspace_public_row(row) for row in rows]}


@router.get("/draft-workspaces/{workspace_id}")
async def get_draft_workspace(
    workspace_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Load one draft and its encrypted input payload for the authenticated tenant."""
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM contract_draft_workspaces
             WHERE id=$1::uuid AND tenant_id=$2::uuid
            """,
            str(workspace_id),
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract draft workspace not found.")
        payload = await _decrypt_workspace_payload(conn, row, ctx)
    return {
        "workspace": _workspace_public_row(row),
        "inputs": payload["inputs"],
        "editable_draft": payload["draft_text"],
    }


@router.post("/draft-workspaces/{workspace_id}/ai-complete")
async def complete_draft_workspace(
    workspace_id: uuid.UUID,
    body: DraftWorkspaceCompletion,
    ctx: TenantContext = Depends(require_context),
):
    """Merge supplied known values, rerender deterministically, and save it.

    Personal AI is intentionally not allowed to fabricate contractual terms.
    Values absent from the saved draft and this request remain marked as missing.
    """
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM contract_draft_workspaces
             WHERE id=$1::uuid AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            str(workspace_id),
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract draft workspace not found.")
        candidate = _builtin_workspace_template(str(row["template_key"]))
        if (
            candidate["version"] != row["template_version"]
            or candidate["template_sha256"] != row["template_sha256"]
        ):
            raise HTTPException(
                status_code=409,
                detail="The source-controlled form changed; start a new draft workspace.",
            )
        payload = await _decrypt_workspace_payload(conn, row, ctx)
        merged_inputs = {**payload["inputs"], **body.inputs}
        await _validate_draft_anchors(conn, ctx, merged_inputs)
        try:
            _validate_workspace_input_size(merged_inputs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        rendered = _render_workspace(candidate, merged_inputs)
        current_metadata = dict(_json(row["metadata"]) or {})
        metadata = _workspace_metadata(
            rendered=rendered,
            candidate=candidate,
            revision=int(current_metadata.get("revision") or 1) + 1,
            completion_count=int(current_metadata.get("completion_count") or 0) + 1,
        )
        ciphertext = await encrypt_pii(
            conn,
            _workspace_storage_payload(inputs=merged_inputs, rendered=rendered),
            _tenant_key(ctx),
        )
        row = await conn.fetchrow(
            """
            UPDATE contract_draft_workspaces
               SET payload_ciphertext=$2,input_hash=$3,status=$4,metadata=$5::jsonb,
                   completed_at=CASE WHEN $4='ready' THEN now() ELSE NULL END
             WHERE id=$1::uuid AND tenant_id=$6::uuid
            RETURNING *
            """,
            str(workspace_id),
            ciphertext,
            rendered["input_sha256"],
            "ready" if rendered["status"] == "READY" else "draft",
            canonical_json(metadata),
            ctx.tenant_id,
        )
    return _workspace_response(row, rendered)


@router.get("/draft-workspaces/{workspace_id}/download")
async def download_draft_workspace(
    workspace_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    """Return a local-save PDF generated from the encrypted working draft."""
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM contract_draft_workspaces
             WHERE id=$1::uuid AND tenant_id=$2::uuid
            """,
            str(workspace_id),
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract draft workspace not found.")
        payload = await _decrypt_workspace_payload(conn, row, ctx)
    with tempfile.TemporaryDirectory(prefix="oracle_draft_download_") as tmp:
        output_path = Path(tmp) / "draft.pdf"
        write_contract_pdf(payload["draft_text"], output_path)
        pdf = output_path.read_bytes()
    document_type = str(row["document_type"]).replace("_", "-")
    filename = f"neoh-{document_type}-draft-{workspace_id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/documents", status_code=201)
async def draft_document(
    body: DocumentDraft,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    key = _tenant_key(ctx)
    explicit_anchors = _document_draft_anchors(body)
    async with tenant_tx(ctx) as conn:
        template = await _get_template(conn, ctx, str(body.template_id), approved=True)
        await _validate_draft_anchors(conn, ctx, explicit_anchors, body.inputs)
        result = _render_document(template, body.inputs)
        if result.get("status") != "SUCCESS":
            raise HTTPException(status_code=422, detail=result)

        content = result["final_contract_text"]
        content_sha = result["content_sha256"]
        metadata = {
            "content_sha256": content_sha,
            "template_sha256": template["template_sha256"],
            "warnings": result.get("warnings", []),
            "vault_client_id": str(body.vault_client_id),
            "anchors": {
                key: str(value)
                for key, value in explicit_anchors.items()
                if value is not None
            },
            "revision": 1,
            "redline_changes": result.get("changes", []),
        }
        ciphertext = await encrypt_pii(conn, content, key)
        row = await conn.fetchrow(
            """
            INSERT INTO contract_documents (
                tenant_id,transaction_id,lead_id,document_type,template_key,
                template_version,input_hash,content_ciphertext,status,
                attorney_review_required,metadata,created_by
            ) VALUES (
                $1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,'review_required',
                true,$9::jsonb,$10
            ) RETURNING *
            """,
            ctx.tenant_id,
            str(body.transaction_id) if body.transaction_id else None,
            str(body.lead_id) if body.lead_id else None,
            template["document_type"],
            template["template_key"],
            template["version"],
            result.get("input_sha256")
            or hashlib.sha256(canonical_json(body.inputs).encode("utf-8")).hexdigest(),
            ciphertext,
            canonical_json(metadata),
            ctx.agent_id,
        )
        approval = await _approval_for_document(
            conn,
            ctx,
            document_id=str(row["id"]),
            content_sha256=content_sha,
            revision=1,
            template=template,
            vault_client_id=str(body.vault_client_id),
            attorney_reviewer=body.attorney_reviewer,
        )
        row = await conn.fetchrow(
            """
            UPDATE contract_documents
               SET approval_id=$2::uuid
             WHERE id=$1::uuid AND tenant_id=$3::uuid
            RETURNING *
            """,
            row["id"],
            approval["id"],
            ctx.tenant_id,
        )
    await _audit_approval_requested(ctx, approval)
    return {
        "document": _public_row(row),
        "approval": approval,
        "editable_draft": content,
        "professional_review_required": True,
    }


@router.get("/documents")
async def list_documents(
    limit: int = Query(default=100, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM contract_documents
             WHERE tenant_id=$1::uuid
             ORDER BY created_at DESC LIMIT $2
            """,
            ctx.tenant_id,
            limit,
        )
    return {"documents": [_public_row(row) for row in rows]}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    include_draft: bool = Query(default=False),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM contract_documents
             WHERE id=$1::uuid AND tenant_id=$2::uuid
            """,
            document_id,
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract document not found.")
        draft = None
        if include_draft:
            try:
                draft = await decrypt_pii(conn, row["content_ciphertext"], _tenant_key(ctx))
            except CryptoError as exc:
                raise HTTPException(status_code=500, detail="Contract draft could not be decrypted.") from exc
    return {"document": _public_row(row), "editable_draft": draft}


@router.put("/documents/{document_id}/draft")
async def edit_document(
    document_id: uuid.UUID,
    body: DocumentEdit,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    if "\x00" in body.revised_text:
        raise HTTPException(status_code=422, detail="Draft contains unsupported control data.")
    try:
        body.revised_text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=422, detail="Draft contains unsupported PDF characters.") from exc
    content_sha = hashlib.sha256(body.revised_text.encode("utf-8")).hexdigest()
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM contract_documents
             WHERE id=$1::uuid AND tenant_id=$2::uuid
             FOR UPDATE
            """,
            str(document_id),
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract document not found.")
        if row["status"] not in {"draft", "review_required"}:
            raise HTTPException(status_code=409, detail="Approved or signed documents cannot be edited.")
        metadata = dict(_json(row["metadata"]) or {})
        await _validate_draft_anchors(
            conn,
            ctx,
            _stored_document_anchors(row, metadata),
        )
        await _revoke_document_approval(conn, ctx, row["approval_id"])
        ciphertext = await encrypt_pii(conn, body.revised_text, _tenant_key(ctx))
        revision = int(metadata.get("revision") or 1) + 1
        metadata.update(
            {
                "content_sha256": content_sha,
                "revision": revision,
            }
        )
        for key in (
            "approval_content_sha256",
            "approval_revision",
            "approval_recorded_at",
        ):
            metadata.pop(key, None)
        row = await conn.fetchrow(
            """
            UPDATE contract_documents
               SET content_ciphertext=$2,status='review_required',approval_id=NULL,
                   metadata=$3::jsonb,reviewed_by=NULL,reviewed_at=NULL,
                   s3_bucket=NULL,s3_key=NULL,artifact_sha256=NULL,encryption=NULL
             WHERE id=$1::uuid AND tenant_id=$4::uuid
            RETURNING *
            """,
            row["id"],
            ciphertext,
            canonical_json(metadata),
            ctx.tenant_id,
        )
        template = await conn.fetchrow(
            """
            SELECT * FROM contract_templates
             WHERE template_key=$1 AND version=$2
               AND tenant_id=$3::uuid AND status='approved'
            """,
            row["template_key"],
            row["template_version"],
            ctx.tenant_id,
        )
        if template is None:
            raise HTTPException(status_code=409, detail="Approved template version is unavailable.")
        if (
            template_sha256(template["body_template"]) != template["template_sha256"]
            or template["template_sha256"] != metadata.get("template_sha256")
        ):
            raise HTTPException(status_code=409, detail="Contract template checksum mismatch.")
        approval = await _approval_for_document(
            conn,
            ctx,
            document_id=str(row["id"]),
            content_sha256=content_sha,
            revision=revision,
            template=template,
            vault_client_id=str(metadata["vault_client_id"]),
            attorney_reviewer=body.attorney_reviewer,
        )
        row = await conn.fetchrow(
            """
            UPDATE contract_documents
               SET approval_id=$2::uuid
             WHERE id=$1::uuid AND tenant_id=$3::uuid AND approval_id IS NULL
            RETURNING *
            """,
            row["id"],
            approval["id"],
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=409, detail="Contract draft changed concurrently.")
    await _audit_approval_requested(ctx, approval)
    return {"document": _public_row(row), "approval": approval}


def _stale_document_approval() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Contract approval is stale or no longer matches this draft.",
    )


async def _transition_document_approval(
    document_id: uuid.UUID,
    body: ReviewDecision,
    ctx: TenantContext,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Atomically bind one decision to the locked content hash and revision."""
    reason = validate_approval_reason(body.reason)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT d.*,
                   a.id AS locked_approval_id,
                   a.action_type AS approval_action_type,
                   a.risk_class AS approval_risk_class,
                   a.target_type AS approval_target_type,
                   a.target_id AS approval_target_id,
                   a.draft_payload,
                   a.payload_hash AS approval_payload_hash,
                   a.status AS approval_status
              FROM contract_documents AS d
              JOIN action_approvals AS a
                ON a.id=d.approval_id AND a.tenant_id=d.tenant_id
             WHERE d.id=$1::uuid AND d.tenant_id=$2::uuid
             FOR UPDATE OF d,a
            """,
            str(document_id),
            ctx.tenant_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contract document not found.")

        payload = dict(_json(row["draft_payload"]) or {})
        metadata = dict(_json(row["metadata"]) or {})
        try:
            revision = int(metadata["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _stale_document_approval() from exc
        content_sha256 = str(metadata.get("content_sha256") or "")
        approval_id = str(row["locked_approval_id"])
        expected_payload_hash = payload_hash(payload)

        is_stale = any(
            (
                row["status"] != "review_required",
                row["approval_status"] != "pending",
                row["approval_action_type"] != "contract.vault_and_approve",
                row["approval_risk_class"] != ActionRisk.LEGAL_DOCUMENT.value,
                row["approval_target_type"] != "contract_document",
                str(row["approval_target_id"]) != str(document_id),
                str(payload.get("document_id")) != str(document_id),
                payload.get("content_sha256") != content_sha256,
                payload.get("revision") != revision,
                payload.get("template_key") != row["template_key"],
                payload.get("template_version") != row["template_version"],
                row["approval_payload_hash"] != expected_payload_hash,
                _CONTENT_SHA256_RE.fullmatch(content_sha256) is None,
                body.approval_id is not None and str(body.approval_id) != approval_id,
                body.content_sha256 is not None
                and body.content_sha256 != content_sha256,
                body.revision is not None and body.revision != revision,
            )
        )
        if is_stale:
            raise _stale_document_approval()

        await _validate_draft_anchors(
            conn,
            ctx,
            _stored_document_anchors(row, metadata),
            {"vault_client_id": payload.get("vault_client_id")},
        )
        try:
            content = await decrypt_pii(
                conn,
                row["content_ciphertext"],
                _tenant_key(ctx),
            )
        except CryptoError as exc:
            raise HTTPException(
                status_code=500,
                detail="Contract draft could not be decrypted.",
            ) from exc
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256:
            raise _stale_document_approval()

        approval_row = await conn.fetchrow(
            """
            UPDATE action_approvals
               SET status=CASE WHEN expires_at <= now() THEN 'expired' ELSE $3 END,
                   decided_by=$4,decided_at=now(),reason=$5
             WHERE id=$1::uuid AND tenant_id=$2::uuid AND status='pending'
               AND payload_hash=$6
               AND action_type='contract.vault_and_approve'
               AND target_type='contract_document' AND target_id=$7
            RETURNING *
            """,
            approval_id,
            ctx.tenant_id,
            body.decision,
            ctx.agent_id,
            reason,
            expected_payload_hash,
            str(document_id),
        )
        if approval_row is None:
            raise _stale_document_approval()
        approval = approval_dict(approval_row)

        if approval["status"] == "approved":
            document_row = await conn.fetchrow(
                """
                UPDATE contract_documents
                   SET metadata=metadata || jsonb_build_object(
                       'approval_content_sha256',$4,
                       'approval_revision',$5::int,
                       'approval_recorded_at',now()
                   )
                 WHERE id=$1::uuid AND tenant_id=$2::uuid
                   AND approval_id=$3::uuid AND status='review_required'
                   AND metadata->>'content_sha256'=$4
                   AND metadata->>'revision'=$5
                RETURNING id
                """,
                str(document_id),
                ctx.tenant_id,
                approval_id,
                content_sha256,
                str(revision),
            )
        else:
            document_row = await conn.fetchrow(
                """
                UPDATE contract_documents
                   SET status='draft',metadata=metadata || jsonb_build_object(
                       'last_rejection_reason',$6,'last_rejected_at',now()
                   )
                 WHERE id=$1::uuid AND tenant_id=$2::uuid
                   AND approval_id=$3::uuid AND status='review_required'
                   AND metadata->>'content_sha256'=$4
                   AND metadata->>'revision'=$5
                RETURNING id
                """,
                str(document_id),
                ctx.tenant_id,
                approval_id,
                content_sha256,
                str(revision),
                reason,
            )
        if document_row is None:
            raise _stale_document_approval()

    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action=f"approval_{approval['status']}",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=approval_id,
        metadata={
            "reason": reason,
            "risk_class": ActionRisk.LEGAL_DOCUMENT.value,
            "content_sha256": content_sha256,
            "revision": revision,
        },
    )
    return row, payload, approval


@router.post("/documents/{document_id}/decision", status_code=202)
async def review_document(
    document_id: uuid.UUID,
    body: ReviewDecision,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    row, payload, approval = await _transition_document_approval(document_id, body, ctx)

    if approval["status"] != "approved":
        return {"approval": approval, "queued": False}

    try:
        job, _ = await enqueue_job(
            ctx,
            job_type="contract:vault",
            payload=payload,
            idempotency_key=(
                f"contract-vault:{document_id}:{payload['content_sha256']}:"
                f"r{payload['revision']}"
            ),
            created_by=ctx.agent_id,
            risk=ActionRisk.LEGAL_DOCUMENT,
            approval_id=str(row["approval_id"]),
            priority=10,
            max_attempts=5,
        )
    except JobApprovalError as exc:
        raise _stale_document_approval() from exc
    return {"approval": approval, "job": job, "queued": True}


def _vault_sync(content: str, vault_object_id: str, client_id: str) -> dict[str, Any]:
    from contract_vault import SovereignVault

    with tempfile.TemporaryDirectory(prefix="oracle_legal_") as tmp:
        path = Path(tmp) / f"{vault_object_id}.pdf"
        write_contract_pdf(content, path)
        artifact_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        vaulted = SovereignVault().vault_pdf(
            path,
            client_id=client_id,
            document_id=vault_object_id,
            expiration_seconds=300,
        )
    data = vaulted.to_dict()
    data.pop("presigned_url", None)
    return {**data, "artifact_sha256": artifact_sha}


async def _vault_document_job(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
    tenant_id = str(reporter.job["tenant_id"])
    ctx = TenantContext(
        agent_id="contract-vault-worker",
        tenant_id=tenant_id,
        role=Role.PLATFORM_ADMIN,
    )
    document_id = str(payload["document_id"])
    try:
        approval_id = str(uuid.UUID(str(reporter.job["approval_id"])))
        revision = int(payload["revision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("contract vault job is missing its approval revision") from exc
    content_sha256 = str(payload.get("content_sha256") or "")
    expected_payload_hash = payload_hash(payload)
    if revision < 1 or _CONTENT_SHA256_RE.fullmatch(content_sha256) is None:
        raise ValueError("contract vault job has an invalid content revision")
    await reporter.progress(10, "Validating approved legal draft")
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT d.*,a.status AS approval_status,
                   a.payload_hash AS approval_payload_hash,
                   a.decided_by AS approval_decided_by
              FROM contract_documents AS d
              JOIN action_approvals AS a
                ON a.id=d.approval_id AND a.tenant_id=d.tenant_id
             WHERE d.id=$1::uuid AND d.tenant_id=$2::uuid
               AND d.approval_id=$3::uuid
             FOR UPDATE OF d,a
            """,
            document_id,
            tenant_id,
            approval_id,
        )
        if row is None:
            raise ValueError("contract document not found")
        metadata = dict(_json(row["metadata"]) or {})
        if (
            row["status"] != "review_required"
            or row["approval_status"] != "approved"
            or row["approval_payload_hash"] != expected_payload_hash
            or metadata.get("content_sha256") != content_sha256
            or metadata.get("revision") != revision
            or metadata.get("approval_content_sha256") != content_sha256
            or metadata.get("approval_revision") != revision
        ):
            raise ValueError("approved content checksum no longer matches")
        await _validate_draft_anchors(
            conn,
            ctx,
            _stored_document_anchors(row, metadata),
            {"vault_client_id": payload.get("vault_client_id")},
        )
        key = derive_tenant_key(tenant_id, os.environ["ORACLE_ENCRYPTION_MASTER_KEY"])
        content = await decrypt_pii(conn, row["content_ciphertext"], key)
        if (
            not content
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256
        ):
            raise ValueError("approved contract content could not be decrypted")
        approval_decided_by = str(row["approval_decided_by"] or "")
        if not approval_decided_by:
            raise ValueError("approved contract is missing an authenticated reviewer")
    vault_object_id = f"{document_id}-{content_sha256[:16]}-r{revision}"
    await reporter.progress(45, "Rendering private PDF")
    result = await asyncio.to_thread(
        _vault_sync,
        content,
        vault_object_id,
        str(payload["vault_client_id"]),
    )
    await reporter.progress(90, "Recording vault provenance")
    async with tenant_tx(ctx) as conn:
        recorded = await conn.fetchrow(
            """
            UPDATE contract_documents AS d
               SET status='approved',reviewed_by=$4,reviewed_at=now(),
                   s3_bucket=$5,s3_key=$6,artifact_sha256=$7,
                   encryption='AES256',metadata=metadata || jsonb_build_object(
                       'vaulted_at',now(),'vault_client_id',$8,
                       'vault_object_id',$9,
                       'professional_review_verification','unverified',
                       'attorney_reviewer_attestation',$10
                   )
             WHERE d.id=$1::uuid AND d.tenant_id=$2::uuid
               AND d.approval_id=$3::uuid AND d.status='review_required'
               AND d.metadata->>'content_sha256'=$11
               AND d.metadata->>'revision'=$12
               AND d.metadata->>'approval_content_sha256'=$11
               AND d.metadata->>'approval_revision'=$12
               AND EXISTS (
                   SELECT 1 FROM action_approvals AS a
                    WHERE a.id=$3::uuid AND a.tenant_id=$2::uuid
                      AND a.status='approved' AND a.payload_hash=$13
               )
            RETURNING d.id
            """,
            document_id,
            tenant_id,
            approval_id,
            approval_decided_by,
            result["bucket"],
            result["s3_key"],
            result["artifact_sha256"],
            str(payload["vault_client_id"]),
            vault_object_id,
            str(payload.get("attorney_reviewer") or ""),
            content_sha256,
            str(revision),
            expected_payload_hash,
        )
        if recorded is None:
            raise ValueError("approved content changed before vault finalization")
    return result


register_handler("contract:vault", _vault_document_job)


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    expires_in: int = Query(default=300, ge=30, le=3600),
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM contract_documents
             WHERE id=$1::uuid AND tenant_id=$2::uuid
            """,
            document_id,
            ctx.tenant_id,
        )
    if row is None or row["status"] not in {"approved", "signed"} or not row["s3_key"]:
        raise HTTPException(status_code=404, detail="Approved contract PDF not found.")
    metadata = dict(_json(row["metadata"]) or {})
    from contract_vault import SovereignVault

    vault_client_id = str(metadata["vault_client_id"])
    vault_object_id = str(metadata.get("vault_object_id") or document_id)
    vault = SovereignVault(bucket_name=row["s3_bucket"])
    if vault.s3_key(vault_client_id, vault_object_id) != row["s3_key"]:
        raise HTTPException(status_code=404, detail="Approved contract PDF not found.")
    url = await asyncio.to_thread(
        vault.generate_expiring_link,
        vault_client_id,
        vault_object_id,
        expires_in,
    )
    if not url:
        raise HTTPException(status_code=502, detail="Contract link could not be generated.")
    return {"url": url, "expires_in": expires_in, "watermark": "PRIVATE LEGAL DOCUMENT"}


@router.post("/documents/{document_id}/signed")
async def record_signature(
    document_id: uuid.UUID,
    body: SignatureRecord,
    ctx: TenantContext = Depends(require_context),
):
    require_feature(Feature.CONTRACTS)
    require_role(ctx, Role.BROKER_OWNER)
    reason = validate_approval_reason(body.reason)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE contract_documents
               SET metadata=metadata || jsonb_build_object(
                   'signature_reference',$2,'signature_recorded_by',$3,
                   'signature_reason',$4,'signature_recorded_at',now(),
                   'signature_verification_status','unverified',
                   'execution_status','not_verified'
               )
             WHERE id=$1::uuid AND tenant_id=$5::uuid AND status='approved'
            RETURNING *
            """,
            str(document_id),
            body.signature_reference,
            ctx.agent_id,
            reason,
            ctx.tenant_id,
        )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="A signature attestation can be recorded only for an approved document.",
        )
    return _public_row(row)
