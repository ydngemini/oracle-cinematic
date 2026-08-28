"""Large uploads must not run their S3 PUT on the event loop.

`media_storage.put_media_bytes` offloads with `asyncio.to_thread` and says why:
`object_storage.put_bytes` is synchronous and does real network I/O. The video
path called that same function directly from two async endpoints, so one upload
froze every other request in the worker for the whole PUT — while holding an
open transaction and a FOR UPDATE lock on the link row.

The asymmetry is what makes it worth a test: photos are capped at 25 MB and were
already offloaded; videos are capped at 512 MB and were not, and one of the two
callers is the unauthenticated public client-upload link.
"""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

import property_view_api


def test_the_video_helper_is_awaitable():
    assert inspect.iscoroutinefunction(property_view_api._put_video_to_storage)


def test_the_blocking_put_runs_off_the_event_loop(monkeypatch):
    """The point is the thread, not the return value."""
    calling_thread: dict[str, int] = {}

    class _FakeStorage:
        @staticmethod
        def put_bytes(key, data, content_type):
            calling_thread["id"] = threading.get_ident()
            return key

    monkeypatch.setitem(
        __import__("sys").modules, "object_storage", _FakeStorage
    )

    async def _run():
        loop_thread = threading.get_ident()
        key = await property_view_api._put_video_to_storage(
            b"\x00" * 1024, "video/mp4", "11111111-1111-1111-1111-111111111111"
        )
        return loop_thread, key

    loop_thread, key = asyncio.run(_run())

    assert calling_thread["id"] != loop_thread, (
        "put_bytes ran on the event loop thread — a 512 MB upload would block "
        "every other request in this worker"
    )
    assert key.startswith("property-view/11111111-1111-1111-1111-111111111111/")


def test_both_call_sites_await_it():
    """A missed `await` here returns a coroutine that is silently stored as s3_key."""
    import ast

    tree = ast.parse(inspect.getsource(property_view_api))
    # ast does not link parents, so attach them before asking "is this awaited?".
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
    unawaited = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_put_video_to_storage"
        and not isinstance(node.parent, ast.Await)
    ]
    assert unawaited == [], f"_put_video_to_storage called without await at {unawaited}"
