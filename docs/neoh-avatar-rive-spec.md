# Neoh avatar — Rive animator handoff

Everything an animator needs to build `neoh-avatar.riv` without reading React.

Every number, name and enum value below is copied from source. The files of
record are:

| Thing | File |
|---|---|
| State machine inputs and numeric indices | `oracle-app/src/neoh/riveInputs.js` |
| Artboard sizes and head placement | `oracle-app/src/neoh/characterGeometry.js` |
| Layer names and vector geometry | `oracle-app/src/neoh/NeohCharacter.jsx` |
| Eye expressions | `oracle-app/src/neoh/eyeSystem.js` |
| Colour tokens, timings, per-state motion | `oracle-app/src/neoh/NeohAvatar.module.css` |
| Runtime wiring | `oracle-app/src/neoh/NeohAvatarRive.jsx`, `NeohAvatar.jsx` |

Reference art: `oracle-app/design/neoh-character-sheet.png`.

There is a working SVG implementation of this character shipping today
(`NeohCharacter.jsx`). The .riv replaces it. If the .riv fails to load, the
app falls back to the SVG permanently — so the two must read as the same
character, not two interpretations of one brief.

---

## 1. State machine

The asset must expose exactly one state machine named **`NeohState`**.

| Input | Type | Range | Meaning |
|---|---|---|---|
| `state` | Number | 0–8 | Which semantic state Neoh is in. See §1.1. |
| `level` | Number | 0..1 | Smoothed audio amplitude. Drives side lights and edge glow ONLY. |
| `actionType` | Number | 0–7 | Which kind of action is running, while `state` = 4. See §1.2. |
| `attention` | Number | 0..1 | How much the person is being asked for. |
| `still` | Boolean | — | `true` = hold the pose, run no loops. |

The names are literal. The runtime looks them up by string
(`useStateMachineInput(rive, 'NeohState', 'state')` and siblings); a rename
silently does nothing.

The indices are a wire format. **Append new values, never renumber existing
ones** — a shipped .riv that disagrees with the app animates the wrong state,
and there is no version handshake to catch it.

### 1.1 `state` — STATE_INDEX

| Value | Name | What is true |
|---|---|---|
| 0 | `idle` | Nothing is happening. |
| 1 | `listening` | The person's microphone is live. |
| 2 | `thinking` | A request is genuinely in flight. |
| 3 | `speaking` | Assistant audio is playing. |
| 4 | `acting` | A tool/action is executing. `actionType` says which. |
| 5 | `success` | A consequential action just confirmed. |
| 6 | `needs_attention` | Something needs the person. |
| 7 | `error` | A genuine failure. |
| 8 | `disconnected` | No channel. |

### 1.2 `actionType` — ACTION_INDEX

| Value | Name |
|---|---|
| 0 | `generic` |
| 1 | `call` |
| 2 | `message` |
| 3 | `calendar` |
| 4 | `property` |
| 5 | `search` |
| 6 | `save` |
| 7 | `share` |

Unknown inputs are coerced to `0` / `generic` by the app before they reach
the asset, so the asset never has to handle an out-of-range value — but it
should still degrade to the `generic` behaviour rather than freeze.

**The action badge is not yours to draw.** The app overlays a small
circular glyph badge in the DOM (lucide icons at 10px, in
`ACTION_GLYPHS`, `NeohAvatar.jsx`) on top of the artboard while
`state = 4`. `actionType` is exposed so the *character* can react — a
posture, a light colour — not so you can redraw the badge. See §7 for the
corner the badge occupies.

---

## 2. Artboards

Three artboards, one per variant. Copied from `VARIANT_GEOMETRY`.

| Artboard | Size | Head transform | Used for |
|---|---|---|---|
| `head` | 32 × 32 | none — head drawn at 1:1 in artboard space | pills, inputs, status |
| `bust` | 40 × 44 | `translate(6.88 1) scale(0.82)` | voice and conversation |
| `full` | 40 × 72 | `translate(8 1) scale(0.75)` | onboarding, empty states, a finished setup |

The head is **always authored in its own 32 × 32 space** and placed into the
larger artboards by that transform. Author the head once and instance it into
`bust` and `full`; do not redraw it at three scales. Every fix to the head
must land in all three for free — that is the entire reason for the
transform.

The app sizes by **height**. Width follows the artboard ratio
(`bust` = height × 40/44, `full` = height × 40/72), so artboard aspect ratios
are load-bearing and must not change.

---

## 3. Layers

Name the nodes as below. These are the class names on the SVG groups in
`NeohCharacter.jsx`; matching them keeps the two renderers reviewable side by
side.

Every part that has to move is its own node with its own transform origin. A
single merged path cannot tilt the head without dragging the shoulders, and
cannot pulse the chest light without pulsing the torso around it.

```
head                    head group — pivots as a head
  shell                 the roof-shaped skull
  roofEdge              stroked polyline along the roofline (separate on purpose)
  visor                 the dark face panel
  sideLeft              left circular module
    sideRing            ring, stroke
    sideCore            filled core
  sideRight             right circular module
    sideRing
    sideCore
  eyes                  the eye pair group — rotated for tilt
body                    bust/full only — grouping node
  neck
  torso
  emblem                home-shaped chest mark
  armLeft / armRight    each: joint, limb, hand
  legs                  full only — two limbs, two feet
```

`body` and `legs` are grouping nodes with no material of their own. `eyes`
and `emblem` are groups so they can be transformed without touching the
shapes inside them.

### 3.1 Head geometry (32 × 32 space)

| Node | Geometry |
|---|---|
| `shell` | `M16 2.6 4.6 11.2v12.2A4.2 4.2 0 0 0 8.8 27.6h14.4a4.2 4.2 0 0 0 4.2-4.2V11.2Z` |
| `roofEdge` | `M4.6 11.2 16 2.6l11.4 8.6`, stroke 1.2, round cap and join, no fill |
| `visor` | `M16 6.4 8 12.4v9.8a2.4 2.4 0 0 0 2.4 2.4h11.2a2.4 2.4 0 0 0 2.4-2.4v-9.8Z` |
| `sideLeft` | centre `cx 3.4, cy 16.4` |
| `sideRight` | centre `cx 28.6, cy 16.4` |
| `sideRing` | `r 2.6`, stroke 1, no fill |
| `sideCore` | `r 1.15`, filled |

`roofEdge` is a separate stroked polyline rather than the shell's own stroke
so the roofline can light and travel without the silhouette changing weight.
Keep it separate in the .riv for the same reason.

The `head` group's pivot is **50% horizontally, 82% vertically of its own
bounding box** — the neck, so a tilt reads as a head turning rather than a
sign swinging.

### 3.2 Bust body (40 × 44 artboard)

| Node | Geometry |
|---|---|
| `neck` | rect `x 17, y 21.5, w 6, h 4.5, rx 2.2` |
| `torso` | `M20 24.5c-6.6 0-11.4 4.3-11.4 10.2V44h22.8v-9.3c0-5.9-4.8-10.2-11.4-10.2Z` |
| `emblem` | translated to `(20, 31.4)`, scale `0.42` |
| `armLeft` | pivot `x 8.2, y 31.5`, length `9` |
| `armRight` | pivot `x 31.8, y 31.5`, length `9` |

### 3.3 Full body (40 × 72 artboard)

| Node | Geometry |
|---|---|
| `neck` | rect `x 17.6, y 16.4, w 4.8, h 3.6, rx 1.8` |
| `torso` | `M20 19c-5.4 0-9.3 3.5-9.3 8.4v13.2c0 2.6 2 4.4 4.6 4.4h9.4c2.6 0 4.6-1.8 4.6-4.4V27.4c0-4.9-3.9-8.4-9.3-8.4Z` |
| `emblem` | translated to `(20, 29.6)`, scale `0.42` |
| `armLeft` | pivot `x 10.1, y 25.6`, length `11` |
| `armRight` | pivot `x 29.9, y 25.6`, length `11` |
| `legs` | two rects `x 14.3` and `x 21.3`, `y 44.4, w 4.4, h 17, rx 2.2` |
| feet | ellipses `cx 16.5` and `cx 23.5`, `cy 63.4, rx 3.5, ry 2.5` |

Arm construction, both variants: a joint circle `r 2.5` at the pivot; a limb
rect `width 3.8, rx 1.9` hanging from it, rotated `-7°` on the left and `+7°`
on the right about the pivot; a hand circle `r 2.2` at
`(pivot.x ± 0.9, pivot.y + length + 0.6)`.

Arms pivot at **top centre** of their own bounding box.

The legs are two rounded columns and two feet. Simple on purpose — this
character is never asked for a walk cycle.

### 3.4 Chest emblem

A home mark, drawn around a local origin so the variants can place it by
translate + scale:

```
M0 0-4.4 3.6V9a1.3 1.3 0 0 0 1.3 1.3h6.2A1.3 1.3 0 0 0 4.4 9V3.6Z
```

Pivot: centre of its own bounding box. Base opacity `0.9`, with a soft bloom
(`--neoh-light-soft`, ~2px).

---

## 4. Eye expressions

Eight expressions. Not twenty. Each answers a question the product actually
asks ("is it listening?", "did that work?"). An expression nobody can name
from a screenshot is decoration, and decoration on a face reads as an emoji.

All values are in the head's own 32 × 32 space. The pair is centred at
`x = 16`; `spread` is **half** the distance between the two eye centres, so
`cx = [16 - spread + gazeX, 16 + spread + gazeX]` and `cy = y + gazeY`.

| Name | rx | ry | y | spread | tilt | gazeX | gazeY | arc | glow |
|---|---|---|---|---|---|---|---|---|---|
| `neutral` | 2.4 | 2.6 | 14.6 | 4 | 0 | 0 | 0 | no | 0.55 |
| `focused` | 2.5 | 2.9 | 14.3 | 4.05 | 0 | 0 | -0.1 | no | 0.8 |
| `thinking` | 2.1 | 2.4 | 13.8 | 3.7 | -8 | 0.35 | -0.85 | no | 0.7 |
| `speaking` | 2.4 | 2.55 | 14.4 | 4 | 0 | 0 | 0 | no | 0.95 |
| `happy` | 2.6 | 1.2 | 14.7 | 4 | 0 | 0 | 0 | **yes** | 0.9 |
| `attention` | 2.7 | 3.0 | 14.0 | 4.1 | 0 | 0 | 0 | no | 1 |
| `error` | 2.3 | 1.7 | 15.0 | 4 | 0 | 0 | 0 | no | 0.5 |
| `offline` | 2.2 | 1.3 | 15.1 | 4 | 0 | 0 | 0 | no | 0.15 |

Column meanings, from `eyeSystem.js`:

- `rx` / `ry` — ellipse radii. A small `ry` is a narrowed eye.
- `y` — vertical centre of the pair.
- `tilt` — degrees, rotated about the pair's centre at `(16, 15)`.
- `gazeX` / `gazeY` — look offset applied to position, not shape. Negative
  `gazeY` is up. Keep position and shape on separate animatable properties;
  the SVG renderer already splits them and Rive will want the same split.
- `arc` — draw as a stroked happy arc instead of a filled ellipse.
- `glow` — 0..1, how strongly the eye light blooms.

Arc construction (`happy` only): for each eye, from
`(cx - rx, cy + 0.6)`, a quadratic `q rx -2.4 (rx × 2) 0`. Stroke 1.5, round
caps, no fill.

### 4.1 State → expression

Several states share one expression. That is intentional: the action glyph
and the side lights already distinguish "hearing you" from "doing the thing",
so the face does not need to. Reusing an expression is cheaper to maintain
than inventing a nuance nobody can name.

| `state` | Expression |
|---|---|
| `idle` | `neutral` |
| `listening` | `focused` |
| `thinking` | `thinking` |
| `speaking` | `speaking` |
| `acting` | `focused` |
| `success` | `happy` |
| `needs_attention` | `attention` |
| `error` | `error` |
| `disconnected` | `offline` |

### 4.2 Idle micro-gaze

On `bust` and `full` only, while `state = idle` and `still` is false, the app
applies a rare autonomous look-away on top of the expression's own gaze
(`idleGaze.js`):

| Property | Value |
|---|---|
| Rest before a glance | 6000 ms + up to 9000 ms random jitter |
| Glance duration | 1100 ms, then back to centre |
| `gazeX` offset | random in -0.55 .. +0.55 |
| `gazeY` offset | random in -0.35 .. +0.35 |

This is punctuation, not motion, and it is deliberately **not** pointer
tracking — eyes that follow the cursor read as surveillance and turn a calm
character into a gimmick. If the .riv owns this drift itself, keep the same
schedule and the same sub-pixel amplitude. At `head` size it is invisible, so
do not run it there.

---

## 5. Per-state animation

The governing rule: **motion must mean something.** Neoh is nearly still at
rest and expressive only when the product state actually changed. A mascot
that pulses forever teaches the eye to skip it, and then the one state that
mattered goes unseen.

All durations below are the shipped CSS values.

| `state` | What moves | Timing |
|---|---|---|
| `idle` | A blink, and nothing else. `eyes` group `scaleY` 1 → 0.12 → 1, held shut across a 2% sliver of the cycle (94%–96%). | 7.2 s loop, ease-in-out |
| `listening` | `roofEdge` stroke goes full cyan. On `bust`/`full` the head leans `rotate(-1.5°) translateY(-0.4%)`. Amplitude does the rest via `level`. | 320 ms head settle |
| `thinking` | `roofEdge` travels: stroke cycles shell-edge → cyan and 1.2 → 1.7 wide. Head holds at `rotate(-3°)`. `sideLeft` and `sideRight` alternate opacity in antiphase (0.3 ↔ 0.85). `emblem` breathes 0.55 ↔ 1. | 2.1 s loop (roofline, sides), 2.6 s loop (emblem), ease-in-out |
| `speaking` | `roofEdge` full cyan. `visor` blooms with amplitude. `emblem` opacity rides amplitude. Energy moves through the character, not the eyes. | amplitude-driven, 90 ms follow |
| `acting` | Steady. `sideCore` pulses opacity 0.5 ↔ 1. The app draws the action badge over the top. | 1.35 s loop, ease-in-out |
| `success` | Identity light becomes `--neoh-success`. `roofEdge` green. Head lifts `translateY(-2%)`. `emblem` pops scale 1 → 1.22 (at 38%) → 1, **once**. | 620 ms one-shot, ease-out |
| `needs_attention` | Identity light becomes `--neoh-attention`. Head `rotate(+2.5°)`. `roofEdge` stroke-width breathes 1.2 ↔ 2. | 2.6 s loop, ease-in-out |
| `error` | Identity light becomes `--neoh-error`. `roofEdge` red. `emblem` drops to opacity 0.45. No loop. | settle only |
| `disconnected` | Whole character to opacity 0.55. Eyes, side ring, side core and emblem drop to opacity 0.4 with all bloom removed. Nothing pretends to be working. | settle only |
| any → not `disconnected` | Eyes fade in from opacity 0.45 to 1. Coming back is a settle, not an entrance. | 280 ms one-shot, ease-out |

`success` is brief and then back to work. The app holds it for
**1400 ms** (`SUCCESS_HOLD_MS`) before the state reverts. No confetti, no
dance, no second bounce.

### 5.1 Transition timing budget

State-to-state transitions live in **150–450 ms**. Longer loops are allowed
*inside* `thinking` (2.1 s and 2.6 s), because that is a state the person
waits in — but the entry and exit still have to land inside the budget.

Shipped values, as a floor and ceiling to match:

| Property | Duration | Easing |
|---|---|---|
| side ring / core opacity and scale | 90 ms | linear |
| eye `rx` / `ry` | 220 ms | `cubic-bezier(0.2, 0.7, 0.2, 1)` |
| eye group transform (tilt) | 240 ms | `cubic-bezier(0.2, 0.7, 0.2, 1)` |
| emblem transform and opacity | 240 ms | ease |
| roofline stroke colour and width | 260 ms | ease |
| eye `cx` / `cy` (gaze) | 300 ms | `cubic-bezier(0.2, 0.7, 0.2, 1)` |
| head transform | 320 ms | `cubic-bezier(0.2, 0.7, 0.2, 1)` |
| arm transform | 320 ms | `cubic-bezier(0.2, 0.7, 0.2, 1)` |

Note the split: shape changes faster than position (220 ms vs 300 ms). That
is what makes an expression change read as the face *reacting* rather than
the whole head sliding.

The amplitude-driven properties are at 90 ms deliberately. Anything slower
lags behind speech; anything faster flickers on the gaps between words.

---

## 6. Audio reactivity

`level` is a 0..1 amplitude, already smoothed by the app (exponential
follow, attack 0.45, release 0.12 — faster attack than release, so it tracks
speech onsets without stuttering in the gaps). Do not smooth it again.

**`level` drives the side modules and the bloom. Nothing else.**

| Target | Mapping |
|---|---|
| `sideRing` opacity | `0.34 + level × 0.5` |
| `sideCore` opacity | `0.45 + level × 0.55` |
| `sideCore` scale | `0.8 + level × 0.45`, about its own centre |
| `visor` bloom radius (`speaking` only) | `1px + level × 3px` |
| `emblem` opacity (`speaking` only) | `0.6 + level × 0.4` |

**Never the eyes. Never a mouth.** This character has no mouth and never
gets one. A light that tracks speech reads as a voice; eyes that track speech
read as a mouth, and a mouth on this face is the uncanny result the whole
design exists to avoid. If an amplitude curve is bound to anything in the
`eyes` group, the asset is wrong.

When `still` is true the app already sends `level = 0`, so the lights settle
to their floor values. Do not special-case it.

---

## 7. `still` and reduced motion

`still` is true when the person has requested reduced motion **or** the
browser tab is hidden. Both take the same path.

When `still` is true:

- **Hold the pose. Run no loops.** No blink, no roofline travel, no pulse.
- **Every state must still be legible.** The eye expression still changes
  shape, the identity colour still shifts, the head still holds its state
  tilt. The state being reported is still true, so the person still needs to
  be able to read it.
- **Never hide the character.** Reduced motion is not an excuse to render
  nothing. Hiding Neoh removes information; it does not remove motion.
- Transitions collapse to instant, not to a shorter duration.

A hidden tab must not keep an animation loop warm. When `still` goes true,
the asset should reach the target pose and stop advancing — not idle at 60fps
on a static frame.

---

## 8. Performance

| Constraint | Target |
|---|---|
| Active state (speaking, thinking, listening) | 60 fps |
| Idle | near-zero cost — one 7.2 s blink, nothing else |
| Tab hidden | fully paused (`still = true`) |
| Reduced motion | fully paused (`still = true`) |

Several Neohs can be on screen at once. Budget per-instance, not per-page.

Keep the asset small. The current SVG face weighs effectively nothing and is
the fallback; a .riv that costs more than it improves will not ship. Avoid
per-frame path morphs where a transform would do, and avoid raster fills
entirely — this character is flat vector by design and must stay crisp from
20px to a full-page hero.

---

## 9. Safe zones and minimum size

**The head must read at 20px.** That is the default size in the app
(`size = '20px'`, and the size property is the *height*). At 20px the head
artboard's 32 units map to roughly 0.63 device pixels per unit. Anything
finer than about 1 unit of stroke will disappear. Test at 20px before
anything else; if the silhouette, the visor and two cyan eyes do not read at
that size, nothing else matters.

Do not add detail that only exists above 20px unless it is genuinely
additive — the `bust` and `full` variants are the same head at 0.82 and 0.75
scale, so extra head detail gets *smaller*, not larger, on the bigger
artboards.

### Badge safe zone

The app overlays the action badge as a DOM element positioned against the
avatar box. Keep these regions clear of anything load-bearing:

| Variant | Badge box | Position |
|---|---|---|
| `head` | 58% × 58% of the box | right `-25%`, bottom `-18%` (mostly outside, bottom-right) |
| `bust` | 30% × 30% | right `2%`, bottom `46%` |
| `full` | 24% × 13% | right `-4%`, bottom `62%` |

The SVG renders with `overflow: visible`, so shapes may extend past the
artboard — but anything that does will be clipped by whatever the avatar is
sitting in. Keep the silhouette inside the artboard.

---

## 10. Verifying your .riv against the app

1. `npm i @rive-app/react-canvas` in `oracle-app/`.
2. Drop the asset at `oracle-app/public/neoh/neoh-avatar.riv`.
3. Set `VITE_NEOH_AVATAR_RIVE=/neoh/neoh-avatar.riv`.
4. Uncomment the real implementation block in
   `oracle-app/src/neoh/NeohAvatarRive.jsx`.

With `VITE_NEOH_AVATAR_RIVE` unset, `NeohAvatarRive.jsx` is never imported,
never bundled, and the Rive runtime is not a dependency. That is why this
handoff can sit in the repo without costing the bundle anything.

No caller changes are needed at any point. The rest of the product speaks
product states and never learns an input name.

### Checklist

- [ ] State machine is named `NeohState`, exactly.
- [ ] All five inputs exist with the exact names and types in §1.
- [ ] Setting `state` to each of 0–8 produces a visibly distinct, nameable
      pose. Screenshot each one and check a colleague can name it.
- [ ] `state = 5` (`success`) plays its pop once and holds — it must look
      right when the app reverts the state after 1400 ms.
- [ ] Sweeping `level` 0 → 1 moves the side modules and the bloom, and
      moves **nothing** in the `eyes` group.
- [ ] `still = true` holds every one of the nine states legibly, with no
      loop running and nothing hidden.
- [ ] `actionType` 0–7 are all handled; unknown behaves as `generic`.
- [ ] All three artboards render at their exact sizes: 32×32, 40×44, 40×72.
- [ ] The head artboard is legible at 20px.
- [ ] Failure path: rename the asset so it 404s, confirm the app falls back
      to the SVG face rather than hanging.

If the .riv fails to load the app calls `onError` once and falls back to the
inline SVG permanently. Silent fallback is the correct behaviour — but it
also means a broken asset looks like a working app, so check the console
rather than the screen.

---

## Not yet defined

- **`attention` input** — the app passes `attentionLevel` (0..1) through to
  the state machine, but the shipped SVG renderer only forwards it to a CSS
  custom property (`--neoh-attention-level`) that no rule currently reads.
  *NOT YET IMPLEMENTED — artist to define* what a rising `attention` does
  beyond the `needs_attention` state's own roofline breathe. Whatever it is,
  it must not become a second, louder alarm: Neoh is never the only place a
  problem is reported.
- **`--neoh-light-dim`** — the token is declared (`color-mix(in srgb,
  var(--neoh-light-primary) 30%, transparent)`) but no rule uses it.
  Available if a dimmed tier of the identity light is needed.
