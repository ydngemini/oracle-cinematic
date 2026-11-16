# External audit intake, 2026-11-13 (verified at `54740ef`)

An outside reviewer delivered a **partial** audit in six chunks on 2026-11-13, pinned at
`main` `54740ef` and PR #45 head `5e71240`. The chunk files are held by the owner; they
are not copied into the repository. Per CONTRACT C1.5, every finding was re-checked
against this checkout before it was entered here. **This page is a backlog, not proof
that anything is fixed.** Status lives in [LEDGER.md](../../claude-prompts/LEDGER.md#external-audit-2026-11-13);
this page records the verdicts and why.

How it was checked: three read-only verifier passes (notes beside this file:
[02-03](verify-02-03.md), [04-05dep](verify-04-05dep.md), [05web-06](verify-05web-06.md)),
each citing file:line at `54740ef`. Chunk 01 and the DR-001 verdict were also re-read by
hand. The notes are model-written. Re-read the cited lines before acting on one (C1.5).
No tests were executed by the verifier passes.

## Verdicts

| ID | Finding (short) | Verdict at `54740ef` | P | Owner | Notes |
|---|---|---|---|---|---|
| SEC-001 | Operator WebSockets skip the live session check | confirmed | P1 | 17 | Fixed on PR #45 (`da3394b`, L17-29): all 3 operator streams, handshake and every 60 s while open |
| SEC-002 | `?token=` webhook secret accepted by default | confirmed (known, L17-10) | P2 | 17 | No code builds such URLs any more; owner must confirm no provider console still uses one before the default flips |
| OBS-001 | Alert query failure reads as zero open alerts | confirmed | P2 | 19 | `admin_ops.health_components` |
| AI-001 | Evidence bundle not an end-to-end pipeline | design gap (wider than claimed) | P1 | 06 | Tracked: L06-03 / T06.03 |
| AI-002 | Mission `task` channel unsupported | design gap; stale docstrings | P2 | 08 | Tracked: L08-01/03/04 (PR #11) |
| AI-003 | Standing autonomy releases a mission | **refuted** | — | 08 | Dial yes only for reversible actions; outbound is irreversible and capped by a DB CHECK (0095). Becomes live if tasks become executable |
| AI-004 | Agent Twin observational only | design gap (deliberate) | P2 | 07 | Needs the AC07.09 test |
| AI-005 | Tool-call ledger interruption behaviour | confirmed, **worse than claimed** | P1 | 07 | A failed row can never be retried; `call_index` restarts per provider tier; the Bedrock tier bypasses the ledger. All fail closed |
| BILL-001 | Failed usage persistence → unmetered action | confirmed, different site | P1 | 18 | Billed path is `record_lead_engagement`; no alert for a delivered command with no usage row; `ai_budget.settle` drops the hold even when the usage insert failed |
| BILL-002 | Meter id truncated below key length | **refuted** (latent) | P2 | 18 | Only `lead_engaged` is reported; its key is 49 chars. Guard test only |
| BILL-003 | Quantity parse escapes a never-raising fn | confirmed, unreachable today | P2 | 18 | Also accepts Infinity |
| COMMS-001 / RUN-04 | Email stuck in `sending` is invisible | confirmed, **more paths** | P1 | 10 | A crash after the claim, a failed requeue, or a failed "sent" write all leave `sending`; health and the sweep ignore it |
| COMMS-002 | Live jurisdiction/provider sign-off unverified | evidence gap | P1 gate | 10/20 | Tracked: row 10 blockers, A04 |
| DR-001 / REC-01 | Restore "VERIFIED" without the master key | **confirmed** | P1 (P0 pre-prod) | 19 | `scripts/verify-restore.sh:178-189`: no key = WARN, exit 0. Contradicted `docs/disaster-recovery-state-map.md` (corrected here) |
| REC-02 | Round trip uses a fixed literal | **confirmed** | P1 | 19 | Encrypts and decrypts with key `'k'`; never reads restored ciphertext or the real key |
| REC-03 | `pg_restore` errors tolerated | confirmed | P1 | 19 | Log deleted; counts checked for a subset |
| REC-04 | RLS check passes on partial protection loss | partial | P1 | 19 | Passes if any one table has RLS |
| OPS-001 | Backup schedule, off-host copy, key escrow | environment | P1 (P0 pre-prod) | 19 | Tracked: row 17, L17-18, A04. Owner action |
| JOB-001 | Reused idempotency key returns a different job | partial (latent) | P2 | 08 | Compare type + payload hash; 409 on mismatch |
| JOB-01 | Job completion and attempt audit not atomic | confirmed | P2 | 08 | |
| MIG-001 | Direct migration notices drift late | partial | P2 | 19 | Production path is guarded |
| DEP-01 | Release pipeline blocked | confirmed / environment | P0 (pipeline) | 19 | `doctl auth` 401 since 2026-11-08; owner must restore DO access |
| DEP-02 | Rollback trusts local migration names | confirmed | P1 | 19 | |
| DEP-03 | Rollback doesn't check the target app | confirmed | P1 | 19 | |
| DEP-03b | Rollback spec has no domains block | **new**, confirmed | P1 when prod has a domain | 19 | DO replaces the whole spec: would detach the hostname |
| DEP-04 | Prod drift guard fails open | confirmed | P1 | 19 | |
| DEP-05 | Rollback classifier is syntactic | confirmed | P2 | 19 | |
| DEP-06 | Post-rollback smoke is advisory | confirmed | P2 | 19 | |
| DEP-07 | One worker; concurrency unproven | design gap | P2 | 19/16 | Tracked: A10. The audit's 100-agent target is not the owner's (20 + owner) |
| DEP-08 | Storage smoke can pass when storage is broken | confirmed, seen in a run | P2 | 19 | Last green staging run warned Spaces unreachable |
| DEP-09 | Old readiness runs aren't launch proof | evidence gap | P1 | 19 | Tracked: A04 |
| WEB-001 | Blocked IndexedDB delete treated as done | partial, different defect | P2 | 17/16 | Service worker and captureStore never close handles, so captures survive sign-out although Capture Studio says they are discarded. Cross-user read not supported |
| WEB-002 | Storage exceptions break login/restore/sign-out | confirmed | P2 | 02 | Restore can say "still signed in" wrongly |
| WEB-003 | Dashboard JWT in WebSocket subprotocol | design gap | P2 | 19 | Router unmounted on DO; session half fixed by SEC-001 |
| UX-001 | Status banner hides first failure | design gap (pinned by test) | P2 | 02 | |
| RUN-01 | Accepted chat turn with no durable job | confirmed | P1 | 07 | Spinner until reload; holds 1 of 2 chat slots up to an hour |
| RUN-02 | Inbound call finalize failure has no retry, incl. opt-outs | **confirmed** | **P0** | 09 | TCPA: a lost opt-out lets later AI calls through. Docstring promises a retry that doesn't exist |
| RUN-03 | ElevenLabs says "saved" before the save | confirmed | P1 | 09 | |
| RUN-05 | Plivo callback acked after a failed outcome write | confirmed | P1 | 10 | Cleanup also drops the state, losing the only `lead_engaged` row (A05) |
| RUN-06 | Losing media task cancelled without await | partial | P1 (opt-out) | 09 | An in-flight opt-out write can be aborted |

## Suggested order

1. **RUN-02** (P0, compliance): record inbound opt-outs per utterance, as outbound already does; sweep and alert on calls stuck at `transcript_status='active'`.
2. **DR-001 + REC-02/03/04** (P0 before production): the verifier must fail without the real key and prove decrypting restored ciphertext.
3. **RUN-04/COMMS-001, RUN-01, RUN-05, RUN-03, BILL-001, AI-005**: durability and honest outcomes, mostly one more step in `reconciliation.run_sweep` plus a health count.
4. **DEP-02/03/03b/04**: rollback and drift-guard failure paths.
5. P2s by category.

Owner actions surfaced: DEP-01 (DigitalOcean access), OPS-001 (backups, escrow), SEC-002 (provider callback URLs).
