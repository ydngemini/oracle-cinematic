"""Which quota a request is charged against — and who shares it.

The per-principal upgrade at `rate_limit_middleware.py:353-358` only fires when
the matched bucket is exactly `"/api/"`. Every path with its *own* entry in
`RATE_LIMITS` therefore stays keyed on client IP even for a fully authenticated
user. That means `/api/ai/chat` (20/min) and `/api/crm/tour` (5/min) are shared
by everyone behind one NAT — an office of 50 agents gets 20 chat messages a
minute between them, and the 51st is told to slow down because a colleague was
typing.

`/api/generate-tour` is worse: it bypasses this middleware entirely for a
process-global counter in `server.py`, so the limit is 10/min shared across all
tenants *per replica*, enforced independently by each one.

These tests pin the current behaviour so the fix is provable, and the ones that
document the defect are marked so they are expected to be inverted.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import pytest

import rate_limit_middleware as rl


BACKEND = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Bucket resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path,expected_bucket,expected_limit",
    [
        ("/auth/login", "/auth/login", 10),
        ("/auth/register", "/auth/register", 5),
        ("/auth/session", "/auth/", 120),
        ("/api/ai/chat", "/api/ai/chat", 20),
        ("/api/ai/chat/messages", "/api/ai/chat", 20),
        ("/api/crm/tour", "/api/crm/tour", 5),
        ("/api/public/lead-intake/abc", "/api/public/lead-intake/", 30),
        ("/api/crm/clients", "/api/", 100),
    ],
)
def test_paths_resolve_to_their_declared_bucket_and_limit(path, expected_bucket, expected_limit):
    assert rl._get_bucket_for_path(path) == expected_bucket
    assert rl._get_limit_for_path(path) == expected_limit


def test_the_most_specific_prefix_wins():
    """`/api/ai/chat` must not fall through to the generic `/api/` ceiling."""
    assert rl._get_bucket_for_path("/api/ai/chat") != "/api/"
    assert rl._get_limit_for_path("/api/ai/chat") < rl._get_limit_for_path("/api/crm/leads")


# ---------------------------------------------------------------------------
# The defect: authenticated users still share an IP bucket on the hot paths
# ---------------------------------------------------------------------------

def test_the_principal_is_resolved_for_every_api_path():
    """INVERTED at P3. Previously this asserted the defect.

    `_authenticated_principal` must be reached for any `/api/` request, not only
    those falling through to the generic bucket. Parsed rather than grepped
    because the *structure* is what matters: the call has to sit under the
    `startswith("/api/")` branch, not under a `bucket == "/api/"` comparison.
    """
    # textwrap.dedent, not inspect.cleandoc — cleandoc is for docstrings and
    # leaves a method's first line unindented relative to the rest.
    source = textwrap.dedent(inspect.getsource(rl.RateLimitMiddleware.dispatch))
    tree = ast.parse(source)

    principal_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_authenticated_principal"
    ]
    assert principal_calls, "the middleware no longer resolves a principal at all"

    # No enclosing `if` of a principal call may test the bucket against "/api/";
    # that guard is exactly what made the upgrade unreachable for hot paths.
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        tests_generic_bucket = any(
            isinstance(cmp_, ast.Compare)
            and isinstance(cmp_.left, ast.Name)
            and cmp_.left.id == "bucket"
            and any(isinstance(c, ast.Constant) and c.value == "/api/" for c in cmp_.comparators)
            for cmp_ in ast.walk(node.test)
        )
        if not tests_generic_bucket:
            continue
        guarded = [
            inner
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and getattr(inner.func, "id", "") == "_authenticated_principal"
        ]
        assert not guarded, (
            "_authenticated_principal is still gated behind `bucket == '/api/'`, "
            "so paths with their own RATE_LIMITS entry stay keyed on client IP"
        )


def test_the_hot_paths_keep_their_own_limits_but_become_per_user():
    """INVERTED at P3. The limits were never wrong — the identity was.

    `/api/ai/chat` still allows 20/min and `/api/crm/tour` still allows 5/min,
    but those are now per authenticated agent rather than per source IP, so an
    office of 50 behind one NAT no longer shares one allowance.
    """
    assert rl._get_limit_for_path("/api/ai/chat") == 20
    assert rl._get_limit_for_path("/api/crm/tour") == 5
    for path in ("/api/ai/chat", "/api/crm/tour"):
        assert rl._get_bucket_for_path(path) != "/api/", (
            f"{path} must keep its own bucket — it is the limit that is specific"
        )


def test_the_authenticated_ceiling_is_higher_than_the_shared_one():
    """The upgrade exists and is generous — it is simply unreachable where it
    is most needed, which is why this is a routing bug rather than a limits one."""
    assert rl.AUTHENTICATED_API_RATE_LIMIT > rl.RATE_LIMITS["/api/"]
    assert rl.AUTHENTICATED_API_RATE_LIMIT >= 100  # its documented floor


# ---------------------------------------------------------------------------
# Dead constant
# ---------------------------------------------------------------------------

def test_burst_multiplier_is_gone():
    """Deleted at P3 rather than implemented.

    `BURST_MULTIPLIER = 1.5` was declared and referenced nowhere — a named
    constant granting no headroom is a claim the code does not honour. Kept as
    a test so it cannot be reintroduced as decoration; if a burst allowance is
    ever wanted, it has to appear in the window function, and this test should
    then assert that it does.
    """
    assert not hasattr(rl, "BURST_MULTIPLIER")

    uses = [
        path.name
        for path in BACKEND.glob("*.py")
        if "BURST_MULTIPLIER" in path.read_text(encoding="utf-8")
    ]
    assert uses == [], f"the dead constant is back in {uses}"


# ---------------------------------------------------------------------------
# The tour limiter that bypasses all of the above
# ---------------------------------------------------------------------------

def test_generate_tour_goes_through_the_shared_limiter():
    """INVERTED at P3.

    The module-global counter is gone and the path has a real bucket, so the
    ceiling is now per-principal and enforced through the same cross-replica
    store as everything else. Previously three replicas allowed 3× the intended
    rate, and one busy tenant could consume the whole allowance for everyone.
    """
    source = (BACKEND / "server.py").read_text(encoding="utf-8")

    assert "_tour_gen_timestamps" not in source, (
        "the process-global tour counter is back in server.py"
    )
    assert "/api/generate-tour" in rl.RATE_LIMITS
    assert rl._get_bucket_for_path("/api/generate-tour") == "/api/generate-tour", (
        "the path must not fall through to the generic /api/ ceiling"
    )


def test_the_tour_limit_stays_configurable():
    """`ORACLE_TOUR_RATE_LIMIT` was the knob on the old counter; moving the
    limit into RATE_LIMITS must not quietly hard-code it."""
    source = (BACKEND / "rate_limit_middleware.py").read_text(encoding="utf-8")
    assert "ORACLE_TOUR_RATE_LIMIT" in source
    assert rl._get_limit_for_path("/api/generate-tour") == 10  # documented default


# ---------------------------------------------------------------------------
# Behaviour that must survive the fix
# ---------------------------------------------------------------------------

def test_an_unparsable_token_yields_no_principal():
    """Supplying a different cookie must never mint a fresh quota."""

    class _Request:
        headers = {"authorization": "Bearer not-a-real-token"}
        cookies: dict = {}

    assert rl._authenticated_principal(_Request()) is None


def test_the_principal_is_hashed_rather_than_raw_ids():
    """Tenant and agent identifiers must stay out of Redis keys and logs."""
    source = inspect.getsource(rl._authenticated_principal)
    assert "sha256" in source
    assert "hexdigest" in source


# ---------------------------------------------------------------------------
# What P3 actually changed, exercised rather than parsed
# ---------------------------------------------------------------------------

class _Recorder:
    """Captures the (identity, bucket, limit) the middleware charges against."""

    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def __call__(self, identity, bucket, limit):
        self.calls.append((identity, bucket, limit))
        return True, 1


def _dispatch(monkeypatch, path, *, principal):
    """Drive dispatch far enough to observe the charge, with no Redis or app."""
    import asyncio

    recorder = _Recorder()
    monkeypatch.setattr(rl, "_check_rate_limit_redis", recorder)
    monkeypatch.setattr(rl, "_get_client_ip", lambda _r: "203.0.113.7")
    monkeypatch.setattr(rl, "_authenticated_principal", lambda _r: principal)

    middleware = rl.RateLimitMiddleware(app=None)
    middleware.enabled = True

    class _URL:
        def __init__(self, p):
            self.path = p

    class _Request:
        def __init__(self, p):
            self.url = _URL(p)
            self.method = "POST"
            self.scope = {"type": "http"}
            self.headers: dict = {}
            self.cookies: dict = {}

    class _Response:
        def __init__(self):
            self.headers: dict = {}

    async def _call_next(_request):
        return _Response()

    asyncio.run(middleware.dispatch(_Request(path), _call_next))
    return recorder.calls[0]


@pytest.mark.parametrize("path,expected_limit", [("/api/ai/chat", 20), ("/api/crm/tour", 5)])
def test_two_agents_on_one_ip_no_longer_share_a_hot_path_quota(monkeypatch, path, expected_limit):
    """The defect, stated as behaviour: same IP, different sessions, separate
    quotas — while the path keeps its own specific limit."""
    id_a, bucket_a, limit_a = _dispatch(monkeypatch, path, principal="principal:aaa")
    id_b, bucket_b, limit_b = _dispatch(monkeypatch, path, principal="principal:bbb")

    assert id_a != id_b, "two authenticated agents still share one rate-limit identity"
    assert id_a.startswith("principal:") and id_b.startswith("principal:")
    assert bucket_a == bucket_b == path
    assert limit_a == limit_b == expected_limit


def test_an_anonymous_caller_is_still_limited_per_ip(monkeypatch):
    """The upgrade must not become a bypass: no valid session, no principal,
    so the client IP remains the identity."""
    identity, bucket, _ = _dispatch(monkeypatch, "/api/ai/chat", principal=None)
    assert identity == "203.0.113.7"
    assert bucket == "/api/ai/chat"


def test_the_generic_bucket_still_gets_the_higher_authenticated_ceiling(monkeypatch):
    """Paths without their own entry keep the documented upgrade."""
    identity, bucket, limit = _dispatch(monkeypatch, "/api/crm/clients", principal="principal:aaa")
    assert identity == "principal:aaa"
    assert bucket == "/api/authenticated"
    assert limit == rl.AUTHENTICATED_API_RATE_LIMIT


def test_generate_tour_is_charged_per_principal(monkeypatch):
    """The whole point of removing the module-global counter."""
    id_a, bucket, limit = _dispatch(monkeypatch, "/api/generate-tour", principal="principal:aaa")
    id_b, _, _ = _dispatch(monkeypatch, "/api/generate-tour", principal="principal:bbb")

    assert bucket == "/api/generate-tour"
    assert limit == 10
    assert id_a != id_b, "tour generation is still shared between tenants"


def test_login_stays_per_ip_even_with_a_session(monkeypatch):
    """Pre-authentication endpoints must key on IP: per-principal limiting on
    login would let an attacker mint a fresh quota per guessed identity."""
    identity, bucket, limit = _dispatch(monkeypatch, "/auth/login", principal="principal:aaa")
    assert identity == "203.0.113.7"
    assert bucket == "/auth/login"
    assert limit == 10
