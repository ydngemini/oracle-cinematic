# Statement of Applicability (ISO/IEC 27001:2022 Annex A)

> **Approved by the owner, 2026-10-14 (v1.0)**. Version 1.0, 2026-10-14. Pinned at `e3ea107`.
> **Approval:** owner ______ (name, signature, date). Not approved until it is signed. Review: yearly, and whenever the scope or a risk changes.
> This SoA records which Annex A controls apply to the Neoh ISMS and why, and how far each has been implemented. **It is not evidence of certification or conformity** (CONTRACT C12). Its status values come from [annex-a-assessment.md](annex-a-assessment.md), which holds the evidence and the gaps behind each row.

## Basis

- **Scope (proposed):** the Neoh multi-tenant real-estate brokerage SaaS. That covers:
  - the source repository;
  - GitHub Actions CI/CD;
  - the DigitalOcean App Platform app with its managed PostgreSQL, Valkey and Spaces;
  - the sub-processors listed in `docs/subprocessor-inventory.md`;
  - the owner's workstation.

  Today only **staging** exists (LEDGER L19-02). The owner must decide either to create production before Stage 1, or to certify a scope that honestly reads "staging / pre-launch". A certification body may not accept the second option.
- **Organisation:** one person. The owner is top management, ISMS owner, developer-reviewer, operator and incident responder. AI coding agents work under `docs/claude-prompts/CONTRACT.md`.
- **Inclusion:** a control is included when the risk assessment, a legal or contractual requirement, or a customer expectation calls for it. The risk register is drafted in the clause 6 work (sibling draft `docs/compliance/iso27001/03-risk-methodology-and-register.md`, separate branch). The justifications below name the main driver; the register must link each risk to its controls.
- **Exclusion:** only A.6.1 and A.6.4 are excluded, because there are no employees. Both come back in when the first person, staff or contractor, is given access to customer data or systems.
- **Inherited:** a control is "inherited" when DigitalOcean, GitHub or Stripe operate it. Neoh still has to obtain and file the provider's attestation each year and do the duties named in the assessment row.

## Summary

| | Count |
|---|---|
| Controls | 93 |
| Applicable | 91 |
| Excluded | 2 (A.6.1, A.6.4) |
| `implemented` | 16 |
| `partial` | 43 |
| `gap` | 5 |
| `inherited` | 14 |
| `owner_action` | 13 |

## A.5 Organisational controls

| Control | Title | Applicable | Justification | Status | Assessment |
|---|---|---|---|---|---|
| A.5.1 | Policies for information security | yes | The ISMS rests on it, and brokerage customers expect a stated security policy. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-1) |
| A.5.2 | Information security roles and responsibilities | yes | Even a one-person company needs named accountability. It also defines what AI agents may do. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-2) |
| A.5.3 | Segregation of duties | yes | Risk: one person can change, approve and deploy. Included to record the exception and its compensating controls. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-3) |
| A.5.4 | Management responsibilities | yes | Top management (the owner) must direct the ISMS. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-4) |
| A.5.5 | Contact with authorities | yes | Breach-notification and telecom-compliance duties require knowing whom to contact. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-5) |
| A.5.6 | Contact with special interest groups | yes | A low-cost source of advisories for a team of one. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-6) |
| A.5.7 | Threat intelligence | yes | An internet-facing SaaS with an AI and telephony attack surface. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-7) |
| A.5.8 | Information security in project management | yes | All change runs as category-scoped projects (CONTRACT). | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-8) |
| A.5.9 | Inventory of information and other associated assets | yes | Customer PII, secrets and many third-party accounts must be known before they can be protected. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-9) |
| A.5.10 | Acceptable use of information and assets | yes | Customers' agents and the AI act on data, so use rules are needed internally and externally. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-10) |
| A.5.11 | Return of assets | yes | Customer-agent offboarding is a product function; contractor offboarding will be needed. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-11) |
| A.5.12 | Classification of information | yes | Several data classes (PII, communications, billing, public records) need different handling. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-12) |
| A.5.13 | Labelling of information | yes | Exports and backups leave the database and must carry their class. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-13) |
| A.5.14 | Information transfer | yes | Email, SMS, webhooks and exports move customer data to and from third parties. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-14) |
| A.5.15 | Access control | yes | Multi-tenant SaaS: tenant and record isolation is the core security property. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-15) |
| A.5.16 | Identity management | yes | Every user and the operator must be uniquely identified for audit and isolation. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-16) |
| A.5.17 | Authentication information | yes | Passwords, OTPs, API keys and the encryption master key protect all customer data. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-17) |
| A.5.18 | Access rights | yes | Brokerages add and remove agents; operator and console access must be reviewed. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-18) |
| A.5.19 | Information security in supplier relationships | yes | About ten sub-processors process customer data. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-19) |
| A.5.20 | Addressing information security within supplier agreements | yes | Sub-processors receive PII and communications, so DPAs are a legal and contractual need. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-20) |
| A.5.21 | Managing information security in the ICT supply chain | yes | Dependencies and base images are part of the shipped product. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-21) |
| A.5.22 | Monitoring, review and change management of supplier services | yes | Provider outages and changes to their terms directly affect the service and compliance. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-22) |
| A.5.23 | Information security for use of cloud services | yes | The whole service runs on DigitalOcean, GitHub and Stripe. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-5-23) |
| A.5.24 | Information security incident management planning and preparation | yes | The service handles PII and communications, so incidents are foreseeable. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-24) |
| A.5.25 | Assessment and decision on information security events | yes | Signals already exist (anomalies, ops alerts) and need a triage rule. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-25) |
| A.5.26 | Response to information security incidents | yes | Containment controls exist and must be applied consistently. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-26) |
| A.5.27 | Learning from information security incidents | yes | Continual improvement is required, and drills already produce lessons. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-27) |
| A.5.28 | Collection of evidence | yes | Breaches and legal disputes need preserved evidence, and platform logs are short-lived. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-28) |
| A.5.29 | Information security during disruption | yes | A provider outage must not cause unsafe outbound actions (calls, texts, charges). | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-29) |
| A.5.30 | ICT readiness for business continuity | yes | Customers depend on the CRM and on communications being available. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-30) |
| A.5.31 | Legal, statutory, regulatory and contractual requirements | yes | TCPA, CAN-SPAM, state privacy and breach laws, 10DLC and customer contracts apply. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-31) |
| A.5.32 | Intellectual property rights | yes | Third-party data licences, MLS history and OSS licences apply. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-32) |
| A.5.33 | Protection of records | yes | Audit, consent and billing records must be retained and tamper-evident. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-33) |
| A.5.34 | Privacy and protection of PII | yes | Core: the product stores consumer PII and communication content. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-34) |
| A.5.35 | Independent review of information security | yes | Needed for credibility; a one-person company cannot review itself independently. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-5-35) |
| A.5.36 | Compliance with policies, rules and standards for information security | yes | Policies only work if conformance is checked. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-5-36) |
| A.5.37 | Documented operating procedures | yes | A sole operator needs written procedures for recovery and for any future backup person. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-5-37) |

## A.6 People controls

| Control | Title | Applicable | Justification | Status | Assessment |
|---|---|---|---|---|---|
| A.6.1 | Screening | no | EXCLUDED for now: no employees or contractors have system access. Re-include at the first hire or contractor access. | `not_applicable` (excluded) | [row](annex-a-assessment.md#a-6-1) |
| A.6.2 | Terms and conditions of employment | yes | The owner and future contractors need written security duties. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-6-2) |
| A.6.3 | Information security awareness, education and training | yes | The owner's competence must be evidenced. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-6-3) |
| A.6.4 | Disciplinary process | no | EXCLUDED for now: no employees. Re-include at the first hire. | `not_applicable` (excluded) | [row](annex-a-assessment.md#a-6-4) |
| A.6.5 | Responsibilities after termination or change of employment | yes | Product offboarding exists; contractor post-engagement duties will be needed. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-6-5) |
| A.6.6 | Confidentiality or non-disclosure agreements | yes | Counsel, pentesters and contractors will see confidential information. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-6-6) |
| A.6.7 | Remote working | yes | All work is done remotely from the owner's workstation. | `gap` (not implemented) | [row](annex-a-assessment.md#a-6-7) |
| A.6.8 | Information security event reporting | yes | Customers and researchers need a way to report security events. | `gap` (not implemented) | [row](annex-a-assessment.md#a-6-8) |

## A.7 Physical controls

| Control | Title | Applicable | Justification | Status | Assessment |
|---|---|---|---|---|---|
| A.7.1 | Physical security perimeters | yes | Applies to the hosting facilities (DigitalOcean); inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-1) |
| A.7.2 | Physical entry | yes | Applies to the hosting facilities; inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-2) |
| A.7.3 | Securing offices, rooms and facilities | yes | Hosting facilities inherited; there is no company office. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-3) |
| A.7.4 | Physical security monitoring | yes | Hosting facilities; inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-4) |
| A.7.5 | Protecting against physical and environmental threats | yes | Hosting facilities; inherited. The single-region risk is tracked under A.8.14. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-5) |
| A.7.6 | Working in secure areas | yes | Hosting facilities; inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-6) |
| A.7.7 | Clear desk and clear screen | yes | The owner's workstation displays customer data and consoles. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-7-7) |
| A.7.8 | Equipment siting and protection | yes | Hosting equipment inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-8) |
| A.7.9 | Security of assets off-premises | yes | The owner's laptop holds source, secrets and development data outside any controlled premises. | `gap` (not implemented) | [row](annex-a-assessment.md#a-7-9) |
| A.7.10 | Storage media | yes | Source, secrets and backups sit on local drives. | `gap` (not implemented) | [row](annex-a-assessment.md#a-7-10) |
| A.7.11 | Supporting utilities | yes | Hosting facilities; inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-11) |
| A.7.12 | Cabling security | yes | Hosting facilities; inherited. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-12) |
| A.7.13 | Equipment maintenance | yes | Hosting equipment inherited; repairs to the owner's device are covered by A.7.14. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-7-13) |
| A.7.14 | Secure disposal or re-use of equipment | yes | The owner's drives held secrets and data; disposal must not leak them. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-7-14) |

## A.8 Technological controls

| Control | Title | Applicable | Justification | Status | Assessment |
|---|---|---|---|---|---|
| A.8.1 | User endpoint devices | yes | The owner's device is the administrative endpoint for every system. | `gap` (not implemented) | [row](annex-a-assessment.md#a-8-1) |
| A.8.2 | Privileged access rights | yes | platform_admin bypasses tenant isolation, and operator and DB admin roles exist. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-2) |
| A.8.3 | Information access restriction | yes | Multi-tenant isolation and per-agent record scope. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-3) |
| A.8.4 | Access to source code | yes | The source contains the security logic; a repository compromise is a platform compromise. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-8-4) |
| A.8.5 | Secure authentication | yes | An internet-facing login protects PII. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-5) |
| A.8.6 | Capacity management | yes | Concurrency limits affect availability for brokerages. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-6) |
| A.8.7 | Protection against malware | yes | Public and authenticated upload paths accept untrusted files. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-7) |
| A.8.8 | Management of technical vulnerabilities | yes | A large dependency surface (Python, npm, container images). | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-8) |
| A.8.9 | Configuration management | yes | Misconfiguration (secrets, demo logins, recovery mode) is a top risk for a small team. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-9) |
| A.8.10 | Information deletion | yes | Neoh has retention and erasure commitments to brokerages and consumers. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-10) |
| A.8.11 | Data masking | yes | Logs, diagnostics and exports must not expose PII. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-11) |
| A.8.12 | Data leakage prevention | yes | Consumer PII and communications are attractive to exfiltrate. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-12) |
| A.8.13 | Information backup | yes | The authoritative CRM data must survive loss and corruption. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-13) |
| A.8.14 | Redundancy of information processing facilities | yes | Paying brokerages expect availability. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-14) |
| A.8.15 | Logging | yes | Accountability for staff, AI and operator actions, and for breach investigation. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-15) |
| A.8.16 | Monitoring activities | yes | A sole operator needs automated detection. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-16) |
| A.8.17 | Clock synchronization | yes | TOTP, token expiry and log correlation depend on time. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-8-17) |
| A.8.18 | Use of privileged utility programs | yes | Operator scripts and DB access windows bypass application controls. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-18) |
| A.8.19 | Installation of software on operational systems | yes | Production must run only reviewed, built artefacts. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-19) |
| A.8.20 | Networks security | yes | Databases must not be reachable from the internet. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-20) |
| A.8.21 | Security of network services | yes | The edge, webhooks and provider APIs carry customer data. | `inherited` (inherited from a provider; our own duties are listed) | [row](annex-a-assessment.md#a-8-21) |
| A.8.22 | Segregation of networks | yes | Staging must never reach production data, and tenants must not reach each other. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-22) |
| A.8.23 | Web filtering | yes | The owner's device browses the web with administrative sessions open. | `owner_action` (awaiting owner action) | [row](annex-a-assessment.md#a-8-23) |
| A.8.24 | Use of cryptography | yes | Risk and customers both require PII encryption, password hashing and TLS. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-24) |
| A.8.25 | Secure development life cycle | yes | The product is in-house software. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-25) |
| A.8.26 | Application security requirements | yes | Security requirements drive every slice. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-26) |
| A.8.27 | Secure system architecture and engineering principles | yes | Architecture choices (RLS, recovery mode) are the main controls. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-27) |
| A.8.28 | Secure coding | yes | In-house code handles authentication, PII and payments. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-28) |
| A.8.29 | Security testing in development and acceptance | yes | Changes must be security-tested before release. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-29) |
| A.8.30 | Outsourced development | yes | AI coding agents write most of the code, so it is treated as outsourced development. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-30) |
| A.8.31 | Separation of development, test and production environments | yes | Live payment and telephony keys make an environment mix-up costly. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-31) |
| A.8.32 | Change management | yes | Changes are frequent, and a push to main deploys staging. | `implemented` (implemented; evidence in the assessment) | [row](annex-a-assessment.md#a-8-32) |
| A.8.33 | Test information | yes | Tests must not use or leak real customer data. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-33) |
| A.8.34 | Protection of information systems during audit testing | yes | Pentests and DAST can disrupt or expose live systems. | `partial` (partially implemented) | [row](annex-a-assessment.md#a-8-34) |
