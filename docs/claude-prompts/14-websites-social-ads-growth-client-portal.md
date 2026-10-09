# Claude implementation prompt 14: Marketing sites, social studio, advertising, IDX, SEO/AEO/GEO and client-facing experience

> Repository `ydngemini/oracle-cinematic`; one brokerage, **20 authorized agents** and an explicitly modeled owner role; *every* entitled web page and advertised workflow is in the final release scope.
> This is an engineering implementation assignment. It does not establish any asset, service, API connection, third-party license, production deployment or benchmark as shipped.

## Your role and preflight

Act as principal engineer, domain specialist and security reviewer. Read root `CLAUDE.md`, this folder's `README.md` and relevant existing audit/implementation specs. Pin current commit and inspect true source paths/line references. Implement focused code changes and passing tests, not just aspirational prose. Preserve existing FastAPI/Postgres/React/Vite, tenant RLS, permission/approval mechanisms, provider contracts and browser-only capture.

**Likely sources to inspect:** backend/sites_api.py; backend/lead_routing_api.py; oracle-app/src/components/OurAITab.jsx; oracle-app/src/components/StudioTab.jsx; oracle-app/src/components/SitePublishPanel.jsx; oracle-app/src/components/SitePreview.jsx; oracle-app/src/components/VideoStudioPanel.jsx; oracle-app/src/components/MarketplaceBrowse.jsx; oracle-app/src/components/ProviderDeliveryPage.jsx

## Implementation work

### Product requirement
“All pages available” includes their declared functions, not decorative listings. OurAITab currently marks Social Studio and ads as setup required, Customer Search App and packaged WordPress IDX plugin as not built, and SEO/AEO/GEO partially/incompletely connected. Scope these as real deliverables or clearly required external-provider setup. Never fake publication/ad account or market data.

### Detailed tasks
1. Audit full site-creation lifecycle: brokerage branding, agent attribution, chosen template, home/search/area pages, licensed IDX entitlement, SEO metadata, preview revision, approval, domain verification, publication, rollback and CRM lead intake. Confirm every corresponding panel/API actually works.
2. Unify publishing contract: Draft -> Validated -> Awaiting_Approval -> Approved -> Provider_Accepted -> Live/Published_Verified -> Degraded/Failed -> Archived, with immutable revision and reviewer. Do not republish live pages or change pricing/scope by unapproved upsert.
3. Implement brand-safe content generation from approved CRM/listing/market evidence; include property freshness, source citations, fair-housing and advertising-compliance checks, copyright/media license, required broker disclosure and explicit human review. No invented “market trends” or seller financial claims.
4. Social: support provider OAuth with least scopes, connection lifecycle, scheduled posts, attachments, previews per channel, short-video output where appropriate, publish receipts, moderation/correction, failed/retry states and analytics with provider timestamps. Do not claim an API integration before functioning live/sandbox verified endpoints.
5. Advertising: decide supported networks, validate partner requirements and account authorization, policy compliance, brokerage/agent spend limits, owner approvals, campaign preview, budget pacing, receipts, pause/cancel and attribution disclaimers. External paid-media purchases always separately authorized.
6. Build genuine campaign templates: listing blast, price drop, open house, just sold, ZIP farming, Google LSA as partner availability allows. Verify eligible audiences and opt-in/opt-out; never use protected housing traits to target/discriminate.
7. SEO/AEO/GEO: source-backed site maps, robots, canonical routes, structured listing metadata allowed by IDX rules, page performance, search appearance tests, answer-ready factual copy and crawl monitor. Analytics must not equate mention with verified traffic, and external ranking promises must be avoided.
8. Client Search App: define whether responsive customer web portal/PWA is the product (native not required); implement customer consent/auth, saved homes, alerts, scheduling, questions, MLS restrictions and CRM synchronization with explicit broker and agent permissions.
9. WordPress IDX plugin: if promised, design package lifecycle/versioning, hardened webhook/API auth, search/lead intake contract, MLS display compliance, security updates and install docs; no fake “install” link.
10. Video Studio and media: asset provenance, authorized input image rights, export/resolution limits, background rendering/gpu budgets, uploads, compression, graceful fallback, preview and publish approval.
11. Full role and workflow QA across Sites, Social, Homeowners, Automations and external preview pages; unauthorized agent cannot publish for another agent, create ad spend or expose unlicensed MLS data.
12. Instrument drafts created vs actually published, leads received, opt-ins, clicks with privacy consent, approved campaign cost and qualified outcomes; attribution is association unless causally established.

### Done
Each advertised publishing channel has either a fully tested integration or an explicitly documented setup/legal limitation; sites and media can be drafted, reviewed, published, reconciled and rolled back; client portal and IDX behavior validated; no fake marketing claims.

## Universal constraints, tests and deliverables

- Verify every associated page, workflow, API and worker; no false capability badges. Customer-required credentials/licensing may show a complete, truthful setup-required flow; genuinely missing advertised functionality must be implemented before calling an all-features release finished.
- Access rights: authorized assigned/shared agent records, team lead only in delegated scope, brokerage owner administration separate from platform admin; revalidate before external side effects and respect consent/PII.
- Evidence quality: distinguish source-backed facts, inferred predictions, provider timeouts, drafts, accepted jobs, delivered messages and actual success. Document source timestamps and failure states.
- Test units, schema/contracts, real PostgreSQL/RLS and migration chain, React UI/Playwright, concurrency and device scenarios as applicable. Use sandbox accounts for providers and synthetic data; never send to actual clients, spend or deploy live without authorized human approval.
- Measure actual latency/memory/cost, specify test conditions and p50/p95; mocked provider timings are not production results.
- Report source-linked baseline and gaps, architecture diagram or sequence, implementation plan with P0/P1/P2, file changes and migrations, tests actually executed, failure/recovery proofs, human/external blockers and next dependent slice. Keep category acceptance ledger updated; never claim “fully operational” from a mock alone.
