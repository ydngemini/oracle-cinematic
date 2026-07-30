import asyncio
import importlib
import sys
from pathlib import Path


_BACKEND = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _BACKEND)
_previous_server = sys.modules.pop("server", None)
try:
    server = importlib.import_module("server")
finally:
    sys.modules.pop("server", None)
    if _previous_server is not None:
        sys.modules["server"] = _previous_server


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, result=1, error=None):
        self.result = result
        self.error = error

    def acquire(self):
        pool = self

        class _Conn:
            async def fetchval(self, query):
                assert query == "SELECT 1"
                if pool.error:
                    raise pool.error
                return pool.result

        return _Acquire(_Conn())


def test_health_requires_a_successful_database_round_trip(monkeypatch):
    monkeypatch.setattr(server, "get_pool", lambda: _Pool(result=1))
    monkeypatch.setattr(server, "pool_stats", lambda: {"size": 2, "idle": 1})

    response = asyncio.run(server.health())

    assert response.status_code == 200
    assert b'"reachable":true' in response.body


def test_health_returns_503_when_pool_exists_but_database_is_unreachable(monkeypatch):
    monkeypatch.setattr(server, "get_pool", lambda: _Pool(error=OSError("offline")))
    monkeypatch.setattr(server, "pool_stats", lambda: {"size": 2, "idle": 1})

    response = asyncio.run(server.health())

    assert response.status_code == 503
    assert b'"reachable":false' in response.body
    assert b"offline" not in response.body


def test_health_returns_503_before_pool_initialization(monkeypatch):
    monkeypatch.setattr(server, "get_pool", lambda: None)
    monkeypatch.setattr(server, "pool_stats", lambda: {})

    response = asyncio.run(server.health())

    assert response.status_code == 503
    assert b"pool not initialised" in response.body
