"""The web tier's Content-Security-Policy (oracle-app/nginx.conf).

Pinned after the first OWASP ZAP baseline against staging (2026-10-06):
`media-src … https:` allowed audio/video from ANY https origin (ZAP 10055),
though every Neoh media URL is same-origin (/api/media/<id>), and connect-src
still allowed the retired Azure domain neohrs.com — a domain that, once
lapsed, anyone could register and the app would happily talk to.
"""

from __future__ import annotations

import re
from pathlib import Path

NGINX = Path(__file__).resolve().parents[2] / "oracle-app" / "nginx.conf"


def _policies() -> list[dict[str, list[str]]]:
    found = re.findall(r'add_header Content-Security-Policy "([^"]+)"', NGINX.read_text())
    assert found, "no CSP in nginx.conf"
    out = []
    for policy in found:
        directives = {}
        for part in policy.split(";"):
            tokens = part.split()
            if tokens:
                directives[tokens[0]] = tokens[1:]
        out.append(directives)
    return out


def test_every_location_sends_the_same_policy():
    policies = _policies()
    assert all(p == policies[0] for p in policies)


def test_no_directive_allows_a_whole_scheme():
    for directives in _policies():
        for name, sources in directives.items():
            for source in sources:
                assert source not in ("https:", "http:", "wss:", "ws:", "*"), (name, source)


def test_media_is_same_origin_only():
    for directives in _policies():
        assert set(directives["media-src"]) <= {"'self'", "blob:"}


def test_no_retired_domain_is_trusted():
    assert "neohrs.com" not in NGINX.read_text()


def test_the_3d_viewer_may_fetch_its_own_blob_urls():
    """The Space viewer downloads the (authenticated) .sog, hands PlayCanvas a
    blob: URL, and PlayCanvas fetch()es it. Without blob: in connect-src the
    Space never loaded on staging — every rehearsal since 2026-10-04 showed
    "could not be loaded" behind a passing check. blob: URLs are minted by this
    origin only, so this opens nothing to other sites."""
    for directives in _policies():
        assert "blob:" in directives["connect-src"]


def test_scripts_and_objects_stay_locked_down():
    for directives in _policies():
        assert directives["object-src"] == ["'none'"]
        assert directives["frame-ancestors"] == ["'none'"]
        assert "'unsafe-inline'" not in directives["script-src"]
        assert "'unsafe-eval'" not in directives["script-src"]
