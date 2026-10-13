# Claude Code — Oracle / Neoh repository guidance

> The entry point for any agent working in this repository. Last updated 2026-10-12.
> This is a navigation and safety guide. It does not claim that any proposed feature exists.

## Start here

1. **Read [docs/claude-prompts/README.md](docs/claude-prompts/README.md).** It indexes the 20 category prompts, the build order and the start rule.
2. **Follow [CONTRACT.md](docs/claude-prompts/CONTRACT.md).** It is binding for every session: scope, tenancy, approvals, privacy, truth vocabulary, testing, the report format and banned claims.
3. **Status lives in [LEDGER.md](docs/claude-prompts/LEDGER.md) only.** Update it in the same commit as the change.
4. **Pick your category:**
   - **No task given:** start with **category 02** (all pages and mobile UI), plus a read-only review of 03, 17 and 18.
   - **Task given:** use the matching category prompt.

   One category per session.
5. **Never without explicit owner approval:** deploy to production, buy services, charge, or contact real customers. **Pushing to `main` deploys staging** (CONTRACT C4.4).

## Owner decisions (binding)

The full register is in [LEDGER.md § Owner decisions](docs/claude-prompts/LEDGER.md#owner-decisions).

- **D-2026-10-12-01: MLS is out of scope.** Do not build, re-enable or advertise MLS/IDX/RESO search, ingestion or display. Category 11 gates the legacy MLS code off. Delete no MLS data or migrations without owner approval.
- **D-2026-10-08-01: Neoh Space is 100% browser-based** (details below). Its four workstreams are approved (D-2026-10-08-02).
- **D-2026-10-12-02: pricing is a hypothesis, not a decision.**

## Platform source audit

Read [docs/neoh-full-platform-source-audit-2026-10-08.md](docs/neoh-full-platform-source-audit-2026-10-08.md) before cross-platform security, privacy, AI, billing, integration, architecture, production-readiness or refactoring work.
- It lists findings **A01–A10**, pinned at `3b63d5b`, with severity, file-and-line evidence, acceptance criteria, ten customer journeys and an ordered backlog. Each finding is owned by a category prompt.
- **Their current status is tracked in the [ledger's audit cross-reference](docs/claude-prompts/LEDGER.md#audit-cross-reference).**
- The audit is a backlog, not proof of implementation, and not a completed line-by-line review. Re-verify any finding at your SHA before acting on it (CONTRACT C1.5).

**Platform-wide priorities:**
1. **P0:**
   - Protect plaintext customer PII and communication content (A01).
   - Prove production readiness separately from staging CI (A04).
   - Report AI, property and 3D source failures, and missing quality metrics, as failures, never as success (A02, A03).
2. **P1:**
   - Standardize staged → approved → delivered → billable events (A05).
   - Test messaging-route races (A07) and provider outages.
   - Cover customer lifecycle journeys end to end.
   - Enforce per-tenant cost budgets and responsible model evaluation.
3. **P2:**
   - Classify shipping vs legacy code.
   - Audit tracked files with their tests and migrations in bounded, commit-pinned batches.
   - Refactor large modules incrementally, after safety coverage exists.

## Category 12 specification: Neoh Space web capture

Category 12 ([prompt](docs/claude-prompts/12-neoh-space-browser-capture-reconstruction.md)) covers:
- mobile property capture and AI guidance;
- browser camera performance;
- room and doorway tracking;
- photorealistic Gaussian-splat reconstruction;
- mobile tour delivery.

Its primary specification is [docs/neoh-space-web-capture-claude.md](docs/neoh-space-web-capture-claude.md). Follow its P0 → P1 → P2 → P3 sequence. Re-read the current code and tests before every change: the design is a proposal, not a feature list. The prompt's §8 names the first slice.

**Product decision (D-2026-10-08-01):** Neoh Space stays **100% web-based**, integrated with Property View and the property tour. Do **not** introduce a required native iOS/Android app, Swift, ARKit, RoomPlan, direct LiDAR access or a native bridge.

**The four approved workstreams (D-2026-10-08-02).** All four are first-class.
1. **Camera registration + room connectivity:** recover real-house camera poses and doorway links reliably. Do not invent missing geometry.
2. **Mobile browser Capture Studio + AI guide:** useful live feedback, resilient original capture and uploads, adaptive frame processing, room-aware rescans.
3. **Photoreal reconstruction + mobile tour:** controlled fidelity and training improvements, structurally valid navigation, efficient SOG delivery, real-phone performance.
4. **Live Reconstruction Readiness Engine:** evidence-based status per room and doorway. Distinguish local hints, server-side geometric verification and post-build quality. Never show fake percentages, and never claim "verified" from local heuristics.

**Approved R&D experiments:**
- commercially licensed learned pose/geometry;
- Gaussian densification;
- room-aware keyframe budgets;
- streaming/LOD.

Each needs verification, benchmarking and a licensing review before it becomes a production dependency.

**Sequence:** instrument the baseline → ship capture and provisional readiness → verify geometric connections and targeted recapture → fidelity and streaming work → field-test and certify.

**Sources of truth:**
- [docs/neoh-space.md](docs/neoh-space.md): actual architecture, job stages, upload, publish and recovery rules.
- [docs/neoh-space-quality.md](docs/neoh-space-quality.md): reconstruction quality measurements, the provisional gate and known limits.
- [docs/neoh-space-baselines.md](docs/neoh-space-baselines.md): measured GPU and browser baseline. **Not** proof on real houses or iPhone Safari.
- [docs/neoh-space-web-capture-claude.md](docs/neoh-space-web-capture-claude.md): roadmap, UX contracts, algorithms, tests, FPS targets and real-house acceptance gates.
- [.claude/agents/spatial-stack-scout.md](.claude/agents/spatial-stack-scout.md): evidence-based, read-only inspection of the existing spatial code, dependencies and constraints.

**Hard implementation rules (category 12):**
1. **Inspect before coding.** Use file:line evidence for existing behaviour. Do not assume proposed modules or APIs exist.
2. **No fabricated spatial truth.** Do not invent unseen geometry, disconnected room transitions, accurate metric scale, tracking confidence or percent-complete.
3. **No fake benchmarks.** Design targets are not measured outcomes. Quality claims need real mobile Safari/Chrome devices, independent held-out house imagery and documented failure cases. The mip-NeRF room benchmark is not a full-house field validation.
4. **Capture reliability over AI frame rate.** Keep the smooth camera preview and recording separate from background quality inference. Use bounded queues, adaptive cadence, real-device measurements and safe fallbacks. The default targets and the device capability matrix are in the primary specification.
5. **Protect customers and budgets.** Preserve:
   - tenant authorization and property-scoped media;
   - raw originals and explicit permissions;
   - secure resumable uploads;
   - quality and cost guards, idempotency and atomic publishing;
   - working 2D/photo fallbacks;
   - tracking of consent, retention and deletion.
6. **Keep the frontend and provider choices.** React/Vite + PlayCanvas/gsplat. Do not add Three.js to the main Oracle bundle. Check licences, transitive weight and browser support before adopting packages or pretrained models.
7. **Work in tested slices:**
   - P0: camera + live pixel quality + durable upload and recovery.
   - P1: overlap, room segments and server preflight.
   - P2: verified whole-house fidelity and mobile delivery.
   - P3: multi-house certification.
8. **Keep reconstruction progress honest.** A backend stage is not a percentage. Use bytes actually acknowledged for upload progress, and measured registration and reconstruction metrics for quality.
9. **No unsupported sensor assumptions.** Ordinary web pages cannot freely use ARKit, RoomPlan or raw Apple LiDAR. A depth API the browser itself exposes (such as WebXR depth sensing) is at most an optional, feature-detected enhancement; nothing may require it. Feature-detect browser APIs and always provide an accessible no-camera fallback, and a fallback for devices without accelerated inference.

Don't prove quality by increasing Gaussian count alone. Prove connected real-house camera registration and whole-home coverage before claiming quality.

## Other Oracle work

For work outside the 20 categories, inspect that area's own docs and code. Don't apply Neoh Space design targets to it. CONTRACT.md still applies.
