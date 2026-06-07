"""
Oracle — Multi-Tenant Memory Core: Just-In-Time (JIT) Context Injection.

The SaaS brain. 100+ wholesalers share ONE resident vLLM engine (see
ml_forge/edge_forge/local_vllm_adapter.py); their individual risk profiles and
chat history live in PostgreSQL, NOT in VRAM. SessionManager pulls a user's
memory at request time, stitches it into the system prompt, and rolls older
history into a dense summary so neither the database nor the model's context
window grows without bound.

Tenancy: every read/write runs inside `db.connection.tenant_tx(ctx)`, so the
RLS policies in db/schema.sql physically scope memory to the caller's brokerage
— wholesaler A can never see wholesaler B's deals even on a shared engine.
Here `user_id` is an operator (agent) within the tenant; the TenantContext
supplied at construction is the brokerage/domain.

Expected schema (see MEMORY_SCHEMA_SQL below — a migration stub):
  user_profiles(user_id, tenant_id, target_mao_pct, max_rehab_budget,
                target_markets jsonb, profile_summary, summary_updated_at)
  user_interactions(id, user_id, tenant_id, role, content, token_estimate,
                    created_at)

asyncpg + the local summarizer LLM are imported/called lazily, so importing
this module never requires a live DB or GPU.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional

# tenancy is the project's single source of truth for the request identity. It's
# imported for typing only — `from __future__ import annotations` makes the hints
# lazy strings, so importing this module never drags in tenancy's FastAPI stack
# (lets bootstrap tools like scripts/ignite_memory.py read MEMORY_SCHEMA_SQL with
# nothing but a Postgres driver). The runtime always passes a real TenantContext.
if TYPE_CHECKING:
    from tenancy import TenantContext

# Roll-up triggers once a user's live history crosses this many tokens; the
# oldest messages beyond `keep_recent` are compressed into the profile summary.
DEFAULT_TOKEN_THRESHOLD = 2000
DEFAULT_KEEP_RECENT = 5

# Local, free summarizer (per workspace convention — Llama-3.2-1B on :8090).
LOCAL_LLM_URL = os.getenv("ORACLE_LOCAL_LLM_URL", "http://127.0.0.1:8090/v1/chat/completions")

# A summarizer is (older_messages_text, prior_summary) -> dense summary string.
Summarizer = Callable[[str, str], Awaitable[str]]


# --------------------------------------------------------------------------- #
# Reference migration — the tables SessionManager reads/writes. RLS mirrors the
# existing leads/clients policies in db/schema.sql (tenant_id = current_tenant).
# --------------------------------------------------------------------------- #
MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id           text PRIMARY KEY,
    tenant_id         text NOT NULL,
    target_mao_pct    numeric(4,3) DEFAULT 0.70,   -- the operator's 70%-rule knob
    max_rehab_budget  numeric(12,2),
    target_markets    jsonb DEFAULT '[]'::jsonb,
    profile_summary   text DEFAULT '',
    summary_updated_at timestamptz
);

CREATE TABLE IF NOT EXISTS user_interactions (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL,
    tenant_id       text NOT NULL,
    role            text NOT NULL,                 -- 'user' | 'assistant'
    content         text NOT NULL,
    token_estimate  int  NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_interactions_user_time
    ON user_interactions (user_id, created_at DESC);
"""


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token). Good enough to
    decide *when* to roll up; exact accounting isn't needed for a threshold."""
    if not text:
        return 0
    return max(1, len(text) // 4)


class SessionManager:
    """Per-request memory broker for one brokerage (TenantContext).

    All three public methods are async and tenant-scoped via tenant_tx().
    """

    def __init__(
        self,
        ctx: TenantContext,
        summarizer: Optional[Summarizer] = None,
        token_threshold: int = DEFAULT_TOKEN_THRESHOLD,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ):
        self.ctx = ctx
        self._summarize = summarizer or _local_llm_summarize
        self.token_threshold = token_threshold
        self.keep_recent = keep_recent

    # ------------------------------------------------------------------ #
    # 1. get_user_context — the operator's parameters + recent history.
    # ------------------------------------------------------------------ #
    async def get_user_context(self, user_id: str) -> dict:
        """Pull the user's real-estate parameters (target MAO %, rehab cap,
        markets, rolling profile summary) and their last 5 interactions.

        Returns a plain dict ready for inject_jit_prompt(). Missing profile →
        sane defaults so a brand-new operator still gets a coherent prompt."""
        from db.connection import tenant_tx  # lazy: avoids requiring a live DB on import

        async with tenant_tx(self.ctx) as conn:
            profile = await conn.fetchrow(
                """
                SELECT target_mao_pct, max_rehab_budget, target_markets,
                       profile_summary, summary_updated_at
                  FROM user_profiles
                 WHERE user_id = $1
                """,
                user_id,
            )
            recent = await conn.fetch(
                """
                SELECT role, content, token_estimate, created_at
                  FROM user_interactions
                 WHERE user_id = $1
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                user_id,
                self.keep_recent,
            )

        params = _profile_to_dict(profile)
        # Re-order to chronological for prompt readability.
        interactions = [
            {
                "role": r["role"],
                "content": r["content"],
                "tokens": r["token_estimate"],
                "at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in reversed(recent)
        ]
        return {
            "user_id": user_id,
            "tenant_id": self.ctx.tenant_id,
            # Whether an actual profile row existed. params is always populated
            # (defaults when None), so callers that want to distinguish a real
            # operator from a brand-new/unknown one must read this flag, not the
            # values — a genuine 70% operator is indistinguishable from a default.
            "found": profile is not None,
            "parameters": params,
            "recent_interactions": interactions,
        }

    # ------------------------------------------------------------------ #
    # 2. inject_jit_prompt — stitch memory into the system prompt.
    # ------------------------------------------------------------------ #
    async def inject_jit_prompt(self, user_id: str, base_prompt: str) -> str:
        """Dynamically weave the user's memory into `base_prompt` before it hits
        the vLLM engine. This is the JIT injection: the base underwriting prompt
        stays generic and shared; the per-operator block is appended at call time
        so no user-specific state lives in the model weights or VRAM."""
        context = await self.get_user_context(user_id)
        memory_block = self._render_memory_block(context)
        return f"{base_prompt.rstrip()}\n\n{memory_block}"

    def _render_memory_block(self, context: dict) -> str:
        p = context["parameters"]
        lines = [
            "## OPERATOR MEMORY (JIT-injected — do not echo)",
            f"- Operator: {context['user_id']} (brokerage {context['tenant_id']})",
            f"- Target MAO rule: {p['target_mao_pct'] * 100:.0f}% of ARV minus rehab",
        ]
        if p.get("max_rehab_budget") is not None:
            lines.append(f"- Max rehab budget: ${p['max_rehab_budget']:,.0f}")
        if p.get("target_markets"):
            lines.append(f"- Target markets: {', '.join(p['target_markets'])}")
        if p.get("profile_summary"):
            lines.append(f"- Profile summary: {p['profile_summary']}")

        interactions = context.get("recent_interactions") or []
        if interactions:
            lines.append("- Recent interactions (oldest→newest):")
            for it in interactions:
                snippet = it["content"].replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."
                lines.append(f"    [{it['role']}] {snippet}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 3. summarize_rolling_memory — compress old history to save DB + VRAM.
    # ------------------------------------------------------------------ #
    async def summarize_rolling_memory(self, user_id: str) -> Optional[str]:
        """If the user's live chat history exceeds the token threshold, compress
        everything older than the last `keep_recent` messages into a dense
        'Profile Summary' string, fold it into user_profiles.profile_summary, and
        delete the archived rows. Returns the new summary, or None if no roll-up
        was needed.

        Keeping only a short tail + one dense summary is what lets a single
        engine hold 100 operators without their histories ballooning the prompt
        (VRAM) or the interactions table (disk)."""
        from db.connection import tenant_tx  # lazy

        async with tenant_tx(self.ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content, token_estimate, created_at
                  FROM user_interactions
                 WHERE user_id = $1
                 ORDER BY created_at ASC
                """,
                user_id,
            )

            total_tokens = sum(r["token_estimate"] for r in rows)
            if total_tokens <= self.token_threshold or len(rows) <= self.keep_recent:
                return None  # nothing to compress yet

            older = rows[: -self.keep_recent]  # everything but the recent tail
            archived_ids = [r["id"] for r in older]
            transcript = "\n".join(f"[{r['role']}] {r['content']}" for r in older)

            prior = await conn.fetchval(
                "SELECT profile_summary FROM user_profiles WHERE user_id = $1",
                user_id,
            )

            new_summary = await self._summarize(transcript, prior or "")
            new_summary = new_summary.strip()

            # Upsert the dense summary and purge the rows it now represents.
            await conn.execute(
                """
                INSERT INTO user_profiles (user_id, tenant_id, profile_summary,
                                           summary_updated_at)
                     VALUES ($1, $2, $3, now())
                ON CONFLICT (user_id) DO UPDATE
                    SET profile_summary = EXCLUDED.profile_summary,
                        summary_updated_at = now()
                """,
                user_id,
                self.ctx.tenant_id,
                new_summary,
            )
            await conn.execute(
                "DELETE FROM user_interactions WHERE id = ANY($1::bigint[])",
                archived_ids,
            )

        return new_summary

    # ------------------------------------------------------------------ #
    # Convenience writer — record a turn (auto token-estimated).
    # ------------------------------------------------------------------ #
    async def record_interaction(self, user_id: str, role: str, content: str) -> None:
        """Append one chat turn for the user. Callers log the operator's message
        and the engine's reply here; summarize_rolling_memory() later folds the
        tail away once it grows past the threshold."""
        from db.connection import tenant_tx  # lazy

        async with tenant_tx(self.ctx) as conn:
            await conn.execute(
                """
                INSERT INTO user_interactions
                       (user_id, tenant_id, role, content, token_estimate)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_id,
                self.ctx.tenant_id,
                role,
                content,
                estimate_tokens(content),
            )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _profile_to_dict(profile) -> dict:
    """Normalize an asyncpg Record (or None) into a defaulted params dict."""
    if profile is None:
        return {
            "target_mao_pct": 0.70,
            "max_rehab_budget": None,
            "target_markets": [],
            "profile_summary": "",
            "summary_updated_at": None,
        }
    markets = profile["target_markets"]
    if isinstance(markets, str):  # asyncpg may hand back raw jsonb text
        try:
            markets = json.loads(markets)
        except (json.JSONDecodeError, TypeError):
            markets = []
    return {
        "target_mao_pct": float(profile["target_mao_pct"] or 0.70),
        "max_rehab_budget": (
            float(profile["max_rehab_budget"])
            if profile["max_rehab_budget"] is not None
            else None
        ),
        "target_markets": markets or [],
        "profile_summary": profile["profile_summary"] or "",
        "summary_updated_at": (
            profile["summary_updated_at"].isoformat()
            if profile["summary_updated_at"]
            else None
        ),
    }


async def _local_llm_summarize(transcript: str, prior_summary: str) -> str:
    """Default summarizer — POST to the local free Llama (:8090). Compresses the
    older transcript (plus any prior summary) into a single dense paragraph.
    Falls back to a deterministic truncation if the local LLM is unreachable, so
    a roll-up never silently fails to free space."""
    system = (
        "You compress a real-estate wholesaler's chat history into a dense, "
        "factual profile summary. Capture their deal criteria, target markets, "
        "risk tolerance, recurring questions, and any standing instructions. "
        "Merge with the prior summary. Output ONE paragraph, no preamble."
    )
    user = f"PRIOR SUMMARY:\n{prior_summary or '(none)'}\n\nOLDER MESSAGES:\n{transcript}"

    try:
        import asyncio
        import urllib.request

        payload = json.dumps(
            {
                "model": "local",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": 400,
            }
        ).encode("utf-8")

        def _post() -> str:
            req = urllib.request.Request(
                LOCAL_LLM_URL, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]

        # Don't block the event loop on the synchronous HTTP call.
        return await asyncio.to_thread(_post)
    except Exception:
        # Deterministic fallback: keep the prior summary + a truncated tail.
        merged = (prior_summary + " " + transcript).strip()
        return merged[:1500]
