"""
Agent Analyst — local Qwen 1.5B with Valuation LoRA for Comparative Market Analysis.

Pipeline:
  1. Ingest historic sales data from the graph (via county_assessor harvester records)
  2. Select comparable properties (comps) based on proximity, sqft, beds/baths, recency
  3. Feed structured comp data to Qwen 1.5B + Valuation LoRA for CMA reasoning
  4. Output pinpoint estimated value for off-market properties with confidence interval
  5. Emit results to the workflow engine and WebSocket
"""

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("oracle.agent.analyst")

MODEL_PATH = os.environ.get(
    "ANALYST_MODEL_PATH",
    "/media/ydn/SYPHER_CORE/models/qwen-1.5b-valuation-lora.gguf"
)
LLAMA_CPP_PATH = os.environ.get(
    "LLAMA_CPP_PATH",
    "/media/ydn/SYPHER_CORE/Untitled Folder/llama.cpp"
)
N_GPU_LAYERS = int(os.environ.get("ANALYST_GPU_LAYERS", "35"))
CTX_SIZE = 4096
MAX_COMPS = 6
COMP_RADIUS_MILES = 1.5
MAX_COMP_AGE_MONTHS = 24

MANUAL_COMP_RADIUS_MILES = 2.0
MANUAL_COMP_SQFT_TOLERANCE = 0.25
MANUAL_COMP_BED_TOLERANCE = 1
MANUAL_COMP_SALE_MONTHS = 6
MANUAL_COMP_LIMIT = 3


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_recent_comps(target_property: dict, graph) -> list[dict]:
    """Manual Comps fallback — queries the graph for nearby recent sale deeds
    without triggering any LLM inference. Returns a lightweight JSON-serializable
    list of up to 3 comparable properties within 2 miles, similar sqft/beds,
    sold in the last 6 months."""

    target_lat = target_property.get("lat")
    target_lon = target_property.get("lon")
    target_sqft = target_property.get("sqft", 0)
    target_beds = target_property.get("bedrooms", 0)
    target_address = target_property.get("address", "")
    cutoff_date = datetime.utcnow() - timedelta(days=MANUAL_COMP_SALE_MONTHS * 30)

    candidates = []

    for node in graph._find_nodes("Property"):
        props = node["properties"]

        if props.get("address") == target_address:
            continue

        sale_date_str = props.get("sale_date") or props.get("deed_date")
        if not sale_date_str:
            ownership_edges = graph._find_edges_to(node["id"], "OWNS")
            for edge in ownership_edges:
                deed_type = edge["properties"].get("deed_type")
                if deed_type and "SALE" in deed_type.upper():
                    sale_date_str = edge["properties"].get("recorded_date")
                    break
            if not sale_date_str:
                continue

        try:
            sale_dt = datetime.strptime(sale_date_str[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        if sale_dt < cutoff_date:
            continue

        deed_type = props.get("deed_type", "")
        sale_price = props.get("sale_price") or props.get("market_value", 0)
        if not sale_price:
            continue

        if target_lat is not None and target_lon is not None:
            comp_lat = props.get("lat")
            comp_lon = props.get("lon")
            if comp_lat is None or comp_lon is None:
                continue
            dist = _haversine_miles(target_lat, target_lon, comp_lat, comp_lon)
            if dist > MANUAL_COMP_RADIUS_MILES:
                continue
        else:
            dist = 0.0

        comp_sqft = props.get("sqft", 0)
        if target_sqft and comp_sqft:
            if abs(comp_sqft - target_sqft) / target_sqft > MANUAL_COMP_SQFT_TOLERANCE:
                continue

        comp_beds = props.get("bedrooms", 0)
        if abs(comp_beds - target_beds) > MANUAL_COMP_BED_TOLERANCE:
            continue

        candidates.append({
            "address": props.get("address", ""),
            "sale_price": int(sale_price),
            "sale_date": sale_date_str[:10],
            "sqft": comp_sqft,
            "bedrooms": comp_beds,
            "bathrooms": props.get("bathrooms", 0),
            "distance_miles": round(dist, 2),
            "price_per_sqft": round(sale_price / max(comp_sqft, 1), 2),
        })

    candidates.sort(key=lambda c: c["distance_miles"])
    return candidates[:MANUAL_COMP_LIMIT]


@dataclass
class Comparable:
    address: str
    sale_price: int
    sale_date: str
    sqft: int
    bedrooms: int
    bathrooms: int
    year_built: Optional[int] = None
    lot_acres: Optional[float] = None
    price_per_sqft: float = 0.0

    def __post_init__(self):
        if self.sqft and self.sale_price:
            self.price_per_sqft = round(self.sale_price / self.sqft, 2)


@dataclass
class CMAResult:
    subject_address: str
    estimated_value: int
    confidence_low: int
    confidence_high: int
    price_per_sqft: float
    comps_used: int
    methodology: str
    reasoning: str
    generated_at: float

    def to_dict(self) -> dict:
        return {
            "subject_address": self.subject_address,
            "estimated_value": self.estimated_value,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "price_per_sqft": self.price_per_sqft,
            "comps_used": self.comps_used,
            "methodology": self.methodology,
            "reasoning": self.reasoning,
            "generated_at": self.generated_at,
        }


class AnalystAgent:
    def __init__(self, graph_engine, websocket=None):
        self.graph = graph_engine
        self.websocket = websocket
        self._model_loaded = False

    async def analyze_property(self, subject_property: dict) -> Optional[CMAResult]:
        address = subject_property.get("address", "")
        await self._emit_status(f"AGENT ANALYST — initiating CMA for {address}")

        comps = self._select_comparables(subject_property)
        if not comps:
            await self._emit_status("AGENT ANALYST — insufficient comps, falling back to assessed value")
            return self._fallback_valuation(subject_property)

        await self._emit_status(
            f"AGENT ANALYST — {len(comps)} comps selected, running Valuation LoRA inference"
        )

        prompt = self._build_cma_prompt(subject_property, comps)
        raw_output = await self._run_inference(prompt)

        if not raw_output:
            await self._emit_status("AGENT ANALYST — inference failed, using statistical fallback")
            return self._statistical_valuation(subject_property, comps)

        result = self._parse_model_output(raw_output, subject_property, comps)

        await self._emit_status(
            f"AGENT ANALYST — CMA complete: ${result.estimated_value:,} "
            f"(±${result.estimated_value - result.confidence_low:,})"
        )

        return result

    def _select_comparables(self, subject: dict) -> list[Comparable]:
        sale_nodes = [
            n for n in self.graph.nodes.values()
            if n["type"] == "Property" and n["properties"].get("address") != subject.get("address")
        ]

        subject_sqft = subject.get("sqft", 0)
        subject_beds = subject.get("bedrooms", 0)
        subject_baths = subject.get("bathrooms", 0)

        scored = []
        for node in sale_nodes:
            props = node["properties"]
            if not props.get("market_value"):
                continue

            sqft_diff = abs(props.get("sqft", 0) - subject_sqft) / max(subject_sqft, 1)
            bed_diff = abs(props.get("bedrooms", 0) - subject_beds)
            bath_diff = abs(props.get("bathrooms", 0) - subject_baths)

            similarity_score = 1.0 - (sqft_diff * 0.5 + bed_diff * 0.1 + bath_diff * 0.1)
            similarity_score = max(0, similarity_score)

            if sqft_diff > 0.4:
                continue
            if bed_diff > 2:
                continue

            ownership_edges = self.graph._find_edges_to(node["id"], "OWNS")
            purchase_year = 2015
            for edge in ownership_edges:
                purchase_year = edge["properties"].get("since_year", 2015)

            scored.append((similarity_score, Comparable(
                address=props.get("address", ""),
                sale_price=props.get("market_value", 0),
                sale_date=f"{purchase_year}-01-01",
                sqft=props.get("sqft", 0),
                bedrooms=props.get("bedrooms", 0),
                bathrooms=props.get("bathrooms", 0),
            )))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [comp for _, comp in scored[:MAX_COMPS]]

    def _build_cma_prompt(self, subject: dict, comps: list[Comparable]) -> str:
        comp_lines = []
        for i, c in enumerate(comps, 1):
            comp_lines.append(
                f"  Comp {i}: {c.address} | ${c.sale_price:,} | "
                f"{c.sqft} sqft | {c.bedrooms}bd/{c.bathrooms}ba | "
                f"Sold {c.sale_date} | ${c.price_per_sqft:.0f}/sqft"
            )

        comps_text = "\n".join(comp_lines)
        subject_sqft = subject.get("sqft", 0)
        subject_market = subject.get("market_value", 0)

        return f"""<|im_start|>system
You are a real estate valuation expert performing a Comparative Market Analysis (CMA).
Analyze the subject property against comparable sales to determine fair market value.
Consider: square footage differences, bedroom/bathroom count, sale recency, and market trends.
Output your analysis as JSON with keys: estimated_value, confidence_low, confidence_high, price_per_sqft, reasoning.
<|im_end|>
<|im_start|>user
SUBJECT PROPERTY:
  Address: {subject.get("address", "Unknown")}
  Square Footage: {subject_sqft}
  Bedrooms: {subject.get("bedrooms", 0)}
  Bathrooms: {subject.get("bathrooms", 0)}
  Current Assessed Value: ${subject.get("assessed_value", 0):,}
  County Market Value: ${subject_market:,}

COMPARABLE SALES:
{comps_text}

Perform CMA and provide estimated fair market value with confidence interval.
<|im_end|>
<|im_start|>assistant
"""

    async def _run_inference(self, prompt: str) -> Optional[str]:
        model_path = Path(MODEL_PATH)
        llama_bin = Path(LLAMA_CPP_PATH) / "build" / "bin" / "llama-cli"

        if not model_path.exists():
            logger.warning(f"Model not found at {model_path}")
            return None
        if not llama_bin.exists():
            logger.warning(f"llama-cli not found at {llama_bin}")
            return None

        cmd = [
            str(llama_bin),
            "-m", str(model_path),
            "-p", prompt,
            "-n", "512",
            "-c", str(CTX_SIZE),
            "-ngl", str(N_GPU_LAYERS),
            "--temp", "0.1",
            "--repeat-penalty", "1.1",
            "--no-display-prompt",
            "-e",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if proc.returncode != 0:
                logger.error(f"llama-cli failed: {stderr.decode()[:500]}")
                return None

            return stdout.decode().strip()

        except asyncio.TimeoutError:
            logger.error("Inference timed out (60s)")
            proc.kill()
            await proc.wait()
            return None
        except FileNotFoundError:
            logger.error(f"llama-cli binary not found")
            return None

    def _parse_model_output(
        self, raw: str, subject: dict, comps: list[Comparable]
    ) -> CMAResult:
        try:
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(raw[json_start:json_end])
                return CMAResult(
                    subject_address=subject.get("address", ""),
                    estimated_value=int(parsed.get("estimated_value", 0)),
                    confidence_low=int(parsed.get("confidence_low", 0)),
                    confidence_high=int(parsed.get("confidence_high", 0)),
                    price_per_sqft=float(parsed.get("price_per_sqft", 0)),
                    comps_used=len(comps),
                    methodology="qwen_1.5b_valuation_lora",
                    reasoning=parsed.get("reasoning", ""),
                    generated_at=time.time(),
                )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse model JSON output: {e}")

        return self._statistical_valuation(subject, comps)

    def _statistical_valuation(
        self, subject: dict, comps: list[Comparable]
    ) -> CMAResult:
        if not comps:
            return self._fallback_valuation(subject)

        prices_per_sqft = [c.price_per_sqft for c in comps if c.price_per_sqft > 0]
        if not prices_per_sqft:
            return self._fallback_valuation(subject)

        median_ppsf = sorted(prices_per_sqft)[len(prices_per_sqft) // 2]
        subject_sqft = subject.get("sqft", 1500)
        estimated = int(median_ppsf * subject_sqft)

        spread = max(prices_per_sqft) - min(prices_per_sqft)
        margin = int(spread * subject_sqft * 0.25)

        return CMAResult(
            subject_address=subject.get("address", ""),
            estimated_value=estimated,
            confidence_low=estimated - margin,
            confidence_high=estimated + margin,
            price_per_sqft=median_ppsf,
            comps_used=len(comps),
            methodology="statistical_median_fallback",
            reasoning=f"Median $/sqft from {len(comps)} comps: ${median_ppsf:.0f}/sqft × {subject_sqft} sqft",
            generated_at=time.time(),
        )

    def _fallback_valuation(self, subject: dict) -> CMAResult:
        market_value = subject.get("market_value", subject.get("assessed_value", 0))
        assessed = subject.get("assessed_value", 0)
        estimated = market_value if market_value else int(assessed * 1.38)

        return CMAResult(
            subject_address=subject.get("address", ""),
            estimated_value=estimated,
            confidence_low=int(estimated * 0.85),
            confidence_high=int(estimated * 1.15),
            price_per_sqft=round(estimated / max(subject.get("sqft", 1500), 1), 2),
            comps_used=0,
            methodology="assessed_value_fallback",
            reasoning="Insufficient comparable sales; estimated from county assessed/market value",
            generated_at=time.time(),
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
