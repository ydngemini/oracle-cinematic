"""The Intelligence Feed's read surface.

Deliberately read-only. The scan decides what deserves attention; acting on an
opportunity goes through the existing approved paths — the AI tool ledger for
CRM writes, the command-approval flow for anything that reaches a person —
because those already carry the audit trail, the compliance gate and the undo
this surface must not route around.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

import opportunity_engine
from platform_policy import Feature, require_feature
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.opportunity_api")

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])


@router.get("")
async def list_opportunities(ctx: TenantContext = Depends(require_context)):
    """Everything worth the agent's attention right now, ranked, with evidence.

    Gated on PREDICTIVE_INTELLIGENCE alongside the rest of the inference
    surface: a tenant who has not enabled that should not be served model
    output through a different door.
    """
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    return await opportunity_engine.scan(ctx)


@router.get("/perception")
async def perception(ctx: TenantContext = Depends(require_context)):
    """What the engine can and cannot currently see.

    Its own endpoint because it answers a question the feed cannot: whether an
    empty result means a quiet week or an unbuilt capture path.
    """
    require_feature(Feature.PREDICTIVE_INTELLIGENCE)
    return await opportunity_engine.perception_coverage(ctx)
