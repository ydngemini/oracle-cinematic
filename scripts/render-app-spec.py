#!/usr/bin/env python3
"""Render the App Platform spec for one environment, pinned to one release.

There is ONE spec, `infra/digitalocean/app.yaml`. Production and staging are
both rendered from it, never hand-maintained side by side. A second copy of a
300-line spec drifts — somebody adds a worker env var to production and not to
staging, and staging stops being a test of what production will run. Rendering
makes "structurally identical except where it must differ" a property of the
code rather than a hope.

What differs, and nothing else:

    app name              neoh              neoh-staging
    Postgres cluster      neoh-postgres     neoh-postgres-staging
    Valkey cluster        neoh-redis        neoh-redis-staging
    Spaces bucket         neoh-media        neoh-media-staging
    ORACLE_ENV            prod              staging
    ORACLE_RECOVERY_MODE  absent            "1" on every backend component
    ORACLE_JWT_ISSUER/    neoh / neoh       neoh-staging / neoh-staging
      _AUDIENCE
    component sizing      (from spec)       reduced — see below
    DB pool ceilings      (from spec)       sized for a 2 GiB cluster

Staging is the REDUCED size, not production parity (owner decision,
2026-10-03): ~$109/month instead of ~$240. What is reduced is capacity, never
shape — the same three components, the same images, the same envs:

    component  production                  staging
    api        2 x apps-s-2vcpu-4gb        2 x apps-s-1vcpu-1gb
    worker     1 x apps-s-2vcpu-4gb        1 x apps-s-1vcpu-2gb
    web        2 x apps-s-1vcpu-1gb        1 x apps-s-1vcpu-0.5gb

The api stays at TWO instances on purpose: cross-replica WebSocket fan-out
(Postgres LISTEN/NOTIFY) is only exercised with more than one replica, so a
one-replica staging would pass releases that break it. The render refuses a
staging api below 2. Measured backend RSS is 255-561 MiB per replica
(docs/capacity-plan.md), which fits 1 GiB.

Pools are cut to fit a 2 GiB Managed Postgres (47 usable connections,
docs/database-connection-budget.md). Each process opens POOL_MAX + 1 (the
ws_hub LISTEN connection) + PLATFORM_POOL_MAX:

    api     2 x (6 + 1 + 3) = 20
    worker  1 x (8 + 1 + 4) = 13
    total                     33 of 47 — headroom for migrations and admin

The render refuses a staging spec whose total leaves less than 10 free.

Staging runs with ORACLE_RECOVERY_MODE on. That is the disaster-recovery kill
switch, reused deliberately: it blocks every provider egress method, SMTP, both
Stripe mutations and the scheduler. A staging instance is a complete Neoh with
real-looking config, and the whole point of the switch is that such an instance
cannot text a client, charge a card, or email anyone. A second, independent
layer already exists in the backend: with ORACLE_ENV=staging a live Stripe key
refuses to boot (billing.py).

The render FAILS rather than emitting anything unsafe:

  * a staging spec without recovery mode on every backend component
  * a production spec WITH recovery mode (production would silently do nothing)
  * staging and production sharing any identity: app, clusters, bucket,
    domain, JWT issuer or audience
  * a backend component without the JWT issuer/audience pair (it cannot boot)
  * a worker carrying a service-only field (drain_seconds, http_port,
    health_check, routes) — App Platform rejects the whole spec
  * a digest that is not sha256:…, or a placeholder surviving substitution
  * ORACLE_ALLOW_LIVE_STRIPE anywhere in staging

Usage:
  scripts/render-app-spec.py --env staging \\
      --backend-digest sha256:… --frontend-digest sha256:… > app.staging.yaml
  scripts/render-app-spec.py --check      # validate both envs, no output
"""

from __future__ import annotations

import argparse
import copy
import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
SPEC = REPO / "infra" / "digitalocean" / "app.yaml"

BACKEND_PLACEHOLDER = "__BACKEND_DIGEST__"
FRONTEND_PLACEHOLDER = "__FRONTEND_DIGEST__"

ENVIRONMENTS = {
    "production": {
        "name": "neoh",
        "clusters": {"neoh-postgres": "neoh-postgres", "neoh-redis": "neoh-redis"},
        "bucket": "neoh-media",
        "oracle_env": "prod",
        "jwt": None,                    # keep the spec's pair ("neoh")
        "components": {},               # production keeps the spec's values exactly
        "recovery_mode": False,
    },
    "staging": {
        "name": "neoh-staging",
        "clusters": {"neoh-postgres": "neoh-postgres-staging",
                     "neoh-redis": "neoh-redis-staging"},
        "bucket": "neoh-media-staging",
        "oracle_env": "staging",
        # Its own issuer/audience: a token minted by staging must never be
        # accepted by production (check_separation refuses a shared pair).
        "jwt": {"ORACLE_JWT_ISSUER": "neoh-staging", "ORACLE_JWT_AUDIENCE": "neoh-staging"},
        "components": {
            "api": {"instance_count": 2, "instance_size_slug": "apps-s-1vcpu-1gb",
                    "envs": {"ORACLE_DB_POOL_MAX": "6", "ORACLE_DB_PLATFORM_POOL_MAX": "3"}},
            "worker": {"instance_count": 1, "instance_size_slug": "apps-s-1vcpu-2gb",
                       "envs": {"ORACLE_DB_POOL_MAX": "8", "ORACLE_DB_PLATFORM_POOL_MAX": "4"}},
            "web": {"instance_count": 1, "instance_size_slug": "apps-s-1vcpu-0.5gb"},
        },
        "recovery_mode": True,
        # db-s-1vcpu-2gb: 25 per GiB minus 3 reserved (database-connection-budget.md).
        "db_connection_budget": 47,
    },
}

# db/connection.py defaults, used when a component sets no explicit value.
_DEFAULT_POOL_MAX = 10
_DEFAULT_PLATFORM_POOL_MAX = 4
_LISTENER_RESERVED = 1   # ws_hub's permanent LISTEN connection
# Connections a budget must leave free for migrations and the admin console,
# beyond the 3 DigitalOcean already reserves for its own maintenance.
_BUDGET_HEADROOM = 10

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class RenderError(ValueError):
    pass


# The SPA runs as a service (its own nginx, so its response headers apply —
# review WEB-3); it is not a backend and takes none of the backend envs.
FRONTEND_COMPONENTS = frozenset({"web"})


def _backend_components(spec: dict) -> list[dict]:
    return [c for c in list(spec.get("services") or []) + list(spec.get("workers") or [])
            if c.get("name") not in FRONTEND_COMPONENTS]


def _frontend_components(spec: dict) -> list[dict]:
    return [c for c in spec.get("services") or [] if c.get("name") in FRONTEND_COMPONENTS] \
        + list(spec.get("static_sites") or [])


def _set_env(component: dict, key: str, value: str) -> None:
    envs = component.setdefault("envs", [])
    for item in envs:
        if item.get("key") == key:
            item["value"] = value
            item.pop("type", None)
            return
    envs.append({"key": key, "value": value})


def _env_value(component: dict, key: str):
    for item in component.get("envs") or []:
        if item.get("key") == key:
            return item.get("value")
    return None


def render(env: str, backend_digest: str, frontend_digest: str,
           domain: str | None = None) -> dict:
    if env not in ENVIRONMENTS:
        raise RenderError(f"unknown environment {env!r}; use {sorted(ENVIRONMENTS)}")
    for label, digest in (("backend", backend_digest), ("frontend", frontend_digest)):
        if not _DIGEST.match(digest or ""):
            raise RenderError(
                f"{label} digest {digest!r} is not sha256:<64 hex>. A tag is a mutable "
                f"pointer; only a digest names the artifact that was tested."
            )

    cfg = ENVIRONMENTS[env]
    spec = copy.deepcopy(yaml.safe_load(SPEC.read_text(encoding="utf-8")))

    spec["name"] = cfg["name"]
    # Domains are attached out-of-band today, so the spec carries none and each
    # app answers on its own ${APP_URL}. If a `domains:` block is ever added, a
    # staging render would otherwise claim the PRODUCTION hostname — so staging
    # never inherits one. Give staging its own domain explicitly if it needs one.
    if env != "production":
        spec.pop("domains", None)

    # The domain comes from per-environment CONFIG (the NEOH_DOMAIN variable
    # on each GitHub environment), not from a click in the DigitalOcean
    # console. `doctl apps update --spec` applies the WHOLE desired state, so a
    # domain attached in the console and absent from the spec is detached by
    # the next deploy — the public URL every provider calls stops resolving.
    # spec-drift.py refuses that; this is how it is avoided.
    if domain:
        if not re.fullmatch(r"(?=.{1,253}$)([a-z0-9-]+\.)+[a-z]{2,}", domain):
            raise RenderError(f"{domain!r} is not a hostname")
        spec["domains"] = [{"domain": domain, "type": "PRIMARY"}]
    for db in spec.get("databases") or []:
        if db.get("name") in cfg["clusters"]:
            db["cluster_name"] = cfg["clusters"][db["name"]]

    for comp in _backend_components(spec):
        _set_env(comp, "ORACLE_ENV", cfg["oracle_env"])
        if _env_value(comp, "ORACLE_S3_BUCKET") is not None:
            _set_env(comp, "ORACLE_S3_BUCKET", cfg["bucket"])
        if cfg["recovery_mode"]:
            _set_env(comp, "ORACLE_RECOVERY_MODE", "1")
        for key, value in (cfg.get("jwt") or {}).items():
            _set_env(comp, key, value)
    by_name = {c.get("name"): c for c in list(spec.get("services") or [])
               + list(spec.get("workers") or [])}
    for name, override in cfg["components"].items():
        comp = by_name.get(name)
        if comp is None:
            raise RenderError(f"{env} sizing names component {name!r}, which the spec does not have")
        for field in ("instance_count", "instance_size_slug"):
            if field in override:
                comp[field] = override[field]
        for key, value in (override.get("envs") or {}).items():
            _set_env(comp, key, value)

    for comp in _backend_components(spec) + _frontend_components(spec):
        image = comp.get("image") or {}
        if image.get("digest") == BACKEND_PLACEHOLDER:
            image["digest"] = backend_digest
        elif image.get("digest") == FRONTEND_PLACEHOLDER:
            image["digest"] = frontend_digest

    validate(env, spec)
    return spec


def _worker_service_only(comp: dict) -> list[str]:
    """Fields App Platform accepts on services but rejects on workers."""
    bad = [f for f in ("http_port", "internal_ports", "health_check", "routes", "cors")
           if f in comp]
    if "drain_seconds" in (comp.get("termination") or {}):
        bad.append("termination.drain_seconds")
    return bad


# (spec section, check) — found by `doctl apps spec validate` on the first
# real staging bring-up; kept offline so it can never ship again.
SERVICE_ONLY_FIELDS = (("workers", _worker_service_only),)

# Sizes App Platform refuses to run more than one instance of ("must not exceed
# 1 instance for instance_size_slug …", `doctl apps spec validate`, 2026-10-03).
SINGLE_INSTANCE_SLUGS = frozenset({"apps-s-1vcpu-0.5gb", "apps-s-1vcpu-1gb-fixed"})


def validate(env: str, spec: dict) -> None:
    """Refuse anything unsafe. Called on every render."""
    text = yaml.safe_dump(spec)
    if BACKEND_PLACEHOLDER in text or FRONTEND_PLACEHOLDER in text:
        raise RenderError("a digest placeholder survived substitution")

    backend = _backend_components(spec)
    if not backend:
        raise RenderError("the spec has no backend components")

    for kind, field in SERVICE_ONLY_FIELDS:
        for comp in spec.get(kind) or []:
            bad = field(comp)
            if bad:
                raise RenderError(
                    f"{kind[:-1]} {comp.get('name')!r} has service-only field(s) {bad}; "
                    f"App Platform rejects the whole spec"
                )

    for comp in list(spec.get("services") or []) + list(spec.get("workers") or []):
        if comp.get("instance_size_slug") in SINGLE_INSTANCE_SLUGS and int(comp.get("instance_count") or 1) > 1:
            raise RenderError(
                f"{comp.get('name')!r}: App Platform allows only 1 instance of "
                f"{comp.get('instance_size_slug')} — use apps-s-1vcpu-1gb or larger to scale"
            )

    for comp in backend:
        for key in ("ORACLE_JWT_ISSUER", "ORACLE_JWT_AUDIENCE"):
            if not str(_env_value(comp, key) or "").strip():
                raise RenderError(
                    f"{comp.get('name')!r} lacks {key}; outside dev the backend refuses to boot "
                    f"without the issuer/audience pair (config._REQUIRED_IN_PROD)"
                )
        rm = str(_env_value(comp, "ORACLE_RECOVERY_MODE") or "")
        if env == "staging" and rm != "1":
            raise RenderError(
                f"staging component {comp.get('name')!r} lacks ORACLE_RECOVERY_MODE=1 — "
                f"it could text real clients, charge real cards, send real email"
            )
        if env == "production" and rm:
            raise RenderError(
                f"production component {comp.get('name')!r} has ORACLE_RECOVERY_MODE set — "
                f"production would silently refuse every outbound action"
            )
        if env == "staging" and _env_value(comp, "ORACLE_ALLOW_LIVE_STRIPE") is not None:
            raise RenderError("staging must never set ORACLE_ALLOW_LIVE_STRIPE")

    if env == "staging":
        api = next((c for c in spec.get("services") or [] if c.get("name") == "api"), None)
        if api is None or int(api.get("instance_count") or 1) < 2:
            raise RenderError(
                "staging api must run at least 2 instances — with one, cross-replica "
                "WebSocket fan-out is never exercised and a release that breaks it passes"
            )

    budget = (ENVIRONMENTS.get(env) or {}).get("db_connection_budget")
    if budget:
        need = connection_demand(spec)
        if need + _BUDGET_HEADROOM > budget:
            raise RenderError(
                f"{env} components can open {need} Postgres connections; the cluster allows "
                f"{budget} and {_BUDGET_HEADROOM} must stay free for migrations and admin "
                f"(docs/database-connection-budget.md)"
            )


def connection_demand(spec: dict) -> int:
    """Postgres connections every backend instance can hold at once:
    (POOL_MAX + the LISTEN connection + PLATFORM_POOL_MAX) x instance_count."""
    total = 0
    for comp in _backend_components(spec):
        pool = int(_env_value(comp, "ORACLE_DB_POOL_MAX") or _DEFAULT_POOL_MAX)
        platform = int(_env_value(comp, "ORACLE_DB_PLATFORM_POOL_MAX") or _DEFAULT_PLATFORM_POOL_MAX)
        total += int(comp.get("instance_count") or 1) * (pool + _LISTENER_RESERVED + platform)
    return total


def identities(spec: dict) -> dict:
    """The values that must NEVER be shared between environments."""
    buckets = {
        _env_value(c, "ORACLE_S3_BUCKET") for c in _backend_components(spec)
    } - {None}
    return {
        "domains": {d.get("domain") for d in spec.get("domains") or []} - {None},
        "app": {spec.get("name")},
        "clusters": {db.get("cluster_name") for db in spec.get("databases") or []},
        "buckets": buckets,
        "jwt_issuers": {_env_value(c, "ORACLE_JWT_ISSUER") for c in _backend_components(spec)} - {None},
        "jwt_audiences": {_env_value(c, "ORACLE_JWT_AUDIENCE") for c in _backend_components(spec)} - {None},
    }


def check_separation() -> None:
    """Render both environments and fail on ANY shared identity."""
    fake = "sha256:" + "0" * 64
    prod = identities(render("production", fake, fake))
    stag = identities(render("staging", fake, fake))
    for kind in prod:
        shared = prod[kind] & stag[kind]
        if shared:
            raise RenderError(
                f"staging and production share {kind}: {sorted(shared)}. Staging "
                f"pointed at a production {kind[:-1]} is a production incident."
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", choices=sorted(ENVIRONMENTS))
    ap.add_argument("--backend-digest")
    ap.add_argument("--frontend-digest")
    ap.add_argument("--domain", default="",
                    help="this environment's public hostname (NEOH_DOMAIN); omitted = none")
    ap.add_argument("--check", action="store_true",
                    help="validate both environments and their separation; print nothing")
    args = ap.parse_args(argv)

    try:
        check_separation()
        if args.check:
            print("ok: production and staging render, validate, and share no identity",
                  file=sys.stderr)
            return 0
        if not (args.env and args.backend_digest and args.frontend_digest):
            ap.error("--env, --backend-digest and --frontend-digest are required")
        spec = render(args.env, args.backend_digest, args.frontend_digest,
                      domain=args.domain or None)
    except RenderError as exc:
        print(f"\n  REFUSING TO RENDER: {exc}\n", file=sys.stderr)
        return 1

    sys.stdout.write(yaml.safe_dump(spec, sort_keys=False, width=1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())
