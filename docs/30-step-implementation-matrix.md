# Autonomous Real Estate Intelligence Platform — Implementation Matrix

This matrix is the acceptance record for the 30-step master plan. “Implemented”
means the repository contains the production path, policy boundary, persistence,
and tests appropriate to that item. It does not mean that a regulated conclusion
has been approved by a lawyer, title company, planner, broker, or other licensed
professional.

All external-data intelligence is constrained to public or licensed property-level
sources. Intelligence envelopes carry source, observation date, confidence, model
version, and observed/inferred status. Legal, financial, outreach, bidding, zoning,
title, and model-promotion actions retain approval gates.

| # | Status | Coded evidence | Verification evidence |
|---:|---|---|---|
| 1 | Implemented | `backend/platform_policy.py`, feature flags in `backend/config.py` and `infra/terraform/variables.tf` | Policy rejection and fair-housing tests in `backend/tests/test_intelligence_platform.py` |
| 2 | Implemented | `backend/db/migrations/0027_real_estate_intelligence_platform.sql` and `0028_google_oauth_states.sql` persist sources, signals, jobs, models, scores, parties, milestones, buyers, entity links, title, zoning, contracts, and OAuth state | Migration invariants exercised by `backend/tests/test_platform_completion.py`; runtime RLS script extended in `backend/tests/platform_rls_test.sql` |
| 3 | Implemented | Tenant RLS, provenance constraints, idempotency, queue indexes, encrypted credentials, append-only audit ledger in migrations 0016–0018 and 0027–0028; `backend/crypto.py` | Cross-tenant and append-only assertions in `platform_rls_test.sql`; authorization/security unit suite |
| 4 | Implemented | Durable PostgreSQL leases and retryable jobs in `backend/automation_jobs.py`; periodic coordinator in `backend/data_integrations/periodic.py` | Lease/idempotency tests in the backend suite |
| 5 | Implemented | Mandatory canonical `IntegrationCache` in `backend/data_integrations/cache.py`, wired through external read connectors and AVM | Fail-closed, deduplication, stale and cache tests in `test_platform_completion.py` and integration tests |
| 6 | Implemented | Incremental Chicago and NYC HPD harvesters in `backend/harvesters/il_chicago_violations.py`, `ny_hpd_violations.py`, `municipal.py`, and `base.py` | Fixture pagination, throttle retry, malformed-row, cursor, normalization, and cache replay test |
| 7 | Implemented | PLUTO zoning, FAR, lot/building area, land use, air-rights, coordinates, and dataset-version mapping in `backend/harvesters/ny_pluto.py` | Harvester normalization tests |
| 8 | Implemented | Checkpointed DE/PA/NJ/MD ingestion in `four_state_firehose.py` and `firehose.py`; ArcGIS/municipal adapters in `backend/data_integrations/state_gis/connectors/` | Checkpoint and schema-drift fixture coverage |
| 9 | Implemented | Harvest schedules, coverage, cursor age, cache savings, failures, circuit state, freshness, and controlled reruns in `backend/harvests_api.py` and `oracle-app/src/components/HarvestControl.jsx` | API authorization and harvester completion tests |
| 10 | Implemented | Public-input Pre-Distress evidence engine in `backend/intelligence_engine.py` and `/api/intelligence` routes | Coverage/calibration gating and exact-input tests; unvalidated models do not emit fabricated probabilities |
| 11 | Implemented | Source-cited highest-and-best-use math and review warnings in `backend/intelligence_engine.py` and `backend/intelligence_api.py` | Fixed zoning/buildable-area calculation tests |
| 12 | Implemented | Public-record deed/entity/address/officer/purchase graph in `backend/graph_engine.py` | Entity-link tests preserve unresolved links and never infer beneficial owners |
| 13 | Implemented | Preliminary recorder/tax/municipal/court title-and-lien findings in the intelligence engine/API and persisted `title_findings` | Tests distinguish preliminary findings, unresolved matches, and chain gaps from insured title work |
| 14 | Implemented | Seven-factor five-year micro-market forecast with confidence intervals and fair-housing review in `intelligence_engine.py` | Exact permits/crime/flood/census/sales/inventory/commercial-activity coverage test |
| 15 | Implemented | Probate/heir, tired-landlord, stalled-permit, and rezoning-agenda detectors in the intelligence engine/API | Identity-verification and outreach-approval policy tests |
| 16 | Implemented | Risk-aware `EMAIL`, `CALL`, and `CALENDAR` router in `backend/commands_api.py` and `command_providers.py`; Google Authorization Code + PKCE in migration 0028/API | Classification, approval, idempotency, OAuth state, token refresh, safe-return-path, and provider tests |
| 17 | Implemented | Consented transcription telemetry, counter-offer extraction, reproducible MAO thresholds, factual objection drafts, and call audit events in command/intelligence services | WebSocket authentication/tenant tests and negotiation calculation tests |
| 18 | Implemented | Prohibited-input and personalization boundary in `platform_policy.py`; no tremor analysis or covert personality profiling | Nested protected/private input rejection tests |
| 19 | Implemented | Opt-in agent style training, PII redaction, evaluation set, model card, checksum, rollback, and RunPod path in `backend/ml_forge/edge_forge/train_lora.py` and `backend/model_training.py` | Model registration and artifact validation tests |
| 20 | Implemented | Reproducible underwriting input/formula/comparable/assumption/risk traces in the intelligence engine/API; no exposed hidden reasoning | ARV, rehab, MAO, and trace-shape tests |
| 21 | Implemented | State/agent LoRA discovery, compatibility, rank/GPU limits, canary, fallback, hot swap, and telemetry in `local_vllm_adapter.py` and `models_api.py`; agent-facing status in `oracle-app/src/components/PersonalAITab.jsx` | Mock-vLLM state/agent adapter test in `test_platform_completion.py`; Personal AI browser navigation test |
| 22 | Implemented | Active contracts, response rate, 72-hour silence, party-aware milestones, deadlines, title/zoning risks, and alerts in `portfolio_api.py` and `PortfolioTab.jsx` | Backend query tests plus frontend lint/build |
| 23 | Implemented | Brokerage profiles, teams, roles, licenses, AI settings, Google status, training, and broker approval in `agent_profile.py`, `BrokerageOnboardingPanel.jsx`, and the dedicated `PersonalAITab.jsx` | Authorization/onboarding suite, frontend lint/build, and mobile/desktop Personal AI tab browser checks |
| 24 | Implemented | Revocable hashed read-only seller/JV dossier links, asset scopes, expiry, watermark, and access audit in `client_portal.py`, `ClientShared.jsx`, and `ClientDetailDrawer.jsx` | Portal scope/revocation/access tests |
| 25 | Implemented | Explicit permissions, broker-managed roles, protected overrides, reason capture, anomaly events, and immutable audit in `admin_ops.py` and `audit_middleware.py` | Authorization override and audit-ledger tests |
| 26 | Implemented | Signed-contract marketplace drafts, buyer-request/buy-box matching, verified-history ranking, and approval-gated truthful competition messaging in `contracts_api.py`, `marketplace_engine.py`, and `marketplace_api.py` | Marketplace matching, ranking, and approval tests |
| 27 | Implemented | Finite timeouts, bounded retries, health/freshness, normalized listing detail, buyer matching, and stale fallback in `mls_portal.py` and `data_integrations/listings_feed.py` | Connector timeout/cache/stale tests |
| 28 | Implemented | Characteristic inference separated into `property_inference.py`; rehab/photo providers in `reconstruction_providers.py`; tour variants and cited topography/viewshed in `spatial_intelligence_api.py` | Inference/reconstruction provider and spatial-intelligence tests |
| 29 | Implemented | Holographic grid, neural overlay, camera rig, hotspots, minimap, penthouse, and responsive HUD in `src/components/three/` and `src/components/tour/`; durable pixel workflow in `scripts/test-spatial-playwright.py` | TypeScript lint and optimized build pass; runtime test asserts desktop/mobile framing, controls, interaction, and nonblank PNG buffers |
| 30 | Implemented; live cutover separate | Assignment/buyer/seller forms, approved templates, defensive redlining, attorney gate in `synthetic_lawyer.py`; AES-256 private vault in `contract_vault.py`; migration-first ECS, alarms, circuit-breaker rollback, smoke scripts in `infra/` | Contract/vault tests, Terraform format/validate, rollback tests, and build checks. Live AWS deployment requires valid target-account credentials and is not asserted here. |

## Cache boundary

`IntegrationCache` is mandatory for external read connectors. External mutations
and credential-bearing actions—SES sends, Twilio calls, Google OAuth/token exchange,
RunPod training, and reconstruction job submission—are intentionally not replayed
from a response cache; they use idempotency keys, durable jobs, approval gates, and
audit records instead.

## Current verification record

- Backend: 153 tests passed; one Starlette deprecation warning.
- Python: complete backend byte-compilation passed.
- Frontends: root cinematic UI lint/build, Oracle application lint/build, and
  observability build passed.
- Infrastructure: `terraform fmt -check -recursive` and `terraform validate` passed.
- Source integrity: `git diff --check` passed.
- PostgreSQL runtime: all 28 migrations applied to an isolated PostgreSQL 16.14
  cluster unpacked in `/tmp`; `platform_rls_test.sql` passed, including OAuth-state
  tenant isolation and restricted updates. Local-only pgaudit/PostGIS availability
  notices were expected and their production preflights remain in infrastructure code.
- Spatial runtime: desktop/mobile home and tour processes passed against the
  prerendered build. Checks cover nonblank hero/neural/tour pixels, horizontal
  overflow, HUD/minimap framing, Dollhouse state, floor-plan dialog, and drag input.
  Headless browsers use a deterministic tour canvas because this host cannot create
  the premium tour's second R3F/SwiftShader context; normal browsers keep the full
  penthouse, hotspots, camera, shadows, and postprocessing path.
