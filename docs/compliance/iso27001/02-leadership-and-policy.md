# ISMS 02: leadership, information security policy, roles (ISO/IEC 27001:2022 clauses 5.1–5.3)

| | |
|---|---|
| Status | **Approved by the owner, 2026-10-14 (v1.0)** |
| Version | 1.0 |
| Date | 2026-10-14 |
| Document owner | Owner |
| Evidence basis | repository at `e3ea107` |

## 5.1 Leadership and commitment

One person is top management. Each 5.1 duty is listed below with the record that would show it. **None of these records exists yet unless an evidence path is given.**

| 5.1 duty | How it is met | Record to keep (create) |
|---|---|---|
| a) Policy and objectives set and aligned with strategy | §5.2 policy below; objectives in [`04`](04-objectives-and-support.md) | Signed approval block (this file) |
| b) ISMS integrated into business processes | The ISMS uses the existing engineering controls: CONTRACT, the LEDGER, CI gates | CONTRACT C1–C12 (`docs/claude-prompts/CONTRACT.md`) |
| c) Resources available | Budget line for ISMS work (CB fees, internal auditor, tooling, production hosting) | Budget decision note (**owner_action**) |
| d) Importance communicated | Policy published in the repo and offered to customers on request | Commit of the approved policy |
| e) ISMS achieves its intended outcomes | Quarterly metric review ([`05`](05-operation-evaluation-improvement.md) §9.1) | Metric log |
| f) Persons directed and supported | Contractors and AI coding agents work under CONTRACT.md | Contractor NDA and briefing records (create on first engagement) |
| g) Continual improvement | CAPA ([`05`](05-operation-evaluation-improvement.md) §10) | CAPA log |
| h) Other managers supported | No other managers exist (`not_applicable` until hires) | — |

## 5.2 Information security policy (DRAFT)

> **Neoh information security policy, v1.0, approved by the owner 2026-10-14.**
>
> 1. **Purpose.** Neoh holds brokerages' client, deal, document, communication and billing data. We protect its confidentiality, integrity and availability. We never make security or compliance claims the evidence does not support.
> 2. **Scope.** As defined in ISMS 01 §4.3.
> 3. **Objectives.** Information security objectives are set, measured and reviewed under ISMS 04 §6.2. They are reviewed at each management review.
> 4. **Commitments.** Neoh commits to:
>    - (a) satisfy applicable legal, regulatory and contractual information security requirements, including customer DPAs, consumer-communication law and privacy law;
>    - (b) continually improve the ISMS;
>    - (c) treat risks according to the risk methodology in ISMS 03.
> 5. **Principles** (each one already binding as a CONTRACT clause):
>    - tenant isolation enforced in the database (C3);
>    - no external side effect without current human approval (C4);
>    - minimise personal data, with no PII or secrets in logs (C5);
>    - honest status vocabulary (C6);
>    - test before claiming (C9);
>    - no banned claims (C12).
> 6. **Topic-specific policies.** Listed in the table below. Each has an owner and a review date.
> 7. **Responsibilities.** As defined in ISMS 02 §5.3. Anyone working on Neoh (contractor or AI coding agent) works under CONTRACT.md and reports any security event to the owner immediately.
> 8. **Exceptions.** Only the owner may grant one, in writing, time-bound, recorded as a risk acceptance in the risk register.
> 9. **Review.** At least annually, and after any significant change: production go-live, a new sub-processor, a serious incident or a new hire.
> 10. **Availability.** This policy is kept in the repository (`docs/compliance/iso27001/02-leadership-and-policy.md`) and given to interested parties on request.
>
> Approved: ______________________ (owner)  Date: __________

### Topic-specific policies (Annex A 5.1): what exists vs what is missing

| Topic | Existing document | Status |
|---|---|---|
| Secure development and change | `docs/claude-prompts/CONTRACT.md` (C8–C10), `.github/workflows/ci.yml` | `partial`: not written as a policy, and has no approval record |
| Incident management | `docs/security-incident-response.md` | `partial`: no incident log exists yet; roles assume an on-call team |
| Credential and secret management | `docs/credential-rotation.md` | `partial`: no last-rotated register |
| Supply chain and vulnerability management | `docs/supply-chain-policy.md` | `partial`: SUP-1 open (no hash-locked Python) |
| Data retention and deletion | `docs/data-retention-policy.md` | `partial`: retention periods await counsel (`docs/privacy-counsel-checklist.md`) |
| Privacy and PII | `docs/privacy-data-map.md`, `docs/privacy-request-runbook.md` | `partial`: A01 open |
| Backup and recovery | `docs/dr-drill-report.md`, `docs/disaster-recovery-state-map.md` | `partial`: backups not scheduled (`dr-drill-report.md:88`) |
| Supplier management | `docs/subprocessor-inventory.md` | `partial`: no DPA records |
| Customer acceptable use | Platform Use Policy (`backend/policy_contract.py`, CONTRACT C5.7) | `implemented` (LEDGER L17-05) |
| Access control (incl. privileged access, access reviews) | — | `gap` |
| Cryptography and key management (incl. master-key escrow) | partly `docs/credential-rotation.md` | `gap` |
| Logging and monitoring | partly `backend/ops_alerts.py`, `infra/digitalocean/app.yaml:222` | `gap` |
| Endpoint / workstation and remote working | — | `gap` |
| Asset inventory and classification | partly the threat-model crown-jewels table, `backend/privacy_data_map.py` | `gap`: no consolidated register |
| Business continuity | — | `gap` |

## 5.3 Roles, responsibilities and authorities

Neoh is a **one-person company**. Every ISMS role below is held by the owner, except the internal auditor. That role cannot be: 9.2.2 requires auditor objectivity and impartiality, and the owner cannot audit their own work.

| Role | Holder | Authority | Note |
|---|---|---|---|
| Top management | Owner | Approves policy, scope, risk acceptance, budget | — |
| ISMS owner / manager | Owner | Runs the ISMS and reports on its performance (5.3 b) | Reports to themself; the management review record is the evidence |
| Risk owner (all risks) | Owner | Accepts residual risk | Register [`03`](03-risk-methodology-and-register.md) |
| Asset owner (all assets) | Owner | — | — |
| Platform admin / operator | Owner (env operator identity + `platform_admin` role) | Cross-tenant admin routes | `docs/security-threat-model.md` §Privileged operations |
| Developer / change author | Owner, plus AI coding agents under CONTRACT.md | Commits | Agents push only with owner approval (CONTRACT C4.4) |
| Change approver / deployer | Owner | Merge to `main` (= staging deploy), manual production promotion | `ci.yml:607` `promote` |
| Incident manager | Owner | Containment (recovery mode, revocation) | `docs/security-incident-response.md` |
| Privacy contact | Owner | Handles data-subject requests | Need for a GDPR DPO: counsel to determine (**owner_action**) |
| Internal auditor | **External contractor (vacant)** | Audits the ISMS | **owner_action**: engage one |
| Deputy / successor (key-person cover) | **Vacant** | Break-glass access only | **owner_action** (risk R04) |

### Segregation of duties (Annex A 5.3): compensating controls

ISO expects conflicting duties to be separated. With one person they cannot be. The table states the conflict plainly and names the compensating control. AI coding agents are **not** counted as an independent second person.

| Conflict | Compensating control | Status | Evidence or gap |
|---|---|---|---|
| Same person writes, approves and deploys code | Mandatory automated gates on every push: tests, RLS proofs, pip-audit, bandit, gitleaks, Trivy fixable-CRITICAL block. Production promotion deploys only digests staging verified | `implemented` | `ci.yml:43`, `:162`, `:194`, `:431`; `docs/supply-chain-policy.md` §Pinning ("Promotion never builds") |
| Same | GitHub environment protection: production restricted to `main`, plus a wait timer (a cooling-off period instead of a second reviewer) | `gap` | `docs/staging-setup.md:31` (staging: branch policy, no reviewers); production rules not verifiable from the repo (CI-1) |
| Same person is platform admin and the person who could tamper with the audit trail | The app role may only INSERT/SELECT on `audit_ledger`; the insert policy is tenant-scoped | `implemented` | `0119_security_review_hardening.sql`; threat model crown-jewels table |
| Same | Off-platform copy of audit and alert events to a mailbox or log store the operator account cannot edit | `gap` | No log forwarding in `infra/digitalocean/app.yaml` |
| Brokerage role changes | Two-person rule (requester may not approve) | `implemented` for tenant users | `backend/admin_ops.py:548`, `:568` |
| Operator acting on their own account | No second person. Compensated by operator MFA (TOTP or emailed code). An alert on each admin sign-in, sent to a separate mailbox, is still to be built | `partial` | `0121_operator_second_factor.sql`; security-launch-review.md §MFA. `backend/ops_alerts.py` has no sign-in alert (**gap**) |
| Owner self-assesses controls | External internal auditor (9.2), and an external pentest before production go-live | `owner_action` | `docs/security-pentest-brief.md` exists; no pentest record |
| Owner accepts their own risks | Every acceptance written with expiry and re-reviewed at management review; the internal auditor samples them | `gap` | register [`03`](03-risk-methodology-and-register.md) |
| Single key holder for `ORACLE_ENCRYPTION_MASTER_KEY` and the credentials | Escrow: sealed or offline copy plus password-manager emergency access to a named trusted person | `gap` | `dr-drill-report.md:92` ("not escrowed") |

## Approval

| Role | Name | Decision | Date | Signature or commit |
|---|---|---|---|---|
| Top management / ISMS owner | | approve / amend | | |
