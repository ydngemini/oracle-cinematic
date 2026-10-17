"""
AGENT LEGAL — contract generation and disclosure checklist module.
"""

import json
from datetime import datetime, timedelta, timezone

# Shown as the Seller until a person states a confirmed one (F11.04). A lead's
# owner_name is the owner of record from an unconfirmed public-record parcel
# match: it is never the contract's Seller, so this module does not read it.
UNCONFIRMED_SELLER = "[SELLER_PLACEHOLDER: seller not confirmed]"


def _stated_seller(property_data: dict) -> str:
    seller = property_data.get("seller_name")
    if isinstance(seller, str) and seller.strip():
        return seller.strip()
    return UNCONFIRMED_SELLER


def generate_legal_package(property_data: dict, strategy: str = "wholesale") -> dict:
    """
    Generate a legal package for a given property and acquisition strategy.

    Args:
        property_data: dict containing address and price (and optionally arv,
                       as_is, assumptions, distress_severity). The Seller comes
                       only from a stated ``seller_name``; without one the
                       field shows UNCONFIRMED_SELLER. ``owner_name`` (the
                       public-record owner of record) is ignored.
        strategy:      "wholesale" (Assignment Contract) or "standard" (Purchase Agreement)

    Returns:
        A dict payload ready for WebSocket dispatch or direct consumption.
    """
    now = datetime.now(tz=timezone.utc)
    closing_date = now + timedelta(days=30)

    # `price` is the MAX ALLOWABLE OFFER (conservative acquisition ceiling for a
    # distressed/off-market target) — NOT retail. arv/as_is are carried for
    # transparency so the contract never hides what the offer is discounted from.
    price = property_data.get("price", property_data.get("market_value", 0))
    arv = property_data.get("arv", 0)
    as_is = property_data.get("as_is", 0)
    earnest_money = round(price * 0.01, 2)

    contract_type = (
        "Assignment Contract" if strategy == "wholesale" else "Purchase Agreement"
    )

    contract_fields = {
        "buyer": "[BUYER_PLACEHOLDER]",
        "seller": _stated_seller(property_data),
        "property_address": property_data.get("address", ""),
        "purchase_price": price,
        "max_allowable_offer": price,
        "arv_estimate": arv,
        "as_is_estimate": as_is,
        "distress_severity": property_data.get("distress_severity", "UNKNOWN"),
        "offer_basis": (
            "Purchase price is the max allowable offer for a distressed/off-market "
            "acquisition (70% rule: 0.70 × ARV − estimated repairs), not retail "
            "value. Figures are model estimates pending interior inspection — not "
            "an appraisal."
        ),
        "valuation_assumptions": property_data.get("assumptions", []),
        "earnest_money": earnest_money,
        "closing_date": closing_date.strftime("%B %d, %Y"),
        "contingencies": [
            "Inspection contingency (10 days)",
            "Financing contingency (21 days)" if strategy != "wholesale" else "Assignment fee disclosure",
            "Clear title contingency",
            "Appraisal contingency" if strategy != "wholesale" else "End-buyer assignment approval",
        ],
    }

    disclosure_checklist = [
        {
            "category": "Structural & Systems",
            "items": [
                "Roof age and condition",
                "HVAC system condition and age",
                "Foundation integrity",
                "Plumbing condition (pipes, water heater)",
                "Electrical system (panel, wiring)",
            ],
        },
        {
            "category": "Environmental Hazards",
            "items": [
                "Lead-based paint (pre-1978 homes)",
                "Asbestos-containing materials",
                "Mold or moisture intrusion",
                "Radon gas levels",
                "Underground storage tanks",
            ],
        },
        {
            "category": "Pest Infestations",
            "items": [
                "Termite or wood-destroying insect activity",
                "Rodent evidence",
                "Bed bug history",
                "Wood-boring insect damage",
            ],
        },
        {
            "category": "Legal & Financial",
            "items": [
                "Outstanding liens or judgments",
                "Easements or right-of-way encumbrances",
                "Boundary or survey disputes",
                "HOA violations or pending fines",
                "Unpaid assessments or back taxes",
            ],
        },
        {
            "category": "Neighborhood Nuisances",
            "items": [
                "Noise or nuisance complaints",
                "Odor or industrial proximity",
                "Flooding or drainage history",
                "Planned nearby developments",
                "Sex offender registry proximity",
            ],
        },
    ]

    return {
        "contract_type": contract_type,
        "contract_fields": contract_fields,
        "disclosure_checklist": disclosure_checklist,
        "generated_at": now.isoformat(),
        "agent": "AGENT LEGAL",
    }


def format_for_websocket(property_data: dict, strategy: str = "wholesale") -> str:
    """
    Build a JSON string ready to pass directly to websocket.send_text().

    Returns:
        JSON-serialised string: {"type": "LEGAL_PACKAGE", "data": {...}}
    """
    package = generate_legal_package(property_data, strategy)
    return json.dumps({"type": "LEGAL_PACKAGE", "data": package})
