#!/usr/bin/env python3
"""Generate the operator's TOTP secret (security review AUTH-12).

    python3 scripts/generate-operator-totp.py ops@example.com

Prints the secret to store as ORACLE_ADMIN_TOTP_SECRET (App Platform SECRET)
and an otpauth:// URI to add to an authenticator app (paste it, or render it
as a QR code locally — never through an online QR service). Run it on a trusted
machine; the output is a credential.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import totp  # noqa: E402

account = sys.argv[1] if len(sys.argv) > 1 else "operator"
secret = totp.new_secret()
print(f"ORACLE_ADMIN_TOTP_SECRET={secret}")
print(totp.provisioning_uri(secret, account))
