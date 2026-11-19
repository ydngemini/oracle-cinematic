"""The in-process session registry must never refuse a sign-in.

It exists for the admin "recent sign-ins on this server" view (28.7). It used to raise 503 once it
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


def test_the_inventory_reports_the_sign_in_s_own_tenant_and_role():
    """Audit 28.7: a database-backed broker owner was reported as an 'agent'
    of a tenant named after their email (the DEMO_TENANCY fallback)."""
    auth._session_registry.clear()
    auth._register_session("owner@brokerage.test", "11111111-1111-4111-8111-111111111111", "broker_owner")
    [entry] = auth.active_sessions()
    assert (entry["tenant_id"], entry["role"]) == ("11111111-1111-4111-8111-111111111111", "broker_owner")


def test_an_unknown_role_is_not_guessed():
    auth._session_registry.clear()
    auth._register_session("someone@x.test")
    [entry] = auth.active_sessions()
    assert entry["role"] is None and entry["tenant_id"] is None


def test_every_operator_surface_says_what_the_inventory_is():
    import pathlib

    source = (pathlib.Path(auth.__file__).parent / "admin_ops.py").read_text()
    assert source.count('"sessions_scope": SESSION_INVENTORY_SCOPE') == 3
    assert "this server only" in auth.SESSION_INVENTORY_SCOPE
