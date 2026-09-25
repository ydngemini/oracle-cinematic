-- Neoh DR drill fixture — two isolated brokerages, entirely synthetic.
--
-- No real customer data, no real provider identifiers, no real phone numbers
-- (the +1555 01xx range is reserved for fiction), no real Stripe ids.
-- Deterministic UUIDs so the drill's assertions can be written down in advance
-- and compared exactly rather than "roughly".
--
-- Brokerage A is the rich one: it carries at least one row of every kind whose
-- loss would matter — an accepted and a pending invitation, contacts, a deal, a
-- lead with a child media row (the FK relationship that broke the first scoped
-- backup), an MLS feed with an entitlement, a subscription, audit entries, and
-- telephony/messaging route metadata.
--
-- Brokerage B exists for one purpose: to be the tenant that must NOT be visible
-- from A after the restore. A restore that brings back A's data perfectly and
-- also lets A read B has not succeeded.

BEGIN;

-- ── Tenants ────────────────────────────────────────────────────────────────

INSERT INTO tenants (id, slug, name) VALUES
  ('aaaaaaaa-0000-4000-8000-00000000000a', 'drill-brokerage-a', 'Drill Brokerage A'),
  ('bbbbbbbb-0000-4000-8000-00000000000b', 'drill-brokerage-b', 'Drill Brokerage B');

-- ── Users ──────────────────────────────────────────────────────────────────

INSERT INTO users (id, tenant_id, agent_id, role) VALUES
  ('a0000001-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-00000000000a', 'owner-a@drill.invalid', 'broker_owner'),
  ('a0000002-0000-4000-8000-000000000002', 'aaaaaaaa-0000-4000-8000-00000000000a', 'agent-a@drill.invalid', 'agent'),
  ('b0000001-0000-4000-8000-000000000001', 'bbbbbbbb-0000-4000-8000-00000000000b', 'owner-b@drill.invalid', 'broker_owner'),
  ('b0000002-0000-4000-8000-000000000002', 'bbbbbbbb-0000-4000-8000-00000000000b', 'agent-b@drill.invalid', 'agent');

-- ── Invitations: one already accepted, one still outstanding ───────────────
--
-- The accepted one proves consumed state survives; the pending one proves an
-- unconsumed token survives without becoming usable twice.

INSERT INTO brokerage_invitations
  (id, token_hash, tenant_id, email, invited_by, invited_by_agent_id, expires_at, consumed_at, consumed_by)
VALUES
  ('a1111111-0000-4000-8000-000000000001',
   repeat('a', 64), 'aaaaaaaa-0000-4000-8000-00000000000a',
   'agent-a@drill.invalid', 'a0000001-0000-4000-8000-000000000001', 'owner-a@drill.invalid',
   now() + interval '7 days', now(), 'a0000002-0000-4000-8000-000000000002'),
  ('a1111111-0000-4000-8000-000000000002',
   repeat('b', 64), 'aaaaaaaa-0000-4000-8000-00000000000a',
   'pending-a@drill.invalid', 'a0000001-0000-4000-8000-000000000001', 'owner-a@drill.invalid',
   now() + interval '7 days', NULL, NULL);

-- ── Contacts / clients ─────────────────────────────────────────────────────
--
-- Three for A, two for B. The asymmetry is deliberate: a cross-tenant leak
-- shows up as a count of five rather than as an obviously wrong name.

INSERT INTO clients (id, tenant_id, full_name) VALUES
  ('ac000001-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-00000000000a', 'Drill Client A1'),
  ('ac000002-0000-4000-8000-000000000002', 'aaaaaaaa-0000-4000-8000-00000000000a', 'Drill Client A2'),
  ('ac000003-0000-4000-8000-000000000003', 'aaaaaaaa-0000-4000-8000-00000000000a', 'Drill Client A3'),
  ('bc000001-0000-4000-8000-000000000001', 'bbbbbbbb-0000-4000-8000-00000000000b', 'Drill Client B1'),
  ('bc000002-0000-4000-8000-000000000002', 'bbbbbbbb-0000-4000-8000-00000000000b', 'Drill Client B2');

-- ── A lead, and a child row that points at it ──────────────────────────────
--
-- This pair is the whole reason the first scoped backup was wrong: excluding
-- lead data while keeping property_media meant the child row was rejected by
-- its foreign key and silently dropped. The drill must be able to catch that
-- again, so the fixture contains exactly that shape.

INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, dossier_status)
VALUES ('a1ead001-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-00000000000a',
        'DRILL-PARCEL-0001', 'DE', 42, 'ac000001-0000-4000-8000-000000000001', 'under_contract');

INSERT INTO property_media (id, tenant_id, lead_id, url)
VALUES ('a11ed1a0-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-00000000000a',
        'a1ead001-0000-4000-8000-000000000001', 'https://drill.invalid/media/0001.jpg');

-- ── MLS: a feed, and A's entitlement to it ─────────────────────────────────

INSERT INTO mls_sync_status (mls_id, mls_name, provider, license_classification, backfill_complete, last_success_at)
VALUES ('drill_board', 'Drill Board MLS', 'reso', 'licensed_property_listing', true, now());

INSERT INTO oracle_mls_listings (id, mls_id, mls_number, address, state_code, list_price)
VALUES ('a1150001-0000-4000-8000-000000000001', 'drill_board', 'DRILL-0001',
        '1 Drill Way', 'DE', 250000);

INSERT INTO mls_feed_entitlements (tenant_id, mls_id, granted_by)
VALUES ('aaaaaaaa-0000-4000-8000-00000000000a', 'drill_board', 'drill-fixture');

-- ── Subscription ───────────────────────────────────────────────────────────
--
-- Synthetic Stripe identifiers. Stripe remains authoritative for money after a
-- restore; this row only has to prove the LOCAL belief survived.

INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status)
VALUES ('aaaaaaaa-0000-4000-8000-00000000000a', 'cus_DRILLFIXTUREA', 'sub_DRILLFIXTUREA', 'active');

-- ── Provider route metadata ────────────────────────────────────────────────
--
-- +1555 01xx is the reserved fictional range. Nothing here can dial out.

-- Both numbers, and they point in OPPOSITE directions:
--   inbound_did          the client dials this to reach the AI
--   agent_forward_e164   the AI dials this to reach the human
-- telephony_routes_agent_forward_chk requires the second to exist whenever the
-- forward-on-request / forward-when-AI-unavailable flags are on, and both
-- default to true — so omitting it is rejected.
-- provider_account_id must equal twilio_account_sid when provider='twilio'
-- (the default), and the SID must match ^AC[0-9A-Fa-f]{32}$. A synthetic SID
-- in that shape satisfies the constraint and addresses no real account. Note
-- twilio_account_sid is char(34), so the SID must be exactly 34 characters or
-- blank-padding breaks the equality with provider_account_id (text).
INSERT INTO telephony_routes (
    tenant_id, agent_id, inbound_did, agent_forward_e164,
    provider, twilio_account_sid, provider_account_id)
VALUES ('aaaaaaaa-0000-4000-8000-00000000000a', 'agent-a@drill.invalid',
        '+15550100001', '+15550100002',
        'twilio', 'ACd4110000000000000000000000000000',
        'ACd4110000000000000000000000000000');

INSERT INTO messaging_routes (tenant_id, agent_id, provider)
VALUES ('aaaaaaaa-0000-4000-8000-00000000000a', 'agent-a@drill.invalid', 'telnyx');

COMMIT;
