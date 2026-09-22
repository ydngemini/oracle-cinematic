-- Migration 0105: let mission_events record a digest send.
--
-- The digest's cadence (backend/missions/digest.py) is tiered — frequent
-- right after the first mission launches, throttled afterward — which needs
-- a durable "when did we last actually send" marker that survives a process
-- restart. mission_events already exists for exactly this kind of "when did
-- X happen" bookkeeping; this adds the one value the digest needs rather
-- than inventing a parallel table.
ALTER TABLE mission_events
    DROP CONSTRAINT IF EXISTS mission_events_kind_check;
ALTER TABLE mission_events
    ADD CONSTRAINT mission_events_kind_check CHECK (kind IN (
        'created', 'simulated', 'launched', 'paused', 'resumed',
        'planned', 'plan_failed', 'candidate_added', 'candidate_excluded',
        'action_withheld', 'action_staged', 'action_released',
        'action_blocked', 'budget_exhausted', 'strategy_changed',
        'completed', 'failed', 'cancelled', 'digest_sent'
    ));
