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
from dataclasses import dataclass, field
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
    # Fact-check provenance from the AVM reconciliation layer. Defaults preserve
    # backward compatibility for the comps/LoRA/statistical paths, which set a
    # plain methodology string and leave the verdict UNVERIFIED.
    verdict: str = "UNVERIFIED"
    confidence: float = 0.0
    sources: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    # Distress-aware figures (set by the distress_valuation safety layer). For a
    # distressed/off-market target, `estimated_value` is the conservative AS-IS
    # value; `arv_estimate` is the retail/after-repair value (the raw AVM number);
    # `investor_offer` is the max acquisition offer. confidence_low/high mirror
    # the as-is band. Defaults keep non-distress callers backward compatible.
    arv_estimate: int = 0
    as_is_low: int = 0
    as_is_high: int = 0
    investor_offer: int = 0
    distress_severity: str = "UNKNOWN"
    valuation_assumptions: list = field(default_factory=list)

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
            "verdict": self.verdict,
            "confidence": self.confidence,
            "sources": self.sources,
            "checks": self.checks,
            "arv_estimate": self.arv_estimate,
            "as_is_low": self.as_is_low,
            "as_is_high": self.as_is_high,
            "investor_offer": self.investor_offer,
            "distress_severity": self.distress_severity,
            "valuation_assumptions": self.valuation_assumptions,
        }


class AnalystAgent:
    def __init__(self, graph_engine, websocket=None):
        self.graph = graph_engine
        self.websocket = websocket
        self._model_loaded = False

    async def analyze_property(self, subject_property: dict) -> Optional[CMAResult]:
        address = subject_property.get("address", "")
        await self._emit_status(f"AGENT ANALYST — initiating CMA for {address}")

        # 1) Establish a RETAIL valuation from the best available source.
        base = await self._compute_base_valuation(subject_property)
        if base is None:
            return None

        # 2) Convert retail → decision-relevant figures via the distress safety
        #    layer. The AVM gives retail/ARV (good condition, arms-length); our
        #    targets are off-market & distressed, so we must derive a conservative
        #    as-is value and a max acquisition offer rather than offer retail.
        return self._finalize_valuation(base, subject_property)

    async def _compute_base_valuation(self, subject_property: dict) -> Optional[CMAResult]:
        """Retail/ARV valuation from the best available source: real external AVM
        first (off-market parcels rarely have in-graph comps), then comps + LoRA,
        then statistical, then assessed-value fallback."""
        # Real-AVM first, with its fact-check layer. Returns None on UNAVAILABLE.
        avm_cma = await self._external_avm_cma(subject_property)
        if avm_cma is not None:
            return avm_cma

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

        return self._parse_model_output(raw_output, subject_property, comps)

    def _finalize_valuation(
        self, base: CMAResult, subject: dict, strategy: str = "wholesale"
    ) -> CMAResult:
        """Apply the distress-aware safety layer to a retail valuation. Treats the
        base estimate as retail/ARV, derives a conservative as-is band and a max
        acquisition offer, lowers confidence for unknown-condition distress, and
        surfaces every assumption. Never fabricates a value (ARV 0 → all 0)."""
        import distress_valuation as dv

        arv = base.estimated_value
        d = dv.adjust(arv, subject, strategy=strategy)

        base.distress_severity = d.severity
        base.valuation_assumptions = d.assumptions
        base.arv_estimate = d.arv

        if d.arv <= 0:
            # No base value (e.g. real lead, no AVM key) — keep it honestly zero.
            base.investor_offer = 0
            return base

        base.arv_estimate = d.arv
        base.estimated_value = d.as_is_mid       # decision-relevant: as-is value
        base.confidence_low = d.as_is_low
        base.confidence_high = d.as_is_high
        base.as_is_low = d.as_is_low
        base.as_is_high = d.as_is_high
        base.investor_offer = d.investor_offer
        base.price_per_sqft = round(d.as_is_mid / max(subject.get("sqft", 0) or 1, 1), 2)
        base.confidence = max(0.1, round(base.confidence - d.confidence_penalty, 2))
        base.methodology = f"{base.methodology} → as-is ({d.severity})"
        base.reasoning = (
            f"ARV ${d.arv:,} [{base.reasoning}]. As-is ${d.as_is_mid:,} "
            f"(${d.as_is_low:,}–${d.as_is_high:,}) after ~{int(d.total_discount_pct*100)}% "
            f"distress/condition adjustment. Max offer ${d.investor_offer:,}. "
            + " ".join(d.assumptions)
        )
        return base

    async def _external_avm_cma(self, subject: dict) -> Optional[CMAResult]:
        """Query the real-AVM + fact-check layer. Returns a CMAResult when a
        provider produced a value (even UNVERIFIED — the verdict carries the
        caveat), or None when no provider is configured/answered so the caller
        falls through to the comps path. Never fabricates a value."""
        try:
            import avm_client
            result = await avm_client.value_property(subject)
        except Exception as e:  # noqa: BLE001 — AVM is best-effort
            logger.warning("External AVM failed: %s", e)
            return None

        if result.verdict == "UNAVAILABLE" or result.estimated_value <= 0:
            return None

        src = "+".join(result.sources) or "avm"
        await self._emit_status(
            f"AGENT ANALYST — AVM {result.verdict} via {src} "
            f"(conf {result.confidence:.0%}): ${result.estimated_value:,}"
        )
        return CMAResult(
            subject_address=subject.get("address", ""),
            estimated_value=result.estimated_value,
            confidence_low=result.confidence_low,
            confidence_high=result.confidence_high,
            price_per_sqft=result.price_per_sqft,
            comps_used=result.comps_used,
            methodology=f"Real AVM ({src}) + fact-check [{result.verdict}]",
            reasoning=self._avm_reasoning(result),
            generated_at=time.time(),
            verdict=result.verdict,
            confidence=result.confidence,
            sources=result.sources,
            checks=result.checks,
        )

    @staticmethod
    def _avm_reasoning(result) -> str:
        """Human-readable summary of which fact-checks passed/failed."""
        bits = []
        for name, c in (result.checks or {}).items():
            p = c.get("pass") if isinstance(c, dict) else None
            mark = "✓" if p is True else ("✗" if p is False else "–")
            bits.append(f"{name}{mark}")
        verdict_note = {
            "VERIFIED": "cross-checks agree",
            "UNVERIFIED": "insufficient corroboration — treat as indicative",
            "CONFLICT": "sources disagree >20% — do not rely on point value",
        }.get(result.verdict, "")
        return f"{result.verdict}: {verdict_note}. Checks: {' '.join(bits)}."

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
