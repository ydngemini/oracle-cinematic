from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PropertyRecord:
    """Unified schema for all real estate API data, regardless of the source."""
    parcel_id: str
    address: str
    city: str
    state: str
    zip_code: str
    owner_name: str
    owner_type: str  # 'individual', 'corporate', 'trust'
    estimated_value: float
    equity_percent: float
    is_absentee_owner: bool
    distress_flags: List[str]  # e.g., ['tax_lien', 'pre_foreclosure']
    last_sale_date: Optional[str]


class PropertyHarvester(ABC):
    """Abstract base class for all property data ingestion."""

    @abstractmethod
    def fetch_by_zip(self, zip_code: str) -> List[PropertyRecord]:
        """Fetch properties by zip code and map to the unified PropertyRecord."""
        pass

    @abstractmethod
    def fetch_cash_buyers(self, state: str, min_purchases: int = 3) -> List[dict]:
        """Fetch active cash buyers in a given state."""
        pass
