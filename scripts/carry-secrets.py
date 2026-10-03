#!/usr/bin/env python3
"""Carry the running app's encrypted secrets into a spec before it is applied.

    doctl apps list-deployments "$APP_ID" -o json > deployments.json
    scripts/carry-secrets.py --deployments deployments.json --rendered app.yaml \
        --out app.deploy.yaml --env production
    doctl apps update "$APP_ID" --spec app.deploy.yaml

SOURCE: the spec of the newest ACTIVE deployment, NEVER `doctl apps spec get`.
After a blank-secret update, the APP spec still shows `EV[…]` for the wiped
keys, but those ciphertexts can be encrypted EMPTY strings: the containers got
nothing (observed on neoh-staging, 2026-10-03). The ACTIVE deployment is the
one that is actually serving, so its values are the ones that booted.

UNVERIFIED: that App Platform accepts `EV[…]` values resubmitted in an
update. The operator must prove it on staging before CI deploys are enabled
(docs/staging-setup.md step 13). If DO rejects it, the fallback is plaintext
injection from GitHub environment secrets at render time.

WHY. `doctl apps update --spec` applies the WHOLE desired state, and a
`type: SECRET` env var with no value in that spec WIPES the secret on the
app. Proven on neoh-staging, 2026-10-03: a CI-shaped spec (rendered from the
committed app.yaml, whose secrets are blank by design) left api and worker
with no ORACLE_SECRET_KEY. Both refused to boot, the deployment failed, and DO
rolled back automatically. Every release after the first would have done the
same, and the automatic rollback would have hidden it as "deploy failed".

WHAT. For every SECRET env var that is empty in the rendered spec, copy the
running spec's value (DO returns it encrypted, `EV[…]`) for the SAME component
and key. Non-empty rendered values (App Platform bindings such as
`${neoh-postgres.PASSWORD}`, or a value set on purpose) are never touched.

REFUSES, before anything is applied, when a REQUIRED secret has no value in
either spec. The message lists key names only. Optional secrets that are
blank in both specs are reported and left blank, which is the same as unset.

NEVER PRINTS A VALUE. Output is component:KEY names and counts. The output file
holds the EV[…] ciphertexts, so treat it like the running spec: do not upload
it as an artifact or print it.

Exit codes: 0 written; 1 a required secret is missing; 2 bad input.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import yaml

COMPONENT_KINDS = ("services", "workers", "jobs", "static_sites")

# Secrets a backend cannot boot or run safely without (config.validate_or_die,
# auth.py). Bindings (ORACLE_DB_PASSWORD, REDIS_URL) are rendered with values
# and never reach this check.
REQUIRED = {
    "api": ("ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY", "ORACLE_DB_PLATFORM_PASSWORD",
            "ORACLE_S3_ACCESS_KEY_ID", "ORACLE_S3_SECRET_ACCESS_KEY",
            "ORACLE_ADMIN_ID", "ORACLE_ADMIN_PASSPHRASE"),
    "worker": ("ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY", "ORACLE_DB_PLATFORM_PASSWORD",
               "ORACLE_S3_ACCESS_KEY_ID", "ORACLE_S3_SECRET_ACCESS_KEY"),
}
OPERATOR_2FA = ("ORACLE_ADMIN_TOTP_SECRET", "ORACLE_ADMIN_OTP_EMAIL")


class CarryError(ValueError):
    pass


def _components(spec: dict) -> dict:
    out = {}
    for kind in COMPONENT_KINDS:
        for c in spec.get(kind) or []:
            out[c.get("name")] = c
    return out


def _has(v) -> bool:
    return v not in (None, "")


def carry(running: Optional[dict], rendered: dict, env: str = "") -> tuple[dict, dict]:
    """Return (spec_to_apply, report). Raises CarryError if a required secret
    would be empty. `report` holds names only."""
    if not isinstance(rendered, dict):
        raise CarryError("rendered spec is not a mapping")
    run_comps = _components(running or {})
    report = {"carried": [], "kept": [], "optional_unset": [], "missing_required": []}
    for name, comp in _components(rendered).items():
        run_envs = {e.get("key"): e for e in (run_comps.get(name, {}).get("envs") or [])}
        for e in comp.get("envs") or []:
            if e.get("type") != "SECRET":
                continue
            key = e.get("key")
            label = f"{name}:{key}"
            if _has(e.get("value")):
                report["kept"].append(label)
                continue
            prev = run_envs.get(key) or {}
            if _has(prev.get("value")):
                e["value"] = prev["value"]
                report["carried"].append(label)
            elif key in REQUIRED.get(name, ()):
                report["missing_required"].append(label)
            else:
                report["optional_unset"].append(label)

        # Conditional requirements, mirroring config.validate_or_die.
        values = {e.get("key"): e.get("value") for e in comp.get("envs") or []}
        if _has(values.get("STRIPE_SECRET_KEY")) and "STRIPE_WEBHOOK_SECRET" in values \
                and not _has(values.get("STRIPE_WEBHOOK_SECRET")):
            report["missing_required"].append(f"{name}:STRIPE_WEBHOOK_SECRET (required once STRIPE_SECRET_KEY is set)")
        if env == "production" and name == "api" and _has(values.get("ORACLE_ADMIN_ID")) \
                and not any(_has(values.get(k)) for k in OPERATOR_2FA):
            report["missing_required"].append("api:ORACLE_ADMIN_TOTP_SECRET|ORACLE_ADMIN_OTP_EMAIL (operator second factor)")

    if report["missing_required"]:
        raise CarryError(
            "refusing to deploy: these required secrets have no value in the rendered spec "
            "AND none on the running app — applying would leave them unset and the backend "
            "would refuse to boot: " + ", ".join(report["missing_required"])
            + ". Set them on the app (DigitalOcean console → component → Environment "
              "Variables, Encrypt) and re-run."
        )
    return rendered, report


def active_deployment_spec(deployments) -> tuple[dict, str]:
    """(spec, deployment id) of the newest ACTIVE deployment in
    `doctl apps list-deployments <app> -o json` output."""
    if isinstance(deployments, dict):           # a single deployment, or {"deployments": [...]}
        deployments = deployments.get("deployments") or [deployments]
    active = [d for d in deployments or [] if str(d.get("phase", "")).upper() == "ACTIVE" and d.get("spec")]
    if not active:
        raise CarryError("no ACTIVE deployment with a spec — nothing safe to carry from (first deploy? "
                         "then set the secrets with `doctl apps create`, docs/staging-setup.md step 8)")
    newest = max(active, key=lambda d: str(d.get("created_at", "")))
    return newest["spec"], str(newest.get("id", ""))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Carry encrypted secrets from the app's ACTIVE deployment.")
    ap.add_argument("--deployments", required=True,
                    help="`doctl apps list-deployments <app-id> -o json` output (the ACTIVE one is used)")
    ap.add_argument("--rendered", required=True, help="spec from scripts/render-app-spec.py")
    ap.add_argument("--out", required=True, help="spec to apply (holds EV[…] ciphertexts — never upload it)")
    ap.add_argument("--env", default="", choices=["", "production", "staging"])
    args = ap.parse_args(argv)
    try:
        with open(args.deployments, encoding="utf-8") as f:
            deployments = yaml.safe_load(f)   # JSON is YAML
        with open(args.rendered, encoding="utf-8") as f:
            rendered = yaml.safe_load(f)
        running, dep_id = active_deployment_spec(deployments)
        if not _components(running):
            raise CarryError("the ACTIVE deployment's spec has no components — refusing to guess")
        spec, report = carry(running, rendered, args.env)
    except CarryError as exc:
        print(f"\n  CARRY-SECRETS: {exc}\n", file=sys.stderr)
        return 1
    except (OSError, yaml.YAMLError) as exc:
        print(f"\n  CARRY-SECRETS: cannot read a spec: {type(exc).__name__}\n", file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, sort_keys=False, width=1000)
    print(f"carried {len(report['carried'])} encrypted secret value(s) from ACTIVE deployment "
          f"{dep_id[:8]}; "
          f"{len(report['kept'])} rendered value(s) kept (bindings)")
    if report["optional_unset"]:
        print("optional secrets unset on this app: " + ", ".join(report["optional_unset"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
