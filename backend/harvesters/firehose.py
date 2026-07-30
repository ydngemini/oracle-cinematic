"""
National Firehose — orchestrates every real state harvester around Delaware.

Coverage: all 50 states + DC (51 jurisdictions). The original Mid-Atlantic ring
(DE MD PA NJ NY VA WV CT MA NC) plus 41 statewide/county/city anchors added in
the harvest-states program (WY was the last state wired in).

Each jurisdiction is a real scraper against that jurisdiction's open-data
endpoint (ArcGIS / Socrata / CARTO; MD via the SDAT Playwright path). The
orchestrator runs them with shared rate-limiting, isolates per-state failures
(one dead portal never sinks the run), and prints aggregate ingestion metrics.

CLI:
  ORACLE_DB_PASSWORD=... ORACLE_INGEST_TENANT_ID=<uuid> \\
  python -m backend.harvesters.firehose            # all jurisdictions
  python -m backend.harvesters.firehose DE PA NJ   # a subset
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Literal, Optional

from .base import BaseHarvester, RateLimiter
from .de_firstmap import DelawareFirstMapHarvester
from .pa_philly_opa import PennsylvaniaPhillyOPAHarvester
from .nj_modiv import NewJerseyModIVHarvester
from .ny_pluto import NewYorkPlutoHarvester
from .va_vgin import VirginiaVGINHarvester
from .wv_parcels import WestVirginiaParcelsHarvester
from .ct_sales import ConnecticutSalesHarvester
from .ma_massgis import MassachusettsMassGISHarvester
from .nc_onemap import NorthCarolinaOneMapHarvester
from .md_sdat_ingest import MdSdatHarvester

# National expansion — 40 additional jurisdictions (statewide + county/city anchors).
from .ak_fnsb import AlaskaFNSBHarvester
from .al_jefferson import AlabamaJeffersonHarvester
from .ar_agiso import ArkansasAGISOHarvester
from .az_maricopa import ArizonaMaricopaHarvester
from .ca_san_diego import CaliforniaSanDiegoHarvester
from .co_parcels import ColoradoParcelsHarvester
from .dc_rpta import DCRPTAHarvester
from .fl_fdor import FloridaFDORHarvester
from .ga_fulton import GeorgiaFultonHarvester
from .hi_honolulu import HawaiiHonoluluHarvester
from .ia_linn import IowaLinnHarvester
from .id_whiteStar import IdahoWhiteStarHarvester
from .il_cook import IllinoisCookHarvester
from .in_indy import IndianaIndyHarvester
from .ks_shawnee import KansasShawneeHarvester
from .ky_boone import KentuckyBoonePVAHarvester
from .la_ebr import LouisianaEBRHarvester
from .me_parcels import MaineParcelsHarvester
from .mi_kent import MichiganKentHarvester
from .mn_parcels import MinnesotaGeospatialHarvester
from .mo_jackson import MissouriJacksonHarvester
from .ms_mdeq import MississippiMDEQHarvester
from .mt_cadastral import MontanaCadastralHarvester
from .nd_gishub import NorthDakotaGISHubHarvester
from .ne_lancaster import NebraskaLancasterHarvester
from .nh_granit import NewHampshireGRANITHarvester
from .nm_bernalillo import NewMexicoBernalilloHarvester
from .nv_washoe import NevadaWashoeHarvester
from .oh_franklin import OhioFranklinHarvester
from .ok_oklahoma_county import OklahomaCountyHarvester
from .or_marion import OregonMarionHarvester
from .ri_pvd import RhodeIslandProvidenceHarvester
from .sc_horry import SouthCarolinaHorryHarvester
from .sd_pennington import SouthDakotaPenningtonHarvester
from .tn_shelby import TennesseeShelbyHarvester
from .tx_bexar import TexasBexarHarvester
from .ut_utah_county import UtahCountyHarvester
from .vt_vcgi import VermontVCGIHarvester
from .wa_snohomish import WASnohomishHarvester
from .wi_parcels import WisconsinSCOHarvester
from .wy_parcels import WyomingParcelsHarvester

logger = logging.getLogger("oracle.harvester.firehose")

# Delaware at the center, then the I-95 / Mid-Atlantic ring.
REGISTRY = {
    "DE": DelawareFirstMapHarvester,
    "MD": MdSdatHarvester,
    "PA": PennsylvaniaPhillyOPAHarvester,
    "NJ": NewJerseyModIVHarvester,
    "NY": NewYorkPlutoHarvester,
    "VA": VirginiaVGINHarvester,
    "WV": WestVirginiaParcelsHarvester,
    "CT": ConnecticutSalesHarvester,
    "MA": MassachusettsMassGISHarvester,
    "NC": NorthCarolinaOneMapHarvester,
    # National expansion — 41 jurisdictions added in the harvest-states program.
    "AK": AlaskaFNSBHarvester,
    "AL": AlabamaJeffersonHarvester,
    "AR": ArkansasAGISOHarvester,
    "AZ": ArizonaMaricopaHarvester,
    "CA": CaliforniaSanDiegoHarvester,
    "CO": ColoradoParcelsHarvester,
    "DC": DCRPTAHarvester,
    "FL": FloridaFDORHarvester,
    "GA": GeorgiaFultonHarvester,
    "HI": HawaiiHonoluluHarvester,
    "IA": IowaLinnHarvester,
    "ID": IdahoWhiteStarHarvester,
    "IL": IllinoisCookHarvester,
    "IN": IndianaIndyHarvester,
    "KS": KansasShawneeHarvester,
    "KY": KentuckyBoonePVAHarvester,
    "LA": LouisianaEBRHarvester,
    "ME": MaineParcelsHarvester,
    "MI": MichiganKentHarvester,
    "MN": MinnesotaGeospatialHarvester,
    "MO": MissouriJacksonHarvester,
    "MS": MississippiMDEQHarvester,
    "MT": MontanaCadastralHarvester,
    "ND": NorthDakotaGISHubHarvester,
    "NE": NebraskaLancasterHarvester,
    "NH": NewHampshireGRANITHarvester,
    "NM": NewMexicoBernalilloHarvester,
    "NV": NevadaWashoeHarvester,
    "OH": OhioFranklinHarvester,
    "OK": OklahomaCountyHarvester,
    "OR": OregonMarionHarvester,
    "RI": RhodeIslandProvidenceHarvester,
    "SC": SouthCarolinaHorryHarvester,
    "SD": SouthDakotaPenningtonHarvester,
    "TN": TennesseeShelbyHarvester,
    "TX": TexasBexarHarvester,
    "UT": UtahCountyHarvester,
    "VT": VermontVCGIHarvester,
    "WA": WASnohomishHarvester,
    "WI": WisconsinSCOHarvester,
    "WY": WyomingParcelsHarvester,
}


FirehoseMode = Literal["harvest", "probe", "catalog_backfill"]


class _LiveProbeCache:
    """Minimal cache seam that forces a real, non-persisted probe request."""

    @staticmethod
    def metrics() -> dict[str, int]:
        return {"hits": 0, "misses": 0}

    async def get_or_fetch(self, _source, _request, fetcher, **_kwargs):
        return await fetcher()


class MultiStateFirehose:
    def __init__(
        self,
        tenant_id: str,
        states: Optional[list[str]] = None,
        agent_id: str = "firehose",
        *,
        mode: FirehoseMode = "harvest",
    ):
        if not tenant_id:
            raise ValueError("tenant_id required (ORACLE_INGEST_TENANT_ID).")
        if mode not in {"harvest", "probe", "catalog_backfill"}:
            raise ValueError(f"Unknown firehose mode: {mode!r}")
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.mode: FirehoseMode = mode
        self.states = [s.upper() for s in (states or list(REGISTRY))]
        unknown = [s for s in self.states if s not in REGISTRY]
        if unknown:
            raise ValueError(f"Unknown state(s): {unknown}. Known: {list(REGISTRY)}")
        # Each source gets its own polite limiter. A global limiter needlessly
        # serialized unrelated government hosts and made national refreshes
        # several times slower without reducing load on any individual portal.
        self.limiters = {state: RateLimiter() for state in self.states}

    def _build(self, state: str):
        cls = REGISTRY[state]
        if issubclass(cls, BaseHarvester):
            return cls(
                self.tenant_id,
                agent_id=f"{self.agent_id}-{state.lower()}",
                limiter=self.limiters[state],
            )
        # MdSdatHarvester predates BaseHarvester (Playwright path) — its own limiter.
        return cls(self.tenant_id, agent_id=f"{self.agent_id}-md")

    def _tracking_source_key(self, state: str) -> str:
        if self.mode == "catalog_backfill":
            return f"property_characteristics_backfill_{state.lower()}"
        return f"regional_parcels_{state.lower()}"

    async def _checkpoint(self, state: str) -> int:
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(ctx) as conn:
            value = await conn.fetchval(
                """
                SELECT cursor_value FROM harvest_sources
                 WHERE tenant_id=$1::uuid AND source_key=$2
                """,
                self.tenant_id,
                self._tracking_source_key(state),
            )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    async def _save_checkpoint(
        self, state: str, metrics: dict[str, Any], adapter_name: str
    ) -> None:
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        checkpoint = metrics.get("checkpoint")
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                INSERT INTO harvest_sources (
                    tenant_id,source_key,display_name,jurisdiction,adapter,
                    cursor_value,cursor_observed_at,last_started_at,
                    last_succeeded_at,last_record_observed_at,coverage
                ) VALUES ($1::uuid,$2,$3,$4,$5,$6,now(),now(),now(),now(),$7::jsonb)
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    cursor_value=EXCLUDED.cursor_value,
                    cursor_observed_at=EXCLUDED.cursor_observed_at,
                    last_succeeded_at=EXCLUDED.last_succeeded_at,
                    last_record_observed_at=EXCLUDED.last_record_observed_at,
                    coverage=EXCLUDED.coverage,
                    failure_count=0,circuit_state='closed',last_error=NULL,
                    last_health_checked_at=now(),health_status='fresh',health_detail=NULL,
                    updated_at=now()
                """,
                self.tenant_id,
                self._tracking_source_key(state),
                f"{state} checkpointed parcel ingestion",
                state,
                adapter_name,
                str(checkpoint) if checkpoint is not None else None,
                json.dumps(metrics, default=str),
            )

    async def _record_failure(self, state: str, harvester: Any, error: Exception) -> None:
        """Persist a per-jurisdiction failure without aborting other states."""
        from db.connection import tenant_tx
        from harvest_health import safe_health_detail
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        detail = safe_health_detail(error)
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                INSERT INTO harvest_sources (
                    tenant_id,source_key,display_name,jurisdiction,adapter,
                    last_started_at,last_health_checked_at,health_status,health_detail,
                    failure_count,circuit_state,last_error
                ) VALUES ($1::uuid,$2,$3,$4,$5,now(),now(),'degraded',$6,1,'closed',$6)
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    last_started_at=now(),last_health_checked_at=now(),
                    failure_count=harvest_sources.failure_count+1,
                    circuit_state=CASE WHEN harvest_sources.failure_count+1>=5
                        THEN 'open' ELSE harvest_sources.circuit_state END,
                    health_status=CASE WHEN harvest_sources.failure_count+1>=5
                        THEN 'failed' ELSE 'degraded' END,
                    health_detail=EXCLUDED.health_detail,last_error=EXCLUDED.last_error,
                    updated_at=now()
                """,
                self.tenant_id,
                self._tracking_source_key(state),
                str(getattr(harvester, "SOURCE_LABEL", f"{state} public parcels")),
                state,
                type(harvester).__name__,
                detail,
            )

    async def _record_probe_success(
        self,
        state: str,
        metrics: dict[str, Any],
        adapter_name: str,
    ) -> None:
        """Record source reachability without changing its durable cursor."""
        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        ctx = TenantContext(
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                INSERT INTO harvest_sources (
                    tenant_id,source_key,display_name,jurisdiction,adapter,
                    last_started_at,last_succeeded_at,last_record_observed_at,
                    last_health_checked_at,health_status,coverage
                ) VALUES (
                    $1::uuid,$2,$3,$4,$5,now(),now(),now(),now(),'fresh',$6::jsonb
                )
                ON CONFLICT (tenant_id,source_key) DO UPDATE SET
                    last_started_at=now(),last_succeeded_at=now(),
                    last_record_observed_at=now(),last_health_checked_at=now(),
                    health_status='fresh',health_detail=NULL,failure_count=0,
                    circuit_state='closed',last_error=NULL,
                    coverage=harvest_sources.coverage || EXCLUDED.coverage,
                    updated_at=now()
                """,
                self.tenant_id,
                f"regional_parcels_{state.lower()}",
                str(metrics.get("source") or f"{state} public parcels"),
                state,
                adapter_name,
                json.dumps(
                    {
                        "probe_fetched": int(metrics.get("fetched") or 0),
                        "probe_checked_at": time.time(),
                    }
                ),
            )

    async def _run_one(self, state: str, max_records: Optional[int], sem: asyncio.Semaphore) -> dict:
        async with sem:
            harvester = self._build(state)
            try:
                checkpoint = 0 if self.mode == "probe" else await self._checkpoint(state)
                if self.mode == "probe":
                    # A health probe must observe the live portal and must not
                    # write 51 throwaway response rows into PostgreSQL.
                    harvester._cache = _LiveProbeCache()
                metrics = await harvester.harvest(
                    max_records=max_records,
                    checkpoint=checkpoint,
                    persist=self.mode == "harvest",
                )
                if self.mode == "catalog_backfill":
                    from .base import upsert_public_records

                    records = list(getattr(harvester, "_records", []))
                    metrics["inserted"] = await upsert_public_records(
                        self.tenant_id,
                        self.agent_id,
                        records,
                        metrics=metrics,
                    )
                    await self._save_checkpoint(
                        state, metrics, type(harvester).__name__
                    )
                elif self.mode == "probe":
                    await self._record_probe_success(
                        state, metrics, type(harvester).__name__
                    )
                else:
                    await self._save_checkpoint(
                        state, metrics, type(harvester).__name__
                    )
                return metrics
            except Exception as e:  # noqa: BLE001 — isolate one portal's failure
                logger.error("[%s] harvest failed: %s", state, e)
                try:
                    await self._record_failure(state, harvester, e)
                except Exception as persistence_error:  # noqa: BLE001 -- preserve original failure signal
                    logger.error("[%s] could not persist harvest failure: %s", state, persistence_error)
                return {"state": state, "error": str(e)[:200],
                        "fetched": 0, "parsed": 0, "inserted": 0}

    async def run(self, *, max_records_per_state: Optional[int] = None, concurrency: int = 2) -> dict:
        t0 = time.monotonic()
        logger.info(
            "Firehose run: %s (mode=%s, concurrency=%d, cap=%s/state)",
            ", ".join(self.states), self.mode, concurrency,
            max_records_per_state or "∞",
        )
        sem = asyncio.Semaphore(max(1, concurrency))
        per_state = await asyncio.gather(
            *(self._run_one(s, max_records_per_state, sem) for s in self.states)
        )

        results = {m["state"]: m for m in per_state}
        totals = {
            "requests": sum(m.get("requests", 0) for m in per_state),
            "retries": sum(m.get("retries", 0) for m in per_state),
            "cache_hits": sum(m.get("cache_hits", 0) for m in per_state),
            "cache_misses": sum(m.get("cache_misses", 0) for m in per_state),
            "fetched": sum(m.get("fetched", 0) for m in per_state),
            "parsed": sum(m.get("parsed", 0) for m in per_state),
            "inserted": sum(m.get("inserted", 0) for m in per_state),
            "errors": sum(1 for m in per_state if m.get("error")),
            "failed_states": sorted(m["state"] for m in per_state if m.get("error")),
            "zero_result_states": sorted(
                m["state"] for m in per_state
                if not m.get("error") and int(m.get("fetched") or 0) == 0
            ),
            "elapsed_s": round(time.monotonic() - t0, 2),
            "checkpointed_states": sum(
                1 for m in per_state if "checkpoint_complete" in m
            ),
        }
        logger.info("Firehose complete in %.1fs — inserted=%d across %d states (%d errors)",
                    totals["elapsed_s"], totals["inserted"],
                    len(self.states) - totals["errors"], totals["errors"])
        for st in self.states:
            m = results.get(st, {})
            logger.info("  %-3s inserted=%-6d fetched=%-7d %s",
                        st, m.get("inserted", 0), m.get("fetched", 0),
                        f"ERROR: {m['error']}" if m.get("error") else "")
        return {"states": results, "totals": totals}


async def _main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tenant_id = os.getenv("ORACLE_INGEST_TENANT_ID", "")
    if not tenant_id:
        raise SystemExit("Set ORACLE_INGEST_TENANT_ID to the target tenant UUID.")

    import sys
    from pathlib import Path
    # Make sibling backend modules importable (db, tenancy, etc.) when invoked
    # from the repo root as `python -m backend.harvesters.firehose`.
    _backend = str(Path(__file__).resolve().parent.parent)
    if _backend not in sys.path:
        sys.path.insert(0, _backend)

    from db.connection import init_pool, close_pool

    states = [a for a in sys.argv[1:] if not a.startswith("-")] or None
    cap = int(os.getenv("FIREHOSE_MAX_PER_STATE", "0")) or None

    await init_pool()
    try:
        firehose = MultiStateFirehose(tenant_id, states=states)
        result = await firehose.run(max_records_per_state=cap)
        logger.info("Totals: %s", json.dumps(result["totals"]))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
