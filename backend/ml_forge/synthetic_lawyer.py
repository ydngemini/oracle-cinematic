"""
Synthetic Lawyer — generates Delaware real estate assignment contract records as JSONL.

Uses Bedrock (Llama 70B primary, DeepSeek v3.2 fallback) to produce
legally-formatted wholesale assignment contracts for training data.
"""

import json
import asyncio
import difflib
import hashlib
import os
import re
import string
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

# Keep deterministic drafting importable in API/test processes that do not
# install the AWS SDK.  The optional synthetic-training path imports Bedrock
# only when it is actually invoked.
PRIMARY_MODEL = "us.meta.llama3-3-70b-instruct-v1:0"
SECONDARY_MODEL = "us.meta.llama3-1-8b-instruct-v1:0"


def invoke_bedrock_model(model_id: str, prompt: str, max_tokens: int = 2048):
    from backend.ml_forge.bedrock_client import invoke_bedrock_model as invoke

    return invoke(model_id, prompt, max_tokens=max_tokens)

TOTAL_RECORDS = 100
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "output",
    "de_assignment_contracts.jsonl",
)
CONTRACT_OUTPUT_DIR = os.environ.get(
    "ORACLE_CONTRACT_OUTPUT_DIR",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "output",
        "contracts",
    ),
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

# These are bootstrap candidates, not silently approved legal forms.  A broker
# may copy them into ``contract_templates`` and an attorney must approve the
# exact checksum/version before the contracts API will render a document.
SELLER_PURCHASE_TEMPLATE = """REAL ESTATE PURCHASE AND SALE AGREEMENT — SELLER FORM
Effective date: {current_date}

Seller: {seller_name}
Buyer: {buyer_name}
Property: {property_address}
Purchase price: ${purchase_price}
Earnest money deposit: ${earnest_money_deposit}
Closing date: {closing_date}

1. PROPERTY AND CONVEYANCE. Seller agrees to convey the Property to Buyer at closing, subject to the approved title, deed, disclosure, and closing requirements for the stated jurisdiction.
2. DUE DILIGENCE. Buyer may complete the inspections and investigations expressly stated in the approved addenda. No condition, title, zoning, tax, or environmental conclusion is made by this draft.
3. TITLE AND CLOSING. The closing professional shall determine acceptable title, payoff, recording, prorations, and funds requirements.
4. DEFAULT AND REMEDIES. The parties' remedies are limited to those in the attorney-approved template and controlling law.
5. ADDENDA. {approved_addenda}

SELLER: ____________________  DATE: __________
BUYER:  ____________________  DATE: __________

PROFESSIONAL REVIEW REQUIRED: This generated draft is not legal advice and must not be signed until the approved template and transaction-specific terms are reviewed by qualified counsel."""

BUYER_PURCHASE_TEMPLATE = """REAL ESTATE PURCHASE OFFER — BUYER FORM
Offer date: {current_date}

Buyer: {buyer_name}
Seller: {seller_name}
Property: {property_address}
Offer amount: ${purchase_price}
Earnest money deposit: ${earnest_money_deposit}
Requested closing date: {closing_date}
Financing: {financing_terms}
Inspection/due-diligence period: {due_diligence_period}

This offer incorporates only the contingencies and addenda expressly listed here: {approved_addenda}
Title, disclosures, prorations, possession, risk of loss, default, and closing obligations are governed by the attorney-approved template for the stated jurisdiction.

BUYER: ____________________  DATE: __________

PROFESSIONAL REVIEW REQUIRED: This generated draft is not legal advice and must not be delivered or signed until reviewed under the brokerage's approval policy."""

JOINT_VENTURE_TEMPLATE = """REAL ESTATE JOINT VENTURE AGREEMENT
Effective date: {current_date}

Party A: {party_a_name}
Party B: {party_b_name}
Project/property: {property_address}
Purpose: {venture_purpose}
Party A contribution: {party_a_contribution}
Party B contribution: {party_b_contribution}
Approved distribution terms: {distribution_terms}
Decision authority: {decision_authority}
Term/termination: {termination_terms}

The parties will maintain truthful books and records, comply with licensing, securities, tax, fair-housing, and real-estate requirements, and use the attorney-approved dispute, indemnity, confidentiality, and wind-down provisions for the jurisdiction.

PARTY A: ____________________  DATE: __________
PARTY B: ____________________  DATE: __________

PROFESSIONAL REVIEW REQUIRED: This generated draft is not legal or tax advice and must be reviewed by qualified counsel before signature or performance."""

REDLINE_REVIEW_TEMPLATE = """DEFENSIVE REDLINE REVIEW PROTOCOL
Version: {protocol_version}

Compare only the supplied original and proposed text. Identify additions, removals, and replacements; flag objective legal/financial risk terms; preserve both source hashes; and make no claim that a clause is enforceable. Every proposed change requires attorney review."""

BUILTIN_CONTRACT_TEMPLATES: dict[str, dict[str, Any]] = {
    "assignment-standard": {
        "document_type": "assignment",
        "jurisdiction": "US-GENERIC",
        "version": "1.0.0",
        "body_template": ASSIGNMENT_CONTRACT_TEMPLATE,
        "required_fields": list(REQUIRED_CONTRACT_VARIABLES),
    },
    "seller-purchase-standard": {
        "document_type": "seller_purchase",
        "jurisdiction": "US-GENERIC",
        "version": "1.0.0",
        "body_template": SELLER_PURCHASE_TEMPLATE,
        "required_fields": [
            "current_date", "seller_name", "buyer_name", "property_address",
            "purchase_price", "earnest_money_deposit", "closing_date",
            "approved_addenda",
        ],
    },
    "buyer-purchase-standard": {
        "document_type": "buyer_purchase",
        "jurisdiction": "US-GENERIC",
        "version": "1.0.0",
        "body_template": BUYER_PURCHASE_TEMPLATE,
        "required_fields": [
            "current_date", "buyer_name", "seller_name", "property_address",
            "purchase_price", "earnest_money_deposit", "closing_date",
            "financing_terms", "due_diligence_period", "approved_addenda",
        ],
    },
    "joint-venture-standard": {
        "document_type": "joint_venture",
        "jurisdiction": "US-GENERIC",
        "version": "1.0.0",
        "body_template": JOINT_VENTURE_TEMPLATE,
        "required_fields": [
            "current_date", "party_a_name", "party_b_name", "property_address",
            "venture_purpose", "party_a_contribution", "party_b_contribution",
            "distribution_terms", "decision_authority", "termination_terms",
        ],
    },
    "defensive-redline-standard": {
        "document_type": "redline",
        "jurisdiction": "US-GENERIC",
        "version": "1.0.0",
        "body_template": REDLINE_REVIEW_TEMPLATE,
        "required_fields": ["protocol_version"],
    },
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


_TEMPLATE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_TEMPLATE_BYTES = 100_000
_MAX_FIELD_CHARS = 4_000
_DOCUMENT_TYPES = {
    "assignment", "seller_purchase", "buyer_purchase", "joint_venture", "redline",
    "account_security_esa",
}
_MONEY_FIELDS = {
    "purchase_price", "wholesale_buy_price", "investor_buy_price",
    "earnest_money", "earnest_money_deposit", "assignment_fee", "offer_amount",
}
_DERIVED_TEMPLATE_FIELDS = {"assignment_fee", "section_4_contingency"}


def template_sha256(body_template: str) -> str:
    return hashlib.sha256(body_template.encode("utf-8")).hexdigest()


def _template_fields(body_template: str) -> tuple[list[str], list[str]]:
    fields: list[str] = []
    invalid: list[str] = []
    try:
        parsed = string.Formatter().parse(body_template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if (
                not _TEMPLATE_FIELD_RE.fullmatch(field_name)
                or format_spec
                or conversion
            ):
                invalid.append(field_name)
            else:
                fields.append(field_name)
    except ValueError:
        invalid.append("malformed_template")
    return list(dict.fromkeys(fields)), list(dict.fromkeys(invalid))


def validate_contract_template(
    document_type: str,
    body_template: str,
    required_fields: list[str],
) -> dict[str, Any]:
    """Validate a versioned template without executing arbitrary formatting.

    Only simple ``{snake_case}`` substitutions are accepted. Attribute access,
    indexing, conversions, and format specs are intentionally rejected.
    """
    issues: list[str] = []
    if document_type not in _DOCUMENT_TYPES:
        issues.append("document_type_invalid")
    if not body_template or len(body_template.encode("utf-8")) > _MAX_TEMPLATE_BYTES:
        issues.append("body_template_size_invalid")
    fields, invalid = _template_fields(body_template)
    issues.extend(f"template_field_invalid:{name}" for name in invalid)
    normalized_required = list(dict.fromkeys(str(v).strip() for v in required_fields))
    for name in normalized_required:
        if not _TEMPLATE_FIELD_RE.fullmatch(name):
            issues.append(f"required_field_invalid:{name}")
    allowed_derived = _DERIVED_TEMPLATE_FIELDS if document_type == "assignment" else set()
    missing_declarations = sorted(set(fields) - set(normalized_required) - allowed_derived)
    if missing_declarations:
        issues.extend(f"placeholder_not_required:{name}" for name in missing_declarations)
    return {
        "valid": not issues,
        "issues": issues,
        "placeholders": fields,
        "required_fields": normalized_required,
        "template_sha256": template_sha256(body_template),
    }


def _safe_contract_scalar(name: str, value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, name
    if isinstance(value, (dict, list, tuple, set)):
        return None, f"{name}_must_be_scalar"
    if name in _MONEY_FIELDS:
        amount = _money(value)
        if amount is None or amount < 0:
            return None, f"{name}_invalid"
        return _money_text(amount), None
    text_value = _date_text(value) or ""
    if (
        not text_value.strip()
        or len(text_value) > _MAX_FIELD_CHARS
        or _CONTROL_CHARACTER_RE.search(text_value)
    ):
        return None, f"{name}_invalid"
    try:
        text_value.encode("cp1252")
    except UnicodeEncodeError:
        return None, f"{name}_unsupported_characters"
    return text_value.strip(), None


def render_approved_contract_template(
    *,
    document_type: str,
    body_template: str,
    required_fields: list[str],
    transaction_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Render a deterministic professional-review draft from an exact template.

    The method returns validation traces, not model reasoning.  The calling API
    is responsible for proving that this exact template checksum/version has an
    approved attorney review row.
    """
    validation = validate_contract_template(document_type, body_template, required_fields)
    if not validation["valid"]:
        return {
            "status": "FATAL_ERROR",
            "missing_variables": validation["issues"],
            "final_contract_text": "",
            "template_sha256": validation["template_sha256"],
        }

    raw: dict[str, Any] = dict(transaction_data)
    assignment_result: dict[str, Any] | None = None
    if document_type == "assignment":
        assignment_result = draft_assignment_contract(transaction_data)
        if assignment_result["status"] != "SUCCESS":
            return {
                **assignment_result,
                "template_sha256": validation["template_sha256"],
            }
        normalized = normalize_assignment_payload(transaction_data)
        wholesale = _money(normalized["wholesale_buy_price"]) or Decimal(0)
        investor = _money(normalized["investor_buy_price"]) or Decimal(0)
        earnest = _money(normalized["earnest_money_deposit"]) or Decimal(0)
        condition = _normalize_condition_flag(normalized.get("condition_flag")) or "None"
        raw.update(normalized)
        raw.update(
            {
                "assignment_fee": investor - wholesale,
                "earnest_money_deposit": earnest,
                "section_4_contingency": CONTINGENCY_CLAUSES.get(condition, ""),
            }
        )

    context: dict[str, str] = {}
    issues: list[str] = []
    for field_name in validation["placeholders"]:
        if field_name == "section_4_contingency":
            # This is controlled text selected from a closed internal map.
            context[field_name] = str(raw.get(field_name) or "")
            continue
        rendered, issue = _safe_contract_scalar(field_name, raw.get(field_name))
        if issue:
            issues.append(issue)
        else:
            context[field_name] = rendered or ""

    for required in validation["required_fields"]:
        if required not in raw or _is_missing(raw.get(required)):
            issues.append(required)
    issues = list(dict.fromkeys(issues))
    if issues:
        return {
            "status": "FATAL_ERROR",
            "missing_variables": issues,
            "final_contract_text": "",
            "template_sha256": validation["template_sha256"],
        }

    try:
        text_value = body_template.format_map(context)
    except (KeyError, ValueError) as exc:
        return {
            "status": "FATAL_ERROR",
            "missing_variables": [f"template_render_failed:{type(exc).__name__}"],
            "final_contract_text": "",
            "template_sha256": validation["template_sha256"],
        }

    input_hash = hashlib.sha256(
        json.dumps(dict(transaction_data), sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = {
        "status": "SUCCESS",
        "document_type": document_type,
        "missing_variables": [],
        "final_contract_text": text_value,
        "template_sha256": validation["template_sha256"],
        "input_sha256": input_hash,
        "content_sha256": hashlib.sha256(text_value.encode("utf-8")).hexdigest(),
        "professional_review_required": True,
        "warnings": [
            "Generated legal draft; not legal advice.",
            "Attorney review and an explicit approval are required before delivery or signature.",
        ],
    }
    if assignment_result is not None:
        result["assignment_fee_calculated"] = assignment_result["assignment_fee_calculated"]
    return result


_WORKSPACE_DATE_FIELDS = {"current_date", "original_contract_date", "closing_date"}


def _workspace_missing_marker(field_name: str) -> str:
    """Return an explicit placeholder instead of inventing a contract term."""
    return f"[[MISSING: {field_name}]]"


def _workspace_error(
    *,
    document_type: str,
    template_sha: str,
    issues: list[str],
) -> dict[str, Any]:
    return {
        "status": "FATAL_ERROR",
        "document_type": document_type,
        "missing_variables": list(dict.fromkeys(issues)),
        "final_contract_text": "",
        "template_sha256": template_sha,
    }


def _workspace_input_hash(transaction_data: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(transaction_data),
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _workspace_draft_text(text_value: str) -> str:
    """Make the downloadable state unambiguous without blocking draft work."""
    return (
        "AI-ASSISTED WORKING DRAFT\n"
        "Preview, save, and device-download copy. Not for signature.\n\n"
        f"{text_value}"
    )


def _validate_workspace_assignment_values(raw: Mapping[str, Any]) -> list[str]:
    """Validate supplied assignment values while allowing genuinely missing ones.

    The full renderer remains the final strict gate.  This narrower check gives
    the draft workspace a useful incomplete state without ever formatting an
    unsafe, malformed, or fabricated term into the preview.
    """
    issues: list[str] = []
    for field_name in _WORKSPACE_DATE_FIELDS:
        value = raw.get(field_name)
        if not _is_missing(value) and _parsed_iso_date(str(value)) is None:
            issues.append(f"{field_name}_invalid")

    current_date = _parsed_iso_date(str(raw.get("current_date") or ""))
    original_date = _parsed_iso_date(str(raw.get("original_contract_date") or ""))
    closing_date = _parsed_iso_date(str(raw.get("closing_date") or ""))
    if current_date and original_date and original_date > current_date:
        issues.append("original_contract_date_after_assignment_date")
    if current_date and closing_date and closing_date < current_date:
        issues.append("closing_date_before_assignment_date")

    wholesale = _money(raw.get("wholesale_buy_price"))
    investor = _money(raw.get("investor_buy_price"))
    earnest = _money(raw.get("earnest_money_deposit"))
    if not _is_missing(raw.get("wholesale_buy_price")) and (
        wholesale is None or wholesale <= 0
    ):
        issues.append("wholesale_buy_price_must_be_positive")
    if not _is_missing(raw.get("investor_buy_price")) and (
        investor is None or investor <= 0
    ):
        issues.append("investor_buy_price_must_be_positive")
    if not _is_missing(raw.get("earnest_money_deposit")):
        if earnest is None or earnest < 0:
            issues.append("earnest_money_deposit_must_be_nonnegative")
        elif earnest != earnest.quantize(Decimal("0.01")):
            issues.append("earnest_money_deposit_has_subcent_precision")
    if wholesale is not None and investor is not None and investor < wholesale:
        issues.append("assignment_fee_negative")
    return issues


def render_contract_workspace_draft(
    *,
    document_type: str,
    body_template: str,
    required_fields: list[str],
    transaction_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Render an encrypted-workspace preview without guessing missing terms.

    Unlike :func:`render_approved_contract_template`, this function intentionally
    permits missing required values.  Each unknown becomes a visible marker so
    a person or Personal AI can finish the draft later.  Supplied values still
    receive the same scalar and injection validation as final rendering.
    """
    validation = validate_contract_template(document_type, body_template, required_fields)
    if not validation["valid"]:
        return _workspace_error(
            document_type=document_type,
            template_sha=validation["template_sha256"],
            issues=validation["issues"],
        )

    raw: dict[str, Any] = dict(transaction_data)
    if document_type == "redline":
        original_text = raw.get("original_text")
        proposed_text = raw.get("proposed_text")
        missing = [
            name
            for name, value in (
                ("original_text", original_text),
                ("proposed_text", proposed_text),
            )
            if _is_missing(value)
        ]
        for field_name, value in (
            ("original_text", original_text),
            ("proposed_text", proposed_text),
        ):
            if _is_missing(value):
                continue
            if not isinstance(value, str) or len(value) > _MAX_TEMPLATE_BYTES or "\x00" in value:
                return _workspace_error(
                    document_type=document_type,
                    template_sha=validation["template_sha256"],
                    issues=[f"{field_name}_invalid"],
                )
            try:
                value.encode("cp1252")
            except UnicodeEncodeError:
                return _workspace_error(
                    document_type=document_type,
                    template_sha=validation["template_sha256"],
                    issues=[f"{field_name}_unsupported_characters"],
                )
        if missing:
            draft = _workspace_draft_text(
                "DEFENSIVE REDLINE REVIEW\n\n"
                f"Original text: {_workspace_missing_marker('original_text')}\n"
                f"Proposed text: {_workspace_missing_marker('proposed_text')}"
            )
            return {
                "status": "INCOMPLETE",
                "document_type": document_type,
                "missing_variables": missing,
                "final_contract_text": draft,
                "template_sha256": validation["template_sha256"],
                "input_sha256": _workspace_input_hash(transaction_data),
                "content_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                "professional_review_required": True,
                "warnings": [
                    "The comparison has not run because both source texts are required.",
                    "Unknown terms are shown as explicit missing-field markers.",
                ],
            }
        result = defensive_redline(str(original_text), str(proposed_text))
        if result["status"] != "SUCCESS":
            return {
                **result,
                "template_sha256": validation["template_sha256"],
            }
        draft = _workspace_draft_text(result["final_contract_text"])
        return {
            **result,
            "status": "READY",
            "final_contract_text": draft,
            "template_sha256": validation["template_sha256"],
            "input_sha256": _workspace_input_hash(transaction_data),
            "content_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
            "warnings": [
                "AI-assisted working draft saved from the supplied source text.",
                *result.get("warnings", []),
            ],
        }

    if document_type == "assignment":
        raw.update(normalize_assignment_payload(transaction_data))
        assignment_issues = _validate_workspace_assignment_values(raw)
        condition_value = raw.get("condition_flag")
        if not _is_missing(condition_value) and _normalize_condition_flag(condition_value) is None:
            assignment_issues.append("condition_flag_invalid")
        if assignment_issues:
            return _workspace_error(
                document_type=document_type,
                template_sha=validation["template_sha256"],
                issues=assignment_issues,
            )
    else:
        date_issues = [
            f"{field_name}_invalid"
            for field_name in _WORKSPACE_DATE_FIELDS
            if not _is_missing(raw.get(field_name))
            and _parsed_iso_date(str(raw.get(field_name))) is None
        ]
        if date_issues:
            return _workspace_error(
                document_type=document_type,
                template_sha=validation["template_sha256"],
                issues=date_issues,
            )

    context: dict[str, str] = {}
    missing: list[str] = []
    scalar_issues: list[str] = []
    for field_name in validation["placeholders"]:
        if field_name == "assignment_fee":
            wholesale = _money(raw.get("wholesale_buy_price"))
            investor = _money(raw.get("investor_buy_price"))
            if wholesale is None or investor is None:
                context[field_name] = _workspace_missing_marker("assignment_fee")
            else:
                context[field_name] = _money_text(investor - wholesale)
            continue
        if field_name == "section_4_contingency":
            condition = _normalize_condition_flag(raw.get("condition_flag"))
            context[field_name] = (
                CONTINGENCY_CLAUSES.get(condition or "None", "")
                if condition is not None
                else _workspace_missing_marker("condition_flag")
            )
            continue
        value = raw.get(field_name)
        if _is_missing(value):
            context[field_name] = _workspace_missing_marker(field_name)
            continue
        rendered, issue = _safe_contract_scalar(field_name, value)
        if issue:
            scalar_issues.append(issue)
        else:
            context[field_name] = rendered or ""

    for field_name in validation["required_fields"]:
        value = raw.get(field_name)
        if _is_missing(value):
            missing.append(field_name)
            continue
        if field_name == "condition_flag":
            continue
        _, issue = _safe_contract_scalar(field_name, value)
        if issue:
            scalar_issues.append(issue)

    if scalar_issues:
        return _workspace_error(
            document_type=document_type,
            template_sha=validation["template_sha256"],
            issues=scalar_issues,
        )
    try:
        preview = body_template.format_map(context)
    except (KeyError, ValueError) as exc:
        return _workspace_error(
            document_type=document_type,
            template_sha=validation["template_sha256"],
            issues=[f"template_render_failed:{type(exc).__name__}"],
        )

    missing = list(dict.fromkeys(missing))
    if not missing:
        strict = render_approved_contract_template(
            document_type=document_type,
            body_template=body_template,
            required_fields=required_fields,
            transaction_data=transaction_data,
        )
        if strict["status"] != "SUCCESS":
            return strict
        preview = strict["final_contract_text"]

    draft = _workspace_draft_text(preview)
    result = {
        "status": "READY" if not missing else "INCOMPLETE",
        "document_type": document_type,
        "missing_variables": missing,
        "final_contract_text": draft,
        "template_sha256": validation["template_sha256"],
        "input_sha256": _workspace_input_hash(transaction_data),
        "content_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        "professional_review_required": True,
        "warnings": [
            "AI-assisted working draft. It is encrypted when saved and can be downloaded to your device.",
            *(
                ["Complete every marked field before moving this draft forward."]
                if missing
                else ["All required values are present; final signature status remains separate."]
            ),
        ],
    }
    if document_type == "assignment" and not missing:
        strict_assignment = draft_assignment_contract(transaction_data)
        result["assignment_fee_calculated"] = strict_assignment.get("assignment_fee_calculated")
    return result


_REDLINE_RISK_TERMS: dict[str, tuple[str, ...]] = {
    "financial": ("price", "fee", "deposit", "liquidated damages", "commission", "payment"),
    "title": ("title", "lien", "encumbrance", "deed", "recording"),
    "remedies": ("default", "indemn", "waiver", "release", "specific performance", "arbitration"),
    "timing": ("closing", "deadline", "inspection", "termination", "notice"),
    "transfer": ("assign", "novation", "successor", "joint venture"),
}


def defensive_redline(original_text: str, proposed_text: str) -> dict[str, Any]:
    """Produce a reproducible, non-opinionated legal change review.

    Risk flags are literal keyword matches, not covert scoring or a claim about
    enforceability.  Both source documents remain authoritative.
    """
    if not original_text or not proposed_text:
        return {"status": "FATAL_ERROR", "missing_variables": ["original_text", "proposed_text"]}
    if max(len(original_text), len(proposed_text)) > _MAX_TEMPLATE_BYTES:
        return {"status": "FATAL_ERROR", "missing_variables": ["document_size_invalid"]}
    if "\x00" in original_text or "\x00" in proposed_text:
        return {"status": "FATAL_ERROR", "missing_variables": ["document_contains_nul"]}
    try:
        original_text.encode("cp1252")
        proposed_text.encode("cp1252")
    except UnicodeEncodeError:
        return {"status": "FATAL_ERROR", "missing_variables": ["document_unsupported_characters"]}

    original_lines = original_text.splitlines()
    proposed_lines = proposed_text.splitlines()
    matcher = difflib.SequenceMatcher(a=original_lines, b=proposed_lines, autojunk=False)
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = "\n".join(original_lines[i1:i2])
        after = "\n".join(proposed_lines[j1:j2])
        probe = f"{before}\n{after}".lower()
        flags = sorted(
            category
            for category, terms in _REDLINE_RISK_TERMS.items()
            if any(term in probe for term in terms)
        )
        changes.append(
            {
                "change_type": tag,
                "original_lines": [i1 + 1, i2],
                "proposed_lines": [j1 + 1, j2],
                "before": before,
                "after": after,
                "literal_risk_flags": flags,
            }
        )

    unified = "\n".join(
        difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile="original",
            tofile="proposed",
            lineterm="",
        )
    )
    report_lines = [
        "DEFENSIVE REDLINE REVIEW",
        "PROFESSIONAL REVIEW REQUIRED — objective text comparison only; not legal advice.",
        f"Original SHA-256: {hashlib.sha256(original_text.encode('utf-8')).hexdigest()}",
        f"Proposed SHA-256: {hashlib.sha256(proposed_text.encode('utf-8')).hexdigest()}",
        f"Detected change groups: {len(changes)}",
        "",
        unified or "No textual changes detected.",
    ]
    final_text = "\n".join(report_lines)
    return {
        "status": "SUCCESS",
        "document_type": "redline",
        "changes": changes,
        "change_count": len(changes),
        "final_contract_text": final_text,
        "original_sha256": hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
        "proposed_sha256": hashlib.sha256(proposed_text.encode("utf-8")).hexdigest(),
        "content_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        "professional_review_required": True,
        "warnings": ["Literal risk flags are review aids, not legal conclusions."],
    }


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
