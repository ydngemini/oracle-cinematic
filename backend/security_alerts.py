"""Tell a person when privileged access is used or changed (ISO 27001 A.8.15 /
A.8.16; ledger L17-09).

Neoh is run by one person, so there is no second operator to review the first
(no segregation of duties). The compensating control is that every use of
the highest privilege leaves a message outside the system, in a mailbox the
operator reads and an intruder who holds the operator's session does not
control:

  * a platform-admin / operator sign-in (each success);
  * repeated failed platform-admin sign-ins (passphrase), a rejected second
    factor after a correct passphrase, and the operator account being
    rate-limited at login;
  * any executed brokerage role change, and any account created as a broker
    owner (self-serve signup, accepted owner invitation);
  * a change of the operator's second factor (the only MFA that exists today
    is the operator's, provisioned from the environment, so "MFA reset" means
    ORACLE_ADMIN_TOTP_SECRET / ORACLE_ADMIN_OTP_EMAIL changed between boots).

What an alert holds: the event, the account id, the client IP and the UTC
time (plus the roles for a role change). Never a passphrase, a code, a token
or a secret. The account id (an email) and the IP are personal data
(docs/privacy-data-map.md §1, "Security alert email"), so they go only into
the email, sent to ORACLE_SECURITY_ALERT_EMAIL (falling back to
ORACLE_ALERT_EMAIL). The log line on `oracle.security.alert` names the
actor by actor_ref() and carries no IP.

Delivery is best-effort and off the request path: a background task with the
SMTP timeout ops_alerts already uses. A failed send is logged by error type and
never fails a sign-in. Failure counting is per process (no shared store): with
N API replicas an attacker gets up to N x threshold guesses before the first
alert, and each replica alerts on its own.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from log_redaction import actor_ref

log = logging.getLogger("oracle.security.alert")

FAILURE_WINDOW_SECONDS = 15 * 60
_failures: dict[str, collections.deque] = {}
_last_failure_alert: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()
_FACTOR_LOCK_KEY = 0x53454346_41435452  # "SECFACTR"
FACTOR_ACTION = "operator_second_factor_configured"


def enabled() -> bool:
    return os.getenv("ORACLE_SECURITY_ALERTS_ENABLED", "1") != "0"


def recipient() -> str:
    return (os.getenv("ORACLE_SECURITY_ALERT_EMAIL", "").strip()
            or os.getenv("ORACLE_ALERT_EMAIL", "").strip())


def failure_threshold() -> int:
    try:
        return max(1, int(os.getenv("ORACLE_SECURITY_ALERT_FAILURE_THRESHOLD", "5")))
    except ValueError:
        return 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def deliver(event: str, subject: str, fields: dict[str, Any]) -> Optional[str]:
    """Log the event (pseudonymous) and email the full alert. Returns an
    error name or None; never raises."""
    log.warning(json.dumps({
        "security_alert": event,
        "actor": actor_ref(fields.get("Account")),
        **({"by": actor_ref(fields["Changed by"])} if fields.get("Changed by") else {}),
    }))
    to = recipient()
    if not to:
        return None
    body = "\n".join(f"{k}: {v if v not in (None, '') else '-'}" for k, v in fields.items())
    body += ("\n\nIf this was not you or an expected change, rotate the operator passphrase and "
             "second factor and review the audit trail. Runbook: docs/security-launch-review.md.")
    try:
        import ops_alerts

        return await ops_alerts.send_email(f"[Neoh security] {subject}", body, to=to)
    except Exception as exc:  # noqa: BLE001 - an alert must never break the caller
        log.warning("security alert not delivered: %s", type(exc).__name__)
        return type(exc).__name__


def _spawn(event: str, subject: str, fields: dict[str, Any]) -> None:
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(deliver(event, subject, fields), name=f"security-alert:{event}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for alerts in flight (tests and shutdown)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ── sign-in ──────────────────────────────────────────────────────────────

def privileged_sign_in(agent_id: str, ip: Optional[str], role: str) -> None:
    _spawn("privileged_sign_in", f"{role} signed in", {
        "Event": f"{role} sign-in succeeded", "Account": agent_id, "IP": ip, "Time (UTC)": _now_iso()})


def privileged_sign_in_failed(agent_id: str, ip: Optional[str], stage: str) -> None:
    """Count a failed privileged sign-in. `stage` is "passphrase" (counted
    against the threshold), or "second_factor" / "rate_limited" (alerted at
    once: a rejected code means the passphrase was right; a rate limit means
    someone is hammering the account)."""
    key = (agent_id or "").strip().lower()
    now = time.monotonic()
    window = _failures.setdefault(key, collections.deque())
    window.append(now)
    while window and window[0] < now - FAILURE_WINDOW_SECONDS:
        window.popleft()
    if stage == "passphrase" and len(window) < failure_threshold():
        return
    # One email per account and kind per window, so a flood of guesses (or
    # of 429s) cannot flood the mailbox. A rejected second factor is rarer
    # and more serious: at most one a minute.
    quiet = 60 if stage == "second_factor" else FAILURE_WINDOW_SECONDS
    if now - _last_failure_alert.get(f"{key}|{stage}", -1e18) < quiet:
        return
    _last_failure_alert[f"{key}|{stage}"] = now
    what = {
        "passphrase": f"{len(window)} failed platform-admin sign-ins in {FAILURE_WINDOW_SECONDS // 60} min",
        "second_factor": "second factor rejected after a correct passphrase",
        "rate_limited": "platform-admin account rate-limited at sign-in",
    }.get(stage, stage)
    _spawn("privileged_sign_in_failed", what, {
        "Event": what, "Account": agent_id, "IP": ip, "Time (UTC)": _now_iso()})


# ── roles ────────────────────────────────────────────────────────────────

def role_changed(*, account: str, prior_role: Optional[str], new_role: str, changed_by: Optional[str],
                 tenant_id: Optional[str], ip: Optional[str], via: str) -> None:
    _spawn("role_changed", f"role change: {prior_role or 'new account'} -> {new_role}", {
        "Event": f"role change ({via})", "Account": account,
        "Role": f"{prior_role or '(new account)'} -> {new_role}", "Changed by": changed_by,
        "Brokerage (tenant id)": tenant_id, "IP": ip, "Time (UTC)": _now_iso()})


# ── the operator's second factor ─────────────────────────────────────────

def operator_factor_fingerprint(factor: Optional[str], material: str) -> dict:
    """What is recorded about the factor: its kind and a truncated hash of its
    material (a 160-bit TOTP secret or the code address). Never the material."""
    digest = hashlib.sha256(f"neoh-operator-factor:v1:{factor}:{material}".encode()).hexdigest()[:16]
    return {"factor": factor or "none", "fingerprint": digest if factor else None}


async def check_operator_factor(*, admin_id: str, factor: Optional[str], material: str) -> Optional[str]:
    """Compare the operator's second factor with the one recorded at the last
    boot; record a change and alert on it. Returns "baseline", "unchanged",
    "changed", "standby" (another replica holds the check) or "skipped"."""
    if not admin_id:
        return "skipped"
    import db.connection as dbc
    from audit_ledger import AuditCategory, _global_chain_conn, ledger

    pool = dbc.get_platform_pool() or dbc.get_pool()
    if pool is None:
        return "skipped"
    current = operator_factor_fingerprint(factor, material)
    async with pool.acquire(timeout=5) as lock_conn:
        if not await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", _FACTOR_LOCK_KEY):
            return "standby"
        try:
            async with _global_chain_conn() as conn:
                previous = await conn.fetchval(
                    "SELECT metadata FROM audit_ledger WHERE category = $1 AND action = $2 "
                    "ORDER BY created_at DESC LIMIT 1",
                    AuditCategory.ADMIN_ACTION.value, FACTOR_ACTION)
            previous = json.loads(previous) if isinstance(previous, str) else previous
            if previous and {k: previous.get(k) for k in current} == current:
                return "unchanged"
            await ledger.record(category=AuditCategory.ADMIN_ACTION, action=FACTOR_ACTION,
                                user_id=admin_id, metadata={**current, "previous_factor":
                                                            (previous or {}).get("factor")})
            if not previous:
                return "baseline"
            fields = {"Event": "operator second factor changed (MFA reset)", "Account": admin_id,
                      "Factor": f"{previous.get('factor')} -> {current['factor']}",
                      "IP": "(configuration change at boot)", "Time (UTC)": _now_iso()}
            if enabled():
                await deliver("operator_second_factor_changed", "operator second factor changed", fields)
            return "changed"
        finally:
            await lock_conn.fetchval("SELECT pg_advisory_unlock($1)", _FACTOR_LOCK_KEY)


async def boot_checks() -> None:
    """Run once per process after the pool is up; never raises."""
    try:
        import auth

        await check_operator_factor(
            admin_id=auth._ADMIN_ID, factor=auth._operator_second_factor(),
            material=auth._ADMIN_TOTP_SECRET or auth._ADMIN_OTP_EMAIL)
    except Exception as exc:  # noqa: BLE001 - a boot check must not stop the boot
        log.warning("operator second-factor check skipped: %s", type(exc).__name__)


def start_boot_checks() -> None:
    task = asyncio.get_running_loop().create_task(boot_checks(), name="security-boot-checks")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
