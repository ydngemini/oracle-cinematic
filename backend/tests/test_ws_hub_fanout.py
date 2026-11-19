"""Realtime fan-out robustness (audits 27.6, 27.7): a frame too large for one
PostgreSQL NOTIFY crosses replicas in parts, and one stalled browser does not
hold a broadcast up for everyone else."""

from __future__ import annotations

import asyncio
import json

import pytest

import ws_hub


class _Ws:
    def __init__(self, stall=False):
        self.stall, self.frames, self.closed = stall, [], False

    async def send_text(self, text):
        if self.stall:
            await asyncio.sleep(3600)
        self.frames.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ws_hub._sockets.clear()
    ws_hub._user_sockets.clear()
    ws_hub._pending_parts.clear()
    monkeypatch.setattr(ws_hub, "SEND_TIMEOUT_SECONDS", 0.2)
    yield
    ws_hub._sockets.clear()
    ws_hub._user_sockets.clear()


def test_a_stalled_socket_does_not_delay_the_others_and_is_dropped():
    async def run():
        stalled, healthy = _Ws(stall=True), [_Ws() for _ in range(3)]
        ws_hub._sockets["t"] = {stalled, *healthy}
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Bounded so a regression fails fast instead of hanging the suite.
        delivered = await asyncio.wait_for(ws_hub._send_group("t", {"type": "X"}), timeout=3)
        took = loop.time() - started
        await asyncio.sleep(0.05)  # let the close task run
        return stalled, healthy, delivered, took

    stalled, healthy, delivered, took = asyncio.run(run())
    assert delivered == 3 and all(w.frames == [{"type": "X"}] for w in healthy)
    assert took < 1.0, "bounded by one send timeout, not a sum of them"
    assert stalled not in ws_hub._sockets.get("t", set()) and stalled.closed
    assert ws_hub.send_stats()["timeouts"] >= 1


class _Conn:
    def __init__(self, sent):
        self.sent = sent

    async def execute(self, sql, channel, payload):
        assert len(payload.encode("utf-8")) <= ws_hub._MAX_NOTIFY_BYTES + 500
        self.sent.append(payload)

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

        return _Tx()


class _Pool:
    def __init__(self, sent):
        self.sent = sent

    def acquire(self):
        conn = _Conn(self.sent)

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def test_a_4000_character_unicode_transcript_chunk_reaches_another_replica(monkeypatch):
    """27.6: 4,000 CJK characters / emoji exceeded the 7,500-byte NOTIFY cap
    (ASCII escaping made it ~24 KB) and were logged and dropped."""
    sent: list[str] = []
    monkeypatch.setattr(ws_hub, "_pool", _Pool(sent))
    monkeypatch.setattr(ws_hub, "_listener_started", True)
    text = ("合同条款😀" * 800)[:4000]
    payload = {"type": "TRANSCRIPT_LINE", "text": text}
    asyncio.run(ws_hub._publish_scoped("t:voice:1", None, payload, mirror=False))
    assert len(sent) > 1, "sent in parts"
    assert all(len(p.encode("utf-8")) < 8000 for p in sent)

    # Another replica receives the parts (shuffled within the transaction's
    # delivery is not possible, but a reorder must still reassemble).
    receiver = _Ws()
    ws_hub._sockets["t:voice:1"] = {receiver}
    monkeypatch.setattr(ws_hub, "_instance_id", "another-replica")

    async def deliver():
        for raw in reversed(sent):
            await ws_hub._receive_notification(raw)

    asyncio.run(deliver())
    assert receiver.frames == [payload]
    assert not ws_hub._pending_parts


def test_an_incomplete_multi_part_frame_expires_and_is_never_delivered(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(ws_hub, "_pool", _Pool(sent))
    monkeypatch.setattr(ws_hub, "_listener_started", True)
    asyncio.run(ws_hub._publish_scoped("t", None, {"type": "X", "text": "é" * 9000}, mirror=False))
    receiver = _Ws()
    ws_hub._sockets["t"] = {receiver}
    monkeypatch.setattr(ws_hub, "_instance_id", "another-replica")
    asyncio.run(ws_hub._receive_notification(sent[0]))
    assert receiver.frames == [] and len(ws_hub._pending_parts) == 1
    monkeypatch.setattr(ws_hub, "_PART_TTL_SECONDS", -1)
    asyncio.run(ws_hub._receive_notification(sent[0]))  # expiry runs on the next part
    assert receiver.frames == []


def test_a_malformed_part_is_ignored():
    asyncio.run(ws_hub._receive_notification(json.dumps({"origin": "x", "part": "p", "i": 5, "n": 2, "d": "x"})))
    assert not ws_hub._pending_parts


def test_a_frame_too_large_even_in_parts_is_refused(monkeypatch, caplog):
    sent: list[str] = []
    monkeypatch.setattr(ws_hub, "_pool", _Pool(sent))
    monkeypatch.setattr(ws_hub, "_listener_started", True)
    huge = "x" * (ws_hub._PART_CHARS * (ws_hub._MAX_PARTS + 1))
    asyncio.run(ws_hub._publish("t", {"text": huge}))
    assert sent == []


def test_a_stalled_agent_socket_is_dropped_from_targeted_sends_too():
    async def run():
        stalled, healthy = _Ws(stall=True), _Ws()
        ws_hub._user_sockets[("t", "a@x.test")] = {stalled}
        ws_hub._user_sockets[("t", "b@x.test")] = {healthy}
        n = await asyncio.wait_for(ws_hub._send_users("t", ["A@x.test", "b@x.test"], {"type": "Y"}), timeout=3)
        await asyncio.sleep(0.05)
        return stalled, healthy, n

    stalled, healthy, n = asyncio.run(run())
    assert n == 1 and healthy.frames == [{"type": "Y"}] and stalled.closed
    assert ("t", "a@x.test") not in ws_hub._user_sockets


def test_a_lone_surrogate_does_not_break_a_broadcast(monkeypatch):
    """27.7 review: with UTF-8 envelopes, a lone surrogate would raise."""
    sent: list[str] = []
    monkeypatch.setattr(ws_hub, "_pool", _Pool(sent))
    monkeypatch.setattr(ws_hub, "_listener_started", True)
    asyncio.run(ws_hub._publish("t", {"text": json.loads('"\\ud83d broken"')}))
    assert len(sent) == 1 and "\\ud83d" in sent[0]
