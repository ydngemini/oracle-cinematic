from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional


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
    # Source-backed characteristics used by HBU/spatial intelligence.  They are
    # optional because most assessor feeds expose only a subset; missing values
    # stay missing rather than being silently imputed during ingestion.
    zoning_district: Optional[str] = None
    max_far: Optional[float] = None
    lot_area_sqft: Optional[float] = None
    building_area_sqft: Optional[float] = None
    land_use: Optional[str] = None
    air_rights_indicator: Optional[bool] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    dataset_version: Optional[str] = None
    source_metadata: dict[str, Any] = field(default_factory=dict)


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
