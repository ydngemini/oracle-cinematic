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
or address is still blocked. Their outbound drafts, the approvals of those
drafts and the jobs that would send them are redacted to the record that an
action happened, not deleted (migration 0182).
"""

from __future__ import annotations

import json
import uuid as uuid_mod
from typing import Any, Optional

from db.connection import tenant_tx
from retention_policy import BACKUP_DAYS as _BACKUP_DAYS
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

# Every table a directive may name (privacy_erase_subject_rows' allow-list;
# the _REDACTED ones are redacted, the rest deleted).
_SUBJECT_TABLES = frozenset({t for t, _ in _DEPENDENTS} | {
    "sms_messages", "inbound_voice_calls", "email_outbox", "outreach_attempt_log",
    "outreach_consent", "outreach_suppression", "clients", "agent_contacts",
    "command_executions", "action_approvals", "automation_jobs"})

# Records attached to a client that go with it (ON DELETE CASCADE): returned
# by an access request, removed with the client by a deletion (external audit
# 29.8 follow-up: they were neither discovered nor returned).
_CLIENT_CHILDREN = ("client_notes", "client_tasks", "client_activities", "client_tags",
                    "buyer_profiles", "showings", "interaction_logs",
                    "contract_synthesis_artifacts", "property_match_decisions")

# Outbound drafts that name the person, their approvals and the execute jobs
# carrying a copy of the approved draft: returned by access; a deletion
# REDACTS them (0182, owner decision 2026-11-20 on L17-38) instead of leaving
# them unlinked. The action skeleton stays as the brokerage's record that an
# action happened (type, channel, state, times, who asked and who decided,
# the provider's message id); the message, recipient and any free text are
# replaced by _REDACTION_MARKER. Value: the column that carries the marker.
_REDACTED = {"command_executions": "draft", "action_approvals": "draft_payload",
             "automation_jobs": "payload"}
_REDACTION_MARKER = "data_subject_erasure"   # {"redacted": <this>, "at": <time>}
# Commands first: their rows are locked before their jobs (a release locks the
# command, then queues its job), so a job queued after discovery is caught.
_REDACT_ORDER = ("command_executions", "automation_jobs", "action_approvals")


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def redaction_note(redacted: dict[str, int], counts: dict[str, int]) -> str:
    """What a deletion redacted and what it did not reach, from the counts
    privacy_erase_subject_rows recorded (0182). Never a fixed claim: each
    sentence is said only when its count is not zero."""
    drafts, approvals = redacted.get("command_executions", 0), redacted.get("action_approvals", 0)
    jobs, traces = redacted.get("automation_jobs", 0), counts.get("decision_traces", 0)
    if not (drafts or approvals or jobs or traces):
        return ""
    where = [_n(drafts, "draft", "drafts"), _n(approvals, "approval", "approvals"),
             _n(jobs, "send job", "send jobs")]
    if counts.get("job_attempt_errors"):
        where.append("the error text of " + _n(counts["job_attempt_errors"], "send attempt", "send attempts"))
    if traces:
        where.append(_n(traces, "AI decision record", "AI decision records")
                     + " (also withdrawn from AI training data)")
    out = [("Redacted, not deleted: the message, the recipient's address or number, names and free-text "
            "notes and errors were removed from " + ", ".join(where) + ". Kept as the brokerage's record "
            "that an action happened: its type and channel, status, times, who asked for it and who "
            "decided, and the provider's message id.")]
    for key, one, many in (
            ("sent", "had already been sent: the message stays with its recipient.",
             "had already been sent: those messages stay with their recipients."),
            ("unsent_cancelled", "had not been sent and was cancelled; it will not be sent.",
             "had not been sent and were cancelled; they will not be sent."),
            ("in_flight", "was being sent at the moment of deletion: its content is redacted here, but that "
                          "send may still go out and its delivery record may be written afterwards.",
             "were being sent at the moment of deletion: their content is redacted here, but those sends "
             "may still go out and their delivery records may be written afterwards."),
            ("uncertain", "has an unconfirmed delivery (being checked with the provider) and may have "
                          "reached the recipient.",
             "have an unconfirmed delivery (being checked with the provider) and may have reached the "
             "recipient."),
            ("not_sent", "had failed or been cancelled earlier and was not sent.",
             "had failed or been cancelled earlier and were not sent.")):
        if counts.get(key):
            out.append(f"{_n(counts[key], 'draft', 'drafts')} {one if counts[key] == 1 else many}")
    if counts.get("approvals_revoked"):
        out.append(f"{_n(counts['approvals_revoked'], 'approval', 'approvals')} still waiting "
                   f"{'was' if counts['approvals_revoked'] == 1 else 'were'} revoked.")
    if counts.get("jobs_cancelled"):
        out.append(f"{_n(counts['jobs_cancelled'], 'waiting send job', 'waiting send jobs')} "
                   f"{'was' if counts['jobs_cancelled'] == 1 else 'were'} cancelled.")
    out.append("Not reached by this deletion: the providers' own records (email relay, text and call "
               "providers), messages already delivered, the approver's reason in the brokerage's audit "
               "log (append-only and hash-chained), and anything typed into notes or chats (see the caveat).")
    return " ".join(out)


async def _redaction_counts(conn, op_id: str) -> tuple[dict[str, int], dict[str, int]]:
    """(rows redacted per table, counts by state) for one operation, read back
    from the erasure_ledger rows privacy_erase_subject_rows wrote (0182)."""
    redacted: dict[str, int] = {}
    counts: dict[str, int] = {}

    def add(target: dict, key: str, value) -> None:
        if int(value or 0):
            target[key] = target.get(key, 0) + int(value or 0)

    for row in await conn.fetch(
            "SELECT target, rows_affected, detail FROM erasure_ledger WHERE operation_id=$1::uuid "
            "AND phase='subject' AND action='anonymized' ORDER BY id", op_id):
        detail = row["detail"]
        detail = json.loads(detail) if isinstance(detail, str) else dict(detail or {})
        jobs = detail.get("jobs") if isinstance(detail.get("jobs"), dict) else {}
        add(redacted, row["target"], row["rows_affected"])
        add(redacted, "automation_jobs", jobs.get("jobs"))
        for key in ("sent", "unsent_cancelled", "in_flight", "uncertain", "not_sent", "decision_traces"):
            add(counts, key, detail.get(key))
        add(counts, "approvals_revoked", detail.get("pending_revoked"))
        add(counts, "jobs_cancelled", detail.get("jobs_cancelled"))
        add(counts, "jobs_cancelled", jobs.get("jobs_cancelled"))
        add(counts, "job_attempt_errors", detail.get("attempt_errors"))
        add(counts, "job_attempt_errors", jobs.get("attempt_errors"))
    return redacted, counts


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
    for table in _CLIENT_CHILDREN:
        found[table] = [r[0] for r in await conn.fetch(
            f"SELECT id FROM {table} WHERE tenant_id=$1 AND client_id = ANY($2::uuid[])", tenant_id, clients)]
    # Drafts and approvals: by their link to the person's records (0142/0179);
    # one linked to nobody (EMAIL to an address alone, or a record deleted
    # earlier) only when its recipient is one of the person's own addresses.
    # A draft linked to someone else is never matched by address.
    emails = [i for i in identifiers if "@" in i]
    phones = [i for i in identifiers if i.startswith("+")]
    for table, target in (("command_executions", "target"), ("action_approvals", "(draft_payload -> 'target')")):
        found[table] = [r[0] for r in await conn.fetch(
            f"SELECT id FROM {table} WHERE tenant_id=$1 "
            "AND (client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[]) "
            "     OR (client_id IS NULL AND contact_id IS NULL "
            f"        AND (lower(btrim({target} ->> 'email')) = ANY($4::text[]) "
            f"             OR btrim({target} ->> 'phone') = ANY($5::text[]))))",
            tenant_id, clients, contacts, emails, phones)]
    # The execute jobs of those commands: each carries a copy of the approved draft.
    found["automation_jobs"] = [r[0] for r in await conn.fetch(
        "SELECT id FROM automation_jobs WHERE tenant_id=$1 AND job_type='command:execute' "
        "AND payload->>'command_id' = ANY($2::text[])",
        tenant_id, [str(i) for i in found["command_executions"]])] if found["command_executions"] else []
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
    (clients and contacts, with their links). Any two that reach different
    record sets are refused before anything is read or deleted. An email or
    phone that reaches no CRM record still matches texts, calls, emails and
    consent rows by its raw value, so it must be one of the addresses on the
    records the other identifiers reach; and when none of several identifiers
    reaches a record, nothing ties them to one person (review of 29.3). Two
    rows of the same person that are not linked to each other are refused
    too: the owner can link them, or ask by record id."""
    from contact_truth import normalize_email, normalize_phone
    from privacy_lifecycle import LifecycleError

    def refuse(detail: str) -> None:
        raise LifecycleError(
            f"ambiguous_subject: {detail}. Nothing was read or deleted. "
            "Send one request per person, or name the record by its id.", status_code=409)

    given = [(label, kw) for label, kw in (
        ("email", {"email": email}), ("phone", {"phone": phone}),
        ("contact_id", {"contact_id": contact_id}), ("client_id", {"client_id": client_id}))
        if next(iter(kw.values()))]
    if len(given) < 2:
        return
    reached: dict[str, frozenset[str]] = {}
    known_addresses: set[str] = set()
    unreached: list[tuple[str, dict]] = []
    for label, kw in given:
        found = await _discover(conn, tenant_id, kw.get("email"), kw.get("phone"),
                                kw.get("contact_id"), kw.get("client_id"))
        records = frozenset([f"client:{i}" for i in found["ids"]["clients"]]
                            + [f"contact:{i}" for i in found["ids"]["agent_contacts"]])
        if records:
            reached[label] = records
            known_addresses.update(found["identifiers"])
        else:
            unreached.append((label, kw))
    if len(set(reached.values())) > 1:
        refuse("these identifiers point to different records ("
               + ", ".join(f"the {label} reaches {len(r)} record(s)" for label, r in reached.items()) + ")")
    if not reached:
        refuse("none of these identifiers reaches a client or contact, so nothing shows they are one person")
    for label, kw in unreached:
        value = kw.get("email") or kw.get("phone")
        if value is None:
            continue  # a record id that names nothing reads and deletes nothing
        try:
            norm = normalize_email(value) if label == "email" else normalize_phone(value)
        except ValueError:
            norm = None
        if norm not in known_addresses:
            refuse(f"the {label} is not on the records the other identifiers reach")


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
            try:
                async with tenant_tx(ctx) as conn:
                    await _finish_operation(conn, op_id, state="failed", result={"matches": counts},
                                            error=f"access bundle incomplete: {type(exc).__name__}")
            except Exception:  # noqa: BLE001 - never mask the original failure
                import logging

                logging.getLogger(__name__).exception("could not mark access request %s failed", op_id)
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

    # Write-ahead restore directive (external audit 29.4): a restore from a
    # backup taken before this deletion brings the person back, and nothing
    # outside the database remembered to delete them again. Saved (and read
    # back) BEFORE anything is deleted; if it cannot be saved, nothing is.
    import asyncio

    from privacy_lifecycle import contact_hmac

    from datetime import datetime, timezone

    directive = {
        "kind": "subject", "state": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": ctx.tenant_id, "operation_id": op_id, "subject_ref": ref,
        "ids": {t: [str(i) for i in ids] for t, ids in found["ids"].items() if ids},
        "tombstones": sorted(contact_hmac(ctx.tenant_id, c) for c in found["identifiers"]),
    }
    # Contract PDFs live in the vault, referenced only by these rows, which the
    # deletion cascades away: collect their keys first (review of 29.8b).
    async with tenant_tx(ctx) as conn:
        vault_keys = [r[0] for r in await conn.fetch(
            "SELECT s3_key FROM contract_synthesis_artifacts WHERE tenant_id=$1 AND id = ANY($2::uuid[]) "
            "AND s3_key IS NOT NULL", ctx.tenant_id, found["ids"].get("contract_synthesis_artifacts") or [])]
    key = await asyncio.to_thread(_write_subject_directive, op_id, directive)
    if key is None:
        async with tenant_tx(ctx) as conn:
            await _finish_operation(conn, op_id, state="failed", result={"matches": counts},
                                    error="restore_directive_unwritten")
        raise LifecycleError(
            "Nothing was deleted: the record that re-applies this deletion after a backup restore "
            "could not be saved. Try again shortly.", status_code=503)
    try:
        deleted, redacted, redaction_counts = await _delete_subject(ctx, op_id, found)
    except Exception:
        # Nothing was deleted (one transaction): drop the directive so a later
        # restore does not delete what the brokerage still holds.
        await asyncio.to_thread(_drop_subject_directive, key)
        async with tenant_tx(ctx) as conn:
            await _finish_operation(conn, op_id, state="failed", result={"matches": counts},
                                    error="deletion failed; nothing was deleted")
        raise
    # Only a committed directive is ever re-applied (review): a pending one
    # may belong to a deletion that never happened.
    committed = await asyncio.to_thread(_write_subject_directive, op_id, {**directive, "state": "committed"})
    files_deleted, files_left = (await asyncio.to_thread(_delete_vault_objects, vault_keys)) if vault_keys else (0, 0)
    removed_with_client = {t: len(found["ids"].get(t) or []) for t in _CLIENT_CHILDREN if found["ids"].get(t)}
    receipt = {"operation_id": op_id, "deleted": deleted, "opt_out_kept_as_keyed_hash": True,
               "removed_with_client": removed_with_client,
               # Said, not hidden: drafts, approvals and execute jobs that
               # named the person are redacted, not deleted (0182).
               "redacted": redacted,
               **({"redaction_counts": redaction_counts,
                   "redaction": redaction_note(redacted, redaction_counts)} if redacted else {}),
               **({"contract_files": {"deleted": files_deleted, "not_deleted": files_left,
                                      **({"note": "These encrypted contract files could not be removed from "
                                                  "the contract vault; Neoh support must delete them."}
                                         if files_left else {})}} if vault_keys else {}),
               "caveat": caveat, "restore_directive": key,
               "restore_directive_state": "committed" if committed else "pending_review",
               "backups": ("Copies remain in encrypted database backups for up to 7 days. If one is "
                           "restored, this deletion is re-applied before the service reopens "
                           "(scripts/reapply-erasures.py).")}
    async with tenant_tx(ctx) as conn:
        await _finish_operation(conn, op_id, state="succeeded",
                                result={"deleted": deleted, "redacted": redacted,
                                        "restore_directive_state": receipt["restore_directive_state"]},
                                receipt=receipt)
    await _audit(ctx, "privacy.subject.deleted", target=op_id, metadata={"deleted": deleted, "redacted": redacted})
    return {"operation_id": op_id, "kind": kind, **receipt}


async def _delete_subject(ctx: TenantContext, op_id: str,
                          found: dict) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    from privacy_lifecycle import contact_hmac

    async with tenant_tx(ctx) as conn:
        return await _erase_subject_rows(
            conn, ctx.tenant_id, op_id, found["ids"],
            [contact_hmac(ctx.tenant_id, c) for c in found["identifiers"]])


async def _erase_subject_rows(conn, tenant_id: str, op_id: str, ids: dict[str, list],
                              tombstones: list[str]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Delete one person's rows and redact their drafts, approvals and execute
    jobs (privacy_erase_subject_rows, under a running dsr_delete operation),
    and keep their opt-out as keyed hashes. Shared by the request and by
    reapply_subject_erasures after a restore. Returns (deleted, redacted,
    redaction counts by state)."""
    deleted: dict[str, int] = {}
    # The opt-out outlives the person, as a keyed hash.
    for hmac_value in tombstones:
        await conn.execute(
            "INSERT INTO suppression_tombstones (tenant_id, contact_hmac, channel, reason) "
            "VALUES ($1,$2,'*','erased_by_request') ON CONFLICT DO NOTHING",
            tenant_id, hmac_value)
    # Redacted, not deleted (0182): content and free text replaced by the
    # marker, an unsent draft cancelled, a pending approval revoked. The
    # counts come back from the erasure ledger rows the function wrote.
    for table in _REDACT_ORDER:
        if ids.get(table):
            await conn.fetchval("SELECT privacy_erase_subject_rows($1,$2,$3::uuid[])", op_id, table, ids[table])
    redacted, redaction_counts = await _redaction_counts(conn, op_id)
    order = [t for t, _ in _DEPENDENTS] + [
        "sms_messages", "inbound_voice_calls", "email_outbox",
        "outreach_attempt_log", "outreach_consent", "outreach_suppression"]
    for table in order:
        if ids.get(table):
            deleted[table] = int(await conn.fetchval(
                "SELECT privacy_erase_subject_rows($1,$2,$3::uuid[])", op_id, table, ids[table]) or 0)
    # Break the clients <-> agent_contacts reference cycle, then delete both.
    await conn.execute("UPDATE agent_contacts SET legacy_client_id=NULL WHERE tenant_id=$1 AND id = ANY($2::uuid[])",
                       tenant_id, ids.get("agent_contacts") or [])
    await conn.execute("UPDATE clients SET contact_id=NULL WHERE tenant_id=$1 AND id = ANY($2::uuid[])",
                       tenant_id, ids.get("clients") or [])
    for table in ("clients", "agent_contacts"):
        if ids.get(table):
            deleted[table] = int(await conn.fetchval(
                "SELECT privacy_erase_subject_rows($1,$2,$3::uuid[])", op_id, table, ids[table]) or 0)
    return deleted, redacted, redaction_counts


def _delete_vault_objects(keys: list[str]) -> tuple[int, int]:
    """(deleted, not deleted) for contract PDFs in the vault. Never raises:
    the receipt reports what is left."""
    import logging

    try:
        from contract_vault import SovereignVault as ContractVault

        vault = ContractVault()
    except Exception:  # noqa: BLE001 - vault not configured: everything is left, and said
        logging.getLogger(__name__).warning("contract vault unavailable; %d file(s) left", len(keys))
        return 0, len(keys)
    deleted = left = 0
    for k in keys:
        try:
            vault.s3_client.delete_object(Bucket=vault.bucket_name, Key=k)
            deleted += 1
        except Exception:  # noqa: BLE001
            left += 1
    return deleted, left


SUBJECT_DIRECTIVES = "privacy/subject-erasure-directives/"
# Kept past the longest backup a restore could bring back, then deleted.
SUBJECT_DIRECTIVE_DAYS = _BACKUP_DAYS + 23
# Never under privacy/erasure-directives/: reapply_erasures reads every
# directive there as a WHOLE-BROKERAGE erasure.


def _write_subject_directive(op_id: str, directive: dict) -> Optional[str]:
    """Save the directive (row ids and keyed hashes; no customer data) and
    read it back. Returns its key, or None when it is not safely stored."""
    import logging

    import object_storage

    key = f"{SUBJECT_DIRECTIVES}{op_id}.json"
    body = json.dumps(directive, sort_keys=True).encode()
    try:
        if not object_storage.is_configured():
            return None
        object_storage.put_bytes(key, body, "application/json")
        return key if object_storage.get_bytes(key) == body else None
    except Exception:  # noqa: BLE001 - the request refuses instead
        logging.getLogger(__name__).exception("subject erasure directive write failed")
        return None


def _drop_subject_directive(key: str) -> None:
    import logging

    import object_storage

    try:
        object_storage.delete_object(key)
    except Exception:  # noqa: BLE001 - a stray directive only re-deletes what the person asked to delete
        logging.getLogger(__name__).exception("could not remove unused directive %s", key)


async def reapply_subject_erasures(*, apply: bool) -> list[dict]:
    """After a database restore: find people deleted by request whom the
    restore brought back (from the directives in object storage) and, with
    apply=True, delete them again under a new dsr_delete operation.
    scripts/reapply-erasures.py runs this after reapply_erasures."""
    import asyncio

    import object_storage
    from privacy_lifecycle import LifecycleError, _platform_ctx

    if not object_storage.is_configured():
        raise LifecycleError("object storage is not configured; erasure directives are unreadable", status_code=503)
    keys = await asyncio.to_thread(object_storage.list_prefix, SUBJECT_DIRECTIVES)
    ctx = _platform_ctx("reapply-erasures")
    report: list[dict] = []
    for key in keys:
        try:
            report.append(await _reapply_one(ctx, key, apply))
        except Exception as exc:  # noqa: BLE001 - one bad directive never stops the run
            report.append({"kind": "subject", "directive": key, "status": "reapply_failed",
                           "detail": type(exc).__name__})
    return report


def _directive_expired(d: dict) -> bool:
    from datetime import datetime, timedelta, timezone

    try:
        made = datetime.fromisoformat(str(d["created_at"]))
    except (KeyError, ValueError):
        return True  # undated (pre-release format, never committed): expire it
    return datetime.now(timezone.utc) - made > timedelta(days=SUBJECT_DIRECTIVE_DAYS)


async def _reapply_one(ctx, key: str, apply: bool) -> dict:
    import asyncio

    import object_storage
    from privacy_lifecycle import _create_operation, _finish_operation, active_legal_hold

    try:
        d = json.loads(await asyncio.to_thread(object_storage.get_bytes, key))
        tenant = str(d["tenant_id"])
        ids = {str(t): [str(uuid_mod.UUID(str(i))) for i in v]
               for t, v in d["ids"].items() if str(t) in _SUBJECT_TABLES}
        tombstones = [str(h) for h in d.get("tombstones") or []]
    except Exception as exc:  # noqa: BLE001 - reported, never skipped silently
        return {"kind": "subject", "directive": key, "status": "unreadable", "detail": type(exc).__name__}
    entry: dict[str, Any] = {"kind": "subject", "directive": key, "tenant_id": tenant,
                             "operation_id": d.get("operation_id")}
    async with tenant_tx(ctx) as conn:
        state = await conn.fetchval("SELECT lifecycle_state FROM tenants WHERE id=$1", tenant)
        # Child rows the restore brought back that point at the directive's
        # OWN client/contact ids but are not listed (e.g. removed between the
        # backup and the request) would block the delete (RESTRICT). Only those
        # are added: never another client or contact, never a match by address
        # (review: the restored data may still carry another person's number).
        for table, rows in (await _linked_children(conn, tenant, ids.get("clients", []),
                                                   ids.get("agent_contacts", []))).items():
            ids[table] = sorted(set(ids.get(table, [])) | set(rows))
        back = {}
        for table, table_ids in ids.items():
            # A redacted row is still there by design: back only if the
            # restore brought its content back.
            unredacted = (f" AND ({_REDACTED[table]} ->> 'redacted') IS DISTINCT FROM '{_REDACTION_MARKER}'"
                          if table in _REDACTED else "")
            n = int(await conn.fetchval(
                f"SELECT count(*) FROM public.{table} WHERE tenant_id=$1 AND id = ANY($2::uuid[]){unredacted}",
                tenant, table_ids) or 0)
            if n:
                back[table] = n
        have = set(await conn.fetchval(
            "SELECT coalesce(array_agg(contact_hmac), '{}') FROM suppression_tombstones "
            "WHERE tenant_id=$1 AND contact_hmac = ANY($2::text[])", tenant, tombstones))
        hold = (await active_legal_hold(conn, tenant)
                or await active_legal_hold(conn, tenant, scope="contact", subject_ref=d.get("subject_ref")))
    entry.update(rows_back=back, tombstones_missing=len(set(tombstones) - have))
    if _directive_expired(d):
        # Older than any backup a restore could use: never applied, and
        # expire_subject_directives removes it. Rows back means an older
        # restore than policy allows: a person must look.
        entry["status"] = "expired_rows_back" if back else "expired"
        return entry
    if state is None or state == "erased":
        entry["status"] = "still_erased" if not back else "tenant_erased_rows_back"
        return entry
    if not (back or entry["tombstones_missing"]):
        entry["status"] = "still_erased"
        return entry
    if d.get("state") != "committed":
        # The deletion may never have committed (a crash, or a failure whose
        # cleanup also failed): the brokerage was told nothing was deleted.
        entry["status"] = "needs_review"
        return entry
    entry["status"] = "resurrected"
    if not apply:
        return entry
    if hold:
        entry["status"] = "skipped_legal_hold"
        return entry
    op_id = None
    try:
        async with tenant_tx(ctx) as conn:
            op = await _create_operation(
                conn, tenant_id=tenant, kind="dsr_delete", requested_by="reapply-erasures",
                subject_kind="contact", subject_ref=d.get("subject_ref"), state="running",
                reason="re-applied after database restore",
                params={"reapply_of": d.get("operation_id")})
        op_id = str(op["id"])
        async with tenant_tx(ctx) as conn:
            deleted, redacted, _counts = await _erase_subject_rows(conn, tenant, op_id, ids, tombstones)
            await _finish_operation(conn, op_id, state="succeeded",
                                    result={"deleted": deleted, "redacted": redacted,
                                            "reapply_of": d.get("operation_id")})
        entry.update(status="reapplied", reapply_operation_id=op_id, deleted=deleted, redacted=redacted)
    except Exception as exc:  # noqa: BLE001 - legal hold or a DB error: needs a person
        held = getattr(exc, "hint", None) == "legal_hold"
        entry.update(status="skipped_legal_hold" if held else "reapply_failed", detail=type(exc).__name__)
        if op_id:
            async with tenant_tx(ctx) as conn:
                await _finish_operation(conn, op_id, state="blocked_legal_hold" if held else "failed",
                                        result={"reapply_of": d.get("operation_id")},
                                        error=type(exc).__name__)
    return entry


async def _linked_children(conn, tenant_id: str, clients: list, contacts: list) -> dict[str, list[str]]:
    """Ids of rows that reference these exact clients/contacts by foreign key."""
    from privacy_lifecycle import _bind_used

    out: dict[str, list[str]] = {}
    if not clients and not contacts:
        return out
    queries = [(t, f"SELECT id FROM {t} WHERE tenant_id=$1 AND ({w})") for t, w in _DEPENDENTS] + [
        ("sms_messages", "SELECT id FROM sms_messages WHERE tenant_id=$1 "
                         "AND (client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[]))"),
        ("inbound_voice_calls", "SELECT id FROM inbound_voice_calls WHERE tenant_id=$1 "
                                "AND (client_id = ANY($2::uuid[]) OR contact_id = ANY($3::uuid[]))"),
        ("email_outbox", "SELECT id FROM email_outbox WHERE tenant_id=$1 AND client_id = ANY($2::uuid[])"),
    ]
    for table, sql in queries:
        rows = await conn.fetch(*_bind_used(sql, (tenant_id, clients, contacts)))
        if rows:
            out[table] = [str(r[0]) for r in rows]
    return out


async def expire_subject_directives() -> int:
    """Delete subject directives older than any backup a restore could use
    (BACKUP_DAYS plus margin): they hold row ids and keyed hashes, and
    re-applying an old one would restart an expired opt-out's clock (review).
    Run by the periodic privacy task."""
    import asyncio

    import object_storage

    if not object_storage.is_configured():
        return 0
    removed = 0
    for key in await asyncio.to_thread(object_storage.list_prefix, SUBJECT_DIRECTIVES):
        try:
            d = json.loads(await asyncio.to_thread(object_storage.get_bytes, key))
        except Exception:  # noqa: BLE001 - unreadable: left for reapply to report
            continue
        if _directive_expired(d):
            try:
                await asyncio.to_thread(object_storage.delete_object, key)
            except Exception as exc:  # noqa: BLE001 - one refusal must not stop the rest; retried next run
                import logging

                logging.getLogger(__name__).warning(
                    "subject directive %s could not be deleted (%s)", key, type(exc).__name__)
                continue
            removed += 1
    return removed


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
        # 0183: the evidence status and reference are about this person's consent;
        # recorded_by (the staff member who recorded it) is not exported.
        "outreach_consent": "SELECT id, contact, channel, consent_type, proof_source, evidence_status, "
                            "evidence_ref, captured_at, created_at, revoked_at FROM outreach_consent",
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
    if ids.get("clients"):
        # The AI's summary and scoring of the person, keyed by client (no id).
        rows = await conn.fetch(
            "SELECT client_id, enabled, status, score_breakdown, normalized_preferences, summary, "
            "next_actions, data_gaps, evidence, property_candidates, model_id, last_evaluated_at, "
            "created_at, updated_at FROM client_ai_state WHERE tenant_id=$1 AND client_id = ANY($2::uuid[])",
            tenant_id, ids["clients"])
        if rows:
            out["client_ai_state"] = [json.loads(json.dumps(dict(r), default=str)) for r in rows]
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
