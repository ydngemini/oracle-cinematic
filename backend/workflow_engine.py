"""
Workflow Engine — orchestrates the Oracle agent pipeline.

Pipeline stages:
  1. HARVEST  — CountyAssessorHarvester scrapes public records → Graph
  2. SCORE    — PropertyGraph.calculate_novelty_score() identifies high-probability sellers
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
import time
from typing import Optional

from graph_engine import PropertyGraph
from harvesters.county_assessor import CountyAssessorHarvester
from agents.agent_analyst import AnalystAgent, CMAResult
from spatial_agent import reconstruct_property, should_trigger_reconstruction, property_id
from legal_agent import format_for_websocket, generate_legal_package
from agents.scout import ScoutAgent
from harvesters.four_state_firehose import MockBatchLeadsAdapter

logger = logging.getLogger("oracle.workflow_engine")

PREDICTIVE_CACHE_INTERVAL = 8
PREDICTIVE_CACHE_TOP_N = 3

SCOUT_UNDERWRITER_STATES = {"DE"}
SCOUT_SCAN_INTERVAL = 30
# Delaware Valley firehose territory (DE/PA/NJ/MD). The mock adapter ignores
# the zips; real BatchLeads adapter will key off them.
SCOUT_TERRITORY = ["19801", "19102", "08102", "21201"]


class WorkflowEngine:
    def __init__(self, websocket=None, mind_service=None):
        self.websocket = websocket
        self.mind_service = mind_service
        self.graph = PropertyGraph()
        self.harvester = CountyAssessorHarvester(self.graph, websocket)
        self.analyst = AnalystAgent(self.graph, websocket)
        self.scout = ScoutAgent(MockBatchLeadsAdapter())
        self._running = False
        self._harvest_task: Optional[asyncio.Task] = None
        self._analysis_task: Optional[asyncio.Task] = None
        self._predictive_task: Optional[asyncio.Task] = None
        self._scout_task: Optional[asyncio.Task] = None
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

        self._harvest_task = asyncio.create_task(self._harvest_loop())
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        self._predictive_task = asyncio.create_task(self._predictive_cache_loop())
        self._scout_task = asyncio.create_task(self._scout_loop())

        await self._emit_status("WORKFLOW ENGINE — all agents online")

        await asyncio.gather(
            self._harvest_task,
            self._analysis_task,
            self._predictive_task,
            self._scout_task,
        )

    async def stop(self):
        self._running = False
        self.harvester.stop()

        if self._harvest_task:
            self._harvest_task.cancel()
        if self._analysis_task:
            self._analysis_task.cancel()
        if self._predictive_task:
            self._predictive_task.cancel()
        if self._scout_task:
            self._scout_task.cancel()

        await self._emit_status("WORKFLOW ENGINE — pipeline shutdown complete")

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
                f"HIGH-PROBABILITY SELLER DETECTED — novelty {novelty}"
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
                        "estimatedValue": cma_result.estimated_value,
                        "confidenceLow": cma_result.confidence_low,
                        "confidenceHigh": cma_result.confidence_high,
                        "pricePerSqft": cma_result.price_per_sqft,
                        "methodology": cma_result.methodology,
                        "bedrooms": hit["bedrooms"],
                        "bathrooms": hit["bathrooms"],
                        "novelty": novelty,
                    },
                })) if self.websocket else None

            await asyncio.sleep(0.5)

            if should_trigger_reconstruction(novelty):
                asyncio.create_task(
                    reconstruct_property(address, self.websocket)
                )

            if self.websocket:
                await self.websocket.send_text(json.dumps({"type": "STAGE_PROPERTY"}))

            await asyncio.sleep(1.0)

            if self.websocket:
                dialogue = [
                    {"agent": "AI CLOSER", "text": f"Initiating contact with {hit['owner_name']}..."},
                    {"agent": "AI CLOSER", "text": f"Signal: {hit['life_event'].replace('_', ' ').title()} + {hit['equity_pct']}% equity"},
                    {"agent": "AI CLOSER", "text": "Call connected. Voice synthesis active."},
                ]
                for line in dialogue:
                    await asyncio.sleep(0.8)
                    await self.websocket.send_text(json.dumps({
                        "type": "TRANSCRIPT_LINE",
                        "agent": line["agent"],
                        "text": line["text"],
                    }))

            await asyncio.sleep(1.2)

            if self.websocket:
                await self._emit_status("AGENT LEGAL — generating contract package")
                await asyncio.sleep(0.6)
                property_data = {
                    "address": address,
                    "price": cma_result.estimated_value if cma_result else hit["market_value"],
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
                        "id": property_id(hit["address"]),
                        "address": hit["address"],
                        "novelty": hit["novelty_score"],
                    })
                    if len(top_props) >= PREDICTIVE_CACHE_TOP_N:
                        break

                if top_props and self.websocket:
                    new_ids = [p["id"] for p in top_props]
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
        await asyncio.sleep(7)

        while self._running:
            try:
                await self._emit_status(
                    f"AGENT SCOUT — sweeping territory {SCOUT_TERRITORY}"
                )
                # scan_territory is a sync generator over mock/HTTP fetches;
                # yield control between events so we don't starve the loop.
                for event in self.scout.scan_territory(SCOUT_TERRITORY):
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
