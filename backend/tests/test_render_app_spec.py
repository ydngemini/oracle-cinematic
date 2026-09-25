"""Staging and production are rendered from ONE spec, and differ only on purpose.

A second hand-maintained spec drifts: a worker env var lands in production and
not staging, and staging stops being a test of what production runs. These
tests pin the renderer that replaces that second copy.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location("render_app_spec", REPO / "scripts" / "render-app-spec.py")
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)

B = "sha256:" + "a" * 64
F = "sha256:" + "b" * 64


def _env(comp, key):
    return next((e.get("value") for e in comp.get("envs") or [] if e.get("key") == key), None)


def test_production_render_is_exactly_the_placeholder_substitution():
    """The YAML round-trip must change NOTHING about production. It is what CI
    and rollback.sh were already validated against."""
    raw = (REPO / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8")
    substituted = yaml.safe_load(raw.replace("__BACKEND_DIGEST__", B).replace("__FRONTEND_DIGEST__", F))
    assert r.render("production", B, F) == substituted


def test_staging_runs_every_backend_component_in_recovery_mode():
    """Reusing the DR kill switch: a staging Neoh cannot text a client, charge
    a card or send an email, whatever credentials it was handed."""
    spec = r.render("staging", B, F)
    comps = r._backend_components(spec)
    assert comps
    assert all(_env(c, "ORACLE_RECOVERY_MODE") == "1" for c in comps)
    assert all(_env(c, "ORACLE_ENV") == "staging" for c in comps)


def test_production_never_runs_in_recovery_mode():
    """It would silently refuse every outbound action — a production outage
    that reports healthy."""
    spec = r.render("production", B, F)
    assert all(_env(c, "ORACLE_RECOVERY_MODE") is None for c in r._backend_components(spec))


def test_staging_differs_from_production_only_where_intended():
    def flat(spec):
        out = {"name": spec["name"]}
        for db in spec["databases"]:
            out[f"db:{db['name']}"] = db["cluster_name"]
        for kind in ("services", "workers"):
            for c in spec.get(kind) or []:
                out[f"{c['name']}:instances"] = c.get("instance_count")
                for e in c.get("envs") or []:
                    out[f"{c['name']}:{e['key']}"] = e.get("value", e.get("type"))
        return out

    prod, stag = flat(r.render("production", B, F)), flat(r.render("staging", B, F))
    changed = {k for k in set(prod) | set(stag) if prod.get(k) != stag.get(k)}
    assert changed == {
        "name", "db:neoh-postgres", "db:neoh-redis",
        "api:instances",
        "api:ORACLE_ENV", "worker:ORACLE_ENV",
        "api:ORACLE_S3_BUCKET", "worker:ORACLE_S3_BUCKET",
        "api:ORACLE_RECOVERY_MODE", "worker:ORACLE_RECOVERY_MODE",
    }, "staging drifted from production somewhere unintended"


def test_both_environments_pin_the_same_digests():
    """Build once, promote the same artifact: nothing about the environment
    may change WHICH image runs."""
    for env in ("production", "staging"):
        spec = r.render(env, B, F)
        digests = {c["image"]["digest"] for c in r._backend_components(spec)}
        assert digests == {B}
        assert spec["static_sites"][0]["image"]["digest"] == F


@pytest.mark.parametrize("bad", ["latest", "sha256:abc", "", "neoh-backend:deadbeef"])
def test_anything_but_a_digest_is_refused(bad):
    with pytest.raises(r.RenderError):
        r.render("staging", bad, F)


def test_staging_and_production_share_no_identity():
    r.check_separation()  # raises on collision


def test_a_shared_cluster_is_caught(monkeypatch):
    """Staging pointed at the production database is a production incident."""
    monkeypatch.setitem(
        r.ENVIRONMENTS["staging"], "clusters",
        {"neoh-postgres": "neoh-postgres", "neoh-redis": "neoh-redis-staging"},
    )
    with pytest.raises(r.RenderError, match="share clusters"):
        r.check_separation()


def test_a_shared_bucket_is_caught(monkeypatch):
    monkeypatch.setitem(r.ENVIRONMENTS["staging"], "bucket", "neoh-media")
    with pytest.raises(r.RenderError, match="share buckets"):
        r.check_separation()


def test_staging_never_inherits_a_production_domain(tmp_path, monkeypatch):
    """Safe today only because the spec has no domains block. If one is ever
    added, staging must not claim the production hostname."""
    raw = yaml.safe_load((REPO / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8"))
    raw["domains"] = [{"domain": "neohrs.com", "type": "PRIMARY"}]
    fake = tmp_path / "app.yaml"
    fake.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(r, "SPEC", fake)

    assert r.render("production", B, F)["domains"][0]["domain"] == "neohrs.com"
    assert "domains" not in r.render("staging", B, F)
    r.check_separation()


def test_staging_refuses_a_live_stripe_override(tmp_path, monkeypatch):
    raw = yaml.safe_load((REPO / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8"))
    raw["services"][0]["envs"].append({"key": "ORACLE_ALLOW_LIVE_STRIPE", "value": "1"})
    fake = tmp_path / "app.yaml"
    fake.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(r, "SPEC", fake)
    with pytest.raises(r.RenderError, match="LIVE_STRIPE"):
        r.render("staging", B, F)


def test_the_cli_refuses_loudly(capsys):
    assert r.main(["--env", "staging", "--backend-digest", "latest", "--frontend-digest", F]) == 1
    assert "REFUSING TO RENDER" in capsys.readouterr().err
