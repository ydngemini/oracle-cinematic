# Claude Code — Oracle / Neoh Repository Guidance

> Repository agent entry point. Last updated 2026-10-08.
> Read this before modifying the project. This document is a navigation and safety guide; it does not claim that proposed features exist.

## Platform-wide source audit and remediation backlog (2026-10-08)

**Read:** [docs/neoh-full-platform-source-audit-2026-10-08.md](docs/neoh-full-platform-source-audit-2026-10-08.md) before undertaking cross-platform Neoh security, privacy, AI, billing, integrations, architecture, production-readiness or refactoring work.

This documents the snapshot audit at commit `3b63d5b`, including confirmed source-level concerns, severity, file-and-line evidence, acceptance criteria, ten full customer journeys, and an ordered remediation backlog. **It is an audit backlog, not proof of implementation or completion of a literal every-line review.** Verify findings against the current code and tests before acting.

**Platform-wide priority order:**
1. **P0:** protect plaintext customer PII/communication content, prove production readiness separately from staging CI, and distinguish AI/property/3D source failures or missing quality metrics from verified success.
2. **P1:** standardize staged/approved/delivered/billable events; test messaging-route races and provider outages; cover customer lifecycle journeys end-to-end; enforce per-tenant cost budgets and responsible model evaluation.
3. **P2:** classify shipping vs legacy code, audit each tracked file and its tests/migrations in bounded commit-pinned batches, and refactor large modules incrementally after safety coverage.

**Implementation rules:** Never claim an issue fixed without changing code and demonstrating tests. Preserve tenant isolation, data licensing, explicit human approvals, idempotency, honest degraded states, and retention/deletion. Use file:line and reproducible tests to validate each finding. Do not confuse passing GitHub Actions staging checks with production certification.

**Relationship to Neoh Space:** These are additional platform-wide priorities; continue to honor **all four** approved Neoh Space workstreams and their **100% browser-based** product decision below. Apply the dedicated spatial specification for capture/reconstruction changes.

## Current requested initiative: Neoh Space Web Capture

**Primary implementation specification:** [docs/neoh-space-web-capture-claude.md](docs/neoh-space-web-capture-claude.md)

When working on mobile property capture, AI guidance, browser camera performance, room/doorway tracking, photorealistic Gaussian Splatting reconstruction, and mobile tour delivery, **read the primary specification first** and follow its P0 → P1 → P2 → P3 development/validation sequence. Re-read the current code and tests before every change; the design is a proposal, not an implemented feature list.

**Product decision:** Neoh Space remains **100% web-based**, integrated with Property View and the property tour. Do **not** introduce a required native iOS/Android app, Swift, ARKit, RoomPlan, direct LiDAR access, or native bridge as part of this initiative.

## Four approved focus areas (2026-10-08)

Treat **all four** as first-class workstreams in [the Neoh Space implementation specification](docs/neoh-space-web-capture-claude.md#four-approved-priority-workstreams-2026-10-08):

1. **Camera registration + room connectivity:** recover real-house visual poses and doorway links reliably; do not invent missing geometry.
2. **Mobile browser Capture Studio + AI guide:** useful live feedback, resilient original capture and uploads, adaptive frame processing, room-aware rescans.
3. **Photoreal reconstruction + mobile tour:** controlled fidelity/training improvements, structurally valid navigation, efficient SOG delivery and actual-phone performance.
4. **Live Reconstruction Readiness Engine:** evidence-based per-room and doorway status; distinguish local hints, server geometric verification and post-build quality. Never show fake percentages or claim “verified” from local heuristics.

Approved related R&D experiments: commercially licensed learned pose/geometry, Gaussian densification, room-aware keyframe budgets and streaming/LOD. They require verification, benchmarking and licensing review before becoming production dependencies.

**Sequence:** instrument baseline → ship capture + provisional readiness → verify geometric connections and targeted recapture → fidelity/streaming work → field-test and certify. The four do not imply code already exists. All remain **100% web-only**.

## Existing sources of truth

- [docs/neoh-space.md](docs/neoh-space.md) — actual architecture, job stages, upload, publish and recovery rules.
- [docs/neoh-space-quality.md](docs/neoh-space-quality.md) — reconstruction quality measurements, provisional gate and known limits.
- [docs/neoh-space-baselines.md](docs/neoh-space-baselines.md) — measured GPU/browser baseline; **not** proof on real houses or iPhone Safari.
- [docs/neoh-space-web-capture-claude.md](docs/neoh-space-web-capture-claude.md) — implementation roadmap, UX contracts, algorithms, tests, FPS targets and real-house acceptance gates.
- [.claude/agents/spatial-stack-scout.md](.claude/agents/spatial-stack-scout.md) — evidence-based, read-only inspection of existing spatial code, dependencies and constraints.

## Hard implementation rules

1. **Inspect before coding.** Use file:line evidence for existing behavior. Do not assume proposed modules/APIs exist.
2. **No fabricated spatial truth.** Don't invent unseen house geometry, disconnected room transitions, accurate metric scale, tracking confidence, or percentage complete.
3. **No fake benchmarks.** Design targets are not measured outcomes. Actual mobile Safari/Chrome devices, independent held-out house imagery and documented failure cases are required for quality claims. The mip-NeRF room benchmark is not a full-house field validation.
4. **Capture reliability over maximum AI FPS.** Keep smooth browser camera preview/recording separate from background quality inference. Use bounded queues, adaptive cadence, actual-device measurements and safe fallbacks. Default targets and capability matrix live in the primary specification.
5. **Protect customers and budgets.** Preserve tenant authorization, property-scoped media, raw originals, explicit permissions, secure resumable uploads, quality/cost guards, idempotency, atomic publishing and working 2D/photo fallbacks. Track consent, retention and deletion.
6. **Respect established frontend and provider choices.** This is React/Vite + PlayCanvas/gsplat; do not add Three.js to the main Oracle bundle. Check dependency licenses, transitive weight and browser compatibility before adopting packages or pretrained models.
7. **Work in tested slices.** Prefer P0 camera + live pixel quality + durable upload/recovery; P1 overlap, room segments and server preflight; P2 verified whole-house fidelity/mobile delivery; P3 multi-house certification. Add unit, integration and device tests, keep docs current.
8. **Keep reconstruction progress honest.** A backend stage is not a real percentage. Use actual bytes acknowledged for upload progress and measured registration/reconstruction metrics for quality reporting.
9. **No unsupported sensor assumptions.** Ordinary web pages cannot freely access ARKit, RoomPlan or raw Apple LiDAR. Feature-detect browser APIs and always provide an accessible no-camera or no-accelerated-inference fallback.

## Starting task for Claude

Read the primary specification and current Property View/upload/Neoh Space code. Produce a concise integration map and a first deliverable implementing the **browser-based Capture Studio** with camera permission handling, a lightweight live quality worker, recording/upload recovery and tests. Don't attempt to “ship state of the art” by increasing Gaussian count alone; prove connected real-house camera registration and whole-home coverage before asserting quality.

For unrelated Oracle work, inspect its own docs and code; don't apply Neoh Space design targets indiscriminately.
