#!/usr/bin/env python3
"""Generate the perf environment's OWN provider secrets — never real ones.

The perf topology drops every real provider credential (topology/up.sh). The
webhook endpoints still verify signatures, so the load test needs secrets the
server trusts and the harness can sign with:

    Stripe   STRIPE_WEBHOOK_SECRET   whsec_… HMAC key
    Telnyx   TELNYX_PUBLIC_KEY       an Ed25519 keypair made here; the server
                                     gets the public half, the harness signs
                                     with the private half
    Plivo    PLIVO_AUTH_ID/TOKEN     V3 HMAC key, and the account id inbound
                                     routes must match

plus the mock endpoints, so model and realtime calls stay on the local mock.

Writes (gitignored):
    performance/out/perf-secrets.json   everything, for fixtures/webhooks.py
    performance/out/perf-secrets.env    the server-side half, for up.sh

Re-running keeps existing secrets unless --rotate is given.
"""

from __future__ import annotations

import base64
import json
import pathlib
import secrets
import sys

OUT = pathlib.Path(__file__).resolve().parents[1] / "out"

MOCK_ENV = {
    # https: the server refuses a non-TLS public URL outside dev (correctly) and
    # validates provider signatures against THIS configured URL, so fixtures are
    # signed for it while the internal hop to the balancer stays plain http.
    "ORACLE_PUBLIC_BASE_URL": "https://oracle-perf-lb:8080",
    "ORACLE_LOCAL_LLM_URL": "http://oracle-perf-mock:9000/v1/chat/completions",
    "ORACLE_LOCAL_LLM_MODEL": "perf-mock",
    "DASHSCOPE_API_KEY": "perf-mock-not-a-key",
    "DASHSCOPE_REALTIME_URL": "ws://oracle-perf-mock:9000/realtime",
    "RECONSTRUCTION_PROVIDER": "stub",
}


def generate() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    raw_priv = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    raw_pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {
        "stripe_webhook_secret": "whsec_perf" + secrets.token_hex(24),
        "telnyx_private_key": base64.b64encode(raw_priv).decode(),
        "telnyx_public_key": base64.b64encode(raw_pub).decode(),
        "plivo_auth_id": "MAPERF" + secrets.token_hex(7).upper(),
        "plivo_auth_token": "perf" + secrets.token_hex(18),
    }


def server_env(s: dict) -> str:
    lines = {
        "STRIPE_WEBHOOK_SECRET": s["stripe_webhook_secret"],
        "TELNYX_PUBLIC_KEY": s["telnyx_public_key"],
        "PLIVO_AUTH_ID": s["plivo_auth_id"],
        "PLIVO_AUTH_TOKEN": s["plivo_auth_token"],
        **MOCK_ENV,
    }
    return "".join(f"{k}={v}\n" for k, v in lines.items())


def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "perf-secrets.json"
    if path.exists() and "--rotate" not in argv:
        s = json.loads(path.read_text())
    else:
        s = generate()
        path.write_text(json.dumps(s, indent=1))
    # 0644: read by fixture tools running as another uid inside the perf
    # network. Perf-only synthetic secrets, gitignored — never real credentials.
    path.chmod(0o644)
    env = OUT / "perf-secrets.env"
    env.write_text(server_env(s))
    env.chmod(0o644)  # read by up.sh inside the DinD host; holds perf-only values
    print(f"  perf secrets: {path} + {env}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
