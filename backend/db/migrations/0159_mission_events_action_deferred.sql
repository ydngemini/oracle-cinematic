-- ---------------------------------------------------------------------------
-- 0159 — mission_events accepts 'action_deferred' (prompt 08, ledger L08-02).
--
-- L05-03 made a mission step with a stated "not before" move its due_at and
-- journal the deferral once as kind 'action_deferred'
-- (missions/executor.py _work_one). mission_events_kind_check (0100) never
-- listed that kind, so every such journal row was refused (23514);
-- executor._journal logs and swallows journal failures by design, so the
-- deferral itself happened but the mission's history silently lacked it.
-- Found reviewing PR #11 against the live CHECK (L08-01).
--
-- Same list as before plus 'action_deferred'. Existing rows all satisfy it.
-- tests/test_mission_event_kinds.py holds every kind the code journals to
-- the newest definition of this CHECK.
--
-- Rollback: re-create the constraint without 'action_deferred' (only after
-- deleting any such rows).
-- ---------------------------------------------------------------------------

ALTER TABLE mission_events
    DROP CONSTRAINT IF EXISTS mission_events_kind_check;
ALTER TABLE mission_events
    ADD CONSTRAINT mission_events_kind_check CHECK (kind IN (
        'created', 'simulated', 'launched', 'paused', 'resumed',
        'planned', 'plan_failed', 'candidate_added', 'candidate_excluded',
        'action_withheld', 'action_staged', 'action_released',
        'action_blocked', 'action_deferred', 'budget_exhausted',
        'strategy_changed', 'completed', 'failed', 'cancelled', 'digest_sent'
    ));
