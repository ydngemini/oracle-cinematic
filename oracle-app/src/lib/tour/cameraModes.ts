/**
 * Camera controllers for the property tour: first-person walk and third-person
 * orbit, sharing one input state so a mode switch never drops a held key or an
 * in-flight touch.
 *
 * Framework-free and engine-injected (the `pc` namespace is passed in) so this
 * module can be tested in Node and so PlayCanvas stays a lazy import.
 */

import type * as pcNS from 'playcanvas';

export type CameraMode = 'walk' | 'orbit';

export interface Bounds {
  min: [number, number, number];
  max: [number, number, number];
}

export interface ControllerConfig {
  /** Metres per second on the ground plane. */
  walkSpeed: number;
  /** Multiplier while shift is held. */
  runMultiplier: number;
  /** Eye height above the floor plane, metres. */
  eyeHeight: number;
  /** Radians per pixel of pointer movement. */
  lookSensitivity: number;
  /** Clamp so the camera never flips over the pole. */
  maxPitch: number;
  /** Orbit distance limits, metres. */
  minDistance: number;
  maxDistance: number;
}

export const DEFAULT_CONFIG: ControllerConfig = {
  walkSpeed: 2.4,
  runMultiplier: 2.2,
  eyeHeight: 1.6,
  lookSensitivity: 0.0022,
  maxPitch: Math.PI / 2 - 0.05,
  minDistance: 1.2,
  maxDistance: 60,
};

const MOVE_KEYS: Record<string, [number, number]> = {
  // [strafe, forward]
  keyw: [0, 1], arrowup: [0, 1],
  keys: [0, -1], arrowdown: [0, -1],
  keya: [-1, 0], arrowleft: [-1, 0],
  keyd: [1, 0], arrowright: [1, 0],
};

export interface InputState {
  keys: Set<string>;
  /** Virtual joystick / drag vector, each axis -1..1. */
  joystick: { x: number; y: number };
  /** Accumulated look delta consumed each frame. */
  lookDelta: { x: number; y: number };
  /** Accumulated zoom delta (orbit only). */
  zoomDelta: number;
  running: boolean;
}

export function createInputState(): InputState {
  return {
    keys: new Set(),
    joystick: { x: 0, y: 0 },
    lookDelta: { x: 0, y: 0 },
    zoomDelta: 0,
    running: false,
  };
}

export interface CameraState {
  mode: CameraMode;
  /** Walk mode: eye position. Orbit mode: pivot/target. */
  position: [number, number, number];
  yaw: number;
  pitch: number;
  /** Orbit only. */
  distance: number;
  bounds: Bounds | null;
  /** Which floor index the camera is on; drives level visibility. */
  floorIndex: number;
  /** Y of the current floor plane, metres. */
  floorY: number;
}

export function createCameraState(mode: CameraMode = 'orbit'): CameraState {
  return {
    mode,
    position: [0, 0, 0],
    yaw: Math.PI,
    pitch: mode === 'orbit' ? -0.35 : 0,
    distance: 12,
    bounds: null,
    floorIndex: 0,
    floorY: 0,
  };
}

/** Translate a KeyboardEvent.code into our movement table. */
export function movementFromKeys(keys: Set<string>): { strafe: number; forward: number } {
  let strafe = 0;
  let forward = 0;
  for (const key of keys) {
    const vec = MOVE_KEYS[key];
    if (!vec) continue;
    strafe += vec[0];
    forward += vec[1];
  }
  return { strafe, forward };
}

/**
 * Clamp a position inside the capture bounds, padded outward slightly.
 *
 * Gaussian splats have no collision and no data outside the camera path, so
 * without this the user walks into an empty void and thinks the tour broke.
 * This is the single most important UX guard in the viewer.
 */
export function clampToBounds(
  position: [number, number, number],
  bounds: Bounds | null,
  padding = 1.0,
): [number, number, number] {
  if (!bounds) return position;
  const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo - padding), hi + padding);
  return [
    clamp(position[0], bounds.min[0], bounds.max[0]),
    clamp(position[1], bounds.min[1], bounds.max[1]),
    clamp(position[2], bounds.min[2], bounds.max[2]),
  ];
}

/**
 * Advance the camera one frame. Pure-ish: mutates `state` and `input` (draining
 * the accumulated deltas) and writes the result onto the PlayCanvas entity.
 */
export function stepCamera(
  camera: pcNS.Entity,
  state: CameraState,
  input: InputState,
  dt: number,
  config: ControllerConfig = DEFAULT_CONFIG,
): void {
  // --- look ---------------------------------------------------------------
  state.yaw -= input.lookDelta.x * config.lookSensitivity;
  state.pitch -= input.lookDelta.y * config.lookSensitivity;
  state.pitch = Math.max(-config.maxPitch, Math.min(config.maxPitch, state.pitch));
  input.lookDelta.x = 0;
  input.lookDelta.y = 0;

  if (state.mode === 'orbit') {
    state.distance = Math.max(
      config.minDistance,
      Math.min(config.maxDistance, state.distance + input.zoomDelta),
    );
  }
  input.zoomDelta = 0;

  // --- movement -----------------------------------------------------------
  const { strafe, forward } = movementFromKeys(input.keys);
  let moveX = strafe + input.joystick.x;
  let moveZ = forward + input.joystick.y;

  // Normalise so diagonal movement isn't ~41% faster than axis-aligned.
  const magnitude = Math.hypot(moveX, moveZ);
  if (magnitude > 1) {
    moveX /= magnitude;
    moveZ /= magnitude;
  }

  if (magnitude > 0) {
    const speed = config.walkSpeed * (input.running ? config.runMultiplier : 1) * dt;
    const sin = Math.sin(state.yaw);
    const cos = Math.cos(state.yaw);
    // Forward is -Z in PlayCanvas's right-handed convention.
    const dx = (moveX * cos - moveZ * sin) * speed;
    const dz = (moveX * sin + moveZ * cos) * speed;

    if (state.mode === 'walk') {
      state.position = clampToBounds(
        [state.position[0] + dx, state.position[1], state.position[2] + dz],
        state.bounds,
      );
      // Walk mode stays pinned to the current floor plane — a splat has no
      // ground collision, so gravity would drop us through the model.
      state.position[1] = state.floorY + config.eyeHeight;
    } else {
      // Orbit mode pans the pivot rather than moving the eye.
      state.position = clampToBounds(
        [state.position[0] + dx, state.position[1], state.position[2] + dz],
        state.bounds,
      );
    }
  }

  // --- write to the entity -------------------------------------------------
  if (state.mode === 'walk') {
    camera.setPosition(state.position[0], state.position[1], state.position[2]);
    camera.setEulerAngles(
      (state.pitch * 180) / Math.PI,
      (state.yaw * 180) / Math.PI,
      0,
    );
  } else {
    // Third-person: place the eye on a sphere around the pivot.
    const cosPitch = Math.cos(state.pitch);
    const eyeX = state.position[0] - Math.sin(state.yaw) * cosPitch * state.distance;
    const eyeY = state.position[1] - Math.sin(state.pitch) * state.distance;
    const eyeZ = state.position[2] - Math.cos(state.yaw) * cosPitch * state.distance;
    camera.setPosition(eyeX, eyeY, eyeZ);
    camera.lookAt(state.position[0], state.position[1], state.position[2]);
  }
}

/**
 * Frame the model on first load: pull the camera back far enough that the whole
 * bounding box fits the vertical FOV, and drop the walk-mode spawn in the
 * middle of the capture rather than at the origin (which is often outside it).
 */
export function frameBounds(
  state: CameraState,
  bounds: Bounds,
  fovDegrees = 55,
): void {
  state.bounds = bounds;
  const centre: [number, number, number] = [
    (bounds.min[0] + bounds.max[0]) / 2,
    (bounds.min[1] + bounds.max[1]) / 2,
    (bounds.min[2] + bounds.max[2]) / 2,
  ];
  const size = Math.max(
    bounds.max[0] - bounds.min[0],
    bounds.max[1] - bounds.min[1],
    bounds.max[2] - bounds.min[2],
  );
  const fovRadians = (fovDegrees * Math.PI) / 180;
  state.position = centre;
  state.distance = Math.max(2, (size / 2) / Math.tan(fovRadians / 2) * 1.35);
  state.floorY = bounds.min[1];
}

/**
 * Move the camera to a floor. Floors come from the tour manifest; `y` is the
 * floor plane height in metres.
 */
export function goToFloor(
  state: CameraState,
  floorIndex: number,
  y: number,
  config: ControllerConfig = DEFAULT_CONFIG,
): void {
  state.floorIndex = floorIndex;
  state.floorY = y;
  if (state.mode === 'walk') {
    state.position = [state.position[0], y + config.eyeHeight, state.position[2]];
  } else {
    state.position = [state.position[0], y + 1.5, state.position[2]];
  }
}
