"""
Tenant-keyed WebSocket hub — broadcast frames to every dashboard a tenant has open.

Replaces the external pub/sub broker a multi-process deployment would need:
background workers (voice intelligence, future harvest completions) finish work
for a tenant and push a frame here; every connected socket for that tenant
receives it. Single-process by design — if the backend ever runs multiple
uvicorn workers, this module is the seam where Redis pub/sub slots in.

Registry mutations are plain set operations with no awaits between read and
write, so they are atomic per asyncio task — no lock needed.
"""

import json
import logging
import os
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger("oracle.ws_hub")

_sockets: dict[str, set[WebSocket]] = defaultdict(set)

# The platform tenant's sockets double as an all-tenant firehose: every frame
# broadcast to any tenant is mirrored there (annotated with source_tenant) so
# the platform-admin OPS console watches live traffic across the whole fleet.
# Membership in this group is JWT-gated — server._resolve_tenant only returns
# the platform tenant for a verified platform_admin token, never for the
# spoofable tenant_id query param.
FIREHOSE_TENANT_ID = os.getenv(
    "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)


def register(tenant_id: str, ws: WebSocket) -> None:
    _sockets[tenant_id].add(ws)


def unregister(tenant_id: str, ws: WebSocket) -> None:
    _sockets[tenant_id].discard(ws)
    if not _sockets[tenant_id]:
        del _sockets[tenant_id]


def connection_counts() -> dict[str, int]:
    """Live socket count per tenant group — surfaced on /api/admin/system."""
    return {tenant_id: len(socks) for tenant_id, socks in _sockets.items()}


async def _send_group(tenant_id: str, payload: dict) -> int:
    """Send one frame to every live socket in one tenant group.

    Dead sockets are dropped from the registry rather than raising — the WS
    handler's own finally block is the authoritative unregister path; this is
    just fast-path cleanup. Returns the number of sockets actually reached.
    """
    targets = list(_sockets.get(tenant_id, ()))
    if not targets:
        return 0

    text = json.dumps(payload)
    delivered = 0
    for ws in targets:
        try:
            await ws.send_text(text)
            delivered += 1
        except Exception:  # noqa: BLE001 — socket died between snapshot and send
            unregister(tenant_id, ws)
    return delivered


async def broadcast(tenant_id: str, payload: dict) -> int:
    """Send one frame to every live socket for this tenant, then mirror it to
    the platform firehose group (if anyone is watching). Returns the number of
    sockets actually reached across both groups."""
    delivered = await _send_group(tenant_id, payload)
    if tenant_id != FIREHOSE_TENANT_ID and FIREHOSE_TENANT_ID in _sockets:
        delivered += await _send_group(
            FIREHOSE_TENANT_ID, {**payload, "source_tenant": tenant_id}
        )
    return delivered
