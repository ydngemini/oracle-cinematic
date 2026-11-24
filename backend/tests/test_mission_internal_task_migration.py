"""Mission internal tasks are real, idempotent CRM work, never outbound calls."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "db/migrations/0129_mission_internal_tasks.sql").read_text(encoding="utf-8")


def test_mission_task_links_to_tenant_scoped_action():
    assert "ADD COLUMN IF NOT EXISTS mission_action_id uuid" in SQL
    assert "FOREIGN KEY (tenant_id, mission_action_id)" in SQL
    assert "REFERENCES mission_actions (tenant_id, id)" in SQL
    assert "ON DELETE SET NULL (mission_action_id)" in SQL


def test_no_duplicate_task_even_after_worker_retry():
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_tasks_mission_action" in SQL
    assert "WHERE mission_action_id IS NOT NULL" in SQL


def test_event_kind_preserves_existing_history_and_adds_execution_receipt():
    for event in ("digest_sent", "action_staged", "action_released", "action_executed"):
        assert f"'{event}'" in SQL
