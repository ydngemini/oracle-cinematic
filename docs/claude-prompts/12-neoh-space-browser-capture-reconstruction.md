# Claude implementation prompt 12: Neoh Space mobile web capture, multiroom reconstruction, splats, tour and quality certification

> Repository `ydngemini/oracle-cinematic`; one brokerage, **20 authorized agents** and an explicitly modeled owner role; *every* entitled web page and advertised workflow is in the final release scope.
> This is an engineering implementation assignment. It does not establish any asset, service, API connection, third-party license, production deployment or benchmark as shipped.

## Your role and preflight

Act as principal engineer, domain specialist and security reviewer. Read root `CLAUDE.md`, this folder's `README.md` and relevant existing audit/implementation specs. Pin current commit and inspect true source paths/line references. Implement focused code changes and passing tests, not just aspirational prose. Preserve existing FastAPI/Postgres/React/Vite, tenant RLS, permission/approval mechanisms, provider contracts and browser-only capture.

**Likely sources to inspect:** docs/neoh-space-web-capture-claude.md; docs/neoh-space-quality.md; docs/neoh-space-baselines.md; backend/reconstruction_worker.py; backend/recon_quality.py; backend/scene_manifest.py; oracle-app/src/components/CaptureSessionPanel.jsx; oracle-app/src/components/PropertyTourViewer.tsx; oracle-app/src/components/WalkableSplatViewer.jsx

## Implementation work

### Non-negotiable product decision
Neoh Space is **100% web-based** for iPhone Safari/Android Chrome. No requirement for native app/Swift, direct ARKit, RoomPlan or raw iPhone LiDAR. Use browser-detectable capabilities only. Read the existing 379-line docs/neoh-space-web-capture-claude.md in full; its four approved workstreams are authoritative and this prompt coordinates them, not supersedes them.

### Four implementation streams
**A. Camera registration/whole-house topology:** measure feature matching/track quality, camera poses, multiroom trajectories, doorway connections, loop closure and room geometry evidence. Detect insufficient overlap and missing coverage; never invent unseen rooms/metric scale. Create per-room registration and doorway validation; secure consistent camera coordinate frames and navigation graph.
**B. Intelligent browser Capture Studio:** camera permission/retry, rear camera choice, bitrate/frame-budget adaptation, device thermals and battery constraints, real-time exposure/blur/coverage/overlap/feature feedback, room checkpoints, accessible guidance, efficient local worker (video preview separate from inference), durable original recording and resumable chunk uploads. No fake “XX% complete” from heuristics.
**C. Photoreal reconstruction/mobile delivery:** experiment with held-out-view accuracy, color/lighting stability, detail/floaters, splat geometry, Gaussian pruning/densification, image quality metrics, streaming LOD, SOG assets, compression and progressive rendering in existing PlayCanvas/gsplat stack. Preserve direct photo/2D fallback and correct media privacy.
**D. Live Readiness and Certification:** distinguish local provisional hints from server registration verification and post-reconstruction field metrics; quality states pending, verified, incomplete, degraded, rejected. Non-blocked grade != professional certification. Verify per-room and graph-level proof, including held-out real views and no disconnected transitions.

### Specific engineering tasks
1. Trace capture URL/access token, media upload, storage key lifecycle, worker claims, GPU provider, reconstruction stages, scene manifest, viewer load, property publication, rollback and retention. Identify which screens have real running code versus specs.
2. Establish source artifact manifest versioning: tenant/property/capture/room IDs, original frames/video hashes, camera metadata (only what browser provides), derived poses, scene scale calibration status, transformed media, quality evidence, license/consent and erasure path.
3. Use bounded frame queues and backpressure; evaluate target 30fps smooth preview independently of AI perception cadence. Profile on actual Safari devices with varied memory/thermal conditions; optimize first visual frame and stable viewer FPS.
4. Make network recovery robust: offline capture staging within permitted storage, resumable uploads, chunk integrity/idempotency, upload acknowledgement, duplicate capture detection and stale session recovery. Handle permission denial and browser tab backgrounding honestly.
5. Benchmark several independent real houses, different room counts, narrow halls, reflective surfaces, low light, moving people, textureless walls, occlusions and inconsistent camera quality. Use held-out images and explicit human review labels.
6. Harden GPU task cost caps by brokerage, time budget, job priority, provider hang/429, retry policy, cancellation and deallocation of model resources. A runaway reconstruction should never block CRM/voice.
7. Integrate tours with property workspace, private share links, brokerage access, MLS licensing, buyer tour interactions and factual grounded AI property Q&A; no fabricated dimensions or AI answer from visual guesses.
8. Test on unsupported/no-camera devices and ensure photo-based property experiences remain usable. Evaluate screen reader, reduced motion, phone orientation, touch navigation, lost WebGL and high-DPI screens.
9. Automate per-stage unit/integration tests, synthetic trajectory and real-house regression evidence, security for signed upload links, uploader resume, storage erasure, GPU budget and browser viewer memory.
10. Define external comparison carefully: Matterport-like tour features are established; unique Neoh value may be authorized bridge between property tour behavior and CRM showing workflows, measured without invasive tracking.

### Launch proof
A real selected property can be captured in ordinary mobile browser, recovered, reconstructed, verified by source-backed metrics, navigated across physically connected rooms and viewed efficiently with functioning fallback. No “A+” quality claim without multi-home independent proof.

## Universal constraints, tests and deliverables

- Verify every associated page, workflow, API and worker; no false capability badges. Customer-required credentials/licensing may show a complete, truthful setup-required flow; genuinely missing advertised functionality must be implemented before calling an all-features release finished.
- Access rights: authorized assigned/shared agent records, team lead only in delegated scope, brokerage owner administration separate from platform admin; revalidate before external side effects and respect consent/PII.
- Evidence quality: distinguish source-backed facts, inferred predictions, provider timeouts, drafts, accepted jobs, delivered messages and actual success. Document source timestamps and failure states.
- Test units, schema/contracts, real PostgreSQL/RLS and migration chain, React UI/Playwright, concurrency and device scenarios as applicable. Use sandbox accounts for providers and synthetic data; never send to actual clients, spend or deploy live without authorized human approval.
- Measure actual latency/memory/cost, specify test conditions and p50/p95; mocked provider timings are not production results.
- Report source-linked baseline and gaps, architecture diagram or sequence, implementation plan with P0/P1/P2, file changes and migrations, tests actually executed, failure/recovery proofs, human/external blockers and next dependent slice. Keep category acceptance ledger updated; never claim “fully operational” from a mock alone.
