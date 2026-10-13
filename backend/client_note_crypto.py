"""Client notes are sealed at rest (audit A01, ledger L17-03).

A note is communication content: an AI call note is what a customer said on a
call, and an agent's note is often the same thing in their words. Since
migration 0132 every app write puts the text only in
`client_notes.body_ciphertext` (pgcrypto under the per-tenant HKDF key — the
scheme `ai_chat_messages` and `agent_contacts` already use, see crypto.py) and
leaves `body` empty; the 0132 CHECK keeps the two from both holding text.

Rows written before 0132 keep their plaintext `body` until
`backfill_client_notes.py` seals them, so every reader opens both shapes:
select `SEALED_COLUMNS` next to `body` and pass the rows through `open_rows`.

The key comes from the row's own tenant, never the caller's context, so a
platform context reading a brokerage's note opens it with that brokerage's key.
A missing master key fails closed (RuntimeError) for writes and for reading a
sealed row, the way contact_truth does; plaintext-only reads still work.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from crypto import CryptoError, derive_tenant_key, encrypt_pii

#: Append to any SELECT / RETURNING list that reads `body`; `open_rows` consumes
#: (and removes) both columns.
SEALED_COLUMNS = "body_ciphertext, tenant_id AS body_tenant_id"


class NoteSealError(RuntimeError):
    """The note could not be sealed or opened. The message names no content."""


def _key(tenant_id: Any) -> str:
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise NoteSealError("client note encryption is unavailable (master key not configured)")
    try:
        return derive_tenant_key(str(tenant_id), master)
    except CryptoError as exc:
        raise NoteSealError("client note encryption could not be initialized") from exc


async def seal_body(conn: Any, tenant_id: Any, body: str) -> bytes:
    """Ciphertext for a note body, under `tenant_id`'s key."""
    try:
        return await encrypt_pii(conn, body, _key(tenant_id))
    except CryptoError as exc:
        raise NoteSealError("client note could not be encrypted") from exc


async def open_rows(conn: Any, rows: Iterable[Any]) -> list[dict]:
    """Rows as dicts with `body` holding the note text, whichever way it is
    stored. One decrypt round trip per tenant present, none when no row is
    sealed."""
    out: list[dict] = []
    sealed: dict[str, list[tuple[int, bytes]]] = {}
    for row in rows:
        data = dict(row)
        ciphertext = data.pop("body_ciphertext", None)
        tenant_id = data.pop("body_tenant_id", None)
        if ciphertext is not None:
            if tenant_id is None:
                raise NoteSealError("a sealed note was read without its tenant")
            sealed.setdefault(str(tenant_id), []).append((len(out), bytes(ciphertext)))
        out.append(data)
    for tenant_id, items in sealed.items():
        try:
            texts = await conn.fetchval(
                """SELECT array_agg(pgp_sym_decrypt(c, $2::text) ORDER BY n)
                     FROM unnest($1::bytea[]) WITH ORDINALITY AS t(c, n)""",
                [ciphertext for _, ciphertext in items], _key(tenant_id),
            )
        except NoteSealError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrong key / corrupt data; never echo it
            raise NoteSealError("client note could not be decrypted") from exc
        for (index, _), text in zip(items, texts or []):
            out[index]["body"] = text
    return out


async def open_row(conn: Any, row: Any) -> dict | None:
    if row is None:
        return None
    return (await open_rows(conn, [row]))[0]
