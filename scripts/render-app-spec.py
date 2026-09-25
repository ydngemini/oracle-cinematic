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
    api instance_count    (from spec)       1
    ORACLE_RECOVERY_MODE  absent            "1" on every backend component

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
  * staging and production sharing any identity: app, clusters, bucket
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
        "api_instances": None,          # keep the spec's value
        "recovery_mode": False,
    },
    "staging": {
        "name": "neoh-staging",
        "clusters": {"neoh-postgres": "neoh-postgres-staging",
                     "neoh-redis": "neoh-redis-staging"},
        "bucket": "neoh-media-staging",
        "oracle_env": "staging",
        "api_instances": 1,
        "recovery_mode": True,
    },
}

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class RenderError(ValueError):
    pass


def _backend_components(spec: dict) -> list[dict]:
    return list(spec.get("services") or []) + list(spec.get("workers") or [])


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


def render(env: str, backend_digest: str, frontend_digest: str) -> dict:
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
    for db in spec.get("databases") or []:
        if db.get("name") in cfg["clusters"]:
            db["cluster_name"] = cfg["clusters"][db["name"]]

    for comp in _backend_components(spec):
        _set_env(comp, "ORACLE_ENV", cfg["oracle_env"])
        if _env_value(comp, "ORACLE_S3_BUCKET") is not None:
            _set_env(comp, "ORACLE_S3_BUCKET", cfg["bucket"])
        if cfg["recovery_mode"]:
            _set_env(comp, "ORACLE_RECOVERY_MODE", "1")
    if cfg["api_instances"] is not None:
        for svc in spec.get("services") or []:
            if svc.get("name") == "api":
                svc["instance_count"] = cfg["api_instances"]

    for comp in _backend_components(spec) + list(spec.get("static_sites") or []):
        image = comp.get("image") or {}
        if image.get("digest") == BACKEND_PLACEHOLDER:
            image["digest"] = backend_digest
        elif image.get("digest") == FRONTEND_PLACEHOLDER:
            image["digest"] = frontend_digest

    validate(env, spec)
    return spec


def validate(env: str, spec: dict) -> None:
    """Refuse anything unsafe. Called on every render."""
    text = yaml.safe_dump(spec)
    if BACKEND_PLACEHOLDER in text or FRONTEND_PLACEHOLDER in text:
        raise RenderError("a digest placeholder survived substitution")

    backend = _backend_components(spec)
    if not backend:
        raise RenderError("the spec has no backend components")

    for comp in backend:
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
        spec = render(args.env, args.backend_digest, args.frontend_digest)
    except RenderError as exc:
        print(f"\n  REFUSING TO RENDER: {exc}\n", file=sys.stderr)
        return 1

    sys.stdout.write(yaml.safe_dump(spec, sort_keys=False, width=1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())
