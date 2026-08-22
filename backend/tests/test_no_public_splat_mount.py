"""Reconstructions must not be readable without authentication.

`/public/splats` was a StaticFiles mount over the directory the reconstruction
pipeline wrote to, and the filenames were derived from `sha256(address)[:16]`.
Anyone who knew a street address could compute the path and download that
property's 3D reconstruction — across tenants, with no credential at all.

The mount is gone and splats are served by /api/media/{id}, which joins through
the tenant-scoped property_media row. These tests exist so neither half can come
back quietly: a convenience mount is a very easy thing to re-add.
"""

from __future__ import annotations

import pytest

from tests.conftest import *  # noqa: F401,F403 — env bootstrap before `server`


def _app():
    import server

    return server.app


def test_no_route_serves_reconstructions_without_auth():
    mounted = [getattr(route, "path", "") for route in _app().routes]

    assert not any("splat" in path.lower() for path in mounted), (
        f"a splat path is mounted again: "
        f"{[p for p in mounted if 'splat' in p.lower()]}"
    )


def test_no_static_file_mount_exists_at_all():
    """StaticFiles bypasses every dependency, so any mount is unauthenticated.

    There is no legitimate static mount in this API — the frontend is served
    separately — so the absence of the class is easier to hold than a per-path
    allowlist.
    """
    from starlette.staticfiles import StaticFiles

    static_mounts = [
        getattr(route, "path", "?")
        for route in _app().routes
        if isinstance(getattr(route, "app", None), StaticFiles)
    ]

    assert static_mounts == [], (
        f"unauthenticated StaticFiles mount(s) present: {static_mounts}"
    )


def test_the_scraping_reconstruction_module_is_gone():
    """spatial_agent ran a second pipeline that scraped listing-site images for
    an arbitrary address and wrote the result to the public directory above."""
    with pytest.raises(ImportError):
        import spatial_agent  # noqa: F401


def test_the_websocket_cannot_start_a_reconstruction_by_address():
    """The trigger took an address off a WS message and reconstructed it.

    Capture now begins only through POST /api/crm/reconstruction-jobs, which is
    authenticated and checks the subject belongs to the caller's tenant.
    """
    import inspect

    import server

    source = inspect.getsource(server)
    assert "REQUEST_RECONSTRUCTION" not in source


def test_the_stub_provider_is_refused_outside_development(monkeypatch):
    """Its output is a generated room stored as ordinary media."""
    import config
    import reconstruction_providers

    monkeypatch.setenv("RECONSTRUCTION_PROVIDER", "stub")
    monkeypatch.setattr(config, "IS_DEV", False)

    provider = reconstruction_providers.get_provider()
    ready, reason = provider.available()

    assert ready is False
    assert "stub" in reason.lower()


def test_the_stub_provider_still_works_in_development(monkeypatch):
    """It is genuinely useful: the only way to exercise the viewer without a GPU."""
    import config
    import reconstruction_providers

    monkeypatch.setenv("RECONSTRUCTION_PROVIDER", "stub")
    monkeypatch.setattr(config, "IS_DEV", True)

    provider = reconstruction_providers.get_provider()

    assert provider.available()[0] is True
    assert provider.produces == "synthetic", "and it must still declare itself"
