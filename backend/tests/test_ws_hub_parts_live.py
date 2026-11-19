"""A frame too large for one NOTIFY crosses replicas in parts on real
PostgreSQL (audit 27.6): the parts arrive together, in order, under the
server's payload limit, and reassemble on the other replica.
Skipped without ORACLE_LIVE_DB_ADMIN_DSN."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


class _Ws:
    def __init__(self):
        self.frames = []

    async def send_text(self, text):
        self.frames.append(json.loads(text))


def test_a_large_unicode_frame_crosses_replicas_in_parts(monkeypatch):
    asyncio.run(_scenario(monkeypatch))


async def _scenario(monkeypatch):
    import asyncpg

    import ws_hub

    received: list[str] = []
    listener = await asyncpg.connect(DSN)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        await listener.add_listener(ws_hub._CHANNEL, lambda *a: received.append(a[3]))
        monkeypatch.setattr(ws_hub, "_pool", pool)
        monkeypatch.setattr(ws_hub, "_listener_started", True)
        payload = {"type": "TRANSCRIPT_LINE", "text": ("合同条款😀" * 800)[:4000]}
        await ws_hub._publish_scoped("live-27-6:voice:1", None, payload, mirror=False)
        for _ in range(50):
            if received and all('"part"' in r for r in received) and len(received) == json.loads(received[0])["n"]:
                break
            await asyncio.sleep(0.05)
        assert len(received) > 1
        assert [json.loads(r)["i"] for r in received] == list(range(len(received))), "in order"

        receiver = _Ws()
        ws_hub._sockets["live-27-6:voice:1"] = {receiver}
        monkeypatch.setattr(ws_hub, "_instance_id", "another-replica")
        for raw in received:
            await ws_hub._receive_notification(raw)
        assert receiver.frames == [payload]
    finally:
        ws_hub._sockets.pop("live-27-6:voice:1", None)
        await listener.close()
        await pool.close()
