# ISMS 01: context, interested parties and scope (ISO/IEC 27001:2022 clauses 4.1–4.4)

| | |
|---|---|
| Status | **DRAFT, needs owner approval** |
| Version | 0.1 |
| Date | 2026-10-14 |
| Document owner | Owner (sole director, ISMS owner) |
| Evidence basis | repository at `e3ea107` |
| Approval record | none yet. The owner approves by signing the approval block at the end. |

This document defines what the information security management system (ISMS) covers. It does not show that any control works. Control status lives in [`annex-a-assessment.md`](annex-a-assessment.md) and [`statement-of-applicability.md`](statement-of-applicability.md). Engineering status lives only in [`LEDGER.md`](../../claude-prompts/LEDGER.md). Neoh is **not** ISO 27001 certified. Only an accredited certification body can issue that after a Stage 1 and Stage 2 audit (see [`../certification-roadmap.md`](../certification-roadmap.md)).

## 4.1 Context of the organization

**Purpose of the ISMS:** to protect the confidentiality, integrity and availability of brokerage and consumer data that Neoh processes. That data covers CRM records, communications, documents, billing state, property media and AI history. The ISMS also has to give brokerages evidence they can rely on.

### External issues

| # | Issue | Effect on the ISMS | Evidence |
|---|---|---|---|
| E1 | Hosting depends on one cloud, DigitalOcean (App Platform, Managed PostgreSQL, Managed Valkey, Spaces) | Physical, hypervisor and DB-engine controls are inherited. The ISMS must manage DO as a supplier | `docs/security-threat-model.md` §system; `infra/digitalocean/app.yaml` |
| E2 | Many communications providers (Plivo, Telnyx, Twilio, ElevenLabs) and carrier rules (10DLC brand and campaign registration) | Outreach availability and legality depend on third parties | `docs/subprocessor-inventory.md`; LEDGER L10-04, L10-09 |
| E3 | US consumer-protection law on outreach: TCPA, CAN-SPAM, state call-recording consent, state privacy laws (CCPA/CPRA) and state breach-notification laws | Legal requirements feed risk and controls (Annex A 5.31, 5.34) | `docs/privacy-counsel-checklist.md`; `backend/outreach_compliance.py` |
| E4 | Fair-housing law (no steering or scoring on protected characteristics) | AI and matching features are constrained | CONTRACT C5.6 |
| E5 | The model provider (Fireworks via LiteLLM) and GPU provider (RunPod) receive CRM context and property photos | Supplier and data-transfer risk | `docs/subprocessor-inventory.md` |
| E6 | Prompt injection through untrusted text (MLS remarks, notes, email, PDFs, callers) | A specific threat class for the AI tool layer | `docs/security-threat-model.md` actors table |
| E7 | Brokerage buyers increasingly ask for security assurance (ISO 27001 or SOC 2) | Drives this programme | Owner request, 2026-10-14 |
| E8 | Climate change (ISO/IEC 27001:2022/Amd 1:2024) | **Determined as not a material issue** for a cloud-hosted service with no premises. The owner confirms or overrides. Region or data-centre resilience is covered under E1 | Owner to confirm |

### Internal issues

| # | Issue | Effect on the ISMS | Evidence |
|---|---|---|---|
| I1 | **A one-person company.** The owner is top management, ISMS owner, sole developer, sole operator and sole platform admin | Segregation of duties (A 5.3) cannot be met by separate people. Compensating controls are in [`02-leadership-and-policy.md`](02-leadership-and-policy.md) §5.3. Key-person risk (R04) | brief; `docs/support-model.md` §Contacts ("OWNER DECISION, NOT YET MADE") |
| I2 | **No production environment exists.** Only staging (`neoh-staging`, app `3386001b…`) | ISMS operation and Stage 2 cannot be shown on a live production service until production exists (A04) | LEDGER L19-02 (`doctl apps list`, 2026-10-07) |
| I3 | Staging runs in recovery mode on purpose, so periodic jobs (sealing, sweeps) do not run there | Scheduled controls have not run in any deployed environment | LEDGER L17-03 (2026-10-14) |
| I4 | Development uses AI coding agents (Claude Code) working on the repository under `docs/claude-prompts/CONTRACT.md` | The AI tool vendor has source-code access. Agent output needs owner review (A 5.19, 5.23, 8.4, 8.25) | `CLAUDE.md`; `docs/claude-prompts/` |
| I5 | Legacy code and stacks remain in the repo: `src/` (Next.js prototype), `observability-platform/` (AWS), legacy AWS and Azure IaC | Reachability must be shown before these count in or out of scope | `docs/infrastructure-status.md`; audit §1 |
| I6 | MLS is out of scope (D-2026-10-12-01), but MLS data and migrations remain and may not be deleted without approval | Stored MLS data stays in scope as information at rest | LEDGER owner decisions |
| I7 | Pricing is undecided (D-2026-10-12-02). The ISMS budget is the owner's own | Constrains what can be bought (CB fees, tooling) | LEDGER owner decisions |

## 4.2 Interested parties and their requirements

| Party | Relevant requirements (what they need from the ISMS) | How the ISMS addresses it | Evidence or gap |
|---|---|---|---|
| Brokerages (customers, controllers of their CRM data) | Tenant isolation, confidentiality, availability, a DPA, a sub-processor list, breach notice, export and deletion at offboarding | FORCE RLS, erasure engine, offboarding runbook, sub-processor inventory | `docs/security-threat-model.md`; `docs/account-offboarding-runbook.md`; customer DPA template **gap** (owner and counsel) |
| Agents (users) | Account security, session revocation, MFA, policy terms | Session epoch revocation (`0117_session_epoch.sql`), Platform Use Policy gate | Broker-owner and agent MFA not built (security-launch-review.md §MFA) |
| Consumers / data subjects (clients, leads, callers) | Lawful processing, consent and opt-out honoured, access and erasure rights, AI-voice disclosure | `outreach_compliance.py`, privacy request runbook, consent-gated call notes | `docs/privacy-request-runbook.md`; A01 plaintext PII open |
| Owner (top management) | A certifiable ISMS at minimum cost, no false claims | This document set; CONTRACT C12 | — |
| DigitalOcean | Acceptable-use and account terms | Supplier management | DO DPA "confirm accepted" (`docs/subprocessor-inventory.md`) |
| GitHub | Terms; source and CI host | Environment protection, secret scanning | Protection rules not verifiable from the repo (CI-1) |
| Stripe | PCI DSS scope kept minimal through hosted Checkout | No card data touches Neoh | SAQ type to be confirmed by the owner (likely SAQ A; **owner_action**) |
| Carriers and The Campaign Registry (10DLC) | Registered brand and campaign, opt-out handling | 10DLC registration flow, STOP handling | LEDGER L10-04, L10-09 |
| Other sub-processors (Plivo, Telnyx, Twilio, ElevenLabs, Fireworks, RunPod, SMTP, public-data APIs) | Their terms and DPAs | Supplier register | No DPA records on file (**owner_action**) |
| Regulators (FTC, FCC, state AGs, CPPA; EU/UK supervisory authorities only if EU/UK personal data is processed, which counsel must confirm) | Legal compliance, breach notification within statutory deadlines | Incident response, counsel checklist | `docs/security-incident-response.md`; `docs/privacy-counsel-checklist.md` |
| Certification body (future) | An operating ISMS with records, an internal audit and a management review | This set plus the records listed in [`05`](05-operation-evaluation-improvement.md) | Not yet selected |
| Counsel | Retention periods, DPA terms, consent wording | Counsel checklist | Open items: `docs/privacy-counsel-checklist.md` |
| AI coding-tool vendor | Terms of use for the repo access granted | Owner reviews before merge or push | CONTRACT C4.4 (push = staging deploy) |

**Which requirements the ISMS addresses (4.2 c):** every row above is in scope. Requirements that only counsel can confirm are recorded as `owner_action` in the risk register ([`03`](03-risk-methodology-and-register.md)).

## 4.3 ISMS scope

### Scope statement (draft)

> The information security management system covering the design, development, deployment, operation and support of the **Neoh multi-tenant real-estate brokerage SaaS**. That comprises the FastAPI backend, the React/Vite web application, their PostgreSQL, Valkey and object-storage data, the GitHub repository and GitHub Actions CI/CD pipeline, the DigitalOcean-hosted staging and production environments, and the owner's administrative workstation. The service is operated by [legal entity name, **owner to supply**] from [location, **owner to supply**], in accordance with the Statement of Applicability version [x], dated [date].

### Boundaries and interfaces

| Element | In scope | Boundary or interface | Evidence |
|---|---|---|---|
| Neoh application code: `backend/`, `oracle-app/`, `backend/db/migrations/`, `infra/digitalocean/`, `scripts/` used for operations | yes | — | `docs/infrastructure-status.md` §ACTIVE PRODUCTION |
| DigitalOcean staging app (`api` ×2, `worker` ×1, `web`), Managed PostgreSQL 16, Managed Valkey, Spaces `neoh-media-staging` | yes | DO is responsible for physical, host and managed-engine controls (inherited). Neoh is responsible for configuration, IAM, trusted sources, secrets and data | `infra/digitalocean/app.yaml`; LEDGER L19-01, L17-02 |
| DigitalOcean **production** app and database | yes, **once created** | Does not exist today (L19-02). Certification Stage 2 needs it to exist and run under the ISMS | LEDGER L19-02 |
| GitHub repository `ydngemini/oracle-cinematic`, GitHub Actions (`.github/workflows/ci.yml`), the `staging` and `production` environments and secrets, the DO Container Registry | yes | GitHub and DO host the platforms (inherited); branch and environment rules are Neoh's | `.github/workflows/ci.yml:250`, `:607`; `docs/staging-setup.md:31` |
| Owner's workstation (development, `doctl`, `gh`, local `.env`, local dev DB) | yes | The workstation is the most privileged endpoint. Other projects on it (Weaver, YDNIDE, llama.cpp and so on) are **out of scope** as information, but the device controls (A 8.1) cover the whole device | — (no endpoint baseline documented: **gap**) |
| Sub-processors (list above) | interface only | Managed through supplier controls A 5.19–5.23. Their internal controls are out of scope | `docs/subprocessor-inventory.md` |
| Customer-side devices, brokerages' own email, calendar and phone accounts | out | Customer responsibility (shared-responsibility statement **to draft**) | — |
| AI coding agents operating on the repository | interface | Supplier with source access. Output reviewed by the owner before merge | `docs/claude-prompts/CONTRACT.md` |

### Exclusions and justification

ISO 27001 does not allow excluding clauses 4–10. Annex A control exclusions are justified only in the [Statement of Applicability](statement-of-applicability.md). The items below are **scope** exclusions.

| Excluded | Justification | Condition to keep it excluded |
|---|---|---|
| `src/` (historical Next.js cinematic prototype) | Not deployed by `infra/digitalocean/app.yaml` | Reachability check shows no shipping route (audit §1). If any part ships, it comes into scope |
| `observability-platform/` (AWS-oriented dashboard) | Not deployed. AWS is retired (`docs/infrastructure-status.md`) | Same as above |
| Legacy AWS and Azure IaC and accounts | LEGACY class, "never run for a release" (`docs/infrastructure-status.md`) | **owner_action:** confirm that no Neoh customer data or live credentials remain in those accounts, and that they are closed or emptied |
| `training_data/`, `backend/ml_forge/` training experiments | Not on a shipping path (bandit already excludes `ml_forge`, `ci.yml:190`) | No model trained there is served to customers. A08 must stay true (no heuristic valuations shown as appraisals) |
| `legal_sentinel/` | Specialised component. Integration not established (audit §1) | Re-assess if wired into a shipping route |
| MLS feature (search, ingestion, display) | Out of scope by owner decision D-2026-10-12-01 | **Stored MLS data is NOT excluded.** It stays in scope as information at rest in the in-scope database |
| Physical premises | No company office. Hosting is in DO data centres (inherited) | Home-office protection for the workstation is handled under A 7.x and 8.1 |

## 4.4 ISMS processes and their interactions

| Process | Inputs | Outputs | Interacts with | Where defined |
|---|---|---|---|---|
| Context and scope review | issues, interested parties | this document | risk assessment, management review | this file |
| Risk assessment and treatment | assets, threats, audit findings, LEDGER blockers | risk register, treatment plan, SoA | change, supplier, management review | [`03`](03-risk-methodology-and-register.md) |
| Secure development and change | CONTRACT, category prompts | commits, CI results, LEDGER rows | release, CAPA | `docs/claude-prompts/CONTRACT.md` C8–C10; `.github/workflows/ci.yml` |
| Release and deploy | CI-built images, staging-verified manifest | staging deploy; production promotion (manual) | change, monitoring | `ci.yml` `release` (`:250`), `promote` (`:607`) |
| Vulnerability management | pip-audit, Trivy, bandit, gitleaks, DAST | findings, fixes | change, risk | `ci.yml:82`, `:176`, `:186`, `:431`; `docs/supply-chain-policy.md` |
| Access and credential management | role changes, secrets | access reviews, rotations | incident, supplier | `docs/credential-rotation.md`; `backend/admin_ops.py:548` |
| Backup and recovery | DB, Spaces, master key | drill reports | continuity, monitoring | `docs/dr-drill-report.md`; `scripts/dr-drill.sh` |
| Incident management | alerts, reports | incident notes, lessons learned | CAPA, communication | `docs/security-incident-response.md`; `docs/status-communication.md` |
| Privacy requests and lifecycle | DSARs, retention schedule | receipts | legal, supplier | `docs/privacy-request-runbook.md`; `docs/data-retention-policy.md` |
| Supplier management | sub-processor list | DPA records, reviews | risk, legal | `docs/subprocessor-inventory.md` |
| Monitoring, internal audit, management review, CAPA | metrics, audit results | review minutes, corrective actions | every process | [`05`](05-operation-evaluation-improvement.md) |

## Approval

| Role | Name | Decision | Date | Signature or commit |
|---|---|---|---|---|
| Top management / ISMS owner | | approve / amend | | |
