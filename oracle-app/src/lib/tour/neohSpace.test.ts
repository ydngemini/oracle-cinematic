/**
 * Neoh Space device reality: what to render, at what quality, in which units,
 * and what the viewer may claim about measurements.
 */
import { describe, expect, it } from 'vitest';

import { assessDevice, type DeviceSnapshot } from './deviceCapability';
import { createFrameRateGovernor, pixelRatioFor } from './renderQuality';
import { configForScene, scaleNotice } from './sceneNavigation';
import { DEFAULT_CONFIG, createCameraState, createInputState, stepCamera } from './cameraModes';

const desktop: DeviceSnapshot = {
  webgl2: true, webgl: true, deviceMemoryGb: 16, hardwareConcurrency: 12,
  touch: false, devicePixelRatio: 2,
};
const midPhone: DeviceSnapshot = {
  webgl2: true, webgl: true, deviceMemoryGb: 4, hardwareConcurrency: 8,
  touch: true, devicePixelRatio: 3, effectiveType: '4g',
};
const lowPhone: DeviceSnapshot = {
  webgl2: false, webgl: true, deviceMemoryGb: 2, hardwareConcurrency: 4,
  touch: true, devicePixelRatio: 2, effectiveType: '3g', saveData: true,
};

describe('device assessment', () => {
  it('gives a desktop GPU the high level', () => {
    const a = assessDevice(desktop);
    expect(a.canRender3D).toBe(true);
    expect(a.quality).toBe('high');
    expect(a.maxPixelRatio).toBe(2);
  });

  it('caps a dense phone screen instead of rendering at 3x', () => {
    const a = assessDevice(midPhone);
    expect(a.quality).toBe('balanced');
    expect(a.maxPixelRatio).toBe(1.5);
    expect(a.constrainedNetwork).toBe(false);
  });

  it('runs a low-memory phone on a slow network at the lightest level, and says so', () => {
    const a = assessDevice(lowPhone);
    expect(a.canRender3D).toBe(true);
    expect(a.quality).toBe('performance');
    expect(a.maxPixelRatio).toBe(1);
    expect(a.constrainedNetwork).toBe(true);
    expect(a.reason).toMatch(/lighter 3D view/);
  });

  it('refuses without WebGL or with a software rasteriser', () => {
    expect(assessDevice({ ...desktop, webgl2: false, webgl: false }).canRender3D).toBe(false);
    const sw = assessDevice({ ...desktop, renderer: 'Google SwiftShader' });
    expect(sw.canRender3D).toBe(false);
    expect(sw.reason).toMatch(/3D view unavailable on this device/);
  });
});

describe('frame-rate governor', () => {
  it('steps down when frames stay slow, never below performance', () => {
    const g = createFrameRateGovernor('high');
    const changes: string[] = [];
    for (let i = 0; i < 600; i += 1) {
      const next = g.sample(1 / 15);
      if (next) changes.push(next);
    }
    expect(changes).toEqual(['balanced', 'performance']);
    expect(g.level()).toBe('performance');
  });

  it('recovers only up to the level it started at', () => {
    const g = createFrameRateGovernor('balanced');
    for (let i = 0; i < 200; i += 1) g.sample(1 / 15);
    expect(g.level()).toBe('performance');
    for (let i = 0; i < 60 * 60; i += 1) g.sample(1 / 60);
    expect(g.level()).toBe('balanced');
  });

  it('ignores pauses such as a hidden tab', () => {
    const g = createFrameRateGovernor('high');
    expect(g.sample(5)).toBeNull();
    expect(g.level()).toBe('high');
  });

  it('maps levels to pixel ratios without exceeding the screen', () => {
    expect(pixelRatioFor('high', 1)).toBe(1);
    expect(pixelRatioFor('performance', 3)).toBe(1);
  });
});

describe('scene-unit navigation', () => {
  it('walks at the capture height in scene units, not an assumed 1.6 m', () => {
    const cfg = configForScene({
      navigation: { eyeHeight: 0.4 },
      denseBounds: { min: [-1, 0, -1], max: [1, 0.7, 1] },
    });
    expect(cfg.eyeHeight).toBeCloseTo(0.4);
    expect(cfg.walkSpeed).toBeCloseTo(DEFAULT_CONFIG.walkSpeed * (0.4 / 1.6));
    expect(cfg.maxDistance).toBeCloseTo(6);
  });

  it('keeps defaults for a capture with no scene metadata', () => {
    expect(configForScene(null)).toBe(DEFAULT_CONFIG);
  });

  it('never implies measurements without a measured scale', () => {
    expect(scaleNotice(null).measurementsAllowed).toBe(false);
    expect(scaleNotice({ status: 'estimated', measurementsAllowed: false }).label)
      .toMatch(/not for measuring/);
    expect(scaleNotice({ status: 'metric', measurementsAllowed: true }).measurementsAllowed).toBe(true);
    // A manifest that says metric but forbids measuring is not trusted.
    expect(scaleNotice({ status: 'metric', measurementsAllowed: false }).measurementsAllowed).toBe(false);
  });
});

describe('collision basics', () => {
  const camera = () => {
    const at = { x: 0, y: 0, z: 0 };
    return {
      at,
      setPosition: (x: number, y: number, z: number) => { at.x = x; at.y = y; at.z = z; },
      setEulerAngles: () => {},
      lookAt: () => {},
    };
  };

  it('never swings the orbit eye under the floor', () => {
    const cam = camera();
    const state = createCameraState('orbit');
    state.position = [0, 0.5, 0];
    state.floorY = 0;
    state.distance = 10;
    state.pitch = 1.2; // looking up from below
    stepCamera(cam as never, state, createInputState(), 0.016, DEFAULT_CONFIG);
    expect(cam.at.y).toBeGreaterThanOrEqual(DEFAULT_CONFIG.minEyeAboveFloor ?? 0);
  });

  it('keeps a walking camera at eye height and inside the captured space', () => {
    const cam = camera();
    const state = createCameraState('walk');
    state.bounds = { min: [-2, 0, -2], max: [2, 2.5, 2] };
    state.position = [0, 1.6, 0];
    const input = createInputState();
    input.keys.add('keyw');
    for (let i = 0; i < 400; i += 1) stepCamera(cam as never, state, input, 0.05, DEFAULT_CONFIG);
    expect(cam.at.y).toBeCloseTo(DEFAULT_CONFIG.eyeHeight);
    expect(Math.abs(cam.at.z)).toBeLessThanOrEqual(3.0001);
  });
});
