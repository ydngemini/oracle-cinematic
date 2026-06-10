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
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger("oracle.ws_hub")

_sockets: dict[str, set[WebSocket]] = defaultdict(set)


def register(tenant_id: str, ws: WebSocket) -> None:
    _sockets[tenant_id].add(ws)


def unregister(tenant_id: str, ws: WebSocket) -> None:
    _sockets[tenant_id].discard(ws)
    if not _sockets[tenant_id]:
        del _sockets[tenant_id]


async def broadcast(tenant_id: str, payload: dict) -> int:
    """Send one frame to every live socket for this tenant.

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
