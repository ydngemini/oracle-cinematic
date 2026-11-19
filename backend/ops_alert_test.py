"""Proof that an operator alert reaches a person (audit 30.3).

The launch check used to ask only whether ORACLE_ALERT_EMAIL was set, so a
mistyped address or a broken relay passed until the first real incident. An
operator now sends a test alert from Operator overview. It carries a one-time
code, and the operator types the code back. A confirmed code is the proof: the
relay accepting a message is not proof that anyone received it.

What is stored (ops_alert_tests, migration 0180): who asked and who confirmed
(operator ids), a SHA-256 prefix of the recipient address (never the address,
so the proof can be matched to today's recipient), an HMAC of the code (never
the code), the relay outcome and the times. Only the most recent
test sent to today's recipient can be confirmed, for CODE_TTL_HOURS, with at
most MAX_ATTEMPTS tries. The code is stored only as an HMAC keyed by the
server secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Optional

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
CODE_LENGTH = 8
CODE_TTL_HOURS = 24
MAX_ATTEMPTS = 5
COOLDOWN_SECONDS = 60
# The launch check accepts a confirmation this recent for today's recipient.
PROOF_MAX_AGE_DAYS = 14
_LOCK_KEY = 0x4F505354_45535431  # "OPSTEST1"

SENT = "sent"
SENDING = "sending"


def fingerprint(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()[:16]


def _normalize(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


def _hash(code: str) -> str:
    """Keyed, so a copy of the table (a backup, a replica) cannot be reversed
    into the code: an unkeyed hash of a 40-bit code falls to a GPU in minutes."""
    from auth import SECRET_KEY

    key = hmac.new(SECRET_KEY.encode(), b"neoh-ops-alert-test-code-v1", hashlib.sha256).digest()
    return hmac.new(key, _normalize(code).encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


SUBJECT = "[Neoh] Test alert: confirm you received it"


def body(code: str) -> str:
    return (
        "This is a test of Neoh's operator alerts, sent from Operator overview.\n\n"
        "To confirm that alerts reach you, enter this code under Operator overview > "
        f"Alert delivery within {CODE_TTL_HOURS} hours:\n\n"
        f"    {display_code(code)}\n\n"
        "Until a test is confirmed, the launch check cannot credit alert delivery.\n"
        "If you did not expect this email, you can ignore it."
    )


@dataclass
class Started:
    id: Optional[int] = None
    code: Optional[str] = None
    recipient: str = ""
    retry_after: Optional[int] = None  # set when refused by the cool-down


async def begin(conn, requested_by: str, recipient: str) -> Started:
    """Record the attempt before anything is sent. Refuses within the cool-down
    (one test a minute across all operators). With no recipient the attempt is
    recorded as recipient_missing and nothing will be sent."""
    await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK_KEY)
    wait = await conn.fetchval(
        "SELECT ceil(extract(epoch FROM (max(requested_at) + make_interval(secs => $1)) - now()))::int "
        "FROM ops_alert_tests HAVING max(requested_at) > now() - make_interval(secs => $1)",
        float(COOLDOWN_SECONDS))
    if wait is not None:
        return Started(retry_after=max(1, int(wait)))
    recipient = recipient.strip()
    if not recipient:
        await conn.execute(
            "INSERT INTO ops_alert_tests (requested_by, recipient_fingerprint, outcome) "
            "VALUES ($1, '', 'recipient_missing')", requested_by)
        return Started()
    code = new_code()
    row_id = await conn.fetchval(
        "INSERT INTO ops_alert_tests (requested_by, recipient_fingerprint, code_hash, outcome) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        requested_by, fingerprint(recipient), _hash(code), SENDING)
    return Started(id=row_id, code=code, recipient=recipient)


async def finish(conn, row_id: int, error: Optional[str]) -> None:
    """Record what the relay said. An unsent code can never be confirmed."""
    if error is None:
        await conn.execute("UPDATE ops_alert_tests SET outcome = $2 WHERE id = $1", row_id, SENT)
    else:
        await conn.execute(
            "UPDATE ops_alert_tests SET outcome = $2, code_hash = NULL WHERE id = $1", row_id, error[:80])


_LATEST_SENT = (
    "SELECT id, code_hash, attempts, confirmed_at FROM ops_alert_tests "
    "WHERE outcome = $1 AND recipient_fingerprint = $2 AND requested_at > now() - make_interval(hours => $3) "
    "ORDER BY requested_at DESC LIMIT 1")


async def confirm(conn, code: str, confirmed_by: str, recipient: str) -> dict[str, Any]:
    """Check a code against the latest test sent to today's recipient. Returns
    {"result": "confirmed" | "wrong_code" | "too_many_attempts" | "no_pending_test", ...}.
    A wrong code is counted in the same transaction, so the caller must let
    the transaction commit before it reports the refusal. Two confirmations
    of the same code serialize on the row lock; the second finds it already
    confirmed (no_pending_test) instead of spending an older test's tries."""
    if not recipient.strip():
        return {"result": "no_pending_test"}
    row = await conn.fetchrow(_LATEST_SENT + " FOR UPDATE", SENT, fingerprint(recipient), CODE_TTL_HOURS)
    if row is None or row["confirmed_at"] is not None or row["code_hash"] is None:
        return {"result": "no_pending_test"}
    if row["attempts"] >= MAX_ATTEMPTS:
        return {"result": "too_many_attempts"}
    if not hmac.compare_digest(_hash(code), row["code_hash"]):
        attempts = await conn.fetchval(
            "UPDATE ops_alert_tests SET attempts = attempts + 1 WHERE id = $1 RETURNING attempts", row["id"])
        return {"result": "wrong_code", "attempts_left": max(0, MAX_ATTEMPTS - attempts)}
    await conn.execute(
        "UPDATE ops_alert_tests SET confirmed_at = now(), confirmed_by = $2, code_hash = NULL WHERE id = $1",
        row["id"], confirmed_by)
    return {"result": "confirmed"}


async def status(conn, recipient: str) -> dict[str, Any]:
    """What the operator console and the launch check read. `proven` is true
    only for a confirmation of today's recipient within PROOF_MAX_AGE_DAYS."""
    recipient = recipient.strip()
    proof = None
    if recipient:
        proof = await conn.fetchrow(
            "SELECT confirmed_at, now() - confirmed_at <= make_interval(days => $2) AS fresh "
            "FROM ops_alert_tests WHERE confirmed_at IS NOT NULL AND recipient_fingerprint = $1 "
            "ORDER BY confirmed_at DESC LIMIT 1",
            fingerprint(recipient), PROOF_MAX_AGE_DAYS)
    pending = None
    if recipient:
        pending = await conn.fetchrow(_LATEST_SENT, SENT, fingerprint(recipient), CODE_TTL_HOURS)
    last = await conn.fetchrow(
        "SELECT outcome, requested_at, confirmed_at FROM ops_alert_tests ORDER BY requested_at DESC LIMIT 1")
    return {
        "recipient_configured": bool(recipient),
        "proven": bool(proof and proof["fresh"]),
        "confirmed_at": proof["confirmed_at"].isoformat() if proof else None,
        "stale": bool(proof and not proof["fresh"]),
        "max_age_days": PROOF_MAX_AGE_DAYS,
        "awaiting_code": bool(pending and pending["confirmed_at"] is None and pending["code_hash"] is not None
                              and pending["attempts"] < MAX_ATTEMPTS),
        "last_attempt": None if last is None else {
            "outcome": last["outcome"],
            "requested_at": last["requested_at"].isoformat(),
            "confirmed": last["confirmed_at"] is not None,
        },
    }
