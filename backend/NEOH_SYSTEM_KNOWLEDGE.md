# NEOH System Knowledge Base

## What NEOH Is
NEOH is an autonomous real-estate command center — a private operating copilot for real estate professionals. It combines a cinematic frontend (Vite React + raw WebGL, no UI libraries, glassmorphism) with a Python workflow engine, multi-agent system, and Azure Foundry AI.

## Core Application Tabs

### Listings
- Browse, search, and filter MLS-sourced property listings
- View detailed property data: tax records, assessed values, owner history
- Stage properties for tours and reconstructions

### Clients (CRM)
- Full CRM with client profiles, stages, lead scoring
- Client assignment, contact management, and task tracking
- Dossier management for motivated sellers and JV partners

### Communications
- Internal messaging and notification feed
- Real-time transcript of AI agent activity and system events
- Voice note logging and transcription

### Personal AI
- Private AI copilot for research, drafting, and deal analysis
- Memory Core: MAO threshold, target markets, interaction history
- Command approval: email, call, and calendar actions require review

### Contract Vault
- Encrypted contract documents with RLS-scoped tenant access
- Template registry for state-specific forms
- Draft workspace with approval workflow

### Deal Book
- Pipeline visualization across all states
- Deal stages: lead → contacted → negotiated → contracted → assigned/sold
- Firehose sync from BatchLeads/PropStream

### My Profile
- Brokerage and agent profile management
- License information, compliance settings
- AI autonomy settings and communication preferences

### Admin Ops (platform_admin only)
- Fleet-level billing summary and tenant management
- Surge telemetry for system health monitoring
- Audit trail with immutable ledger

## AI Capabilities

### Memory Core
- Per-operator risk profiles in PostgreSQL (not VRAM)
- JIT context injection: operator preferences appended to system prompt at call time
- Rolling memory summarization for long-running conversations
- Tenant-scoped via Postgres RLS

### Model Registry
- Validated LoRA and base model registration
- Agent-style training on consented examples
- State-level underwriting models (DE, PA, NJ, MD)

### Command Approval
- Email (SES), Call (Twilio), Calendar (Google OAuth)
- All commands gated behind approval workflow
- Safety: no outreach, financial, or legal actions without review

### Web Search (new)
- Tavily-powered web search for market data, news, public records
- Available to the AI for fact-grounded responses
- Limited to 3 calls per conversation

## Data Sources
- MLS (Multiple Listing Service) via real-time feed
- County Assessor records (parcel, tax, owner data)
- Court records via CourtListener
- Census Bureau demographics
- Municipal open data portals (Chicago, NYC, San Diego, Washoe)
- Regrid nationwide parcel facts
- GovInfo MCP for federal/state government records

## Security & Compliance

### Tenant Isolation
- PostgreSQL Row-Level Security (RLS) enforces tenant data boundaries
- hybrid model: private data walled per brokerage, MLS listings cross-tenant readable
- platform_admin has god-mode bypass

### Audit Ledger
- Immutable append-only audit trail with SHA-256 hash chain
- Categories: ACCESS_PII, EXPORT_LEAD, AI_PHONE_CALL, GENERATE_TOUR, STRIPE_WEBHOOK, AUTH_FAIL
- Tamper-evident verification via database function

### Encryption
- PII columns encrypted via pgcrypto AES-256
- Per-tenant encryption key derived and injected as GUC
- Contract documents encrypted at rest

### Policy
- Public-record data only (no covert profiling)
- Fair housing compliance enforced at input validation
- All legal/title/tax/zoning conclusions require professional review

## Architecture

### Frontend
- Vite React SPA (served by nginx on port 8080)
- raw WebGL spatial scenes (no Three.js)
- CSS Modules with glassmorphism design system
- WebSocket connection to backend for real-time updates

### Backend
- FastAPI with Uvicorn on port 8000
- asyncpg connection pool to PostgreSQL
- WebSocket hub for cross-replica push notifications
- Durable automation job system with worker leases

### Production (Azure)
- Azure Container Apps: neoh-api (backend) + neoh-web (SPA) + clamav (malware scanning)
- Azure PostgreSQL Flexible Server (private VNet, oracle_app_login role, FORCE RLS)
- Azure Container Registry: neoh120ea104.azurecr.io
- Azure Key Vault: neoh-kv-120ea104
- Azure Foundry: project neoh, agent neoh-kimi-k2-6, model Kimi-K2.6
