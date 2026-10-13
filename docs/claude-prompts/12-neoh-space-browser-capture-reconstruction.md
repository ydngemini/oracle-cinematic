# 12 — Neoh Space browser capture, multiroom reconstruction and tours

> **Scope:** Let an agent capture a whole house in an ordinary mobile browser, reconstruct connected rooms, and publish a tour whose quality labels match the evidence (the four approved workstreams in the primary specification). This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 03 · **Unblocks:** 20 · **Ledger:** [L12](LEDGER.md#l12) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- **Primary specification:** [neoh-space-web-capture-claude.md](../neoh-space-web-capture-claude.md). Read all of it; its four workstreams are authoritative and this prompt coordinates them.
- Existing behaviour: `docs/neoh-space.md`, `docs/neoh-space-quality.md`, `docs/neoh-space-baselines.md`; audit finding [A03](../neoh-full-platform-source-audit-2026-10-08.md).
- Backend: `backend/recon_quality.py`, `backend/reconstruction_worker.py`, `backend/reconstruction_providers.py`, `backend/recon_pod_tools.py`, `backend/space_status.py`, `backend/scene_manifest.py`, `backend/tour_api.py`, `backend/capture_quality.py`, `backend/frame_selection.py`, `backend/capture_sidecars.py`, `backend/property_view_api.py`
- Schema: `backend/db/migrations/0073_capture_sessions.sql`; erasure in `backend/privacy_lifecycle.py`
- Frontend: `oracle-app/src/components/PropertyViewTab.jsx`, `oracle-app/src/components/CaptureSessionPanel.jsx`, `oracle-app/src/components/PropertyMediaUploader.jsx`, `oracle-app/src/components/PropertyTourViewer.tsx`, `oracle-app/src/components/WalkableSplatViewer.jsx`, `oracle-app/src/lib/tour/captureGuide.ts`, `oracle-app/src/lib/tour/deviceCapability.ts`, `oracle-app/src/lib/tour/renderQuality.ts`
- Tests: `backend/tests/test_recon_quality.py`, `backend/tests/test_recon_pod_tools.py`, `backend/tests/test_neoh_space_pipeline.py`, `backend/tests/test_pod_pipeline_dry_run.py`, `oracle-app/src/components/CaptureSessionPanel.test.jsx`
- Reconnaissance helper: `.claude/agents/spatial-stack-scout.md`

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **A run without held-out metrics is not blocked.** `recon_quality.py:79` `assess` adds `quality_unverified` and returns `blocked=False` for an empty report (`:83`) or one with no held-out PSNR/SSIM (`:101`).
- **The worker publishes every non-blocked verdict.** `reconstruction_worker.py:564` `_assess_quality` raises `QualityGateFailure` only when `verdict.blocked` (`:581`); the job then reaches `_publish` (`:977`). A resumed conversion starts from `assess(None)` (`:1134`).
- **The tour badge comes from asset existence, not quality.** `tour_api.py:41` `_TIER_BADGE` gives tier 3 "Full 3D Walkthrough"; `:335` labels every captured splat that way with `walkable: True` (`:334`). `tour_api.py` never reads `limitations`.
- **No certification state exists.** `space_status.py:73` (`LABELS[READY]`) says "Your space is ready to walk through." for every ready build. `quality_unverified` only adds a caveat (`:103`, `LIMITATION_LABELS`). `git grep -i certif` finds no 3D certification in backend or frontend.
- **Caveats reach the capture panel, not the buyer tour.** `CaptureSessionPanel.jsx:219` renders `job.caveats`. `PropertyTourViewer.tsx:113` declares `limitations` and never uses it.
- **Gate thresholds are provisional.** `recon_quality.py:15` says so. `:37` `BLOCK_PSNR_DB = 16.0`; `:44` `BLOCK_REGISTRATION = 0.20`; `:45` `PARTIAL_REGISTRATION = 0.60`.
- **Capture guidance is static and there is no in-browser camera capture.** `captureGuide.ts:12` `CAPTURE_STEPS` is five fixed steps. The only `getUserMedia` call in the frontend is audio-only (`PowerDialerPage.jsx:126`); media arrives as uploaded files.
- **The pod path is sequential and frame-capped.** `reconstruction_worker.py:347` `_VIDEO_SAMPLE_FPS` defaults to 2. `reconstruction_providers.py:1243` `POD_SEQUENTIAL_IMAGES = 150`; the matcher defaults to sequential (`:2019`) and `RECON_POD_STEPS` to 30000 (`:2059`).
- **Capture sessions are tenant-scoped.** `0073_capture_sessions.sql:24` creates `capture_sessions`, with FORCE RLS at `:80`. `reconstruction_worker.py:765` `_open_capture_session` writes it.
- **Spend has guards.** `tour_api.py:718` `enqueue_reconstruction` documents subscription, idempotency key, daily rebuild limit and bounded queues. `reconstruction_worker.py:1471` `MAX_ATTEMPTS` caps retries.
- **The best measurement is a benchmark room, not a house.** `docs/neoh-space-quality.md:60` (2026-10-06 run 2): held-out PSNR 28.82 dB, SSIM 0.890, LPIPS 0.086, COLMAP 150/150, on mip-NeRF 360 `room`. Browser numbers are headless Chrome on Iris 540 (`docs/neoh-space-baselines.md:44`); iOS Safari is "not measured" (`:64`).
- **Upload links and erasure exist.** `property_view_api.py:48` treats an upload link as a capability URL. `privacy_lifecycle.py:922` erases `splats/{tenant_id}/` objects.

## 3. Goal

An agent opens a property in mobile Safari or Android Chrome, captures connected rooms with live guidance, recovers from interruptions without data loss, and gets either a tour or a specific rescan list. Every quality label states the evidence behind it. A space with missing or failed evidence is never shown as verified or professional, and the photo fallback always works.

## 4. Engineering tasks

- **T12.01 Integration map.** Trace capture link and access token, media upload, storage key lifecycle, worker claims, GPU provider, stages, scene manifest, viewer load, publication, rollback and retention, with `path:line`. Mark which screens run real code and which exist only in the spec.
- **T12.02 Source artifact manifest.** Version tenant, property, capture and room IDs, original frame and video hashes, browser-provided camera metadata, derived poses, scale calibration status, transformed media, quality evidence, consent and the erasure path.
- **T12.03 Capture Studio, first slice (workstream B).** Launch from Property View; keep the existing upload and build paths working.
  - Camera permission from a user gesture with retry, rear-camera choice, recording format and bitrate negotiation.
  - A lightweight live quality worker (exposure, blur, overlap) separate from the preview, adapting to thermal and battery limits.
  - Room checkpoints, one accessible instruction at a time, durable original recording, and tests. No "XX% complete" from heuristics.
- **T12.04 Bounded frame budget.** Use bounded frame queues and backpressure. Keep a 30 fps preview (target, not measured) independent of AI cadence. Profile first frame and steady viewer FPS on real Safari devices under memory and thermal pressure.
- **T12.05 Network and session recovery.** Stage capture offline within permitted storage. Make chunk uploads resumable, integrity-checked and idempotent, with server receipts. Detect duplicate captures, recover stale sessions, and handle permission denial and tab backgrounding honestly.
- **T12.06 Registration and room connectivity (workstream A).** Measure feature tracks, camera registration, multiroom trajectories, doorway links, loop closure and room geometry evidence, per room. Report insufficient overlap and missing coverage. Never invent unseen rooms or metric scale. Produce one camera frame and a navigation graph.
- **T12.07 Room-aware keyframes.** Replace global sharpness thinning (150-image cap) with overlap-, room- and transition-aware selection inside a bounded GPU budget. Do not raise the global cap or the Gaussian count alone.
- **T12.08 Readiness engine (workstream D).** Implement the spec's three trust stages: `local_precheck`, `server_preflight`, `post_reconstruction`. Implement its per-room states. Only the backend may set a server-verified state.
- **T12.09 Certification policy (A03).** Add an explicit quality state (`pending`, `incomplete`, `degraded`, `rejected`, `verified`) separate from "ready to view". "Verified" requires measured held-out quality, registration, doorway connectivity and device tour performance. A missing metric is missing evidence (C6).
- **T12.10 Verify whether any consumer infers quality from a non-blocked verdict.** Check `_TIER_BADGE` and `walkable`, the property workspace, public sites, AI answers and the operator view. Bind each label to the verdict, or relabel it.
- **T12.11 Show caveats to buyers.** Render the scene's `limitations` in the tour viewer and property workspace, with the same labels as `LIMITATION_LABELS`.
- **T12.12 Photoreal reconstruction and mobile delivery (workstream C).** Experiment with held-out accuracy, colour and lighting stability, floaters, Gaussian pruning and densification, SOG compression, streaming LOD and progressive rendering in the PlayCanvas/gsplat stack. Keep the photo and 2D fallback and media privacy.
- **T12.13 Real-house benchmark.** Use several independent houses: different room counts, narrow halls, reflective surfaces, low light, moving people, textureless walls, occlusion and mixed camera quality. Use held-out images and explicit human review labels.
- **T12.14 GPU cost and isolation.** Cap spend per brokerage, set time budgets and priorities, and handle provider hangs and 429s. Add retry policy and cancellation, and release model resources. A runaway build must never block CRM or voice.
- **T12.15 Tours in the product.** Connect tours to the property workspace, private share links, brokerage access, buyer tour interactions and grounded property Q&A. No dimensions or AI answers from visual guesses. Property facts come from prompt 11's sources.
- **T12.16 Fallback and accessibility.** Test no-camera and unsupported devices, screen readers, reduced motion, orientation, touch navigation, lost WebGL and high-DPI screens. Photo-based property views stay usable.
- **T12.17 Automated regression.** Add per-stage unit and integration tests, synthetic trajectories, real-house regression evidence, and security tests for signed upload links. Also test upload resume, storage erasure, the GPU budget and viewer memory.
- **T12.18 External comparison.** Compare honestly with established tour products. Measure Neoh's possible distinct value (tour behaviour linked to CRM showings) without invasive tracking.

## 5. Failure and adversarial cases

- **F12.01 Unverified reads as verified.** A `quality_unverified`, `partial_registration` or stale run shows a "verified", "professional" or full-walkthrough claim.
- **F12.02 Client-only proof.** Browser heuristics alone move a room to a server-verified state.
- **F12.03 Disconnected house.** Two unconnected room groups are presented as one whole-house tour, or tap-to-move crosses an unobserved wall.
- **F12.04 Lost or phantom upload.** A tab switch, network change or full quota loses chunks, or the UI calls unsynced data saved.
- **F12.05 Inference backlog.** Analysis queues grow and the preview stutters or recording drops frames.
- **F12.06 Late upload.** New media arrives after a proof, and the old verdict silently stands.
- **F12.07 Runaway GPU.** A hung or retried job exceeds the brokerage budget or starves CRM and voice.
- **F12.08 Invented geometry.** An answer or label states dimensions, scale or unseen rooms that capture did not observe.
- **F12.09 Dead end.** A device with no camera or lost WebGL loses the property page.
- **F12.10 Cross-brokerage media.** A signed link, upload link or space asset reaches another brokerage.
- **F12.11 Native creep.** A change requires a native app, ARKit, RoomPlan or raw LiDAR.

## 6. Acceptance criteria

- **AC12.01 The integration map and manifest exist.** Each lifecycle step has `path:line` and a stated status, and the manifest schema is versioned. Evidence: committed map and schema doc, plus a schema test. Covers: T12.01, T12.02.
- **AC12.02 Three connected rooms are captured and resumed in a real browser.** Original media survive the tested interruptions. Evidence: real-device protocol: iPhone Safari and Android Chrome, three connected rooms, scripted interruptions (tab switch, network loss, low storage); chunk receipts compared with local hashes. Covers: T12.03, T12.05, F12.04, F12.11.
- **AC12.03 Preview stays smooth under load.** Preview holds 30 fps during a 10-minute capture (target, not measured) with no growing queue. Evidence: on-device trace of preview FPS, queue age and dropped samples, with device, browser and duration recorded. Covers: T12.04, F12.05.
- **AC12.04 Rooms connect only with evidence.** A disconnected two-room capture never becomes whole-house ready. Evidence: synthetic trajectory test and one real disconnected capture; A/B report of registered fraction, connected components and verified room edges against the current selector. Covers: T12.06, T12.07, F12.03.
- **AC12.05 Readiness labels follow evidence.** Client-only data never yields a server-verified state, and a late upload invalidates the old proof. Evidence: deterministic state-machine tests. Covers: T12.08, F12.02, F12.06.
- **AC12.06 No certification without evidence (closes A03).** Missing or invalid metrics, disconnected rooms, unregistered captures and stale records cannot produce "verified", "professional" or tier 3 "Full 3D Walkthrough". Evidence: tests on `assess`, the tier badge and every consumer found by T12.10. Covers: T12.09, T12.10, F12.01.
- **AC12.07 Buyers see the caveats.** Every limitation on a published space is visible in the tour and property workspace. Evidence: component test plus a phone-viewport Playwright check. Covers: T12.11.
- **AC12.08 Quality is reported per house.** Median and worst-case held-out PSNR, SSIM and LPIPS are reported per house and room. The pilot target is PSNR ≥ 27 dB, SSIM ≥ 0.88 and LPIPS ≤ 0.12 (target, not measured). Evidence: committed benchmark report with capture list, evaluation policy and human review labels. Covers: T12.12, T12.13, T12.18.
- **AC12.09 GPU spend is bounded and isolated.** A hung provider job is cancelled within budget, and CRM and voice latency are unaffected. Evidence: failure-injection test with a fake provider. Covers: T12.14, F12.07.
- **AC12.10 Tours are grounded and private.** Tour Q&A states no unmeasured dimension, and cross-brokerage access to links and assets is denied. Evidence: evaluation set of tour questions; real-PostgreSQL RLS test plus a signed-URL test. Covers: T12.15, F12.08, F12.10.
- **AC12.11 Fallbacks and regression hold.** No-camera, lost-WebGL and reduced-motion paths keep the property page usable, and per-stage tests run in CI. Evidence: Playwright fallback suite plus the CI run ID. Covers: T12.16, T12.17, F12.09.
- **AC12.12 One real property end to end (launch proof).** A real selected property is captured in an ordinary mobile browser, recovered, reconstructed, checked by source-backed metrics, navigated across physically connected rooms, and viewed with a working fallback. Evidence: dated run log with device, browser, capture ID, metrics and screenshots. Covers: T12.03, T12.06, T12.09, T12.13, T12.16.

## 7. Out of scope, owner decisions, category-specific rules

- **100% browser-based** (D-2026-10-08-01). No native app, Swift, ARKit, RoomPlan, direct or raw LiDAR access, or native bridge. Feature-detect browser APIs; never assume from the user agent.
- **Depth sensing.** A depth API that the browser itself exposes (for example WebXR depth sensing) may be used only as an optional enhancement. It must sit behind feature detection, with a complete no-depth path. No feature may require it. Whether to invest in it at all is an owner question.
- **One upload path.**
  - Capture media is uploaded by `PropertyViewTab.jsx:237` (`POST /api/crm/property-view/media`). `CaptureSessionPanel.jsx` builds the 3D space from that media.
  - `PropertyMediaUploader.jsx:73` is the separate lead and listing photo uploader.
  - Extend these. Do not add a competing uploader (spec §8).
- **All four workstreams are approved and first-class** (D-2026-10-08-02): registration and room connectivity; Capture Studio and guide; photoreal reconstruction and mobile tour; live readiness. Approval is not completion.
- **MLS is out of scope** (D-2026-10-12-01). Tours do not depend on listing feeds.
- **No quality claim without multi-home independent proof.** No "A+" or state-of-the-art label from one benchmark room.
- **Keep originals.** Browser inference uses downscaled copies. Never delete original media when derived splats or chunks are purged.
- **Thresholds stay provisional** until more measured runs; log each run in `docs/neoh-space-baselines.md`.
- **Owner question:** should a `quality_unverified` space publish as a draft visible only to the agent, or publish with caveats as today?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the integration map;
  - the versioned artifact manifest;
  - Capture Studio with tests;
  - the certification policy and consumer audit;
  - the readiness engine;
  - the real-house benchmark report (`docs/neoh-space-house-pilot.md`, proposed).
- **First slice:**
  - Read the primary specification and the current Property View, upload and Neoh Space code.
  - Produce a concise integration map (T12.01).
  - Deliver the browser Capture Studio with camera permission handling, a lightweight live quality worker, recording and upload recovery, and tests (T12.03, T12.05; AC12.02).
  - Don't chase quality by raising the Gaussian count alone. Prove connected real-house camera registration and whole-home coverage before asserting quality (T12.06, T12.07; AC12.04).
- **Then:** T12.09 + T12.10 + AC12.06, which closes A03.
- **Ledger:** add L12-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
