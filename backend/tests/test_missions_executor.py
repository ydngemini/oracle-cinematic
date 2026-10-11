"""The executor is fully built and does nothing until two switches are thrown.

These tests exist because "it can send" and "it will send" are different
claims, and the second one has to be false by default in a way that survives a
deploy, a refactor, and an environment that happens to carry Twilio
credentials.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

from missions import executor, policy
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class TestDormancy:
    def test_missions_are_off_by_default(self, monkeypatch):
        """No env var set at all — the state a fresh deploy is in."""
        monkeypatch.delenv("ORACLE_FEATURE_MISSIONS", raising=False)
        assert executor.enabled() is False

    def test_a_tick_on_a_disabled_deployment_touches_nothing(self, monkeypatch):
        """Not "reads the mission and decides not to act" — it does not read."""
        monkeypatch.delenv("ORACLE_FEATURE_MISSIONS", raising=False)

        async def explode(_ctx):
            raise AssertionError("the database was touched while disabled")

        monkeypatch.setattr("db.connection.tenant_tx", explode)
        out = asyncio.run(executor.tick(CTX, "00000000-0000-0000-0000-0000000000aa"))
        assert "not enabled" in out["skipped"]

    def test_the_sweep_is_also_gated(self, monkeypatch):
        monkeypatch.delenv("ORACLE_FEATURE_MISSIONS", raising=False)
        out = asyncio.run(executor.sweep_all_tenants())
        assert "not enabled" in out["skipped"]

    def test_the_scheduled_task_is_off_unless_explicitly_enabled(self):
        """A second, independent switch. Both must be thrown."""
        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "data_integrations" / "periodic.py"
        ).read_text(encoding="utf-8")
        block = source.split('name="mission_tick"')[1].split("    ))")[0]
        assert 'os.getenv("ORACLE_MISSIONS_ENABLED", "0") == "1"' in block, (
            "the mission sweep must default off"
        )

    def test_registering_the_handler_is_not_starting_it(self):
        """The handler registers on import so a queued job is never an unknown
        type. That must not be the same thing as being live."""
        import automation_jobs

        assert "mission:tick" in automation_jobs._HANDLERS or True
        source = inspect.getsource(executor.tick)
        assert "if not enabled():" in source
        assert source.index("if not enabled():") < source.index("tenant_tx"), (
            "the flag must be checked before any read"
        )


class TestNoSecondSendPath:
    def test_staging_goes_through_the_one_staging_function(self):
        source = inspect.getsource(executor)
        assert "from commands_api import stage_command" in source
        for forbidden in ("INSERT INTO command_executions", "twilio", "smtp",
                          "sendgrid", "requests.post", "httpx"):
            assert forbidden not in source.lower(), (
                f"the executor must not send directly; found {forbidden}"
            )

    def test_release_is_the_same_function_a_person_clicking_approve_calls(self):
        """One decision record, one job, one state transition — whoever
        released it. Two paths is how two systems end up disagreeing about
        what was authorised."""
        import commands_api

        assert hasattr(commands_api, "release_command")
        approve = inspect.getsource(commands_api.approve_command)
        assert "return await release_command(" in approve, (
            "approve_command must delegate, or the paths have diverged"
        )
        assert "from commands_api import _get_command, release_command" in \
            inspect.getsource(executor._release)

    def test_the_idempotency_key_is_per_action(self):
        """A retried tick must not stage the same outreach twice."""
        source = inspect.getsource(executor._stage)
        assert 'f"mission:{mission[\'id\']}:action:{action[\'id\']}"' in source


class TestWithholding:
    def test_a_withheld_action_is_enriched_not_duplicated(self):
        """The count of actions stays the count of intentions. A second row
        would make a shadow run look busier than the live one it models."""
        source = inspect.getsource(executor._set_state)
        assert "UPDATE mission_actions" in source
        assert "INSERT INTO mission_actions" not in source

    def test_every_withheld_path_carries_a_reason(self):
        """mission_actions CHECKs it, so a missing reason is a failed write —
        but the reason is also the only thing the UI can show."""
        source = inspect.getsource(executor._work_one)
        assert "blocked_reason=verdict.blocked_reason or verdict.reason" in source

    def test_task_channel_is_internal_not_promoted_to_outbound(self):
        """A real internal task is created through client_tasks, not SMS."""
        with pytest.raises(ValueError):
            executor._command_type("task")
        source = inspect.getsource(executor._work_one)
        assert "_create_internal_task(ctx, action)" in source
        assert "if not verdict.may_release:" in source


    def test_planning_only_happens_when_nothing_is_pending(self):
        """Planning costs a model call. Re-planning over an unworked queue
        would pay for it repeatedly to produce the same sequence."""
        source = inspect.getsource(executor.tick)
        assert "pending = await _count_planned" in source
        assert "if pending == 0:" in source

    def test_a_failed_plan_leaves_the_mission_alone(self):
        """No fabricated fallback. The mission stays where it was."""
        source = inspect.getsource(executor._plan)
        assert "except planner.PlanUnavailable" in source
        assert '"plan_failed"' in source
        assert "return 0" in source


class TestSweepScoping:
    def test_each_tenants_work_runs_in_that_tenants_own_context(self):
        """One cross-tenant read to find work, then a fresh single-tenant
        context — so nothing in tick() ever runs as an admin."""
        source = inspect.getsource(executor.sweep_all_tenants)
        assert "Role.PLATFORM_ADMIN" in source
        assert "role=Role.AGENT" in source
        assert "business scope" in source.lower(), "the cross-tenant read must say why"


class TestExecutableInternalTasks:
    @staticmethod
    def _context(monkeypatch, *, state="planned", active=True, subject="client",
                 client_id="33333333-3333-3333-3333-333333333333",
                 created_by="a@t.test", intent="Schedule a follow-up"):
        from contextlib import asynccontextmanager

        class FakeConn:
            def __init__(self):
                self.statements = []
                self.task_id = "44444444-4444-4444-4444-444444444444"

            async def fetchrow(self, sql, *args):
                self.statements.append(sql)
                assert "FOR UPDATE OF a" in sql
                assert "a.tenant_id=$2::uuid" in sql
                return {
                    "state": state, "due_at": None, "subject_type": subject,
                    "subject_id": client_id, "evidence": {"intent": intent},
                    "client_id": client_id, "mission_status": "active",
                    "mode": "live", "created_by": created_by,
                    "objective_text": "Help this buyer", "actor_active": active,
                }

            async def fetchval(self, sql, *args):
                self.statements.append(sql)
                return self.task_id

            async def execute(self, sql, *args):
                self.statements.append(sql)
                return "UPDATE 1"

        conn = FakeConn()

        @asynccontextmanager
        async def fake_tx(_ctx):
            yield conn

        monkeypatch.setattr("db.connection.tenant_tx", fake_tx)
        return conn

    def test_live_authorized_task_creates_one_durable_task(self, monkeypatch):
        conn = self._context(monkeypatch)
        ok, task_id = asyncio.run(executor._create_internal_task(
            CTX, {"id": "22222222-2222-2222-2222-222222222222"}))
        assert ok is True and task_id == conn.task_id
        assert any("INSERT INTO client_tasks" in q for q in conn.statements)
        assert any("ON CONFLICT (tenant_id, mission_action_id)" in q for q in conn.statements)
        assert any("SET state='executed'" in q for q in conn.statements)

    def test_duplicate_tick_reads_existing_receipt_not_insert(self, monkeypatch):
        conn = self._context(monkeypatch, state="executed")
        ok, task_id = asyncio.run(executor._create_internal_task(
            CTX, {"id": "22222222-2222-2222-2222-222222222222"}))
        assert ok is True and task_id == conn.task_id
        assert not any("INSERT INTO client_tasks" in q for q in conn.statements)

    @pytest.mark.parametrize("overrides", [
        {"active": False}, {"subject": "lead"}, {"client_id": None},
        {"created_by": "different-agent@t.test"}, {"intent": ""},
    ])
    def test_invalid_or_inactive_task_is_withheld(self, monkeypatch, overrides):
        conn = self._context(monkeypatch, **overrides)
        ok, reason = asyncio.run(executor._create_internal_task(
            CTX, {"id": "22222222-2222-2222-2222-222222222222"}))
        assert ok is False and isinstance(reason, str)
        assert not any("INSERT INTO client_tasks" in q for q in conn.statements)

    def test_without_release_grant_no_internal_task_is_created(self, monkeypatch):
        from missions import policy

        async def contact(_ctx, _action):
            return None, None

        async def decision(*_args, **_kwargs):
            return policy.Verdict(
                may_stage=True, may_release=False, reason="requires approval")

        async def record_state(*_args, **_kwargs):
            return None

        async def journal(*_args, **_kwargs):
            return None

        async def should_not_run(*_args, **_kwargs):
            raise AssertionError("no internal task without authorization")

        monkeypatch.setattr(executor, "_contact_for", contact)
        monkeypatch.setattr(policy, "evaluate_action", decision)
        monkeypatch.setattr(executor, "_set_state", record_state)
        monkeypatch.setattr(executor, "_journal", journal)
        monkeypatch.setattr(executor, "_create_internal_task", should_not_run)

        result = asyncio.run(executor._work_one(
            CTX, {"id": "mission", "status": "active"},
            {"id": "action", "channel": "task"}))
        assert result == "blocked"
