from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "db"
    / "migrations"
    / "0037_application_sequence_privileges.sql"
)


def test_runtime_role_receives_minimal_current_and_future_sequence_access():
    sql = MIGRATION.read_text()

    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO oracle_app" in sql
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in sql
    assert "GRANT USAGE, SELECT ON SEQUENCES TO oracle_app" in sql
    assert "GRANT ALL" not in sql
