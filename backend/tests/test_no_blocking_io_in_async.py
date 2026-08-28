"""No synchronous network or process call may sit on the event loop.

FastAPI runs `async def` handlers on a single event loop per worker, so one
blocking call freezes every concurrent request in that process for its full
duration. Two had crept in:

  * `property_view_api._put_video_to_storage` ran a boto3 PUT of up to 512 MB
    inline, from the unauthenticated public upload link, inside an open
    transaction holding a FOR UPDATE lock.
  * `ai_chat_agent._web_search` ran a Tavily request with a 15-second timeout.

The codebase is otherwise disciplined about this — the usual shape is a nested
sync `_blocking()` handed to `asyncio.to_thread`. That convention is exactly
what makes a lapse hard to spot in review, so it is asserted here instead.
"""

from __future__ import annotations

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]

_BLOCKING_CALLS = {
    "requests.get", "requests.post", "requests.put", "requests.delete",
    "requests.request", "requests.head", "requests.patch",
    "time.sleep", "subprocess.run", "subprocess.call", "subprocess.check_output",
    "urllib.request.urlopen", "httpx.get", "httpx.post",
}
# Attribute names that mean real I/O regardless of the module they hang off.
_BLOCKING_METHODS = {
    "put_bytes", "put_file", "upload_file", "download_file",
    "get_object", "put_object",
}
_OFFLOADERS = ("to_thread", "run_in_threadpool", "run_in_executor")

_SKIP_DIRS = {"venv", "tests", "node_modules", "ml_forge", "__pycache__"}


def _shielded(node, async_def) -> bool:
    """True when the call is offloaded rather than run on the loop.

    Either it sits inside a nested sync def / lambda — the standard offload
    target, which some other line hands to a thread — or inside a to_thread
    call directly.
    """
    current = getattr(node, "parent", None)
    while current is not None and current is not async_def:
        if isinstance(current, (ast.FunctionDef, ast.Lambda)):
            return True
        if isinstance(current, ast.Call):
            if any(word in ast.unparse(current.func) for word in _OFFLOADERS):
                return True
        current = getattr(current, "parent", None)
    return False


def _sources():
    for path in BACKEND.rglob("*.py"):
        if _SKIP_DIRS & set(path.relative_to(BACKEND).parts):
            continue
        yield path


def test_no_blocking_call_runs_on_the_event_loop():
    offenders: list[str] = []

    for path in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = ast.unparse(call.func)
                if _shielded(call, node):
                    continue
                if isinstance(getattr(call, "parent", None), ast.Await):
                    continue
                if name in _BLOCKING_CALLS or (
                    "." in name and name.rsplit(".", 1)[-1] in _BLOCKING_METHODS
                ):
                    offenders.append(
                        f"{path.relative_to(BACKEND)}:{call.lineno} "
                        f"in async {node.name}() -> {name}"
                    )

    assert offenders == [], (
        "Blocking I/O on the event loop freezes every concurrent request in the "
        "worker. Wrap it: `await asyncio.to_thread(...)`.\n  "
        + "\n  ".join(sorted(set(offenders)))
    )
