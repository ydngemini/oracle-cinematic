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
                out[f"{c['name']}:size"] = c.get("instance_size_slug")
                out[f"{c['name']}:image"] = repr(c.get("image"))
                for e in c.get("envs") or []:
                    out[f"{c['name']}:{e['key']}"] = e.get("value", e.get("type"))
        for k, v in spec.items():
            if k not in ("name", "databases", "services", "workers"):
                out[f"top:{k}"] = repr(v)
        return out

    prod, stag = flat(r.render("production", B, F)), flat(r.render("staging", B, F))
    changed = {k for k in set(prod) | set(stag) if prod.get(k) != stag.get(k)}
    assert changed == {
        "name", "db:neoh-postgres", "db:neoh-redis",
        "api:ORACLE_ENV", "worker:ORACLE_ENV",
        "api:ORACLE_S3_BUCKET", "worker:ORACLE_S3_BUCKET",
        "api:ORACLE_RECOVERY_MODE", "worker:ORACLE_RECOVERY_MODE",
        # Its own JWT issuer/audience: a staging token never validates in production.
        "api:ORACLE_JWT_ISSUER", "worker:ORACLE_JWT_ISSUER",
        "api:ORACLE_JWT_AUDIENCE", "worker:ORACLE_JWT_AUDIENCE",
        # Reduced staging (owner decision 2026-10-03): capacity, never shape.
        "api:size", "worker:size", "web:instances", "web:size",
        "api:ORACLE_DB_POOL_MAX",                 # absent (default 10) -> 6
        "worker:ORACLE_DB_POOL_MAX", "worker:ORACLE_DB_PLATFORM_POOL_MAX",
    }, "staging drifted from production somewhere unintended"


def test_staging_is_the_reduced_size():
    spec = r.render("staging", B, F)
    comps = {c["name"]: c for c in spec["services"] + spec["workers"]}
    assert (comps["api"]["instance_count"], comps["api"]["instance_size_slug"]) == (2, "apps-s-1vcpu-1gb")
    assert (comps["worker"]["instance_count"], comps["worker"]["instance_size_slug"]) == (1, "apps-s-1vcpu-2gb")
    assert (comps["web"]["instance_count"], comps["web"]["instance_size_slug"]) == (1, "apps-s-1vcpu-0.5gb")
    assert _env(comps["api"], "ORACLE_DB_POOL_MAX") == "6"
    assert _env(comps["api"], "ORACLE_DB_PLATFORM_POOL_MAX") == "3"
    assert _env(comps["worker"], "ORACLE_DB_POOL_MAX") == "8"
    assert _env(comps["worker"], "ORACLE_DB_PLATFORM_POOL_MAX") == "4"


def test_staging_pools_fit_a_2_gib_cluster_with_headroom():
    """2 x (6+1+3) + (8+1+4) = 33 of the 47 a db-s-1vcpu-2gb allows."""
    assert r.connection_demand(r.render("staging", B, F)) == 33


def test_staging_pools_over_budget_are_refused(monkeypatch):
    sizing = {**r.ENVIRONMENTS["staging"]["components"]}
    sizing["worker"] = {**sizing["worker"], "envs": {"ORACLE_DB_POOL_MAX": "20",
                                                     "ORACLE_DB_PLATFORM_POOL_MAX": "4"}}
    monkeypatch.setitem(r.ENVIRONMENTS["staging"], "components", sizing)
    with pytest.raises(r.RenderError, match="Postgres connections"):
        r.render("staging", B, F)


def test_staging_api_below_two_replicas_is_refused(monkeypatch):
    """One replica never exercises cross-replica WebSocket fan-out."""
    sizing = {**r.ENVIRONMENTS["staging"]["components"]}
    sizing["api"] = {**sizing["api"], "instance_count": 1}
    monkeypatch.setitem(r.ENVIRONMENTS["staging"], "components", sizing)
    with pytest.raises(r.RenderError, match="at least 2 instances"):
        r.render("staging", B, F)


def test_both_environments_pin_the_same_digests():
    """Build once, promote the same artifact: nothing about the environment
    may change WHICH image runs."""
    for env in ("production", "staging"):
        spec = r.render(env, B, F)
        digests = {c["image"]["digest"] for c in r._backend_components(spec)}
        assert digests == {B}
        assert [c["image"]["digest"] for c in r._frontend_components(spec)] == [F]


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


def test_every_backend_component_carries_the_jwt_pair():
    """Outside dev the backend refuses to boot without ORACLE_JWT_ISSUER and
    ORACLE_JWT_AUDIENCE. The first staging bring-up found the spec had neither."""
    for env in ("production", "staging"):
        for comp in r._backend_components(r.render(env, B, F)):
            assert _env(comp, "ORACLE_JWT_ISSUER") and _env(comp, "ORACLE_JWT_AUDIENCE"), (env, comp["name"])


def test_a_component_without_the_jwt_pair_is_refused(tmp_path, monkeypatch):
    raw = yaml.safe_load((REPO / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8"))
    raw["workers"][0]["envs"] = [e for e in raw["workers"][0]["envs"] if e["key"] != "ORACLE_JWT_AUDIENCE"]
    fake = tmp_path / "app.yaml"
    fake.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(r, "SPEC", fake)
    with pytest.raises(r.RenderError, match="ORACLE_JWT_AUDIENCE"):
        r.render("production", B, F)


def test_a_shared_jwt_issuer_is_caught(monkeypatch):
    """Same issuer AND same audience would let a staging token pass production's
    claim checks; either one shared is refused."""
    monkeypatch.setitem(r.ENVIRONMENTS["staging"], "jwt",
                        {"ORACLE_JWT_ISSUER": "neoh", "ORACLE_JWT_AUDIENCE": "neoh-staging"})
    with pytest.raises(r.RenderError, match="share jwt_issuers"):
        r.check_separation()


SERVICE_ONLY = ("http_port", "internal_ports", "health_check", "routes", "cors")


def test_workers_carry_no_service_only_fields():
    """`doctl apps spec validate` rejected the first real staging spec:
    `unknown field "drain_seconds"` on the worker. Checked offline here."""
    for env in ("production", "staging"):
        for w in r.render(env, B, F).get("workers") or []:
            assert not [f for f in SERVICE_ONLY if f in w], w["name"]
            assert "drain_seconds" not in (w.get("termination") or {}), w["name"]
            assert 1 <= (w.get("termination") or {}).get("grace_period_seconds", 120) <= 600


def test_no_component_scales_a_single_instance_size():
    """App Platform rejected production's 2x apps-s-1vcpu-0.5gb web."""
    for env in ("production", "staging"):
        spec = r.render(env, B, F)
        for c in spec["services"] + spec["workers"]:
            if c.get("instance_size_slug") in r.SINGLE_INSTANCE_SLUGS:
                assert int(c.get("instance_count") or 1) == 1, (env, c["name"])


def test_scaling_a_single_instance_size_is_refused(monkeypatch):
    sizing = {**r.ENVIRONMENTS["staging"]["components"]}
    sizing["web"] = {"instance_count": 2, "instance_size_slug": "apps-s-1vcpu-0.5gb"}
    monkeypatch.setitem(r.ENVIRONMENTS["staging"], "components", sizing)
    with pytest.raises(r.RenderError, match="only 1 instance"):
        r.render("staging", B, F)


def test_a_worker_with_drain_seconds_is_refused(tmp_path, monkeypatch):
    raw = yaml.safe_load((REPO / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8"))
    raw["workers"][0].setdefault("termination", {})["drain_seconds"] = 30
    fake = tmp_path / "app.yaml"
    fake.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(r, "SPEC", fake)
    with pytest.raises(r.RenderError, match="drain_seconds"):
        r.render("staging", B, F)


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
