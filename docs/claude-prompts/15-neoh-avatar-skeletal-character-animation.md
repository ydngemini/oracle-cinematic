# 15 — Neoh mascot: rig, animation stability, awareness and speech-reactive behaviour

> **Scope:** Turn the Neoh mouthless-visor robot from a layered SVG status character into a stable, situation-aware animated character whose every state is driven by real product facts, with the SVG kept as the safe fallback. This is an engineering assignment, not a claim that anything is implemented, configured or verified.

**Depends on:** 02 · **Unblocks:** 20 · **Ledger:** [L15](LEDGER.md#l15) · **Contract:** [CONTRACT.md](CONTRACT.md) (binding)

## 1. Read first

- State and contracts: `oracle-app/src/neoh/avatarModel.js`, `oracle-app/src/neoh/useNeohAvatarState.js`, `oracle-app/src/neoh/riveInputs.js`, `oracle-app/src/neoh/actionLabels.js`, `oracle-app/src/neoh/callPresence.js`
- Renderers: `oracle-app/src/neoh/NeohAvatar.jsx`, `oracle-app/src/neoh/NeohCharacter.jsx`, `oracle-app/src/neoh/NeohAvatarRive.jsx`, `oracle-app/src/neoh/characterGeometry.js`, `oracle-app/src/neoh/eyeSystem.js`, `oracle-app/src/neoh/idleGaze.js`
- 3D corner: `oracle-app/src/neoh/NeohCorner.jsx`, `oracle-app/src/neoh/NeohCorner3D.jsx`, `oracle-app/src/neoh/neohModel3d.js`
- Call sites: `oracle-app/src/neoh/NeohSurface.jsx`, `oracle-app/src/neoh/NeohConversation.jsx`, `oracle-app/src/neoh/NeohSurfacePanel.jsx`
- Tests: `oracle-app/src/neoh/avatarModel.test.js`, `oracle-app/src/neoh/NeohAvatar.test.jsx`, `oracle-app/src/neoh/NeohCorner.test.jsx`, `oracle-app/src/neoh/useNeohAvatarState.test.jsx`, `oracle-app/src/neoh/eyeSystem.test.js`
- Spec: `docs/neoh-avatar-rive-spec.md` (animator handoff); bundle ceiling `oracle-app/bundle-budget.json`

## 2. Current verified state (at `745f2fa`, 2026-10-12)

- **Neoh has nine product states with fixed precedence.** `avatarModel.js:17` `AVATAR_STATES` lists idle, listening, thinking, speaking, acting, success, needs_attention, error, disconnected. `:52` `PRECEDENCE` orders error first and idle last, and `:109` `avatarState` picks the first true candidate.
- **A success pose holds for 1.4 s.** `avatarModel.js:67` `SUCCESS_HOLD_MS = 1_400`.
- **No caller supplies a "speaking" fact.** `useNeohAvatarState` accepts `speaking` and `audioLevel` (`useNeohAvatarState.js:39-40`), but `NeohSurface.jsx:59` and `NeohConversation.jsx:100` pass neither. The comment at `NeohSurface.jsx:64` says "there is no TTS".
- **The Rive renderer is a stub.** `NeohAvatar.jsx:31-33` lazy-imports `NeohAvatarRive.jsx` only when `VITE_NEOH_AVATAR_RIVE` is set. `NeohAvatarRive.jsx:32` `NeohAvatarRive` reports an error on mount and returns `null`; the real block is commented out.
- **No Rive, glTF or GLB asset exists.** `git ls-files | grep -iE '\.riv$|\.glb$|\.gltf$'` returns nothing. `oracle-app/package.json` has no `rive` or `@rive-app` dependency; it has `playcanvas` (`package.json:23`).
- **The Rive input contract is written down.** `riveInputs.js:8` requires a `NeohState` state machine with `state`, `level`, `actionType`, `attention` and `still`; `docs/neoh-avatar-rive-spec.md` is the animator handoff that copies it.
- **A procedural 3D Neoh already exists in PlayCanvas.** `neohModel3d.js:88` `buildNeoh` builds him from primitives (no bones, skin or glTF). `NeohCorner.jsx:32` lazy-loads `NeohCorner3D.jsx` after a 30 s dwell (`:81` `CORNER_3D_DWELL_MS`) on wide screens only (`:61`).
- **The 3D corner does not receive product state.** `NeohCorner3D.jsx:22` `NeohCorner3D` takes only `onFailure` and `onReady`.
- **The 3D corner already animates outside React and handles loss.** It updates per frame via `app.on('update')` (`NeohCorner3D.jsx:109`), pauses on `visibilitychange` (`:166`), and handles `webglcontextlost` (`:171`). `NeohCorner.jsx:136` falls back to the flat mascot on failure.
- **Reduced motion holds a still pose.** `NeohAvatar.jsx:89` sets `still` from `policy.reduced || hidden`; `NeohCorner.jsx:46` skips 3D under `prefers-reduced-motion`.
- **Eye expressions are mapped from state.** `eyeSystem.js:37` `EYE_EXPRESSIONS` and `:102` `eyeExpression`.

## 3. Goal

Neoh moves only because something real happened. An agent sees a coherent sequence from idle → listening → thinking → acting → awaiting approval → speaking → outcome, with no snaps, jitter, clipping or stale success. A rig or Rive asset is an optional enhancement over the SVG until it is built, reviewed and measured on real devices, and every failure path lands on the SVG.

## 4. Engineering tasks

- **T15.01 Baseline the current character.** Inspect SVG geometry, CSS timing, eye expressions, idle gaze, state precedence, the nine states, the Rive spec and the stub. Keep the `NeohAvatar.jsx` props and behaviour backward compatible.
- **T15.02 Define a validated `NeohAnimationEvent` envelope.** Fields: event ID, origin surface, action and session IDs, stage, timestamp, priority, validity window, transient or permanent, verified source, optional amplitude and urgency. Behaviour comes from backend and UI state; never from LLM-generated coordinates.
- **T15.03 Separate operational state from demeanour and pose.** Map app stage (CRM, search, phone, Neoh Space, approval, success, failure, disconnect), speech state and real events into a behaviour statechart with bounded transitions and hysteresis.
- **T15.04 Model animation layers.** Idle breathing and weight shift, head and neck, gaze and blink, arms and hands, chest lighting, action badge, voice-reactive glow, and optional blend shapes. Blend with crossfades, additive tracks and interruption; no snaps, no stuck success pose, no repetitive exaggerated loops.
- **T15.05 Keep semantic truth.** Speaking only while audio actually plays. Success only on a confirmed action receipt. Thinking only for an in-flight request. Listening only when the microphone or an active call confirms it. Distinguish provider timeout, awaiting approval and user cancellation.
- **T15.06 Design the rig from the existing silhouette.** Stable hierarchy (root, pelvis, spine, neck, head; left and right clavicle, shoulder, elbow, wrist; hand gestures; optional hips, knees, feet). Skin weights for flexible joints, rigid shell segments. Start from 20–30 controls as a proposal (target, not measured) and settle the count by an animation proof.
- **T15.07 Specify rig delivery.** Versioned glTF 2.0 asset, clip and naming manifest, bounds, materials, texture size and LOD, state maps, unit scale, collision and clipping checks. Icon scale stays SVG; an expanded panel may use a bust; larger views a full body. Use PlayCanvas, already a dependency, rather than a second 3D engine.
- **T15.08 Decide the Rive path.** Choose 2D Rive for small surfaces or the 3D rig, based on measured quality, bundle size and browser support. Keep the SVG fallback either way.
- **T15.09 Synchronize with speech.** An audio-playback analyser drives amplitude, lighting and gesture envelopes. Use visemes only if the voice source exposes valid timing and the design asks for it (the mouthless visor is acceptable). Barge-in interrupts output and changes pose promptly. Never animate from guessed text speed.
- **T15.10 Add scene awareness from verified UI data.** React to the selected property or client, active approvals, loading stages and user focus. Respect context and user preference. Claim no camera, room or emotional awareness without sensors and consent.
- **T15.11 Meet performance budgets.** Lazy-load assets; budget mesh, polygons and textures; measure frame rates on iPhone Safari and Android Chrome; adaptive LOD; pause offscreen and in the background; no React state updates per frame; clamp delta time; recover from context loss; still poses for reduced motion; keep accessibility.
- **T15.12 Test automatically and by hand.** Transition collisions, rapid events, stale success, concurrent actions, low-power mode, audio timing, interruption, missing asset or WebGL, keyboard and reduced motion, visual regressions, and real-device memory and FPS.
- **T15.13 Feed product state into the 3D corner.** Pass the avatar state, action type, attention and still flag to `NeohCorner3D` so the 3D and flat mascots never disagree.
- **T15.14 Verify whether a browser audio source for "speaking" exists.** Find what browser surface plays Neoh's voice, if any. If none, keep "speaking" unreachable in the browser and record it, rather than inventing a source.

## 5. Failure and adversarial cases

- **F15.01 Stale success.** A success pose plays without a receipt, or persists after the hold.
- **F15.02 Speaking without audio.** Neoh looks like it is talking while nothing plays, or keeps talking after barge-in.
- **F15.03 Event storm.** Rapid or out-of-order events cause flicker, snaps or a stuck state.
- **F15.04 Missing asset or WebGL.** The `.riv` or glTF fails to load, WebGL is absent, or the context is lost, and the mascot disappears or leaves a blank box.
- **F15.05 Bundle regression.** A runtime or asset lands in the main bundle and breaks `bundle-budget.json`.
- **F15.06 Frame drops on phones.** Frame time is unstable, or per-frame React updates cause jank, on a mid-range phone.
- **F15.07 Reduced motion ignored.** Loops still run when the viewer asks for reduced motion or the tab is hidden.
- **F15.08 Model-driven motion.** A model response sets pose or coordinates directly, or an unverified event drives a state.
- **F15.09 Two mascots disagree.** The 3D corner and the flat mascot show different states at once.

## 6. Acceptance criteria

- **AC15.01 Baseline and contracts are recorded.** The slice report lists the nine states, precedence, inputs and fallbacks with `path:line`, and `NeohAvatar` keeps its existing props. Evidence: baseline section plus the existing `NeohAvatar.test.jsx` passing unchanged. Covers: T15.01.
- **AC15.02 Only validated events drive behaviour.** Events missing a verified source, expired, or carrying arbitrary coordinates are rejected, and the statechart applies hysteresis. Evidence: `oracle-app/src/neoh/animationEvent.test.js` (proposed) with rejection, ordering and storm cases. Covers: T15.02, T15.03, F15.03, F15.08.
- **AC15.03 States tell the truth.** Success requires a receipt and clears after `SUCCESS_HOLD_MS`; speaking requires playing audio and stops on barge-in; listening requires mic or call state. Evidence: extended `avatarModel.test.js` and `useNeohAvatarState.test.jsx` cases. Covers: T15.05, T15.09, T15.14, F15.01, F15.02.
- **AC15.04 Layers blend without snaps.** A scripted hero sequence (idle → listening → thinking → acting → approval → speaking → outcome) shows no snaps, clipping, foot sliding or jitter. Evidence: a recorded sequence reviewed against a written checklist, plus visual-regression snapshots. Covers: T15.04, T15.10.
- **AC15.05 The rig decision is evidence-based.** A rig and delivery spec exist, and the Rive vs 3D choice cites measured bundle size, quality review and browser support. Evidence: `docs/neoh-avatar-rig-spec.md` (proposed) with the measurement table. Covers: T15.06, T15.07, T15.08.
- **AC15.06 Every failure falls back to the SVG.** Missing asset, missing WebGL, context loss and chunk failure each show the flat mascot. Evidence: component tests mocking each failure, as `NeohCorner.test.jsx` does for the 3D chunk. Covers: F15.04, T15.12.
- **AC15.07 Performance is measured on devices.** Frame time p50/p95, memory and asset bytes are recorded on iPhone Safari and Android Chrome with conditions, and the main bundle stays within `bundle-budget.json`. Evidence: device measurement protocol and results; `npm run bundle:check` output. Covers: T15.11, F15.05, F15.06.
- **AC15.08 Reduced motion and hidden tabs hold still.** No loop runs under reduced motion or a hidden tab, and keyboard focus is unaffected. Evidence: component tests with mocked `matchMedia` and `visibilitychange`. Covers: F15.07, T15.12.
- **AC15.09 The 3D corner shows the same state.** For each of the nine states, the 3D corner and the flat mascot agree. Evidence: `NeohCorner.test.jsx` extended with a state prop. Covers: T15.13, F15.09.

## 7. Out of scope, owner decisions, category-specific rules

- **The rig or Rive asset is optional until built and verified.** No page may depend on it; the SVG stays the default and the fallback.
- **No claim of "state-of-the-art" quality** without a reviewed asset and device measurements.
- **No Three.js in the main Oracle bundle** ([C7.2](CONTRACT.md#c7-reuse-the-architecture)); reuse PlayCanvas in a lazy chunk.
- **Brand:** keep the mouthless visor. No lip-sync from guessed timing.
- **Owner question:** who creates the rig or `.riv` asset (in-house or a contracted animator), and under what licence?

## 8. Deliverables, first slice, ledger update

- **Deliverables:**
  - the validated event envelope and statechart with tests;
  - state wiring into the 3D corner;
  - the rig and delivery spec with the Rive vs 3D decision;
  - the device performance report;
  - the fallback test set.
- **First slice:** T15.02 + T15.05 + T15.13 with AC15.02, AC15.03 and AC15.09 (truthful states on the existing renderers). Then T15.08 + AC15.05.
- **Ledger:** add L15-01 onward for each slice, per [C10](CONTRACT.md#c10-ledger-update-rule).
