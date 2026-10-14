# ISMS 04: objectives, planning of changes and support (ISO/IEC 27001:2022 clauses 6.2, 6.3, 7.1–7.5)

| | |
|---|---|
| Status | **DRAFT, needs owner approval** (every target value below is a proposal) |
| Version | 0.1 |
| Date | 2026-10-14 |
| Document owner | Owner |
| Evidence basis | repository at `e3ea107` |

## 6.2 Information security objectives

Each objective is measurable from something the repository or the providers actually produce. Every value in the "Baseline" column comes from evidence. No objective has been measured under the ISMS yet.

| ID | Objective | Measure (how) | Target (proposed) | Baseline (evidence) | Frequency | Responsible | Due |
|---|---|---|---|---|---|---|---|
| OBJ1 | No known Critical or High security finding open at any release | `docs/security-launch-gate.json` conditions; DAST report | 0 open Critical/High at each production promotion | Gate verdict PASS; staging DAST 0 High (L17-01, 2026-10-07) | each release | Owner | ongoing |
| OBJ2 | Every `main` commit passes the security gates | Pass rate of CI jobs `backend`, `security`, `rls`, plus the `release` Trivy step (`ci.yml:43`, `:162`, `:194`, `:431`) | 100% of commits deployed to production | Not yet collected as a metric | monthly | Owner | from go-live |
| OBJ3 | Fixable CRITICAL vulnerabilities do not ship | Trivy gate in `release`; SBOM artifact | 0 at each release; fixable High fixed within 30 days (proposed) | Baseline 2026-09-25 in `docs/supply-chain-policy.md:17` | each release | Owner | ongoing |
| OBJ4 | Remove plaintext customer PII and communication content (A01) | Count of A01 columns still plaintext (privacy data map) | 5 column groups → 0 | 5 open: clients contact fields, `leads.payload`, `sms_messages.body`, `user_interactions.content`, `negotiation_events.transcript_excerpt` (L17-03) | quarterly | Owner | owner to set |
| OBJ5 | Recovery is proven, not assumed | `scripts/dr-drill.sh` result and measured RTO/RPO | Monthly drill pass; RTO ≤ 4 h and RPO ≤ 24 h for production (proposed) | Drill PASS, class-A RTO ~3 min on synthetic data; RPO undefined (`dr-drill-report.md`) | monthly | Owner | from go-live |
| OBJ6 | Credentials are rotated on schedule | Credential register (to create): last-rotated date per row of `docs/credential-rotation.md` | 100% of rows dated; DO token rotated within 7 days of approval | No register exists | quarterly | Owner | owner to set |
| OBJ7 | Data-subject requests answered on time | Privacy request receipts (`docs/privacy-request-runbook.md`) | 100% within the legal deadline (for example GDPR: one month; CCPA: 45 days) | No requests recorded | per request | Owner | ongoing |
| OBJ8 | Incidents handled and learned from | Incident log (to create) | 100% logged; post-incident review within 5 business days | No incident log exists | per incident | Owner | ongoing |
| OBJ9 | The ISMS runs its own cycle | Internal audit report; management review minutes | One full internal audit and one management review before Stage 1; then at least annually | None held | annual | Owner + external auditor | before Stage 1 |
| OBJ10 | Supplier terms are on file before data flows | Supplier register with DPA link per in-scope sub-processor | 100% of providers marked "Prod" in `docs/subprocessor-inventory.md` | 0 DPA records on file | before go-live, then annually | Owner | before go-live |

**Plan for achieving them (6.2 f–j):** the actions are the risk treatment plan in [`03`](03-risk-methodology-and-register.md) §6.1.3. Results are evaluated at the management review ([`05`](05-operation-evaluation-improvement.md) §9.3).

## 6.3 Planning of changes

A change to the ISMS is planned before it is made: its purpose, consequences, resources and responsibilities.

| Change type | Planned through | Record |
|---|---|---|
| Code, schema or infrastructure | CONTRACT C8–C10: one category per session; tests; LEDGER row updated in the same commit | git commit + LEDGER row |
| Deploy | CONTRACT C4.4 (push to `main` deploys staging); production by manual `promote` | CI run + release manifest artifact |
| New sub-processor | Supplier review + privacy data map entry (CONTRACT C5.1) before first data flow | supplier register entry |
| Scope or policy change | Edit of these ISMS documents, version bump, owner approval | commit + approval block |
| New owner decision | LEDGER § Owner decisions (CONTRACT C2.4) | decision row |

## 7.1 Resources

| Resource | Today | Needed for certification | Status |
|---|---|---|---|
| People | Owner only | Owner time for ISMS operation (estimate: a few hours a week); external internal auditor; optional ISO consultant | `owner_action` |
| Hosting | Staging only (≈ $109/mo, `docs/launch-state.md:24`) | Production environment (L19-02); cost not estimated in the repo | `owner_action` |
| Tooling | GitHub Actions, gitleaks, bandit, pip-audit, Trivy, ZAP (all in use) | Optional compliance-automation platform ([`../certification-roadmap.md`](../certification-roadmap.md)) | `implemented` / option |
| Budget | Not allocated | CB audit fees, internal audit, pentest, production hosting, 10DLC fees | `owner_action` |

## 7.2 Competence

| Role | Competence needed | Evidence (record to create) |
|---|---|---|
| Owner as ISMS manager | ISO/IEC 27001 requirements; risk assessment | Training certificate or self-study log (for example an ISO 27001 Lead Implementer course: an option, not a requirement) |
| Owner as developer and operator | Secure development (OWASP), PostgreSQL RLS, DigitalOcean operations | CV or experience summary; the repo's security review work is supporting evidence (`docs/security-launch-review.md`) |
| Internal auditor | ISO/IEC 27001 audit competence (for example ISO 19011 / Lead Auditor), independent of Neoh | Auditor CV and certificate, kept with the audit report |
| Contractors (future) | Role-specific | NDA + briefing record |

**No competence record exists yet.**

## 7.3 Awareness

Any person doing work under Neoh's control (owner, contractors, AI coding agents) must know the policy, how they contribute, and what non-conformance means.

| Audience | Mechanism | Record |
|---|---|---|
| Owner | Annual re-read and sign of the policy (02 §5.2) | signed approval block |
| Contractors | Policy + CONTRACT.md briefing at onboarding; NDA | briefing record |
| AI coding agents | `CLAUDE.md` → `docs/claude-prompts/CONTRACT.md` loaded every session (C1) | the files themselves (in git); not a substitute for human awareness |
| Customers (brokerage users) | Platform Use Policy acceptance gate | `backend/policy_contract.py`; acceptance rows (L17-05) |

## 7.4 Communication

| What | With whom | When | Who | How |
|---|---|---|---|---|
| Incident status | Affected brokerages | Per `docs/status-communication.md` phases | Owner | email or status templates |
| Personal data breach | Brokerages (controllers); regulators where Neoh has the duty | Within contract or statutory deadlines (counsel to confirm) | Owner | per `docs/security-incident-response.md` step 4 |
| Sub-processor changes | Brokerages | Before the change (DPA terms) | Owner | notice |
| Security questionnaires / policy requests | Prospects, customers | On request | Owner | policy PDF from this repo |
| Vulnerability reports | Researchers | On receipt | Owner | `security.txt` or contact address (**gap**: none published) |
| Audit communication | Certification body | Stage 1/2, surveillance | Owner | CB portal |

## 7.5 Documented information

How git versioning meets 7.5:

| 7.5 requirement | How it is met | Gap |
|---|---|---|
| 7.5.2 a) identification (title, date, author, reference number) | Each document has a header block: status, version, date, owner. git records author, date and the commit SHA as the reference | — |
| 7.5.2 b) format and media | Markdown in the repository; PDF exports for external parties | — |
| 7.5.2 c) review and approval | Approval = an owner-signed approval block, committed by the owner. Every change is a reviewable diff | No approval recorded yet |
| 7.5.3 a) available and suitable where needed | Repo on GitHub plus local clones | — |
| 7.5.3 b) protected (confidentiality, integrity) | GitHub access control; protected `main`; history immutable unless force-pushed | Confirm repo visibility is private and force-push is blocked on `main` (**owner_action**: screenshot) |
| 7.5.3 c–d) distribution, access, retrieval, storage, preservation, version control | git history = version control; `git log -- <file>` shows every revision | — |
| 7.5.3 e) retention and disposition | Documents: retained in git indefinitely. **Records** produced by CI (logs, SBOMs, release manifests) follow GitHub's Actions retention (default 90 days, configurable) | Export audit-relevant CI records into a dated evidence store before they expire (**gap**) |
| 7.5.3 external origin | DO, GitHub and Stripe assurance reports, DPAs, CB reports: stored in an evidence folder outside the public tree | No such folder or records yet (**owner_action**) |

**Records the ISMS will require (none exist yet; create them as they occur):**
- risk assessment run log;
- risk acceptances;
- SoA approval;
- competence records;
- awareness / briefing records;
- monitoring metric log;
- internal audit programme and reports;
- management review minutes;
- nonconformity and CAPA entries;
- incident log;
- supplier register with DPAs;
- access review records;
- credential rotation register;
- DR drill reports (the existing 2026-09-24 report counts as one pre-ISMS record);
- DAST and pentest reports (the existing staging ZAP reports in `docs/security/` count as pre-ISMS records).
