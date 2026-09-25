"""Provider-registered callback URLs stay mounted and reachable.

A release that moves or unmounts one fails silently: the provider gets errors,
we get no exception, and calls, payments or messages quietly stop arriving.
This is exactly how every outbound Plivo call came to fetch a 404 for its
instructions (tests/test_plivo_outbound_webhooks.py).

The list lives in infra/digitalocean/callback-routes.txt, shared with the
release smoke test, which checks the same URLs on the deployed app.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
LIST = REPO / "infra" / "digitalocean" / "callback-routes.txt"


def _entries():
    out = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        method, path, status, provider = line.split()
        out.append((method, path, int(status), provider))
    return out


ENTRIES = _entries()


def test_the_list_covers_every_provider_that_calls_us():
    providers = {e[3] for e in ENTRIES}
    assert {"stripe", "plivo", "telnyx", "twilio", "google-oauth"} <= providers


@pytest.mark.parametrize("method, path, status, provider", ENTRIES,
                         ids=[f"{e[3]}:{e[1]}" for e in ENTRIES])
def test_each_callback_is_mounted(method, path, status, provider):
    import server

    for route in server.app.routes:
        rx = "^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(getattr(route, "path", "") or "")) + "$"
        if re.match(rx, path) and method in (getattr(route, "methods", None) or set()):
            return
    pytest.fail(f"{provider} is registered to call {method} {path}, and no route serves it")


@pytest.mark.parametrize("method, path, status, provider",
                         [e for e in ENTRIES if e[0] == "POST"],
                         ids=[f"{e[3]}:{e[1]}" for e in ENTRIES if e[0] == "POST"])
def test_each_posted_callback_is_csrf_exempt(method, path, status, provider):
    """A provider has no session and no CSRF token. A webhook that is mounted
    but not exempt answers every real delivery with 403."""
    from csrf_middleware import CSRFMiddleware

    assert CSRFMiddleware(app=None)._is_exempt(path), (
        f"{provider}'s {path} is not CSRF-exempt — every delivery would get 403"
    )


def test_the_smoke_test_reads_this_list():
    smoke = (REPO / "infra" / "digitalocean" / "smoke-test.sh").read_text(encoding="utf-8")
    assert "callback-routes.txt" in smoke
