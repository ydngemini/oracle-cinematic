"""PostgreSQL-leased durable automation jobs.

The queue is safe across ECS replicas: enqueue is idempotent, claim uses
``FOR UPDATE SKIP LOCKED``, every mutation is guarded by a random lease token,
and expired leases are recoverable.  High-risk jobs require an approved payload
hash before they can enter the executable queue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from platform_policy import ActionRisk, requires_approval
from tenancy import Role, TenantContext

import db.connection as dbc

logger = logging.getLogger("oracle.automation_jobs")

JobHandler = Callable[[dict[str, Any], "JobReporter"], Awaitable[dict[str, Any]]]

_PLATFORM_TENANT_ID = os.getenv(
    "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)
_LEASE_SECONDS = max(30, int(os.getenv("ORACLE_JOB_LEASE_SECONDS", "120")))
_POLL_SECONDS = max(0.25, float(os.getenv("ORACLE_JOB_POLL_SECONDS", "2")))
_WORKER_COUNT = max(1, min(16, int(os.getenv("ORACLE_JOB_WORKERS", "2"))))


class JobLeaseLost(RuntimeError):
    pass


class JobApprovalError(ValueError):
    pass


def _exception_detail(exc: Exception) -> str:
    """Keep the exception class visible when built-in timeouts have no text."""
    return str(exc).strip() or repr(exc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def interval_idempotency_key(job_type: str, tenant_id: str, bucket: str) -> str:
    material = f"scheduled:{tenant_id}:{job_type}:{bucket}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _platform_context(worker_id: str) -> TenantContext:
    return TenantContext(
        agent_id=worker_id,
        tenant_id=_PLATFORM_TENANT_ID,
        role=Role.PLATFORM_ADMIN,
    )


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    for field in ("payload", "result"):
        result[field] = _decode_json(result.get(field))
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


async def enqueue_job(
    ctx: TenantContext,
    *,
    job_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    created_by: Optional[str] = None,
    queue_name: str = "default",
    priority: int = 50,
    max_attempts: int = 5,
    scheduled_at: Optional[datetime] = None,
    risk: ActionRisk = ActionRisk.READ_ONLY,
    approval_id: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    """Insert one job or return the existing idempotent job.

    Returns ``(job, created)``.  Approval validation and insertion share the
    same tenant-scoped transaction, closing the approve-then-edit race.
    """
    if not job_type or len(job_type) > 120:
        raise ValueError("job_type is required and must be <= 120 characters")
    if not idempotency_key or len(idempotency_key) > 240:
        raise ValueError("idempotency_key is required and must be <= 240 characters")
    if not 0 <= priority <= 100:
        raise ValueError("priority must be between 0 and 100")
    if not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be between 1 and 20")

    body = dict(payload)
    body_hash = payload_hash(body)
    initial_state = "queued"
    async with dbc.tenant_tx(ctx) as conn:
        if requires_approval(risk):
            if not approval_id:
                raise JobApprovalError(f"{risk.value} jobs require approval")
            approval = await conn.fetchrow(
                """
                SELECT id, status, payload_hash, expires_at
                FROM action_approvals
                WHERE id = $1::uuid AND tenant_id = $2::uuid
                FOR SHARE
                """,
                approval_id,
                ctx.tenant_id,
            )
            if not approval:
                raise JobApprovalError("approval was not found")
            if approval["status"] != "approved":
                raise JobApprovalError("approval is not approved")
            if approval["expires_at"] <= datetime.now(timezone.utc):
                raise JobApprovalError("approval has expired")
            if approval["payload_hash"] != body_hash:
                raise JobApprovalError("approved payload no longer matches the job payload")

        row = await conn.fetchrow(
            """
            INSERT INTO automation_jobs (
                tenant_id, job_type, queue_name, state, risk_class, payload,
                priority, scheduled_at, max_attempts, idempotency_key,
                approval_id, created_by
            ) VALUES (
                $1::uuid, $2, $3, $4, $5, $6::jsonb,
                $7, COALESCE($8, now()), $9, $10, $11::uuid, $12
            )
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING *
            """,
            ctx.tenant_id,
            job_type,
            queue_name,
            initial_state,
            risk.value,
            canonical_json(body),
            priority,
            scheduled_at,
            max_attempts,
            idempotency_key,
            approval_id,
            created_by or ctx.agent_id,
        )
        created = row is not None
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM automation_jobs WHERE tenant_id=$1::uuid AND idempotency_key=$2",
                ctx.tenant_id,
                idempotency_key,
            )
        if row is None:  # defensive: deleted concurrently by a privileged maintenance role
            raise RuntimeError("idempotent job disappeared during enqueue")
    return _row_dict(row), created


async def claim_next_job(worker_id: str, *, queue_name: str = "default") -> Optional[dict[str, Any]]:
    """Atomically recover an expired lease and claim one executable job."""
    if dbc.get_pool() is None:
        return None
    lease_token = uuid.uuid4()
    ctx = _platform_context(worker_id)
    async with dbc.tenant_tx(ctx) as conn:
        await _reap_exhausted_leases(conn)
        row = await conn.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                FROM automation_jobs
                WHERE queue_name = $1
                  AND attempt_count < max_attempts
                  AND scheduled_at <= now()
                  AND (next_retry_at IS NULL OR next_retry_at <= now())
                  AND (
                      state IN ('queued','failed')
                      OR (state IN ('leased','running') AND lease_expires_at < now())
                  )
                ORDER BY priority ASC, scheduled_at ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE automation_jobs AS j
               SET state = 'leased',
                   lease_owner = $2,
                   lease_token = $3,
                   lease_expires_at = now() + ($4 || ' seconds')::interval,
                   attempt_count = j.attempt_count + 1,
                   started_at = COALESCE(j.started_at, now()),
                   status_message = 'leased',
                   updated_at = now()
              FROM candidate
             WHERE j.id = candidate.id
            RETURNING j.*
            """,
            queue_name,
            worker_id,
            lease_token,
            str(_LEASE_SECONDS),
        )
        if row is None:
            return None
        await conn.execute(
            """
            INSERT INTO automation_job_attempts
                (tenant_id, job_id, attempt_number, worker_id, lease_token)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid)
            """,
            str(row["tenant_id"]),
            str(row["id"]),
            row["attempt_count"],
            worker_id,
            lease_token,
        )
    return _row_dict(row)


async def _reap_exhausted_leases(conn) -> int:
    """Dead-letter expired jobs that can no longer be claimed.

    Without this sweep, a running job whose final lease expires at
    ``attempt_count == max_attempts`` remains stuck in ``running`` forever
    because the claim query correctly refuses another attempt.
    """
    result = await conn.execute(
        """
        UPDATE automation_jobs
           SET state='dead_letter',
               status_message='dead letter: lease attempts exhausted',
               completed_at=COALESCE(completed_at, now()),
               lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
               last_error_code=COALESCE(
                   last_error_code,
                   'LEASE_ATTEMPTS_EXHAUSTED'
               ),
               last_error=COALESCE(
                   last_error,
                   'job lease expired after maximum attempts'
               ),
               updated_at=now()
         WHERE state IN ('leased','running')
           AND lease_expires_at < now()
           AND attempt_count >= max_attempts
        """
    )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


async def _lease_update(
    job: Mapping[str, Any],
    worker_id: str,
    sql: str,
    *args: Any,
) -> Any:
    ctx = _platform_context(worker_id)
    async with dbc.tenant_tx(ctx) as conn:
        result = await conn.fetchrow(
            sql,
            job["id"],
            worker_id,
            job["lease_token"],
            *args,
        )
    if result is None:
        raise JobLeaseLost(f"lease lost for job {job['id']}")
    return result


async def mark_running(job: Mapping[str, Any], worker_id: str) -> None:
    await _lease_update(
        job,
        worker_id,
        """
        UPDATE automation_jobs
           SET state='running', status_message='running',
               lease_expires_at=now() + ($4 || ' seconds')::interval,
               last_error=NULL,last_error_code=NULL,
               updated_at=now()
         WHERE id=$1::uuid AND lease_owner=$2 AND lease_token=$3::uuid
        RETURNING id
        """,
        str(_LEASE_SECONDS),
    )


async def heartbeat_job(
    job: Mapping[str, Any], worker_id: str, *, progress: float, message: str
) -> None:
    progress = max(0.0, min(99.99, float(progress)))
    await _lease_update(
        job,
        worker_id,
        """
        UPDATE automation_jobs
           SET progress=$4, status_message=$5,
               lease_expires_at=now() + ($6 || ' seconds')::interval,
               updated_at=now()
         WHERE id=$1::uuid AND lease_owner=$2 AND lease_token=$3::uuid
        RETURNING id
        """,
        progress,
        message[:500],
        str(_LEASE_SECONDS),
    )


async def complete_job(
    job: Mapping[str, Any], worker_id: str, result: Mapping[str, Any]
) -> None:
    result_body = dict(result)
    # A national job can finish all available work while still containing
    # failed jurisdictions.  Preserve that terminal truth instead of calling
    # it a success; the worker attempt itself still completed normally.
    state = "partial" if result_body.pop("_terminal_state", None) == "partial" else "succeeded"
    status_message = "partial completion" if state == "partial" else "succeeded"
    await _lease_update(
        job,
        worker_id,
        """
        UPDATE automation_jobs
           SET state=$4, result=$5::jsonb, progress=100,
               status_message=$6, completed_at=now(),
               lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
               last_error=NULL, last_error_code=NULL, updated_at=now()
         WHERE id=$1::uuid AND lease_owner=$2 AND lease_token=$3::uuid
        RETURNING id
        """,
        state,
        canonical_json(result_body),
        status_message,
    )
    await _finish_attempt(job, worker_id, "succeeded")


async def fail_job(
    job: Mapping[str, Any],
    worker_id: str,
    exc: Exception,
    *,
    error_code: str = "JOB_HANDLER_ERROR",
) -> None:
    attempt = int(job.get("attempt_count") or 1)
    max_attempts = int(job.get("max_attempts") or 1)
    terminal = attempt >= max_attempts
    retry_seconds = min(3_600, 5 * (2 ** max(0, attempt - 1)))
    error_detail = _exception_detail(exc)
    await _lease_update(
        job,
        worker_id,
        """
        UPDATE automation_jobs
           SET state=$4, status_message=$5, last_error_code=$6,
               last_error=$7,
               next_retry_at=CASE WHEN $4='failed'
                                  THEN now() + ($8 || ' seconds')::interval
                                  ELSE NULL END,
               completed_at=CASE WHEN $4='dead_letter' THEN now() ELSE NULL END,
               lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
               updated_at=now()
         WHERE id=$1::uuid AND lease_owner=$2 AND lease_token=$3::uuid
        RETURNING id
        """,
        "dead_letter" if terminal else "failed",
        "dead letter" if terminal else f"retry in {retry_seconds}s",
        error_code[:120],
        error_detail[:2_000],
        str(retry_seconds),
    )
    await _finish_attempt(job, worker_id, "failed", error_code, error_detail)
    logger.error(
        "ORACLE_METRIC automation_job_failure job_type=%s state=%s error_code=%s attempt=%d",
        str(job.get("job_type") or "unknown")[:120],
        "dead_letter" if terminal else "failed",
        error_code[:120],
        attempt,
    )


async def _finish_attempt(
    job: Mapping[str, Any],
    worker_id: str,
    outcome: str,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> None:
    ctx = _platform_context(worker_id)
    async with dbc.tenant_tx(ctx) as conn:
        changed = await conn.fetchval(
            """
            SELECT finish_automation_job_attempt($1::uuid, $2, $3, $4, $5)
            """,
            job["id"],
            int(job["attempt_count"]),
            outcome,
            error_code,
            error_detail[:2_000] if error_detail else None,
        )
    if not changed:
        logger.warning(
            "automation job attempt was already finalized or unavailable job=%s attempt=%d",
            str(job.get("id") or "unknown"),
            int(job.get("attempt_count") or 0),
        )


async def get_job(ctx: TenantContext, job_id: str) -> Optional[dict[str, Any]]:
    async with dbc.tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM automation_jobs WHERE id=$1::uuid", job_id
        )
    return _row_dict(row) if row else None


async def list_jobs(
    ctx: TenantContext,
    *,
    state: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(200, limit))
    async with dbc.tenant_tx(ctx) as conn:
        if state:
            rows = await conn.fetch(
                "SELECT * FROM automation_jobs WHERE state=$1 ORDER BY created_at DESC LIMIT $2",
                state,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM automation_jobs ORDER BY created_at DESC LIMIT $1", limit
            )
    return [_row_dict(row) for row in rows]


class JobReporter:
    def __init__(self, job: dict[str, Any], worker_id: str):
        self.job = job
        self.worker_id = worker_id

    async def progress(self, percent: float, message: str) -> None:
        await heartbeat_job(self.job, self.worker_id, progress=percent, message=message)
        try:
            import ws_hub

            payload = {
                    "type": "JOB_PROGRESS",
                    "version": 1,
                    "job_id": str(self.job["id"]),
                    "job_type": self.job["job_type"],
                    "progress": max(0.0, min(100.0, float(percent))),
                    "message": message[:500],
                }
            if self.job["job_type"] == "ai_chat:response":
                await ws_hub.broadcast_user(
                    str(self.job["tenant_id"]), str(self.job.get("created_by") or ""), payload
                )
            else:
                await ws_hub.broadcast(str(self.job["tenant_id"]), payload)
        except Exception as exc:  # noqa: BLE001 - telemetry cannot fail the job
            logger.debug("job progress broadcast failed: %s", exc)


_HANDLERS: dict[str, JobHandler] = {}


def register_handler(job_type: str, handler: JobHandler) -> None:
    if not job_type:
        raise ValueError("job_type is required")
    _HANDLERS[job_type] = handler


def registered_handlers() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


class DurableJobWorkers:
    def __init__(self, worker_count: int = _WORKER_COUNT):
        host = socket.gethostname()
        self.worker_ids = [f"{host}:{os.getpid()}:{index}" for index in range(worker_count)]
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._loop(worker_id), name=f"durable-job-{index}")
            for index, worker_id in enumerate(self.worker_ids)
        ]
        logger.info("Durable job workers started: %d", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, worker_id: str) -> None:
        while self._running:
            try:
                job = await claim_next_job(worker_id)
                if job is None:
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                handler = _HANDLERS.get(str(job["job_type"]))
                if handler is None:
                    await fail_job(
                        job,
                        worker_id,
                        RuntimeError(f"no handler registered for {job['job_type']}"),
                        error_code="NO_JOB_HANDLER",
                    )
                    continue
                await mark_running(job, worker_id)
                reporter = JobReporter(job, worker_id)
                await reporter.progress(1, "started")
                try:
                    result = await handler(dict(job.get("payload") or {}), reporter)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - persisted retry path
                    await fail_job(job, worker_id, exc)
                else:
                    await complete_job(job, worker_id, result or {})
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - one queue error cannot kill worker
                logger.exception("durable job worker %s recovered from error: %s", worker_id, exc)
                await asyncio.sleep(min(10.0, _POLL_SECONDS * 2))


workers = DurableJobWorkers()


async def start_job_workers() -> None:
    await workers.start()


async def stop_job_workers() -> None:
    await workers.stop()
