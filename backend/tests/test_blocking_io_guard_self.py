"""Self-test for the blocking-I/O guard in test_no_blocking_io_in_async.py.

`_shielded` must treat a call inside a nested sync def as offloaded only when
that def is actually handed to an offloader; a helper the async function calls
directly still runs on the event loop.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import textwrap

_GUARD_PATH = pathlib.Path(__file__).with_name("test_no_blocking_io_in_async.py")
_spec = importlib.util.spec_from_file_location("_blocking_io_guard", _GUARD_PATH)
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)


def _requests_get_shielded(source: str) -> bool:
    tree = ast.parse(textwrap.dedent(source))
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
    async_def = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    calls = [
        n
        for n in ast.walk(async_def)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "requests.get"
    ]
    assert len(calls) == 1
    return _guard._shielded(calls[0], async_def)


def test_a_nested_helper_called_directly_on_the_loop_is_not_treated_as_offloaded():
    source = """
        async def handler():
            def _fetch():
                return requests.get("https://example.invalid")

            return _fetch()
    """
    assert _requests_get_shielded(source) is False


def test_a_nested_helper_passed_to_to_thread_is_still_offloaded():
    source = """
        async def handler():
            def _fetch():
                return requests.get("https://example.invalid")

            return await asyncio.to_thread(_fetch)
    """
    assert _requests_get_shielded(source) is True
