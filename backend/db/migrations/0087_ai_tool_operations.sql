-- 0087 — the execution ledger that makes AI tool calls replay-safe
--
-- Today a tool round that dies after its mutation commits but before the model
-- sees the receipt is unrecoverable: retrying re-runs the mutation, so the rule
-- has to be "tool rounds never retry" (see llm_gateway). That rule costs us
-- every legitimate recovery to prevent one illegitimate one. This table buys the
-- retries back by making the second execution recognise the first.
--
-- IDENTITY is (tenant_id, assistant_id, call_index) and none of it is invented.
-- `assistant_id` is minted at ai_chat_store.py:296, which inserts the assistant
-- row with status='pending' BEFORE the model runs, and the duplicate path at
-- :168-180 returns the SAME assistant_id for a repeated request_id — so a
-- regenerated completion keeps its round identity for free. The provider's own
-- tool_call_id is deliberately NOT the key: it changes when a completion is
-- regenerated, which is exactly the moment dedupe has to work.
--
-- tool_name and arguments_hash are an INTEGRITY CHECK, not part of the key. The
-- same (assistant_id, call_index) arriving with different arguments is not a
-- second operation, it is evidence the round diverged, and the code raises
-- TOOL_CALL_IDENTITY_MISMATCH rather than quietly executing it.
--
-- TWO COMMITTED STATES, and that is the whole state machine.
-- _execute_safe_tool runs inside ONE tenant_tx (ai_chat_store.py:1041), so:
--
--   success  claim INSERT + mutation + action ledger + result UPDATE  → COMMIT
--   failure  all of it rolls back (no row at all), then 'failed' is
--            written from a FRESH connection after the tx has exited
--
-- There is therefore no interleaving in which another connection observes a
-- half-done operation, and no 'running' state exists to be stranded. The claim
-- INSERT is a transaction-local mutex on the unique index, not a lifecycle
-- stage — which is why this design needs no lease, heartbeat, or sweeper. The
-- row is written as 'completed' at claim time because, if it commits at all, it
-- commits atomically with the mutation it describes; if the mutation does not
-- commit, the row never existed.
--
-- ABSENCE of a row means "no durable effect": either execution never reached the
-- claim, or the tx rolled back. Both are safe to retry. That is the guarantee.
--
-- SCOPE is the ~16 effectful tools. The ~101 read-only tools get no row —
-- ~100 inserts per turn on the hot path buys no replay value.
--
-- This is the FIRST of three ledgers and does not replace either of the others:
-- ai_chat_actions records what changed and how to reverse it, and
-- billing_usage_events records what was consumed. Their idempotency boundaries
-- genuinely differ — one model call can dispatch several tool calls, and a
-- retried round is correctly billed twice for one committed effect — so they are
-- correlatable by assistant_id but must never be merged.

CREATE TABLE IF NOT EXISTS ai_tool_operations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         text NOT NULL,
    assistant_id    uuid NOT NULL REFERENCES ai_chat_messages(id) ON DELETE CASCADE,
    -- Monotonic across the whole turn, not per round: round 2's first call must
    -- not collide with round 1's first call.
    call_index      integer NOT NULL CHECK (call_index >= 0),
    tool_name       text NOT NULL,
    -- sha256 over the canonical (sorted-key, separator-normalised) JSON of the
    -- arguments. Hashed rather than stored because arguments carry CRM values
    -- and this table is not encrypted; the hash is only ever compared.
    arguments_hash  text NOT NULL,
    status          text NOT NULL CHECK (status IN ('completed', 'failed')),
    -- The receipt the first execution returned, replayed verbatim to the model
    -- on a duplicate call. NULL is legitimate for 'failed', and for a 'completed'
    -- row whose result UPDATE did not land — the mutation still committed, so a
    -- replay must still refuse to re-run it.
    result          jsonb,
    error_code      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    CONSTRAINT ai_tool_operations_identity
        UNIQUE (tenant_id, assistant_id, call_index)
);

-- The replay lookup is exactly the unique key, so no second index is needed for
-- it. This one serves the operator view: "what did the agent do for this user".
CREATE INDEX IF NOT EXISTS idx_ai_tool_operations_agent_time
    ON ai_tool_operations (tenant_id, user_id, created_at DESC);

ALTER TABLE ai_tool_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_tool_operations FORCE ROW LEVEL SECURITY;

-- Mirrors ai_chat_actions: a broker's AI operations are private to that broker,
-- not merely to the tenant.
DROP POLICY IF EXISTS ai_tool_operations_tenant_isolation ON ai_tool_operations;
CREATE POLICY ai_tool_operations_tenant_isolation ON ai_tool_operations
    USING (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()
    ))
    WITH CHECK (app_is_platform_admin() OR (
        tenant_id = app_current_tenant() AND user_id = app_current_agent()
    ));

-- 0003 revokes PUBLIC, so a table without an explicit grant is owner-only and
-- the app cannot see it. No DELETE: an execution record is history.
GRANT SELECT, INSERT, UPDATE ON ai_tool_operations TO oracle_app;
