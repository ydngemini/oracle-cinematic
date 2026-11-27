"""The AI call-notes service: what the AI may keep from a call, and where.

Moved verbatim out of commands_api.py (external audit 35-02, slice 3): the
callee's consent answer (record_call_note_consent, which withdraws notes
already saved when the answer is no), each note written as it is taken
(save_call_note, returning a CallNoteWrite), delivery at hang-up
(deliver_call_notes), the requester's content-free task when notes were not
kept, who may receive the live CALL_NOTES frame (call_note_recipients), and
their helpers, SQL and limits. Every write reads as the platform context
(call_platform_ctx), seals the note under the brokerage's key
(client_note_crypto) and removes card numbers first (pan_redaction).

commands_api re-exports the public names its importers and tests use
(record_call_note_consent, save_call_note, deliver_call_notes, CallNoteWrite,
format_call_notes, call_note_recipients, AI_CALL_NOTE_AUTHOR,
CALL_NOTES_WITHDRAWN), and elevenlabs_realtime still imports the three entry
points from commands_api when it calls them, so
`monkeypatch.setattr(commands_api, "save_call_note", ...)` (and the other two)
keeps replacing what the call bridge calls. Calls between the functions of
this module resolve here, not through commands_api: a test that replaces a
helper (_call_note_client, for example) or the tenant_tx these functions read
through must patch this module. The private helpers are not re-exported, so
such a patch on commands_api raises AttributeError instead of silently
missing.

Logs go to the "oracle.commands" logger, as they did from commands_api.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, NamedTuple, Optional

import pan_redaction
import ws_hub
from call_platform_ctx import _call_platform_ctx
from db.connection import tenant_tx

logger = logging.getLogger("oracle.commands")


#: Who wrote a note the AI took on a call — never the agent's own id: the agent
#: did not say these words, the callee did, and the AI summarised them.
AI_CALL_NOTE_AUTHOR = "neoh-ai-call"
AI_CALL_NOTE_CONSENT_BASIS = "verbal_consent_on_ai_call"
_MAX_CALL_NOTES = 25
_MAX_CALL_NOTE_CHARS = 300


async def record_call_note_consent(provider_call_id: str, granted: bool) -> bool:
    """The callee's answer to "may I take a few notes for your agent?".

    Outbound calls keep nothing by default (consent_recorded=false,
    'awaiting_explicit_live_transcription_consent'). A yes said to the AI on
    the call, after the recorded-line disclosure, is that explicit consent;
    a no is recorded too, so nobody later mistakes silence for agreement.
    Returns whether the call's session row was found — no row, no notes.

    Bounded like save_call_note (2 s lock wait, 5 s per statement).

    A no after notes were saved during the call withdraws them, in the same
    transaction: the call's AI note is deleted and its timeline row keeps
    only that consent was withdrawn (no content) — what delivery at hang-up
    did before notes were written as they were taken (34.4 / RUN-03).
    """
    if not provider_call_id:
        return False
    async with tenant_tx(_call_platform_ctx("ai-call-consent")) as conn:
        # The caller is waiting on this answer too (and a no may delete the
        # call's note): a timeout rolls everything back and raises, which the
        # bridge treats as an unrecorded answer (a no is retried at hang-up).
        await conn.execute(_CALL_NOTE_WRITE_LIMITS)
        row = await conn.fetchrow(
            """
            UPDATE live_call_sessions
               SET consent_recorded=$2,
                   consent_basis=CASE WHEN $2 THEN $3 ELSE 'declined_on_ai_call' END,
                   transcript_status=CASE WHEN $2 THEN 'active' ELSE 'complete' END
             WHERE provider_call_id=$1
            RETURNING id, tenant_id, client_id, lead_id
            """,
            provider_call_id, bool(granted), AI_CALL_NOTE_CONSENT_BASIS,
        )
        if row is not None and not granted:
            await _withdraw_call_note(conn, row, provider_call_id)
    return row is not None


#: The call's timeline row once the caller withdrew consent: no content.
CALL_NOTES_WITHDRAWN = "Call notes withdrawn: the caller withdrew consent, nothing from this call is kept"


async def _withdraw_call_note(conn: Any, session: Any, provider_call_id: str) -> None:
    client_id = await _call_note_client(conn, session)
    if client_id is None:
        return
    activity = await _call_notes_activity(conn, client_id, provider_call_id)
    if activity is None:
        return
    meta = _jsonb_dict(activity["meta"])
    if meta.get("note_id"):
        await conn.execute(
            "DELETE FROM client_notes WHERE id=$1 AND client_id=$2 AND author_id=$3",
            uuid.UUID(str(meta["note_id"])), client_id, AI_CALL_NOTE_AUTHOR)
    meta.update({"note_id": None, "note_count": 0, "tool_call_keys": [], "review_state": None,
                 "unsaved": 0, "withdrawn": True})
    await conn.execute(
        "UPDATE client_activities SET summary=$2, meta=$3::jsonb WHERE id=$1",
        activity["id"], CALL_NOTES_WITHDRAWN, json.dumps(meta))
    logger.info("call notes withdrawn with consent: %s", provider_call_id)


def format_call_notes(notes: list[str]) -> str:
    lines = "\n".join(f"- {n}" for n in notes)
    return f"Notes from Neoh's call (the caller agreed to notes):\n{lines}"


def _call_note_lines(body: str) -> list[str]:
    """The items of a call note body (its "- " lines), for the live frame."""
    return [line[2:].strip() for line in str(body or "").splitlines()
            if line.startswith("- ") and line[2:].strip()]


def _call_notes_summary(count: int, review_state: Optional[str], unsaved: int = 0) -> str:
    summary = f"Call notes from Neoh ({count})"
    if review_state == "proposed":
        summary += " — waiting for your review"
    if unsaved:
        summary += f" — {unsaved} could not be saved"
    return summary


def _jsonb_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value) if isinstance(value, (str, bytes)) else None
    except ValueError:
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


#: The call's session row, locked: every write of a call's notes (each item as
#: it is taken, and delivery at call end) runs one at a time per call.
_CALL_NOTE_SESSION_SQL = """
    SELECT s.id, s.tenant_id, s.client_id, s.contact_id, s.lead_id, s.command_id, s.consent_recorded,
           s.created_by, c.created_by AS command_created_by
      FROM live_call_sessions s
      LEFT JOIN command_executions c ON c.id = s.command_id
     WHERE s.provider_call_id=$1 FOR UPDATE OF s"""


#: save_call_note answers a caller who is waiting on the line.
_CALL_NOTE_WRITE_LIMITS = "SET LOCAL lock_timeout = '2s'; SET LOCAL statement_timeout = '5s'"


def _call_note_requester(session: Any) -> str:
    return str(session["command_created_by"] or session["created_by"] or "").strip()


async def _call_note_client(conn: Any, session: Any) -> Any:
    """The client a call's notes belong to. A lead-only call (chk_call_anchor
    allows it; leads have no notes table) on a lead that is a client's own
    house follows that client (0143), so its notes do too (L09-06, T09.17)."""
    client_id = session["client_id"]
    if client_id is None and session["lead_id"] is not None:
        client_id = await conn.fetchval(
            "SELECT seller_client_id FROM leads WHERE id=$1", session["lead_id"])
    return client_id


async def _call_notes_activity(conn: Any, client_id: Any, provider_call_id: str) -> Any:
    """The call's 'ai_call_notes' timeline row: the idempotency key of the
    call's one AI note (its meta names the note, the count and the tool calls
    already saved)."""
    return await conn.fetchrow(
        """SELECT id, meta FROM client_activities
            WHERE client_id=$1 AND meta->>'provider_call_id'=$2 AND meta->>'source'='ai_call_notes'
            ORDER BY created_at LIMIT 1""",
        client_id, provider_call_id,
    )


def _call_notes_frame(client_id: Any, session: Any, note_id: str, notes: list[str],
                      review_state: str) -> dict[str, Any]:
    return {
        "type": "CALL_NOTES", "version": 1,
        "client_id": str(client_id),
        "command_id": str(session["command_id"]) if session["command_id"] else None,
        "note_id": note_id, "notes": notes, "review_state": review_state,
    }


class CallNoteWrite(NamedTuple):
    """What save_call_note did. `saved` is true only for a committed write
    (or a retried tool call whose write had committed)."""

    status: str  # saved | duplicate | refused | no_client | full | empty
    note_id: Optional[str] = None
    count: int = 0

    @property
    def saved(self) -> bool:
        return self.status in ("saved", "duplicate")


async def save_call_note(provider_call_id: str, note: str, *, tool_call_id: str = "",
                         conversation_id: str = "") -> CallNoteWrite:
    """Write one note the AI took on a call into the call's one AI note, now,
    while the call goes on (external audit 34.4 / RUN-03; owner decision
    2026-11-20: one AI note per call, written and updated incrementally).

    The first item creates the note the way deliver_call_notes does: sealed
    under the brokerage's key, author neoh-ai-call, in the requesting agent's
    review mode, on the same client (lead-only calls included), with the same
    'ai_call_notes' timeline row — so delivery at call end finds it and writes
    no second note. Each later item appends a line to that same note.

    This returns only after the transaction committed: the AI may say "saved"
    exactly when the result is `saved` (or `duplicate`); an exception means
    nothing may be claimed. `tool_call_id` (the provider's id for this tool
    call) is recorded on the timeline row with a keyed tag of the text
    (client_note_crypto.content_tag), so a retried tool call — same id, same
    text — is answered `duplicate` and never appends twice, while the same id
    with other text is written. The transaction is bounded (2 s lock wait,
    5 s statement): a timeout rolls it back and raises, i.e. "not saved".

    In 'review' mode an append puts the note back to proposed: words the agent
    has not read never ride on an earlier approval; 'direct' mode never
    approves a note that is waiting for review. An append keeps the agent's
    own edits. A note the agent discarded mid-call stays discarded: the next
    item starts a new note, which the timeline row then names.
    """
    import call_note_settings
    import client_note_crypto

    # A card number the callee read out never becomes a note, a timeline
    # entry or a CALL_NOTES frame (PCI DSS SAQ A). seal_body checks again.
    redaction = pan_redaction.redact(str(note or "").strip()[:_MAX_CALL_NOTE_CHARS])
    text = str(redaction.text or "").strip()
    tool_call_id = str(tool_call_id or "").strip()[:200]
    if not provider_call_id or not text:
        return CallNoteWrite("empty")
    async with tenant_tx(_call_platform_ctx(AI_CALL_NOTE_AUTHOR)) as conn:
        # Bounded: the caller is waiting on this answer. A lock or statement
        # that runs long rolls the write back, and the AI says "not saved".
        await conn.execute(_CALL_NOTE_WRITE_LIMITS)
        session = await conn.fetchrow(_CALL_NOTE_SESSION_SQL, provider_call_id)
        pan_redaction.report(
            "ai_call_note", redaction.cards, redaction.codes,
            tenant_id=session["tenant_id"] if session else None,
            target_id=session["client_id"] if session else None)
        if session is None or not session["consent_recorded"]:
            logger.warning("call note refused — no consented session: %s", provider_call_id)
            return CallNoteWrite("refused")
        requester = _call_note_requester(session)
        client_id = await _call_note_client(conn, session)
        if client_id is None:
            # A prospect with no client record: nowhere to keep it for review.
            # The requester is told once per call; the caller is told now.
            await _tell_requester_notes_not_kept(conn, session, requester, provider_call_id, 1)
            return CallNoteWrite("no_client")
        activity = await _call_notes_activity(conn, client_id, provider_call_id)
        meta = _jsonb_dict(activity["meta"]) if activity is not None else {}
        seen = [str(t) for t in (meta.get("tool_call_keys") or []) if t]
        count = int(meta.get("note_count") or 0)
        # A retry is the same tool call id with the same text; the same id
        # with other text is a new note and is written.
        call_key = (f"{tool_call_id}:{client_note_crypto.content_tag(session['tenant_id'], text)}"
                    if tool_call_id else "")
        if call_key and call_key in seen:
            return CallNoteWrite("duplicate", meta.get("note_id"), count)
        if count >= _MAX_CALL_NOTES:
            return CallNoteWrite("full", meta.get("note_id"), count)
        assignee = await conn.fetchval("SELECT assignee_id FROM clients WHERE id=$1", client_id)
        mode = await call_note_settings.read_mode(conn, session["tenant_id"], requester)
        current = None
        if meta.get("note_id"):
            # Locked: an agent editing the note now is not overwritten.
            current = await client_note_crypto.open_row(conn, await conn.fetchrow(
                f"""SELECT id, body, review_state, {client_note_crypto.SEALED_COLUMNS}
                      FROM client_notes WHERE id=$1 AND client_id=$2 FOR UPDATE""",
                uuid.UUID(str(meta["note_id"])), client_id))
        if current is not None:
            kept = str(current.get("body") or "").rstrip()
            body = f"{kept}\n- {text}" if kept else format_call_notes([text])
            review_state = "proposed" if mode == "review" else str(current["review_state"])
            sealed = await client_note_crypto.seal_body(conn, session["tenant_id"], body,
                                                        surface="ai_call_note")
            await conn.execute(
                """UPDATE client_notes
                      SET body='', body_ciphertext=$2, review_state=$3, updated_at=now(),
                          reviewed_by=CASE WHEN $3='approved' THEN reviewed_by END,
                          reviewed_at=CASE WHEN $3='approved' THEN reviewed_at END
                    WHERE id=$1""",
                current["id"], sealed, review_state,
            )
            note_id = str(current["id"])
        else:
            body = format_call_notes([text])
            review_state = "proposed" if mode == "review" else "approved"
            # Sealed at rest under the brokerage's key, never a plaintext body
            # (0132, audit A01; the database refuses one).
            sealed = await client_note_crypto.seal_body(conn, session["tenant_id"], body,
                                                        surface="ai_call_note")
            note_id = str(await conn.fetchval(
                """INSERT INTO client_notes (tenant_id, client_id, body, body_ciphertext, author_id,
                                             pinned, review_state)
                   VALUES ($1, $2, '', $3, $4, false, $5) RETURNING id""",
                session["tenant_id"], client_id, sealed, AI_CALL_NOTE_AUTHOR, review_state,
            ))
        count += 1
        if call_key:
            seen.append(call_key)
        meta.update({
            "source": "ai_call_notes", "note_id": note_id,
            "provider_call_id": provider_call_id,
            "command_id": str(session["command_id"]) if session["command_id"] else None,
            "consent_basis": AI_CALL_NOTE_CONSENT_BASIS,
            "conversation_id": meta.get("conversation_id") or conversation_id or None,
            "review_state": review_state, "note_count": count, "tool_call_keys": seen,
            "withdrawn": False,
        })
        summary = _call_notes_summary(count, review_state, int(meta.get("unsaved") or 0))
        if activity is None:
            await conn.execute(
                """INSERT INTO client_activities (tenant_id, client_id, kind, summary, meta, actor)
                   VALUES ($1, $2, 'note', $3, $4::jsonb, $5)""",
                session["tenant_id"], client_id, summary, json.dumps(meta), AI_CALL_NOTE_AUTHOR,
            )
        else:
            await conn.execute(
                "UPDATE client_activities SET summary=$2, meta=$3::jsonb WHERE id=$1",
                activity["id"], summary, json.dumps(meta),
            )
    # Committed. The frame names the whole note as it now stands, to the same
    # recipients as delivery's frame, re-checked against the record (C3.6).
    frame = _call_notes_frame(client_id, session, note_id, _call_note_lines(body), review_state)
    try:
        await ws_hub.broadcast_record(
            str(session["tenant_id"]), frame, client_id=client_id,
            contact_id=session["contact_id"], only=call_note_recipients(requester, assignee),
            mirror=False)  # private to its recipients, as broadcast_user was
    except Exception:  # noqa: BLE001 — the note is saved; the live frame is extra
        logger.warning("CALL_NOTES frame not sent: %s", provider_call_id, exc_info=True)
    return CallNoteWrite("saved", note_id, count)


async def deliver_call_notes(provider_call_id: str, notes: list[str],
                             conversation_id: str = "", *, unsaved: int = 0) -> Optional[str]:
    """Put the notes the AI took on a call where the agent works: a note on
    the client plus a timeline entry, and a live CALL_NOTES frame.

    Only for a call whose callee said yes (consent_recorded); idempotent per
    call. Returns the note id, or None when nothing was written.

    Since 34.4 / RUN-03 the bridge writes each note as it is taken
    (save_call_note), so at call end the call's note normally exists already:
    delivery finds it by the same key (the call's 'ai_call_notes' timeline
    row), writes no second note and returns None. It still closes the
    session's transcript status (hang-up calls it for every consented call,
    notes or not), and `unsaved` (notes the caller was told
    could not be saved) is shown on that timeline row — or, when nothing was
    saved at all, in a task for the requesting agent that carries no content.

    The note lands at once in the mode the calling agent chose
    (call_note_settings, owner decision D-2026-10-12-03): 'review' saves it
    *proposed* for the agent to approve, edit or discard; 'direct' saves it
    approved. The live frame carries customer content, so it goes only to the
    agent who requested the call and the client's assigned agent, and only if
    they may still read the client (CONTRACT C3.6, ws_hub.broadcast_record).
    It used to go to every session in the brokerage.
    """
    import call_note_settings
    import client_note_crypto

    clean = [str(n).strip()[:_MAX_CALL_NOTE_CHARS] for n in notes if str(n).strip()][:_MAX_CALL_NOTES]
    unsaved = max(0, int(unsaved or 0))
    if not provider_call_id:
        return None
    # A card number the callee read out never becomes a note, a timeline
    # entry or a CALL_NOTES frame (PCI DSS SAQ A). seal_body checks again.
    redactions = [pan_redaction.redact(n) for n in clean]
    clean = [r.text for r in redactions]
    body = format_call_notes(clean)
    async with tenant_tx(_call_platform_ctx(AI_CALL_NOTE_AUTHOR)) as conn:
        session = await conn.fetchrow(_CALL_NOTE_SESSION_SQL, provider_call_id)
        pan_redaction.report(
            "ai_call_note", sum(r.cards for r in redactions), sum(r.codes for r in redactions),
            tenant_id=session["tenant_id"] if session else None,
            target_id=session["client_id"] if session else None)
        if session is None or not session["consent_recorded"]:
            logger.warning("call notes dropped — no consented session: %s", provider_call_id)
            return None
        # Hang-up closes the consented call's transcript status, whether or not
        # any note was taken (it used to stay 'active' when none was).
        await conn.execute(
            "UPDATE live_call_sessions SET transcript_status='complete' WHERE id=$1", session["id"])
        if not (clean or unsaved):
            return None
        requester = _call_note_requester(session)
        client_id = await _call_note_client(conn, session)
        if client_id is None:
            # A prospect with no client record: there is nowhere the agent can
            # review a note, so nothing the callee said is kept — and the agent
            # is told so, once, instead of the notes vanishing into a log line.
            await _tell_requester_notes_not_kept(conn, session, requester, provider_call_id,
                                                 len(clean) or unsaved)
            return None
        activity = await _call_notes_activity(conn, client_id, provider_call_id)
        if activity is not None:
            # The call's note exists (written as the call went): no second one.
            if unsaved:
                meta = _jsonb_dict(activity["meta"])
                meta["unsaved"] = max(int(meta.get("unsaved") or 0), unsaved)
                state = None
                if meta.get("note_id"):
                    state = await conn.fetchval(
                        "SELECT review_state FROM client_notes WHERE id=$1 AND client_id=$2",
                        uuid.UUID(str(meta["note_id"])), client_id)
                await conn.execute(
                    "UPDATE client_activities SET summary=$2, meta=$3::jsonb WHERE id=$1",
                    activity["id"],
                    _call_notes_summary(int(meta.get("note_count") or len(clean)), state, meta["unsaved"]),
                    json.dumps(meta))
            return None
        if not clean:
            # Nothing was saved during the call; the caller heard so each time.
            await _tell_requester_notes_not_kept(conn, session, requester, provider_call_id, unsaved,
                                                 client_id=client_id, reason="not_saved")
            return None
        assignee = await conn.fetchval(
            "SELECT assignee_id FROM clients WHERE id=$1", client_id)
        mode = await call_note_settings.read_mode(conn, session["tenant_id"], requester)
        review_state = "proposed" if mode == "review" else "approved"
        # What the customer said is sealed at rest under the brokerage's key,
        # never a plaintext body (0132, audit A01; the database refuses one).
        sealed = await client_note_crypto.seal_body(conn, session["tenant_id"], body,
                                                    surface="ai_call_note")
        note = await conn.fetchrow(
            """INSERT INTO client_notes (tenant_id, client_id, body, body_ciphertext, author_id,
                                         pinned, review_state)
               VALUES ($1, $2, '', $3, $4, false, $5) RETURNING id""",
            session["tenant_id"], client_id, sealed, AI_CALL_NOTE_AUTHOR, review_state,
        )
        await conn.execute(
            """INSERT INTO client_activities (tenant_id, client_id, kind, summary, meta, actor)
               VALUES ($1, $2, 'note', $3, $4::jsonb, $5)""",
            session["tenant_id"], client_id, _call_notes_summary(len(clean), review_state, unsaved),
            json.dumps({
                "source": "ai_call_notes", "note_id": str(note["id"]),
                "provider_call_id": provider_call_id,
                "command_id": str(session["command_id"]) if session["command_id"] else None,
                "consent_basis": AI_CALL_NOTE_CONSENT_BASIS,
                "conversation_id": conversation_id or None,
                "review_state": review_state,
                "note_count": len(clean), "unsaved": unsaved,
            }),
            AI_CALL_NOTE_AUTHOR,
        )
    frame = _call_notes_frame(client_id, session, str(note["id"]), clean, review_state)
    # Among the requester and the assignee, only those who may still read the
    # client (a revoked share, a reassignment) — C3.4/C3.6, 0145.
    try:
        await ws_hub.broadcast_record(
            str(session["tenant_id"]), frame, client_id=client_id,
            contact_id=session["contact_id"], only=call_note_recipients(requester, assignee),
            mirror=False)  # private to its recipients, as broadcast_user was
    except Exception:  # noqa: BLE001 — the note is saved; the live frame is extra
        logger.warning("CALL_NOTES frame not sent: %s", provider_call_id, exc_info=True)
    return str(note["id"])


async def _tell_requester_notes_not_kept(conn: Any, session: Any, requester: str,
                                         provider_call_id: str, count: int, *,
                                         client_id: Any = None, reason: str = "no_client") -> None:
    """A task for the agent who asked for the call: notes the callee agreed
    to were not kept — because the call was about a lead with no client
    record ('no_client'), or because every write failed during the call
    ('not_saved'; the caller was told each time). Carries no note content.
    Idempotent per call."""
    marker = f"Call {provider_call_id}"
    if not requester or await conn.fetchval(
        """SELECT 1 FROM client_tasks
            WHERE tenant_id=$1 AND created_by=$2 AND details LIKE $3""",
        session["tenant_id"], AI_CALL_NOTE_AUTHOR, f"%{marker}%",
    ):
        return
    if reason == "not_saved":
        title = "Call notes could not be saved"
        details = (f"The callee agreed to notes on a call, but Neoh could not save {count} "
                   f"note(s) they shared; it told them each time that the note was not saved, "
                   f"and nothing they said is in this task. Ask them directly. {marker}.")
    else:
        title = "Call notes were not kept: this lead has no client record"
        details = (f"Neoh took {count} note(s) with the callee's consent on a call about a lead "
                   f"(lead {session['lead_id']}), but the lead is not linked to a client, so there "
                   f"is nowhere to keep them for your review and nothing was saved. Add the person "
                   f"as a client before the next call to keep its notes. {marker}.")
    await conn.execute(
        """INSERT INTO client_tasks (tenant_id, client_id, title, details, status, priority,
                                     assignee_id, created_by)
           VALUES ($1, $2, $3, $4, 'open', 'normal', $5, $6)""",
        session["tenant_id"], client_id, title, details, requester, AI_CALL_NOTE_AUTHOR,
    )
    logger.info("call notes not kept (%s); requester told: %s", reason, provider_call_id)


def call_note_recipients(requester: Optional[str], assignee: Optional[str]) -> list[str]:
    """Who may receive a call's note text live: the agent who requested the
    call and the client's assigned agent, once each (agent ids compare
    case-insensitively), never the AI author or an empty id."""
    out: list[str] = []
    for agent in (requester, assignee):
        agent = str(agent or "").strip()
        if agent and agent != AI_CALL_NOTE_AUTHOR and agent.lower() not in {a.lower() for a in out}:
            out.append(agent)
    return out
