"""
State Compliance, Licensing, MLS Integration, and Market Data Router.

Five endpoint groups covering the full regulatory surface for a multi-state
real-estate operation:

  * /api/states        — State regulatory profiles: disclosure forms, contract
                         templates, advertising rules.
  * /api/licensing     — Agent license requirements per state, reciprocity
                         matrix, per-agent status, CE credit logging.
  * /api/mls           — MLS board registry, sync health, normalized property
                         search, and listing detail.
  * /api/market        — State/county aggregate stats, flood zone (FEMA),
                         school district, and zoning lookups.
  * /api/compliance    — Transactional compliance engine: form checklist
                         generation, disclosure tracking, form validation.

Auth pattern: every route calls require_context (Bearer JWT → TenantContext).
Role-differentiated gates use require_role() inline.  All DB reads go through
tenant_tx(ctx) so RLS + SET LOCAL GUCs apply automatically.

Tenant scoping: compliance state data (forms, contracts) is shared/public but
agent licensing rows and transaction checklists are per-tenant.  The engine
class encapsulates the state-specific business rules; the routes are thin.
"""

# NOTE: this module was split from a former 2067-line monolith into the
# per-concern submodules below. Public contract is unchanged:
#     from state_compliance import router
from ._common import router
# Imported for their side effect: each module registers its routes on `router`.
from . import routes_reference  # noqa: F401 - side effect: registers routes on router
from . import routes_coverage  # noqa: F401 - side effect: registers routes on router
from . import routes_agents  # noqa: F401 - side effect: registers routes on router
from . import routes_mls  # noqa: F401 - side effect: registers routes on router
from . import routes_market  # noqa: F401 - side effect: registers routes on router
from . import routes_compliance  # noqa: F401 - side effect: registers routes on router

__all__ = ["router"]
