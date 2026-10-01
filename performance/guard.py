#!/usr/bin/env python3
"""Refuse to load-test anything that is not positively a load-test target.

Fail closed. Every check must PASS for a run to proceed; "could not tell" is a
refusal, never a pass. A warning printed before a load test starts is read by
nobody — the process just has to not start.

Three independent conditions, all required:

  1. The operator opted in:    NEOH_LOAD_TEST_ALLOWED=1
  2. The URL is not production: not a known production hostname, not a
     hostname the operator has named as production in NEOH_PRODUCTION_HOSTS
  3. The SERVER agrees:         GET /version reports environment "staging" or
                                "loadtest" — what the target says about itself,
                                not what the operator typed

Condition 3 is the one that matters. An operator can mistype a URL; a
production process cannot claim to be a load-test environment unless someone
deliberately configured it to, which is exactly the intent the check is
reading.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

#: Hostnames that are production, whatever else is configured. Extend with
#: NEOH_PRODUCTION_HOSTS (comma-separated) — the DigitalOcean default
#: *.ondigitalocean.app name of the production app belongs there once it exists.
KNOWN_PRODUCTION_HOSTS = {"neohrs.com", "www.neohrs.com", "neoh.app", "www.neoh.app"}

ALLOWED_ENVIRONMENTS = {"staging", "loadtest"}


class Refused(Exception):
    pass


def production_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("NEOH_PRODUCTION_HOSTS", "").split(",") if h.strip()}
    return KNOWN_PRODUCTION_HOSTS | extra


def check_opt_in(env=os.environ) -> None:
    if env.get("NEOH_LOAD_TEST_ALLOWED") != "1":
        raise Refused(
            "NEOH_LOAD_TEST_ALLOWED=1 is not set. Load tests must be opted into "
            "explicitly, every time."
        )


def check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Refused(f"{url!r} is not an http(s) URL")
    host = parsed.hostname.lower()
    for prod in production_hosts():
        if host == prod or host.endswith("." + prod):
            raise Refused(f"{host} is a production hostname. Refusing.")
    return host


def fetch_version(url: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/version", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Refused(
            f"could not read {url}/version ({exc}). A target that cannot say what "
            f"it is cannot be confirmed safe."
        ) from exc


def check_server(version: dict) -> str:
    env = str(version.get("environment") or "").lower()
    if env not in ALLOWED_ENVIRONMENTS:
        raise Refused(
            f"the target reports environment {env or '(none)'!r}; only "
            f"{sorted(ALLOWED_ENVIRONMENTS)} may be load-tested."
        )
    return env


def check(url: str, env=os.environ, version: dict | None = None) -> dict:
    """Run every check. Returns metadata for the result artifact."""
    check_opt_in(env)
    host = check_url(url)
    version = version if version is not None else fetch_version(url)
    environment = check_server(version)
    return {
        "target_host": host,
        "target_environment": environment,
        "target_git_sha": version.get("git_sha", "unknown"),
        "target_migration_head": version.get("migration_head"),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: guard.py <target-url>", file=sys.stderr)
        return 2
    try:
        meta = check(argv[1])
    except Refused as exc:
        print(f"\n  REFUSING TO LOAD TEST: {exc}\n", file=sys.stderr)
        return 1
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
