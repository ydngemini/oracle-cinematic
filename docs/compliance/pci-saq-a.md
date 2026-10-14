# PCI DSS scope: SAQ A (payment cards never enter Neoh)

> Ledger row: [L17-07](../claude-prompts/LEDGER.md#l17-07-pci-dss-saq-a-scope-no-card-data-outside-stripe). Written 2026-10-14 at base `0a76a72`.
> This is an engineering scope note, not a QSA assessment. The brokerage-facing self-assessment is the operator's to complete and sign. No acquirer or QSA has reviewed it.

## The boundary

Neoh takes money in one way: a brokerage owner pays the subscription through Stripe.

- **Checkout.** `POST /api/billing/create-checkout-session` (`backend/billing.py`, `create_checkout_session`) creates a Stripe Checkout Session server-side. The browser is redirected to the Stripe-hosted URL (`oracle-app/src/components/BillingOverlay.jsx`, `handleSubscribe`, `window.location.href = url`). The card is typed on `checkout.stripe.com`, never on a Neoh page.
- **Card changes and invoices.** `POST /api/billing/create-portal-session` hands the owner a Stripe customer-portal URL (`billing_portal.Session.create`).
- **Webhook.** `POST /api/billing/webhook` receives signed Stripe events. They carry ids, status and amounts. Stripe never sends a full card number to a webhook, and `billing.py` stores no card fields (`grep -n "last4\|payment_method\|exp_month" backend/billing*.py` finds nothing at `0a76a72`).
- **No Stripe.js or Elements.** The SPA does not load `js.stripe.com`. The production CSP (`oracle-app/nginx.conf`, `script-src 'self' https://maps.googleapis.com https://maps.gstatic.com`) would block it if it tried.

A merchant whose card acceptance is fully outsourced through a redirect to a PCI DSS-validated processor's hosted page, and which stores, processes and transmits no cardholder data, is the SAQ A shape. Two parts of Neoh are in scope even so:
- the page that sends the browser to Checkout;
- the server that creates the session.

Their integrity matters, because a tampered redirect could send a customer to a fake payment page.

## What keeps us in SAQ A

1. **Payment stays on Stripe's pages.** Never embed a card field, an iframe we control, Stripe Elements or a custom form. Never accept a card number in an API.
2. **No card data enters through any other channel.** This is the part Neoh's product makes hard. Neoh is a communications product, so a card number can arrive as:
   - a caller reading it to the AI on a call;
   - a client texting it;
   - an agent pasting it into a note or an email;
   - someone typing it into the AI chat.

   Stored, even encrypted, that is cardholder data at rest, and the scope becomes SAQ D. Since L17-07, **`backend/pan_redaction.py` removes it before storage** on every path below.
3. **The AI never asks for a card.** Every voice and chat persona carries `PAYMENT_CARD_RULE`. It says: never ask for, repeat, confirm or read back a card number, expiry or security code; never write one into a note, message or tool call; stop a caller who starts and point them to a secure payment link. The rule lives in:
   - `neoh_persona.build_system_prompt` (both densities);
   - `qwen_omni_realtime._system_instructions` (Qwen realtime, ElevenLabs outbound calls and the provisioned ElevenLabs agent via `scripts/elevenlabs-agent.py`);
   - `inbound_voice.build_inbound_intake_instructions`;
   - `ai_chat_agent._VOICE_PROMPT`;
   - `qwen_voice_agent`.
4. **The redirect page and the session endpoint stay locked down:**
   - the CSP, which allows scripts only from Neoh itself and Google Maps;
   - owner-only checkout/portal (BILL-4);
   - signed, replay-checked webhooks (BILL-1/BILL-3, `docs/security-launch-review.md`).

## Where cards are removed (all before storage, model, live frame or log)

| Surface (`report()` name) | Path | Stored in |
|---|---|---|
| `client_note` | `client_note_crypto.seal_body`: every note write (agent route create/update, AI chat tool, AI call) | `client_notes.body_ciphertext` |
| `ai_call_note`, `ai_call_save_note` | `commands_api.deliver_call_notes`, `elevenlabs_realtime` `save_note` tool | client note, `client_activities`, CALL_NOTES frame |
| `call_transcript_excerpt` | `commands_api.call_telemetry` | `negotiation_events.transcript_excerpt` |
| `voice_transcript_chunk` | `voice_intel.ingest_voice_telemetry` (before the counter-offer parser) | `negotiation_events`, VOICE_TELEMETRY frame |
| `voice_note_transcript` | `voice_intel._process` (before the extraction model) | `interaction_logs.payload` |
| `inbound_call_transcript` | `inbound_voice._clean_transcript` | `inbound_voice_calls` sealed transcript + answers, callback task |
| `voice_call_speech` | `ai_chat_agent._generate_voice_reply` | not stored; removed before the model provider |
| `sms_inbound`, `sms_outbound` | `messaging_data.record_inbound_message` / `record_outbound_message` | `sms_messages.body` |
| `opt_out_source_text` | `outreach_compliance.ConsentLedger.suppress` | `outreach_suppression.source_text` |
| `crm_message_<channel>` | `crm.send_message` (email, SMS log, internal note) | `interaction_logs.payload`, `email_outbox.body_text` (so the email sent) |
| `outreach_draft` | `commands_api.stage_command` / `edit_command`, **before the approval hash** (C4.2) | `command_executions.draft`, the approval payload, the message sent |
| `ai_chat_user_message`, `ai_chat_assistant_message`, `ai_chat_client_note` | `ai_chat_store` | `ai_chat_messages` (and what the model reads back), client notes |
| `chat_memory_<role>` | `memory_core.session_manager.record_interaction` | `user_interactions.content` |
| `contact_intake` | `contacts_api.create_intake` | `contact_intake_sessions` (sealed) |
| `perception_event`, `portal_activity` | `intelligence_os_api.capture_events`, `client_portal._clean_portal_payload` | `interaction_logs.payload` |
| logs | `pan_redaction.PanLogRedactionFilter` on every root/uvicorn handler (`server._configure_logging`) | container log stream |

**What is recorded.** The text keeps `[card number removed]` / `[security code removed]` in place, so the agent sees that something was said. The removal itself is recorded as surface plus counts, never the number:
- a `WARNING oracle.pci` log line;
- a process counter (`REDACTION_COUNTS`);
- an `audit_ledger` row with category `PCI_REDACTION` and action `payment_card_data_removed`, written best effort off the request path.

**Detection rule.** A match needs all three of:
- 13 to 19 digits, solid or in card-shaped groups with one consistent separator (space, hyphen, dot or comma);
- a valid Luhn check;
- a payment-network prefix that is valid for that length.

A 15- or 16-digit card said as digit words ("four one one one…", with "oh" as zero) is also matched, optionally followed by an expiry and/or a code. A CVV is matched:
- after `CVV`/`CVC`/`CSC`/"card verification" on its own;
- after looser phrases ("security code", "the code on the back") only when a card was found in the same text.

`tests/test_pan_redaction.py` pins these cases as left alone:
- phone numbers, alone or in pairs;
- parcel/APN/MLS/folio ids and anything those words introduce;
- prices, ZIP+4, dates, UUIDs and timestamps;
- one-time login codes;
- solid reference numbers longer than 19 digits.

## Known limits (what would still break SAQ A)

- **Not covered by the redactor:**
  - uploaded files, images, PDFs and chat attachments (the bytes are stored as uploaded);
  - call audio, if a recording is ever turned on (ElevenLabs is provisioned with recording off);
  - free-text fields outside the table above (tasks, deal and showing notes, documents, `leads.payload`);
  - text inside rows written before this change. Nothing historical was scanned; no backfill was run.
- **Detection gaps:**
  - spoken numbers other than single digit words ("forty-one eleven", "double four");
  - dictated 13-, 14- or 19-digit cards;
  - a card split across two messages or two call turns;
  - an expiry date on its own (it is not cardholder data without the number).
- **False positive (the safe way to be wrong).** Two phone numbers, or a phone number and a ZIP, dictated digit by digit with nothing between them are redacted about 1–2% of the time. The seeded sample in the tests bounds it at under 3%.
- **Provider copies.**
  - The redactor runs after the text reaches Neoh, so it cannot reach copies a provider already holds. Telnyx may keep the original SMS, and ElevenLabs or the speech provider may hold the live transcript under its own retention. Neither has been checked.
  - On a call, the rule stops the AI from asking for a card or repeating it, but it cannot stop a caller from reading one out.
- **What breaks SAQ A outright:**
  - Stripe Elements or any card field on a Neoh page;
  - a "take payment over the phone" feature;
  - storing Stripe card details beyond what the portal shows;
  - turning on call recording;
  - a new free-text storage path that does not call `pan_redaction.scrub`. The coverage guard `test_each_write_path_still_applies_the_redactor` lists the known ones; a new path must be added to it.

## Operator and owner actions (not code)

- The brokerage-facing SAQ A (eligibility criteria and attestation) is completed and signed by the operator. Engineering evidence is this file and the L17-07 tests.
- Confirm with Stripe or the acquirer that Neoh's merchant category qualifies for SAQ A.
- Agents should be told, in training and in the Platform Use Policy, not to take card numbers by phone, text or email. The drafted policy wording is in the L17-07 report.
