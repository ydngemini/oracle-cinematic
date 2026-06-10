"""
CMA Synthesizer — the listing-appointment weapon.

POST /api/agents/generate-cma takes the property facts and returns a
three-part executive summary (retail value / highest-and-best-use per zoning /
the Neoh advantage) as markdown, generated through the same Bedrock client and
primary→secondary fallback as the tour generator and disposition enforcer.

Markdown is the deliberate output format: it renders in the dashboard, drops
into the client portal, and a PDF pipeline can consume it later without this
endpoint changing.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL, SECONDARY_MODEL
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.cma")

router = APIRouter(prefix="/api/agents", tags=["Listing Presentations"])

CMA_PROMPT = """You are an elite real-estate analyst building a listing presentation an agent
will hand to a homeowner.

Property data:
- Address: {address}
- Zoning: {zoning} (analyze the development potential this zoning permits)
- Lot/structure: {sqft} sqft
- AI-projected ARV: ${arv:,}

Write a three-part executive summary addressed to the homeowner:
1. **The Retail Value** — what a standard family buyer pays and why.
2. **The Highest & Best Use Value** — why a developer may pay more given the
   zoning; be specific about what the zoning allows. Do not invent zoning
   rights not implied by the code.
3. **The Neoh Advantage** — why 3D spatial scanning plus an off-market cash
   buyer network sells this faster than the MLS alone.

Use only the figures provided — never fabricate comps, prices, or guarantees.
Output a professional markdown document, no preamble."""


class CMARequest(BaseModel):
    property_address: str = Field(..., min_length=4, max_length=200)
    zoning_code: str = Field(..., min_length=1, max_length=40)
    square_footage: int = Field(..., gt=0, lt=1_000_000)
    arv_estimate: int = Field(..., gt=0, lt=1_000_000_000)


@router.post("/generate-cma")
async def generate_listing_presentation(
    body: CMARequest,
    ctx: TenantContext = Depends(require_context),
):
    prompt = CMA_PROMPT.format(
        address=body.property_address,
        zoning=body.zoning_code,
        sqft=body.square_footage,
        arv=body.arv_estimate,
    )

    raw = await asyncio.to_thread(invoke_bedrock_model, PRIMARY_MODEL, prompt, 3000)
    if not raw:
        raw = await asyncio.to_thread(invoke_bedrock_model, SECONDARY_MODEL, prompt, 3000)
    if not raw:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CMA generation unavailable — both Bedrock models failed",
        )

    logger.info(
        "CMA generated for %r (tenant=%s, %d chars)",
        body.property_address, ctx.tenant_id, len(raw),
    )
    return {"status": "success", "cma_markdown": raw}
