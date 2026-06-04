from .property_adapter import PropertyHarvester, PropertyRecord


class MockBatchLeadsAdapter(PropertyHarvester):
    """Temporary mock adapter to test the multi-state Scout pipeline."""

    def fetch_by_zip(self, zip_code: str) -> list[PropertyRecord]:
        return [
            PropertyRecord(
                parcel_id="PA-9921-A",
                address="404 Broad St",
                city="Philadelphia",
                state="PA",
                zip_code=zip_code,
                owner_name="John Doe",
                owner_type="individual",
                estimated_value=250000.0,
                equity_percent=85.0,
                is_absentee_owner=True,
                distress_flags=["tax_lien"],
                last_sale_date="2005-06-15",
            ),
            PropertyRecord(
                parcel_id="DE-3050-C",
                address="78 Delaware Ave",
                city="Wilmington",
                state="DE",
                zip_code=zip_code,
                owner_name="Mary Smith",
                owner_type="individual",
                estimated_value=210000.0,
                equity_percent=72.0,
                is_absentee_owner=True,
                distress_flags=["pre_foreclosure"],
                last_sale_date="2008-03-22",
            ),
            PropertyRecord(
                parcel_id="NJ-1102-B",
                address="12 Cherry Ln",
                city="Camden",
                state="NJ",
                zip_code=zip_code,
                owner_name="Cherry LLC",
                owner_type="corporate",
                estimated_value=180000.0,
                equity_percent=20.0,
                is_absentee_owner=False,
                distress_flags=[],
                last_sale_date="2022-01-10",
            ),
        ]

    def fetch_cash_buyers(self, state: str, min_purchases: int = 3) -> list[dict]:
        return [{"entity_name": "Apex Holdings LLC", "purchases_ytd": 5, "state": state}]
