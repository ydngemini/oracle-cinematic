# Re-verification of external audit chunks 02 (AI-001..005) and 03 (BILL-001..003, COMMS-001..002)

- Checkout: `/media/ydn/SYPHER_CORE2/Oracle`, branch `main`, `git rev-parse --short HEAD` = **54740ef** (matches the audit snapshot). Working tree clean.
- Method: read-only. Grep, then line ranges. I ran no tests. The only code I executed was a stdlib `python3 -I` check of `Decimal` behaviour.
- The audit files were treated as claims. Every line number below is from the local checkout.
- All of the audit's cited line ranges match the checkout to within about 3 lines. The one exception is BILL-003's "line 174": the `Decimal` call is at `billing_usage.py:177`.

## Summary

| id | verdict | file:line | cat | already tracked? | P | smallest fix |
|---|---|---|---|---|---|---|
| AI-001 | DESIGN-GAP (stronger than claimed: zero production consumers) | `backend/evidence.py:134-187`; no non-test importer of `evidence` | 06 | Yes: L06-03, ledger row 06 next task T06.03 (A02 adjacent) | P1 | T06.03 context compiler that feeds `EvidenceBundle.to_dict()` (with `unknowns` and `degraded`) into the chat context |
| AI-002 | DESIGN-GAP, plus stale docs (understated) | `missions/executor.py:209-218` (skip), `:271-283` (stale docstring), `missions/planner.py:52` (stale line ref) | 08 | Yes: L08-01 / L08-03 / L08-04 (PR #11 rebuild waits on the owner) | P2 | Fix both stale docstring bullets and the planner reference now; rebuild PR #11 after the owner answers |
| AI-003 | REFUTED as a live release path (latent dead branch) | `missions/policy.py:278,285-289`; `autonomy.py:178-184`; `db/migrations/0095…sql:283-285` | 08 | Partly. The owner question at LEDGER L08-01 (line 1179) is about the *mission grant*, not the dial. Review point (4) of L08-01 (LEDGER:1173) notes the task-channel risk. | P2 | Delete the `elif dial_allowed` branch so the dial is never a release authority |
| AI-004 | DESIGN-GAP, deliberate | `agent_twin.py:29-34`; only consumer is `intelligence_os_api.py:311,346,356-359` | 07 (T07.11) | In prompt 07 (T07.11 / AC07.09); **no ledger row** | P2 | Add the AC07.09 test (a twin preference cannot release or reorder); keep the twin advisory unless the owner decides otherwise |
| AI-005 | PARTIAL: two concrete defects the audit did not name | `ai_chat_store.py:1167-1169,1229-1236,1330-1340`; `ai_chat_agent.py:780,1178,1582-1585`; `0087_ai_tool_operations.sql:72-73` | 07 | **No.** The A09 row mentions only the staged-claim guard on Foundry/Bedrock. | P2 (P1 if the cross-tier duplicate test fails) | Make `call_index` turn-global across provider tiers, pass it on the Bedrock tier, and let a claim take over a `failed` row |
| BILL-001 | PARTIAL: wrong function for revenue, plus an unnamed cost-cap leak | `billing_usage.py:198-209` (`record_usage`), `:246-260` (`record_lead_engagement`), `:262-299` (reconcile, no caller); `ai_budget.py:286-296` | 18 | Partly: A05 row (LEDGER:3529) says "reconcile_lead_engagements has no caller". The missing-row case is not tracked. | P1 | A reconcile query for delivered speed-to-lead commands with no usage row, on a schedule, with an alert |
| BILL-002 | REFUTED in practice (latent) | `billing_usage.py:357`; key built at `outreach_lifecycle.py:311` | 18 | Adjacent: L18-06 (LEDGER:3263) "the usage drain relies on Stripe's 24 h identifier dedup" | P2 | Replace `[:100]` with a fixed-length digest when a second Stripe-reported metric is added; add a guard test now |
| BILL-003 | PARTIAL: real at the boundary, unreachable by current callers; misses Infinity | `billing_usage.py:177` | 18 | No | P2 | Move the conversion into the guarded block and reject values that are not finite |
| COMMS-001 | PARTIAL: monitoring exists (overstated); a crash window is invisible (understated) | `email_outbox.py:72-82,145-148,155-161`; `component_health.py:156-159,205-207`; `reconciliation.py:110` | 10 (T10.02) | No ledger row | P1 | Count and sweep stale `sending` rows whatever their error; mark them `delivery_unknown`; never resend |
| COMMS-002 | DESIGN-GAP (evidence / launch validation) | `outreach_compliance.py:255-340`; `email_outbox.py:95-126`; `messaging_api.py:432-529` | 10 / 20 | Yes: ledger row 10 blockers (10DLC, deploy approval, L10-01 real connect), A04, `production_verified` = 0 | P1 (launch gate) | Staging acceptance runs on test accounts, plus counsel/provider sign-off, before real outbound |

---

## AI-001: Evidence contract has no end-to-end consumer

**Code.**
- `backend/evidence.py:134-187`: `EvidenceBundle` validates C6 source status, refuses facts that cite unchecked sources (`:152-156`) and exposes `unknowns` / `degraded`.
- `grep -rln "EvidenceBundle|from evidence import|import evidence" backend --include=*.py` (excluding tests) returns **only `evidence.py` itself**.
- No `context_compiler` or `compile_context` exists anywhere in the backend.
- So the claim is stronger than "not demonstrated": no production code path builds or sends an `EvidenceBundle`. The belief adapter is inside `evidence.py:206+` (`facts_from_beliefs`).

**Verdict.** DESIGN-GAP, confirmed. The contract exists, has tests, and nothing uses it.

**Tracked.**
- LEDGER L06-03 (`LEDGER.md:1066`): "No consumer yet … The context compiler (T06.03) is the first consumer".
- Category row 06 next task: "T06.03 context compiler" (`LEDGER.md:41`).
- Adjacent: A02 (report source failures as failures).

**Category:** 06.

**Smallest fix and test.**
- Implement T06.03. The compiler builds an `EvidenceBundle` from caller-authorized sources and attaches `to_dict()` (including `unknowns` and `degraded`) to the chat system context.
- Failing-first test: `test_chat_context_carries_unknowns_when_a_source_failed`. Stub one source as failed and assert that the compiled model context contains `"degraded": true` and an `unknowns` entry naming that source.
- Then the AC06.03 live-Postgres test: `test_compiled_context_contains_no_record_outside_actor_scope`.

**Severity:** P1. **Customer impact:** answers are not structurally forced to say "unknown / source down"; a failed lookup can read as "no data".

## AI-002: Mission `task` channel unsupported

**Code.**
- `missions/executor.py:209-218`: a `task` action is set `skipped` with "task actions are not executed yet — no internal task surface". It returns `"withheld"` rather than a distinct `"skipped"` result; this is minor.
- `missions/planner.py:48,57,77`: `task` maps to `None` (no tool), so the planner refuses it before the model call. Pinned at `tests/test_missions_planner.py:215-229`.

**What the audit understated (stale documentation).**
1. **Docstring bullet 1 is stale.** `executor.py:271-283` says draft bodies are "not built here". In fact `drafter.draft_message` is called at `:298-305`.
2. **Docstring bullet 2 is also stale.** The docstring says `target.state_code` "has no source". In fact `_candidate_context` selects `state_code` from `leads` (`:554`, `:579`) and `_target_for` sets it (`:637`). The comment at `:230` repeats the same stale claim.
3. **Stale line reference.** `planner.py:52` cites "executor.py:178" for the skip; the skip is at `:209`.

**Verdict.** DESIGN-GAP (the capability is missing), with stale docs.

**Tracked.**
- L08-01 (`LEDGER.md:1162-1183`): PR #11 is not mergeable; its rebuild waits on the owner's grant answer.
- L08-03 / L08-04 (`:1195-1203`).
- Row 08 (`:43`).

**Category:** 08.

**Smallest fix and test.**
- Now: correct the docstring at `executor.py:271-283` and `:230`, and the reference at `planner.py:52`.
- Later: rebuild PR #11 per the L08-01 review: a new migration number, keep `action_deferred`, let RLS pick the client, and map 42501 to `blocked`.
- Failing-first test once tasks exist: `test_task_action_creates_one_client_task_and_no_command`. Assert one `client_tasks` row, zero `command_executions` rows, and an idempotent re-tick.

**Severity:** P2. **Customer impact:** a mission objective that implies internal to-dos does less than it reads; the plan says so, so nothing is silent.

## AI-003: Standing autonomy dial may authorize mission release

**What the code says.** The branch the audit cites exists: `missions/policy.py:285-289` reads `elif dial_allowed: authority = "standing autonomy dial"; may_release = True`. But for any outbound channel `dial_allowed` can never be True, for three independent reasons:
1. **Every outbound channel is irreversible.** `policy.py:278` sets `reversible = channel == "task"`, so email, sms and voice are irreversible. `autonomy.may_act` (`autonomy.py:180-184`) returns False for any irreversible action, even on autopilot.
2. **The code caps the dial.** `autonomy.py:42` sets `ASSIST_MAX = ("calls","texts","emails")` with ceiling `assist`. `may_act` returns False unless the level is `autopilot` (`:178-179`).
3. **The database caps the dial.** CHECK `autonomy_outbound_ceiling` (`db/migrations/0095_perception_and_living_graph.sql:283-285`) forbids `autopilot` for calls, texts and emails.

So the only channel where the dial can say yes is `task` (`crm_hygiene`, ceiling `autopilot`, reversible). The executor skips `task` before `_stage` and `_release` (`executor.py:209-218`). The only way an outbound action is released is a mission grant plus consent (`policy.py:283-286`).

`tests/test_missions_policy.py:111-117` pins that the dial is consulted and refuses.

**Latent issues.**
- The `elif` at `policy.py:288-289` contradicts the module docstring (`:19-24`, "the standing dial is not the release authority for a mission") and the comment at `:274-276`.
- When PR #11 makes `task` executable, this branch becomes a live release authority for internal tasks. This matches L08-01 review point (4) (`LEDGER.md:1173`).

**Verdict.** REFUTED as described: the dial cannot release an outbound action at this SHA. What remains is a dead branch, a P2 hygiene item.

**Tracked.**
- The open owner question (`LEDGER.md:1179`, row 08) is "does a mission's standing **grant** count as approval (C4.1)". The audit conflated the grant with the dial. The grant question is real and still open.

**Category:** 08.

**Smallest fix and test.**
- Delete `policy.py:288-289`, so `may_release` is only `granted and consented`.
- Failing-first test: `test_dial_on_autopilot_never_releases_a_task_action`. Stub `may_act` to return `(True, …)` for `crm_hygiene`, give the mission no `auto_channels`, and assert `verdict.may_release is False` and `release_authority == ""`.

**Severity:** P2. **Customer impact:** none today; this prevents silent autonomy once internal tasks ship.

## AI-004: Agent Twin is observational only

**Code.**
- `agent_twin.py:29-34`: "Nothing in this module feeds a model prompt or changes a ranking yet."
- Its only production consumer is the API in `intelligence_os_api.py:311` (`record_decision`), `:346` (`attach_rationale`) and `:356-359` (`GET` policy).
- Other modules import only its `wilson_interval` helper: `expected_value.py:95`, `missions/simulator.py:20`, `missions/evaluator.py:26`.
- `expected_value.fit` (`expected_value.py:80`) exists. `command_center.py:184` uses `expected_value.value_of`, not twin policy.

**Verdict.** DESIGN-GAP, and deliberate. The governing prompt requires it: prompt 07 T07.11 says the twin is "advice only … never … reprioritizes", and AC07.09 requires a "test that a twin preference cannot release or reorder an action" (`docs/claude-prompts/07-ai-gateway-model-tool-governance.md:54,81`).

The audit's "close the loop" remediation would conflict with T07.11 unless the owner decides otherwise.

**Tracked.** In prompt 07 (T07.11 / AC07.09). There is no LEDGER row (grep finds no `T07.11` / `AC07.09`).

**Category:** 07.

**Smallest fix and test.**
- Add AC07.09: `test_twin_policy_cannot_release_or_reorder`. Seed twin decisions strongly favouring one card kind; assert that the `command_center` ordering and `missions.policy` verdicts are identical with and without the twin data.

**Severity:** P2. **Customer impact:** none; it is just not yet personalised.

## AI-005: Execution ledger continuity

**What the audit got right.**
- The claim is taken in the mutation's own transaction (`ai_chat_store.py:1330-1340`).
- The receipt is attached separately and best-effort (`:1239-1275`).
- A claim without a receipt replays "already applied" (`:1201-1212`).
- Callers without `call_index` are unledgered (`:1918-1919`).

**What the audit understated: two concrete defects.**

1. **A `failed` row cannot actually be retried.**
   - `_ledger_replay` returns None for a `failed` row, documented as "safe to retry" (`:1167-1169`, `:1196-1197`).
   - The caller then runs `_ledger_claim`, a **plain INSERT** with no ON CONFLICT (`:1229-1236`).
   - That INSERT hits the unique `ai_tool_operations_identity (tenant_id, assistant_id, call_index)` (`0087_ai_tool_operations.sql:72-73`) still held by the `failed` row.
   - The UniqueViolation is caught by the generic handler (`:1966-1972`) and reported as "could not be persisted".
   - `tests/test_ai_tool_operations.py:114-118` (`test_a_failed_row_permits_a_retry`) checks only the replay helper, never the claim that follows.
   - Also: the name/hash mismatch check (`:1191-1195`) runs **before** the status check, so a `failed` row with different work raises `ToolIdentityMismatch`.

2. **Where it fires: provider fall-through, and the Bedrock tier is unledgered.**
   - `call_index` resets to 0 in every tier for the same `assistant_id`: Foundry at `ai_chat_agent.py:780`, `_local_fallback` (gateway providers and Fireworks) at `:1178`.
   - A tier that fails without record-changing actions falls through to the next tier (`:1497-1512`, `:1524-1536`, `:1547-1563`).
   - Gated staging receipts carry no `action_id` (`:537-544`), so a staged request does not stop fall-through.
   - In the next ledgered tier, calls at indices already used either raise mismatch ("conflicts with one already recorded"), hit defect 1, or replay the earlier receipt.
   - The final Bedrock tier calls `execute_safe_tool` **without `call_index`** (`:1582-1585`), so its effectful calls are entirely outside the ledger.
   - Whether this produces a duplicate staged approval across tiers is **unverified**. It depends on whether gated staging is idempotent by itself, and a test should settle it.

**Verdict.** PARTIAL. The audit called this a validation gap; there are reachable logic defects. Ledgered tiers fail closed (no double write, no false success). The Bedrock tier does not use the ledger at all.

**Tracked.** No. No LEDGER row mentions cross-tier `call_index`, the failed-row claim conflict, or the unledgered Bedrock tier. The A09 row (`LEDGER.md:3533`) covers only the staged-claim reply guard on Foundry/Bedrock.

**Category:** 07.

**Smallest fix and test.**
- (a) Keep one turn-level counter passed into every tier, for example a mutable counter on the bundle, instead of resetting at `:780` and `:1178`.
- (b) Pass `call_index` at `:1582-1585`.
- (c) In `_ledger_claim`, use `INSERT … ON CONFLICT ON CONSTRAINT ai_tool_operations_identity DO UPDATE SET status='completed', error_code=NULL, … WHERE ai_tool_operations.status='failed' AND tool_name=EXCLUDED.tool_name AND arguments_hash=EXCLUDED.arguments_hash`, and treat zero rows as a mismatch.

Failing-first tests (live Postgres):
- `test_failed_operation_can_be_reclaimed_live`: write a `failed` row for (assistant, 0), call `execute_safe_tool(..., call_index=0)` with the same work, and assert `ok` plus status `completed`.
- `test_fallthrough_tier_does_not_collide_with_previous_tier_ledger`: tier 1 stages a gated draft at idx 0 and then raises; tier 2 issues a different tool. Assert it is not refused for identity mismatch.
- `test_bedrock_tier_tool_calls_are_ledgered`: assert an `ai_tool_operations` row for a Bedrock-tier effectful call.

**Severity:** P2, escalating to P1 if the cross-tier duplicate-staging test fails. **Customer impact:** after a provider outage mid-turn, legitimate actions can be refused with confusing errors; Bedrock-tier writes have no replay protection.

## BILL-001: Failed usage insert leaves the action unmetered

**What the code does.**
- `billing_usage.py:198-209` (`record_usage`) swallows insert failures and returns False. This matches the claim. The test is at `tests/test_billing_usage.py:163-174`.
- But the only Stripe-reported metric is `lead_engaged` (`_STRIPE_REPORTED`, `:75`). `record_usage` **refuses** it (`_STAGE_BOUND`, `:82`, `:173-178`).
- `lead_engaged` is written only by `record_lead_engagement` (`:212-260`), called from `outreach_lifecycle.meter_delivered_engagement` (`outreach_lifecycle.py:272-314`). That path also swallows failures (`:254-260`).
- So for billing, the risk sits in `record_lead_engagement`, not `record_usage`. A failed `record_usage` loses only metrics that accrue locally and are not billed.

**Mitigations the audit missed.**
- `advance_first_response` calls `meter_delivered_engagement` on every delivered event, even when the row did not move (`outreach_lifecycle.py:267-268`). So a redelivered receipt retries metering.
- This is opportunistic, not a guarantee.

**Gaps.**
- `reconcile_lead_engagements` (`billing_usage.py:262-299`) has **no caller** (grep finds only its definition). It also only inspects rows that exist. It cannot find "delivered command, no usage row".

**What the audit missed entirely: the cost cap leaks.**
- `ai_budget.settle` (`ai_budget.py:286-296`) records token usage and **deletes the hold in the same transaction**.
- If the usage insert fails inside its SAVEPOINT, the hold is still deleted. Those tokens then never count toward the brokerage's monthly cap: `_TOTALS` reads `billing_usage_events` (`ai_budget.py:190-207`).
- The docstring states this as a gap (`:288-289`, "a usage row refused inside it … is not counted"). It is a leak in the per-tenant cost guard, which is a platform P1 priority.

**Verdict.** PARTIAL. It is real, but the revenue path is `record_lead_engagement`, and the larger unnamed effect is on the cost cap.

**Tracked.**
- Partly. The A05 row (`LEDGER.md:3529`) says "reconcile_lead_engagements has no caller and never reads Stripe".
- Not tracked: missing-row detection, and the settle/hold leak.

**Category:** 18.

**Smallest fix and test.**
- (a) Add a `missing_engagement` query: delivered `lead_response_events` (speed_to_lead) with a succeeded command and no `billing_usage_events` row. Run it, together with `reconcile_lead_engagements`, from the scheduler, and alert on non-zero.
- (b) In `settle`, delete the hold only when `record_inference` actually wrote rows, or have it raise on failure so the hold lapses.
- Failing-first tests:
  - `test_delivered_engagement_without_usage_row_is_reported`: insert a delivered event with no usage row; assert the reconcile output contains it.
  - `test_settle_keeps_hold_when_usage_insert_fails`: break the insert and assert the reservation row still exists.

**Severity:** P1. **Customer impact:** an under-billed engagement (revenue), and a brokerage able to exceed its configured AI budget.

## BILL-002: Meter identifier truncated to 100 characters

**Code.**
- `billing_usage.py:357`: `identifier=str(row["idempotency_key"])[:100]`.
- The drain sends only `lead_engaged` rows (`:331-343`, `_STRIPE_REPORTED`).
- Their key is always `f"lead-engaged:{anchor}"` (`outreach_lifecycle.py:311`), where the anchor is a UUID (`:299`). That is 13 + 36 = 49 characters, well under 100. Truncation never takes effect today.

**Related, also unreachable.**
- `record_usage` truncates keys to 240 characters (`:193`; test at `tests/test_billing_usage.py:155-160`). AI keys come from `ai_budget.py:468` (`chat:{uuid}:{lane}:{n}`, short).

**Verdict.** REFUTED in practice. It is latent: it only matters if a metric with long keys is ever added to `_STRIPE_REPORTED`.

**Tracked.** Adjacent: L18-06 (`LEDGER.md:3263`), "the usage drain relies on Stripe's 24 h identifier dedup". The dedup window, not truncation, is the real Stripe-side caveat.

**Category:** 18.

**Smallest fix and test.**
- Add a guard test now: `test_every_stripe_reported_key_fits_the_meter_identifier`. Assert that `len(f"lead-engaged:{uuid4()}") <= 100`, and that `_STRIPE_REPORTED == {"lead_engaged"}`, so adding a metric forces a review.
- When needed, replace with `sha256(tenant|metric|key).hexdigest()[:64]`.

**Severity:** P2. **Customer impact:** none today.

## BILL-003: Quantity conversion outside the guarded block

**Code.**
- `billing_usage.py:177` evaluates `Decimal(str(quantity)) < 0` before the `try` at `:196`.
- Local check: `Decimal("abc")` raises InvalidOperation; `Decimal("nan") < 0` raises InvalidOperation; `Decimal("inf")` passes (`< 0` is False) and would be inserted.

**Callers.**
- `record_inference` (`:131-145`) passes ints from `token_usage_of`, which coerces with `max(0,int(...))` (`llm_gateway.py:586-589`).
- `portfolio_api.py:1153` and `media_api.py:268` pass the literal `1`.
- With `conn=` set (portfolio, media), a raise would fail the caller's CRM action. No current caller can trigger it.

**Verdict.** PARTIAL. The contract hole is real but unreachable today. The audit missed that Infinity passes the check.

**Tracked.** No.

**Category:** 18.

**Smallest fix and test.**
- Parse inside a `try`; reject `not d.is_finite() or d < 0`.
- Failing-first test: `test_record_usage_rejects_nonnumeric_nan_and_infinity_without_raising`. Parametrize over `"abc"`, `float("nan")` and `float("inf")`; assert the result is False and nothing was inserted.

**Severity:** P2. **Customer impact:** none with current callers.

## COMMS-001: Ambiguous email delivery

**Code.**
- `email_outbox.py:145-148`: on `ProviderRequestError` the row stays `sending` with `delivery_unknown: …` and is never resent. Pinned at `tests/test_email_outbox.py:97-103`. This is correct.

**Monitoring exists (overstated by the audit).**
- `component_health.py:156-159` counts `status='sending' AND error LIKE 'delivery_unknown%'`, and `:205-207` reports DEGRADED.
- `admin_ops.py:520-560` lists outbox rows by status for platform admins.

**Gaps.**
- There is no resolve action. The only writers of `email_outbox` status are `email_outbox.py:73,157,168`.

**What the audit understated: an invisible crash window.**
- The claim commits `status='sending'` **without touching `error`** (`email_outbox.py:72-82`).
- **Crash before send.** If the worker dies after the claim but before or during the send, the job retry finds `not_queued` (`:84-85`). The row stays `sending` with a NULL or stale `held: …` error.
- **Send succeeded, final write failed.** If the post-send UPDATE (`:155-161`) fails after a real send, the same thing happens: the email was sent, the row reads `sending`, and `last_contacted_at` is never advanced.
- **Nothing sees these rows.** The health check counts only `delivery_unknown%` errors and only stale `queued` rows (`component_health.py:157-158`). Reconciliation re-enqueues only `status='queued'` (`reconciliation.py:110`). Both stuck states are invisible to health and to the sweep.

**Verdict.** PARTIAL.

**Tracked.** Not as a ledger row. The lifecycle requirement is prompt 10 T10.02 ("delivery unknown" state).

**Category:** 10.

**Smallest fix and test.**
- Health counts `status='sending' AND updated_at < now() - interval '15 minutes'` whatever the error.
- The reconciliation sweep turns stale `sending` rows into `error='delivery_unknown: worker lost after claim'`, never `queued`.
- Add a platform-admin "resolve as sent / failed / requeue (with reason)" action.
- Failing-first test: `test_stale_sending_row_without_error_is_reported_and_never_requeued`. Seed `status='sending', error=NULL, updated_at=now()-1h`. Assert health reports `unknown>=1`, the sweep sets the `delivery_unknown` prefix, and the status stays `sending`.

**Severity:** P1. **Customer impact:** an email can be sent while the CRM never shows the contact, or can silently never be sent, with nobody alerted.

## COMMS-002: Compliance gates without live sign-off

**Code (the audit's description is accurate).**
- `outreach_compliance.py:255-340`: suppression is absolute (`:282-284`); voice requires written consent (`:291-296`); calling window (`:301-306`); voice frequency cap (`:309-311`); BIPA states (`:316-317`).
- `email_outbox.py:95-126`: re-checks entitlement, suppression and footer at send time.
- `messaging_api.py:432-529`: unsigned Telnyx webhooks are rejected (`:446-457`). A failed STOP suppression returns 503 so Telnyx redelivers (`:499-511`). Orphan STOP is kept (`:465-470`).

**Verdict.** DESIGN-GAP (evidence). This is a launch-validation item, not a defect.

**Tracked.** Yes:
- Ledger row 10 blockers: Google OAuth, 10DLC, deploy approval, L10-01 real connect (`LEDGER.md:45`).
- A04 (production readiness).
- `production_verified` count is 0 (`LEDGER.md:60`).

**Category:** 10, certified in 20.

**Smallest fix and test.**
- Run a staging acceptance script on test accounts once the owner approves real sends (CONTRACT C4): opt-in, STOP, unsubscribe, a disabled subscription blocking a queued email, webhook redelivery after a forced 503, and voice consent refusal.
- Automatable now: `test_queued_email_blocked_after_unsubscribe_live`, `test_stop_redelivery_after_suppression_failure_records_once_live`.

**Severity:** P1 launch gate. **Customer impact:** none until real outbound starts; required before it does.

---

### Cross-cutting notes

- The audit was too pessimistic in AI-003 and BILL-002, where the code already blocks the scenario. It understated AI-005 (concrete ledger defects), BILL-001 (cost-cap leak, wrong revenue function) and COMMS-001 (invisible crash window).
- Not tracked in the ledger anywhere:
  - AI-005's defects;
  - BILL-001's missing-row reconcile and settle leak;
  - BILL-003;
  - COMMS-001's stale-`sending` gap;
  - AI-004's AC07.09 test.
