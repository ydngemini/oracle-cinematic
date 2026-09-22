/**
 * Neoh's eyes.
 *
 * The face is a dark visor with no mouth, so every readable emotion has to
 * come from two shapes. That makes the eye set worth defining once, in one
 * place, rather than inline in a renderer: the SVG renderer, a future Rive
 * asset and the tests all need to agree on what "thinking" looks like.
 *
 * Eight expressions, not twenty. Each one has to answer a question the
 * product actually asks ("is it listening?", "did that work?"). An expression
 * nobody can name from a screenshot is decoration, and decoration on a face
 * reads as an emoji.
 *
 * Geometry is in the head's own 32x32 coordinate space. Renderers scale it.
 */

/** Centre of the eye pair in head space. */
const MID_X = 16;
const BASE_Y = 14.6;
const SPREAD = 4;

/**
 * @typedef {object} EyeExpression
 * @property {string}  name    the expression, for tests and debugging
 * @property {number}  rx      horizontal radius
 * @property {number}  ry      vertical radius (a small ry is a narrowed eye)
 * @property {number}  y       vertical centre
 * @property {number}  spread  half the distance between the two eyes
 * @property {number}  tilt    degrees, rotated about the pair's centre
 * @property {number}  gazeX   horizontal look offset
 * @property {number}  gazeY   vertical look offset (negative is up)
 * @property {boolean} arc     draw as a happy arc rather than a filled shape
 * @property {number}  glow    0..1, how strongly the eye light blooms
 */

/** @type {Record<string, EyeExpression>} */
export const EYE_EXPRESSIONS = Object.freeze({
  // At rest. Open, level, unremarkable — this is the shape everything else
  // is read against, so it must not be interesting.
  neutral: Object.freeze({
    name: 'neutral', rx: 2.4, ry: 2.6, y: BASE_Y, spread: SPREAD, tilt: 0, gazeX: 0, gazeY: 0, arc: false, glow: 0.55,
  }),
  // Attending to the person. Slightly wider and a touch higher: the whole
  // difference between "on" and "listening to you" is about half a pixel.
  focused: Object.freeze({
    name: 'focused', rx: 2.5, ry: 2.9, y: 14.3, spread: 4.05, tilt: 0, gazeX: 0, gazeY: -0.1, arc: false, glow: 0.8,
  }),
  // Working. Gaze goes up and inward, the way a person looks away from you
  // to hold something in mind. No spinner, no orbiting dots.
  thinking: Object.freeze({
    name: 'thinking', rx: 2.1, ry: 2.4, y: 13.8, spread: 3.7, tilt: -8, gazeX: 0.35, gazeY: -0.85, arc: false, glow: 0.7,
  }),
  // Talking. Energy sits in the lights, not here — eyes that move per
  // syllable read as a mouth, and a mouth on this character is uncanny.
  speaking: Object.freeze({
    name: 'speaking', rx: 2.4, ry: 2.55, y: 14.4, spread: SPREAD, tilt: 0, gazeX: 0, gazeY: 0, arc: false, glow: 0.95,
  }),
  // It worked. The arc eyes from the character sheet, used briefly.
  happy: Object.freeze({
    name: 'happy', rx: 2.6, ry: 1.2, y: 14.7, spread: SPREAD, tilt: 0, gazeX: 0, gazeY: 0, arc: true, glow: 0.9,
  }),
  // "I need you." Wide and level and steady — awake, not frightened.
  attention: Object.freeze({
    name: 'attention', rx: 2.7, ry: 3.0, y: 14.0, spread: 4.1, tilt: 0, gazeX: 0, gazeY: 0, arc: false, glow: 1,
  }),
  // Something failed. Narrowed and settled. Not a sad face, not a wince —
  // this is a professional system state and Neoh is not injured.
  error: Object.freeze({
    name: 'error', rx: 2.3, ry: 1.7, y: 15.0, spread: SPREAD, tilt: 0, gazeX: 0, gazeY: 0, arc: false, glow: 0.5,
  }),
  // No channel. Dim and nearly shut. Nothing here pretends to be working.
  offline: Object.freeze({
    name: 'offline', rx: 2.2, ry: 1.3, y: 15.1, spread: SPREAD, tilt: 0, gazeX: 0, gazeY: 0, arc: false, glow: 0.15,
  }),
});

/**
 * Which expression a semantic state wears.
 *
 * Several states share one: `acting` and `listening` are both `focused`,
 * because Neoh's face does not need to distinguish "hearing you" from
 * "doing the thing" — the action glyph and the lights already say that.
 * Reusing an expression is cheaper to maintain than inventing a nuance
 * nobody can name.
 */
const STATE_TO_EXPRESSION = Object.freeze({
  idle: 'neutral',
  listening: 'focused',
  thinking: 'thinking',
  speaking: 'speaking',
  acting: 'focused',
  success: 'happy',
  needs_attention: 'attention',
  error: 'error',
  disconnected: 'offline',
});

/**
 * @param {string} state semantic state from avatarModel
 * @returns {EyeExpression}
 */
export function eyeExpression(state) {
  return EYE_EXPRESSIONS[STATE_TO_EXPRESSION[state] || 'neutral'];
}

/**
 * Resolve an expression into the two eyes' drawing geometry.
 *
 * Gaze is applied here rather than as a transform on the group so that a
 * renderer can animate position and shape independently — Rive will want the
 * same split.
 *
 * @param {EyeExpression} exp
 * @param {{gazeX?: number, gazeY?: number}} [drift] autonomous idle micro-gaze
 */
export function eyeGeometry(exp, drift) {
  // `= {}` would not help here: the caller passes null when there is no
  // drift, and a default only fires for undefined.
  const d = drift || {};
  const gx = exp.gazeX + (d.gazeX || 0);
  const gy = exp.gazeY + (d.gazeY || 0);
  return {
    cx: [MID_X - exp.spread + gx, MID_X + exp.spread + gx],
    cy: [exp.y + gy, exp.y + gy],
    rx: exp.rx,
    ry: exp.ry,
    tilt: exp.tilt,
    arc: exp.arc,
    glow: exp.glow,
  };
}

/** Every expression name, for tests and the artist handoff. */
export const EYE_EXPRESSION_NAMES = Object.freeze(Object.keys(EYE_EXPRESSIONS));
