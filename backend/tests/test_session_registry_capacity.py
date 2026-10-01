"""The in-process session registry must never refuse a sign-in.

It exists for the admin "who is signed in" view. It used to raise 503 once it
held 100 agents — per process — which capped the whole platform at roughly
100 signed-in agents per API replica (Mission 8 capacity audit).
"""

import auth


def test_a_full_registry_evicts_instead_of_refusing(monkeypatch):
    monkeypatch.setattr(auth, "MAX_SESSIONS", 3)
    auth._session_registry.clear()
    for agent in ("a", "b", "c", "d", "e"):
        auth._register_session(agent)  # must not raise
    assert list(auth._session_registry) == ["c", "d", "e"]


def test_re_signing_in_refreshes_rather_than_duplicating(monkeypatch):
    monkeypatch.setattr(auth, "MAX_SESSIONS", 3)
    auth._session_registry.clear()
    for agent in ("a", "b", "c"):
        auth._register_session(agent)
    auth._register_session("a")          # a is now the newest
    auth._register_session("d")          # so b, the stalest, is evicted
    assert list(auth._session_registry) == ["c", "a", "d"]


def test_default_bound_is_not_a_launch_size_limit():
    # A first-customer platform has hundreds of agents; the bound is memory
    # hygiene (a few hundred bytes per entry), not admission control.
    assert auth.MAX_SESSIONS >= 5000
