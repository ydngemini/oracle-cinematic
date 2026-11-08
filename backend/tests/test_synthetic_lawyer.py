"""
Synthetic Lawyer injection-engine tests.

These tests cover the deterministic contract path only; the Bedrock JSONL
training generator remains exercised separately by manual forge runs.
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager

import pytest

from ml_forge.synthetic_lawyer import (
    BUILTIN_CONTRACT_TEMPLATES,
    draft_assignment_contract,
    draft_assignment_contract_json,
    fetch_assignment_transaction_for_client,
    generate_assignment_contract_artifact,
    generate_assignment_contract_vault_artifact,
    normalize_assignment_payload,
    render_contract_workspace_draft,
)
from contract_vault import VaultedContract


def _payload(**overrides):
    base = {
        "current_date": "2026-07-07",
        "assignor_name": "Neoh Acquisitions LLC",
        "assignee_name": "Atlas Cash Buyers LLC",
        "seller_name": "Dorothy A. Henson",
        "property_address": "42 Elkton Rd, Newark, DE 19711",
        "original_contract_date": "2026-06-15",
        "wholesale_buy_price": 195000,
        "investor_buy_price": 220000,
        "earnest_money": 3000,
        "closing_date": "2026-08-01",
        "condition_flag": "None",
    }
    base.update(overrides)
    return base


def test_draft_calculates_assignment_fee_and_injects_template():
    result = draft_assignment_contract(_payload())

    assert result["status"] == "SUCCESS"
    assert result["missing_variables"] == []
    assert result["assignment_fee_calculated"] == 25000
    contract = result["final_contract_text"]
    assert "$25,000" in contract
    assert "$3,000" in contract
    assert "Dorothy A. Henson" in contract
    assert "{{" not in contract


def test_draft_ignores_supplied_assignment_fee_and_uses_price_delta():
    result = draft_assignment_contract(_payload(assignment_fee=999999))

    assert result["status"] == "SUCCESS"
    assert result["assignment_fee_calculated"] == 25000
    assert "$999,999" not in result["final_contract_text"]


def test_draft_appends_pre_foreclosure_clause_to_section_4():
    result = draft_assignment_contract(_payload(condition_flag="pre_foreclosure"))

    assert result["status"] == "SUCCESS"
    assert "4(a). Pre-Foreclosure Contingency" in result["final_contract_text"]


def test_draft_appends_probate_clause_from_distress_flags():
    result = draft_assignment_contract(
        _payload(condition_flag=None, payload={"distress_flags": ["probate"]})
    )

    assert result["status"] == "SUCCESS"
    assert "4(a). Probate Contingency" in result["final_contract_text"]


def test_draft_fatal_error_when_required_data_missing():
    result = draft_assignment_contract(
        _payload(assignee_name="", seller_name=None, investor_buy_price=None)
    )

    assert result["status"] == "FATAL_ERROR"
    assert "assignee_name" in result["missing_variables"]
    assert "seller_name" in result["missing_variables"]
    assert "investor_buy_price" in result["missing_variables"]
    assert result["final_contract_text"] == ""


def test_public_record_owner_is_never_used_as_the_seller():
    # F11.04: an owner of record comes from a public-record parcel match that
    # nobody has confirmed.  It is not the contract's Seller.
    transaction = _payload(
        seller_name=None,
        payload={"owner_name": "Public Record Owner"},
    )

    assert normalize_assignment_payload(transaction)["seller_name"] is None
    result = draft_assignment_contract(transaction)

    assert result["status"] == "FATAL_ERROR"
    assert "seller_name" in result["missing_variables"]
    _assert_workspace_asks_for_the_seller(transaction)


def _assert_workspace_asks_for_the_seller(transaction):
    # A FATAL_ERROR draft has no text at all; the workspace preview is the
    # path that renders text without a seller, so the owner check lives here.
    template = BUILTIN_CONTRACT_TEMPLATES["assignment-standard"]
    draft = render_contract_workspace_draft(
        document_type=template["document_type"],
        body_template=template["body_template"],
        required_fields=template["required_fields"],
        transaction_data=transaction,
    )
    assert draft["status"] == "INCOMPLETE"
    assert "seller_name" in draft["missing_variables"]
    assert "[[MISSING: seller_name]]" in draft["final_contract_text"]
    assert "Public Record Owner" not in draft["final_contract_text"]


def test_lead_seller_name_still_fills_the_seller():
    result = draft_assignment_contract(
        _payload(
            seller_name=None,
            payload={"seller_name": "Confirmed Seller", "owner_name": "Public Record Owner"},
        )
    )

    assert result["status"] == "SUCCESS"
    assert "Confirmed Seller" in result["final_contract_text"]
    assert "Public Record Owner" not in result["final_contract_text"]


def _select_expression(query, alias):
    """The SELECT-list expression a query names ``alias``, however it is spelled.

    Walks back from ``AS <alias>`` to the top-level comma (or SELECT) that
    starts the expression, skipping parentheses and quoted literals.
    """
    found = list(re.finditer(rf"\bAS\s+{alias}\b", query, re.IGNORECASE))
    assert len(found) == 1, f"the deal query must name exactly one {alias} column"
    end = found[0].start()
    depth, quoted, i = 0, False, end - 1
    while i >= 0:
        ch = query[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted:
            if ch == ")":
                depth += 1
            elif ch == "(":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and (ch == "," or re.search(r"\bSELECT\s*$", query[: i + 1], re.IGNORECASE)):
                break
        i -= 1
    return query[i + 1:end].strip()


def _assert_seller_never_reads_an_owner(query):
    """F11.04 guard, independent of SQL spelling: the Seller expression names
    no owner field at all, and the query never reads public-record parcels."""
    expression = _select_expression(query, "seller_name")
    assert not re.search(r"owner", expression, re.IGNORECASE), expression
    assert "public_property_records" not in query.lower()


class _ContractDealConn:
    """Answers the deal query; a Seller expression that reads any owner field
    yields the owner of record, as Postgres would."""

    def __init__(self, *, seller_client_name=None):
        self.seller_client_name = seller_client_name
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        payload = {"owner_name": "Public Record Owner", "earnest_money": 3000}
        seller = self.seller_client_name
        if seller is None and re.search(r"owner", _select_expression(query, "seller_name"), re.IGNORECASE):
            seller = payload["owner_name"]
        return {
            "assignee_name": "Atlas Cash Buyers LLC",
            "property_address": "42 Elkton Rd, Newark, DE 19711",
            "seller_name": seller,
            "contract_execution_date": "2026-06-15",
            "contract_expires_at": "2099-08-01",
            "underwriting": {"wholesale_buy_price": 195000, "investor_buy_price": 220000},
            "payload": json.dumps(payload),
            "acquisition_entity": {"entity_name": "Neoh Acquisitions LLC"},
            "listing_price": None,
        }


def _fetch_with(monkeypatch, conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr("db.connection.tenant_tx", tx)
    return asyncio.run(
        fetch_assignment_transaction_for_client("11111111-1111-1111-1111-111111111111", object())
    )


def test_client_contract_without_a_linked_seller_asks_for_one(monkeypatch):
    conn = _ContractDealConn()
    transaction = _fetch_with(monkeypatch, conn)

    _assert_seller_never_reads_an_owner(conn.queries[0][0])
    assert transaction["seller_name"] is None
    result = draft_assignment_contract(transaction)
    assert result["status"] == "FATAL_ERROR"
    assert "seller_name" in result["missing_variables"]
    _assert_workspace_asks_for_the_seller(transaction)


@pytest.mark.parametrize(
    "regression",
    [
        "COALESCE(seller.full_name, leads.payload->>'seller_name', leads.payload->>'owner_name')",
        "COALESCE(seller.full_name, leads.payload ->> 'owner_name')",
        "COALESCE(seller.full_name, leads.payload#>>'{owner_name}')",
        "COALESCE(seller.full_name, (SELECT p.owner_name FROM public_property_records p"
        " WHERE p.parcel_id = leads.parcel_id LIMIT 1))",
        "COALESCE(seller.full_name, (SELECT p.name FROM public_property_records p LIMIT 1))",
        "COALESCE(seller.full_name, leads.payload->>'OWNER')",
    ],
)
def test_seller_guard_catches_every_spelling_of_an_owner_fallback(monkeypatch, regression):
    conn = _ContractDealConn()
    _fetch_with(monkeypatch, conn)
    query = conn.queries[0][0]
    shipped = _select_expression(query, "seller_name")

    regressed = query.replace(shipped, regression)
    assert regressed != query
    with pytest.raises(AssertionError):
        _assert_seller_never_reads_an_owner(regressed)


def test_client_contract_uses_the_linked_seller_client(monkeypatch):
    conn = _ContractDealConn(seller_client_name="Linked Seller Client")
    transaction = _fetch_with(monkeypatch, conn)

    result = draft_assignment_contract(transaction)
    assert result["status"] == "SUCCESS"
    assert "Linked Seller Client" in result["final_contract_text"]
    assert "Public Record Owner" not in result["final_contract_text"]


def test_draft_rejects_corrupt_financial_and_date_values():
    result = draft_assignment_contract(
        _payload(
            wholesale_buy_price=-195000,
            investor_buy_price=-170000,
            earnest_money=-1,
            closing_date="2026-01-01",
        )
    )

    assert result["status"] == "FATAL_ERROR"
    assert "wholesale_buy_price_must_be_positive" in result["missing_variables"]
    assert "investor_buy_price_must_be_positive" in result["missing_variables"]
    assert "earnest_money_deposit_must_be_nonnegative" in result["missing_variables"]
    assert "closing_date_before_assignment_date" in result["missing_variables"]


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity", "1e100"])
def test_draft_rejects_nonfinite_or_extreme_money(bad_amount):
    result = draft_assignment_contract(_payload(investor_buy_price=bad_amount))

    assert result["status"] == "FATAL_ERROR"
    assert "investor_buy_price" in result["missing_variables"]


def test_draft_rejects_subcent_earnest_money():
    result = draft_assignment_contract(_payload(earnest_money="100.001"))

    assert result["status"] == "FATAL_ERROR"
    assert "earnest_money_deposit_has_subcent_precision" in result["missing_variables"]


def test_draft_rejects_clause_injection_in_plain_text_fields():
    result = draft_assignment_contract(
        _payload(seller_name="Seller Name\n6. Injected Clause: Not allowed")
    )

    assert result["status"] == "FATAL_ERROR"
    assert "seller_name_invalid" in result["missing_variables"]
    assert result["final_contract_text"] == ""


def test_draft_rejects_characters_the_pdf_font_cannot_preserve():
    result = draft_assignment_contract(_payload(seller_name="Seller \u2603"))

    assert result["status"] == "FATAL_ERROR"
    assert "seller_name_unsupported_characters" in result["missing_variables"]


def test_minified_json_output_is_valid_and_has_no_markdown():
    raw = draft_assignment_contract_json(_payload())

    assert raw.startswith("{")
    assert "\n" not in raw
    parsed = json.loads(raw)
    assert parsed["status"] == "SUCCESS"


def test_pdf_artifact_written_for_success(tmp_path):
    output_path = tmp_path / "assignment.pdf"
    result = generate_assignment_contract_artifact(_payload(), output_path)

    assert result["status"] == "SUCCESS"
    assert result["pdf_path"] == str(output_path)
    assert output_path.read_bytes().startswith(b"%PDF-1.4")


def test_pdf_artifact_not_written_for_fatal_error(tmp_path):
    output_path = tmp_path / "assignment.pdf"
    result = generate_assignment_contract_artifact(
        _payload(property_address=""),
        output_path,
    )

    assert result["status"] == "FATAL_ERROR"
    assert not output_path.exists()


def test_vault_artifact_uses_temporary_pdf_and_returns_signed_url():
    class FakeVault:
        def __init__(self):
            self.local_pdf_path = None

        def vault_pdf(self, pdf_file_path, *, client_id, document_id, expiration_seconds):
            self.local_pdf_path = pdf_file_path
            with open(pdf_file_path, "rb") as fh:
                assert fh.read(5) == b"%PDF-"
            return VaultedContract(
                document_id=document_id,
                bucket="contract-bucket",
                s3_key=f"clients/{client_id}/contracts/{document_id}.pdf",
                presigned_url="https://example.test/signed",
                expires_in=expiration_seconds,
            )

    fake = FakeVault()
    result = generate_assignment_contract_vault_artifact(
        _payload(),
        client_id="11111111-1111-1111-1111-111111111111",
        document_id="doc-123",
        vault=fake,
    )

    assert result["status"] == "SUCCESS"
    assert result["document_id"] == "doc-123"
    assert result["presigned_url"] == "https://example.test/signed"
    assert fake.local_pdf_path is not None
    assert not os.path.exists(fake.local_pdf_path)


def test_vault_artifact_rejects_path_traversal_document_id(tmp_path):
    escaped_path = tmp_path / "escaped.pdf"

    with pytest.raises(ValueError, match="document_id"):
        generate_assignment_contract_vault_artifact(
            _payload(),
            client_id="11111111-1111-1111-1111-111111111111",
            document_id=f"../{escaped_path.stem}",
            vault=object(),
        )

    assert not escaped_path.exists()
