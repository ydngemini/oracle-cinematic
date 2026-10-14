# ISMS 05: operation, performance evaluation and improvement (ISO/IEC 27001:2022 clauses 8.1, 9.1–9.3, 10.1–10.2)

| | |
|---|---|
| Status | **DRAFT, needs owner approval** |
| Version | 0.1 |
| Date | 2026-10-14 |
| Document owner | Owner |
| Evidence basis | repository at `e3ea107` |

**No internal audit, management review, CAPA entry or ISMS metric record exists yet.** This document defines them. The first records are created when the owner starts operating the ISMS.

## 8.1 Operational planning and control

### ISMS operating calendar (one person, minimum viable)

| Cadence | Activity | Output record | Source / tool |
|---|---|---|---|
| Every push | CI: tests, RLS proofs, pip-audit, gitleaks, bandit; Trivy + SBOM on release | CI run (export monthly) | `.github/workflows/ci.yml` |
| Every production promotion | Manual `promote`; rollback target captured | release manifest + rollback artifact | `ci.yml:607`, `:751` |
| Weekly (15 min) | Read alerts, CI failures, dependency advisories; log any security event | metric log / incident log | `backend/ops_alerts.py`; GitHub |
| Monthly | DR drill (`scripts/dr-drill.sh`, `scripts/dr-drill-redis.sh`); export CI evidence; metric snapshot | drill report; evidence folder | `docs/dr-drill-report.md` |
| Quarterly | Access review (DO, GitHub, Stripe, providers, platform admins, tenant broker owners); credential rotation check; risk register refresh; supplier changes | access review record; credential register; register version | `docs/credential-rotation.md`; [`03`](03-risk-methodology-and-register.md) |
| Quarterly | DAST on production (ZAP baseline + authenticated active, rate-bounded, test tenant) | ZAP report | `docs/security/zap-*-staging-2026-10-06.md` (method) |
| Annually | Pentest (external); policy review; supplier review (DPAs, assurance reports); internal audit; management review; DigitalOcean PITR drill | reports, minutes | `docs/security-pentest-brief.md` |
| On change | Risk re-assessment triggers ([`03`](03-risk-methodology-and-register.md) §6.1.2) | register version | — |

### Control of externally provided processes (8.1, last paragraph)

| Process | Provider | Control | Evidence to hold |
|---|---|---|---|
| Hosting, DB, cache, object storage | DigitalOcean | Trusted sources; private Spaces; per-environment scoped token; annual review of DO assurance reports | L17-02; DO DPA acceptance (**owner_action**) |
| Source and CI/CD | GitHub | Environment protection; secret scanning; 2FA on the owner account | screenshots (**owner_action**) |
| Payments | Stripe (hosted Checkout) | No card data touches Neoh; webhook signature + replay; dedup ledger (0118) | Stripe PCI attestation (download), SAQ (**owner_action**) |
| Telephony, SMS, voice, LLM, GPU, SMTP | per `docs/subprocessor-inventory.md` | DPA before enabling; data minimisation; deletion path | supplier register (**owner_action**) |
| Software development assistance | AI coding agents | CONTRACT.md; owner review before push | git history |

## 9.1 Monitoring, measurement, analysis and evaluation

Only metrics the repository or its providers can actually produce are listed here.

| Metric | What is monitored | Method (reproducible) | Who | When analysed | Linked objective |
|---|---|---|---|---|---|
| M1 CI security-gate pass rate | `backend`, `security`, `rls`, Trivy step | `gh run list --workflow ci.yml --json conclusion,headSha` over the period | Owner | monthly | OBJ2 |
| M2 Dependency CVEs | pip-audit (`ci.yml:82`), `npm ci` lockfile, Trivy fixable CRITICAL/High | CI logs + SBOM artifact per release | Owner | monthly | OBJ3 |
| M3 Secrets in history | gitleaks full-history scan (`ci.yml:176`) | CI result | Owner | each push | OBJ1 |
| M4 SAST new findings | bandit vs `backend/.bandit-baseline.json` (`ci.yml:186`) | CI result; baseline size trend | Owner | monthly | OBJ1 |
| M5 Tenant isolation proofs | `rls` job: every migration + RLS/revocation/privilege SQL proofs (`ci.yml:194`) | CI result | Owner | each push | OBJ1 |
| M6 DAST High/Medium count | ZAP on the deployed environment | ZAP report (`docs/security/`) | Owner | quarterly | OBJ1 |
| M7 Recovery | DR drill pass/fail, measured RTO, achieved RPO | `scripts/dr-drill.sh` output (`docs/dr-drill-report.md` format) | Owner | monthly | OBJ5 |
| M8 Open audit findings | A01–A10 status | LEDGER § Audit cross-reference | Owner | monthly | OBJ1, OBJ4 |
| M9 Open security blockers | LEDGER rows in category 17/19 with `blocked` / `external_configuration_required` | `grep` of LEDGER | Owner | monthly | — |
| M10 Plaintext PII columns | A01 column groups remaining | `docs/privacy-data-map.md` | Owner | quarterly | OBJ4 |
| M11 Credential age | days since last rotation, per credential | credential register (to create) | Owner | quarterly | OBJ6 |
| M12 Incidents | count, severity, time to contain | incident log (to create) | Owner | quarterly | OBJ8 |
| M13 Privacy requests | count, days to complete | receipts (`docs/privacy-request-runbook.md`) | Owner | quarterly | OBJ7 |
| M14 Component health | `/health` component states, alert emails | `backend/component_health.py`, `backend/ops_alerts.py` | Owner | weekly | — |

## 9.2 Internal audit programme

**Auditor:** an external, competent, independent person engaged by the owner (**owner_action**). The owner may not audit their own work (9.2.2 c: objectivity and impartiality).

| Cycle | Scope | Timing | Method |
|---|---|---|---|
| Year 0 (pre-certification) | **All** of clauses 4–10 plus every applicable Annex A control in the SoA | After at least one full operating calendar month with records, and before management review and Stage 1 | Document review + sampling of records + technical spot checks (CI runs, DO settings, access lists) |
| Years 1–2 (surveillance) | Clauses 4–10 every year; Annex A split so that every applicable control is audited at least once in 3 years; high risks yearly | Before each surveillance audit | Same |
| Triggered | Area of a serious incident or major change | Within 3 months | Focused |

**Audit plan / report template** (copy per audit):

```
Audit ID: IA-YYYY-NN    Auditor: <name, independence statement>    Dates:
Scope (clauses / controls):            Criteria: ISO/IEC 27001:2022, ISMS 01–05, SoA vX
Records sampled (path / SHA / date):
Findings: | # | clause/control | type (major NC / minor NC / observation / OFI) | evidence | 
Conclusion on conformity and effective implementation:
Report delivered to: Owner (top management)   Date:
```

## 9.3 Management review

**Frequency:** before Stage 1, then at least annually, and after any serious incident. **Attendee:** the owner (top management). With one person, the review is a dated, written record. It is still required.

**Agenda / minutes template (inputs 9.3.2 a–g, outputs 9.3.3):**

```
Management review MR-YYYY-NN    Date:    Attendee(s):
a) Status of actions from previous reviews:
b) Changes in external and internal issues (ISMS 01 §4.1):
c) Changes in needs and expectations of interested parties (ISMS 01 §4.2):
d) Feedback on information security performance:
   1. nonconformities and corrective actions (CAPA log)
   2. monitoring and measurement results (M1–M14)
   3. audit results (IA reports; external audits)
   4. fulfilment of objectives (OBJ1–OBJ10)
e) Feedback from interested parties (customer questions, complaints, CB):
f) Results of risk assessment and status of risk treatment plan (ISMS 03):
g) Opportunities for continual improvement:
OUTPUTS (9.3.3):
- Decisions on continual improvement opportunities:
- Any need for changes to the ISMS (scope, policy, resources, objectives):
- Risk acceptances confirmed / renewed:
Signed (top management):
```

## 10.1 Continual improvement

Sources: CAPA trends, metrics, audit observations, incident lessons, and the LEDGER "next_task" fields. Improvement decisions are recorded in management review outputs.

## 10.2 Nonconformity and corrective action

**Procedure:**

1. **Detect.** Sources: CI failure on `main`, audit finding, incident, missed calendar activity, failed drill, customer complaint, LEDGER regression.
2. **React (10.2 a).** Control and correct it, and deal with the consequences. Use containment from `docs/security-incident-response.md` if security-relevant.
3. **Evaluate the need for action (10.2 b).** Review the NC, find its root cause (5-whys is enough), and check whether similar NCs exist or could occur. A grep across the codebase for the same pattern is expected (the LEDGER already does this, for example the L17-04 AST sweep).
4. **Implement action (10.2 c).** A tested slice per CONTRACT C8–C10.
5. **Review effectiveness (10.2 d).** Re-run the evidence command in the deployed environment. The status must reach `tests_pass` or better in the environment where the NC occurred.
6. **Update the ISMS if needed (10.2 e).** Risk register, policies, this procedure.
7. **Keep records (10.2 f–g)** of the nature of the NC, actions taken and results.

**CAPA log: the LEDGER, with one limit.** `docs/claude-prompts/LEDGER.md` already records, per row:
- `id` and title;
- `status` (CONTRACT C6 vocabulary);
- `env`;
- `evidence_shas`;
- `tests_run` (command, result, date);
- `blockers`;
- `audit_refs`;
- `next_task`;
- `updated`.

That covers 10.2 a, c, d and g for **technical** nonconformities. It does **not** record root cause or an explicit effectiveness check, and it does not hold non-technical NCs (a missed review, a missing DPA). Proposal (needs owner approval, because LEDGER edits are outside this document set):
- Add a `capa:` block to a LEDGER row when that row is an ISMS nonconformity: `{nc_id: NC-YYYY-NN, source, root_cause, correction, corrective_action, effectiveness_check: {cmd, env, result, date}, closed_by_owner: date}`.
- Non-technical NCs get rows in a new LEDGER category `ISMS` (L21-xx), or a separate `docs/compliance/iso27001/capa-log.md`.

**No nonconformity has been recorded under the ISMS yet.** Pre-ISMS defects already fixed with evidence (for example security-launch-review.md BILL-1, RLS-2; the DAST fixes in L17-01) are history, not CAPA records.
