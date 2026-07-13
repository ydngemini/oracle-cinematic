"""
Synthetic Lawyer — generates Delaware real estate assignment contract records as JSONL.

Uses Bedrock (Llama 70B primary, DeepSeek v3.2 fallback) to produce
legally-formatted wholesale assignment contracts for training data.
"""

import json
import asyncio
import os
import re
import sys
import time
import random
import textwrap
import tempfile
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.ml_forge.bedrock_client import (
    invoke_bedrock_model,
    PRIMARY_MODEL,
    SECONDARY_MODEL,
)

TOTAL_RECORDS = 100
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "output",
    "de_assignment_contracts.jsonl",
)
CONTRACT_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "output",
    "contracts",
)

DEFAULT_ASSIGNOR_NAME = os.getenv("ORACLE_ASSIGNOR_NAME", "Neoh Acquisitions LLC")

REQUIRED_CONTRACT_VARIABLES = (
    "current_date",
    "assignor_name",
    "assignee_name",
    "seller_name",
    "property_address",
    "original_contract_date",
    "wholesale_buy_price",
    "investor_buy_price",
    "earnest_money_deposit",
    "closing_date",
    "condition_flag",
)

_PLAIN_TEXT_LIMITS = {
    "assignor_name": 200,
    "assignee_name": 200,
    "seller_name": 200,
    "property_address": 500,
}
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_MONEY_VALUE = Decimal("1000000000000")
_MAX_PRESIGNED_URL_SECONDS = 3600

ASSIGNMENT_CONTRACT_TEMPLATE = """ASSIGNMENT OF REAL ESTATE PURCHASE AND SALE AGREEMENT
THIS ASSIGNMENT OF REAL ESTATE PURCHASE AND SALE AGREEMENT (this "Assignment") is made and entered into as of {current_date}, by and between:

ASSIGNOR: {assignor_name} (Your LLC / The Wholesaler)

ASSIGNEE: {assignee_name} (The Cash Buyer / Investor)

RECITALS

WHEREAS, Assignor entered into a certain Real Estate Purchase and Sale Agreement (the "Original Agreement") dated {original_contract_date}, with {seller_name} ("Seller"), for the purchase and sale of that certain real property located at:

Property Address: {property_address} (the "Property").

WHEREAS, Assignor desires to assign, transfer, sell and convey to Assignee all of Assignor's right, title and interest in, to and under the Original Agreement, and Assignee desires to receive and accept such assignment.

AGREEMENT

NOW, THEREFORE, for and in consideration of the sum of ${assignment_fee} (the "Assignment Fee") and other good and valuable consideration, the receipt and sufficiency of which are hereby acknowledged, the parties agree as follows:

1. Assignment: Assignor hereby assigns, transfers, and conveys to Assignee all of Assignor's rights, title, and interest in and to the Original Agreement.

2. Acceptance and Assumption: Assignee hereby accepts the within assignment and assumes all of the terms, covenants, conditions, and obligations of Assignor under the Original Agreement. Assignee agrees to be bound by all the terms and conditions of the Original Agreement and to close the transaction on or before {closing_date}.

3. Assignment Fee: Assignee agrees to pay Assignor the non-refundable Assignment Fee of ${assignment_fee}.

An earnest money deposit in the amount of ${earnest_money_deposit} shall be paid by Assignee to the closing title company/attorney within 48 hours of executing this Assignment.

The balance of the Assignment Fee shall be paid to Assignor at closing.

4. "As-Is" Condition: Assignee acknowledges they are purchasing the Property in "AS-IS" condition. Assignee has conducted their own due diligence and is not relying on any representations or warranties from Assignor regarding the condition of the Property.{section_4_contingency}

5. Default: If Assignee fails to close on the Property by the Closing Date, the Earnest Money Deposit shall be forfeited to Assignor as liquidated damages, and this Assignment shall become null and void. Assignor shall retain the right to close on the Property themselves or assign to another party.

IN WITNESS WHEREOF, the parties have executed this Assignment as of the date first above written.

ASSIGNOR: Signature: ___________________________

Name: {assignor_name}

Date: ___________________________

ASSIGNEE: Signature: ___________________________

Name: {assignee_name}

Date: ___________________________"""

CONTINGENCY_CLAUSES = {
    "Pre-Foreclosure": (
        "\n\n4(a). Pre-Foreclosure Contingency: Assignee acknowledges that the "
        "Property has been flagged as Pre-Foreclosure. This Assignment is "
        "contingent upon the Original Agreement remaining enforceable through "
        "closing, Seller's delivery of marketable title, and receipt of payoff "
        "figures or closing instructions sufficient for the closing title "
        "company/attorney to satisfy foreclosure-related liens from closing "
        "proceeds."
    ),
    "Probate": (
        "\n\n4(a). Probate Contingency: Assignee acknowledges that the Property "
        "has been flagged as Probate. This Assignment is contingent upon receipt "
        "of any required probate court, personal representative, executor, "
        "administrator, or estate authority necessary for Seller to convey "
        "marketable title at closing."
    ),
}

DE_COUNTIES = ["New Castle", "Kent", "Sussex"]
DE_CITIES = {
    "New Castle": ["Wilmington", "Newark", "Bear", "Hockessin", "Middletown", "New Castle", "Pike Creek"],
    "Kent": ["Dover", "Smyrna", "Milford", "Camden", "Harrington"],
    "Sussex": ["Rehoboth Beach", "Lewes", "Georgetown", "Seaford", "Millsboro"],
}

PROMPT_TEMPLATE = """Generate a realistic Delaware real estate wholesale assignment contract record as valid JSON.

Requirements:
- This is record {record_num} of a training dataset
- County: {county}
- City: {city}
- The contract must include all legally required fields for a DE assignment of contract
- Assignment fee should be between $5,000 and $35,000
- Original purchase price between $80,000 and $450,000
- Property must be residential (SFR, duplex, or townhome)
- Include realistic Delaware-specific legal language in the clauses
- All dates should be in 2025-2026
- Generate unique names, addresses, and parcel IDs

Output ONLY valid JSON with these exact keys:
{{
  "record_id": "DE-ASG-XXXXX",
  "assignor": {{"name": "", "address": "", "entity_type": "individual|llc"}},
  "assignee": {{"name": "", "address": "", "entity_type": "individual|llc"}},
  "seller": {{"name": "", "address": ""}},
  "property": {{
    "address": "",
    "city": "",
    "county": "",
    "state": "DE",
    "zip": "",
    "parcel_id": "",
    "property_type": "SFR|duplex|townhome",
    "bedrooms": 0,
    "bathrooms": 0,
    "sqft": 0,
    "year_built": 0
  }},
  "original_contract": {{
    "purchase_price": 0,
    "earnest_money": 0,
    "execution_date": "",
    "closing_deadline": "",
    "contingencies": []
  }},
  "assignment": {{
    "assignment_fee": 0,
    "total_price_to_assignee": 0,
    "assignment_date": "",
    "consideration_paid": 0,
    "deposit_held_by": ""
  }},
  "legal_clauses": {{
    "assignability_clause": "",
    "indemnification": "",
    "governing_law": "",
    "dispute_resolution": "",
    "closing_obligations": ""
  }},
  "notarization": {{
    "notary_name": "",
    "commission_expiry": "",
    "county_recorded": "",
    "recording_ref": ""
  }}
}}

Output ONLY the JSON object, no markdown, no explanation."""


def _loads(value: Any, default: Any = None) -> Any:
    """Normalize json/jsonb values that asyncpg may return as strings."""
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {} if default is None else default
    return value


def _nested_get(data: Mapping[str, Any], dotted_key: str) -> Any:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first_value(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = _nested_get(data, key) if "." in key else data.get(key)
        if value is not None and value != "":
            return value
    return None


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, int):
            amount = Decimal(value)
        elif isinstance(value, float):
            amount = Decimal(str(value))
        elif isinstance(value, str):
            cleaned = value.strip().replace("$", "").replace(",", "")
            if not cleaned:
                return None
            amount = Decimal(cleaned)
        else:
            amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or abs(amount) > _MAX_MONEY_VALUE:
        return None
    return amount


def _date_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _money_text(value: Decimal) -> str:
    integral = value == value.to_integral_value()
    if integral:
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _whole_dollar_int(value: Decimal) -> int | None:
    if value != value.to_integral_value():
        return None
    return int(value)


def _parsed_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _condition_from_flags(flags: Any) -> str | None:
    if not isinstance(flags, list):
        return None
    normalized = {str(flag).strip().lower().replace(" ", "_").replace("-", "_") for flag in flags}
    if "pre_foreclosure" in normalized or "foreclosure" in normalized:
        return "Pre-Foreclosure"
    if "probate" in normalized:
        return "Probate"
    return "None"


def _normalize_condition_flag(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    key = raw.lower().replace("_", "-").replace(" ", "-")
    if key in {"", "none", "n/a", "na", "null"}:
        return "None"
    if key in {"pre-foreclosure", "foreclosure"}:
        return "Pre-Foreclosure"
    if key == "probate":
        return "Probate"
    return None


def _property_address(data: Mapping[str, Any]) -> Any:
    explicit = _first_value(data, "property_address", "Property Address", "payload.address")
    if explicit:
        return explicit

    prop = data.get("property")
    if not isinstance(prop, Mapping):
        return None

    pieces = [
        prop.get("address"),
        prop.get("city"),
        prop.get("state"),
        prop.get("zip"),
    ]
    if any(pieces):
        return ", ".join(str(piece).strip() for piece in pieces if piece)
    return None


def normalize_assignment_payload(transaction_data: Mapping[str, Any]) -> dict[str, Any]:
    """Map the app's known CRM/lead/synthetic shapes onto the contract fields.

    This function only remaps supplied values. It does not invent required deal
    terms; draft_assignment_contract() is the fatal gatekeeper.
    """
    payload = _loads(transaction_data.get("payload"), {}) or {}
    underwriting = _loads(transaction_data.get("underwriting"), {}) or {}
    acquisition_entity = _loads(transaction_data.get("acquisition_entity"), {}) or {}

    data: dict[str, Any] = {
        **transaction_data,
        "payload": payload,
        "underwriting": underwriting,
        "acquisition_entity": acquisition_entity,
    }

    condition_flag = _first_value(
        data,
        "condition_flag",
        "special_condition_flag",
        "Special Condition Flag",
        "payload.condition_flag",
        "payload.special_condition_flag",
        "underwriting.condition_flag",
    )
    if condition_flag is None:
        condition_flag = _condition_from_flags(
            payload.get("distress_flags") or underwriting.get("distress_flags")
        )

    return {
        "current_date": _date_text(_first_value(data, "current_date", "Current Date")),
        "assignor_name": _first_value(
            data,
            "assignor_name",
            "assignor.name",
            "Assignor (Wholesaler)",
            "acquisition_entity.entity_name",
        ),
        "assignee_name": _first_value(
            data,
            "assignee_name",
            "assignee.name",
            "buyer.full_name",
            "client.full_name",
            "Assignee (Cash Buyer)",
        ),
        "seller_name": _first_value(
            data,
            "seller_name",
            "seller.name",
            "seller_full_name",
            "payload.seller_name",
            "payload.owner_name",
            "Original Seller",
        ),
        "property_address": _property_address(data),
        "original_contract_date": _date_text(
            _first_value(
                data,
                "original_contract_date",
                "original_contract.execution_date",
                "contract_execution_date",
                "payload.original_contract_date",
            )
        ),
        "wholesale_buy_price": _first_value(
            data,
            "wholesale_buy_price",
            "original_contract.purchase_price",
            "purchase_price",
            "contract_price",
            "underwriting.wholesale_buy_price",
            "underwriting.contract_price",
            "underwriting.mao",
            "underwriting.max_allowable_offer",
            "underwriting.investor_offer",
        ),
        "investor_buy_price": _first_value(
            data,
            "investor_buy_price",
            "assignment.total_price_to_assignee",
            "assignment_total_price",
            "total_price_to_assignee",
            "listing_price",
            "listing.price",
            "underwriting.investor_buy_price",
            "underwriting.disposition_price",
        ),
        "earnest_money_deposit": _first_value(
            data,
            "earnest_money_deposit",
            "earnest_money",
            "original_contract.earnest_money",
            "payload.earnest_money",
            "payload.earnest_money_deposit",
        ),
        "closing_date": _date_text(
            _first_value(
                data,
                "closing_date",
                "target_closing_date",
                "original_contract.closing_deadline",
                "contract_expires_at",
                "payload.closing_date",
            )
        ),
        "condition_flag": condition_flag,
        "_missing_variables": list(transaction_data.get("_missing_variables") or []),
    }


def draft_assignment_contract(transaction_data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the strict NEOH-COUNSEL JSON payload for one assignment contract."""
    normalized = normalize_assignment_payload(transaction_data)

    missing = list(normalized.get("_missing_variables") or [])
    for key in REQUIRED_CONTRACT_VARIABLES:
        if _is_missing(normalized.get(key)):
            missing.append(key)

    wholesale = _money(normalized.get("wholesale_buy_price"))
    investor = _money(normalized.get("investor_buy_price"))
    earnest = _money(normalized.get("earnest_money_deposit"))

    if wholesale is None:
        missing.append("wholesale_buy_price")
    if investor is None:
        missing.append("investor_buy_price")
    if earnest is None:
        missing.append("earnest_money_deposit")

    for key, max_length in _PLAIN_TEXT_LIMITS.items():
        value = normalized.get(key)
        if not _is_missing(value) and (
            not isinstance(value, str)
            or len(value) > max_length
            or _CONTROL_CHARACTER_RE.search(value)
        ):
            missing.append(f"{key}_invalid")
        elif isinstance(value, str):
            try:
                value.encode("cp1252")
            except UnicodeEncodeError:
                missing.append(f"{key}_unsupported_characters")

    current_date = _parsed_iso_date(normalized.get("current_date"))
    original_date = _parsed_iso_date(normalized.get("original_contract_date"))
    closing_date = _parsed_iso_date(normalized.get("closing_date"))
    if not _is_missing(normalized.get("current_date")) and current_date is None:
        missing.append("current_date_invalid")
    if not _is_missing(normalized.get("original_contract_date")) and original_date is None:
        missing.append("original_contract_date_invalid")
    if not _is_missing(normalized.get("closing_date")) and closing_date is None:
        missing.append("closing_date_invalid")
    if current_date and original_date and original_date > current_date:
        missing.append("original_contract_date_after_assignment_date")
    if current_date and closing_date and closing_date < current_date:
        missing.append("closing_date_before_assignment_date")

    if wholesale is not None and wholesale <= 0:
        missing.append("wholesale_buy_price_must_be_positive")
    if investor is not None and investor <= 0:
        missing.append("investor_buy_price_must_be_positive")
    if earnest is not None and earnest < 0:
        missing.append("earnest_money_deposit_must_be_nonnegative")
    if earnest is not None and earnest != earnest.quantize(Decimal("0.01")):
        missing.append("earnest_money_deposit_has_subcent_precision")

    condition_flag = _normalize_condition_flag(normalized.get("condition_flag"))
    if normalized.get("condition_flag") is not None and condition_flag is None:
        missing.append("condition_flag_invalid")

    assignment_fee = Decimal(0)
    assignment_fee_int = 0
    if wholesale is not None and investor is not None:
        assignment_fee = investor - wholesale
        whole_fee = _whole_dollar_int(assignment_fee)
        if assignment_fee < 0:
            missing.append("assignment_fee_negative")
        elif whole_fee is None:
            missing.append("assignment_fee_must_be_whole_dollars")
        else:
            assignment_fee_int = whole_fee

    missing = list(dict.fromkeys(missing))
    if missing:
        return {
            "status": "FATAL_ERROR",
            "missing_variables": missing,
            "assignment_fee_calculated": assignment_fee_int,
            "final_contract_text": "",
        }

    section_4_contingency = CONTINGENCY_CLAUSES.get(condition_flag or "None", "")
    contract_text = ASSIGNMENT_CONTRACT_TEMPLATE.format(
        current_date=normalized["current_date"],
        assignor_name=normalized["assignor_name"],
        assignee_name=normalized["assignee_name"],
        seller_name=normalized["seller_name"],
        property_address=normalized["property_address"],
        original_contract_date=normalized["original_contract_date"],
        closing_date=normalized["closing_date"],
        assignment_fee=_money_text(assignment_fee),
        earnest_money_deposit=_money_text(earnest or Decimal(0)),
        section_4_contingency=section_4_contingency,
    )

    return {
        "status": "SUCCESS",
        "missing_variables": [],
        "assignment_fee_calculated": assignment_fee_int,
        "final_contract_text": contract_text,
    }


def draft_assignment_contract_json(transaction_data: Mapping[str, Any]) -> str:
    """Return ONLY the minified JSON object expected by the contract prompt."""
    return json.dumps(
        draft_assignment_contract(transaction_data),
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _pdf_escape(text: str) -> str:
    safe = text.encode("cp1252").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_contract_pdf(contract_text: str, output_path: str | os.PathLike[str]) -> str:
    """Write a clean, text-only letter PDF using only the standard library."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    page_width = 612
    page_height = 792
    margin_x = 54
    top_y = 742
    font_size = 10.5
    leading = 14
    wrap_width = 91
    lines_per_page = 48

    blocks: list[list[str]] = []
    for paragraph in contract_text.split("\n\n"):
        block: list[str] = []
        for raw in paragraph.splitlines():
            block.extend(
                textwrap.wrap(
                    raw,
                    width=wrap_width,
                    break_long_words=True,
                    replace_whitespace=False,
                )
                or [""]
            )
        if blocks:
            block.insert(0, "")
        blocks.append(block)

    pages: list[list[str]] = []
    current_page: list[str] = []
    for block in blocks:
        candidate = block
        if current_page and len(current_page) + len(candidate) > lines_per_page:
            pages.append(current_page)
            current_page = []
            candidate = candidate[1:] if candidate and candidate[0] == "" else candidate
        while len(candidate) > lines_per_page:
            pages.append(candidate[:lines_per_page])
            candidate = candidate[lines_per_page:]
        current_page.extend(candidate)
    if current_page or not pages:
        pages.append(current_page)

    objects: list[bytes] = [
        b"",
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    page_ids: list[int] = []

    for page_lines in pages:
        commands = [
            "BT",
            f"/F1 {font_size} Tf",
            f"{leading} TL",
            f"{margin_x} {top_y} Td",
        ]
        for line in page_lines:
            if line:
                commands.append(f"({_pdf_escape(line)}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", "replace")

        content_id = len(objects)
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\n"
            b"stream\n" + stream + b"\nendstream"
        )
        page_id = len(objects)
        page_ids.append(page_id)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("ascii")
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects[1:], start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    xref_at = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects)} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n"
        ).encode("ascii")
    )

    output.write_bytes(pdf)
    return str(output)


def _safe_pdf_name(client_id: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "-", client_id).strip("-")
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"assignment_{token}_{stamp}.pdf"


def _vault_identifiers(
    client_id: str,
    document_id: str | None,
    expiration_seconds: int,
) -> tuple[str, str, int]:
    try:
        safe_client_id = str(uuid.UUID(str(client_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("client_id must be a UUID") from exc

    safe_document_id = str(document_id or uuid.uuid4()).strip()
    if not _SAFE_DOCUMENT_ID_RE.fullmatch(safe_document_id):
        raise ValueError("document_id contains unsupported characters")

    try:
        safe_expiration = int(expiration_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("expiration_seconds must be an integer") from exc
    if not 1 <= safe_expiration <= _MAX_PRESIGNED_URL_SECONDS:
        raise ValueError(
            f"expiration_seconds must be between 1 and {_MAX_PRESIGNED_URL_SECONDS}"
        )
    return safe_client_id, safe_document_id, safe_expiration


def generate_assignment_contract_artifact(
    transaction_data: Mapping[str, Any],
    output_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Draft the strict JSON payload and, on success, emit the PDF artifact."""
    result = draft_assignment_contract(transaction_data)
    if result["status"] != "SUCCESS":
        return result
    pdf_path = write_contract_pdf(result["final_contract_text"], output_path)
    return {**result, "pdf_path": pdf_path}


def generate_assignment_contract_vault_artifact(
    transaction_data: Mapping[str, Any],
    *,
    client_id: str,
    document_id: str | None = None,
    expiration_seconds: int = 3600,
    vault: Any = None,
) -> dict[str, Any]:
    """Draft, render to a temporary PDF, upload to S3, and return a signed URL."""
    result = draft_assignment_contract(transaction_data)
    if result["status"] != "SUCCESS":
        return result

    safe_client_id, safe_document_id, safe_expiration = _vault_identifiers(
        client_id,
        document_id,
        expiration_seconds,
    )
    with tempfile.TemporaryDirectory(prefix="oracle_contract_") as tmp_dir:
        pdf_path = Path(tmp_dir) / f"{safe_document_id}.pdf"
        write_contract_pdf(result["final_contract_text"], pdf_path)

        if vault is None:
            try:
                from contract_vault import SovereignVault
            except ImportError:
                from backend.contract_vault import SovereignVault

            vault = SovereignVault()

        vaulted = vault.vault_pdf(
            pdf_path,
            client_id=safe_client_id,
            document_id=safe_document_id,
            expiration_seconds=safe_expiration,
        )

    return {**result, **vaulted.to_dict()}


def _today_for_contract() -> str:
    return date.today().isoformat()


async def fetch_assignment_transaction_for_client(client_id: str, ctx: Any) -> dict[str, Any]:
    """Pull the latest buyer-linked deal facts for a client from Postgres.

    The query is parameterized and runs through tenant_tx(ctx), so RLS supplies
    tenant isolation. It intentionally returns sparse data; the drafting gate
    decides whether the contract can be generated.
    """
    try:
        uuid.UUID(str(client_id))
    except (TypeError, ValueError, AttributeError):
        return {"_missing_variables": ["client_id_invalid"], "current_date": _today_for_contract()}

    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            WITH buyer AS (
                SELECT id, full_name
                  FROM clients
                 WHERE id = $1
            ),
            latest_showing AS (
                SELECT client_id, listing_id, lead_id, shown_at
                  FROM showings
                 WHERE client_id = $1
                 ORDER BY shown_at DESC
                 LIMIT 1
            )
            SELECT
                buyer.full_name AS assignee_name,
                COALESCE(listings.address, leads.address, leads.payload->>'address') AS property_address,
                COALESCE(seller.full_name, leads.payload->>'seller_name', leads.payload->>'owner_name') AS seller_name,
                leads.contract_execution_date,
                leads.contract_expires_at,
                leads.underwriting,
                leads.payload,
                leads.acquisition_entity,
                COALESCE(listings.price, leads.asking_price) AS listing_price
              FROM buyer
              LEFT JOIN latest_showing ON latest_showing.client_id = buyer.id
              LEFT JOIN listings ON listings.id = latest_showing.listing_id
              LEFT JOIN leads ON leads.id = COALESCE(latest_showing.lead_id, listings.lead_id)
              LEFT JOIN clients seller ON seller.id = COALESCE(listings.seller_client_id, leads.seller_client_id)
            """,
            client_id,
        )

    if row is None:
        return {"_missing_variables": ["client_id_not_found"], "current_date": _today_for_contract()}

    payload = _loads(row["payload"], {}) or {}
    underwriting = _loads(row["underwriting"], {}) or {}
    acquisition_entity = _loads(row["acquisition_entity"], {}) or {}

    return {
        "current_date": _today_for_contract(),
        "assignor_name": acquisition_entity.get("entity_name") or DEFAULT_ASSIGNOR_NAME,
        "assignee_name": row["assignee_name"],
        "seller_name": row["seller_name"],
        "property_address": row["property_address"],
        "original_contract_date": row["contract_execution_date"],
        "closing_date": row["contract_expires_at"],
        "wholesale_buy_price": _first_value(
            {"underwriting": underwriting, "payload": payload},
            "underwriting.wholesale_buy_price",
            "underwriting.contract_price",
            "underwriting.mao",
            "underwriting.max_allowable_offer",
            "underwriting.investor_offer",
            "payload.wholesale_buy_price",
            "payload.contract_price",
        ),
        "investor_buy_price": _first_value(
            {"underwriting": underwriting, "payload": payload, "listing_price": row["listing_price"]},
            "underwriting.investor_buy_price",
            "underwriting.disposition_price",
            "underwriting.total_price_to_assignee",
            "payload.investor_buy_price",
            "payload.total_price_to_assignee",
            "listing_price",
        ),
        "earnest_money_deposit": _first_value(
            {"underwriting": underwriting, "payload": payload},
            "underwriting.earnest_money",
            "payload.earnest_money",
            "payload.earnest_money_deposit",
        ),
        "condition_flag": (
            _first_value(
                {"underwriting": underwriting, "payload": payload},
                "underwriting.condition_flag",
                "payload.condition_flag",
                "payload.special_condition_flag",
            )
            or _condition_from_flags(payload.get("distress_flags") or underwriting.get("distress_flags"))
            or "None"
        ),
        "payload": payload,
        "underwriting": underwriting,
        "acquisition_entity": acquisition_entity,
    }


async def generate_assignment_contract_for_client(
    client_id: str,
    ctx: Any,
    output_dir: str | os.PathLike[str] = CONTRACT_OUTPUT_DIR,
    *,
    vault: Any = None,
    store_in_vault: bool = True,
    expiration_seconds: int = 3600,
) -> dict[str, Any]:
    """Fetch a client's latest deal, draft the contract, and store the PDF."""
    transaction = await fetch_assignment_transaction_for_client(client_id, ctx)
    if store_in_vault:
        return await asyncio.to_thread(
            generate_assignment_contract_vault_artifact,
            transaction,
            client_id=client_id,
            vault=vault,
            expiration_seconds=expiration_seconds,
        )

    output_path = Path(output_dir) / _safe_pdf_name(client_id)
    return generate_assignment_contract_artifact(transaction, output_path)


def generate_record(record_num: int) -> dict | None:
    county = random.choice(DE_COUNTIES)
    city = random.choice(DE_CITIES[county])

    prompt = PROMPT_TEMPLATE.format(
        record_num=record_num,
        county=county,
        city=city,
    )

    result = invoke_bedrock_model(PRIMARY_MODEL, prompt, max_tokens=2048)

    if not result:
        result = invoke_bedrock_model(SECONDARY_MODEL, prompt, max_tokens=2048)

    if not result:
        return None

    try:
        json_start = result.find("{")
        json_end = result.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            record = json.loads(result[json_start:json_end])
            record["_meta"] = {
                "generated_at": datetime.utcnow().isoformat(),
                "model": PRIMARY_MODEL,
                "record_num": record_num,
            }
            return record
    except json.JSONDecodeError:
        pass

    return None


def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    print(f"{'='*70}")
    print(f"  SYNTHETIC LAWYER — Delaware Assignment Contract Generator")
    print(f"  Models: {PRIMARY_MODEL} (primary) | {SECONDARY_MODEL} (fallback)")
    print(f"  Target: {TOTAL_RECORDS} JSONL records")
    print(f"  Output: {OUTPUT_PATH}")
    print(f"{'='*70}\n")

    success = 0
    failed = 0
    t_start = time.time()

    with open(OUTPUT_PATH, "w") as f:
        for i in range(1, TOTAL_RECORDS + 1):
            record = generate_record(i)

            if record:
                line = json.dumps(record)
                f.write(line + "\n")
                f.flush()
                success += 1

                prop = record.get("property", {})
                asg = record.get("assignment", {})
                print(
                    f"[{i:03d}/{TOTAL_RECORDS}] "
                    f"{record.get('record_id', 'N/A'):<16} "
                    f"{prop.get('address', ''):<35} "
                    f"{prop.get('city', ''):<15} "
                    f"${asg.get('assignment_fee', 0):>8,} fee | "
                    f"${asg.get('total_price_to_assignee', 0):>10,} total"
                )
            else:
                failed += 1
                print(f"[{i:03d}/{TOTAL_RECORDS}] FAILED — retrying next record")

            time.sleep(0.2)

    elapsed = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"  COMPLETE: {success} records generated, {failed} failed")
    print(f"  Time: {elapsed:.1f}s ({elapsed/TOTAL_RECORDS:.2f}s/record)")
    print(f"  Output: {OUTPUT_PATH}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
