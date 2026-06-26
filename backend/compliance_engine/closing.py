"""
ClosingEngine — determines the closing process for any US state transaction.

Closing process types:
    attorney_close  — Attorneys required at closing
    title_company   — Independent title company closes
    escrow          — Escrow officer handles closing
    hybrid          — Mix within the state (e.g. IL by county)

Funding types:
    wet   — Funds disbursed same day as signing
    dry   — Funds held, disbursed after recording (common in escrow states)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


_CLOSING_PROFILES: Dict[str, Dict[str, Any]] = {
    "AL": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",    "notes": "Attorneys required in AL closings."},
    "AK": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "none",       "notes": "No state transfer tax. Title/escrow companies common."},
    "AZ": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "Title company escrow common; community property state."},
    "AR": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys handle AR closings."},
    "CA": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "Escrow companies standard. Dry funding; county documentary transfer tax."},
    "CO": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Title companies and attorneys both common."},
    "CT": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required. State + municipal transfer tax."},
    "DE": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "split",      "notes": "Attorneys required. Buyer and seller split 4% transfer tax."},
    "DC": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "split",      "notes": "High recordation and transfer taxes, split buyer/seller."},
    "FL": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Title companies dominate. Doc stamps on deed paid by seller."},
    "GA": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required. Intangible tax on new mortgages."},
    "HI": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "Escrow companies handle closings. Tiered conveyance tax."},
    "ID": {"method": "title_company",   "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "No state transfer tax."},
    "IL": {"method": "hybrid",          "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys common in Cook/collar counties. Title in downstate."},
    "IN": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Title companies. Auditor's sales disclosure required."},
    "IA": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Title/abstract companies common."},
    "KS": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Title companies and attorneys both used."},
    "KY": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys typically handle KY closings."},
    "LA": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Notarial act required — must be executed before notary."},
    "ME": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "split",      "notes": "Attorneys handle ME closings. Transfer tax split."},
    "MD": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "split",      "notes": "Settlement attorneys common. State + county taxes."},
    "MA": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required. Excise stamps paid by seller."},
    "MI": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Title companies or attorneys. State + county transfer taxes."},
    "MN": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Deed tax paid by seller."},
    "MS": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys typically handle closings."},
    "MO": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "negotiable", "notes": "No state transfer tax."},
    "MT": {"method": "title_company",   "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "No state transfer tax."},
    "NE": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Documentary stamp tax on seller."},
    "NV": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "Escrow companies standard. County transfer tax."},
    "NH": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "split",      "notes": "Attorneys handle NH closings. RETT split."},
    "NJ": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required. Realty Transfer Fee (RTF)."},
    "NM": {"method": "title_company",   "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "Community property state."},
    "NY": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required. NYC has additional transfer taxes."},
    "NC": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required. Excise tax on deed."},
    "ND": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Documentary fee."},
    "OH": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Conveyance fee by seller."},
    "OK": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Documentary stamp tax."},
    "OR": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "No state transfer tax but some municipalities add one."},
    "PA": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "split",      "notes": "2% state RTT split; municipalities add more. Philadelphia: ~4.278% total."},
    "RI": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys handle closings."},
    "SC": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Attorneys required."},
    "SD": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Transfer tax on seller."},
    "TN": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Realty transfer tax."},
    "TX": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "negotiable", "notes": "No state transfer tax. Community property state."},
    "UT": {"method": "title_company",   "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "No state transfer tax."},
    "VT": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Property transfer tax."},
    "VA": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "split",      "notes": "Grantor's tax (seller) + recordation tax (buyer)."},
    "WA": {"method": "escrow",          "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "REET tiered by price; some cities add local REET."},
    "WV": {"method": "attorney_close",  "funding": "wet",  "transfer_tax_payer": "split",      "notes": "Attorneys required. Excise tax split."},
    "WI": {"method": "title_company",   "funding": "wet",  "transfer_tax_payer": "seller",     "notes": "Real estate transfer return fee."},
    "WY": {"method": "title_company",   "funding": "dry",  "transfer_tax_payer": "seller",     "notes": "No state transfer tax."},
}

ATTORNEY_CLOSE_STATES = {
    k for k, v in _CLOSING_PROFILES.items()
    if v["method"] in ("attorney_close", "hybrid")
}
DRY_FUNDING_STATES = {
    k for k, v in _CLOSING_PROFILES.items()
    if v["funding"] == "dry"
}
COMMUNITY_PROPERTY_STATES = {"AZ", "CA", "ID", "LA", "NM", "NV", "TX", "WA", "WI"}

# States/jurisdictions that permit Remote Online Notarization (RON). DC added
# per RULONA (D.C. Law 22-189). CA (SB 696, eff. Jan 2024), NJ (P.L.2021 c.179),
# NC (S.L. 2022-54), RI, and ME also authorize RON and were previously omitted.
# NOTE: legal data — confirm against current state statutes before relying on it
# for a given transaction.
_RON_PERMITTED_STATES = {
    "AK", "AL", "AR", "AZ", "CA", "CO", "DC", "FL", "HI", "IA", "ID", "IN",
    "KS", "KY", "LA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND",
    "NE", "NH", "NJ", "NV", "NY", "OH", "OK", "OR", "PA", "RI", "SD", "TN",
    "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
}


@dataclass
class ClosingProfile:
    """The closing process profile for a given transaction."""

    state_code: str
    closing_method: str
    funding_type: str
    transfer_tax_payer: str
    requires_attorney: bool
    is_dry_funding: bool
    is_community_property_state: bool
    remote_ok: bool
    notes: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_code": self.state_code,
            "closing_method": self.closing_method,
            "funding_type": self.funding_type,
            "transfer_tax_payer": self.transfer_tax_payer,
            "requires_attorney": self.requires_attorney,
            "is_dry_funding": self.is_dry_funding,
            "is_community_property_state": self.is_community_property_state,
            "remote_ok": self.remote_ok,
            "notes": self.notes,
            "warnings": self.warnings,
        }


class ClosingEngine:
    """Determines the closing process for a given transaction context."""

    def get_closing_profile(self, context: Dict[str, Any]) -> ClosingProfile:
        """
        Return a ClosingProfile for the transaction.

        Required context fields:
            state (str): Two-letter state code.

        Optional context fields:
            county (str): County name — refines hybrid states like IL.
            price (float): Sale price — triggers tiered tax warnings.
            is_cash (bool): Cash purchase.
        """
        state = context.get("state", "").upper()
        if not state or state not in _CLOSING_PROFILES:
            raise ValueError(
                f"Unknown or missing state code: '{state}'. "
                "Must be a 2-letter US state abbreviation."
            )

        profile = _CLOSING_PROFILES[state]
        warnings: List[str] = []

        if state == "IL":
            county = (context.get("county") or "").strip().lower()
            if county.endswith(" county"):
                county = county[: -len(" county")].rstrip()
            cook_collars = {"cook", "dupage", "kane", "lake", "mchenry", "will"}
            if county in cook_collars:
                warnings.append(
                    "Cook County and collar counties: attorney closing strongly recommended."
                )
            else:
                warnings.append(
                    "Downstate IL: title company closing common; attorney not required."
                )

        price = context.get("price", 0)
        if state == "NY" and price >= 1_000_000:
            warnings.append(
                f"NY Mansion Tax applies: price ${price:,.0f} >= $1,000,000. "
                "Tiered additional transfer tax on buyer."
            )

        if state == "CA":
            warnings.append(
                "Verify county documentary transfer tax rate — varies by county. "
                "Some cities (SF, Santa Monica) impose additional city transfer taxes."
            )

        if state == "WA":
            if price < 500_000:
                warnings.append("WA REET rate: 1.1% on first $500k.")
            elif price < 1_500_000:
                warnings.append("WA REET rate: 1.28% on $500k–$1.5M portion.")
            else:
                warnings.append("WA REET rate: 2.75% on $1.5M–$3M, 3.0% above $3M.")

        return ClosingProfile(
            state_code=state,
            closing_method=profile["method"],
            funding_type=profile["funding"],
            transfer_tax_payer=profile["transfer_tax_payer"],
            requires_attorney=(state in ATTORNEY_CLOSE_STATES),
            is_dry_funding=(state in DRY_FUNDING_STATES),
            is_community_property_state=(state in COMMUNITY_PROPERTY_STATES),
            remote_ok=(state in _RON_PERMITTED_STATES),
            notes=profile["notes"],
            warnings=warnings,
        )

    @staticmethod
    def attorney_close_states() -> List[str]:
        return sorted(ATTORNEY_CLOSE_STATES)

    @staticmethod
    def escrow_states() -> List[str]:
        return sorted(k for k, v in _CLOSING_PROFILES.items() if v["method"] == "escrow")

    @staticmethod
    def community_property_states() -> List[str]:
        return sorted(COMMUNITY_PROPERTY_STATES)
