# Subprocessor inventory

Every third party that receives brokerage or client data, what it gets,
where it is processed, how long it keeps it, and how Neoh deletes or stops
it. Status as of 2026-10-01, from `infra/digitalocean/app.yaml`, the
backend's provider code and each provider's published terms. **Before
launch, counsel confirms each DPA and the brokerage-facing subprocessor list**
(`docs/privacy-counsel-checklist.md`).

"Prod" means it is configured in the production app spec. "Local" means it is
wired in code and configured only in a development `.env`.

| Provider | Prod? | What it receives | Region | Provider retention | Neoh's deletion / stop | DPA |
|---|---|---|---|---|---|---|
| **DigitalOcean** (App Platform, Managed PostgreSQL, Managed Valkey, Spaces) | yes | everything (hosting) | US (nyc) | PG backups 7 days; Spaces versioning off; build logs 90 days | erasure engine; backups expire | DO DPA (standard terms) — confirm accepted |
| **Twilio** (voice, numbers) | yes | callee/caller numbers, call audio in transit, forwarding DIDs | US | message bodies kept until deleted; call logs; recordings until deleted; 30-day backup | numbers released at erasure (`release_forwarding_number`, new); calls/messages: Twilio DELETE APIs — not yet called | Twilio DPA — confirm |
| **Stripe** | yes | owner name/email, card (Stripe-hosted), invoices | US | Stripe keeps customer & invoice records 5+ years (its own obligations); DELETE customer leaves it retrievable | renewal stopped at closure; Neoh keeps billing 7 years as controller; customer redaction via Stripe's redaction-job API — manual, on request | Stripe DPA (automatic) |
| **Bridge Interactive / Unlock MLS (ACTRIS)** | yes | none from brokerages (inbound listing feed) | US | — | licence-end purge (`privacy_purge_mls_feed`, runbook §R2) | MLS licence agreement |
| **SMTP — smtp.gmail.com** | yes | recipient addresses, subjects, bodies of every email Neoh sends | Google | a copy stays in the sending account's Sent mail | none from Neoh | **Consumer Gmail has no DPA. Must be Google Workspace (with the Workspace DPA) or a transactional provider before launch.** |
| **Fireworks AI** (LLM) | yes | prompts with CRM context, tool outputs | US | serverless: no prompt storage without opt-in | none needed | Fireworks DPA — confirm |
| **Google** (OAuth: Gmail/Calendar per agent; Maps) | yes (OAuth app) | per-agent tokens; calendar events written to the agent's own calendar | Google | the agent's own account | grant revoked at offboarding/erasure (`revoke_google_token`, new); events stay in the agent's calendar | Google API terms; agents' own accounts |
| **Plivo** (voice) | local | call audio, numbers | US | CDRs 90 days, redacted CDRs 7 years, recordings 30 days | numbers released at erasure (new); recordings DELETE API exists — not yet called | confirm before enabling |
| **Telnyx** (hosted SMS, 10DLC) | local | message bodies, numbers, brand EIN/address | US | bodies wiped within 10 days; metadata and hashes kept (undisclosed) | hosted number disconnected at erasure (fixed 2026-10-01: it was silently failing); no message-delete API | confirm before enabling |
| **Alibaba DashScope (Qwen realtime voice)** | local; **default ON for Plivo calls** | live call audio | **Singapore (ap-southeast-1) by default** | not published | none | **Re-region to the US (Virginia) endpoint, or disclose Singapore processing, before enabling with real calls** |
| **ElevenLabs** (TTS) | local | text to be spoken | US | trains on content by default on non-Enterprise plans; history until deleted | history DELETE API — not called | **opt out of training in the account before use** |
| **RunPod** (GPU reconstruction) | local | property photos for one job | US (SECURE cloud) | pod terminated after the job → everything deleted (volume 0 GB) | `_terminate` with retry | confirm |
| **Regrid, RentCast, ATTOM, Census, FRED, WalkScore** | local/partial | parcel ids and addresses (no client data) | US | per provider | none needed | API terms |
| **Google Street View** (via property imagery) | local | addresses | Google | — | Neoh caches images 30 days (`property_imagery.py:47`) — **Google's terms may forbid caching; counsel/engineering to confirm** | Maps terms |
| **Tavily** (web search) | not configured | search queries | US | may use queries to improve service | — | do not enable with client data |

## Actions before launch

1. Replace consumer Gmail SMTP with Workspace (DPA) or a transactional email provider.
2. Re-region DashScope to the US, or disable Qwen realtime, or disclose Singapore processing to brokerages.
3. Opt out of ElevenLabs training, or keep it off.
4. Confirm the signed or accepted DPA for DigitalOcean, Twilio, Fireworks, Plivo, Telnyx and RunPod.
5. Wire the provider-side deletes that exist but are not called yet: Twilio messages, calls and recordings; Plivo recordings; ElevenLabs history. Where the brokerage's own records still need these, it is a policy decision first.
6. Publish this list to brokerages, and notify them of changes. Most processor terms require advance notice of new subprocessors.
