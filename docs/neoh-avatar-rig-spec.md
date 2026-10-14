# Neoh mascot: rig and delivery spec, and the Rive vs 3D evidence

> Category 15 (T15.06, T15.07, T15.08; AC15.05). Written 2026-10-14 against base `2fef2e5`.
> This is a **proposal plus measured evidence**. The rig does not exist, no `.riv` or `.glb` asset exists, and the Rive vs 3D choice is an **owner decision** (§5). Nothing here is a quality or device-performance claim.

## 1. What exists today (verified at `2fef2e5`)

| Fact | Evidence |
|---|---|
| Nine product states with fixed precedence | `oracle-app/src/neoh/avatarModel.js:17` `AVATAR_STATES`, `:52` `PRECEDENCE` |
| States reach the mascot only as validated events | `oracle-app/src/neoh/animationEvent.js` (envelope + statechart), `avatarFacts.js` (the only producers), L15-02 |
| Flat SVG mascot, the default and the fallback | `oracle-app/src/neoh/NeohCharacter.jsx`, `NeohAvatar.jsx` |
| Rive path is a stub that fails over to the SVG | `oracle-app/src/neoh/NeohAvatarRive.jsx:34` reports an error on mount; imported only when `VITE_NEOH_AVATAR_RIVE` is set (`NeohAvatar.jsx:32`) |
| Rive input contract written down | `oracle-app/src/neoh/riveInputs.js`, `docs/neoh-avatar-rive-spec.md` |
| Procedural 3D bust in PlayCanvas, no bones or glTF | `oracle-app/src/neoh/neohModel3d.js` `buildNeoh` |
| 3D corner shows the same state as the flat mascot | `NeohCorner3D.jsx` takes `state`/`actionType`/`attentionLevel`/`still`; pose from `cornerPose.js`, derived from `eyeSystem.js` |
| No rig asset, no Rive runtime | `git ls-files \| grep -iE '\.riv$\|\.glb$\|\.gltf$'` → 0; `grep -c rive oracle-app/package.json` → 0 |

## 2. Proposed rig (T15.06)

Built from the existing silhouette in `neohModel3d.js`. It's a bust: legs stay out at corner and panel scale, because the head carries the character.

**Hierarchy:**

```
root
└─ spine
   ├─ chest            (torso shell, emblem — rigid)
   │  ├─ clavicle_L ─ upperarm_L ─ forearm_L ─ hand_L
   │  └─ clavicle_R ─ upperarm_R ─ forearm_R ─ hand_R
   └─ neck
      └─ head          (shell, roof, visor, ear pods — rigid)
         ├─ eye_L      (scale/translate only: blink, expression, gaze)
         └─ eye_R
```

- **Joints:** 15 bones, plus 2 eye controls and about 6 expression morphs (the eight `EYE_EXPRESSIONS` minus neutral, with `acting` and `listening` sharing `focused`). That totals about 23 controls. **This is a starting target, not a measured need**. The count is settled by the animation proof in §4.
- **Skinning:** only the neck, shoulder and elbow joints are weighted. The shells (head, roof, visor, torso) are rigid segments. The charcoal joint spheres in today's model already hide the seams.
- **Visor:** keep it mouthless. Expression lives in the eyes and lights only. There are no visemes, and no lip-sync from guessed text timing (the brand rule in prompt §7).

## 3. Delivery format (T15.07)

| Item | Requirement |
|---|---|
| Format | glTF 2.0 binary (`.glb`), versioned file name `neoh-rig-vMAJOR.MINOR.glb`, served same-origin from `oracle-app/public/neoh/` |
| Loader | PlayCanvas (already a dependency, MIT, 2.21.3) through `app.assets.loadFromUrl(url, 'container')`, inside the existing lazy `NeohCorner3D` chunk. No second 3D engine, and no Three.js in the main bundle (CONTRACT C7.2) |
| Units, axes | The same units as `neohModel3d.js` (bust spans about y −1.7 to +2.2; head radius about 1.2), Y-up, facing +Z. `NeohCorner3D`'s camera framing assumes these |
| Materials | 4 or fewer: shell, shell shadow, visor, and one self-lit material named `neoh_glow` (eyes, ear rings, emblem) that the code recolours per state, as `cornerPose.js` does today |
| Textures | None preferred. At most one 512² visor texture |
| Budgets (proposed targets, unmeasured) | Bust 15k triangles or fewer; `.glb` 150 KB gzip or smaller; LOD0 only at corner size; LOD1 for an expanded panel bust |
| Clip manifest | A `neoh-rig-vX.json` beside the asset listing every clip, its length, whether it loops, and its state (table below). The loader refuses an asset whose manifest version or clip names don't match |
| Clipping check | No arm or hand intersects the torso, and no head part intersects the roof, at any frame of any clip. This is checked by a bounds script before acceptance, not by eye |

**State → clip map** (the nine states. A clip may only *express* a state the statechart already chose. It never chooses one):

| State | Clip(s) | Loop | Notes |
|---|---|---|---|
| idle | `neoh_idle_breathe` + additive `neoh_idle_gaze`, `neoh_blink` | yes | Close to motionless (the existing SVG rule) |
| listening | `neoh_listen_enter` → `neoh_listen_hold` | hold | Head toward the viewer. The glow follows the mic level only |
| thinking | `neoh_think_loop` | yes | Gaze up and inward, as `EYE_EXPRESSIONS.thinking` does |
| speaking | `neoh_speak_hold` + glow from playback amplitude | hold | Unreachable until a browser audio source exists (T15.14) |
| acting | `neoh_act_loop` (+ the action badge stays the SVG glyph) | yes | |
| success | `neoh_success_once` (no longer than `SUCCESS_HOLD_MS` = 1.4 s) | **no** | The validator refuses any success that claims to hold longer |
| needs_attention | `neoh_attend_loop` | yes | |
| error | `neoh_error_once` → hold | no | |
| disconnected | `neoh_offline_hold` | hold | Dim, head lowered |

Transitions crossfade at 150–250 ms (a design target). Reduced motion and hidden tabs hold the state's first pose (AC15.08).

## 4. Measurements for the Rive vs 3D choice (T15.08)

Each figure says how it was measured. **No device measurement exists.** iPhone Safari and Android Chrome frame-time, memory and battery figures need real hardware (AC15.07, open).

| | Rive `@rive-app/react-canvas` + `@rive-app/canvas` | Rive `@rive-app/canvas-lite` | 3D rig on PlayCanvas (existing engine) | SVG (today) |
|---|---|---|---|---|
| New dependency | Yes: `@rive-app/react-canvas` 4.36.0 → `@rive-app/canvas` 2.44.0 (pins), MIT | Yes: 2.44.2, MIT | **None**: `playcanvas` 2.21.3, MIT, already shipped lazily for tours and the corner | None |
| Runtime JS | `rive.js` 570,043 B raw / 124,659 B gzip, plus the React wrapper 21,951 / 5,959 | `rive.js` 489,769 / 109,204 | 0 new. The `NeohCorner3D` chunk is 7,723 B raw after this slice (6,107 before); the engine chunk is 2,325,752 B raw (already in the build, lazy) | 0 |
| WebAssembly | `rive.wasm` 2,057,365 B raw / 848,969 B gzip | `rive.wasm` 772,160 / 314,730 | None needed for primitives or a plain `.glb`. The engine contains optional wasm decoders (Draco/Basis) that a plain glTF doesn't need | — |
| Asset | None exists | None exists | None exists. The procedural bust is the interim model | In code |
| Production CSP (`oracle-app/nginx.conf:28`) | **Blocked as configured:** `script-src` has no `'wasm-unsafe-eval'`, so instantiating `rive.wasm` would be refused. Its default wasm URL is `https://unpkg.com/…` (found in `rive.js`), which `connect-src` blocks. Adoption needs a CSP change plus a self-hosted wasm | Same | No change known. **Not verified under the production CSP** | None |
| Browser requirement | WebAssembly + Canvas 2D | WebAssembly + Canvas 2D (lite omits features. See its README) | WebGL2 (PlayCanvas 2.21.3 `graphics-device-create.js` adds WEBGL2, then NULL) | Any |
| Quality review | Impossible: no asset | Impossible | The procedural bust was rendered for all nine states in **headless Chromium 145 (SwiftShader software GL, not a device)**. Every state rendered with its own hue and eye pose, with 0 runtime errors | Shipped |
| Frame time | Not measured | Not measured | Headless SwiftShader only, 120 rAF intervals per state at a 176×200 CSS px canvas, one run on a shared dev machine: p50 16.7–33.3 ms, p95 16.7–100.1 ms (the 16.7 ms floor is the 60 Hz rAF cadence). **Not a device result, and not a target** | — |

**How measured:** `npm pack` of each Rive package into an isolated scratch directory (not installed, not executed); `stat` for raw bytes; `gzip -9 | wc -c` for gzip. The PlayCanvas numbers come from `npm run build` at this slice. The headless run used a throwaway harness page that wasn't committed.

## 5. Owner decision needed

**Question:** for surfaces larger than the icon, should Neoh's animated character be a 2D Rive asset, or a rigged glTF bust rendered by PlayCanvas? And who authors it (in-house or a contracted animator), under what licence?

**Evidence summary:**
- Rive adds about 0.98 MB gzip of new runtime on first use (`canvas` wasm + JS + the React wrapper: 979,587 B). With `canvas-lite` that's 0.42 MB (423,934 B, without a React wrapper). It also needs a CSP change (`'wasm-unsafe-eval'`) and a self-hosted wasm.
- A glTF rig adds no runtime. It reuses the PlayCanvas engine the corner and tours already load, and needs no CSP change that we know of.
- Neither option has an asset, so neither can be quality-reviewed. Neither has been measured on a phone.

**Recommendation (for the owner to accept or reject):** use the 3D rig on PlayCanvas for the corner and an expanded panel bust, and keep the SVG for icon scale and as the universal fallback. This avoids a second runtime, a CSP relaxation and a new third-party dependency. Revisit Rive only if an animator delivers a `.riv` whose quality clearly beats the rig, *and* the owner accepts the CSP change.

**Until then:**
- the SVG stays the default everywhere;
- the procedural bust stays the 3D corner;
- no page depends on any asset.

**Licence notes:**
- all three packages measured are MIT;
- an animator-made asset needs its own written licence that assigns or grants perpetual use to the business. A tool's licence doesn't cover the asset.
