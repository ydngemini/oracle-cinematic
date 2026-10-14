# Framework crosswalk: ISO 27001, SOC 2, GDPR, US state privacy, PCI DSS, HIPAA

> **DRAFT, needs owner approval.** Version 0.1, 2026-10-14. Base commit `e3ea107`.
> This page is a planning map, not a compliance claim. Nothing here says that Neoh is
> "ISO 27001 compliant", "SOC 2 compliant", "GDPR compliant", "PCI compliant" or
> "HIPAA compliant". The banned-claims list in CONTRACT C12 applies.
> Applicability calls are engineering readings of repo evidence. **Privacy counsel
> confirms each one** (`docs/privacy-counsel-checklist.md`).
> Statutory thresholds and section numbers below are from general knowledge, not
> from the repo. Counsel verifies them at the date of reliance.

**What this page is for.** One control set, built once, mapped to every framework the
owner pasted. The ISO 27001:2022 Annex A control is the anchor, and the ISMS documents
in `docs/compliance/iso27001/` own the control text. Each other framework adds only the
rows in §3.

**Status vocabulary:**
- `implemented`: repo or ledger evidence exists.
- `partial`: some of the control exists.
- `gap`: nothing exists yet.
- `inherited`: a provider supplies it; what we must still do is listed.
- `not_applicable`: the reason is given.
- `owner_action`: needs a decision, a signature, a purchase or an external audit.

**Environment caveat.** Only staging exists; production has never been created
(LEDGER L19-02). Every `implemented` row means "in code, or on staging". None is
production operating evidence.

**One-person company.** The owner is top management, the ISMS owner, the developer and
the only operator. SOC 2 CC1.3 / CC5.1, ISO A.5.3 and PCI 12.x all assume segregation of
duties, which Neoh cannot have. The compensating controls are:
- CI gates that cannot be skipped (`.github/workflows/ci.yml`);
- the production `promote` job: manual dispatch, a typed `deploy` confirmation, `main`
  only, and the environment's required reviewer (`ci.yml:600-616`);
- an append-only, hash-chained audit ledger (`backend/audit_ledger.py:106`);
- operator TOTP (`backend/totp.py:47`, migration `0121`);
- periodic independent review, either an external consultant or the certification audit
  itself (ISO A.5.35).

---

## 1. Applicability

### 1.1 Summary

| Framework | Applies? | Reason (repo evidence) | What would change the answer |
|---|---|---|---|
| **ISO 27001:2022** | voluntary | A certification standard, not a law. Brokerage buyers may ask for it. | — |
| **SOC 2** | voluntary | A market expectation for B2B SaaS that holds customer PII. | — |
| **GDPR / UK GDPR** | **not_applicable today, conditional** | §1.3 | an EU/UK establishment, customer or targeting |
| **CCPA/CPRA** | **conditional** | Neoh is the service provider for brokerage CRM data. Its own leads data may be in scope (§1.4). | revenue above the threshold, or 100k+ CA consumers, or selling/sharing |
| **Other US state privacy laws** | **conditional, likely for some** | DE leads, TX (ACTRIS) customers, MD processor rule (§1.4) | thresholds per state |
| **PCI DSS** | **applies (Neoh is a merchant); expected scope SAQ A** | §1.5 | card data reaching a Neoh page, server or call |
| **HIPAA** | **not_applicable** | §1.6 | serving a covered entity with PHI |
| **TCPA, 10DLC, CAN-SPAM, BIPA, call recording, fair housing** | **applies** | Neoh sends SMS, email and AI voice calls (§1.7) | — |

### 1.2 SOC 2 — scope recommendation

| Trust Services Category | Include? | Why |
|---|---|---|
| Security (CC1–CC9, the Common Criteria) | **required** | mandatory in every SOC 2 |
| **Confidentiality** (C1.1–C1.2) | **include** | Neoh's crown jewels are confidential brokerage CRM data and communication content (`docs/security-threat-model.md` §Crown jewels). The work is mostly done already: the retention schedule (`docs/data-retention-policy.md:31`) and erasure (`backend/privacy_lifecycle.py:708`) already map to C1.2 (disposal). |
| **Availability** (A1.1–A1.3) | **include once production exists** | Brokerages run their day on the CRM. The DR drill (`docs/dr-drill-report.md:18-30`, measured class-A RTO about 3 min) and the runbooks (`docs/runbooks/`) are A1.2/A1.3 evidence. **Not before production:** there is no capacity evidence on production-shaped infrastructure (A10), and no uptime history. |
| Processing Integrity (PI1.1–PI1.5) | **defer** | Only the billing/usage metering is a candidate (`backend/billing_usage.py`, A05). It adds audit cost for little buyer value at this stage. |
| Privacy (P1–P8) | **exclude** | Neoh is mostly a processor. The P-series assumes a controller's notice and choice. State-law obligations are handled in §3. Revisit if Neoh's own leads data (§1.4) becomes a product. |

**Type I vs Type II:**
- **Type I** tests that the controls are designed and in place at one date. It can be
  issued as soon as the policies are approved and production exists.
- **Type II** tests that the controls operated over an observation period. Three months
  is a common minimum, and 6–12 months is typical for a first Type II (an estimate).
  The period cannot start before production and the ISMS records exist.
- Recommended path: SOC 2 Type I first, then a Type II window that overlaps the ISO
  Stage 2 audit, so one set of records serves both.

**Only a licensed CPA firm issues a SOC 2 report** (AICPA attestation standards,
SSAE 18 / AT-C 205). Compliance-automation platforms collect evidence. They do not issue
reports.

### 1.3 GDPR / UK GDPR

| Test (GDPR Art. 3) | Evidence | Result |
|---|---|---|
| Establishment in the EU/UK (Art. 3(1)) | one-person US company; hosting is DO `region: nyc` (`infra/digitalocean/app.yaml:68`), Spaces `nyc3` (`:196`) | no |
| Offering goods or services to people in the EU (Art. 3(2)(a)) | US brokerages only. No EUR, no EU locale and no "GDPR" string in `backend/` or `oracle-app/src` (grep 2026-10-14). The product is built around US state law (TREC, MLS, 10DLC, TCPA). | no |
| Monitoring behaviour in the EU (Art. 3(2)(b)) | no EU tracking or profiling | no |
| An EU resident inside a US brokerage's CRM (incidental) | possible, for example an overseas buyer | does not by itself make the brokerage, or Neoh as its processor, subject to GDPR when nothing targets the EU — counsel confirms |

**Conclusion: `not_applicable` today.** It becomes applicable when **any** of these
happens:
- Neoh signs an EU/UK brokerage;
- Neoh markets to the EU (EU language, currency or ads);
- Neoh opens an EU office;
- a customer contractually requires GDPR processor terms.

When it triggers:
- an Art. 27 EU representative (no EU establishment);
- Art. 28 processor terms;
- an Art. 30 record of processing (RoPA);
- Chapter V transfer tools: the DigitalOcean DPA's SCCs and the EU–US Data Privacy
  Framework where a provider is certified;
- the provider regions: ElevenLabs offers EU residency (`ELEVENLABS_API_BASE`,
  `docs/subprocessor-inventory.md:26`), and DashScope processes in Singapore (`:24`).

The §2 mapping is kept anyway: GDPR articles 5, 25, 28, 32 and 33 describe the same
controls US state laws and SOC 2 expect, so building to them costs nothing extra.

### 1.4 CCPA/CPRA and other US state privacy laws — roles per data set

| Data set (store) | Neoh's role | Evidence | Notes |
|---|---|---|---|
| Brokerage CRM: `clients`, `agent_contacts`, notes, messages, calls, transactions | **processor / service provider** (the brokerage is the controller) | `docs/privacy-data-map.md:15-17`; `docs/privacy-request-runbook.md:4-7` | Needs the CCPA service-provider contract terms (11 CCR §7051) and the VA/DE/TX/MD processor terms. **No executed template exists** (`privacy-counsel-checklist.md:14-23`). |
| Neoh accounts, billing, security/audit logs (`users`, `subscriptions`, `audit_ledger`) | **controller** | `privacy-data-map.md:15-17,36` | Agents and owners are B2B contacts. CCPA has covered B2B and employee data in full since 2023-01-01. |
| **Public-records leads** (`leads`: 10.4M rows in dev, `payload` holds owner names **in plaintext**) | **unclear: controller, or a processor for the requesting brokerage. Counsel.** | `privacy-data-map.md:42`. Harvesters write the rows under a platform-admin context with Neoh's own `motivation_score` (`backend/harvesters/base.py:682-701`). | (1) **Raw government records** are "publicly available" and fall outside "personal information" (CCPA §1798.140(v)(2); most state laws have the same exclusion). (2) **`motivation_score` and distress or underwriting inferences are Neoh-made inferences.** They are not public, so they can be personal information or profiling. (3) If Neoh compiles these records and supplies them to brokerages for value, check the **data-broker registration** laws (CA Delete Act, TX, OR, VT). (4) MD MODPA's "strictly necessary" data-minimisation standard is the strictest test for keeping millions of rows. |
| Call audio and AI call notes | processor | `subprocessor-inventory.md:26`; L17-03 | may contain sensitive data (health, finances) in free text (§1.6) |
| Voice biometrics | none are created | `privacy-counsel-checklist.md:79` (item 19) | BIPA gate exists (§1.7) |

**Thresholds** (counsel verifies; figures as generally known, not from the repo):

| Law | Applies to a business that… | Neoh today |
|---|---|---|
| CCPA/CPRA (CA) | has more than about $26.6M revenue (CPI-adjusted), **or** buys, sells or shares the PI of 100k+ CA consumers, **or** gets 50%+ of revenue from selling or sharing PI | below the revenue threshold. CA leads are not known to be harvested. **Verify the `leads` state mix.** |
| VA VCDPA, CO CPA, NJ, others | 100k consumers, or 25k plus a sale-revenue share | depends on the leads per state, and on whether the inferences count |
| **DE DPDPA** | 35k consumers, or 10k plus 20% sale revenue. HB 380 lowers the thresholds from 2027-01-01 (`privacy-counsel-checklist.md:24-27`). | **Real DE leads exist.** The raw records are excluded as public; whether the scores count is the open question. |
| **MD MODPA** (in force 2025-10-01) | 35k consumers, or 10k plus 20% sale revenue. A processor must notify within 10 days (repo assumption, `privacy-request-runbook.md:126`). | the 10-day breach clock is planned |
| **TX TDPSA** | no numeric threshold; SBA small businesses are exempt, **except** from the consent rule for selling sensitive data | probably exempt (`privacy-counsel-checklist.md:24-26`) |
| CA CPPA regulations: risk assessments, cybersecurity audits, ADMT | apply only to businesses inside CCPA. The audit duties are phased in. | AI call notes and lead scoring would be candidates for risk assessment, but only if CCPA applies |

**What the privacy lifecycle code already provides (verified 2026-10-14):**

| Capability | Evidence | Status |
|---|---|---|
| Machine-readable data map; a test fails when a migration adds a table the map does not name | `backend/privacy_data_map.py:61,228-245`; `backend/tests/test_privacy_data_map.py` | `implemented` |
| Subject access (preview, then run) | `backend/privacy_api.py:188`; `backend/privacy_requests.py:167` | `implemented` (processor-side; the brokerage verifies the requester's identity) |
| Subject deletion; refuses on broker records (409) and legal hold (423) | `backend/privacy_requests.py:182,190` | `implemented` |
| Opt-out kept as a keyed hash after erasure | `backend/outreach_compliance.py:341` | `implemented` |
| Brokerage export: JSONL, manifest, SHA-256, expires after 7 days | `backend/privacy_api.py:114,127`; `backend/privacy_export.py` | `implemented` |
| Tenant erasure: 10 checkpointed, idempotent phases, a receipt and a hash | `backend/privacy_lifecycle.py:708,809` | `implemented` (live test: `tests/test_privacy_lifecycle_live.py`) |
| Re-erasure after a restore | `scripts/reapply-erasures.py`; `privacy-request-runbook.md:97-109` | `implemented` |
| Retention schedule plus the code that enforces each row | `docs/data-retention-policy.md:31,68`; `backend/retention_policy.py:127` | `partial`: the periods marked "counsel" are unconfirmed |
| Correction right (CCPA §1798.106) | the CRM edit UI; no request-type workflow | `partial` |
| Discovery of free-text mentions | not found by identifier search, and every result says so | `partial`, by design (`privacy-data-map.md:150`) |
| Leads (`leads`) inside the subject-request flow | the data map classes it as business CRM (`privacy_data_map.py:61`); discovery is by email and phone, and leads carry owner names and addresses | `gap`: a request by name or address will not find lead rows. Counsel decides whether it must (role question above). |

### 1.5 PCI DSS — the expected scope is SAQ A

| Fact | Evidence |
|---|---|
| Card entry happens on **Stripe-hosted Checkout**: the frontend redirects the whole page to Stripe's URL | `oracle-app/src/components/BillingOverlay.jsx:113-119` (`window.location.href = url`) |
| The session is created server-side with no card fields: `mode="subscription"`, and no `ui_mode="embedded"` | `backend/billing.py:270-309` |
| Card management happens on the **Stripe-hosted Billing Portal** | `backend/billing.py:384`; `oracle-app/src/state/useSubscription.js:67` |
| Webhooks are signature-verified | `backend/billing.py:537` |
| There is no Stripe.js, Elements or card field in the frontend | grep `stripe` in `oracle-app/src`, `package.json`, `index.html`: billing displays only |
| The page that redirects has a strict CSP: `script-src 'self'` plus Google Maps only, and HSTS | `oracle-app/nginx.conf:26,28` |

**Conclusion:** Neoh is a card-not-present e-commerce merchant that fully outsources
payment pages, so it is expected to qualify for **SAQ A** (an inference; Stripe's
dashboard or the acquirer confirms the SAQ type). **No QSA is needed for SAQ A.** The
self-assessment is signed by the owner.

SAQ A still requires the following (v4.0.1 numbering; confirm against the SAQ version
current at attestation):

| Requirement | What we must do | Status |
|---|---|---|
| Eligibility: the site is "not susceptible to attacks from scripts" that could affect payment. This replaced 6.4.3 and 11.6.1 in SAQ A in 2025. | keep the CSP strict; no third-party scripts on the billing route; SRI or self-hosting for any script added later | `partial`: the CSP exists (`nginx.conf:28`). Google Maps scripts load site-wide, including near billing. Document the justification, or exclude them from the billing view. |
| 2.2.2 vendor default accounts | no default credentials on the components that serve the site | `inherited` (DO App Platform) plus our app config |
| 3.1 / 3.2.1 no stored card data | Neoh stores only Stripe ids | `implemented` (`billing.py`, plus the BrokerageDiagnostics customer ref) |
| 6.3.3 patch critical vulnerabilities | pip-audit, Trivy CRITICAL blocking | `implemented` (`ci.yml:84`, `:436-438`) |
| 8.x authentication for accounts that can change the site | MFA on GitHub, DigitalOcean and Stripe dashboard accounts | `owner_action`: confirm MFA is on all three (no repo evidence) |
| 9.4 media with card data | none exists | `not_applicable` (no paper or media with card data) |
| 11.3.2 external ASV scans (where SAQ A requires them for the redirecting site) | a quarterly ASV scan of the public site | `owner_action` / confirm applicability. The OWASP ZAP scan (L17-01) is **not** an ASV scan. |
| 12.8 manage the third-party service provider | keep Stripe's PCI Attestation of Compliance on file; record responsibilities | `owner_action` |
| 12.10.1 incident response plan | `docs/security-incident-response.md` | `partial`: no card-compromise playbook (Stripe key leak only, `:66`) |
| Annual SAQ A plus an Attestation of Compliance | complete it, usually in the Stripe dashboard | `owner_action` |

**What would break SAQ A:**
- Stripe Elements, a card field on a Neoh page, or embedded Checkout. Some of these are
  still SAQ A, but the script-integrity duty becomes a real one.
- Any API path that receives PAN, which means SAQ D.
- **An AI voice agent, SMS thread or email that takes card numbers.** Call audio, AI
  transcripts (`inbound_voice_calls`), `client_notes` and `sms_messages` would become
  cardholder-data stores, and a call recording containing a PAN is in scope.
  *Recommendation:* tell the AI persona never to ask for card details, and add a PAN
  detector that redacts card numbers in transcripts and notes (§3, technical gap T-PCI-1).
- Taking cards by phone through an operator.

### 1.6 HIPAA

| Test | Evidence | Result |
|---|---|---|
| Covered entity (provider, health plan, clearinghouse)? | real-estate brokerage SaaS | no |
| Business associate (handles PHI for a covered entity)? | no healthcare customers; no integration with an EHR, payer or provider | no |
| Health fields in the schema | `platform_policy.py:203-217` **prohibits** `disability`, `medical_condition`, `pregnancy` and `biometric` as scoring or targeting fields | no health fields; they are blocked on purpose |
| Health facts inside free text | AI call notes, transcripts and notes may mention them, for example "needs a wheelchair-accessible home" or a relocation for medical care | possible, but **not PHI**: the information does not come from a covered entity |

**Conclusion: `not_applicable`.** Free-text health mentions still matter in three ways:
1. They are **sensitive data** under the CPRA ("health"), VA, CO, TX and MD laws. MD
   bans the sale of sensitive data.
2. **Washington's My Health My Data Act** defines "consumer health data" broadly and has
   no revenue threshold. Counsel checks whether it applies if Neoh serves WA brokerages.
3. **Fair housing:** disability and familial status are protected classes. They must
   never drive targeting or steering. This is already enforced in `platform_policy.py`
   and `neoh_persona.py:93-94`.

**What would change the answer:**
- selling to senior-living placement, hospital relocation or health-plan relocation
  programmes that pass medical information;
- an integration with a covered entity;
- storing accommodation records received from a healthcare provider.

A BAA and the HIPAA Security Rule would follow from any of these.

### 1.7 Sector rules the repo already touches

| Rule | Code and docs | Status | Open point |
|---|---|---|---|
| TCPA / FCC 24-17: AI voice is "artificial"; prior express written consent; calling hours | `backend/outreach_compliance.py:2-33,100-109,211,255`; gate called from `commands_api.py:2718,2851`, `crm.py:1415`, `speed_to_lead.py`, `telephony_api.py` | `implemented` (gate) | evidence of the consent-capture wording: counsel |
| Mini-TCPA (FL FTSA, OK OTSA): 8am–8pm, frequency caps | `outreach_compliance.py:79-101,308-323` | `implemented` | — |
| TCPA revocation by any reasonable method; internal DNC kept 5 years | STOP keywords `outreach_compliance.py:119-121,203`; opt-out hash `:341`; retention `privacy-counsel-checklist.md:38-41` | `implemented` | whether a hash is enough evidence (counsel item 6) |
| A2P 10DLC: brand and campaign before sending | `outreach_compliance.py:33`; `backend/messaging_provider.py`, `provider_purchases.py`; A07 fix | `partial`: no approved brand or campaign on staging (memory: agent connections status) | `owner_action`: brand and campaign registration |
| CAN-SPAM: suppression, opt-out, sender address | email is suppression-checked (`commands_api.py:2718`, `crm.py:1415`) | `partial`: **no `List-Unsubscribe` header, unsubscribe link or postal-address footer found** in `backend/email_outbox.py` (grep 2026-10-14) | technical gap T-CS-1 |
| BIPA (IL) / CUBI (TX) voiceprints | `outreach_compliance.py:31,90-95,111-117,314-319` (hard gate in IL) | `implemented` (gate); no voiceprints are created | counsel item 19 |
| Call recording / all-party consent (CA, DE, FL, IL, MD, MA, MT, NH, PA, WA and others) | `AI_VOICE_DISCLOSURE` "…on a recorded line…" spoken first (`outreach_compliance.py:105-109`; `commands_api.py:3177`; `acs_call_handler.py:412`; Plivo `disclosed` stage `plivo_call_handler.py:173`) | `implemented` for AI calls | the repo treats only DE, PA, MD and CA as all-party states (`privacy-counsel-checklist.md:69-71`). Counsel confirms the full list and the ElevenLabs Agents script. |
| AI call notes consent | owner decision D-2026-10-12-03; `backend/call_note_settings.py:1-5` (review by default); Platform Use Policy v2 §4 (L17-05) | `implemented` | DPIA-style risk assessment (§3) |
| Fair housing | `platform_policy.py:200-232` (prohibited fields); `check_fair_housing` tool (`ai_chat_agent.py:408`); `neoh_persona.py:93-94`; policy §3 (`policy_contract.py:38`) | `implemented` (text and field gates) | no disparate-impact test on lead scoring (A08 heuristics) |
| FTC Safeguards Rule (if Neoh is a "financial institution") | `privacy-counsel-checklist.md:28-30` (item 4) | `owner_action` (counsel) | the ISMS would satisfy most of a written information security programme |

---

## 2. Crosswalk — one control set

Columns: **ISO** = ISO/IEC 27001:2022 Annex A (and clauses); **SOC 2** = 2017 TSC (rev.
2022) ids; **GDPR** = articles; **CCPA** = Cal. Civ. Code § / 11 CCR §; **PCI** = SAQ A
(v4.0.1) requirement, "—" where SAQ A asks nothing. Status and evidence are for the
**engineering control**; ISMS records (the evidence that it *operates*) are listed in
§4.

| # | Requirement area | ISO 27001:2022 | SOC 2 | GDPR | CCPA/CPRA | PCI SAQ A | Status | Evidence (file:line) |
|---|---|---|---|---|---|---|---|---|
| 1 | **Policies** (ISMS policy set, acceptable use) | 5.1, 5.10, 5.37; cl. 5.2 | CC1.1, CC2.2, CC5.3 | 24(2) | §1798.100(e) ("reasonable security") | 12.10.1 (IR plan) | `partial`: customer-facing Platform Use Policy only; ISMS policies are drafts in `docs/compliance/iso27001/` | `backend/policy_contract.py:22-82`; L17-05 |
| 2 | **Risk assessment and treatment** | cl. 6.1.2, 6.1.3, 8.2, 8.3; 5.7 | CC3.1–CC3.4, CC9.1 | 24, 32(1), 35 | 11 CCR risk-assessment regs (if CCPA applies) | — | `partial`: threat model and audit backlog exist; no risk register with owner, likelihood/impact, treatment and acceptance | `docs/security-threat-model.md:21-100`; `docs/neoh-full-platform-source-audit-2026-10-08.md` (A01–A10) |
| 3 | **Asset and data inventory, classification** | 5.9, 5.12, 5.13 | CC6.1, C1.1 | 30 | §1798.100(a) (categories) | — | `implemented` (data); `gap` (hardware/endpoint asset list) | `backend/privacy_data_map.py:61,228`; `docs/privacy-data-map.md:19-60`; `docs/subprocessor-inventory.md:13-30` |
| 4 | **Access control and tenant isolation** | 5.15, 5.18, 8.3, 8.2 | CC6.1, CC6.2, CC6.3 | 25, 32(1)(b) | §1798.100(e), §1798.81.5 | 8.x (site admin accounts) | `implemented` (app): FORCE RLS, tenant wall as index condition, per-record scope | `backend/db/migrations/0018_harvest_idempotency_and_force_rls.sql`, `0017_rls_policy_canonicalize.sql`, `0120_platform_admin_is_a_role.sql`, `0131`/`0134` (D-2026-10-13-01), `0141` (L16-01); `backend/tests/rls_tenant_wall.sql`, `test_rls_enforcement_guard.py` |
| 5 | **Authentication, sessions, privileged access** | 5.16, 5.17, 8.5, 8.2, 8.18 | CC6.1, CC6.2 | 32 | §1798.81.5 | 8.2.x–8.3.x | `implemented` (operator TOTP, session epoch revoke-all); `owner_action` (MFA on GitHub/DO/Stripe consoles: no evidence) | `backend/totp.py:47`; migration `0121_operator_second_factor.sql:2-8`; `backend/auth.py:348-368`, migration `0117_session_epoch.sql`; L17-01 (logout revokes all) |
| 6 | **Encryption (transit, rest, field)** | 8.24, 8.20, 8.21 | CC6.1, CC6.7, C1.1 | 32(1)(a), 34(3)(a) | §1798.150 (encryption defence), §1798.82 | 4.x n/a (no CHD); TLS on redirect page | `partial`: TLS + HSTS, DB TLS, per-tenant HKDF keys for contacts and notes; **`clients` contact fields, `leads.payload`, `sms_messages.body` plaintext (A01)** | `oracle-app/nginx.conf:26`; `backend/db/connection.py:395,460`; `backend/crypto.py:8,99`; `backend/client_note_crypto.py`, migration `0132` (L17-03); gap: `docs/privacy-data-map.md:142` |
| 7 | **Logging, monitoring, alerting** | 8.15, 8.16, 8.17 | CC4.1, CC7.1, CC7.2 | 32(1)(d), 33(5) | §1798.81.5 | 10.x n/a for SAQ A | `partial`: hash-chained audit ledger, log-PII guard, ops alert email; SQLite fallback never purged; no SIEM, no alert-on-audit-anomaly, log retention at DO short | `backend/audit_ledger.py:47,106,178`; `backend/audit_middleware.py`; `backend/tests/test_log_pii_guard.py` (L17-04); `infra/digitalocean/app.yaml:222-227` (`ORACLE_ALERT_EMAIL`, `backend/ops_alerts.py`); `docs/privacy-data-map.md:146` |
| 8 | **Vulnerability management, secure development, testing** | 8.8, 8.25, 8.26, 8.28, 8.29, 8.31, 8.4 | CC7.1, CC8.1 | 25, 32(1)(d) | §1798.100(e) | 6.3.3 | `implemented` (CI): gitleaks, bandit, pip-audit strict, Trivy CRITICAL blocking, SBOM 400 d, RLS suite; staging DAST 0 High. `gap`: no npm audit step (Trivy lockfile scan covers it), no external pentest | `.github/workflows/ci.yml:84,173-192,436-470`; `docs/supply-chain-policy.md`; L17-01, `docs/security/zap-active-staging-2026-10-06.md`; `docs/security-pentest-brief.md` (brief only) |
| 9 | **Change management** | 8.32, 8.9, 8.31 | CC8.1, CC5.2 | 25 | — | 6.3.x | `implemented` (pipeline): every change via CI; staging auto-deploy with PRE_DEPLOY migrations; production promote is manual, typed, main-only, required reviewer; migration ledger + checksums. `partial`: one-person self-approval | `ci.yml:600-616`; L19-01; `backend/run_migrations.py` |
| 10 | **Incident response and breach notification** | 5.24–5.28, 6.8 | CC7.3, CC7.4, CC7.5, CC2.3 | 33, 34 | §1798.82 (breach notice), §1798.150 | 12.10.1 | `partial`: playbooks + containment controls; US clocks drafted; no 72-h GDPR path (n/a today), no card-compromise playbook, no customer notification template, no exercised tabletop | `docs/security-incident-response.md:8-75`; `docs/privacy-request-runbook.md:122-134`; `docs/privacy-counsel-checklist.md:90-99` |
| 11 | **Vendor / sub-processor management** | 5.19, 5.20, 5.21, 5.22, 5.23 | CC9.2, CC3.4 | 28(2),(4), 44–46 | §1798.140(ag), 11 CCR §7051 | 12.8.1–12.8.5 | `partial`: inventory with region/retention/deletion; **no DPA confirmed on file; consumer Gmail SMTP has no DPA; DashScope Singapore; ElevenLabs training opt-out unconfirmed** | `docs/subprocessor-inventory.md:13-39` |
| 12 | **Backup, DR, business continuity** | 8.13, 8.14, 5.29, 5.30, 8.6 | A1.2, A1.3, CC7.5, CC9.1 | 32(1)(c) | — | — | `implemented` (staging drill: class-A RTO ~3 min, 0 rows lost); `inherited` (DO PITR 7 d, multi-AZ is a production choice); `gap`: logical backups unscheduled, full 47 GB restore never measured, no production | `docs/dr-drill-report.md:18-34,86-99`; `docs/disaster-recovery-state-map.md`; `docs/data-retention-policy.md:86-93` |
| 13 | **Privacy rights (access, delete, correct, opt-out)** | 5.34 | (P4–P5 if Privacy TSC) | 12–22 | §1798.100, .105, .106, .110, .115, .120, .121, .130, .135 | — | `implemented` (processor tools: access/delete/export/opt-out); `partial` (correction, leads by name); `gap` (controller notice, "Do Not Sell or Share", GPC) | §1.4 table; `backend/privacy_api.py:188`; `backend/privacy_requests.py:167-190`; `docs/privacy-request-runbook.md:9-55` |
| 14 | **Retention and deletion** | 8.10, 5.33, 7.14 | C1.2, CC6.5 | 5(1)(e), 17, 28(3)(g) | §1798.100(a)(3), §1798.105, 11 CCR §7022 | 3.2.1 | `implemented` (schedule + enforcement + erasure + re-erasure after restore); `partial` (periods "counsel"; provider-side deletes for Twilio/Plivo/ElevenLabs not called) | `docs/data-retention-policy.md:11-102`; `backend/retention_policy.py:29-255`; `backend/privacy_lifecycle.py:708,809`; `docs/subprocessor-inventory.md:38` |
| 15 | **Physical and environmental** | 7.1–7.14 | CC6.4 | 32 | — | 9.x | `inherited` (DO data centres; Stripe); we still own **7.7/7.9/7.10/7.14 + 8.1** for the owner's laptop: disk encryption, screen lock, no PII on local disk | none in repo: `owner_action` (endpoint statement) |
| 16 | **People: screening, awareness training, NDA, offboarding** | 6.1–6.6, cl. 7.2, 7.3 | CC1.4, CC2.2 | 32(4), 39(1)(b) | 11 CCR §7100 (training on rights, if applicable) | 12.6 n/a for SAQ A | `gap`: no training record, no contractor NDA template. Owner-only today; contractor onboarding needs it | — |
| 17 | **Independent review, internal audit, management review** | 5.35, 5.36; cl. 9.1, 9.2, 9.3, 10 | CC4.1, CC4.2, CC5.1 | 24(1), 32(1)(d) | 11 CCR cybersecurity audit (if applicable) | — | `owner_action`: none has happened; must not be back-dated | — |
| 18 | **Legal and contractual requirements register** | 5.31, 5.32, 5.34 | CC2.3, CC3.1 | 5(2), 30 | §1798.185 regs | — | `partial`: counsel checklist + MLS licence purge + outreach statutes in code; no single obligations register | `docs/privacy-counsel-checklist.md`; `backend/outreach_compliance.py:2-33` |

---

## 3. Gaps unique to each framework (beyond ISO 27001)

ISO 27001 does not by itself require the items below. Ids are local to this page
(`X-…`) so other documents can cite them.

### SOC 2

| Id | Gap | Type | Note |
|---|---|---|---|
| X-SOC-1 | **An observation period of operating evidence** (Type II): access reviews, change tickets, alerts handled, backups tested, all dated inside the window | `owner_action` | starts only after production + ISMS records |
| X-SOC-2 | **System description** (DC 200: services, boundaries, sub-processors, CUECs = complementary user-entity controls the brokerage must run, CSOCs = carved-out sub-service organisations: DO, Stripe, Twilio…) | `gap` | `docs/security-threat-model.md` + `subprocessor-inventory.md` are the raw material |
| X-SOC-3 | Sub-service organisations' own SOC 2 reports collected yearly and their CUECs mapped (DO, Stripe, GitHub, Twilio) | `owner_action` | carve-out method expected |
| X-SOC-4 | **Board / oversight** (CC1.2): a one-person company has no independent board; document the owner's oversight and use an external advisor review as the compensating control | `owner_action` | auditor will note it |
| X-SOC-5 | **Quarterly user-access review** with evidence (CC6.2/6.3) for DO, GitHub, Stripe, DB roles, operator accounts | `gap` | script can list them; the review is a record |

### GDPR (only when triggered, §1.3)

| Id | Gap | Type |
|---|---|---|
| X-GDPR-1 | **Art. 30 RoPA** (controller + processor records). `privacy-data-map.md` is ~70% of it; missing purposes, lawful bases, transfer column | `gap` (cheap to derive) |
| X-GDPR-2 | **Art. 33 72-hour supervisory-authority notice** and Art. 33(2) "without undue delay" processor→controller notice; current plan is 10 days (MD) | `gap` |
| X-GDPR-3 | **Art. 35 DPIA for AI call notes** (live call audio → LLM → notes about third parties; new technology, possibly large scale) and for lead **motivation scoring** (profiling) | `gap`; useful now as the CPPA/CO risk-assessment equivalent |
| X-GDPR-4 | Art. 27 EU representative; Art. 37 DPO assessment (likely not required) | `owner_action` |
| X-GDPR-5 | Chapter V transfer assessment (US, Singapore DashScope) + SCCs/DPF per sub-processor | `owner_action` |
| X-GDPR-6 | Art. 28(3) processor terms in the brokerage agreement | `owner_action` (same template as the CCPA §7051 terms) |

### CCPA/CPRA and state laws

| Id | Gap | Type |
|---|---|---|
| X-US-1 | **Service-provider / processor contract** with §7051's required terms + VA/DE/TX/MD processor terms in the brokerage agreement | `owner_action` (`privacy-counsel-checklist.md:14-23`) |
| X-US-2 | **Privacy policy / notice at collection** (§1798.100(b), 11 CCR §7012) for Neoh-as-controller data (agents, owners, website visitors, and leads if controller). **No privacy-policy page or route found in `oracle-app/src`** (grep 2026-10-14) | `gap` |
| X-US-3 | **"Do Not Sell or Share" + GPC** (§1798.120, .135; 11 CCR §7025) — needed only if Neoh sells/shares (leads to brokerages could be a "sale" if Neoh is controller) | `owner_action` (counsel decides role first) |
| X-US-4 | **Leads data role decision** + data-broker registration check (CA Delete Act/DROP, TX, OR, VT) | `owner_action` |
| X-US-5 | Lead lookup by name/address inside subject requests if Neoh is controller of `leads` | `gap` (code, after X-US-4) |
| X-US-6 | **Encrypt `clients` contact fields, `leads.payload`, `sms_messages.body`** — CCPA §1798.150 private right of action applies to *non-encrypted* PI breached through a security failure; encryption is the strongest defence | `gap` (A01; already the P0) |
| X-US-7 | Sensitive-data handling for health/financial facts in free text (CPRA §1798.121 "limit use", MD sale ban, WA MHMDA) | `gap` (policy: purpose limitation statement; optional detection) |
| X-US-8 | Breach-notice templates per state (DE, TX AG 30 days for 250+, NJ State Police first, MD 10-day processor) | `owner_action` (counsel) |

### PCI DSS (SAQ A)

| Id | Gap | Type |
|---|---|---|
| X-PCI-1 | Annual SAQ A + AOC; keep Stripe's AOC on file | `owner_action` |
| X-PCI-2 | ASV quarterly external scan if the SAQ version requires it for the redirecting site | `owner_action` (confirm) |
| X-PCI-3 | Script-integrity eligibility statement for the billing route (justify or isolate Google Maps scripts) | `gap` (S) |
| T-PCI-1 | Never let AI voice/SMS collect card numbers; PAN pattern redaction (Luhn-checked) on transcripts, notes and inbound SMS before storage | `gap` (code, M) |

### Sector rules

| Id | Gap | Type |
|---|---|---|
| T-CS-1 | CAN-SPAM: `List-Unsubscribe` (+ one-click, RFC 8058) header, visible unsubscribe link and the sender's postal address in commercial emails; unsubscribe writes `outreach_suppression` | `gap` (code, M) |
| X-SEC-1 | Full all-party-consent state list confirmed (repo lists DE, PA, MD, CA) and applied to every call path including ElevenLabs Agents | `owner_action` (counsel) + `partial` (code) |
| X-SEC-2 | 10DLC brand + campaign registration | `owner_action` |

---

## 4. Owner / external actions (priority order)

Costs and lead times are **estimates** (market ranges for a small US SaaS, not quotes)
unless stated otherwise.

| # | Action | Why | Rough cost (estimate) | Lead time (estimate) |
|---|---|---|---|---|
| 1 | **Privacy counsel review**: answer `docs/privacy-counsel-checklist.md` items 1–22 + this page's §1.4 leads role, data-broker check, §1.7 all-party list | Decides which laws bind Neoh directly; everything in §3 US depends on it | $5k–$15k fixed-scope | 2–6 weeks |
| 2 | **Brokerage agreement + DPA template** (CCPA §7051, VA/DE/TX/MD processor terms, GDPR Art. 28-ready) | Required before the first paying brokerage stores client data | included in #1 or $3k–$8k | with #1 |
| 3 | **Sub-processor DPAs**: accept/sign and file DO, Twilio, Stripe, Fireworks, ElevenLabs (+ training opt-out), RunPod, Plivo/Telnyx if enabled; **replace consumer Gmail SMTP** with Workspace (DPA) or a transactional provider; re-region or disable DashScope | No DPA on file is confirmed (`subprocessor-inventory.md:32-39`); these are records the owner must create | mostly $0 (click-through); email provider $0–$20/mo | 1–2 weeks |
| 4 | **Create production** (L19-02) | Every SOC 2 / ISO / PCI control needs a production scope to be evidenced | owner budget decision (staging ~$109/mo per memory; production size TBD) | days |
| 5 | **Enable and evidence MFA** on GitHub, DigitalOcean, Stripe, domain registrar, email | PCI 8.x, SOC 2 CC6.1, ISO 8.5 — no repo evidence | $0 (hardware keys ~$50–$100) | 1 day |
| 6 | **Approve the ISMS documents** (`docs/compliance/iso27001/`) and start the records: risk register, access reviews, change log, incident log, training record | Records are what auditors test; they cannot be back-dated | owner time | start now; ≥3 months of records before Stage 2 / Type II |
| 7 | **PCI SAQ A + AOC** in the Stripe dashboard; confirm SAQ type and whether ASV scans apply | Merchant obligation now that billing is live on staging/production | $0 for SAQ; ASV $100–$500/yr if needed | 1 day |
| 8 | **Cyber insurance** (first-party breach costs + third-party liability, TCPA exclusions to check) | One-person company: incident costs are existential; brokerages may require proof | $1.5k–$5k/yr for low limits | 2–4 weeks |
| 9 | **External penetration test** (scope from `docs/security-pentest-brief.md`) against production | Independent evidence for ISO 8.29 / SOC 2 CC7.1; buyers ask for it | $5k–$20k | 2–6 weeks booking |
| 10 | **10DLC brand + campaign registration** | SMS cannot lawfully deliver at scale without it | carrier fees ~$4–$50 one-off + monthly campaign fee | 1–4 weeks vetting |
| 11 | **Compliance-automation platform** (optional) | Collects evidence for SOC 2 + ISO from one control set | $7k–$20k/yr | 2–4 weeks setup |
| 12 | **ISO 27001 certification body** (accredited, e.g. UKAS/ANAB) — Stage 1 + Stage 2 | Only an accredited body certifies | $8k–$20k for year 1 (small scope) + surveillance audits | Stage 1 after records exist; Stage 2 typically 1–3 months later |
| 13 | **SOC 2 CPA firm** — Type I, then Type II | Only a licensed CPA firm issues the report | Type I $10k–$25k; Type II $15k–$40k | Type I 4–8 weeks after readiness; Type II after a 3–12 month window |
| 14 | **QSA** | **Not needed** for SAQ A (self-assessed) | $0 | — |
| 15 | GDPR EU representative / DPIA sign-off | Only if §1.3 triggers | €1k–€5k/yr representative | when triggered |

---

## 5. Records the owner must create (none exist yet; do not invent them)

Risk register and treatment plan · Statement of Applicability (signed) · ISMS policy
approvals · access-review records (quarterly) · training/awareness record (owner) ·
supplier DPA and SOC/AOC files · incident log (even if empty, dated) · tabletop exercise
record · internal audit report (independent person) · management review minutes ·
SAQ A + AOC · breach-notice templates · brokerage DPA template · RoPA (if GDPR) · DPIA /
risk assessment for AI call notes and lead scoring.
