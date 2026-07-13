"""Source-backed public-record property/entity graph.

The graph models only identities and relationships stated in supplied public
records.  An officer is an officer, not an inferred beneficial owner; missing
people and private contact details are never synthesized.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, AsyncGenerator, Mapping, Optional


_PRIVATE_KEYS = {
    "email", "phone", "mobile", "private_contact", "ssn", "date_of_birth",
    "bank_account", "credit_score", "beneficial_owner",
}
_HIGH_SIGNAL_EVENTS = {
    "PROBATE", "NOTICE_OF_DEFAULT", "TAX_LIEN", "PRE_FORECLOSURE",
    "VACANCY", "CODE_VIOLATIONS", "DELINQUENT_TAX",
}


def _clean_attributes(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in values.items()
        if value is not None and value != "" and str(key).lower() not in _PRIVATE_KEYS
    }


def _canonical(value: Any) -> str:
    return " ".join(str(value or "").upper().split())


class PropertyGraph:
    def __init__(self):
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self._edge_keys: set[tuple[str, str, str, str]] = set()

    @staticmethod
    def _node_id(node_type: str, canonical_key: str) -> str:
        material = f"{node_type}:{canonical_key}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:32]

    def _add_node(
        self,
        node_type: str,
        properties: Mapping[str, Any],
        *,
        canonical_key: Optional[str] = None,
        source: Optional[Mapping[str, Any]] = None,
    ) -> str:
        cleaned = _clean_attributes(properties)
        key = _canonical(canonical_key or cleaned.get("record_id") or cleaned.get("name") or cleaned.get("address"))
        if not key:
            raise ValueError(f"{node_type} requires a public-record canonical key")
        node_id = self._node_id(node_type, key)
        existing = self.nodes.get(node_id)
        if existing:
            existing["properties"].update(cleaned)
            if source:
                existing["sources"].append(dict(source))
            return node_id
        self.nodes[node_id] = {
            "id": node_id,
            "type": node_type,
            "canonical_key": key,
            "properties": cleaned,
            "sources": [dict(source)] if source else [],
            "created_at": time.time(),
        }
        return node_id

    def _add_edge(
        self,
        edge_type: str,
        from_id: str,
        to_id: str,
        properties: Optional[Mapping[str, Any]] = None,
        *,
        source_record_id: str = "",
        confidence: float = 1.0,
        match_status: str = "exact",
    ) -> None:
        if from_id == to_id:
            return
        edge_key = (edge_type, from_id, to_id, source_record_id)
        if edge_key in self._edge_keys:
            return
        self._edge_keys.add(edge_key)
        self.edges.append(
            {
                "type": edge_type,
                "from": from_id,
                "to": to_id,
                "properties": _clean_attributes(properties or {}),
                "source_record_id": source_record_id,
                "confidence": max(0.0, min(float(confidence), 1.0)),
                "match_status": match_status,
                "created_at": time.time(),
            }
        )

    def _find_nodes(self, node_type: str) -> list[dict[str, Any]]:
        return [node for node in self.nodes.values() if node["type"] == node_type]

    def _find_edges_from(self, node_id: str, edge_type: Optional[str] = None) -> list[dict[str, Any]]:
        return [
            edge for edge in self.edges
            if edge["from"] == node_id and (edge_type is None or edge["type"] == edge_type)
        ]

    def _find_edges_to(self, node_id: str, edge_type: Optional[str] = None) -> list[dict[str, Any]]:
        return [
            edge for edge in self.edges
            if edge["to"] == node_id and (edge_type is None or edge["type"] == edge_type)
        ]

    async def ingest_public_record(self, record_data: dict) -> str:
        """Join a recorder/assessor/court record without inventing identity.

        Supported optional shapes include ``acquisition_entity``, ``officers``,
        ``deeds``, and ``purchase_history``. Every such relationship remains
        explicitly labelled by its record type and source record identifier.
        """
        record_type = str(record_data.get("record_type") or "PUBLIC_RECORD").upper()
        record_id = str(
            record_data.get("record_id")
            or record_data.get("case_number")
            or record_data.get("parcel_id")
            or ""
        )
        address = str(record_data.get("address") or "").strip()
        parcel_id = str(record_data.get("parcel_id") or "").strip()
        property_key = parcel_id or address
        if not property_key:
            raise ValueError("public record requires parcel_id or address")
        source = {
            "source": record_data.get("source") or record_data.get("_source") or record_type,
            "record_id": record_id,
            "observed_at": record_data.get("observed_at") or record_data.get("event_date"),
        }

        property_id = self._add_node(
            "Property",
            {
                "parcel_id": parcel_id,
                "address": address,
                "assessed_value": record_data.get("assessed_value"),
                "market_value": record_data.get("market_value"),
                "sqft": record_data.get("sqft"),
                "bedrooms": record_data.get("bedrooms"),
                "bathrooms": record_data.get("bathrooms"),
                "county": record_data.get("county"),
                "state": record_data.get("state"),
                "motivation_score": record_data.get("motivation_score"),
                "motivation_validated": bool(record_data.get("motivation_validated", False)),
                "record_type": record_type,
            },
            canonical_key=property_key,
            source=source,
        )

        if address:
            address_id = self._add_node(
                "Address",
                {"address": address, "county": record_data.get("county"), "state": record_data.get("state")},
                canonical_key=address,
                source=source,
            )
            self._add_edge("LOCATED_AT", property_id, address_id, source_record_id=record_id)

        owner_name = str(record_data.get("owner_name") or "").strip()
        entity = record_data.get("acquisition_entity")
        entity_name = ""
        if isinstance(entity, Mapping):
            entity_name = str(entity.get("name") or entity.get("entity_name") or "").strip()
        elif isinstance(entity, str):
            entity_name = entity.strip()
        owner_id: Optional[str] = None
        if entity_name:
            owner_id = self._add_node(
                "AcquisitionEntity",
                {"name": entity_name, "entity_id": entity.get("entity_id") if isinstance(entity, Mapping) else None},
                canonical_key=entity_name,
                source=source,
            )
        elif owner_name:
            owner_id = self._add_node(
                "PersonOfRecord",
                {
                    "name": owner_name,
                    "equity_pct": record_data.get("equity_pct"),
                    "years_owned": record_data.get("years_owned"),
                },
                canonical_key=owner_name,
                source=source,
            )
        if owner_id:
            self._add_edge(
                "OWNER_OF_RECORD",
                owner_id,
                property_id,
                {
                    "since_year": record_data.get("purchase_year"),
                    "mortgage_balance": record_data.get("mortgage_balance"),
                },
                source_record_id=record_id,
            )

        # Officers are joined only when an explicit public filing says so.  No
        # OFFICER_OF edge is re-labelled as ownership or control.
        if owner_id and self.nodes[owner_id]["type"] == "AcquisitionEntity":
            for officer in record_data.get("officers") or []:
                if not isinstance(officer, Mapping) or not officer.get("name"):
                    continue
                officer_record = str(officer.get("record_id") or record_id)
                officer_id = self._add_node(
                    "Officer",
                    {"name": officer["name"], "title": officer.get("title")},
                    canonical_key=officer["name"],
                    source={**source, "record_id": officer_record},
                )
                self._add_edge(
                    "OFFICER_OF",
                    officer_id,
                    owner_id,
                    {"title": officer.get("title")},
                    source_record_id=officer_record,
                    match_status="exact",
                )

        life_event = str(record_data.get("life_event") or "").upper().strip()
        if life_event:
            event_id = self._add_node(
                "PublicFiling",
                {
                    "event_type": life_event,
                    "filed_date": record_data.get("event_date"),
                    "record_id": record_id,
                    "record_type": record_type,
                },
                canonical_key=f"{record_type}:{record_id or property_key}:{life_event}",
                source=source,
            )
            self._add_edge(
                "AFFECTS_PROPERTY", event_id, property_id, source_record_id=record_id
            )
            if owner_id:
                self._add_edge(
                    "NAMES_PARTY", event_id, owner_id, source_record_id=record_id
                )

        deed_rows = list(record_data.get("deeds") or [])
        if record_type in {"DEED", "SALE"}:
            deed_rows.append(record_data)
        for deed in deed_rows:
            if not isinstance(deed, Mapping):
                continue
            deed_record_id = str(deed.get("record_id") or record_id)
            if not deed_record_id:
                continue
            deed_id = self._add_node(
                "Deed",
                {
                    "record_id": deed_record_id,
                    "recorded_at": deed.get("recorded_at") or deed.get("event_date"),
                    "consideration": deed.get("sale_price") or deed.get("consideration"),
                    "grantor": deed.get("grantor"),
                    "grantee": deed.get("grantee"),
                },
                canonical_key=deed_record_id,
                source={**source, "record_id": deed_record_id},
            )
            self._add_edge("CONVEYS", deed_id, property_id, source_record_id=deed_record_id)

        for purchase in record_data.get("purchase_history") or []:
            if not isinstance(purchase, Mapping) or not purchase.get("record_id"):
                continue
            purchase_id = self._add_node(
                "Deed",
                {
                    "record_id": purchase["record_id"],
                    "recorded_at": purchase.get("recorded_at"),
                    "consideration": purchase.get("consideration"),
                },
                canonical_key=purchase["record_id"],
                source={**source, "record_id": purchase["record_id"]},
            )
            self._add_edge("PURCHASE_HISTORY", property_id, purchase_id, source_record_id=str(purchase["record_id"]))

        await asyncio.sleep(0)
        return property_id

    async def calculate_novelty_score(self) -> AsyncGenerator[dict, None]:
        """Yield evidence-labelled sourcing candidates, never calibrated odds."""
        for prop in self._find_nodes("Property"):
            prop_id = prop["id"]
            properties = prop["properties"]
            evidence: list[str] = []
            event_type = ""
            for edge in self._find_edges_to(prop_id, "AFFECTS_PROPERTY"):
                event = self.nodes.get(edge["from"])
                kind = str((event or {}).get("properties", {}).get("event_type") or "")
                if kind in _HIGH_SIGNAL_EVENTS:
                    evidence.append(kind)
                    event_type = event_type or kind

            try:
                motivation = float(properties.get("motivation_score") or 0)
            except (TypeError, ValueError):
                motivation = 0.0
            if motivation >= 60:
                evidence.append("PUBLIC_SIGNAL_STACK")

            ownership = self._find_edges_to(prop_id, "OWNER_OF_RECORD")
            owner = self.nodes.get(ownership[0]["from"]) if ownership else None
            equity = (owner or {}).get("properties", {}).get("equity_pct")
            try:
                equity_value = float(equity) if equity is not None else None
            except (TypeError, ValueError):
                equity_value = None
            if equity_value is not None and equity_value > 40 and evidence:
                evidence.append("PUBLIC_EQUITY_ESTIMATE_OVER_40")

            if not evidence:
                continue
            # A transparent evidence-strength index for ordering only. It is
            # deliberately not labelled or presented as a seller probability.
            strength = min(100.0, 45.0 + 12.0 * len(set(evidence)) + min(motivation, 100.0) * 0.25)
            yield {
                "property_id": prop_id,
                "address": properties.get("address", ""),
                "sqft": properties.get("sqft", 0) or 0,
                "market_value": properties.get("market_value", 0) or 0,
                "bedrooms": properties.get("bedrooms", 0) or 0,
                "bathrooms": properties.get("bathrooms", 0) or 0,
                "owner_name": (owner or {}).get("properties", {}).get("name", ""),
                "owner_record_type": (owner or {}).get("type"),
                "equity_pct": equity_value,
                "life_event": event_type or "PUBLIC_SIGNAL_STACK",
                "novelty_score": round(strength, 1),
                "classification": "PUBLIC_RECORD_REVIEW_CANDIDATE",
                "evidence": sorted(set(evidence)),
                "score_kind": "evidence_strength_not_probability",
                "_source": "public-record",
            }

    def export(self) -> dict[str, Any]:
        return {
            "nodes": list(self.nodes.values()),
            "edges": list(self.edges),
            "policy": {
                "beneficial_ownership_inferred": False,
                "private_contacts_included": False,
                "officer_relationships_are_public_record_roles_only": True,
            },
        }
