"""Communication text sealed at rest (audit A01 / ISO R01, ledger L17-21).

Three columns hold what a person wrote or said and are read only by id or by
their parent record, never searched by content:

    sms_messages.body                      -> body_ciphertext
    negotiation_events.transcript_excerpt  -> transcript_ciphertext
    user_interactions.content              -> content_ciphertext

Since migration 0152 every app write puts the text only in the ciphertext
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

    @property
    def tenant_alias(self) -> str:
        return f"{self.cipher}_tenant_id"


SMS_BODY = SealedField("sms_messages", "body", "body_ciphertext", None)
TRANSCRIPT = SealedField("negotiation_events", "transcript_excerpt", "transcript_ciphertext", None)
INTERACTION = SealedField("user_interactions", "content", "content_ciphertext", "")

FIELDS: tuple[SealedField, ...] = (SMS_BODY, TRANSCRIPT, INTERACTION)


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
            out[index][field.plain] = text
    return out
