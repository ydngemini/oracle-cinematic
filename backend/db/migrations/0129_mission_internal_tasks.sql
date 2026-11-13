-- 0129 — real, idempotent internal CRM tasks from mission action rows.
-- Do not edit the applied 0019 or 0100 migration history.
-- A mission task is not an EMAIL/SMS/CALL command and must not be silently
-- translated into an external action. Link it to the exact authorized action.
BEGIN;

ALTER TABLE mission_actions
    ADD CONSTRAINT mission_actions_tenant_id_id_unique UNIQUE (tenant_id, id);

ALTER TABLE client_tasks
    ADD COLUMN IF NOT EXISTS mission_action_id uuid;

ALTER TABLE client_tasks
    ADD CONSTRAINT client_tasks_mission_action_tenant_fk
    FOREIGN KEY (tenant_id, mission_action_id)
    REFERENCES mission_actions (tenant_id, id)
    ON DELETE SET NULL (mission_action_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_client_tasks_mission_action
    ON client_tasks (tenant_id, mission_action_id)
    WHERE mission_action_id IS NOT NULL;

-- A created task is executed internal work, not a staged outbound command.
ALTER TABLE mission_events
    DROP CONSTRAINT IF EXISTS mission_events_kind_check;
ALTER TABLE mission_events
    ADD CONSTRAINT mission_events_kind_check CHECK (kind IN (
        'created', 'simulated', 'launched', 'paused', 'resumed',
        'planned', 'plan_failed', 'candidate_added', 'candidate_excluded',
        'action_withheld', 'action_staged', 'action_released',
        'action_blocked', 'action_executed', 'budget_exhausted',
        'strategy_changed', 'completed', 'failed', 'cancelled', 'digest_sent'
    ));

COMMIT;
