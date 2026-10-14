#!/usr/bin/env python3
"""Read-only staging smoke as the demo owner and agent.

Signs in through the real routes (the owner with a two-step code from the
local 0600 credentials file), accepts the current policy, and reads the
record-scoped lists plus a few public gates. Prints statuses, counts and
timings only — never secrets, names or record content. Writes nothing.

    venv/bin/python scripts/staging-smoke.py
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_tenant_common import AGENT, OWNER, Api, ApiError, load_or_create_credentials  # noqa: E402

BASE = "https://neoh-staging-ksfpn.ondigitalocean.app"
ROUTES = (
    "/api/crm/clients",
    "/api/crm/contacts?limit=50",
    "/api/commands",
    "/api/commands/approvals",
    "/api/brokerage/setup",
    "/api/property-records?limit=5",
)


def _get(api: Api, path: str):
    started = time.time()
    try:
        data, status = api.get(path), 200
    except ApiError as err:
        data, status = None, err.status
    return status, data, (time.time() - started) * 1000


def _shape(path: str, data) -> object:
    if path == "/api/brokerage/setup" and isinstance(data, dict):
        return {"commercial_email": (data.get("commercial_email") or {}).get("status")}
    if isinstance(data, dict):
        return {k: len(v) for k, v in data.items() if isinstance(v, list)}
    return type(data).__name__


def main() -> int:
    creds = load_or_create_credentials()
    for who, user in (("owner", OWNER), ("agent", AGENT)):
        api = Api(BASE)
        api.login(user["email"], creds[user["email"]], creds=creds)
        api.accept_policies()
        _, pol, _ = _get(api, "/auth/policy-acceptance")
        _, mfa, _ = _get(api, "/auth/mfa")
        print(f"== {who}: policy={pol.get('policy_version')} esa={pol.get('account_security_version')} "
              f"two_step enabled={mfa and mfa.get('enabled')} required={mfa and mfa.get('required')}")
        for path in ROUTES:
            status, data, ms = _get(api, path)
            print(f"   {path:32} {status} {ms:5.0f} ms {_shape(path, data)}")
    password_only = Api(BASE)
    try:
        password_only.request("POST", "/auth/login",
                              json_body={"agent_id": OWNER["email"], "passphrase": creds[OWNER["email"]]})
        print("owner password-only sign-in: ACCEPTED (unexpected)")
    except ApiError as err:
        print(f"owner password-only sign-in: {err.status} (code required)")
    for path in ("/.well-known/security.txt", "/api/public/email/unsubscribe/not-a-real-token"):
        try:
            with urllib.request.urlopen(urllib.request.Request(BASE + path), timeout=30) as resp:
                print(path, resp.status)
        except urllib.error.HTTPError as err:
            print(path, err.code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
