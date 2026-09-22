# Neoh — mascot design spec

What Neoh looks like, and which parts of that are not open for revision.

This document exists to stop drift. A mascot degrades one reasonable-seeming
change at a time: a mouth "so it can smile", a float "so it feels alive", a
brand-blue repaint "for consistency". Each is defensible alone and the sum is
a different character. The constraints below are the ones with a reason
behind them; the reason is stated so a future change can argue with it rather
than around it.

Reference art: `oracle-app/design/neoh-character-sheet.png`.

Implementation of record: `oracle-app/src/neoh/` —
`NeohCharacter.jsx` (geometry), `NeohAvatar.module.css` (material and
behaviour), `eyeSystem.js` (expressions), `characterGeometry.js`
(proportions). The animator handoff is `docs/neoh-avatar-rive-spec.md`.

---

## 1. Visual identity

Neoh is a small white robot assistant.

| Element | Description |
|---|---|
| Shell | White / soft-silver. Matte, flat vector. No gradients, no bevel, no rendered highlight. |
| Head silhouette | **Roof-shaped** — a pitched apex over straight sides with a rounded base. The house is in the outline, not in an applied logo. |
| Visor | A dark glossy face panel inset into the shell. It carries the whole face. |
| Eyes | Two cyan lights on the visor. The only facial feature. |
| Side modules | Two circular illuminated modules, one at each side of the head. These carry voice. |
| Chest emblem | A glowing home-shaped mark on the torso. `bust` and `full` only. |
| Mouth | **None. Ever.** |

The roof silhouette and the home emblem are the same idea said twice, at two
scales. That is deliberate: at 20px only the silhouette survives, so the
silhouette has to carry the meaning on its own.

---

## 2. Colour

Tokens are declared once on `.avatar` in `NeohAvatar.module.css`. State rules
**remap the tokens** rather than restating hex values — that is what keeps
Neoh's error from drifting one red away from the rest of the product.

| Token | Light value | Dark override |
|---|---|---|
| `--neoh-light-primary` | `#35c8f5` | — |
| `--neoh-light-soft` | `color-mix(in srgb, var(--neoh-light-primary) 55%, transparent)` | — |
| `--neoh-light-dim` | `color-mix(in srgb, var(--neoh-light-primary) 30%, transparent)` | — |
| `--neoh-shell` | `#f4f5f7` | `#d7dae0` |
| `--neoh-shell-shade` | `#dcdfe5` | `#aeb4bf` |
| `--neoh-shell-edge` | `color-mix(in srgb, var(--neoh-light-primary) 42%, transparent)` | — |
| `--neoh-visor` | `#1b2030` | `#10131d` |
| `--neoh-joint` | `#3a4250` | — |
| `--neoh-success` | `var(--oracle-green)` | follows the product token |
| `--neoh-attention` | `var(--oracle-accent)` | follows the product token |
| `--neoh-error` | `var(--oracle-red)` | follows the product token |

Dark-theme overrides are only three: shell, shell-shade, visor. The shell
darkens slightly so a pure-white robot does not punch a hole in a dark
surface; the visor darkens so it stays *darker than the page*, which is what
makes it read as glass rather than as a grey rectangle. Nothing else changes,
and in particular the cyan does not.

`--neoh-light-dim` is declared but currently unused by any rule. It is
available if a dimmed tier of the identity light is needed; it is not a gap
to fill for its own sake.

### Why the cyan is not from the palette

`#35c8f5` is the one colour here not taken from the product palette.
Everything else defers to the interface. The cyan is what makes this a
character rather than a status dot — if it becomes the product's accent
colour, Neoh stops being a thing and becomes a state.

### Semantic recolouring

Three states swap `--neoh-light-primary` wholesale, which recolours the eyes,
side lights, roofline and emblem together:

| State | `--neoh-light-primary` becomes |
|---|---|
| `success` | `--neoh-success` |
| `needs_attention` | `--neoh-attention` |
| `error` | `--neoh-error` |

All three inherit from the product's own tokens, so a palette change carries
through without touching this file. `disconnected` does not recolour — it
drops the whole character to opacity `0.55` and strips the bloom, because the
honest statement there is "no signal", not "a different signal".

---

## 3. Variants and proportions

From `characterGeometry.js`. The head is always authored in its own 32 × 32
space and placed by transform, so every variant inherits each fix to the head
for free.

| Variant | Artboard | Head transform | Head scale |
|---|---|---|---|
| `head` | 32 × 32 | none | 1.00 |
| `bust` | 40 × 44 | `translate(6.88 1) scale(0.82)` | 0.82 |
| `full` | 40 × 72 | `translate(8 1) scale(0.75)` | 0.75 |

Sizing is by **height**. The caller sets one length and the width follows the
artboard (`bust` = height × 40/44, `full` = height × 40/72), so variants can
be swapped without arithmetic at the call site. Default size is `20px`.

Key proportions:

| | `bust` | `full` |
|---|---|---|
| Neck | `6 × 4.5`, rx 2.2, at `(17, 21.5)` | `4.8 × 3.6`, rx 1.8, at `(17.6, 16.4)` |
| Chest emblem | at `(20, 31.4)`, scale 0.42 | at `(20, 29.6)`, scale 0.42 |
| Shoulder pivots | `x 8.2` / `x 31.8`, `y 31.5` | `x 10.1` / `x 29.9`, `y 25.6` |
| Arm length | 9 | 11 |
| Limb width | 3.8, rx 1.9 | 3.8, rx 1.9 |
| Arm rest angle | -7° left, +7° right | -7° left, +7° right |
| Legs | — | two columns `4.4 × 17`, rx 2.2, at `y 44.4` |
| Feet | — | ellipses `rx 3.5, ry 2.5` at `y 63.4` |

Head, in its own 32 × 32 space: apex at `(16, 2.6)`, roofline down to
`x 4.6` / `x 27.4` at `y 11.2`, base at `y 27.6` with `4.2` corner radius.
Visor inset from `(16, 6.4)` down to `y 24.6` with `2.4` corner radius. Side
modules centred at `(3.4, 16.4)` and `(28.6, 16.4)`, ring `r 2.6`, core
`r 1.15`. The eye pair sits centred on `x = 16` at roughly `y = 14`–`15`,
with half-spread 4.

The legs are two rounded columns and two feet. Simple on purpose — this
character is never asked to walk, and legs detailed enough to imply a gait
create an expectation the product will not meet.

---

## 4. Where each variant may appear

| Variant | Allowed | Not allowed |
|---|---|---|
| `head` | Pills, inputs, status indicators, inline next to a line of text, list rows. The product default. | — |
| `bust` | Voice and conversation surfaces, where expression has room and the person is actually looking at Neoh. | Anywhere the person is doing something else. |
| `full` | Onboarding, empty states, a finished setup, a genuinely major success. Moments. | **Never a panel decoration.** Never a sidebar ornament, never a corner watermark, never filler in a layout that felt empty. |

The rule underneath: the bigger the variant, the more attention it claims,
and attention Neoh takes is attention the person's work does not get. `full`
is for the handful of screens where looking at Neoh *is* the task. In the
shipped app, `NeohSurface` uses `bust` only while voice is active and `head`
otherwise; nothing renders `full` yet.

An empty state is not automatically a `full` moment. If the panel would look
fine with a sentence, use a sentence.

---

## 5. Motion character

One rule governs everything: **motion must mean something.** Neoh is nearly
still at rest and expressive only when the product state actually changed. A
mascot that pulses forever teaches the eye to skip it, and then the one state
that mattered goes unseen.

Concretely, at rest Neoh does exactly one thing: blink, on a 7.2 s cycle,
with the eyes shut across about 2% of that cycle. On `bust` and `full` there
is additionally a rare autonomous look-away — a glance every 6–15 seconds,
lasting 1.1 s, offsetting the gaze by under two thirds of a unit. That is
punctuation, not motion.

Everything else is a response to a real fact. State transitions land in
150–450 ms. The only long loops belong to `thinking` (2.1 s and 2.6 s),
because that is a state the person is waiting in and a slow cycle is what
makes waiting feel attended rather than frozen.

Reduced motion and hidden tabs hold the pose. Every state stays legible
through expression and colour — the eyes still change shape, the accent still
shifts. Reduced motion never hides Neoh, because the state being reported is
still true.

Full timing values are in `docs/neoh-avatar-rive-spec.md` §5.

---

## 6. What NOT to change

These are the load-bearing decisions. Changing one of them produces a
different character wearing Neoh's colours.

### The roof silhouette

The pitched roofline is the identity at small sizes. At 20px the visor is a
smudge and the eyes are two dots — the outline is the only thing left that
says which product this is. Rounding the apex, squaring the sides, or
replacing the shape with a circle deletes the identity at exactly the size it
is used most.

### The mouthless visor

Neoh has no mouth and never gets one. Two reasons, both hard:

1. A mouth on a face that is otherwise a dark panel lands squarely in the
   uncanny valley, and it gets worse the better it is animated.
2. A mouth invites lip-sync, and lip-sync invites binding audio amplitude to
   the face. The moment amplitude drives anything in the face, Neoh reads as
   chewing. Amplitude belongs in the side modules and the bloom — a *light*
   that tracks speech reads as a voice.

The eight expressions in `eyeSystem.js` are the entire emotional range and
that is sufficient. If a state cannot be expressed with two shapes, the
answer is words in the interface, not a ninth facial feature.

### The cyan identity light

`#35c8f5` is Neoh. Do not repaint it to match a theme, a tenant's brand, or
the current accent colour. The three semantic recolours (`success`,
`needs_attention`, `error`) are the complete list of times it changes, and
each one is a statement about the system rather than a style choice.

### No continuous bounce or float

Nothing idles by moving. No hover bob, no breathing scale, no floating
offset, no slow rotation. The blink is the whole idle vocabulary. A character
in perpetual motion is a character nobody looks at, and Neoh's entire value is
being looked at the one time it matters.

### No cartoon squash-and-stretch

Neoh is a machine with a rigid shell. The shell does not deform. Motion comes
from transforms of separate parts around real pivots — the head turns about
its neck (50% / 82% of its own box), arms swing from their shoulders. A
squashing head reads as rubber, and rubber is not a product surface the
person is supposed to trust with their CRM.

The one permitted scale is the chest emblem's success pop: 1 → 1.22 → 1 over
620 ms, once. That is the emblem's own light flaring, not the body deforming.

### No confetti, no celebration choreography

`success` is a colour change, a 2% lift and a single emblem pop, held for
1400 ms, then back to work. It is an acknowledgement, not a party. The person
closed a deal, not a level.

### No mouse-following gaze

The eyes never track the cursor. Eyes that follow the pointer around the
screen read as surveillance rather than life, and they are the fastest way to
turn a calm character into a gimmick. The idle drift in `idleGaze.js` runs on
its own schedule and has no idea where anyone is — that is the point, and the
file says so.

### Neoh is never the only indicator

`error`, `needs_attention` and `disconnected` are states Neoh *reflects*.
They must always also be stated in the interface, in words. A 20px face is
not an error message, and a person who has scrolled past it has not been
told.

---

## 7. Adding a state or expression

Don't, unless the product actually asks a new question.

There are nine states and eight expressions, deliberately few. A status
indicator with twenty faces is a virtual pet, and a pet is the wrong thing to
put on top of someone's CRM. Several states already share an expression —
`acting` and `listening` are both `focused`, because the glyph and the lights
already distinguish them.

The test: name the new expression from a screenshot, with no context. If you
cannot, it is decoration, and decoration on a face reads as an emoji.

If a new state does earn its place, the numeric indices in `riveInputs.js`
are a wire format shared with the animated asset: **append, never renumber.**
A shipped .riv that disagrees with the app animates the wrong state, and
nothing will catch it.
