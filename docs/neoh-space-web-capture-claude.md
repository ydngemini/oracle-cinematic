# Neoh Space — Web-Only Intelligent Capture and A+ Reconstruction Plan

> Implementation brief for Claude Code. Updated 2026-10-08.
> **Status: design and acceptance criteria, NOT a claim of implemented functionality or proven A+ quality.**
> **Owner decision: 100% browser-based. No native iOS/Android app, Swift, ARKit, RoomPlan, private Apple APIs, or raw LiDAR dependence.**
> Existing Neoh Space lives **inside Property View** and the property tour; do not invent a separate product or standalone app.

## 0. Instructions to Claude

When asked to implement this initiative:

1. **Read** `docs/neoh-space.md`, `docs/neoh-space-quality.md`, `docs/neoh-space-baselines.md`, the relevant code, tests, and any newer architecture changes before editing. Those documents describe existing behavior; this file describes desired changes.
2. Use `.claude/agents/spatial-stack-scout.md` for read-only reconnaissance where helpful. Verify third-party package licenses and actual APIs before adding dependencies. No noncommercial-only code, weights, or datasets in production without rights.
3. Preserve existing tenant isolation, property-scoped media access, provenance, quality caveats, source media, atomic publish, idempotency, cost controls, and working fallback modes.
4. Ship **small vertical slices** with tests, browser/device evidence, and docs updated. Do not claim supported browsers, FPS, reconstruction quality, measured scale, or room completeness without evidence.
5. Do not fabricate room geometry, unseen surfaces, coverage percentages, camera poses, metric dimensions, reconstruction progress, benchmark runs, or production readiness.
6. Make no change to global product navigation unless required; the initial experience should launch from existing Property View.
7. Keep full-quality originals; browser inference uses downscaled **copies**. Never replace reconstruction inputs with tiny inference thumbnails. Account for video compression and the camera's actual achievable resolution.

## 1. Scope and product goal

**Goal:** An ordinary real-estate agent opens the Neoh website in mobile Safari/Chrome, selects a property, captures multiple connected rooms using the camera, receives useful live guidance, can pause and recover safely, uploads original media, and gets a credible photoreal 3D walkthrough or specific evidence-based rescan instructions.

**In scope:**
- Responsive mobile-first **Capture Studio** in existing React/Vite web app.
- Browser camera + recording + optional still-photo capture.
- Progressive quality analysis and adaptive keyframe selection.
- Scene overlap, room transitions, candidate coverage, confidence-aware prompts.
- Resilient local buffering, chunk uploads, verified server receipts, interrupted-session recovery.
- Preflight server registration analysis and granular repair guidance **before expensive GPU runs**, wherever affordable.
- Better whole-house reconstruction, geometry consistency, premium mobile tour delivery, and measurable QA.
- Privacy, consent, accessibility, progressive enhancement, and cost / battery / thermal protection.

**Out of scope:** native app, ARKit/RoomPlan/LiDAR direct access, bypassing OS security, guaranteed 3D from missing observations, whole-home measured dimensions without calibration, live full-quality neural reconstruction on phone, and equating research benchmarks with validated real listings.

## 2. Verified starting point (2026-10-08 code inspection)

Existing components/modules to extend, **not rewrite blindly**:
- `oracle-app/src/components/PropertyViewTab.jsx`: property-linked photo/video upload, source categorization, per-file flow, capture/build integration.
- `oracle-app/src/components/PropertyMediaUploader.jsx`: simpler photo media uploader, 25 MB photo guard, `crmUpload`.
- `oracle-app/src/components/CaptureSessionPanel.jsx`: static `CAPTURE_STEPS`, build start/resume, honest stage progress, idempotency/retries.
- `oracle-app/src/lib/tour/captureGuide.ts`: current **static** five-step guidance, not live intelligence.
- `oracle-app/src/components/PropertyTourViewer.tsx`: PlayCanvas `.sog` tour, walk/orbit, touch joystick, floor navigation and quality governor.
- `oracle-app/src/lib/tour/deviceCapability.ts` and `renderQuality.ts`: device assessment + tour renderer FPS governor; **this does not govern capture inference**.
- `backend/capture_quality.py`: server-side post-upload pixel quality gate (darkness, blur, duplicates, resolution, unusable captures).
- `backend/frame_selection.py`: existing sharpness-based temporal frame selection.
- `backend/reconstruction_worker.py`, `reconstruction_providers.py`, `recon_pod_tools.py`, `recon_quality.py`, `capture_sidecars.py`, `scene_manifest.py`, `tour_api.py`.
- `backend/db/migrations/0073_capture_sessions.sql`: capture attempts with metadata and media association (confirm actual current schema/routes before assuming active use).

Current documented pipeline: media → ffmpeg video frame extraction (about 2 fps) → pixel quality gate → frame thinning (150-image current pod path) → COLMAP sequential registration → gsplat training (30k steps) → `.sog` + sidecars → scene/quality → publish. Documented benchmark: 2026-10-06 mip-NeRF 360 `room`, a **real-photograph benchmark**, not a real property agent's full-house walkthrough: PSNR 28.82 dB, SSIM 0.890, LPIPS 0.086 (held out), with 150/150 registration. Other phone capture experiments registered far fewer frames (roughly 18–37%). Headless Chrome/Iris 540 viewer results are **not actual iPhone Safari tests**. Review latest baselines for changes.

A+ on several real houses is **not yet proven**. Registration, missing ceiling/room coverage, camera transition links, metric-scale uncertainty, and mobile playback are bigger blockers than inflating Gaussian counts.

## 3. User journeys and mobile UI design

### Entry and preparation
Property View → **Scan Property** → full-screen, mobile-safe capture workspace. Existing upload/build paths remain functional. Show property identity, floor, room label, selected mode, offline/storage status, and permission result. Request camera explicitly from a user gesture on HTTPS; explain reason and provide photo/video upload fallback.

Preflight: sufficient light, clear lens, phone orientation suggestion, main camera recommendation, record people/privacy warning, storage estimate where possible, and a short doorway/overlap demonstration. Never display unsupported sensor capability as available.

### Live scanning UI
- **Top bar:** address abbreviated; floor and room selector; visible pause/exit.
- **Center:** mostly unobstructed `<video playsInline muted>` view with modest framing reticle. Never render a fake spatial coverage mesh unless based on validated tracking.
- **Live state:** simple “good / slow down / low light / tracking uncertain / reconnect doorway” with clear reason.
- **One active prompt at a time**; no simultaneous alert flood. Use optional spoken cues and limited vibration when permitted; include mute and reduced-motion/accessibility options.
- **Bottom bar:** large record/pause control, selected mode, last confirmed saved segment, and “Review”.
- **Capture modes:** Guided Video (convenience and continuity), Precision Photos (maximal still resolution when supported; can fall back to OS photo picker). Record actual device settings. Note that preview/video resolution is **not equivalent** to full-resolution photos.
- **Room completion:** distinguish “enough promising evidence locally” from “server-verified”; do not award a room a final green state based solely on elapsed time or frame counts.
- **Rescan loop:** when a doorway is weak, guide agent to reframe the last known good view, then walk through the transition again. Never erase good segments.
- Handle voice accessibility, large tap targets, landscape/portrait, Safe Area insets, dynamic viewport units, hardware back gestures, permission denial, phone calls, app/tab backgrounding.

### Review/upload UI
Show room list with `not_started | capturing | provisional_good | needs_more | verification_pending | verified | failed`. Each label has a human explanation and evidence count/links. Give targeted actionable repairs (e.g., “Record another slow pass through the living-room/hallway doorway”).
Uploads show **bytes actually acknowledged** per segment or determinate percent only when measurable; reconstruction remains **stage-only** unless backend has real progress. Never replace an active/published scene without existing confirmation rules.

### Finished buyer tour
Simplify mobile navigation: tap-to-move toward **verified navigable positions** as default, joystick optional; room selector, clear floor chips, subtle mini-map when geometry is adequate, walk/orbit controls, accessible back-to-property and fallbacks. Tap-to-move must respect collision and room connectivity; do not allow teleporting through unobserved walls. Prefer lazy/streamed LOD assets when genuinely implemented and tested. Preserve honesty labels for uncertain scale.

## 4. Browser capture and progressive enhancement

**Feature-detect, never user-agent assume**:
- Camera `navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 30 } }, audio: false })`, HTTPS + permissions; inspect `MediaStreamTrack.getSettings()` for actual resolution/FPS; let users choose camera if multiple.
- `HTMLVideoElement.requestVideoFrameCallback` when present, `requestAnimationFrame` fallback, timestamp every sampled frame; callback scheduling must never run expensive AI synchronously.
- `MediaRecorder.isTypeSupported` and MIME negotiation; test pause/resume, chunk boundaries, interrupted streams and Safari format differences on real devices.
- OffscreenCanvas where supported; worker-based analysis with ImageBitmap/transferable buffers or a safe Canvas fallback. Do not block the camera preview or exceed memory budgets.
- WebAssembly SIMD where available; WebGPU only after feature detection, warmup and end-to-end benchmarking, with CPU/WASM fallback. Protect bundle size using dynamic imports and optional downloads.
- Optional motion APIs need gestures/permissions on some platforms. Motion data and browser orientation are **hints, not a reliable global 6-DoF pose or metric scale**.
- Storage: IndexedDB and/or OPFS for pending chunks and recovery metadata; storage is evictable and quota-limited. Track confirmed server receipts; never call unsynced files safely backed up.
- `visibilitychange`, `pagehide`, `devicechange`, track `ended`/mute and permission changes → clean pause/stop/recover. Wake Lock only if supported and held while foregrounded; no reliance on continuous background camera capture.
- Avoid simultaneous camera capture plus costly graphics rendering or multiple GPU models; prioritize stability.

## 5. Frame-rate and compute budgets (design targets, not measured results)

**Hard separation: preview/recording FPS ≠ AI inference FPS ≠ final tour rendering FPS.** Camera preview should stay smooth even if guidance is throttled.

| Pipeline | Balanced target | High-end target | Degraded target |
|---|---:|---:|---:|
| Preview and video | 30 fps requested | 30 fps, possibly 60 if proven useful | negotiate device-supported fps |
| Blur, luma, shake, near duplicates | 8–10 fps | 10–15 fps | 3–5 fps |
| Feature overlap / tracking | 5–8 fps | 8–10 fps | 2–4 fps |
| Heavy room/structure model | 1–2 fps | 2–3 fps | 0.5–1 fps or disabled |
| Advanced pose/depth model | 0.5–1 fps or cloud preflight | 1–2 fps only if validated | disabled |
| Retained high-quality keyframe candidates | 1–3 fps, content-adaptive | content-adaptive | content-adaptive |

Default architecture:
1. **Unblocked camera pipeline:** stream preview and source media independently of AI. Request 1080p/30; record actual settings; optional precision high-res still mode.
2. **Single in-flight analyzer per model:** sample most recent frame, use bounded 1-item queue, skip stale samples, never build a processing backlog.
3. **Two-resolution policy:** downsized frames (e.g., 320–640 px edge) for real-time quality/tracking; original pixels saved for downstream reconstruction.
4. **Budget governor:** measure rolling per-stage time, time-to-next-guidance, preview dropped-frame indicators (where observable), worker queue age, GPU/CPU inference memory failures, and long-task responsiveness. Reduce inference frequency, image dimensions or heavy-model usage first; don't sacrifice source recording.
5. **Hysteresis and cooldown:** debounce quality warnings, only change tiers after sustained pressure/recovery; do not flicker instructions every frame.
6. **Stop unnecessary work** when backgrounded, paused, out of battery according to permitted signals, camera ended, or upload constrained; gracefully resume with recovery hint.

Initial acceptance targets to **measure on real phones**: ~30 fps preview during a 10-minute capture, simple guidance every 100–200 ms where supported, complex useful prompts within ~1 s, no growing queues, no large thermal degradation, no recording interruptions or lost chunks. Targets are not guaranteed across all devices; publish capability-specific results and fallback behaviors.

Suggested output events:
```ts
type CaptureGuidanceEvent = {
  sessionId: string;
  roomId?: string;
  type: 'blur' | 'low_light' | 'duplicate' | 'overlap_weak' |
        'tracking_uncertain' | 'doorway_revisit' | 'coverage_hint';
  severity: 'info' | 'action' | 'stop';
  confidence: number; // calibrated [0,1], not a fake coverage percentage
  source: 'browser_pixel' | 'browser_tracking' | 'server_geometry';
  capturedAtMs: number;
  suggestedAction: string;
  evidenceFrameIds?: string[];
  algorithmVersion: string;
};
```
Treat `coverage_hint` as provisional until supported by geometric registration, and never claim evidence for a room not represented in actual source media.

## 6. Capture intelligence: staged algorithm

**P0 cheap local quality:** downscale and analyze per-frame luma/exposure, variance-of-Laplacian sharpness, inter-frame motion or approximate optical flow, similarity hashes, codec/frame drops, and scene changes. Normalize thresholds for resized frames and device characteristics; tune against real captures. Store reason codes, not arbitrary 0–100 “AI quality” scores.

**P1 useful keyframes:** retain sharp, overlapping yet nonduplicative viewpoints. Evaluate feature-match count/geometry, movement or parallax evidence and continuity. Use higher density at doorways, corners and stairs; maintain room-aware budgets. Do **not** conclude that a weak textureless wall is inherently a failed user capture. Camera switching must maintain intrinsic group identities, and source media remains available for server re-extraction.

**P1 room segments:** agent-confirmed room names and doorway markers; optional detection only as suggestions. Associate keyframes and media ranges with property/floor/room; each transition is a directed evidence edge. Infer route only with confidence and explicit uncertainty; do not invent room graph links.

**P2 geometry-aware guide:** use server preflight (or validated WASM visual odometry) for candidate camera poses, loop closure, room continuity and observed surface rays. An **actual geometrically grounded** coverage view can highlight missing ceilings/walls; raw temporal sampling alone cannot. Unreliable drift, shiny surfaces, mirrors/windows or motion blur must create caveats not fabricated geometry.

**Priority logic:** `camera_unavailable > unsaved_data > severe_blur > lost_overlap > low_light > missing_evidence > nice_to_have`; surface one actionable prompt, debounce transient noise, let the agent override advice. Privacy mask people/pets in server-side reconstruction when available; consider reflections/glass handling with careful validation.

## 7. Reconstruction upgrades for real-house A+ candidates

Sequence by measured bottleneck, not benchmark optics:
- Fix **camera registration success / scene connectivity** first. Current sequential COLMAP and 150-frame job budget are poor fits for whole houses. Experiment with room-aware grouping, stronger feature matching, overlap-aware keyframes, robust camera intrinsics, loop closure, pose graphs and verified learned geometry **only under commercial licenses**. Verify compatibility and compare against baseline.
- Allow sufficient per-room/transition image coverage while enforcing worker memory, GPU time and spend budgets. Do not merely raise a global cap and hope.
- Keep walls, gravity, room/floor graph, collision and scale as a **separate validated structural representation** from visual splats. No measured floor dimensions without calibrating scale from reliable evidence.
- Improve held-out detail with genuinely higher-resolution captures, lens/exposure normalization, carefully measured training configurations, floater/reflection management, adaptive densification. Do not claim unseen ceilings can be reconstructed faithfully.
- Investigate streamed spatial levels of detail or progressive `.sog` **after verifying actual PlayCanvas pipeline/API/license support** and mobile gains. Preserve existing `.sog` and legacy fallbacks.
- Make `quality_unverified` publish policy deliberate: fail closed for **professional certification** when evidence is absent, while maintaining appropriate accessible fallback/draft flows; do not silently break existing customers.
- Use regression datasets, per-room quality measurements, image comparisons, visual review, and phone thermal/performance profiles. Maintain provenance.

### Proof, not marketing

The 2026-10-06 benchmark (mip-NeRF 360 `room`) is a useful baseline but not independent proof for houses. Assemble a **minimum 10-house engineering pilot** with coverage of small/large, low-light, reflective, occupied, and multistory homes; expand to **20–30 or more** independent homes for broader reliability claims. Hold out unseen captured photos; assess novel-view fidelity with PSNR, SSIM, LPIPS under consistent camera/resolution/evaluation policy. Candidate pilot stretch targets: PSNR >= 27 dB, SSIM >= 0.88, LPIPS <= 0.12, ≥30 mobile tour FPS on specified devices, **all required rooms represented**, reliable transitions/collision and no severe visible artifacts. These are provisional internal goals, not universal state-of-the-art thresholds. Benchmark against contemporary competitors/methods before claiming SOTA, and assess per-house distributions not only best case.

## 8. Suggested modules and integration points

**Proposals, not existing files:**
- `oracle-app/src/components/CaptureStudio.jsx` + CSS module: full-screen mobile entry, preparation/capture/review/recovery.
- `oracle-app/src/hooks/useWebCamera.ts`: permission negotiation, actual settings, lifecycle and track recovery.
- `oracle-app/src/workers/captureAnalysis.worker.ts`: downscaled pixel checks, bounded queues, transferables and fallback.
- `oracle-app/src/lib/tour/keyframeSelector.ts`: pure frame scoring and candidates; deterministic unit tests.
- `oracle-app/src/lib/tour/coverageTracker.ts`: provisional room/doorway evidence graph and confidence.
- Extend `oracle-app/src/lib/tour/captureGuide.ts` to versioned, deterministic guidance policy with accessibility copy.
- Extend existing `PropertyViewTab.jsx` and `CaptureSessionPanel.jsx` to launch/cancel/resume capture and consume upload receipts/review. Do not duplicate competing uploader implementations.
- Extend backend upload APIs and `capture_sessions` via a **new forward-only migration**, scoped by tenancy, for room segments, recording chunks, actual media frame IDs, guidance version, and verification result. Confirm RLS, NOT NULL requirements, upload-size limits, CSRF/auth, signed storage, and per-property association before changes.
- Extend `backend/capture_quality.py` and related provider preflight for connected feature tracks / pose registration **before GPU leasing**, returning detailed yet safe customer reasons. Preserve provider/operator-only diagnostics boundary.
- Extend `PropertyTourViewer.tsx` with optional room nav / safe tap-to-move, validated floors and actual LOD capability; preserve orbit/walk, WebGL loss recovery and fallbacks.

### Suggested contracts (version before shipping)

```ts
type CaptureRoomSegment = {
  captureSessionId: string;
  propertyId: string;
  floorKey: string;
  roomKey: string; // user-confirmed unless source recorded as model_suggestion
  startTimestampMs: number;
  endTimestampMs?: number;
  sourceMediaIds: string[];
  keyframeIds: string[];
  transitionToRoomKey?: string;
  verification: 'not_checked' | 'provisional' | 'verified' | 'needs_rescan';
  findings: Array<{code: string; message: string; confidence?: number}>;
};

type UploadReceipt = {
  sessionId: string;
  chunkId: string;
  objectKey: string; // scoped opaque key, never user-selected arbitrary path
  bytesReceived: number;
  checksum: string;
  acceptedAt: string;
};
```

The specific API route shapes are TBD; match actual CRM patterns and tenant auth. **Use direct-to-object-storage multipart or a well-audited resumable upload protocol rather than retrying gigantic blobs**. Ensure content integrity, bounded retries/backoff, session token rotation, idempotent chunk acceptance, cleanup of orphans, and original preservation. Restore pending sessions without falsely claiming uploads succeeded. Consent, property ownership, content retention/deletion, bystander privacy and access controls require explicit review.

## 9. Recommended delivery phases and done criteria

### Phase P0: Browser Capture Studio (must work end-to-end)
- Mount entry in Property View; browser permission and meaningful fallback.
- Reliable recording/photo path, supported codec negotiation, actual FPS settings.
- Fast cheap live quality worker with graceful throttling; one stable user instruction.
- Multi-room manual labels and room/doorway metadata.
- Durable chunks and server-confirmed upload receipts; resume after tab interruption where possible.
- Feature, accessibility and mobile tests including denial/background/storage exhaustion.
- **Done:** a real agent can scan three connected rooms in mobile Safari/Chrome and safely upload source material with honest UI; existing build path still works.

### Phase P1: Smart capture and geometric repair
- Quality/keyframe selection; robust visual overlap checks and room graph evidence.
- Server preflight COLMAP feasibility/registration, room-specific failure reasons.
- Repair-only rescan workflow, avoiding re-uploading good segments.
- **Done:** real failed samples yield specific actionable recapture guidance, and preflight catches meaningful failures before pod spend.

### Phase P2: Photoreal + validated whole-house tour
- Improve registration across 1–multi floor real homes; improve lighting/detail/masking.
- Per-room independent image metrics, verified connected navigation, structural caveats, mobile LOD/streaming experiments.
- **Done:** measured property pilot, actual iOS Safari/Android Chrome metrics, reproducible quality report with failures and caveats.

### Phase P3: Professional quality certification
- Define draft vs verified publication policy, monitored regressions and human review.
- Expand pilot and compare with leading published methods/competitors; do not use “A+ / SOTA” as automatic quality labels.
- **Done:** audit trail (originals, pose tracks, model version, metrics, manual reviewer), objective acceptance reports per home.

## 10. Testing and observability checklist

**Frontend unit/component:** camera denied/granted, codec fallback, selected actual settings, repeated rapid taps, unmount cleanup, slow worker, stale-frame dropping, prompt precedence/debounce, duplicate/blur edge cases, room segment boundaries, upload retry, resumable metadata, hidden/visible, accessible operation, localization readiness.

**Integration:** valid property ownership, RLS, signed chunk upload and checksum, overlapping concurrent upload/scan sessions, session deletion, preflight refusal preserving originals, Build button idempotency and currently published scene continuity, no false floorplan/scale claims, media format compatibility, memory cleanup, no lost data on network change.

**Devices:** real iOS Safari across at least a non-LiDAR iPhone and a recent Pro model; Android Chrome midrange and flagship; desktop Chrome fallback; camera permission and recording MIME capability matrix. Test 10+ min sessions with phone warmth, visibility changes, incoming interruptions, low bandwidth, constrained storage and battery. Mobile emulators/headless browsers do **not** prove camera FPS, GPU/thermal or Safari compatibility.

**Telemetry:** requested/actual capture FPS; inference fps and p50/p95 latency by stage; dropped analysis samples; preview/recording disruptions; time to first useful prompt; keyframe acceptance rate; registration and connected-camera ratios; failed room links; preflight vs GPU rejection counts; bytes acknowledged; storage quota failures; mobile viewer steady FPS and asset load; GPU spend per successful home. Record hardware/browser/algorithm versions and permission states while respecting privacy. Avoid logging raw interior frames or sensitive room content in analytics.

**Test commands (verify repo state):**
- `cd oracle-app && npm run lint && npm run typecheck && npm run test && npm run build`
- Backend tests: invoke the focused `tests/test_recon_quality.py`, `tests/test_recon_pod_tools.py`, `tests/test_neoh_space_pipeline.py` and newly added capture tests via the project's supported runner.
- Record pass/fail and limitations; don't invent test runs.

## 11. Non-negotiable safety and product boundaries

- **Web only.** Do not propose native bridges as prerequisites. Camera access is consent-based; no bypasses.
- Never assert unrestricted iPhone LiDAR, ARKit, RoomPlan, protected APIs, or face-biometric access in a browser.
- No invented interiors, arbitrary scale as measured dimensions, unsupported wall navigation, or fake room completeness.
- Never delete original media just because derived splats or temporary chunks were purged.
- Protect real estate interiors and people: consent disclosures, tenancy, retention, encryption, least privilege, user/admin controls.
- Client browser cannot directly trust AI model output, upload receipts or arbitrary room IDs as authoritative. Backend validates everything.
- Avoid adding Three.js to the Oracle bundle; preserve PlayCanvas/gsplat and the existing architectural separation.
- **A good degraded capture experience beats fragile maximum-FPS inference.** Stable 30 fps preview and sound source media take priority.

## 12. First task Claude should execute

1. Audit the existing Property View upload/build lifecycle, `CaptureSessionPanel` and `captureGuide`; produce a file:line integration map.
2. Draft a route/state machine and protocol for `idle → permission → preflight → capture → paused → review → uploading → server_verification → submitted/repair`; include denied, hidden, offline, quota-full, camera-ended and unexpected-unmount paths.
3. Implement a minimal **working camera preview + P0 quality worker + graceful upload/recovery** slice in the existing UI (with tests), without claiming advanced coverage tracking yet.
4. Collect actual camera/inference/frame-rate measurements on phones before adjusting budgets or adding large browser AI models.
5. Only then build room/doorway evidence, server preflight, photoreal training improvements, and a 10-house study.

**Definition of success:** agent gets actionable guidance and reliable source capture; server can explain geometric failures before costly reconstruction; buyers get truthful, visually excellent tours on phones; objective house-level evidence supports every quality claim.
