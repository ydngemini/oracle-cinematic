# Zylo SaaS compliance checklist: where Neoh stands

> **DRAFT, needs owner approval.** v0.1, 2026-10-14.
>
> Source: the owner's link, zylo.com/blog/saas-compliance-checklist (the 10-item list, paraphrased).
>
> The article is written mostly for a company **buying** SaaS. Items 1–9 also apply to Neoh as a **vendor**; item 10 and the "onboarding a new SaaS app" list apply to the tools Neoh buys (its sub-processors).
>
> No item here claims compliance or certification (CONTRACT C12). The status vocabulary is the one in [README](README.md); the detail lives in [framework-crosswalk.md](framework-crosswalk.md) and [iso27001/](iso27001/).

| # | Checklist item (paraphrased) | Status | Evidence | What's left |
|---|---|---|---|---|
| 1 | **Map which regulations apply.** | `partial` | [framework-crosswalk.md](framework-crosswalk.md) §1. GDPR is conditional, CCPA and state laws are conditional, PCI is SAQ A, HIPAA does not apply. | Counsel must confirm Neoh's role for the public-records leads data and the state-law thresholds. See the "Frameworks named in the article" table below. |
| 2 | **MFA and SSO; least-privilege roles, reviewed regularly.** | `partial` | Operator TOTP: `backend/totp.py`, migration 0121. Session revoke-all: migration 0117. Record-level least privilege for agents: own + pool + shared, migrations 0131–0145 (LEDGER L03-02…L03-08). Database: FORCE RLS on every tenant table (the SQL proofs in `backend/tests/*.sql`). | **Gap:** MFA for broker owners and agents (AUTH-12). **Gap:** SSO (SAML/OIDC) for brokerages. **Owner:** MFA on GitHub, DigitalOcean, Stripe and the registrar. The quarterly access-review script is in progress (L17-xx). |
| 3 | **Encrypt in transit (TLS 1.2 or higher) and at rest (AES-256).** | `partial` | Verified on staging 2026-10-14 with `openssl s_client`: TLS 1.0 and 1.1 refused; TLS 1.2 (ECDHE-ECDSA-AES128-GCM) and TLS 1.3 (TLS_AES_256_GCM_SHA384) accepted; HSTS `max-age=31536000; includeSubDomains`. Field encryption with a per-tenant key: call notes and client notes (0132, L17-03). | **At rest:** `inherited`. DigitalOcean managed PostgreSQL and Spaces encryption; the owner should file DO's attestation. **Gap (A01):** client name, email and phone, `leads.payload` and `sms_messages.body` are not field-encrypted. |
| 4 | **Log authentication, privileged actions, configuration changes and data access/export; retain and monitor the logs.** | `partial` | Hash-chained `audit_ledger` (`backend/audit_ledger.py`); a log-PII guard (L17-04). | **Gap:** log forwarding with defined retention (App Platform logs are short-lived). **In progress:** alerts on privileged sign-in, role changes and MFA reset. **Gap:** an audit event on bulk export. |
| 5 | **Document incident response with notification timelines; test it every year.** | `partial` | `docs/security-incident-response.md`. | **Gap:** no recorded exercise yet; the first tabletop is a record the owner must create (ISMS 05). **Gap:** a breach-notice matrix (GDPR 72 h; US state laws). Counsel. |
| 6 | **Disclose what data you collect, why, for how long, and who receives it.** | `gap` | Platform Use Policy v5 covers users; `docs/privacy-data-map.md` and `docs/subprocessor-inventory.md` exist internally. | **Gap:** no public privacy policy or notice at collection exists. Counsel writes the text; wiring the page is S. |
| 7 | **Handle access, correction and deletion requests within legal deadlines.** | `partial` | `backend/privacy_requests.py`; `docs/privacy-request-runbook.md`; privacy lifecycle proof `backend/tests/privacy_lifecycle.sql`. | **Gap:** discovery finds people by email and phone only, not by name or address in leads. **Gap:** deletes at providers (Twilio, Plivo, ElevenLabs) are not wired. |
| 8 | **DPAs or BAAs with every vendor that handles personal data.** | `owner_action` | Sub-processor list: `docs/subprocessor-inventory.md`. | Collect and file DPAs (DO, Stripe, Twilio/Plivo/Telnyx, ElevenLabs with the training opt-out, Fireworks, RunPod). Replace consumer Gmail SMTP. BAAs are `not_applicable` (no PHI, crosswalk §1). |
| 9 | **Take payments through a PCI-compliant processor.** | `implemented` | Stripe-hosted Checkout and Billing Portal (`oracle-app/src/components/BillingOverlay.jsx`, `backend/billing.py`); no card field anywhere; signed webhooks. | **Owner:** the annual SAQ A self-assessment. **In progress:** keep card numbers out of transcripts, notes and texts (L17-07). |
| 10 | **Restrict, log and review (quarterly) access to financial systems.** | `owner_action` | Stripe is the only financial system. | **Owner:** Stripe dashboard MFA, a named user list and a quarterly review record. The access-review script in progress covers the database and app roles. |

## Frameworks named in the article
| Framework | Applies to Neoh? | Why |
|---|---|---|
| SOC 2, ISO 27001 | Voluntary: what customers ask for | Roadmap: [certification-roadmap.md](certification-roadmap.md). |
| GDPR, CCPA/CPRA, PCI DSS, HIPAA | See the crosswalk §1 | Same as item 1 above. |
| PIPEDA (Canada), LGPD (Brazil) | `not_applicable` today | No Canadian or Brazilian brokerages, no CAD/BRL billing, US-only hosting (`infra/digitalocean/app.yaml`). This changes with the first customer there. |
| SOX | `not_applicable` | Neoh is not a public company. A public-company customer may still ask for a SOC 1 later. |
| FedRAMP | `not_applicable` | No US federal agency customers. |

## The article's vendor-onboarding list, applied to Neoh's own sub-processors
For each provider in `docs/subprocessor-inventory.md`, the owner records:
- **Certifications:** SOC 2 / ISO 27001 (report or certificate on file).
- **Privacy terms:** reviewed.
- **DPA:** signed.
- **Account security:** SSO/MFA enabled on the account.
- **Owner:** a named owner (today, the owner).
- **Data tier:** from `privacy-data-map.md`.

None of these records exist yet; ISO A.5.19–A.5.22 tracks them.

The article recommends reviewing controls every year, every quarter for fast-growing environments, and after any incident or regulatory change. That cadence is the ISMS operating calendar in [iso27001/05-operation-evaluation-improvement.md](iso27001/05-operation-evaluation-improvement.md).
