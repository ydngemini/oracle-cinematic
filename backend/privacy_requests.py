"""Data-subject requests: one person asks a brokerage what it holds on them
(access) or to delete it (deletion).

The brokerage is the controller and decides; Neoh is the processor and
carries it out. Discovery is by identifier — the person's email and/or phone
— across every place an identifier is stored (docs/privacy-data-map.md):
CRM clients, encrypted contacts (via the keyed lookup hash), text messages,
calls, emails, consent records. Free-text mentions (a name inside a note or a
chat message) cannot be found by identifier; the result says so rather than
implying completeness.

Deletion is all-or-nothing per request and refuses when the person appears
in a transaction, offer, contract or call session: those are the brokerage's
own legally required records (TREC 22 TAC §535.2 four years, Maryland five,
DE/PA three) and the decision to destroy them is counsel's, not ours. Their
opt-out survives deletion as a keyed hash, so a re-import of the same number
or address is still blocked.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import TenantContext

# Children that would block (RESTRICT) deleting a client or contact; removed
# first, in this order. (table, where-clause over $1 tenant, $2 client ids, $3 contact ids)
_DEPENDENTS: tuple[tuple[str, str], ...] = (
    ("smart_plan_step_runs", "enrollment_id IN (SELECT id FROM smart_plan_enrollments "
                             "WHERE tenant_id=$1 AND contact_id = ANY($3::uuid[]))"),
    ("smart_plan_enrollments", "contact_id = ANY($3::uuid[])"),
    ("agent_call_intents", "contact_id = ANY($3::uuid[])"),
    ("lead_intake_events", "contact_id = ANY($3::uuid[])"),
    ("contact_nurture_jobs", "contact_id = ANY($3::uuid[])"),
    ("intake_handoff_tasks", "client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[])"),
    ("contact_intake_sessions", "client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[])"),
    ("contact_property_relationships", "client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[])"),
)

# Broker records that name the person and are never deleted by a request.
_RETAINED = (
    ("transactions", "client_id = ANY($2::uuid[])"),
    ("transaction_parties", "client_id = ANY($2::uuid[])"),
    ("live_call_sessions", "client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[])"),
)


async def _discover(conn, tenant_id: str, email: Optional[str], phone: Optional[str],
                    contact_id: Optional[str] = None, client_id: Optional[str] = None) -> dict[str, Any]:
    from contact_truth import lookup_hash, normalize_email, normalize_phone

    e = normalize_email(email) if email else None
    p = normalize_phone(phone) if phone else None
    he = lookup_hash(tenant_id, "email", e) if e else None
    hp = lookup_hash(tenant_id, "phone", p) if p else None
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

    clients = [r[0] for r in await conn.fetch(
        """
        SELECT id FROM clients WHERE tenant_id=$1 AND (
              ($2::text IS NOT NULL AND lower(email) = $2)
           OR ($3::text <> '' AND regexp_replace(coalesce(phone,''), '\\D', '', 'g') IN ($3, '1' || $3)))
        """, tenant_id, e, national)]
    if client_id:
        clients += [r[0] for r in await conn.fetch(
            "SELECT id FROM clients WHERE tenant_id=$1 AND id=$2::uuid", tenant_id, client_id)]
    contacts = [r[0] for r in await conn.fetch(
        """
        SELECT id FROM agent_contacts WHERE tenant_id=$1 AND (
              ($2::text IS NOT NULL AND email_lookup_hash = $2)
           OR ($3::text IS NOT NULL AND phone_lookup_hash = $3)
           OR legacy_client_id = ANY($4::uuid[]))
        """, tenant_id, he, hp, clients)]
    if contact_id:
        contacts += [r[0] for r in await conn.fetch(
            "SELECT id FROM agent_contacts WHERE tenant_id=$1 AND id=$2::uuid", tenant_id, contact_id)]
        contacts = sorted(set(contacts), key=str)
    more_clients = [r[0] for r in await conn.fetch(
        "SELECT id FROM clients WHERE tenant_id=$1 AND contact_id = ANY($2::uuid[])", tenant_id, contacts)]
    clients = sorted(set(clients) | set(more_clients), key=str)
    found: dict[str, list] = {"clients": clients, "agent_contacts": contacts}
    found["sms_messages"] = [r[0] for r in await conn.fetch(
        "SELECT id FROM sms_messages WHERE tenant_id=$1 AND (($2::text IS NOT NULL AND (from_e164=$2 OR to_e164=$2)) "
        "OR client_id = ANY($3::uuid[]) OR contact_id = ANY($4::uuid[]))", tenant_id, p, clients, contacts)]
    found["inbound_voice_calls"] = [r[0] for r in await conn.fetch(
        "SELECT id FROM inbound_voice_calls WHERE tenant_id=$1 AND (($2::text IS NOT NULL AND caller_phone_lookup_hash=$2) "
        "OR client_id = ANY($3::uuid[]) OR contact_id = ANY($4::uuid[]))", tenant_id, hp, clients, contacts)]
    found["email_outbox"] = [r[0] for r in await conn.fetch(
        "SELECT id FROM email_outbox WHERE tenant_id=$1 AND (($2::text IS NOT NULL AND lower(to_email)=$2) "
        "OR client_id = ANY($3::uuid[]))", tenant_id, e, clients)]
    # Every address the person is known by — the ones asked about, and the
    # ones on their records (a request by record id names none) — so the
    # opt-out tombstone covers them all.
    found_ids: set[str] = {v for v in (e, p) if v}
    for row in await conn.fetch("SELECT email, phone FROM clients WHERE tenant_id=$1 AND id = ANY($2::uuid[])",
                                tenant_id, clients):
        for value, norm in ((row["email"], normalize_email), (row["phone"], normalize_phone)):
            try:
                if value and norm(value):
                    found_ids.add(norm(value))
            except ValueError:
                pass
    if contacts:
        from contact_truth import open_json

        for row in await conn.fetch(
                "SELECT pii_ciphertext FROM agent_contacts WHERE tenant_id=$1 AND id = ANY($2::uuid[]) "
                "AND pii_ciphertext IS NOT NULL", tenant_id, contacts):
            try:
                pii = await open_json(conn, tenant_id, row["pii_ciphertext"])
            except Exception:  # noqa: BLE001 - undecryptable contact: its identifiers stay unknown
                continue
            for key, norm in (("email", normalize_email), ("phone", normalize_phone)):
                try:
                    if pii.get(key) and norm(pii[key]):
                        found_ids.add(norm(pii[key]))
                except ValueError:
                    pass
    identifiers = sorted(found_ids)
    for table in ("outreach_consent", "outreach_suppression", "outreach_attempt_log"):
        found[table] = [r[0] for r in await conn.fetch(
            f"SELECT id FROM {table} WHERE tenant_id=$1 AND contact = ANY($2::text[])", tenant_id, identifiers)]
    from privacy_lifecycle import _bind_used

    for table, where in _DEPENDENTS:
        found[table] = [r[0] for r in await conn.fetch(*_bind_used(
            f"SELECT id FROM {table} WHERE tenant_id=$1 AND ({where})", (tenant_id, clients, contacts)))]
    retained = {}
    for table, where in _RETAINED:
        n = await conn.fetchval(*_bind_used(f"SELECT count(*) FROM {table} WHERE tenant_id=$1 AND ({where})",
                                            (tenant_id, clients, contacts)))
        if n:
            retained[table] = int(n)
    return {"ids": found, "retained": retained, "identifiers": identifiers}


async def _assert_one_subject(conn, tenant_id: str, email: Optional[str], phone: Optional[str],
                              contact_id: Optional[str], client_id: Optional[str]) -> None:
    """Every identifier given must point at the same person (external audit
    29.3). Discovery ORs them, so A's email with B's phone (or B's record id)
    used to return, or delete, both people's records under one request.

    Each identifier is resolved on its own to the CRM records it reaches
    (clients and contacts, with their links). An identifier that reaches
    nothing proves nothing either way; any two that reach different record
    sets are refused before anything is read or deleted. Two rows of the same
    person that are not linked to each other are refused too: the owner can
    link them, or ask by record id."""
    from privacy_lifecycle import LifecycleError

    given = [(label, kw) for label, kw in (
        ("email", {"email": email}), ("phone", {"phone": phone}),
        ("contact_id", {"contact_id": contact_id}), ("client_id", {"client_id": client_id}))
        if next(iter(kw.values()))]
    if len(given) < 2:
        return
    reached: dict[str, frozenset[str]] = {}
    for label, kw in given:
        found = await _discover(conn, tenant_id, kw.get("email"), kw.get("phone"),
                                kw.get("contact_id"), kw.get("client_id"))
        records = frozenset([f"client:{i}" for i in found["ids"]["clients"]]
                            + [f"contact:{i}" for i in found["ids"]["agent_contacts"]])
        if records:
            reached[label] = records
    if len(set(reached.values())) > 1:
        detail = ", ".join(f"the {label} reaches {len(records)} record(s)" for label, records in reached.items())
        raise LifecycleError(
            f"ambiguous_subject: these identifiers point to different records ({detail}). "
            "Nothing was read or deleted. Send one request per person, or name the record by its id.",
            status_code=409)


def subject_ref(tenant_id: str, email: Optional[str], phone: Optional[str]) -> str:
    """Stable keyed reference to the person, stored instead of their address."""
    from privacy_lifecycle import contact_hmac

    return contact_hmac(tenant_id, "|".join(sorted(v for v in ((email or "").lower(), phone or "") if v)))


async def handle_subject_request(ctx: TenantContext, *, kind: str, email: Optional[str],
                                 phone: Optional[str], reason: str, preview: bool,
                                 contact_id: Optional[str] = None, client_id: Optional[str] = None) -> dict:
    from privacy_lifecycle import LifecycleError, _audit, _create_operation, _finish_operation, active_legal_hold

    ref = subject_ref(ctx.tenant_id, email, phone or contact_id or client_id)
    async with tenant_tx(ctx) as conn:
        await _assert_one_subject(conn, ctx.tenant_id, email, phone, contact_id, client_id)
        found = await _discover(conn, ctx.tenant_id, email, phone, contact_id, client_id)
    counts = {t: len(ids) for t, ids in found["ids"].items() if ids}
    caveat = ("Found by email/phone only. Names or details typed into free-text notes, chat "
              "messages or documents are not discoverable by identifier; search for those manually.")
    if preview:
        return {"preview": True, "kind": kind, "matches": counts, "retained_broker_records": found["retained"],
                "caveat": caveat}

    async with tenant_tx(ctx) as conn:
        op = await _create_operation(conn, tenant_id=ctx.tenant_id, kind=kind, requested_by=ctx.agent_id,
                                     reason=reason, subject_kind="contact", subject_ref=ref, state="running")
    op_id = str(op["id"])

    if kind == "dsr_access":
        try:
            async with tenant_tx(ctx) as conn:
                data = await _access_bundle(conn, ctx.tenant_id, found["ids"])
                await _finish_operation(conn, op_id, state="succeeded", result={"matches": counts},
                                        receipt={"matches": counts, "caveat": caveat})
        except Exception as exc:
            # Fail closed (29.8): no partial answer is handed over as complete,
            # and the operation does not stay 'running'.
            async with tenant_tx(ctx) as conn:
                await _finish_operation(conn, op_id, state="failed", result={"matches": counts},
                                        error=f"access bundle incomplete: {type(exc).__name__}")
            raise
        await _audit(ctx, "privacy.subject.access", target=op_id, metadata={"matches": counts})
        return {"operation_id": op_id, "kind": kind, "matches": counts, "records": data, "caveat": caveat}

    # deletion
    async with tenant_tx(ctx) as conn:
        hold = await active_legal_hold(conn, ctx.tenant_id, scope="contact", subject_ref=ref)
    if hold:
        async with tenant_tx(ctx) as conn:
            await _finish_operation(conn, op_id, state="blocked_legal_hold", result={"matches": counts},
                                    error="legal hold")
        raise LifecycleError("This person's records are under a legal hold and cannot be deleted.", status_code=423)
    if found["retained"]:
        async with tenant_tx(ctx) as conn:
            await _finish_operation(conn, op_id, state="failed",
                                    result={"matches": counts, "retained_broker_records": found["retained"]},
                                    error="retained_broker_records")
        raise LifecycleError(
            "This person appears in transaction records the brokerage must keep by law. "
            "Nothing was deleted; ask counsel how to proceed.", status_code=409)

    deleted = await _delete_subject(ctx, op_id, found)
    receipt = {"operation_id": op_id, "deleted": deleted, "opt_out_kept_as_keyed_hash": True,
               "caveat": caveat,
               "backups": "Copies remain in encrypted database backups for up to 7 days."}
    async with tenant_tx(ctx) as conn:
        await _finish_operation(conn, op_id, state="succeeded", result={"deleted": deleted}, receipt=receipt)
    await _audit(ctx, "privacy.subject.deleted", target=op_id, metadata={"deleted": deleted})
    return {"operation_id": op_id, "kind": kind, **receipt}


async def _delete_subject(ctx: TenantContext, op_id: str, found: dict) -> dict[str, int]:
    from privacy_lifecycle import contact_hmac

    ids = found["ids"]
    deleted: dict[str, int] = {}
    async with tenant_tx(ctx) as conn:
        # The opt-out outlives the person, as a keyed hash.
        for contact in found["identifiers"]:
            await conn.execute(
                "INSERT INTO suppression_tombstones (tenant_id, contact_hmac, channel, reason) "
                "VALUES ($1,$2,'*','erased_by_request') ON CONFLICT DO NOTHING",
                ctx.tenant_id, contact_hmac(ctx.tenant_id, contact))
        order = [t for t, _ in _DEPENDENTS] + [
            "sms_messages", "inbound_voice_calls", "email_outbox",
            "outreach_attempt_log", "outreach_consent", "outreach_suppression"]
        for table in order:
            if ids.get(table):
                deleted[table] = int(await conn.fetchval(
                    "SELECT privacy_erase_subject_rows($1,$2,$3::uuid[])", op_id, table, ids[table]) or 0)
        # Break the clients <-> agent_contacts reference cycle, then delete both.
        await conn.execute("UPDATE agent_contacts SET legacy_client_id=NULL WHERE tenant_id=$1 AND id = ANY($2::uuid[])",
                           ctx.tenant_id, ids["agent_contacts"])
        await conn.execute("UPDATE clients SET contact_id=NULL WHERE tenant_id=$1 AND id = ANY($2::uuid[])",
                           ctx.tenant_id, ids["clients"])
        for table in ("clients", "agent_contacts"):
            if ids.get(table):
                deleted[table] = int(await conn.fetchval(
                    "SELECT privacy_erase_subject_rows($1,$2,$3::uuid[])", op_id, table, ids[table]) or 0)
    return deleted


async def _access_bundle(conn, tenant_id: str, ids: dict[str, list]) -> dict[str, Any]:
    """The person's records, readable. Encrypted contact fields decrypted.

    Complete or nothing (external audit 29.8): every table discovery found
    rows in is returned, in full. It used to cap each table at 5,000 rows,
    answer a failed query with no rows, skip encrypted contacts and calls
    when the key was missing, and count but never return the dependent tables
    (intake sessions, lead events, call intents, ...). Each of those now
    raises, and the request fails instead of handing over a partial answer."""
    import os

    from crypto import derive_tenant_key
    from privacy_lifecycle import LifecycleError

    out: dict[str, Any] = {}
    key = derive_tenant_key(tenant_id, os.environ["ORACLE_ENCRYPTION_MASTER_KEY"]) \
        if os.getenv("ORACLE_ENCRYPTION_MASTER_KEY") else None
    if not key and (ids.get("agent_contacts") or ids.get("inbound_voice_calls")
                    or any(ids.get(t) for t in _sealed_generic_tables())):
        raise LifecycleError("The encryption key is unavailable, so this person's encrypted records "
                             "cannot be opened. Nothing was returned.", status_code=503)
    queries = {
        "clients": "SELECT id, full_name, email, phone, company, notes, preferences, created_at FROM clients",
        # 0152 (A01): a body is sealed at rest; opened below, never silently dropped.
        "sms_messages": "SELECT id, direction, from_e164, to_e164, body, created_at, "
                        "body_ciphertext, tenant_id AS body_ciphertext_tenant_id FROM sms_messages",
        # 0154 (A01): subject and body sealed at rest; opened below.
        "email_outbox": "SELECT id, to_email, subject, body_text, status, sent_at, created_at, "
                        "subject_ciphertext, tenant_id AS subject_ciphertext_tenant_id, "
                        "body_text_ciphertext, tenant_id AS body_text_ciphertext_tenant_id FROM email_outbox",
        "outreach_consent": "SELECT id, contact, channel, consent_type, proof_source, created_at, revoked_at FROM outreach_consent",
        "outreach_suppression": "SELECT id, contact, channel, reason, created_at, lifted_at FROM outreach_suppression",
    }
    for table, sql in queries.items():
        if ids.get(table):
            rows = await conn.fetch(sql + " WHERE tenant_id=$1 AND id = ANY($2::uuid[])",
                                    tenant_id, ids[table])
            if table in ("sms_messages", "email_outbox"):
                import sealed_text

                # Outside the try: a body that cannot be opened fails the
                # request instead of leaving the person's texts out of it.
                sealed = {"sms_messages": (sealed_text.SMS_BODY,),
                          "email_outbox": (sealed_text.OUTBOX_SUBJECT, sealed_text.OUTBOX_BODY)}[table]
                rows = await sealed_text.open_fields(conn, rows, *sealed)
            out[table] = [json.loads(json.dumps(dict(r), default=str)) for r in rows]
    if ids.get("agent_contacts") and key:
        rows = await conn.fetch(
            "SELECT id, pgp_sym_decrypt(pii_ciphertext, $3) AS pii, birthday_month, birthday_day, source, created_at "
            "FROM agent_contacts WHERE tenant_id=$1 AND id = ANY($2::uuid[])", tenant_id, ids["agent_contacts"], key)
        out["agent_contacts"] = [json.loads(json.dumps(dict(r), default=str)) for r in rows]
    if ids.get("inbound_voice_calls") and key:
        rows = await conn.fetch(
            "SELECT id, created_at, CASE WHEN summary_ciphertext IS NULL THEN NULL ELSE "
            "pgp_sym_decrypt(summary_ciphertext, $3) END AS summary FROM inbound_voice_calls "
            "WHERE tenant_id=$1 AND id = ANY($2::uuid[])", tenant_id, ids["inbound_voice_calls"], key)
        out["inbound_voice_calls"] = [json.loads(json.dumps(dict(r), default=str)) for r in rows]
    for table, table_ids in ids.items():
        if table_ids and table not in out:
            out[table] = await _generic_rows(conn, tenant_id, table, table_ids, key)
    missing = sorted(t for t, table_ids in ids.items() if table_ids and t not in out)
    if missing:  # defensive: never answer as complete without them
        raise LifecycleError(f"Records were found in {', '.join(missing)} but could not be returned.",
                             status_code=500)
    return out


def _sealed_generic_tables() -> list[str]:
    from privacy_export import DECRYPT

    return [t for t in DECRYPT if t not in ("agent_contacts", "inbound_voice_calls", "sms_messages", "email_outbox")]


async def _generic_rows(conn, tenant_id: str, table: str, table_ids: list, key: Optional[str]) -> list[dict]:
    """Every column of the person's rows in a table discovery found, as the
    brokerage export writes them: sealed columns opened (privacy_export.DECRYPT),
    hashes, secrets and raw ciphertext left out (privacy_export._omit)."""
    from privacy_export import DECRYPT, _default, _omit, _q

    cols = await conn.fetch(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
        "WHERE a.attrelid = to_regclass('public.' || quote_ident($1)) AND a.attnum > 0 "
        "AND NOT a.attisdropped ORDER BY a.attnum", table)
    if not cols:
        raise RuntimeError(f"no columns for {table}")
    decrypt = DECRYPT.get(table, {})
    select = []
    for c in cols:
        name, dtype = c["attname"], c["t"]
        if name in decrypt:
            select.append(f"CASE WHEN {_q(name)} IS NULL THEN NULL ELSE "
                          f"pgp_sym_decrypt({_q(name)}, $3) END AS {_q(decrypt[name])}")
        elif not _omit(name, dtype):
            select.append(_q(name))
    sql = f"SELECT {', '.join(select)} FROM public.{_q(table)} WHERE tenant_id=$1 AND id = ANY($2::uuid[])"
    args: list[Any] = [tenant_id, table_ids]
    if any(c["attname"] in decrypt for c in cols):
        args.append(key)
    rows = await conn.fetch(sql, *args)
    return [json.loads(json.dumps(dict(r), default=_default)) for r in rows]
