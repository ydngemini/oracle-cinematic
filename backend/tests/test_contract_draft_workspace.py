"""Coverage for the preview/save/download Personal AI contract workspace."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import contracts_api
from contracts_api import (
    DraftWorkspaceCompletion,
    DraftWorkspaceCreate,
    complete_draft_workspace,
    create_draft_workspace,
    download_template_library_pdf,
    download_registered_pdf,
    download_draft_workspace,
    pdf_library,
    template_library,
)
from policy_contract import account_security_esa_pdf_text
from ml_forge.synthetic_lawyer import (
    BUILTIN_CONTRACT_TEMPLATES,
    render_contract_workspace_draft,
)
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)


def _seller_inputs(**overrides):
    values = {
        "current_date": "2026-07-15",
        "seller_name": "Darya Seller",
        "buyer_name": "Neoh Acquisitions LLC",
        "property_address": "123 Main St, Dover, DE 19901",
        "purchase_price": "100000",
        "earnest_money_deposit": "1000",
        "closing_date": "2026-08-15",
        "approved_addenda": "None",
    }
    values.update(overrides)
    return values


def _assignment_inputs(**overrides):
    values = {
        "current_date": "2026-07-15",
        "assignor_name": "Neoh Acquisitions LLC",
        "assignee_name": "Atlas Cash Buyers LLC",
        "seller_name": "Darya Seller",
        "property_address": "123 Main St, Dover, DE 19901",
        "original_contract_date": "2026-07-01",
        "wholesale_buy_price": "195000",
        "investor_buy_price": "220000",
        "earnest_money_deposit": "3000",
        "closing_date": "2026-08-15",
        "condition_flag": "None",
    }
    values.update(overrides)
    return values


def test_workspace_renderer_marks_missing_values_instead_of_inventing_them():
    candidate = BUILTIN_CONTRACT_TEMPLATES["seller-purchase-standard"]

    result = render_contract_workspace_draft(
        document_type=candidate["document_type"],
        body_template=candidate["body_template"],
        required_fields=candidate["required_fields"],
        transaction_data={"seller_name": "Darya Seller"},
    )

    assert result["status"] == "INCOMPLETE"
    assert "buyer_name" in result["missing_variables"]
    assert "[[MISSING: buyer_name]]" in result["final_contract_text"]
    assert "AI-ASSISTED WORKING DRAFT" in result["final_contract_text"]


def test_workspace_renderer_keeps_assignment_fee_deterministic_when_complete():
    candidate = BUILTIN_CONTRACT_TEMPLATES["assignment-standard"]

    result = render_contract_workspace_draft(
        document_type=candidate["document_type"],
        body_template=candidate["body_template"],
        required_fields=candidate["required_fields"],
        transaction_data=_assignment_inputs(),
    )

    assert result["status"] == "READY"
    assert result["missing_variables"] == []
    assert result["assignment_fee_calculated"] == 25000
    assert "$25,000" in result["final_contract_text"]


def test_workspace_rejects_malformed_supplied_date_instead_of_saving_it():
    candidate = BUILTIN_CONTRACT_TEMPLATES["seller-purchase-standard"]

    result = render_contract_workspace_draft(
        document_type=candidate["document_type"],
        body_template=candidate["body_template"],
        required_fields=candidate["required_fields"],
        transaction_data=_seller_inputs(current_date="tomorrow"),
    )

    assert result["status"] == "FATAL_ERROR"
    assert "current_date_invalid" in result["missing_variables"]


def test_template_library_exposes_all_builtin_forms_for_preview(monkeypatch):
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)

    result = asyncio.run(template_library(CTX))

    template_keys = {item["template_key"] for item in result["templates"]}
    assert "account-security-esa" in template_keys
    assert BUILTIN_CONTRACT_TEMPLATES.keys() <= template_keys
    assert any(
        item["template_key"] == "account-security-esa"
        and item["document_type"] == "account_security_esa"
        and item["version"] == "1.0.0"
        and item["jurisdiction"] == "NEOH™"
        and item["source_control"]["status"] == "approved_source"
        for item in result["templates"]
    )
    assert all(item["preview_text"] for item in result["templates"])
    assert result["draft_workflow"]["encrypted_backend_save"] is True
    assert result["draft_workflow"]["device_download"] is True


def test_account_security_esa_form_renders_as_workspace_ready(monkeypatch):
    candidate_type = "account_security_esa"
    body_template = account_security_esa_pdf_text()
    result = render_contract_workspace_draft(
        document_type=candidate_type,
        body_template=body_template,
        required_fields=[],
        transaction_data={},
    )
    assert result["status"] == "READY"
    assert result["missing_variables"] == []
    assert "NEOH™ Account Security Agreement (ESA)" in result["final_contract_text"]

class _WorkspaceConn:
    def __init__(self):
        self.row = None

    async def fetchrow(self, query, *args):
        if "INSERT INTO contract_draft_workspaces" in query:
            now = datetime.now(timezone.utc)
            self.row = {
                "id": uuid.uuid4(),
                "tenant_id": args[0],
                "document_type": args[1],
                "template_key": args[2],
                "template_version": args[3],
                "template_sha256": args[4],
                "input_hash": args[5],
                "payload_ciphertext": args[6],
                "status": args[7],
                "metadata": args[8],
                "created_by": args[9],
                "created_at": now,
                "updated_at": now,
                "completed_at": now if args[7] == "ready" else None,
            }
            return self.row
        if "SELECT * FROM contract_draft_workspaces" in query:
            return self.row
        if "UPDATE contract_draft_workspaces" in query:
            self.row.update(
                {
                    "payload_ciphertext": args[1],
                    "input_hash": args[2],
                    "status": args[3],
                    "metadata": args[4],
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            return self.row
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch(self, query, *_args):
        assert "contract_draft_workspaces" in query
        return [self.row] if self.row else []


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    return tx


async def _fake_encrypt(_conn, plaintext, _key):
    return plaintext.encode("utf-8")


async def _fake_decrypt(_conn, ciphertext, _key):
    return ciphertext.decode("utf-8")


def test_workspace_saves_completion_and_downloads_to_device(monkeypatch):
    conn = _WorkspaceConn()
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "test-only-key")
    monkeypatch.setattr(contracts_api, "tenant_tx", _fake_tenant_tx(conn))
    monkeypatch.setattr(contracts_api, "encrypt_pii", _fake_encrypt)
    monkeypatch.setattr(contracts_api, "decrypt_pii", _fake_decrypt)
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)

    created = asyncio.run(
        create_draft_workspace(
            DraftWorkspaceCreate(
                template_key="seller-purchase-standard",
                inputs={"seller_name": "Darya Seller"},
            ),
            CTX,
        )
    )

    assert created["workspace"]["status"] == "draft"
    assert "payload_ciphertext" not in created["workspace"]
    assert "Darya Seller" not in json.dumps(created["workspace"])

    completed = asyncio.run(
        complete_draft_workspace(
            uuid.UUID(created["workspace"]["id"]),
            DraftWorkspaceCompletion(inputs=_seller_inputs()),
            CTX,
        )
    )

    assert completed["workspace"]["status"] == "ready"
    assert completed["assistant"]["missing_fields"] == []
    assert "Darya Seller" in completed["editable_draft"]

    response = asyncio.run(
        download_draft_workspace(uuid.UUID(completed["workspace"]["id"]), CTX)
    )
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert "attachment" in response.headers["content-disposition"]


class _PdfLibraryConn:
    async def fetch(self, query, *_args):
        if "authorized_document_sources" in query:
            return []
        if "authorized_form_source_links" in query:
            return []
        if "state_disclosure_forms" in query:
            return [
                {
                    "id": "state-form-pdf",
                    "state_code": "TX",
                    "form_name": "Texas Seller Disclosure",
                    "form_type": "seller_disclosure",
                    "effective_date": None,
                    "download_url": "https://example.test/tx/seller-disclosure.pdf",
                },
                {
                    "id": "state-form-page",
                    "state_code": "TX",
                    "form_name": "Texas Source Page",
                    "form_type": "seller_disclosure",
                    "effective_date": None,
                    "download_url": "https://example.test/tx/seller-disclosure",
                },
            ]
        if "state_contract_templates" in query:
            return [
                {
                    "id": "state-contract-pdf",
                    "state_code": "TX",
                    "template_name": "Texas Residential Contract",
                    "association": "TREC",
                    "version": "2026.1",
                    "effective_date": None,
                    "download_url": "https://example.test/tx/residential-contract.pdf",
                },
                {
                    "id": "state-contract-unsafe",
                    "state_code": "TX",
                    "template_name": "Unsafe Contract",
                    "association": "TREC",
                    "version": "2026.1",
                    "effective_date": None,
                    "download_url": "javascript:alert(1)",
                },
            ]
        raise AssertionError(f"Unexpected query: {query}")


def test_pdf_library_lists_source_controlled_and_direct_pdf_sources(monkeypatch):
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)
    monkeypatch.setattr(contracts_api, "tenant_tx", _fake_tenant_tx(_PdfLibraryConn()))

    result = asyncio.run(pdf_library(CTX))

    ids = {item["id"] for item in result["items"]}
    assert {f"source-template:{key}" for key in BUILTIN_CONTRACT_TEMPLATES} <= ids
    assert "official:epa-lead-seller-en" in ids
    assert "official:epa-lead-lessor-en" in ids
    assert "state-form:state-form-pdf" in ids
    assert "state-contract:state-contract-pdf" in ids
    assert "state-form:state-form-page" not in ids
    assert "state-contract:state-contract-unsafe" not in ids
    assert all(item["delivery"] in {"authenticated_pdf", "external_pdf"} for item in result["items"])
    assert len(result["states"]) == 50
    assert result["states"][0] == {
        "state_code": "AL",
        "state_name": "Alabama",
        "document_count": 0,
        "public_pdf_count": 0,
        "licensed_source_count": 0,
    }
    texas = next(state for state in result["states"] if state["state_code"] == "TX")
    assert texas["document_count"] == 2
    assert texas["public_pdf_count"] == 2
    assert texas["licensed_source_count"] == 0


def test_source_template_pdf_is_a_real_pdf_download(monkeypatch):
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)

    response = asyncio.run(download_template_library_pdf("assignment-standard", CTX))

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert "inline" in response.headers["content-disposition"]
    assert "assignment-standard" in response.headers["content-disposition"]


class _AuthorizedPdfLibraryConn:
    async def fetch(self, query, *_args):
        if "authorized_document_sources" in query:
            return [{
                "source_key": "ny-dos-current-disclosure",
                "authority_scope": "state",
                "state_code": "NY",
                "document_kind": "document",
                "title": "Property Condition Disclosure Statement",
                "subtitle": "NY · required beginning 2025-07-01",
                "source_name": "New York Department of State",
                "source_url": "https://dos.ny.gov/additional-forms-real-estate-salesperson",
                # The official source is a PDF despite not ending in .pdf.
                "pdf_url": "https://dos.ny.gov/property-condition-disclosure-statement-eff-7125",
                "version": "DOS-1614-f",
                "effective_date": None,
            }]
        if "authorized_form_source_links" in query:
            return [
                {
                    "source_key": "ny-association-form-library",
                    "authority_scope": "state",
                    "state_code": "NY",
                    "document_kind": "contract",
                    "title": "New York association form library",
                    "subtitle": "NY · member transaction forms",
                    "source_name": "New York association",
                    "source_url": "https://forms.example.test/new-york",
                    "access_mode": "licensed_association",
                    "access_note": "Licensed access required.",
                },
                {
                    "source_key": "unsafe-form-source",
                    "authority_scope": "state",
                    "state_code": "NY",
                    "document_kind": "contract",
                    "title": "Unsafe form source",
                    "subtitle": "NY",
                    "source_name": "Unsafe source",
                    "source_url": "javascript:alert(1)",
                    "access_mode": "licensed_association",
                    "access_note": "",
                },
                {
                    "source_key": "cfpb-forms",
                    "authority_scope": "federal",
                    "state_code": None,
                    "document_kind": "document",
                    "title": "CFPB model forms",
                    "subtitle": "US federal · loan estimate and closing disclosure",
                    "source_name": "Consumer Financial Protection Bureau",
                    "source_url": "https://www.consumerfinance.gov/forms-samples/",
                    "access_mode": "public_portal",
                    "access_note": "Official federal source.",
                },
            ]
        if "state_disclosure_forms" in query or "state_contract_templates" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")


def test_pdf_library_uses_verified_registry_for_extensionless_official_pdf(monkeypatch):
    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)
    monkeypatch.setattr(contracts_api, "tenant_tx", _fake_tenant_tx(_AuthorizedPdfLibraryConn()))

    result = asyncio.run(pdf_library(CTX))

    item = next(item for item in result["items"] if item["id"] == "authorized-source:ny-dos-current-disclosure")
    assert item["group"] == "Verified state PDFs"
    assert item["delivery"] == "external_pdf"
    assert item["pdf_url"].endswith("eff-7125")
    assert item["download_url"] == "/api/contracts/pdf-library/registered/ny-dos-current-disclosure/download"

    licensed_source = next(
        item for item in result["items"]
        if item["id"] == "form-source:ny-association-form-library"
    )
    assert licensed_source["group"] == "Licensed association forms"
    assert licensed_source["delivery"] == "source_link"
    assert licensed_source["source_url"] == "https://forms.example.test/new-york"
    assert "download_url" not in licensed_source
    assert "form-source:unsafe-form-source" not in {item["id"] for item in result["items"]}

    federal_source = next(item for item in result["items"] if item["id"] == "form-source:cfpb-forms")
    assert federal_source["group"] == "Federal form portals"
    assert federal_source["authority_scope"] == "federal"
    assert federal_source["state_code"] is None
    assert result["federal_sources"] == {
        "document_count": 1,
        "public_pdf_count": 0,
        "official_portal_count": 1,
    }

    new_york = next(state for state in result["states"] if state["state_code"] == "NY")
    assert new_york["document_count"] == 2
    assert new_york["public_pdf_count"] == 1
    assert new_york["licensed_source_count"] == 1


def test_registered_pdf_download_is_an_attachment_from_a_government_registration(monkeypatch):
    async def fake_source(_ctx, source_key):
        assert source_key == "ny-dos-current-disclosure"
        return {
            "source_key": source_key,
            "title": "Property Condition Disclosure Statement",
            "pdf_url": "https://dos.ny.gov/property-condition-disclosure-statement-eff-7125",
        }

    async def fake_download(pdf_url):
        assert pdf_url == "https://dos.ny.gov/property-condition-disclosure-statement-eff-7125"
        return b"%PDF-1.7\nverified"

    monkeypatch.setattr(contracts_api, "require_feature", lambda _feature: None)
    monkeypatch.setattr(contracts_api, "_registered_pdf_source", fake_source)
    monkeypatch.setattr(contracts_api, "_download_registered_pdf_bytes", fake_download)

    response = asyncio.run(download_registered_pdf("ny-dos-current-disclosure", CTX))

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-")
    assert response.headers["content-disposition"].startswith("attachment;")
