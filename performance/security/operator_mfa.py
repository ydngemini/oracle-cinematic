"""Live check of the operator second factor (TOTP mode) on a test topology.

  docker exec -e NEOH_SECURITY_TEST_ALLOWED=1 -e SECRET=<the topology's ORACLE_ADMIN_TOTP_SECRET> \
      -w /app oracle-perf-worker python /perf/security/operator_mfa.py
"""
import os
import sys
import time

sys.path.insert(0, "/app")
import httpx  # noqa: E402
import totp  # noqa: E402

if os.getenv("NEOH_SECURITY_TEST_ALLOWED") != "1":
    sys.exit("refusing: test topology only")
B = os.getenv("TARGET", "http://oracle-perf-lb:8080")
H = {"X-Forwarded-For": "203.0.113.141"}
admin, pw, secret = os.environ["ORACLE_ADMIN_ID"], os.environ["ORACLE_ADMIN_PASSPHRASE"], os.environ["SECRET"]
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if not ok else ""))
    if not ok:
        FAILS.append(name)


def login(**kw):
    return httpx.post(f"{B}/auth/login", json={"agent_id": admin, "passphrase": pw, **kw}, headers=H, timeout=30)


r = login()
check("operator passphrase alone is not enough", r.status_code == 401 and "OTP_REQUIRED" in r.text, r.text[:120])
r = httpx.post(f"{B}/auth/login", json={"agent_id": admin, "passphrase": "wrong-passphrase-x"}, headers=H)
check("a wrong passphrase never reaches the OTP prompt", r.status_code == 401 and "OTP" not in r.text, r.text[:120])
r = login(otp="000000")
check("a wrong code is refused", r.status_code == 401 and "OTP_INVALID" in r.text, r.text[:120])
code = totp.code_at(secret, int(time.time() // 30))
r = login(otp=code)
check("passphrase + current code signs in as platform admin", r.status_code == 200 and r.json().get("role") == "platform_admin", r.text[:120])
r = login(otp=code)
check("the same code cannot be used twice", r.status_code == 401 and "OTP_INVALID" in r.text, r.text[:120])
print(f"\n{len(FAILS)} failure(s)")
sys.exit(len(FAILS))
