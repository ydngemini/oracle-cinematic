# ISMS 03: risk methodology, register and treatment plan (ISO/IEC 27001:2022 clauses 6.1.1–6.1.3, 8.2, 8.3)

| | |
|---|---|
| Status | **DRAFT, needs owner approval** (methodology, acceptance criteria and every acceptance) |
| Version | 0.1 |
| Date | 2026-10-14 |
| Risk owner | Owner (holds every risk; see [`02`](02-leadership-and-policy.md) §5.3) |
| Evidence basis | repository at `e3ea107` |
| Seed sources | `docs/security-threat-model.md`; `docs/security-launch-review.md`; `docs/neoh-full-platform-source-audit-2026-10-08.md` (A01–A10); `docs/claude-prompts/LEDGER.md` blockers; `docs/dr-drill-report.md`; `docs/subprocessor-inventory.md`; `docs/supply-chain-policy.md` |

This is the **first** risk assessment. No earlier one exists. The register is seeded only from repository evidence. Scores are the author's proposal; the owner confirms or changes each score when approving. Annex A control numbers refer to ISO/IEC 27001:2022 Annex A. Applicability is decided in [`statement-of-applicability.md`](statement-of-applicability.md) and control status in [`annex-a-assessment.md`](annex-a-assessment.md).

## 6.1.1 Risks and opportunities for the ISMS itself

| # | Risk or opportunity | Action |
|---|---|---|
| M1 | Risk: the ISMS exists on paper only because one person runs everything | Small, recurring calendar of records ([`05`](05-operation-evaluation-improvement.md) §8.1). Automate evidence wherever CI already produces it |
| M2 | Risk: certification is attempted before production exists, so Stage 2 has nothing operating to sample | Roadmap gate: production first ([`../certification-roadmap.md`](../certification-roadmap.md)) |
| M3 | Opportunity: CI already produces repeatable security evidence (tests, RLS proofs, scans, SBOMs) | Reuse it as ISMS monitoring records |
| M4 | Opportunity: the LEDGER already tracks status with evidence and blockers | Use it as the technical CAPA log ([`05`](05-operation-evaluation-improvement.md) §10.2) |

## 6.1.2 Risk assessment method

**Approach:** asset-based. For each in-scope asset or asset group: threat × vulnerability → risk. The register is reproducible: a re-run on the same evidence gives the same risks.

**Asset groups** (taken from the threat model's crown-jewels table, `docs/security-threat-model.md`):

| ID | Asset group |
|---|---|
| AS1 | Tenant CRM data (clients, leads, deals, notes, tasks) |
| AS2 | Communications content (SMS, call transcripts, recordings, AI call notes) |
| AS3 | Contracts and documents |
| AS4 | Secrets and keys (`ORACLE_SECRET_KEY`, `ORACLE_ENCRYPTION_MASTER_KEY`, provider credentials, DO and GitHub tokens) |
| AS5 | Billing and entitlement state |
| AS6 | Property media and 3D assets |
| AS7 | AI memory and chat history |
| AS8 | Audit trail |
| AS9 | Service availability (staging; production once it exists) |
| AS10 | Source code and CI/CD pipeline |
| AS11 | Owner workstation |
| AS12 | Stored MLS data (legacy) |

**Likelihood (L), 1–5:**

| L | Meaning |
|---|---|
| 1 | Rare: no known path; would need several independent failures |
| 2 | Unlikely: a known path exists but is guarded by tested controls |
| 3 | Possible: guarded only partly, or the guard is untested in a deployed environment |
| 4 | Likely: the weakness is present and reachable, or is a known open finding |
| 5 | Almost certain: occurring now, or a standing condition |

**Impact (I), 1–5:**

| I | Meaning |
|---|---|
| 1 | Negligible: no customer data, no downtime |
| 2 | Minor: one tenant, internal data, under 1 h downtime |
| 3 | Moderate: one tenant's personal data, or a legal or contract breach without notification duty, or under 1 day downtime |
| 4 | Major: several tenants, notifiable personal data breach, regulator or carrier action, or over 1 day downtime |
| 5 | Severe: cross-tenant or platform-wide disclosure, loss of keys or data, or threat to business survival |

**Score = L × I (1–25).**

| Band | Score | Acceptance criteria (draft, **owner approves**) |
|---|---|---|
| Low | 1–4 | The owner may accept. Review annually |
| Medium | 5–9 | Accept only with a named treatment or a written rationale. Review at each management review |
| High | 10–14 | Treat. Temporary acceptance needs a written expiry of 90 days or less and a plan |
| Critical | 15–25 | **Not acceptable** for production go-live with real customer data. Treat before go-live |

**Current score** means the score with today's controls, at `e3ea107`. **Target** means the residual score expected after the treatment is in place and evidenced. A treatment counts as done only when its LEDGER row is `tests_pass` or better **in the deployed environment** (CONTRACT C6).

**Treatment options:** modify (add or strengthen a control), avoid, share (transfer), or retain (accept).

**Re-assessment triggers (8.2):** quarterly; production go-live; any new sub-processor; any serious incident; any audit finding A-xx or LEDGER blocker added or closed; any change to scope.

## 6.1.2 / 8.2 Risk register

All risks: owner = **Owner**. Abbreviations: Mod = modify, Ret = retain, Shr = share, Avd = avoid.

| ID | Asset | Threat → vulnerability | Source evidence | L | I | Current | Treatment (option: action) | Annex A | Target |
|---|---|---|---|---|---|---|---|---|---|
| R01 | AS1, AS2 | DB backup or DB-read leak → client name, email and phone, `leads.payload`, `sms_messages.body`, `user_interactions.content`, `negotiation_events.transcript_excerpt` are stored in plaintext | A01; RLS-8 (security-launch-review.md); LEDGER L17-03 scope blocker; `docs/privacy-data-map.md` | 3 | 5 | **15** | Mod: field-level encryption plus HMAC lookup for the listed columns (T17.04), with batched migration of historical rows | 8.24, 5.34, 8.11, 8.10, 8.13 | 2×5=10 |
| R02 | AS2 | Pre-0132 client notes stay plaintext because the sealing job does not run while recovery mode is on | LEDGER L17-03 (2026-10-14: 7 notes plaintext on staging) | 4 | 3 | 12 | Mod: run `backend/backfill_client_notes.py` on staging with approval. Production created without recovery mode seals on its first pass | 8.24, 8.32 | 1×3=3 |
| R03 | AS9, all | Production is created later without staging's proven controls (trusted sources, DAST, env protection, secrets, alerting) | A04; LEDGER L19-02, L17-01, L17-02 next_task | 3 | 4 | 12 | Mod: a production go-live checklist that repeats L17-01 and L17-02 and the launch gate on production | 8.9, 8.32, 8.29, 5.23 | 2×4=8 |
| R04 | AS4, AS9 | Owner unavailable (illness or loss) → no operator, no incident response; master key held by one person, not escrowed | brief (one-person company); `dr-drill-report.md:92`; `docs/support-model.md:7` | 3 | 5 | **15** | Mod: escrow the master key and break-glass credentials (sealed offline copy plus password-manager emergency access to a named person); successor runbook. Shr: business-interruption or cyber insurance (owner option) | 5.29, 5.30, 5.37, 8.13, 8.24 | 2×4=8 |
| R05 | AS1, AS8, AS10 | Insider error or misuse by the sole operator, with no segregation of duties | brief; [`02`](02-leadership-and-policy.md) §5.3 | 2 | 5 | 10 | Mod: the compensating controls in 02 §5.3 (env wait timer, off-platform audit copy, external internal audit, written acceptances) | 5.3, 8.2, 8.15, 5.35 | 2×4=8 |
| R06 | AS4, AS10 | The DO API token used by CI has not been rotated since it was set up; rotation is pending | Owner session notes (not a repo record; **owner to confirm**). Procedure: `docs/credential-rotation.md` §DigitalOcean API token; per-environment tokens: `docs/supply-chain-policy.md:72` | 3 | 4 | 12 | Mod: rotate now; a separate scoped token per GitHub environment; record rotation dates in a credential register | 5.17, 8.2, 8.5, 8.24 | 1×4=4 |
| R07 | AS1–AS8 | DB loss or corruption → logical backups unscheduled and local-only; RPO undefined; full-scale restore never timed | `dr-drill-report.md:88-96`; DO PITR 7-day window inherited (`dr-drill-report.md:29`) | 3 | 4 | 12 | Mod: scheduled encrypted off-host backups to private Spaces; `scripts/dr-drill.sh` in CI (25 s, `dr-drill-report.md:103`); quarterly PITR drill | 8.13, 8.14, 5.30 | 2×3=6 |
| R08 | AS6 | Object deletion or overwrite → Spaces versioning off; object recovery unproven | `dr-drill-report.md:94` | 2 | 3 | 6 | Mod: enable versioning or replication, or Ret with a written rationale | 8.13 | 1×3=3 |
| R09 | AS5 | Billing misconfigured at go-live → Stripe key, price and webhook secret have no values on staging, so checkout has never run in a deployed environment | LEDGER L18-04 | 4 | 3 | 12 | Mod: set test-mode values on staging (owner) and run one test checkout. Production boot already refuses a missing webhook secret (BILL-1 fix) | 8.29, 8.9, 5.20 | 1×3=3 |
| R10 | AS9, AS1 | Scheduled privacy and retention controls never exercised in a deployed environment → staging runs in recovery mode, so the scheduler is off | LEDGER L17-03 (worker log 'PeriodicScheduler NOT started') | 5 | 3 | **15** | Mod: production without recovery mode, with first-run evidence for sealing and retention sweeps; or a staged enablement of periodic jobs on staging | 8.10, 5.33, 8.16 | 2×3=6 |
| R11 | AS1 | Cross-tenant read via RLS defect or SQLi-driven GUC forgery | Threat model boundary 2 (RLS-1 residual MEDIUM); `security-launch-gate.json` (idor_sweep 0 leaks; rls CI job) | 2 | 5 | 10 | Mod (existing): FORCE RLS, `rls` CI job (`ci.yml:194`), IDOR sweep. Add an external pentest before go-live | 8.3, 8.26, 8.29, 8.28 | 1×5=5 |
| R12 | AS1 | Customer PII in logs → addresses, agent emails as `agent_id`, exception text; no staging log sample reviewed | A06; LEDGER L17-04 blockers | 3 | 3 | 9 | Mod: extend `tests/test_log_pii_guard.py`; review a staging log sample (AC17.05); move `agent_id` emails to ids (T17.09) | 8.15, 8.11, 5.34 | 2×2=4 |
| R13 | AS9, AS8 | Incident evidence lost → App Platform logs are short-lived; no log forwarding or retention | `docs/security-incident-response.md:11-15`; `infra/digitalocean/app.yaml` (no log destination) | 4 | 3 | 12 | Mod: forward logs to a retained store; define retention | 8.15, 5.28, 8.16 | 2×3=6 |
| R14 | AS9 | Undetected outage or attack → alerts reach only the logs if `ORACLE_ALERT_EMAIL` is unset; no uptime monitor | `infra/digitalocean/app.yaml:222-227`; whether it is set on staging is not recorded | 3 | 3 | 9 | Mod: set and test `ORACLE_ALERT_EMAIL` to a mailbox protected by strong 2FA; add an external uptime probe | 8.16, 5.25 | 2×2=4 |
| R15 | AS10, AS9 | Unreviewed change reaches staging or production → push to `main` deploys staging; staging environment has no reviewers; production rules unverifiable | CONTRACT C4.4; `docs/staging-setup.md:31`; CI-1 | 3 | 4 | 12 | Mod: production environment restricted to `main` with a wait timer; screenshot evidence; staging likewise | 8.32, 8.4, 5.3 | 2×3=6 |
| R16 | AS1 | Brokerage account takeover → no MFA for broker owners or agents | security-launch-review.md §MFA, AUTH-12 | 3 | 4 | 12 | Mod: TOTP MFA for broker owners (mandatory) and agents (optional) | 8.5, 5.17 | 2×4=8 |
| R17 | AS4, AS1 | Operator takeover → an emailed code is only as strong as that mailbox | security-launch-review.md §MFA | 2 | 5 | 10 | Mod: TOTP as the operator's default (`scripts/generate-operator-totp.py`); hardware-key 2FA on the mailbox, GitHub and DO accounts (owner) | 8.5, 8.2 | 1×5=5 |
| R18 | AS11, AS4 | Workstation compromise or loss → one device holds repo, `doctl`/`gh` sessions, `.env` (launch review: local Stripe key in LIVE mode) and the local DB | security-launch-review.md §Blockers item 4 | 3 | 5 | **15** | Mod: full-disk encryption, screen lock, OS patching, EDR or AV, separate admin browser profile, no live keys in local `.env`, local backups encrypted; documented baseline | 8.1, 7.9, 7.10, 8.7, 6.7 | 2×4=8 |
| R19 | AS10 | Malicious or vulnerable dependency → Python deps not hash-locked (SUP-1); 2 range pins invisible to the scanner | `docs/supply-chain-policy.md:47-51`; SUP-1 | 3 | 3 | 9 | Mod: compiled hash-locked lock file (`pip-compile --generate-hashes`), scanned in CI | 8.8, 5.21, 8.28 | 2×3=6 |
| R20 | AS1, AS2 | Sub-processor processes data without adequate terms → consumer Gmail SMTP has no DPA; DashScope default region Singapore (default ON for Plivo calls); ElevenLabs trains by default; DPAs "confirm" | `docs/subprocessor-inventory.md` | 4 | 4 | **16** | Mod: transactional SMTP or Workspace with DPA; re-region DashScope or disable it; opt out of ElevenLabs training; collect each DPA (owner). Avd: do not enable a provider without a DPA record | 5.19, 5.20, 5.21, 5.22, 5.23, 5.34 | 2×3=6 |
| R21 | AS9, legal | Carrier filtering or TCPA exposure → 10DLC brand and campaign not registered | LEDGER L10-04 (`external_configuration_required`) | 3 | 3 | 9 | Mod: register 10DLC (owner, paid, multi-day). Avd: no business SMS before registration | 5.31 | 1×3=3 |
| R22 | AS1, AS5 | Integrity defects shown to customers → failure presented as zero results (A02, fixed locally, undeployed); 3D "Full" badge without verified quality (A03, partial); engagement metered before delivery (A05, fixed locally, undeployed) | LEDGER L11-02, L11-03, L12-02, L18-02 | 3 | 3 | 9 | Mod: deploy the fixes with approval; build certification state (T12.09–T12.11) | 8.25, 8.29, 5.36 | 2×2=4 |
| R23 | AS1 | Messaging route race or duplicate 10DLC submission (A07): fixed locally, migrations 0136 and 0139 not deployed | LEDGER L10-06, L10-09 | 2 | 3 | 6 | Mod: deploy after approval | 8.25, 8.32 | 1×3=3 |
| R24 | AS1 | Prompt injection drives an unapproved action | Threat model boundary 5; `security-launch-gate.json` (ai_chain_attack 9/9) | 2 | 4 | 8 | Mod (existing): approval gates, offered-tool enforcement, model-unwritable contact fields. Re-test after each tool change | 8.26, 8.28, 5.15 | 2×3=6 |
| R25 | AS9 | Capacity or latency failure under load → nothing measured on production-shaped infrastructure; JIT cost on large visible counts (153 ms vs 1.2–1.5 s) | A10; LEDGER L16-01, L03-05 design blocker | 3 | 3 | 9 | Mod: capacity test on production; page counts or set `jit=off` for the app role if a route needs it | 8.6 | 2×2=4 |
| R26 | AS1, legal | Unlawful retention or late breach notice → retention periods and breach duties await counsel | `docs/privacy-counsel-checklist.md`; `docs/data-retention-policy.md` | 3 | 4 | 12 | Mod: counsel review (owner); notification matrix in the incident procedure | 5.31, 5.34, 5.24, 5.26 | 2×3=6 |
| R27 | AS12 | MLS licence breach → stored MLS data has no RLS by design and the feature is off | RLS-5; D-2026-10-12-01 | 2 | 3 | 6 | Ret, with entitlement filters (existing). Owner decides on purge under the licence terms (no deletion without approval) | 5.32, 5.33 | 2×3=6 |
| R28 | AS10 | Attack surface from unreachable or legacy code (`src/`, `observability-platform/`, `ml_forge` heuristics A08) | Audit §1; A08 | 2 | 3 | 6 | Mod: reachability inventory (category 01); remove or isolate; label heuristic targets | 8.25, 8.27 | 1×3=3 |
| R29 | AS4 | A static ACS or custom-call query secret appears in URLs | HOOK-3 (Open, LOW) | 2 | 2 | 4 | Ret, or move to a header or signature | 8.24, 8.5 | 2×2=4 |
| R30 | AS10 | AI coding agents with repo access introduce defects or exfiltrate code | [`01`](01-scope-and-context.md) I4; CONTRACT C4.4 | 2 | 3 | 6 | Mod: owner review before push; no secrets in the repo (gitleaks); the vendor's terms reviewed as a supplier | 5.19, 5.23, 8.4, 8.25 | 2×2=4 |
| R31 | All | Legacy AWS or Azure accounts still hold data or credentials | `docs/infrastructure-status.md` (LEGACY) | 2 | 3 | 6 | Mod: owner confirms the accounts are closed or empty; credentials revoked | 5.9, 8.10, 5.23 | 1×2=2 |

**Counts at current score:**

| Band | Count | Risks |
|---|---|---|
| Critical (≥15) | 5 | R01, R04, R10, R18, R20 |
| High (10–14) | 12 | R02, R03, R05, R06, R07, R09, R11, R13, R15, R16, R17, R26 |
| Medium (5–9) | 13 | R08, R12, R14, R19, R21, R22, R23, R24, R25, R27, R28, R30, R31 |
| Low (1–4) | 1 | R29 |

Total: 31 risks. R03 is High by score (12) but comes first in the treatment plan, because without production there is no ISMS operation to certify.

## 6.1.3 Risk treatment plan (ordered)

| Priority | Risks | Action | Kind | Effort | LEDGER link |
|---|---|---|---|---|---|
| 1 | R03, R10, M2 | Owner decision to create production; go-live checklist re-running L17-01 and L17-02 on production | owner_action + code/ops | L | L19-02 |
| 2 | R04, R07 | Escrow the master key; scheduled off-host encrypted backups; `dr-drill.sh` in CI | owner_action + code | M | proposed L19-04 |
| 3 | R20, R26 | DPAs, SMTP provider change, DashScope region, ElevenLabs opt-out; counsel on retention and breach | owner_action | — | proposed L17-06 |
| 4 | R18, R06, R17 | Workstation baseline; DO token rotation; TOTP and hardware-key 2FA | owner_action | S | proposed L19-05 |
| 5 | R01, R02 | Field encryption (T17.04); notes backfill on staging | code | L | L17-03 |
| 6 | R13, R14, R05 | Log forwarding and retention; alert mailbox; admin sign-in alert | code + config | M | proposed L19-06 |
| 7 | R15 | Environment protection with wait timer; evidence screenshots | owner_action | S | L17-01 |
| 8 | R16 | Broker-owner MFA | code | M | proposed L17-07 |
| 9 | R19 | Hash-locked Python lock | code | S | — |
| 10 | R09, R21–R25, R12 | Stripe test values; 10DLC; deploy the local fixes; capacity on production; log guard | mixed | M | L18-04, L10-04, L11-02, L16-01, L17-04 |

**Risk acceptance (6.1.3 f):** no residual risk has been accepted yet. The owner signs below per risk, or per band.

| Risk(s) | Accepted residual score | Expiry | Owner signature | Date |
|---|---|---|---|---|
| | | | | |

**Statement of Applicability (6.1.3 d):** see [`statement-of-applicability.md`](statement-of-applicability.md). Every Annex A control named in this register must appear there as applicable.

## 8.3 Records of treatment results

Keep a dated entry per completed treatment: risk ID, the LEDGER row and the status it reached, environment, evidence SHA, the score after treatment. **No entry exists yet.**
