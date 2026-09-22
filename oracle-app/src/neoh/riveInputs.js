/**
 * riveInputs — the numeric contract between Neoh's product states and the
 * animated character asset.
 *
 * Kept in its own module (no component export) so the mapping can be tested
 * and referenced by the asset author without importing a React component.
 *
 * The final .riv must expose a state machine named "NeohState" with:
 *
 *   Number  state       — see STATE_INDEX below
 *   Number  level       — 0..1 smoothed amplitude. Drives the side lights and
 *                         edge glow ONLY. Never a mouth: this character has
 *                         no mouth, and a face that chews on audio is exactly
 *                         the uncanny result to avoid.
 *   Number  actionType  — see ACTION_INDEX below
 *   Number  attention   — 0..1, how much the person is being asked for
 *   Boolean still       — true = hold the pose, run no loops (hidden tab or
 *                         reduced motion)
 *
 * The indices are a wire format: append new values, never renumber existing
 * ones, or a shipped asset starts animating the wrong state.
 */

export const STATE_INDEX = Object.freeze({
  idle: 0,
  listening: 1,
  thinking: 2,
  speaking: 3,
  acting: 4,
  success: 5,
  needs_attention: 6,
  error: 7,
  disconnected: 8,
});

export const ACTION_INDEX = Object.freeze({
  generic: 0,
  call: 1,
  message: 2,
  calendar: 3,
  property: 4,
  search: 5,
  save: 6,
  share: 7,
});

export function stateInput(state) {
  return STATE_INDEX[state] ?? STATE_INDEX.idle;
}

export function actionInput(actionType) {
  return ACTION_INDEX[actionType] ?? ACTION_INDEX.generic;
}
