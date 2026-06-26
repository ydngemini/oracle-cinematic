import asyncio
import uuid
import time
from typing import AsyncGenerator


class PropertyGraph:
    def __init__(self):
        self.nodes = {}
        self.edges = []

    def _add_node(self, node_type: str, properties: dict) -> str:
        node_id = str(uuid.uuid4())
        self.nodes[node_id] = {
            "id": node_id,
            "type": node_type,
            "properties": properties,
            "created_at": time.time(),
        }
        return node_id

    def _add_edge(self, edge_type: str, from_id: str, to_id: str, properties: dict = None):
        self.edges.append({
            "type": edge_type,
            "from": from_id,
            "to": to_id,
            "properties": properties or {},
            "created_at": time.time(),
        })

    def _find_nodes(self, node_type: str) -> list:
        return [n for n in self.nodes.values() if n["type"] == node_type]

    def _find_edges_from(self, node_id: str, edge_type: str = None) -> list:
        return [
            e for e in self.edges
            if e["from"] == node_id and (edge_type is None or e["type"] == edge_type)
        ]

    def _find_edges_to(self, node_id: str, edge_type: str = None) -> list:
        return [
            e for e in self.edges
            if e["to"] == node_id and (edge_type is None or e["type"] == edge_type)
        ]

    async def ingest_public_record(self, record_data: dict):
        record_type = record_data.get("record_type", "TAX_ASSESSMENT")

        homeowner_id = self._add_node("Homeowner", {
            "name": record_data.get("owner_name", "Unknown"),
            "equity_pct": record_data.get("equity_pct", 0),
            "years_owned": record_data.get("years_owned", 0),
        })

        property_id = self._add_node("Property", {
            "address": record_data.get("address", ""),
            "assessed_value": record_data.get("assessed_value", 0),
            "market_value": record_data.get("market_value", 0),
            "sqft": record_data.get("sqft", 0),
            "bedrooms": record_data.get("bedrooms", 0),
            "bathrooms": record_data.get("bathrooms", 0),
            "county": record_data.get("county", "New Castle"),
            "state": record_data.get("state", "DE"),
            # Real harvester-computed motivated-seller signal (0 for synthetic
            # records, which surface via the equity/life-event path instead).
            "motivation_score": record_data.get("motivation_score", 0),
        })

        self._add_edge("OWNS", homeowner_id, property_id, {
            "since_year": record_data.get("purchase_year", 2015),
            "mortgage_balance": record_data.get("mortgage_balance", 0),
        })

        life_event_type = record_data.get("life_event")
        if life_event_type:
            event_id = self._add_node("LifeEvent", {
                "event_type": life_event_type,
                "filed_date": record_data.get("event_date", ""),
                "county_ref": record_data.get("case_number", ""),
            })
            self._add_edge("EXPERIENCED", homeowner_id, event_id, {
                "source": record_type,
            })

        await asyncio.sleep(0)
        return property_id

    async def calculate_novelty_score(self) -> AsyncGenerator[dict, None]:
        high_signal_events = {"DIVORCE_FILING", "PROBATE", "NOTICE_OF_DEFAULT", "TAX_LIEN", "PRE_FORECLOSURE"}
        # Real harvested parcels surface on their genuine motivation_score rather
        # than the synthetic equity/life-event gate (real leads are often not
        # equity-enriched). 60 ≈ multiple stacked distress signals.
        MOTIVATION_THRESHOLD = 60

        for prop in self._find_nodes("Property"):
            prop_id = prop["id"]

            ownership_edges = self._find_edges_to(prop_id, "OWNS")
            if not ownership_edges:
                continue

            for own_edge in ownership_edges:
                owner_id = own_edge["from"]
                owner = self.nodes.get(owner_id)
                if not owner:
                    continue

                equity_pct = owner["properties"].get("equity_pct", 0)

                # ── Real-signal path ───────────────────────────────────────────
                # Harvested parcels carry a real motivation_score. Surface the
                # high-motivation ones directly — no fabricated equity, no
                # synthetic life event required. Synthetic records score 0 here
                # and fall through to the equity/life-event path below.
                motivation = prop["properties"].get("motivation_score", 0) or 0
                if motivation >= MOTIVATION_THRESHOLD:
                    # Label with the homeowner's real distress signal when present.
                    signal = "MOTIVATED_SELLER"
                    for ev_edge in self._find_edges_from(owner_id, "EXPERIENCED"):
                        ev = self.nodes.get(ev_edge["to"])
                        if ev:
                            signal = ev["properties"].get("event_type", signal)
                            break
                    yield {
                        "property_id": prop_id,
                        "address": prop["properties"].get("address", ""),
                        "sqft": prop["properties"].get("sqft", 0),
                        "market_value": prop["properties"].get("market_value", 0),
                        "bedrooms": prop["properties"].get("bedrooms", 0),
                        "bathrooms": prop["properties"].get("bathrooms", 0),
                        "owner_name": owner["properties"].get("name", ""),
                        "equity_pct": equity_pct,
                        "life_event": signal,
                        "novelty_score": round(min(float(motivation), 99.0), 1),
                        "classification": "HIGH_PROBABILITY_SELLER",
                        "_source": "real",
                    }
                    break

                if equity_pct <= 40:
                    continue

                event_edges = self._find_edges_from(owner_id, "EXPERIENCED")
                if not event_edges:
                    continue

                for ev_edge in event_edges:
                    event_node = self.nodes.get(ev_edge["to"])
                    if not event_node:
                        continue

                    event_type = event_node["properties"].get("event_type", "")
                    if event_type not in high_signal_events:
                        continue

                    equity_weight = min((equity_pct - 40) / 60, 1.0) * 8
                    event_weight = 6 if event_type in ("PROBATE", "NOTICE_OF_DEFAULT") else 4
                    base = 85
                    score = min(base + equity_weight + event_weight, 99)

                    result = {
                        "property_id": prop_id,
                        "address": prop["properties"].get("address", ""),
                        "sqft": prop["properties"].get("sqft", 0),
                        "market_value": prop["properties"].get("market_value", 0),
                        "bedrooms": prop["properties"].get("bedrooms", 0),
                        "bathrooms": prop["properties"].get("bathrooms", 0),
                        "owner_name": owner["properties"].get("name", ""),
                        "equity_pct": equity_pct,
                        "life_event": event_type,
                        "novelty_score": round(score, 1),
                    }

                    if score > 90:
                        result["classification"] = "HIGH_PROBABILITY_SELLER"
                        yield result
                    break
