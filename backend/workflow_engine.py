"""
Workflow Engine — orchestrates the Oracle agent pipeline.

Pipeline stages:
  1. HARVEST  — CountyAssessorHarvester scrapes public records → Graph
  2. SCORE    — PropertyGraph.calculate_novelty_score() ranks public-record review candidates
  3. ANALYZE  — AnalystAgent runs CMA on each high-score property
  4. SPATIAL  — SpatialAgent reconstructs 3D splat for qualifying properties
  5. CONTACT  — AI Closer initiates outreach (downstream, not managed here)
  6. LEGAL    — LegalAgent generates contract package (downstream)

The engine manages lifecycle, scheduling, and inter-agent data flow.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Optional

from graph_engine import PropertyGraph
from harvesters.county_assessor import CountyAssessorHarvester
from agents.agent_analyst import AnalystAgent, CMAResult
from spatial_agent import reconstruct_property, should_trigger_reconstruction, property_id
from legal_agent import format_for_websocket, generate_legal_package
from agents.scout import ScoutAgent
from harvesters.four_state_firehose import RegionalParcelAdapter
from real_leads import fetch_real_records
from outreach_compliance import AI_VOICE_DISCLOSURE

logger = logging.getLogger("oracle.workflow_engine")

# How many real `leads` rows to seed the graph with per session. Keeps the
# flagship pipeline operating on REAL parcels even when the live county portals
# are unreachable (DNS/404 in dev).
REAL_LEAD_SEED_LIMIT = 50

PREDICTIVE_CACHE_INTERVAL = 8
PREDICTIVE_CACHE_TOP_N = 3

SCOUT_UNDERWRITER_STATES = {"DE"}
SCOUT_SCAN_INTERVAL = 30
# Delaware Valley firehose territory (DE/PA/NJ/MD).
SCOUT_TERRITORY = ["19801", "19102", "08102", "21201"]

# Live regional sweeps are opt-in because each WebSocket session owns a workflow
# engine and municipal portals must not be multiplied by passive viewers.  The
# only available adapter is real/public; there is no synthetic fallback.
SCOUT_REGIONAL_ENABLED = os.environ.get("SCOUT_REGIONAL_ENABLED", "").lower() in {
    "1", "true", "yes", "on"
}

# The CountyAssessorHarvester targets hard-coded Delaware portals
# (recorder.delaware.gov — currently NXDOMAIN — and newcastlede.gov/parcelview).
# With no reachable endpoint it returns zero records yet retries on every 300s
# cycle, flooding logs with connection failures on every WebSocket session. The
# real pipeline is seeded from the DB by _seed_real_leads(), so the live scraper
# is opt-in: off by default until a working portal/credentials are wired.
COUNTY_HARVEST_ENABLED = os.environ.get("COUNTY_HARVEST_ENABLED", "").lower() in {"1", "true", "yes", "on"}


class WorkflowEngine:
    def __init__(self, websocket=None, mind_service=None, tenant_id="", user_id=""):
        self.websocket = websocket
        self.mind_service = mind_service
        # Operator identity — used to seed the graph from this tenant's real
        # leads (RLS-scoped) so the visible pipeline runs on real parcels.
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.graph = PropertyGraph()
        self.harvester = CountyAssessorHarvester(self.graph, websocket)
        self.analyst = AnalystAgent(self.graph, websocket)
        if SCOUT_REGIONAL_ENABLED:
            self.scout = ScoutAgent(RegionalParcelAdapter(tenant_id=tenant_id))
        else:
            self.scout = None
            logger.info(
                "Per-session regional Scout disabled; durable municipal harvest jobs remain active. "
                "Set SCOUT_REGIONAL_ENABLED=1 for a live regional sweep."
            )
        self._running = False
        self._harvest_task: Optional[asyncio.Task] = None
        self._analysis_task: Optional[asyncio.Task] = None
        self._predictive_task: Optional[asyncio.Task] = None
        self._scout_task: Optional[asyncio.Task] = None
        # Fire-and-forget reconstruct tasks — tracked so stop() can cancel them.
        self._recon_tasks: set[asyncio.Task] = set()
        self._last_pushed_ids: list[str] = []
        self._stats = {
            "cycles": 0,
            "properties_analyzed": 0,
            "high_value_targets": 0,
            "start_time": None,
        }

    async def start(self):
        self._running = True
        self._stats["start_time"] = time.time()

        await self._emit_status("WORKFLOW ENGINE — initializing pipeline")

        # Seed the graph from REAL leads before the analysis loop runs, so the
        # novelty → CMA → contact pipeline operates on real parcels even when the
        # live county portals are unreachable. Best-effort: degrades to whatever
        # the harvester pulls if the DB seed yields nothing.
        await self._seed_real_leads()

        if COUNTY_HARVEST_ENABLED:
            self._harvest_task = asyncio.create_task(self._harvest_loop())
        else:
            logger.info(
                "County-assessor harvester disabled — no reachable Delaware portal "
                "and COUNTY_HARVEST_ENABLED is off. Graph runs on _seed_real_leads()."
            )
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        self._predictive_task = asyncio.create_task(self._predictive_cache_loop())
        self._scout_task = asyncio.create_task(self._scout_loop())

        await self._emit_status("WORKFLOW ENGINE — all agents online")

        await asyncio.gather(*[
            t for t in (
                self._harvest_task,
                self._analysis_task,
                self._predictive_task,
                self._scout_task,
            )
            if t is not None
        ])

    async def stop(self):
        self._running = False
        self.harvester.stop()

        tasks_to_cancel = [
            t for t in (
                self._harvest_task,
                self._analysis_task,
                self._predictive_task,
                self._scout_task,
            )
            if t is not None
        ] + list(self._recon_tasks)

        for t in tasks_to_cancel:
            t.cancel()

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self._recon_tasks.clear()
        await self._emit_status("WORKFLOW ENGINE — pipeline shutdown complete")

    async def _seed_real_leads(self):
        """Ingest this tenant's real `leads` rows into the graph so the live
        pipeline scores real parcels. Never fabricates data; on any failure the
        graph is simply left to whatever the harvester provides."""
        if not self.tenant_id:
            logger.info("No tenant context — skipping real-lead graph seed.")
            return
        try:
            records = await fetch_real_records(self.tenant_id, self.user_id, REAL_LEAD_SEED_LIMIT)
        except Exception as e:  # noqa: BLE001 — seeding is best-effort
            logger.warning("Real-lead seed failed: %s", e)
            return
        if not records:
            await self._emit_status("WORKFLOW ENGINE — no real leads to seed (graph empty)")
            return
        for record in records:
            try:
                await self.graph.ingest_public_record(record)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to ingest seed record: %s", e)
        await self._emit_status(
            f"WORKFLOW ENGINE — seeded {len(records)} real parcels into graph"
        )

    async def _harvest_loop(self):
        try:
            await self.harvester.start()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Harvester crashed: {e}")

    async def _analysis_loop(self):
        await asyncio.sleep(5)

        while self._running:
            self._stats["cycles"] += 1
            try:
                await self._run_analysis_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Analysis cycle error: {e}")

            await asyncio.sleep(10)

    async def _run_analysis_cycle(self):
        await self._emit_status("AGENT ANALYST — scoring novelty across graph")

        async for hit in self.graph.calculate_novelty_score():
            address = hit["address"]
            novelty = hit["novelty_score"]

            await self._emit_status(
                f"PUBLIC-RECORD REVIEW CANDIDATE — evidence strength {novelty}"
            )

            await asyncio.sleep(0.3)

            cma_result = await self.analyst.analyze_property(hit)
            self._stats["properties_analyzed"] += 1

            if cma_result:
                hit["cma"] = cma_result.to_dict()
                hit["estimated_value"] = cma_result.estimated_value

                await self.websocket.send_text(json.dumps({
                    "type": "DATA_PULLED",
                    "data": {
                        "address": address,
                        "squareFootage": hit["sqft"],
                        "price": hit["market_value"],
                        # estimatedValue is the conservative AS-IS value for a
                        # distressed/off-market target; arvEstimate is retail.
                        "estimatedValue": cma_result.estimated_value,
                        "arvEstimate": cma_result.arv_estimate,
                        "asIsLow": cma_result.as_is_low,
                        "asIsHigh": cma_result.as_is_high,
                        "investorOffer": cma_result.investor_offer,
                        "distressSeverity": cma_result.distress_severity,
                        "confidenceLow": cma_result.confidence_low,
                        "confidenceHigh": cma_result.confidence_high,
                        "pricePerSqft": cma_result.price_per_sqft,
                        "methodology": cma_result.methodology,
                        # Fact-check provenance — the UI must never present an
                        # UNVERIFIED/CONFLICT valuation as a confident number.
                        "avmVerdict": cma_result.verdict,
                        "avmConfidence": cma_result.confidence,
                        "avmSources": cma_result.sources,
                        "bedrooms": hit["bedrooms"],
                        "bathrooms": hit["bathrooms"],
                        "novelty": novelty,
                    },
                })) if self.websocket else None

            await asyncio.sleep(0.5)

            if should_trigger_reconstruction(novelty):
                _t = asyncio.create_task(
                    reconstruct_property(address, self.websocket)
                )
                self._recon_tasks.add(_t)
                _t.add_done_callback(self._recon_tasks.discard)

            if self.websocket:
                await self.websocket.send_text(json.dumps({"type": "STAGE_PROPERTY"}))

            await asyncio.sleep(1.0)

            if self.websocket:
                # No real telephony is placed here — this is a scripted demo
                # transcript. It leads with AI_VOICE_DISCLOSURE (TCPA / FCC 24-17:
                # an artificial voice must disclose itself) and every line carries
                # "simulated": true so the UI and audit trail never present it as a
                # live call. life_event is NULL for real parcels with no distress
                # signal — guard it rather than .replace() on None.
                signal = (hit.get("life_event") or "GENERAL").replace("_", " ").title()
                dialogue = [
                    {"agent": "AI CLOSER", "text": f"Initiating contact with {hit['owner_name']}..."},
                    {"agent": "AI CLOSER", "text": f"Signal: {signal} + {hit['equity_pct']}% equity"},
                    {"agent": "AI CLOSER", "text": "Call connected. Voice synthesis active."},
                    {"agent": "AI CLOSER", "text": AI_VOICE_DISCLOSURE},
                ]
                for line in dialogue:
                    await asyncio.sleep(0.8)
                    await self.websocket.send_text(json.dumps({
                        "type": "TRANSCRIPT_LINE",
                        "agent": line["agent"],
                        "text": line["text"],
                        "simulated": True,
                    }))

            await asyncio.sleep(1.2)

            if self.websocket:
                await self._emit_status("AGENT LEGAL — generating contract package")
                await asyncio.sleep(0.6)
                # The contract price is the conservative MAX ACQUISITION OFFER
                # (70% rule on a distressed target) — NEVER the retail ARV, or we
                # would contract to overpay on exactly the properties we target.
                offer = 0
                if cma_result:
                    offer = cma_result.investor_offer or cma_result.estimated_value
                property_data = {
                    "address": address,
                    "price": offer or hit["market_value"],
                    "arv": cma_result.arv_estimate if cma_result else 0,
                    "as_is": cma_result.estimated_value if cma_result else 0,
                    "assumptions": cma_result.valuation_assumptions if cma_result else [],
                    "distress_severity": cma_result.distress_severity if cma_result else "UNKNOWN",
                    "owner_name": hit["owner_name"],
                }
                legal_payload = format_for_websocket(property_data, strategy="wholesale")
                await self.websocket.send_text(legal_payload)

            self._stats["high_value_targets"] += 1
            break

    @property
    def stats(self) -> dict:
        uptime = time.time() - self._stats["start_time"] if self._stats["start_time"] else 0
        return {
            **self._stats,
            "uptime_seconds": round(uptime, 1),
            "harvester": self.harvester.stats,
        }

    async def _predictive_cache_loop(self):
        await asyncio.sleep(PREDICTIVE_CACHE_INTERVAL)

        while self._running:
            try:
                top_props = []
                async for hit in self.graph.calculate_novelty_score():
                    top_props.append({
                        "propertyId": property_id(hit["address"]),
                        "address": hit["address"],
                        "novelty": hit["novelty_score"],
                    })
                    if len(top_props) >= PREDICTIVE_CACHE_TOP_N:
                        break

                if top_props and self.websocket:
                    new_ids = [p["propertyId"] for p in top_props]
                    if new_ids != self._last_pushed_ids:
                        self._last_pushed_ids = new_ids
                        await self.websocket.send_text(json.dumps({
                            "type": "PREDICTIVE_CACHE",
                            "properties": top_props,
                        }))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Predictive cache error: {e}")

            await asyncio.sleep(PREDICTIVE_CACHE_INTERVAL)

    async def _scout_loop(self):
        """Drives the Scout agent over the firehose territory and routes each
        MOTIVATED_SELLER_FOUND event through the state gatekeeper."""
        if self.scout is None:
            await self._emit_status(
                "AGENT SCOUT — per-session sweep disabled (durable harvest queue remains active)"
            )
            return
        await asyncio.sleep(7)

        while self._running:
            try:
                await self._emit_status(
                    f"AGENT SCOUT — sweeping territory {SCOUT_TERRITORY}"
                )
                # The compatibility scanner is synchronous; isolate municipal
                # HTTP/Playwright work in a thread so the authenticated socket's
                # heartbeat and live telemetry stay responsive.
                events = await asyncio.to_thread(
                    lambda: list(self.scout.scan_territory(SCOUT_TERRITORY))
                )
                for event in events:
                    await self.handle_scout_event(event)
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scout loop error: {e}")

            await asyncio.sleep(SCOUT_SCAN_INTERVAL)

    async def handle_scout_event(self, event: dict) -> None:
        """Gatekeeper for Scout-emitted events. Routes by state per decision 004
        (Multi-State Underwriting OOD): DE → Bedrock underwriter, else halt."""
        if event.get("event_type") != "MOTIVATED_SELLER_FOUND":
            return

        state = (event.get("state") or "").upper()
        payload = event.get("payload", {})
        parcel_id = payload.get("parcel_id", "<unknown>")
        score = event.get("motivation_score")

        if state in SCOUT_UNDERWRITER_STATES:
            await self._emit_status(
                f"SCOUT → UNDERWRITER ({state}) — parcel {parcel_id} score {score}"
            )
            await self._invoke_de_underwriter(payload, score)
        else:
            logger.warning(
                f"OOD deal halted: state={state} parcel={parcel_id} score={score} "
                f"— non-DE underwriting deferred per decision 004"
            )
            await self._emit_status(
                f"SCOUT → DEFERRED ({state}) — OOD pipeline not yet wired"
            )

    async def _invoke_de_underwriter(self, payload: dict, score) -> None:
        """Bedrock oracle-underwriter-70b entrypoint. Stub until the fine-tune
        job (wbtis0vmhc54) completes — then wires to ml_forge.bedrock_client."""
        logger.info(
            f"DE underwriter invoked: parcel={payload.get('parcel_id')} score={score}"
        )

    async def _emit_status(self, message: str):
        logger.info(message)
        if self.websocket:
            try:
                await self.websocket.send_text(json.dumps({
                    "type": "STATUS_UPDATE",
                    "agent": message,
                }))
            except Exception:
                pass

        if self.mind_service:
            agent_id = "SCOUT"
            upper = message.upper()
            if "ANALYST" in upper:
                agent_id = "ANALYST"
            elif "CLOSER" in upper:
                agent_id = "CLOSER"
            elif "LEGAL" in upper:
                agent_id = "LEGAL"
            self.mind_service.observe(agent_id, message, importance=0.6)
