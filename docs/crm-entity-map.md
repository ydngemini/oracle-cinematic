# CRM entity map: how each channel reaches one person (T04.01, T04.13)

Read at branch `claude/continue-n7jgwq` (2026-10-16). Line numbers drift; re-verify before relying on one (CONTRACT C1.5). Ledger: L04-04.

## The entities

| Entity | Table | Identity | Notes |
|---|---|---|---|
| Contact | `agent_contacts` | `id`; blind lookup hashes `email_lookup_hash`, `phone_lookup_hash` (`contact_truth.lookup_hash`, HMAC per tenant over the normalized value) | Sealed PII. Hash indexes are **not unique** (0054); `legacy_client_id` links back to a client |
| Client | `clients` | `id`; `contact_id` → `agent_contacts` (not unique) | The CRM working record; plaintext name/email/phone still present (A01, owned by 17) |
| Lead | `leads` | `id`; `seller_client_id` → `clients` | Property-side record |
| Client lookup index | `client_contact_index` (0156) | hashes + name tokens for `clients` | Filled, not read yet (L17-23) |
| Deal party | `transaction_parties` | optional `client_id`, else `display_name` only | Linked by hand |

Normalization is shared: `contact_truth.normalize_email` / `normalize_phone` (E.164; +1 for 10 digits) and `lookup_hash`.

## Channels

| Channel | Code | Match key | Creates a contact? |
|---|---|---|---|
| Lead webhook | `lead_routing_api.receive_lead` (`POST /api/public/lead-intake/{public_id}`) | email hash **or** phone hash on `agent_contacts`, newest first | **Yes**: client + contact, linked both ways; tenant advisory lock; event dedup by `(tenant, connector, external_event_id)` |
| Inbound text | `messaging_data.record_inbound_message` → `_match_contact` | phone hash only, oldest contact first | No: stored with no contact |
| Inbound call | `inbound_voice.prepare_inbound_call` → `_match_contact` | phone hash (agent's own contact first, then oldest); fallback digit match on `clients.phone` | No: `unqualified` + a task with no client |
| Website | `public_sites_api` (GET only); `sites_api.record_attribution` (signed-in, free-text `subject_id`) | none | No public submit route exists |
| Email in | none (L10-07: nothing reads an inbox) | — | — |
| Deal parties | `portfolio_api` create/add party | explicit `client_id` | No |
| Manual | `contacts_api.create_contact`, `crm.create_client` | none (no dedup) | Yes, unconditionally |

## Same person through several channels

- **Webhook first, then text and call:** one contact; the text and the call link on arrival (same normalization and hash).
- **Text and call first, then the webhook:** before L04-04 the webhook created the contact and the earlier text and call stayed linked to nobody for good. **Since L04-04** (`contact_linking.link_earlier`), the webhook links that brokerage's unlinked inbound texts (same E.164 sender) and calls (same caller hash) from the last 90 days to the contact. Rows already linked to someone are never moved. Proven by `backend/tests/test_contact_linking_live.py`.

## Known gaps (open, in L04-04)

1. Website forms never create or link a person (no public submit route; attribution `subject_id` is unvalidated text).
2. Texts and calls still never create a contact on their own; they wait for intake or a manual entry. A contact created **manually** (`contacts_api.create_contact`) runs the same linking since L04-05, and so does a phone added later by editing a contact (L04-06).
3. Match keys and tie-breaks differ: texts and calls by phone only (oldest / agent-first), the webhook by email or phone (newest).
4. The call fallback's digit match compares E.164 digits (`1302…`) with raw `clients.phone` digits, so `(302) …` never matches.
5. The webhook does not look at `clients` that have no contact (from `crm.create_client` or a house profile), so the same person can get a second client.
6. No unique constraint on either hash; manual creation does no dedup (AC04.02 is not met).
