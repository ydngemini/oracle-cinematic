#!/usr/bin/env python3
"""Seal the plaintext message bodies, call transcript excerpts and chat turns
written before migration 0152 (audit A01, ledger L17-21), and the email
subjects/bodies, interaction-log subjects/content, AI tool receipts, belief
source quotes and profile summaries written before 0154 (L17-22). Operator
script, and the periodic sealed_text_seal task's engine.

Bounded, resumable, idempotent: each batch selects only rows still in the
plaintext shape (at most ORACLE_SEALED_TEXT_BACKFILL_BATCH, default 500) and
commits on its own, so an interrupted run loses nothing and the next run
carries on; a second run over a sealed table changes nothing. Batches lock with
SKIP LOCKED, so the app keeps writing while it runs. negotiation_events,
user_interactions and interaction_logs are append-only to oracle_app, so their
batches go through the 0152 / 0154 SECURITY DEFINER functions, which can only
move a row's own text between its two shapes. Prints counts, tenant ids and
timings, never text.

Sealing an email_outbox row bumps its updated_at (trigger), so a row still
'queued' restarts its 10-minute reconciliation window once.

    python backfill_sealed_text.py                    # every field
    python backfill_sealed_text.py --field sms        # one field (see FIELDS)
    python backfill_sealed_text.py --count            # remaining only, no writes
    python backfill_sealed_text.py --max-rows 10000   # stop after ~N rows per field
    python backfill_sealed_text.py --unseal           # rollback: plaintext back (see 0152)

Not a periodic job: staging runs ORACLE_RECOVERY_MODE, where no periodic job
runs (L17-03). Run it where ORACLE_ENCRYPTION_MASTER_KEY is already set (the
app's own environment), after the 0152 code serves. Exit 1 = rows remain
(locked by a writer, or a tenant id that is not a UUID); run again.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

import sealed_text
from db.connection import close_pool, init_pool, tenant_tx
from tenancy import Role, TenantContext

_ACTOR = "sealed-text-backfill"
_PLATFORM_TENANT = "00000000-0000-0000-0000-000000000000"

# Literal statements only (bandit B608). $1 tenant, $2 key, $3 batch size.
# Every batch picks its rows in a MATERIALIZED CTE (L17-24): as
# `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` the planner may run
# a nested-loop semi join that re-runs the locking subquery per outer row, so
# a "batch of 1" sealed 3 rows. A locking CTE runs exactly once.
_SMS_SEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM sms_messages
        WHERE tenant_id = $1::uuid AND body_ciphertext IS NULL
        AND COALESCE(body, '') <> ''
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE sms_messages
       SET body_ciphertext = pgp_sym_encrypt(body, $2::text, 's2k-mode=1'), body = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_SMS_UNSEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM sms_messages
        WHERE tenant_id = $1::uuid AND body_ciphertext IS NOT NULL
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE sms_messages
       SET body = pgp_sym_decrypt(body_ciphertext, $2::text), body_ciphertext = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_TRANSCRIPT = "SELECT seal_negotiation_transcripts($1::uuid, $2::text, $3::int, $4::boolean)"
_INTERACTION = "SELECT seal_user_interactions($1::text, $2::text, $3::int, $4::boolean)"
_INTERACTION_LOG = "SELECT seal_interaction_logs($1::uuid, $2::text, $3::int, $4::boolean)"

# 0154 (L17-22). The app may UPDATE these four tables, under RLS in the
# platform context of the row's own tenant.
_EMAIL_SEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM email_outbox
        WHERE tenant_id = $1::uuid AND body_html IS NULL
        AND ((subject_ciphertext IS NULL AND subject <> '')
        OR (body_text_ciphertext IS NULL AND body_text <> ''))
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE email_outbox
       SET subject_ciphertext = COALESCE(subject_ciphertext,
                                         pgp_sym_encrypt(NULLIF(subject, ''), $2::text, 's2k-mode=1')),
           subject = '',
           body_text_ciphertext = COALESCE(body_text_ciphertext,
                                           pgp_sym_encrypt(NULLIF(body_text, ''), $2::text, 's2k-mode=1')),
           body_text = ''
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_EMAIL_UNSEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM email_outbox
        WHERE tenant_id = $1::uuid
        AND (subject_ciphertext IS NOT NULL OR body_text_ciphertext IS NOT NULL)
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE email_outbox
       SET subject = COALESCE(pgp_sym_decrypt(subject_ciphertext, $2::text), subject),
           subject_ciphertext = NULL,
           body_text = COALESCE(pgp_sym_decrypt(body_text_ciphertext, $2::text), body_text),
           body_text_ciphertext = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_TOOL_SEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM ai_tool_operations
        WHERE tenant_id = $1::uuid AND result_ciphertext IS NULL AND result IS NOT NULL
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE ai_tool_operations
       SET result_ciphertext = pgp_sym_encrypt(result::text, $2::text, 's2k-mode=1'), result = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_TOOL_UNSEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM ai_tool_operations
        WHERE tenant_id = $1::uuid AND result_ciphertext IS NOT NULL
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE ai_tool_operations
       SET result = pgp_sym_decrypt(result_ciphertext, $2::text)::jsonb, result_ciphertext = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_BELIEF_SEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM beliefs
        WHERE tenant_id = $1::uuid AND source_quote_ciphertext IS NULL
        AND COALESCE(source_quote, '') <> ''
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE beliefs
       SET source_quote_ciphertext = pgp_sym_encrypt(source_quote, $2::text, 's2k-mode=1'), source_quote = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
_BELIEF_UNSEAL = """
    WITH picked AS MATERIALIZED (
        SELECT id FROM beliefs
        WHERE tenant_id = $1::uuid AND source_quote_ciphertext IS NOT NULL
        ORDER BY id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE beliefs
       SET source_quote = pgp_sym_decrypt(source_quote_ciphertext, $2::text), source_quote_ciphertext = NULL
     WHERE id IN (SELECT id FROM picked)
    RETURNING id
"""
# user_profiles.tenant_id is text (0009); passed exactly as stored.
_PROFILE_SEAL = """
    WITH picked AS MATERIALIZED (
        SELECT user_id FROM user_profiles
        WHERE tenant_id = $1::text AND profile_summary_ciphertext IS NULL
        AND COALESCE(profile_summary, '') <> ''
        ORDER BY user_id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE user_profiles
       SET profile_summary_ciphertext = pgp_sym_encrypt(profile_summary, $2::text, 's2k-mode=1'),
           profile_summary = ''
     WHERE user_id IN (SELECT user_id FROM picked)
    RETURNING user_id
"""
_PROFILE_UNSEAL = """
    WITH picked AS MATERIALIZED (
        SELECT user_id FROM user_profiles
        WHERE tenant_id = $1::text AND profile_summary_ciphertext IS NOT NULL
        ORDER BY user_id LIMIT $3
        FOR UPDATE SKIP LOCKED)
    UPDATE user_profiles
       SET profile_summary = pgp_sym_decrypt(profile_summary_ciphertext, $2::text),
           profile_summary_ciphertext = NULL
     WHERE user_id IN (SELECT user_id FROM picked)
    RETURNING user_id
"""
_DIRECT = {  # field -> (seal, unseal), plain UPDATEs the app role may run
    "sms": (_SMS_SEAL, _SMS_UNSEAL),
    "email": (_EMAIL_SEAL, _EMAIL_UNSEAL),
    "tool_result": (_TOOL_SEAL, _TOOL_UNSEAL),
    "belief_quote": (_BELIEF_SEAL, _BELIEF_UNSEAL),
    "profile_summary": (_PROFILE_SEAL, _PROFILE_UNSEAL),
}
_FUNCTIONS = {"transcript": _TRANSCRIPT, "interaction": _INTERACTION, "interaction_log": _INTERACTION_LOG}

# Tenants with rows to seal / to unseal.
_SEAL_TENANTS = {
    "sms": "SELECT DISTINCT tenant_id::text AS t FROM sms_messages "
           "WHERE body_ciphertext IS NULL AND COALESCE(body, '') <> '' ORDER BY 1",
    "email": "SELECT DISTINCT tenant_id::text AS t FROM email_outbox WHERE body_html IS NULL "
             "AND ((subject_ciphertext IS NULL AND subject <> '') "
             "OR (body_text_ciphertext IS NULL AND body_text <> '')) ORDER BY 1",
    "interaction_log": "SELECT DISTINCT tenant_id::text AS t FROM interaction_logs "
                       "WHERE (subject_ciphertext IS NULL AND COALESCE(subject, '') <> '') "
                       "OR (content_ciphertext IS NULL "
                       "AND payload ?| ARRAY['body','text','transcript','summary','extraction']) ORDER BY 1",
    "tool_result": "SELECT DISTINCT tenant_id::text AS t FROM ai_tool_operations "
                   "WHERE result_ciphertext IS NULL AND result IS NOT NULL ORDER BY 1",
    "belief_quote": "SELECT DISTINCT tenant_id::text AS t FROM beliefs "
                    "WHERE source_quote_ciphertext IS NULL AND COALESCE(source_quote, '') <> '' ORDER BY 1",
    "profile_summary": "SELECT DISTINCT tenant_id AS t FROM user_profiles "
                       "WHERE profile_summary_ciphertext IS NULL AND COALESCE(profile_summary, '') <> '' ORDER BY 1",
    "transcript": "SELECT DISTINCT tenant_id::text AS t FROM negotiation_events "
                  "WHERE transcript_ciphertext IS NULL AND COALESCE(transcript_excerpt, '') <> '' ORDER BY 1",
    "interaction": "SELECT DISTINCT tenant_id AS t FROM user_interactions "
                   "WHERE content_ciphertext IS NULL AND content <> '' ORDER BY 1",
}
_UNSEAL_TENANTS = {
    "sms": "SELECT DISTINCT tenant_id::text AS t FROM sms_messages WHERE body_ciphertext IS NOT NULL ORDER BY 1",
    "email": "SELECT DISTINCT tenant_id::text AS t FROM email_outbox "
             "WHERE subject_ciphertext IS NOT NULL OR body_text_ciphertext IS NOT NULL ORDER BY 1",
    "interaction_log": "SELECT DISTINCT tenant_id::text AS t FROM interaction_logs "
                       "WHERE subject_ciphertext IS NOT NULL OR content_ciphertext IS NOT NULL ORDER BY 1",
    "tool_result": "SELECT DISTINCT tenant_id::text AS t FROM ai_tool_operations "
                   "WHERE result_ciphertext IS NOT NULL ORDER BY 1",
    "belief_quote": "SELECT DISTINCT tenant_id::text AS t FROM beliefs "
                    "WHERE source_quote_ciphertext IS NOT NULL ORDER BY 1",
    "profile_summary": "SELECT DISTINCT tenant_id AS t FROM user_profiles "
                       "WHERE profile_summary_ciphertext IS NOT NULL ORDER BY 1",
    "transcript": "SELECT DISTINCT tenant_id::text AS t FROM negotiation_events "
                  "WHERE transcript_ciphertext IS NOT NULL ORDER BY 1",
    "interaction": "SELECT DISTINCT tenant_id AS t FROM user_interactions "
                   "WHERE content_ciphertext IS NOT NULL ORDER BY 1",
}
_REMAINING = {
    "sms": "SELECT count(*) FROM sms_messages WHERE body_ciphertext IS NULL AND COALESCE(body, '') <> ''",
    "transcript": "SELECT count(*) FROM negotiation_events "
                  "WHERE transcript_ciphertext IS NULL AND COALESCE(transcript_excerpt, '') <> ''",
    "interaction": "SELECT count(*) FROM user_interactions WHERE content_ciphertext IS NULL AND content <> ''",
    # A row with body_html is never sealed (nothing writes one; the 0154 CHECK
    # needs it NULL on a sealed row), so it is not counted as remaining.
    "email": "SELECT count(*) FROM email_outbox WHERE body_html IS NULL "
             "AND ((subject_ciphertext IS NULL AND subject <> '') "
             "OR (body_text_ciphertext IS NULL AND body_text <> ''))",
    "interaction_log": "SELECT count(*) FROM interaction_logs "
                       "WHERE (subject_ciphertext IS NULL AND COALESCE(subject, '') <> '') "
                       "OR (content_ciphertext IS NULL "
                       "AND payload ?| ARRAY['body','text','transcript','summary','extraction'])",
    "tool_result": "SELECT count(*) FROM ai_tool_operations WHERE result_ciphertext IS NULL AND result IS NOT NULL",
    "belief_quote": "SELECT count(*) FROM beliefs "
                    "WHERE source_quote_ciphertext IS NULL AND COALESCE(source_quote, '') <> ''",
    "profile_summary": "SELECT count(*) FROM user_profiles "
                       "WHERE profile_summary_ciphertext IS NULL AND COALESCE(profile_summary, '') <> ''",
}
FIELDS = tuple(_REMAINING)


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext(agent_id=_ACTOR, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


async def _batch(field: str, tenant_id: str, key: str, size: int, unseal: bool) -> int:
    async with tenant_tx(_ctx(tenant_id)) as conn:
        if field in _FUNCTIONS:
            return int(await conn.fetchval(_FUNCTIONS[field], tenant_id, key, size, unseal) or 0)
        seal, undo = _DIRECT[field]
        return len(await conn.fetch(undo if unseal else seal, tenant_id, key, size))


async def tenants_pending(field: str, *, unseal: bool = False) -> list[str]:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return [str(r["t"]) for r in await conn.fetch((_UNSEAL_TENANTS if unseal else _SEAL_TENANTS)[field])]


async def remaining(field: str) -> int:
    async with tenant_tx(_ctx(_PLATFORM_TENANT)) as conn:
        return int(await conn.fetchval(_REMAINING[field]))


async def backfill_field(field: str, *, batch_size: int, unseal: bool = False,
                         max_rows: int | None = None) -> dict:
    """Seal (or unseal) one field across every tenant. Returns counts and the
    measured time; `max_rows` stops after about that many (whole batches)."""
    started = time.monotonic()
    changed, skipped = 0, []
    for tenant_id in await tenants_pending(field, unseal=unseal):
        if not _is_uuid(tenant_id):
            skipped.append(tenant_id)  # text tenant id (user_interactions, user_profiles): no context can own it
            continue
        key = sealed_text.key_for(tenant_id)
        while max_rows is None or changed < max_rows:
            t0 = time.monotonic()
            done = await _batch(field, tenant_id, key, batch_size, unseal)
            if not done:
                break
            changed += done
            ms = (time.monotonic() - t0) * 1000
            print(f"{field} tenant {tenant_id}: {'unsealed' if unseal else 'sealed'} {done} row(s) "
                  f"in {ms:.0f} ms ({changed} total)", flush=True)
        if max_rows is not None and changed >= max_rows:
            break
    seconds = time.monotonic() - started
    return {"field": field, "changed": changed, "seconds": round(seconds, 2),
            "rows_per_second": round(changed / seconds, 1) if seconds and changed else None,
            "skipped_tenants": len(skipped)}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--field", choices=FIELDS + ("all",), default="all")
    parser.add_argument("--unseal", action="store_true", help="rollback: write the plaintext back")
    parser.add_argument("--count", action="store_true", help="print remaining plaintext rows; change nothing")
    parser.add_argument("--max-rows", type=int, default=None, help="stop after about N rows per field")
    args = parser.parse_args(argv)
    if not os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "").strip():
        print("ORACLE_ENCRYPTION_MASTER_KEY is required", file=sys.stderr)
        return 2
    batch_size = int(os.getenv("ORACLE_SEALED_TEXT_BACKFILL_BATCH", "500"))
    if not 1 <= batch_size <= 5_000:
        print("ORACLE_SEALED_TEXT_BACKFILL_BATCH must be 1..5000", file=sys.stderr)
        return 2
    fields = FIELDS if args.field == "all" else (args.field,)
    await init_pool(min_size=1, max_size=2)
    left = 0
    try:
        for field in fields:
            if not args.count:
                print(await backfill_field(field, batch_size=batch_size, unseal=args.unseal,
                                           max_rows=args.max_rows), flush=True)
            if not args.unseal:
                n = await remaining(field)
                left += n
                print(f"{field}: {n} plaintext row(s) remain", flush=True)
    finally:
        await close_pool()
    if left and not args.count and args.max_rows is None:
        print(f"{left} plaintext row(s) remain — run again", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
