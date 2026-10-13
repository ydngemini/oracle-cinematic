# 14 — Sites, social, advertising, search presence and the client portal

> **Scope:** Make every advertised growth channel (hyperlocal sites, social, paid media, SEO/AEO/GEO, video and the client-facing portal) either work end to end with approval, receipts and rollback, or show a truthful setup-required state. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 02, 07, 08, 10, 11 · **Unblocks:** 20 · **Ledger:** [L14](LEDGER.md#l14) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- Sites backend: `backend/sites_api.py`, `backend/db/migrations/0056_studio_sites_and_contract_types.sql`, `backend/db/migrations/0060_site_access_controls.sql`, `backend/tests/test_studio_sites.py`
- Lead intake and routing: `backend/lead_routing_api.py`, `backend/tests/test_lead_routing.py`
- Client portal: `backend/client_portal.py`, `backend/db/migrations/0008_client_portals.sql`, `oracle-app/src/components/SecureDossierPage.jsx`
- Video: `backend/video_studio_api.py`, `backend/video_studio.py`, `oracle-app/src/components/VideoStudioPanel.jsx`
- Frontend: `oracle-app/src/components/OurAITab.jsx`, `oracle-app/src/components/StudioTab.jsx`, `oracle-app/src/components/SitePublishPanel.jsx`, `oracle-app/src/components/SitePreview.jsx`, `oracle-app/src/components/MarketplaceBrowse.jsx`, `oracle-app/src/components/ProviderDeliveryPage.jsx`, `oracle-app/src/App.jsx`
- Approvals: `backend/approval_service.py`; privacy map: `backend/privacy_data_map.py`
- Prompt [11](11-property-market-intelligence.md) for property sources and the legacy MLS gating it owns (D-2026-10-12-01).

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Sites have four statuses.** `sites_api.py:27` `SiteStatus` is `draft`, `preview`, `published`, `archived`; the table check matches (`0056_studio_sites_and_contract_types.sql:61`).
- **Publishing is bound to an approved payload hash.** `sites_api.py:648` `publish_site` rejects an approval that is not `approved`, has expired, or whose `payload_hash` differs from the revision payload (`:671-676`).
- **A custom domain is only ever recorded as pending.** `publish_site` inserts `hyperlocal_site_domains` with status `pending` (`sites_api.py:693`) and returns `custom_domain_status` `pending_verification` (`:724`). The table allows `verified`/`active` (`0056_studio_sites_and_contract_types.sql:119-120`), but no route in `sites_api.py` sets them.
- **No rollback or unpublish route exists.** The router (`sites_api.py:24`) serves templates, list, create, get, revisions, preview, publish-approval, publish, attribution, funnel and collaborators only.
- **Nothing serves the returned public path.** `publish_site` returns `platform_path` `/sites/{slug}` (`sites_api.py:723`); grep finds no frontend or backend route for it. Only `/site-preview/` is routed (`App.jsx:105`, `SitePreview.jsx:8`).
- **Attribution writes need an agent session.** `sites_api.py:728` `record_attribution` depends on `require_context`, so an anonymous site visitor cannot record one.
- **Public lead intake requires an HMAC signature.** `lead_routing_api.py:642` `receive_lead` requires `X-Oracle-Timestamp` and `X-Oracle-Signature` headers (`:646-647`) checked against the connector secret (`:669`).
- **A campaigns table exists with no code behind it.** `0056_studio_sites_and_contract_types.sql:162` creates `studio_campaigns` with `hard_budget` and an approval status; outside tests, only `privacy_data_map.py:95` names it.
- **No social or ad network integration exists in the backend.** `grep -rliE "instagram|facebook|linkedin|tiktok|google_ads" backend --include=*.py` (excluding tests) matches only a prompt string in `disposition_enforcer.py:60`.
- **Our AI lists growth channels as setup required.** In `OurAITab.jsx`: Social Studio `:477`, Multi-Channel Advertising `:483`, OpenAI Ads `:489`, Listing Blast `:495`, Own Your ZIP Code `:501`, Social Ads `:507`, Google LSA `:513`, Customer Search App `:534`, AEO `:588` and GEO `:594` use `STATUS.setup`; SEO is `STATUS.partial` (`:600`).
- **"Live" means only that an endpoint answered.** `OurAITab.jsx:338` `sourceState` returns `STATUS.live` whenever the source loaded. Hyperlocal Website (`:564`) and Website Design (`:582`) use `sourceState('sites')`, so they read "Live" with zero published sites.
- **Legacy MLS/IDX surfaces remain in the Sites flow (to be gated, D-2026-10-12-01).** `OurAITab.jsx:82` polls `/api/mls/health` as source `idx`; `:570` and `:576` list the legacy "MLS / IDX" and "WordPress IDX Plugin" items. `StudioTab.jsx:127` `loadIdx` calls the same health route for a wizard step (`:162`). `sites_api.py:162` `AuthorizedIdxSource` and `:182` `idx_requested` stay in the revision schema.
- **The client portal has a front door.** `client_portal.py:113` mints a single-use token link (`/vault/secure-access/{token}`, `:158`); `App.jsx:110` routes it to `SecureDossierPage.jsx`, which opens `/portal/session/{token}` (`:162`).
- **Video Studio is behind a flag.** `server.py:433` mounts `video_studio_api` only when `config.ORACLE_FEATURE_VIDEO_STUDIO` is set; workers start under the same flag (`server.py:156`).

## 3. Goal

A broker or agent can draft a site, social post, campaign or video from approved evidence, preview it, get approval bound to the exact content, publish it through a real provider or Neoh's own host, see the provider receipt, and roll it back. Every channel that needs a customer account shows a truthful setup-required flow. The client portal lets a consenting client see only what was shared with them. No status badge claims more than the evidence shows.

## 4. Engineering tasks

- **T14.01 Audit the site lifecycle.** Walk branding, agent attribution, template, home and area pages, SEO metadata, preview revision, approval, domain, publication, rollback and CRM lead intake. Record which panel and route backs each step, and which step has none.
- **T14.02 Gate legacy MLS/IDX out of Sites and Our AI (D-2026-10-12-01).**
  - Remove the legacy `idx` source poll (`OurAITab.jsx:82`), the "MLS / IDX" and "WordPress IDX Plugin" items, and the IDX wizard step in `StudioTab.jsx`.
  - Stop new revisions from accepting `authorized_idx_sources`; keep the column and existing data (no deletion without owner approval).
  - Coordinate with T11.01 so the shared `/api/mls/health` gating lands once.
- **T14.03 Unify the publishing contract.** One state machine for sites, posts, campaigns and videos: Draft → Validated → Awaiting_Approval → Approved → Provider_Accepted → Published_Verified → Degraded/Failed → Archived. Each revision is immutable and names its reviewer. No unapproved upsert may republish a live page or change price or scope.
- **T14.04 Build domain verification and rollback.** Issue a DNS verification challenge, move `hyperlocal_site_domains` through `verified`/`active`/`failed` on evidence, and add a tested rollback to the previous published revision.
- **T14.05 Serve published sites.** Implement the public route behind `platform_path` (or change the contract), with caching that honours unpublish and archive at once.
- **T14.06 Brand-safe content generation.** Generate only from approved CRM, broker-owned listing (`/api/crm/listings`) and market evidence, with freshness and citations. Run fair-housing, advertising-compliance, media-licence and broker-disclosure checks, then require human review. No invented market trends or seller financial claims.
- **T14.07 Social channels.** Provider OAuth with least scopes, connection lifecycle, scheduled posts, attachments, per-channel previews, short video where it fits, publish receipts, moderation and correction, failed/retry states, and analytics with provider timestamps. No integration is called connected before a live or sandbox call succeeds.
- **T14.08 Advertising.** Choose the supported networks. Validate partner requirements and account authorization, policy compliance, brokerage and agent spend limits, owner approval, campaign preview, budget pacing, receipts, pause/cancel and attribution disclaimers. Wire `studio_campaigns` or replace it. Every paid purchase is separately approved ([C4.1](CONTRACT.md#c4-external-actions-and-approvals)).
- **T14.09 Campaign templates.** Listing blast, price drop, open house, just sold, ZIP farming and Google LSA where partner access allows. Check audience eligibility and opt-in/opt-out; never target or exclude on protected housing traits.
- **T14.10 SEO/AEO/GEO.** Source-backed sitemaps, robots, canonical routes, structured metadata for broker-owned content, page performance, search-appearance tests, answer-ready factual copy and a crawl monitor. A mention is never reported as verified traffic, and no ranking is promised.
- **T14.11 Client Search App as the responsive web portal.** Decide with the owner whether the portal or a PWA is the product (no native app required). Implement client consent and auth, saved homes, alerts, scheduling, questions and CRM sync, with broker and agent permissions.
- **T14.12 Video Studio and media.** Asset provenance, rights to input images, export and resolution limits, background rendering and GPU budgets, uploads, compression, fallback, preview and publish approval.
- **T14.13 Role and workflow QA.** Cover Sites, Social, Homeowners, Automations and external preview pages. An agent cannot publish for another agent, create ad spend or expose data outside their grant.
- **T14.14 Instrument real outcomes.** Count drafts vs published, leads received, opt-ins, consented clicks, approved campaign cost and qualified outcomes. Attribution is association unless causally established.
- **T14.15 Public lead capture from sites.** Give published sites a lead form and visitor attribution that work without an agent session or a browser-held HMAC secret, with rate limits, consent capture and spam controls.
- **T14.16 Honest capability badges.** Make `sourceState` say "Live" only on evidence of a working channel (for example a published, verified site), not on an endpoint answering.

## 5. Failure and adversarial cases

- **F14.01 Content changes after approval.** A revision, hostname or budget is edited after approval and still publishes.
- **F14.02 Fake connection.** A disconnected social or ad account, or an unverified domain, is shown as "Live".
- **F14.03 Legacy MLS/IDX returns.** A gated IDX item, wizard step or listing feed reappears on a site or in Our AI.
- **F14.04 Cross-agent publishing or spend.** An agent publishes, schedules or spends for another agent's site or account.
- **F14.05 Provider accepted is reported as published.** A post or ad is reported live before the provider confirms it, or a failed post shows no retry state.
- **F14.06 Duplicate effect on retry.** A retried publish, post or ad purchase runs twice.
- **F14.07 Unpublish leaks.** A rolled-back or archived site is still served from a cache or CDN.
- **F14.08 Lead spam or forged intake.** The public form is flooded, replayed, or used to inject a lead into another brokerage.
- **F14.09 Discriminatory targeting or unsupported claims.** A campaign targets protected traits, or copy states an unsourced market or financial claim.
- **F14.10 Portal over-shares.** A portal token reaches media or records outside its single-asset grant, or still works after revocation or expiry.

## 6. Acceptance criteria

- **AC14.01 The site lifecycle is mapped.** Every step from T14.01 links to its panel, route and test, and missing steps are listed. Evidence: lifecycle table in the slice report and in the ledger. Covers: T14.01.
- **AC14.02 Legacy IDX is gone from the growth surface; its data is untouched.** No IDX item, wizard step or `/api/mls/health` poll remains in Our AI or Studio, and stored revisions are unchanged. Evidence: component tests for `OurAITab` and `StudioTab`; row count of `hyperlocal_site_revisions` before and after. Covers: T14.02, F14.03.
- **AC14.03 Publishing follows one approved, immutable contract.** Any change after approval forces a new approval, and each transition is audited with its reviewer. Evidence: `backend/tests/test_publish_contract.py` (proposed) with tampered revision, hostname and budget cases. Covers: T14.03, F14.01.
- **AC14.04 Domains and rollback work.** A domain reaches `active` only after its DNS challenge passes, and a rollback restores the prior revision, which the public route then serves. Evidence: integration test with a stubbed resolver plus a route test after rollback and archive. Covers: T14.04, T14.05, F14.07.
- **AC14.05 Channels are truthful.** Each social and ad channel is either exercised in a provider sandbox with a stored receipt, or shows setup required. "Published" appears only after provider confirmation. Evidence: sandbox run log with receipt IDs; status-mapping tests. Covers: T14.07, T14.08, T14.16, F14.02, F14.05.
- **AC14.06 Effects run once.** A retried or duplicated publish, post or purchase produces one provider effect. Evidence: idempotency tests with injected crashes between provider acceptance and commit. Covers: F14.06, T14.08.
- **AC14.07 Generated content is sourced and compliant.** Every claim in generated copy cites a source record, and compliance checks block protected-trait targeting and unsourced claims. Evidence: an evaluation set of at least 20 drafts, scored for citation and compliance. Covers: T14.06, T14.09, F14.09.
- **AC14.08 Roles hold.** Same-brokerage cross-agent and cross-brokerage publish, schedule and spend are denied under the real application role. Evidence: real-PostgreSQL RLS tests plus Playwright role journeys on desktop and phone. Covers: T14.13, F14.04.
- **AC14.09 Public lead capture is safe.** A site form creates one lead in the right brokerage with consent recorded; replay, flooding and cross-tenant injection fail. Evidence: `backend/tests/test_site_lead_capture.py` (proposed). Covers: T14.15, F14.08.
- **AC14.10 The portal shares only what was granted.** Revoked, expired and out-of-scope portal requests are denied, and saved homes and alerts respect agent permissions. Evidence: portal API tests plus a Playwright client journey. Covers: T14.11, F14.10.
- **AC14.11 Search presence and media are measured, not promised.** Sitemaps, canonical tags and structured data validate; video jobs respect resolution and GPU budgets and need approval to publish. Evidence: SEO validation test output; `backend/tests/test_video_studio.py` extended with budget and approval cases. Covers: T14.10, T14.12.
- **AC14.12 Outcomes are counted, not drafts.** Metrics separate drafted, approved, accepted, published and qualified outcomes. Evidence: metric unit tests and one dashboard snapshot. Covers: T14.14.

## 7. Out of scope, owner decisions, category-specific rules

- **MLS, IDX and RESO are out of scope** (D-2026-10-12-01). The WordPress IDX plugin, IDX listing pages and IDX-governed listing metadata are not built or advertised. Do not invent a replacement.
- **Owner question:** should public sites show broker-owned listings from `/api/crm/listings` instead of an IDX feed (out of scope, D-2026-10-12-01)?
- **Owner question:** which social and ad networks are in launch scope, and who funds sandbox and partner accounts?
- **Owner question:** is the Customer Search App the responsive portal, a PWA, or both?
- **Fair housing and advertising law:** no targeting on protected characteristics; every ad carries the required broker disclosure.
- **No ranking or traffic promises** to customers.

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the IDX gating change with tests;
  - the publishing state machine and its tests;
  - domain verification, rollback and the public site route;
  - the per-channel status table (provider, auth, sandbox receipt or setup state);
  - the public lead-capture contract.
- **First slice:** T14.02 + T14.16 + AC14.02 (gate legacy IDX and stop false "Live" badges). Then T14.04 + T14.05 + AC14.04.
- **Ledger:** add L14-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
