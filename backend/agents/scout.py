from typing import Iterator, Dict, Any

from harvesters.property_adapter import PropertyHarvester, PropertyRecord


class ScoutAgent:
    def __init__(self, harvester: PropertyHarvester):
        self.harvester = harvester

    def calculate_motivation(self, record: PropertyRecord) -> int:
        """Calculate a 1-100 motivation score."""
        score = 0
        if record.equity_percent > 60.0:
            score += 40
        if record.is_absentee_owner:
            score += 20
        if "tax_lien" in record.distress_flags or "pre_foreclosure" in record.distress_flags:
            score += 30
        if record.owner_type == "corporate":
            score -= 20

        return max(0, min(100, score))

    def scan_territory(self, zip_codes: list[str]) -> Iterator[Dict[str, Any]]:
        """Scans zip codes and yields events for the Workflow Engine."""
        for zip_code in zip_codes:
            records = self.harvester.fetch_by_zip(zip_code)

            for record in records:
                motivation = self.calculate_motivation(record)

                if motivation >= 80:
                    yield {
                        "event_type": "MOTIVATED_SELLER_FOUND",
                        "state": record.state,
                        "payload": record.__dict__,
                        "motivation_score": motivation,
                    }
