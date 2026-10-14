/**
 * cornerPose — what each product state looks like on the 3D corner Neoh.
 *
 * Derived from the SAME eye expressions the flat SVG mascot wears
 * (eyeSystem.js), so the two renderers agree by construction: the 3D eyes
 * take their openness, width, gaze and glow as ratios of the neutral
 * expression, and the light colour follows the stylesheet's per-state hue
 * (NeohAvatar.module.css: success green, attention amber, error red,
 * disconnected dim).
 *
 * This is a pose TABLE keyed by state, not a channel for coordinates: no
 * event, model response or caller can set a number here. Unknown states get
 * the idle pose.
 *
 * Pure; the PlayCanvas renderer (NeohCorner3D.jsx) applies it.
 */
import { AVATAR_STATES } from './avatarModel';
import { EYE_EXPRESSIONS, eyeExpression } from './eyeSystem';
import { PALETTE } from './neohModel3d';

const NEUTRAL = EYE_EXPRESSIONS.neutral;
const MAX_GLOW = 1.25;

/** Approximations of the CSS state hues, as linear-ish 0..1 RGB for an emissive. */
const LIGHT = Object.freeze({
  success: Object.freeze([0.392, 0.824, 0.549]),         // ≈ --oracle-green (dark theme)
  needs_attention: Object.freeze([1.0, 0.737, 0.122]),   // --oracle-accent #ffbc1f
  error: Object.freeze([0.9, 0.29, 0.25]),               // ≈ --oracle-red, lifted to read as light
});

/**
 * How much of the autonomous idle drift each state keeps (0 = none), and a
 * head pitch in degrees. Attending states look at the viewer; a state with
 * something to say does not wander while saying it.
 */
const MOTION = Object.freeze({
  idle: { drift: 1, pitch: 0 },
  listening: { drift: 0.2, pitch: 0 },
  thinking: { drift: 0.35, pitch: 0 },
  speaking: { drift: 0.3, pitch: 0 },
  acting: { drift: 0.5, pitch: 0 },
  success: { drift: 0.3, pitch: -3 },
  needs_attention: { drift: 0.1, pitch: 0 },
  error: { drift: 0.15, pitch: 4 },
  disconnected: { drift: 0, pitch: 8 },
});

/** Gaze offsets are in head space (±~1); this turns them into ±1 look units. */
const GAZE_UNITS = 1.2;

/**
 * @param {string} state semantic state from avatarModel
 * @returns {{state: string, expression: string, eyeOpen: number, eyeWidth: number,
 *            gazeX: number, gazeY: number, glow: number, light: number[],
 *            drift: number, pitch: number}}
 */
export function cornerPose(state) {
  const known = AVATAR_STATES.includes(state) ? state : 'idle';
  const exp = eyeExpression(known);
  const motion = MOTION[known];
  // Bounded both ways: below 0.6 a lit state reads as off; above ~1.25 the
  // emissive saturates to white and the state hue is lost (checked in
  // headless Chromium, SwiftShader). Only `disconnected` may go dimmer.
  const ratio = exp.glow / NEUTRAL.glow;
  const glow = known === 'disconnected' ? ratio : Math.min(MAX_GLOW, Math.max(0.6, ratio));
  return Object.freeze({
    state: known,
    expression: exp.name,
    // A happy arc has no 3D equivalent here; a squint is its closest read.
    eyeOpen: exp.ry / NEUTRAL.ry,
    eyeWidth: exp.rx / NEUTRAL.rx,
    gazeX: Math.max(-1, Math.min(1, exp.gazeX / GAZE_UNITS)),
    gazeY: Math.max(-1, Math.min(1, exp.gazeY / GAZE_UNITS)),
    glow,
    light: LIGHT[known] || PALETTE.glow,
    drift: motion.drift,
    pitch: motion.pitch,
  });
}
