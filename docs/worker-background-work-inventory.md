# Worker background work: inventory and scaling preconditions (audit 30.9)

Pinned at `5bf2831` (2026-11-19). Line numbers are evidence at that SHA; re-check them before acting.

The DigitalOcean spec runs **one** worker (`ORACLE_PROCESS_ROLE=worker`, `instance_count: 1`), and the launch check `worker_count` requires exactly one. This page records what that worker runs, what each piece loses on a SIGKILL, and what would break with a second worker. It also lists what must change before `instance_count` can go above 1.

## Summary

- **No work is held only in memory any more.** Voice walkthroughs, 3D reconstructions and video reels each queue in their own table, written before the in-process wake-up event fires. The event only lowers latency. The comments that still said "in-process asyncio.Queue" were corrected with this page.
- **One worker is still required.** The reasons are a set of single-process assumptions, not lost queues: boot recovery sweeps with no lease (the reconstruction one also releases the recorded GPU), a video claim race, a per-process video provider semaphore, and an unlocked hourly enforcer. The GPU pod reaper is a lesser concern: it reaps only pods older than twice the provider ceiling plus 30 min (`reconstruction_worker.py:1513-1531`), but it still decides on age alone.
- **Alerting:** worker heartbeat loss pages (`workers`), and so does a stale scheduler (`scheduler`). `job_queue` covers `automation_jobs`. The new `media_jobs` component covers the voice and reconstruction tables: a backlog while a worker is alive, or a running job whose 60 s heartbeat has been silent for 10 minutes. Running voice jobs now heartbeat like reconstructions, so a long transcription, or one waiting on the shared whisper lock, does not page. Before this, all of it was invisible: the process heartbeat proves the process is up, not that its loops take work.
- **Live calls are not worker work.** Call media runs on the API replicas' WebSockets. An unfinalized call is retried at hang-up and recovered by the reconciliation sweep (audit 34.6).

## Inventory

Background work starts when `RUNS_BACKGROUND_WORK = PROCESS_ROLE != "web"` (`backend/config.py:107` at the pinned SHA).

| Mechanism | Durable record before work starts | SIGKILL / restart | With two workers | Concurrency cap | Alert |
|---|---|---|---|---|---|
| **Voice walkthroughs** (`voice_intel.py`; started `server.py:166-167`) | Yes: `voice_walkthrough_jobs` row with the audio (`0114`), inserted at `voice_intel.py:390-400` before the wake event (`:401`) | Row stays `running`. `recover_interrupted_jobs` at boot requeues it, or fails it at 3 attempts (`:1090-1109`, `:284`). No lease and no periodic sweep: a hung job stays `running` until the next restart. Running jobs now heartbeat every 60 s (added with this page), so `media_jobs` reports a job whose loop stopped. The re-run can add a duplicate interaction note (plain INSERT `:933-937` before `_finish` `:1060`) | Claim is SKIP LOCKED-safe (`:1028-1033`), but the boot sweep would requeue the other worker's live jobs (the code says so at `:1095-1096`) | `ORACLE_VOICE_WORKERS` 2 (`:88`); 100 queued per tenant (`:84`, `:382-389`) | `media_jobs` (new) |
| **3D reconstruction** (`reconstruction_worker.py`; started `server.py:168`) | Yes: `reconstruction_jobs` row inserted by `tour_api.py:846-852` before `enqueue()` sets the wake event (`:98-102`) | `fail_orphaned_jobs` at boot requeues the job below 2 attempts, otherwise fails it; either way it releases the recorded GPU (`_release_provider_jobs`, `:1573-1652`). Heartbeat every 60 s (`:216-232`); `provider_reconciliation` fails a job silent for 6 h (`reconciliation.py:55`, `:114-124`); pod reaper every 30 min (`:1533-1563`). The in-memory billing clock is lost (inferred, `reconstruction_providers.py:2612-2618`) | The boot sweep would treat the other worker's live job as orphaned and release its GPU (`:1596-1599`, `:1636`); the reaper decides on pod age only (`:1505-1509`), with a floor well above a normal run | `RECON_WORKER_COUNT` 1 (`:69`); 20 queued per tenant, 100 globally (`tour_api.py:793`, `:959-973`) | `media_jobs` (new) |
| **Video studio** (`video_studio.py`; only with `ORACLE_FEATURE_VIDEO_STUDIO`, default off and absent from the spec, `server.py:179-181`) | Yes: `video_studio_jobs` row (`video_studio_api.py:215-229`); no attempts column | Reclaimed after a 900 s lease (`:64`, `:721-737`); `_set_status` per clip is the heartbeat. No attempt cap (a job that crashes the process loops). A kill between storing and `ready` re-generates and re-bills (`:996-998`) | The claim of a `queued` row only bumps `updated_at` (`:724`), so a second worker, or a second loop in one process (`ORACLE_VIDEO_WORKER_COUNT` > 1), can take the same row. The provider semaphore is per process (`video_providers.py:75`) against provider-wide limits | Provider semaphores 2-4 (`video_providers.py`); daily seconds quota under an advisory lock (`:921-946`) | none (feature off) |
| **Disposition enforcer** (`disposition_enforcer.py:233-255`; `server.py:169`) | No queue; hourly sweep, state change is the guard | Delayed to the next sweep | No lock or SKIP LOCKED (`:151-161`): both workers generate AI assets for the same leads (double spend); last UPDATE wins | 1 sweep/hour | none |
| **Security alert emails** (`security_alerts.py`, both roles) | Factor-change alerts: ledger row first (`:200`), email after (`:209`) | In-flight sends drained 10 s at shutdown (`server.py:243-247`); a SIGKILL between ledger and send loses that alert for good (the next boot sees "unchanged") | Per-process failure counters | n/a | n/a |
| **Audit middleware** (`audit_middleware.py:139`, `:360-364`, both roles) | Pending writes in memory | Drained 5 s at shutdown (`server.py:241`); SIGKILL loses them | n/a | n/a | audit fallback drain (separate) |
| **LLM metering** (`llm_gateway.py:185-200`, both roles) | `ai_token_reservations` hold (`ai_budget.py:259-262`) | The hold lapses after 600 s; the actual usage is not recorded (inferred) | n/a | n/a | none |
| **Operator alerts** (`ops_alerts.py:44`, every replica, leader by advisory lock) | Incidents in `ops_alerts` | The two-evaluation confirmation counter resets: one evaluation late | Leader election already handles it | n/a | n/a |
| **WebSocket fan-out** (`ws_hub.py:38-42`) | Frames are ephemeral by design; split frames held 30 s | Live clients reconnect and re-hydrate | Built for many replicas | 64 pending splits | `realtime_fanout` |
| **Spoken opt-out spool** (`voice_opt_out.py:94-101`) | File spool on container disk, resumed at boot (`server.py:163-165`) | Lost on redeploy | Per container | n/a | `outbound_opt_outs` |

Not work queues: `agent_mind/mind_service.py` (an HTTP session), `data_integrations/scheduler.py:76` (a PriorityQueue that is never instantiated), `auth.py:181` (a 2-thread password-hash pool), the periodic scheduler's `next_due` (enqueues are idempotency-keyed, `periodic.py:264-268`).

Contrast: `automation_jobs` is replica-safe. Claims use SKIP LOCKED with a lease owner, a token and a 120 s expiry (`automation_jobs.py:254-321`). Expired leases are reclaimed, attempts are recorded, jobs dead-letter at their limit, every update checks owner and token, and leases renew every 40 s (`:324-462`).

## Before `instance_count` > 1 (in this order)

1. **Voice:** give claims a lease (owner, token, expiry, renewal), as `automation_jobs` does. Replace the boot sweep with expired-lease reclaim. Make the interaction note idempotent per job.
2. **Reconstruction:** recover orphans (and release their GPUs) by expired heartbeat lease, not "every running row at boot". Reap only pods whose job is terminal or lease-expired, never by age alone.
3. **Video studio:** claim atomically into a non-`queued` state with a lease. Add an attempts cap. Use a provider-wide limit, not a per-process semaphore. Record the stored artifact before re-generating.
4. **Disposition enforcer:** take an advisory lock, or claim leads with SKIP LOCKED.
5. **Rehearse:** SIGKILL the worker during an active voice walkthrough, a running reconstruction and (if enabled) a video, and an API replica during an active call. Confirm each recovers once with no duplicate side effect.
6. Only then raise `instance_count`, and change the `worker_count` launch check with it.

Until then: keep one worker; the caps above bound concurrent work; `workers`, `scheduler`, `job_queue` and `media_jobs` page through `ops_alerts`.
