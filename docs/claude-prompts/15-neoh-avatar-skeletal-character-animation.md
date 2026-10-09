# Claude implementation prompt 15: Neoh mascot skeletal mesh, animation stability, awareness and speech-reactive behavior

> Repository `ydngemini/oracle-cinematic`; one brokerage, **20 authorized agents** and an explicitly modeled owner role; *every* entitled web page and advertised workflow is in the final release scope.
> This is an engineering implementation assignment. It does not establish any asset, service, API connection, third-party license, production deployment or benchmark as shipped.

## Your role and preflight

Act as principal engineer, domain specialist and security reviewer. Read root `CLAUDE.md`, this folder's `README.md` and relevant existing audit/implementation specs. Pin current commit and inspect true source paths/line references. Implement focused code changes and passing tests, not just aspirational prose. Preserve existing FastAPI/Postgres/React/Vite, tenant RLS, permission/approval mechanisms, provider contracts and browser-only capture.

**Likely sources to inspect:** oracle-app/src/neoh/NeohAvatar.jsx; oracle-app/src/neoh/NeohCharacter.jsx; oracle-app/src/neoh/NeohAvatarRive.jsx; oracle-app/src/neoh/avatarModel.js; oracle-app/src/neoh/eyeSystem.js; oracle-app/src/neoh/useNeohAvatarState.js; oracle-app/src/neoh/callPresence.js; oracle-app/src/neoh/NeohConversation.jsx; docs/neoh-avatar-rive-spec.md

## Implementation work

### Goal
Upgrade recognizable Neoh mouthless-visor robot from current layered SVG status character to believable, stable situationally aware skeletal character. Do not imply shipped GLB/GLTF/RIV assets exist; asset and runtime need real implementation.

### Phase 1: Baseline and contracts
1. Inspect SVG geometry, CSS timing, eye expressions, idle-gaze states, state priorities, current nine semantics (idle/listening/thinking/speaking/acting/success/needs_attention/error/disconnected), Rive specification and stub renderer. Preserve compatibility in NeohAvatar.jsx.
2. Define validated NeohAnimationEvent envelope: event ID, origin/app surface, action/session IDs, stage, timestamp, priority, validity, transient/permanent, verified source, optional amplitude and urgency. No LLM-generated arbitrary coordinates; backend/UI state drives behavior.
3. Separate operational state from demeanor/gesture and skeletal pose; map app stage (CRM, search, phone, Neoh Space, approval, success, failure, disconnect), speech state and meaningful real events into a behavior statechart with bounded transitions/hysteresis.
4. Model animation layers: idle breathing/weight shift, head/neck, gaze/blink, arms/hands, chest lighting, action badge, voice-reactive glow and optional facial blend shapes. Blend with crossfades, additive tracks and interruptibility; avoid resets/snaps, persistent success pose and looping repetitive exaggerated gestures.
5. Maintain semantic truth: speaking only during audio actually playing, success only on confirmed action receipts, thinking only for actual in-flight request, listening only when microphone or active call state confirms it. Distinguish provider timeout, awaiting approval and user cancellation.
6. Design GLB rig from existing silhouette: stable hierarchy root/pelvis/spine/neck/head, left/right clavicle/shoulder/elbow/wrist, hand gestures and optionally hips/knees/feet; skin weights for flexible joints and rigid shell segments; 20–30 controls as initial proposal, determined through animation proof.
7. Rig delivery: glTF 2.0 versioned asset, clips and naming manifest, bounds, materials, texture size/LOD, animation state maps, unit scale, collision/clipping quality. Character at icon scale uses SVG; expanded panel may use skeletal bust; larger experiences full body. Do not add a second heavy 3D engine if PlayCanvas meets requirements.
8. Rive path: existing Rive spec is a vector animator handoff and NeohAvatarRive.jsx is not deployed renderer. Decide 2D Rive for small surfaces or 3D rig based on measured quality, bundle size and browsers; support safe SVG fallback.
9. Speech synchronization: audio playback analyzer drives amplitude/lighting/gesture envelope; visemes only if model exposes valid timing and brand design warrants it (mouthless visor is acceptable). Barge-in interrupts output and transitions pose promptly; do not animate lip-sync from speculative text speed.
10. Scene awareness: Neoh reacts to selected property/client, active approvals, loading stages and user focus using verified UI data. Limit motion to context and user preference; do not claim camera/room awareness or inferred emotional state without sensors and consent.
11. Performance: lazy load GLB, budget mesh/poly/texture, frame rates on iPhone Safari/Android Chrome, adaptive LOD, pause offscreen/background, no React state updates per frame, delta-time clamps, context-loss recovery, reduced-motion still poses and robust accessibility.
12. Automated and manual test: transition collisions, rapid events, stale success, multiple concurrent actions, low-power mode, voice/audio timing, interruption, missing asset/WebGL, keyboard and reduced-motion behavior, visual regressions and tested real-device memory/FPS.

### Release acceptance
A coherent hero conversation sequence from idle -> listen -> reason -> tool -> approval -> speak -> outcome; graceful fallback, no clipped rig/foot sliding or jitter, verified app-context timing and stable frame-time. State-of-art visual quality claims require actual reviewed model and device measurements.

## Universal constraints, tests and deliverables

- Verify every associated page, workflow, API and worker; no false capability badges. Customer-required credentials/licensing may show a complete, truthful setup-required flow; genuinely missing advertised functionality must be implemented before calling an all-features release finished.
- Access rights: authorized assigned/shared agent records, team lead only in delegated scope, brokerage owner administration separate from platform admin; revalidate before external side effects and respect consent/PII.
- Evidence quality: distinguish source-backed facts, inferred predictions, provider timeouts, drafts, accepted jobs, delivered messages and actual success. Document source timestamps and failure states.
- Test units, schema/contracts, real PostgreSQL/RLS and migration chain, React UI/Playwright, concurrency and device scenarios as applicable. Use sandbox accounts for providers and synthetic data; never send to actual clients, spend or deploy live without authorized human approval.
- Measure actual latency/memory/cost, specify test conditions and p50/p95; mocked provider timings are not production results.
- Report source-linked baseline and gaps, architecture diagram or sequence, implementation plan with P0/P1/P2, file changes and migrations, tests actually executed, failure/recovery proofs, human/external blockers and next dependent slice. Keep category acceptance ledger updated; never claim “fully operational” from a mock alone.
