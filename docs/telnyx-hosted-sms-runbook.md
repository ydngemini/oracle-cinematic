# Telnyx Hosted SMS — staging runbook

Neoh runs two independent carrier rails against **one** public business number:

```
Agent's public business number  (e.g. +1 302 555 1234)
  ├── Voice     → Plivo   (backend/voice_provider.py, telephony_routes)
  └── Messaging → Telnyx  (backend/messaging_provider.py, messaging_routes)
```

Messaging **reuses** the number voice already verified. There is no second
public number and no second number field — `messaging_routes` joins
`telephony_routes` on `(tenant_id, agent_id)` and reads
`voice_caller_id_e164`. An agent cannot set up texting until voice's
server-side Verified Caller ID flow has completed for that number.

Every step below is marked:

- **[NEOH]** — automated by Neoh, no human action
- **[TELNYX]** — manual action in the Telnyx Mission Control Portal
- **[CUSTOMER]** — the brokerage/agent must do something

---

## 1. Telnyx account preparation

| # | Step | Who |
|---|------|-----|
| 1 | Create a Telnyx account | **[TELNYX]** |
| 2 | Reach **Level 2 Verification** — hosted-messaging orders cannot be submitted below this | **[TELNYX]** |
| 3 | Create a V2 API key (`KEY…`), set as `TELNYX_API_KEY` | **[TELNYX]** |
| 4 | Copy the account **Public Key** (Account Settings → Keys & Credentials), set as `TELNYX_PUBLIC_KEY` | **[TELNYX]** |
| 5 | Set `ORACLE_MESSAGING_PROVIDER=telnyx` | **[TELNYX]** |

Both keys are secrets. In production they belong in DigitalOcean's
encrypted app-level environment configuration, or (preferred, per tenant) in
Neoh's encrypted `provider_credentials` vault under provider `telnyx`.

## 2. Messaging Profile + webhook

| # | Step | Who |
|---|------|-----|
| 6 | Create a Messaging Profile with webhook URL `${ORACLE_PUBLIC_BASE_URL}/api/messaging/webhooks/telnyx` | **[TELNYX]** or **[NEOH]** via `MessagingProvider.create_messaging_profile` |
| 7 | Confirm the webhook URL is reachable over HTTPS from the public internet | **[TELNYX]** |

The webhook path is CSRF-exempt by design (`backend/csrf_middleware.py`) —
Telnyx authenticates with an Ed25519 signature over the raw body, not a
session cookie.

## 3. Per-agent number setup

| # | Step | Who |
|---|------|-----|
| 8 | Agent connects + verifies their business number for **voice** first (Plivo) | **[CUSTOMER]** |
| 9 | "Check availability" → `POST /api/messaging/business-number/check-eligibility` | **[NEOH]** |
| 10 | If ineligible, Neoh shows the honest reason and stops. **Wireless numbers and Google Voice numbers are not supported.** | **[NEOH]** |
| 11 | "Connect text messages" → `PUT /api/messaging/business-number` submits the real hosted order and requests an SMS ownership code | **[NEOH]** |
| 12 | Agent enters the code → `POST /api/messaging/business-number/verify/complete` | **[CUSTOMER]** |
| 13 | Carrier processing (asynchronous, up to ~24–48 business hours) | **[TELNYX]** |
| 14 | "Check status" → `POST /api/messaging/business-number/refresh` polls the real order status | **[NEOH]** |

### If Telnyx asks for documents

Some orders return `incomplete_documentation` / `loa_file_invalid`. Neoh
moves the route to `pending_documents` and exposes:

`POST /api/messaging/business-number/documents/{loa|invoice}` (PDF, ≤5 MB)

The file is stored in Neoh's own protected object storage first (never a
public bucket, never a permanent public URL, contents never logged), then
forwarded to Telnyx's own document-upload API for that order.

An **LOA must be signed and dated by the end user within the last 30 days**
and the invoice must match it. **[CUSTOMER]**

## 4. 10DLC (required for US long-code business texting)

Neoh is the ISV. Each brokerage gets its **own** end-user Brand — never a
single shared Neoh brand.

| # | Step | Who |
|---|------|-----|
| 15 | Brokerage supplies legal business info (legal name, EIN, entity type, address, website) | **[CUSTOMER]** |
| 16 | `PUT /api/messaging/business/registration/brand` registers the Brand | **[NEOH]** |
| 17 | Brand vetting — brand data must match IRS CP-575 exactly or it stays unverified | **[TELNYX]** |
| 18 | `PUT /api/messaging/business/registration/campaign` submits the Campaign | **[NEOH]** |
| 19 | Carrier campaign review (industry-wide, not instant) | **[TELNYX]** |
| 20 | `POST /api/messaging/business/registration/campaign/assign-number` assigns the number once the campaign is approved | **[NEOH]** |

## 5. Verification tests

| # | Test | Expected |
|---|------|----------|
| 21 | Text the agent's business number from another phone | Inbound webhook arrives signed; one `sms_messages` row; CRM activity created if the sender matches a known contact |
| 22 | Send an approved outbound SMS through Neoh | Recipient sees the **agent's business number** as sender |
| 23 | Reply `STOP` from that phone | Contact suppressed across channels in Neoh's consent ledger; Telnyx also blocks at platform level |
| 24 | Place a voice call to the same number | **Plivo voice still answers — messaging setup does not touch the voice rail** |
| 25 | Replay the same inbound webhook twice | Exactly one message row, one CRM activity (idempotent on `provider` + `provider_message_id`) |

## 6. Disconnect

`DELETE /api/messaging/business-number`

- disables Neoh outbound + inbound routing
- submits hosted-number removal to Telnyx
- **preserves** message history, CRM activities, audit trail, and every
  consent/opt-out record
- **does not touch voice** — Plivo calling keeps working

## 7. Known unsupported number types

| Type | Hosted SMS |
|------|-----------|
| US/CA landline long code | ✅ supported |
| Toll-free | ✅ supported (separate, slower review) |
| Wireless / mobile | ❌ not supported (`NUMBER_CAN_NOT_BE_WIRELESS`) |
| Google Voice | ❌ not supported |
| Non-US | ❌ not supported (`NUMBER_IS_NOT_A_US_NUMBER`) |
| Already on Telnyx / hosted elsewhere | ❌ not eligible |

Neoh surfaces these as explicit eligibility states rather than silently
provisioning a different number.
