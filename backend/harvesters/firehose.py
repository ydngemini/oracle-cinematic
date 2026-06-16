"""
National Firehose — orchestrates every real state harvester around Delaware.

Coverage: all 50 states + DC (51 jurisdictions). The original Mid-Atlantic ring
(DE MD PA NJ NY VA WV CT MA NC) plus 40 statewide/county/city anchors added in
the harvest-states program.

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
from typing import Optional

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
    # National expansion — 40 jurisdictions added in the harvest-states program.
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
}


class MultiStateFirehose:
    def __init__(self, tenant_id: str, states: Optional[list[str]] = None, agent_id: str = "firehose"):
        if not tenant_id:
            raise ValueError("tenant_id required (ORACLE_INGEST_TENANT_ID).")
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.states = [s.upper() for s in (states or list(REGISTRY))]
        unknown = [s for s in self.states if s not in REGISTRY]
        if unknown:
            raise ValueError(f"Unknown state(s): {unknown}. Known: {list(REGISTRY)}")
        # One shared limiter keeps the whole run polite even under concurrency.
        self.limiter = RateLimiter()

    def _build(self, state: str):
        cls = REGISTRY[state]
        if issubclass(cls, BaseHarvester):
            return cls(self.tenant_id, agent_id=f"{self.agent_id}-{state.lower()}", limiter=self.limiter)
        # MdSdatHarvester predates BaseHarvester (Playwright path) — its own limiter.
        return cls(self.tenant_id, agent_id=f"{self.agent_id}-md")

    async def _run_one(self, state: str, max_records: Optional[int], sem: asyncio.Semaphore) -> dict:
        async with sem:
            try:
                return await self._build(state).harvest(max_records=max_records)
            except Exception as e:  # noqa: BLE001 — isolate one portal's failure
                logger.error("[%s] harvest failed: %s", state, e)
                return {"state": state, "error": str(e)[:200],
                        "fetched": 0, "parsed": 0, "inserted": 0}

    async def run(self, *, max_records_per_state: Optional[int] = None, concurrency: int = 2) -> dict:
        t0 = time.monotonic()
        logger.info("Firehose run: %s (concurrency=%d, cap=%s/state)",
                    ", ".join(self.states), concurrency, max_records_per_state or "∞")
        sem = asyncio.Semaphore(max(1, concurrency))
        per_state = await asyncio.gather(
            *(self._run_one(s, max_records_per_state, sem) for s in self.states)
        )

        results = {m["state"]: m for m in per_state}
        totals = {
            "fetched": sum(m.get("fetched", 0) for m in per_state),
            "parsed": sum(m.get("parsed", 0) for m in per_state),
            "inserted": sum(m.get("inserted", 0) for m in per_state),
            "errors": sum(1 for m in per_state if m.get("error")),
            "elapsed_s": round(time.monotonic() - t0, 2),
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
