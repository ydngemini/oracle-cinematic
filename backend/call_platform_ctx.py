"""The platform context the call webhooks and the call-notes service read as.

Moved verbatim out of commands_api.py (external audit 35-02, slice 2) so that
commands_api's call-notes code and twilio_webhook_auth's durable call-state
lookup share one definition without importing each other. It reads only
ORACLE_PLATFORM_TENANT_ID, at call time, as before.
"""

from __future__ import annotations

import os

from tenancy import Role, TenantContext


def _call_platform_ctx(agent_id: str) -> TenantContext:
    return TenantContext(
        agent_id=agent_id,
        tenant_id=os.getenv("ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"),
        role=Role.PLATFORM_ADMIN,
    )
