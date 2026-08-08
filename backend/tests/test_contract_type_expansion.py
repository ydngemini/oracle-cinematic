"""The expanded workflow categories accept exact templates but add no invented forms."""

from pathlib import Path

from ml_forge.synthetic_lawyer import BUILTIN_CONTRACT_TEMPLATES, validate_contract_template


EXPANDED_TYPES = (
    "buyer_representation",
    "buyer_offer",
    "inspection_repair_request",
    "financing_contingency_addendum",
    "listing_agreement",
    "seller_disclosure",
    "counteroffer_addendum",
    "termination_release",
)


def test_expanded_types_validate_tenant_supplied_exact_templates():
    template = "AUTHORIZED FORM\nParty: {party_name}\nProperty: {property_address}"
    for document_type in EXPANDED_TYPES:
        result = validate_contract_template(
            document_type,
            template,
            ["party_name", "property_address"],
        )
        assert result["valid"] is True
        assert len(result["template_sha256"]) == 64


def test_expansion_does_not_ship_unlicensed_builtin_legal_language():
    builtin_types = {item["document_type"] for item in BUILTIN_CONTRACT_TEMPLATES.values()}
    assert not (set(EXPANDED_TYPES) & builtin_types)

    contracts_source = (
        Path(__file__).parents[1] / "contracts_api.py"
    ).read_text(encoding="utf-8")
    for document_type in EXPANDED_TYPES:
        assert f'"{document_type}"' in contracts_source
