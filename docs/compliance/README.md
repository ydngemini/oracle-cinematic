# Compliance programme index

| | |
|---|---|
| Status | **DRAFT, needs owner approval** (every document in this folder is a draft) |
| Version | 0.1 |
| Date | 2026-10-14 |
| Owner | Owner (sole director, ISMS owner) |

On 2026-10-14 the owner asked for ISO/IEC 27001 compliance and certification, fast. They also asked about SOC 2, GDPR/CCPA, HIPAA and PCI DSS. This folder is the **path** to that. It is not proof of it.

**What these documents are not.** Neoh is **not**:
- ISO 27001 certified or "compliant";
- SOC 2 compliant;
- HIPAA compliant;
- PCI DSS compliant.

Do not write or present any such claim (CONTRACT C12).

Certification is issued only by an accredited certification body after a Stage 1 and a Stage 2 audit of an operating ISMS. Engineering status stays in [`../claude-prompts/LEDGER.md`](../claude-prompts/LEDGER.md) only.

## Documents

| File | ISO 27001:2022 | Content | Maintained by |
|---|---|---|---|
| [`iso27001/01-scope-and-context.md`](iso27001/01-scope-and-context.md) | 4.1–4.4 | Context, interested parties, scope, boundaries, exclusions, ISMS processes | this set |
| [`iso27001/02-leadership-and-policy.md`](iso27001/02-leadership-and-policy.md) | 5.1–5.3 | Leadership duties, information security policy (draft), topic-policy gaps, roles, segregation-of-duties compensations | this set |
| [`iso27001/03-risk-methodology-and-register.md`](iso27001/03-risk-methodology-and-register.md) | 6.1.1–6.1.3, 8.2, 8.3 | Risk method (L×I 1–5), acceptance criteria, 31-risk register seeded from repo evidence, treatment plan | this set |
| [`iso27001/04-objectives-and-support.md`](iso27001/04-objectives-and-support.md) | 6.2, 6.3, 7.1–7.5 | Measurable objectives, change planning, resources, competence, awareness, communication, documented-information control (git) | this set |
| [`iso27001/05-operation-evaluation-improvement.md`](iso27001/05-operation-evaluation-improvement.md) | 8.1, 9.1–9.3, 10.1–10.2 | Operating calendar, metrics the repo can produce, internal audit programme, management review template, CAPA procedure (LEDGER as CAPA log) | this set |
| [`iso27001/annex-a-assessment.md`](iso27001/annex-a-assessment.md) | Annex A (93 controls) | Evidence-backed status per control | separate workstream |
| [`iso27001/statement-of-applicability.md`](iso27001/statement-of-applicability.md) | 6.1.3 d | Applicability and justification per control | separate workstream |
| [`framework-crosswalk.md`](framework-crosswalk.md) | — | ISO 27001 ↔ SOC 2 ↔ GDPR/CCPA ↔ HIPAA ↔ PCI DSS | separate workstream |
| [`certification-roadmap.md`](certification-roadmap.md) | — | Fastest honest route to a certificate, timeline estimate, owner actions | this set |
| [`saas-checklist-map.md`](saas-checklist-map.md) | — | The owner's Zylo SaaS checklist (10 items) mapped to evidence; PIPEDA/LGPD/SOX/FedRAMP applicability; TLS verified on staging | 2026-10-14 |

## Status vocabulary (every control and requirement)

| Status | Meaning |
|---|---|
| `implemented` | Repo or ledger evidence exists (file:line, commit, or LEDGER row) |
| `partial` | Some of it exists; the gap is named |
| `gap` | Nothing exists |
| `inherited` | Provided by DigitalOcean, Stripe or GitHub; what Neoh must still do is stated |
| `not_applicable` | Does not apply; the reason is stated |
| `owner_action` | Needs an owner decision, signature, purchase or external audit |

## Existing evidence these documents build on

| Area | Evidence |
|---|---|
| Threat model, security review, launch gate | `../security-threat-model.md`; `../security-launch-review.md`; `../security-launch-gate.json` |
| DAST | `../security/` (ZAP, staging, 2026-10-06; LEDGER L17-01) |
| Incident response, pentest scope | `../security-incident-response.md`; `../security-pentest-brief.md` |
| Privacy | `../privacy-data-map.md`; `../privacy-request-runbook.md`; `../privacy-counsel-checklist.md`; `../data-retention-policy.md`; `../subprocessor-inventory.md` |
| Recovery | `../dr-drill-report.md`; `../disaster-recovery-state-map.md` |
| Supply chain, credentials | `../supply-chain-policy.md`; `../credential-rotation.md` |
| Platform audit | `../neoh-full-platform-source-audit-2026-10-08.md` (A01–A10; status in the LEDGER audit cross-reference) |
| Engineering rules and status | `../claude-prompts/CONTRACT.md`; `../claude-prompts/LEDGER.md` |

## Approval

When the owner approves a document:
- they fill in its approval block;
- they change its status line to `APPROVED vX.Y, <date>`;
- they commit the change.

The commit is the approval record ([04 §7.5](iso27001/04-objectives-and-support.md#75-documented-information)).
