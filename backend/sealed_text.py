"""Communication text sealed at rest (audit A01 / ISO R01, ledgers L17-21, L17-22).

These columns hold what a person wrote or said (or what was written about
them) and are read only by id or by their parent record, never searched by
content:

    sms_messages.body                      -> body_ciphertext          (0152)
    negotiation_events.transcript_excerpt  -> transcript_ciphertext    (0152)
    user_interactions.content              -> content_ciphertext       (0152)
    email_outbox.subject / .body_text      -> subject_ / body_text_ciphertext (0154)
    interaction_logs.subject               -> subject_ciphertext       (0154)
    interaction_logs.payload content keys  -> content_ciphertext       (0154)
    ai_tool_operations.result              -> result_ciphertext        (0154)
    beliefs.source_quote                   -> source_quote_ciphertext  (0154)
    user_profiles.profile_summary          -> profile_summary_ciphertext (0154)

interaction_logs.payload is a jsonb object that also carries behavioural keys
(listing ids, link kinds). Only its content keys (`json_keys`) are sealed: the
writer moves them into a JSON object under content_ciphertext (`split_json`),
and `open_rows` merges them back into the payload it returns.

Since migrations 0152 / 0154 every app write puts the text only in the ciphertext
column (pgp_sym_encrypt under the per-tenant HKDF key: the scheme of
crypto.derive_tenant_key, ai_chat_messages and client_note_crypto) and leaves
the plaintext column empty (NULL, or '' where it is NOT NULL). The 0152 CHECKs
keep the two from both holding text. Rows written before 0152 keep their
plaintext until backfill_sealed_text.py seals them, so every reader opens both
shapes: select the field's `columns` next to the plaintext column and pass the
rows through `open_rows`.

The key comes from the row's own tenant, never the caller's context. A missing
master key fails closed (SealError) for writes and for reading a sealed row;
plaintext-only reads still work. No message here ever names the content.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from crypto import CryptoError, derive_tenant_key


class SealError(RuntimeError):
    """Text could not be sealed or opened. The message names no content."""


@dataclass(frozen=True)
class SealedField:
    table: str
    plain: str          # the legacy plaintext column
    cipher: str         # the bytea column 0152 added
    sealed_plain: Any   # what `plain` holds on a sealed row (None or '')
    # When set, `plain` is a jsonb object and only these keys are sealed: the
    # cipher holds them as one JSON object, and opening merges them back.
    json_keys: tuple[str, ...] = ()

    @property
    def tenant_alias(self) -> str:
        return f"{self.cipher}_tenant_id"


SMS_BODY = SealedField("sms_messages", "body", "body_ciphertext", None)
TRANSCRIPT = SealedField("negotiation_events", "transcript_excerpt", "transcript_ciphertext", None)
INTERACTION = SealedField("user_interactions", "content", "content_ciphertext", "")

# 0154 (L17-22)
OUTBOX_SUBJECT = SealedField("email_outbox", "subject", "subject_ciphertext", "")
OUTBOX_BODY = SealedField("email_outbox", "body_text", "body_text_ciphertext", "")
LOG_SUBJECT = SealedField("interaction_logs", "subject", "subject_ciphertext", None)
LOG_CONTENT = SealedField("interaction_logs", "payload", "content_ciphertext", None,
                          json_keys=("body", "text", "transcript", "summary", "extraction"))
TOOL_RESULT = SealedField("ai_tool_operations", "result", "result_ciphertext", None)
BELIEF_QUOTE = SealedField("beliefs", "source_quote", "source_quote_ciphertext", None)
PROFILE_SUMMARY = SealedField("user_profiles", "profile_summary", "profile_summary_ciphertext", "")

FIELDS: tuple[SealedField, ...] = (
    SMS_BODY, TRANSCRIPT, INTERACTION,
    OUTBOX_SUBJECT, OUTBOX_BODY, LOG_SUBJECT, LOG_CONTENT, TOOL_RESULT, BELIEF_QUOTE, PROFILE_SUMMARY,
)


def key_for(tenant_id: Any) -> str:
    """The tenant's pgcrypto passphrase (crypto.derive_tenant_key, unchanged).

    `user_interactions.tenant_id` is text; a UUID is canonicalised so the text
    and uuid forms of one tenant derive the same key (a no-op for the canonical
    form every other table stores)."""
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise SealError("text encryption is unavailable (master key not configured)")
    raw = str(tenant_id or "")
    try:
        raw = str(uuid.UUID(raw))
    except ValueError:
        pass
    try:
        return derive_tenant_key(raw, master)
    except CryptoError as exc:
        raise SealError("text encryption could not be initialized") from exc


def key_if_needed(tenant_id: Any, *values: Any) -> str | None:
    """The tenant key when any value has text to seal, else None.

    pgp_sym_encrypt is STRICT, so a NULL key with a NULL value stores NULL:
    a row with nothing to seal (a portal view, an empty subject) needs no
    master key, while a row with text fails closed without one."""
    if any(v not in (None, "") for v in values):
        return key_for(tenant_id)
    return None


def split_json(payload: Any, field: SealedField) -> tuple[dict, str | None]:
    """(the payload to store in plaintext, the JSON text to seal or None).

    The content keys of `field.json_keys` leave the stored payload; nothing
    else changes, so filters and counts on behavioural keys keep working."""
    data = dict(payload or {})
    sealed = {k: data.pop(k) for k in field.json_keys if k in data}
    return data, (json.dumps(sealed, default=str) if sealed else None)


def _merge_json(current: Any, text: str) -> Any:
    """Merge opened content keys back into a payload, keeping its shape (asyncpg
    hands jsonb back as text unless a codec is set; a dict stays a dict)."""
    opened = json.loads(text) if text else {}
    if isinstance(current, str):
        try:
            base = json.loads(current)
        except json.JSONDecodeError:
            base = {}
        return json.dumps({**(base if isinstance(base, dict) else {}), **opened})
    return {**dict(current or {}), **opened}


async def open_rows(conn: Any, rows: Iterable[Any], field: SealedField) -> list[dict]:
    """Rows as dicts with `field.plain` holding the text, whichever way it is
    stored. Consumes the cipher column and its tenant alias. One decrypt round
    trip per tenant present, none when no row is sealed."""
    out: list[dict] = []
    sealed: dict[str, list[tuple[int, bytes]]] = {}
    for row in rows:
        data = dict(row)
        ciphertext = data.pop(field.cipher, None)
        tenant_id = data.pop(field.tenant_alias, None)
        if ciphertext is not None:
            if tenant_id is None:
                raise SealError(f"a sealed {field.table} row was read without its tenant")
            sealed.setdefault(str(tenant_id), []).append((len(out), bytes(ciphertext)))
        out.append(data)
    for tenant_id, items in sealed.items():
        key = key_for(tenant_id)
        try:
            texts = await conn.fetchval(
                """SELECT array_agg(pgp_sym_decrypt(c, $2::text) ORDER BY n)
                     FROM unnest($1::bytea[]) WITH ORDINALITY AS t(c, n)""",
                [ciphertext for _, ciphertext in items], key,
            )
        except Exception as exc:  # noqa: BLE001 — wrong key / corrupt data; never echo it
            raise SealError(f"{field.table} text could not be decrypted") from exc
        for (index, _), text in zip(items, texts or []):
            if field.json_keys:
                try:
                    out[index][field.plain] = _merge_json(out[index].get(field.plain), text)
                except (TypeError, ValueError) as exc:
                    raise SealError(f"{field.table} sealed content is not a JSON object") from exc
            else:
                out[index][field.plain] = text
    return out


async def open_fields(conn: Any, rows: Iterable[Any], *fields: SealedField) -> list[dict]:
    """`open_rows` for several sealed columns of the same rows."""
    out = [dict(r) for r in rows]
    for field in fields:
        out = await open_rows(conn, out, field)
    return out
