# ISO/IEC 27001:2022 certification roadmap

| | |
|---|---|
| Status | **DRAFT, needs owner approval** |
| Version | 0.1 |
| Date | 2026-10-14 |
| Document owner | Owner |
| Evidence basis | repository at `e3ea107` |

**Where Neoh stands:** Neoh is **not** certified and is **not** "ISO 27001 compliant" (CONTRACT C12). Code and documents alone cannot produce certification. Only an **accredited certification body (CB)** can issue a certificate, after a Stage 1 and a Stage 2 audit of an ISMS that has been **operating** and producing records, including an internal audit and a management review. This roadmap is the fastest honest route there.

**Every duration and cost below is an estimate.** None comes from a CB quote. Replace them with quotes.

## Hard prerequisite: a production environment in scope

Stage 2 tests whether the ISMS controls are implemented and effective **on the service in scope**. Today only staging exists. `doctl apps list` (LEDGER L19-02, 2026-10-07) found no production app, and audit finding A04 is open.

Until production exists and runs under the ISMS for a records period, there is no live service to sample: no production access list, no production backup or drill, no production DAST, no production change records. Creating production needs owner approval and budget (L19-02). CONTRACT C4.3 prevents an agent from doing it unasked. **Step 0 is therefore an owner decision.**

## Steps

| # | Step | What it contains | Who | Estimate | Can it be compressed? |
|---|---|---|---|---|---|
| 0 | Owner decisions | Approve:<br>• the scope ([01](iso27001/01-scope-and-context.md))<br>• the policy ([02](iso27001/02-leadership-and-policy.md))<br>• the risk method and acceptance criteria ([03](iso27001/03-risk-methodology-and-register.md))<br>• the objectives ([04](iso27001/04-objectives-and-support.md))<br>• the budget<br>• **the creation of production** (L19-02) | Owner | 1–2 weeks | Yes: this is decision time only |
| 1 | Production go-live with the staging controls carried over | Production app + DB:<br>• trusted sources (as L17-02)<br>• environment protection<br>• secrets<br>• alert mailbox<br>• DAST (as L17-01)<br>• no recovery mode (so the scheduled controls run, R10) | Owner + engineering | 2–6 weeks (estimate) | Partly. The CI `promote` job exists (`.github/workflows/ci.yml:607`). The provider accounts (Stripe live, 10DLC, SMTP DPA) carry external lead times |
| 2 | Gap closure: the Criticals first | Treat:<br>• R01: plan with dates is acceptable for Stage 2 if the risk is formally treated<br>• R04 escrow<br>• R07 backups<br>• R10<br>• R18 workstation<br>• R20 DPAs<br>Also: the topic policies marked `gap` in 02; supplier register; credential register; asset inventory | Owner + engineering | 4–8 weeks, overlapping step 3 | Yes. ISO requires risks to be **assessed and treated per a plan**, not reduced to zero. Lower risks can stay in the treatment plan with dates and owner acceptance |
| 3 | Operate the ISMS: records period | Run the calendar in [05](iso27001/05-operation-evaluation-improvement.md) §8.1 and keep the records:<br>• monthly drill<br>• quarterly access review<br>• metric log<br>• incident log<br>• supplier reviews<br>• risk register refresh | Owner | ~3 months of records is commonly expected (estimate; the standard sets no fixed period; CB-dependent) | **Hardly.** The CB samples evidence of operation; one month of records rarely shows that the quarterly controls ran. Ask the chosen CB what minimum it accepts |
| 4 | Internal audit | All of clauses 4–10 + every applicable SoA control, by an **independent** auditor ([05](iso27001/05-operation-evaluation-improvement.md) §9.2) | External auditor (owner engages one) | 2–4 days of audit + 2–4 weeks to close findings (estimate) | No. It must be done and its findings handled before Stage 2. The owner may not audit their own work |
| 5 | Management review | Inputs 9.3.2 a–g, outputs 9.3.3, signed ([05](iso27001/05-operation-evaluation-improvement.md) §9.3) | Owner | 1 day | No. It must follow the internal audit and come before Stage 2 (many CBs expect it before Stage 1) |
| 6 | Choose a CB (start **in parallel with step 1**) | Accredited by an IAF MLA signatory accreditation body, for example **UKAS** (UK), **ANAB** (US) or **IAS** (US), for ISO/IEC 27001:2022. Check the accreditation scope and the CB on IAF CertSearch. Get ≥ 3 quotes. Avoid non-accredited "certificates" | Owner | quotes 2–4 weeks; booking lead time several weeks (estimate) | Yes: start early so the audit dates are ready when the records are |
| 7 | Stage 1 | CB reviews:<br>• documentation (scope, policy, risk method + assessment + treatment plan, SoA, objectives, competence evidence, monitoring results, audit programme and results, management review, CAPA)<br>• readiness | CB | ~1 day for a micro-company, often remote (estimate) | Partly. Stage 1 can happen while the records accumulate if the CB agrees |
| 8 | Close Stage 1 findings | Fix what Stage 1 flagged | Owner | 2–8 weeks gap before Stage 2 (estimate; CB-dependent) | Partly: shorter if Stage 1 finds little |
| 9 | Stage 2 | CB samples records, interviews the owner, tests controls on the in-scope service (production, CI, workstation) | CB | 1–2 days for one person (estimate; audit time is set by the CB under ISO/IEC 27006-1) | No |
| 10 | Certification decision | Major nonconformities must be closed (and minor ones covered by an accepted plan) before the CB issues the certificate | CB + owner | 2–8 weeks (estimate) | No |
| 11 | Surveillance and recertification | Annual surveillance audits in years 1 and 2; a recertification audit before the 3-year certificate expires. The ISMS calendar keeps running | CB + owner | yearly | No |

## Realistic timeline for a one-person SaaS (estimate)

| Path | From today to certificate | Assumes |
|---|---|---|
| Aggressive | ~5–6 months | Production approved within 2 weeks and live within ~6 weeks; CB booked in month 1; ~3-month records period accepted; few audit findings |
| Expected | ~7–9 months | Production plus provider lead times (Stripe live, 10DLC, SMTP DPA) take 2–3 months; normal CB lead times |
| Slow | 12+ months | Production deferred; or major nonconformities at Stage 2 |

The clock that cannot be bought down is **step 1 (production exists) + step 3 (records period) + steps 4–5 (independent audit, then management review) + the CB's own scheduling and decision time.**

## What can and cannot be compressed

| Can be compressed | Cannot be compressed |
|---|---|
| Drafting: this document set exists as drafts | The existence of the in-scope production service |
| CB selection and booking, done in parallel | A records period that shows the periodic controls actually ran |
| Low and medium risk treatments: kept in the treatment plan with dates | The independent internal audit, and acting on its findings |
| Evidence collection: CI already produces it; export it monthly | The management review record |
| A tight scope: one service, one environment set, one person | The CB's Stage 1 → Stage 2 → decision sequence |
| Optional: a compliance-automation platform (below) | Closing major nonconformities before the certificate |

## Option: compliance-automation platforms (owner decision)

Platforms such as **Vanta**, **Drata** and **Secureframe** typically offer:
- policy templates;
- continuous evidence collection from connected systems (for example GitHub);
- task tracking;
- access to auditor or CB partners.

They do **not** certify. The accredited CB still runs Stage 1 and Stage 2.

| Question to ask before buying | Why |
|---|---|
| Does it integrate with **DigitalOcean** App Platform, Managed DBs and Spaces? | Neoh's hosting is DO-only. Coverage varies; verify. Do not assume |
| Can it ingest the GitHub Actions evidence already produced (CI gates, SBOMs)? | Avoids duplicate tooling |
| Price | **Obtain quotes. Any figure is an estimate.** Compare against the cost of running the calendar in [05](iso27001/05-operation-evaluation-improvement.md) by hand |
| Does it impose its own policy set that conflicts with CONTRACT.md? | One source of truth (LEDGER) must stay |

With one person and an existing evidence-producing CI, a manual ISMS run from this repository is feasible. A platform mainly saves the owner's time on evidence collection and reminders.

## Owner and external actions in priority order

| # | Action | Why | Cost / lead time |
|---|---|---|---|
| 1 | Decide on and approve production creation (L19-02) | Hard prerequisite for Stage 2 | Hosting cost not estimated in the repo; staging is ≈ $109/mo (`docs/launch-state.md:24`) |
| 2 | Approve the ISMS drafts (01–05), the SoA and the risk acceptance criteria | Mandatory documented information | Owner time |
| 3 | Escrow `ORACLE_ENCRYPTION_MASTER_KEY` and break-glass access; name a trusted successor (R04) | Critical risk; key-person risk | Low cost; days |
| 4 | Collect DPAs (DO, Stripe, Fireworks, ElevenLabs, Plivo, Telnyx, Twilio, RunPod); replace consumer Gmail SMTP; fix the DashScope region; opt out of ElevenLabs training (R20) | Critical supplier risk; privacy law | Mostly free (standard DPAs); a transactional SMTP or Workspace subscription (estimate: low monthly cost); days to weeks |
| 5 | Rotate the DO API token; scope one token per GitHub environment; TOTP or hardware-key 2FA on GitHub, DO, Stripe and the operator mailbox (R06, R17) | High risk | Free to low cost; hours |
| 6 | Set GitHub environment protection (production: `main` only + wait timer) and screenshot it (R15) | High risk; CI-1 unverifiable from the repo | Free; hours |
| 7 | Set the Stripe test-mode key, price and webhook secret on staging (L18-04) | High risk; billing never exercised | Free (test mode); hours |
| 8 | Engage counsel on retention periods, breach notification and the customer DPA template (R26) | High risk; legal | Counsel fees (estimate); weeks |
| 9 | Request CB quotes (UKAS, ANAB or IAS accredited) | CB booking lead time | Estimate: Stage 1 + 2 for a micro-company in the low thousands to ~10k USD; obtain ≥ 3 quotes |
| 10 | Engage an independent internal auditor | 9.2 objectivity | Estimate: a few days of contractor time; obtain quotes |
| 11 | External pentest of production before go-live | Compensates for no segregation; customer assurance | Estimate: quote-based; brief exists (`docs/security-pentest-brief.md`) |
| 12 | 10DLC brand and campaign registration (L10-04) | Medium risk; carrier rules | Paid, multi-day approval (LEDGER L10-04) |
| 13 | Confirm legacy AWS and Azure accounts hold no Neoh data or live credentials (R31) | Scope boundary | Owner time |

## Related

- ISMS core: [`iso27001/`](iso27001/).
- Control assessment: [`iso27001/annex-a-assessment.md`](iso27001/annex-a-assessment.md).
- SoA: [`iso27001/statement-of-applicability.md`](iso27001/statement-of-applicability.md).
- SOC 2, GDPR/CCPA, HIPAA and PCI DSS mapping: [`framework-crosswalk.md`](framework-crosswalk.md). SOC 2 is a separate AICPA attestation issued by a licensed CPA firm, not a CB.
