#!/usr/bin/env python3
"""Which provider callbacks this release MUST have working (audit 30.4).

    python3 scripts/callback-requirements.py --spec /tmp/app.production.yaml \\
        --also "$REQUIRE_CALLBACK_PROVIDERS"
    -> plivo stripe telnyx

infra/digitalocean/smoke-test.sh probes every URL in callback-routes.txt. A
webhook whose signing secret is not configured answers 503 (DigitalOcean's
edge shows it as 504 + `x-do-orig-status: 503`), and the smoke test treats that
as "provider not configured here" — a WARN — unless the provider is listed in
REQUIRE_CALLBACK_PROVIDERS. That list used to come only from a repository
variable which was never set, so a production release that lost
STRIPE_WEBHOOK_SECRET passed its smoke test while every subscription event
bounced.

This derives the list from the spec the release deploys instead (the RENDERED
spec, scripts/render-app-spec.py output, which declares every secret without a
value), and adds whatever `--also` names (the repository variable):

  stripe  the api declares STRIPE_WEBHOOK_SECRET and billing is not turned off.
          Billing is on unless ORACLE_BILLING_ENFORCED is 0/false/no/off, or
          it is unset and ORACLE_ENV is a development value — the same rule as
          billing.billing_enforced(). A switch whose value the spec does not
          show (a SECRET) counts as on: fail closed.
  telnyx  the api declares TELNYX_API_KEY or TELNYX_PUBLIC_KEY.
  plivo   the api declares PLIVO_AUTH_ID or PLIVO_AUTH_TOKEN.

The api is the component that serves these webhooks, so only its envs (plus
app-level envs, which App Platform applies to every component) count. A
provider is declared in the committed spec only when it is used (see the
"deliberately NOT listed here" note in infra/digitalocean/app.yaml), so a
declared provider whose callback is unconfigured on production means its
secret never reached the app, or was lost: a failed release.

Prints the providers, space-separated, sorted, on one line (possibly empty —
smoke-test.sh refuses an empty set in production unless a named waiver is
given). The reasons go to stderr. Names only: a value is never printed.

Exit codes: 0 printed; 2 the spec is unreadable or has no api service (never
read as "nothing required").
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Optional

import yaml

COMPONENT_KINDS = ("services", "workers", "jobs", "static_sites")
API = "api"
# config._DEV_VALUES: the only environments where billing is off by default.
DEV_VALUES = {"dev", "development", "local"}
OFF_VALUES = {"0", "false", "no", "off"}
CARRIERS = {
    "telnyx": ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY"),
    "plivo": ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
}


class SpecError(ValueError):
    pass


def _api_envs(spec) -> dict:
    """key -> env entry for the api: app-level envs, overridden by the api's own."""
    if not isinstance(spec, dict):
        raise SpecError("the spec is not a mapping")
    api = None
    for kind in COMPONENT_KINDS:
        for comp in spec.get(kind) or []:
            if isinstance(comp, dict) and comp.get("name") == API:
                api = comp
    if api is None:
        raise SpecError("the spec has no 'api' service")
    out = {}
    for entries in (spec.get("envs") or [], api.get("envs") or []):
        for e in entries:
            if isinstance(e, dict) and e.get("key"):
                out[str(e["key"])] = e
    return out


def _plain(entry: Optional[dict]) -> Optional[str]:
    """The readable value of a non-secret env, '' when declared without one,
    None when absent or when its value is not shown (a SECRET)."""
    if entry is None or str(entry.get("type") or "").upper() == "SECRET":
        return None
    v = entry.get("value")
    return "" if v is None else str(v)


def billing_on(envs: dict) -> tuple[bool, str]:
    """billing.billing_enforced(), read from the spec. (on, why)."""
    switch = envs.get("ORACLE_BILLING_ENFORCED")
    if switch is not None:
        raw = _plain(switch)
        if raw is None:
            return True, "ORACLE_BILLING_ENFORCED is a secret, its value unknown (fail closed)"
        if raw.strip():
            on = raw.strip().lower() not in OFF_VALUES
            return on, f"ORACLE_BILLING_ENFORCED={raw.strip()!r}"
    env = (_plain(envs.get("ORACLE_ENV")) or "").strip().lower()
    if env in DEV_VALUES:
        return False, f"ORACLE_BILLING_ENFORCED unset and ORACLE_ENV={env!r} (development)"
    return True, f"ORACLE_BILLING_ENFORCED unset and ORACLE_ENV={env or 'unset'!r}"


def derive(spec) -> tuple[list[str], list[str]]:
    """(providers, reasons) — raises SpecError when there is no api to read."""
    envs = _api_envs(spec)
    need, why = set(), []
    on, billing_why = billing_on(envs)
    if "STRIPE_WEBHOOK_SECRET" in envs:
        if on:
            need.add("stripe")
            why.append(f"stripe: the api declares STRIPE_WEBHOOK_SECRET and billing is on ({billing_why})")
        else:
            why.append(f"stripe: not required, billing is off ({billing_why})")
    else:
        why.append("stripe: not required, the api does not declare STRIPE_WEBHOOK_SECRET")
    for provider, keys in CARRIERS.items():
        declared = [k for k in keys if k in envs]
        if declared:
            need.add(provider)
            why.append(f"{provider}: the api declares {', '.join(declared)}")
        else:
            why.append(f"{provider}: not required, the api declares none of {', '.join(keys)}")
    return sorted(need), why


def _names(text: str) -> list[str]:
    return [n for n in re.split(r"[\s,]+", text or "") if n]


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--spec", required=True, help="the rendered App Platform spec this release deploys")
    ap.add_argument("--also", default="", help="extra providers (the REQUIRE_CALLBACK_PROVIDERS variable), "
                                               "space- or comma-separated")
    args = ap.parse_args(argv)
    try:
        with open(args.spec, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        derived, why = derive(spec)
    except (OSError, yaml.YAMLError) as exc:
        print(f"callback-requirements: cannot read {args.spec}: {type(exc).__name__}", file=sys.stderr)
        return 2
    except SpecError as exc:
        print(f"callback-requirements: {exc}; refusing to report 'nothing required'", file=sys.stderr)
        return 2
    extra = _names(args.also)
    for line in why:
        print(f"  {line}", file=sys.stderr)
    if extra:
        print(f"  also required by REQUIRE_CALLBACK_PROVIDERS: {' '.join(extra)}", file=sys.stderr)
    print(" ".join(sorted(set(derived) | set(extra))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
