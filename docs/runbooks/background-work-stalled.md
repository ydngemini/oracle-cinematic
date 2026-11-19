# Runbook: background work stalled (workers, scheduler, job queue)

Background work runs in **one** App Platform worker component (`worker`, `ORACLE_PROCESS_ROLE=worker`, pinned to `instance_count: 1`). It holds the durable job queue (sends, syncs, Neoh replies, exports, erasures) and the periodic scheduler (MLS sync, reconciliation every 15 min, privacy sweeps). API replicas never claim jobs.

**You see:**
- alert `[Neoh] workers UNAVAILABLE`: no worker heartbeat for 120 s (4 missed 30 s beats);
- alert `[Neoh] scheduler …`: no scheduler tick for 2 ticks + 2 min;
- alert `[Neoh] job_queue …`: the oldest ready job keeps getting older, or dead letters are appearing;
- alert `[Neoh] media_jobs DEGRADED`: voice walkthroughs or 3D reconstructions are waiting too long (15 min / 3 h) while a worker is alive, or a running one has stopped its 60 s heartbeat for 10 min (its worker loop stopped);
- customers: Neoh replies, texts and syncs wait. The CRM itself works.

**What Neoh does by itself:**
- Every job is durable; nothing queued is lost while the worker is down.
- A job that was mid-run when the worker died is re-claimed when its 120 s lease lapses.
  - A chat turn interrupted this way is **not** re-run. The agent sees "Neoh was interrupted before finishing that response… please ask again."
- A job that has been ready for more than 60 s is claimed oldest-first (`ORACLE_JOB_STARVATION_SECONDS`), so no job starves behind a stream of higher-priority work.

**Do:**
1. App Platform → `worker` component: is it running? Look at the last deploy and its logs (crash loop, OOM, failed boot).
2. `GET /health/workers`: is a worker alive, and on the **current** release (`git_sha`)? A worker on an old release after a deploy means the rollout is stuck. Redeploy.
3. Restart the worker component. It is safe: leases and job state are in Postgres.
4. If jobs keep failing: `GET /api/admin/health/components` → `job_queue.detail.dead_letter_1h`. Find the failing `job_type` in the worker logs (`automation_jobs` lines). One bad handler does not block other job types.
5. If the queue is only slow (sustained `oldest_ready_s` > 60 s with a healthy worker): raise `ORACLE_JOB_WORKERS` (default lane, 8 in production) or `ORACLE_INTERACTIVE_JOB_WORKERS` (Neoh replies, 16), then redeploy. **Never** set the worker's `instance_count` above 1 (see the comment in `infra/digitalocean/app.yaml`).

**`media_jobs` specifically** (what each job loses on a restart: [worker inventory](../worker-background-work-inventory.md)):
- `voice_silent` > 0: the voice loop stopped while a job was running. Restarting the worker requeues it (the audio is in its row); after 3 attempts it fails with a reason.
- `recon_silent` > 0: a reconstruction stopped beating. A restart **fails or requeues it and terminates its recorded GPU pod**, using one of its 2 attempts. If the worker is alive and healthy, check its logs for that job first; `provider_reconciliation` fails a reconstruction silent for 6 h by itself.
- A backlog with a healthy loop: voice runs 2 loops (`ORACLE_VOICE_WORKERS`), reconstruction 1 (`RECON_WORKER_COUNT`). Raise those, **never** the worker's `instance_count`.

**Verify:** the `workers`, `scheduler`, `job_queue` and `media_jobs` components are HEALTHY, `oldest_ready_s` is falling, and the alerts close by themselves.
