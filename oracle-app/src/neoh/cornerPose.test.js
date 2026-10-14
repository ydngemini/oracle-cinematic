import { describe, expect, it } from 'vitest';

import { AVATAR_STATES } from './avatarModel';
import { cornerPose } from './cornerPose';
import { eyeExpression } from './eyeSystem';

describe('cornerPose', () => {
  it('wears the same eye expression as the flat mascot for every state', () => {
    for (const state of AVATAR_STATES) {
      const pose = cornerPose(state);
      expect(pose.state).toBe(state);
      expect(pose.expression, state).toBe(eyeExpression(state).name);
    }
  });

  it('falls back to the idle pose for anything unknown', () => {
    for (const state of [undefined, '', 'dancing', 'happy', '__proto__']) {
      expect(cornerPose(state).state).toBe('idle');
    }
  });

  it('colours the lights like the stylesheet: outcome, attention and failure differ from rest', () => {
    const idle = cornerPose('idle').light.join();
    const lights = ['success', 'needs_attention', 'error'].map((s) => cornerPose(s).light.join());
    expect(new Set([idle, ...lights]).size).toBe(4);
    expect(cornerPose('disconnected').glow).toBeLessThan(cornerPose('idle').glow);
  });

  it('keeps every number bounded — a pose table, not a channel for coordinates', () => {
    for (const state of AVATAR_STATES) {
      const pose = cornerPose(state);
      for (const key of ['gazeX', 'gazeY']) expect(Math.abs(pose[key])).toBeLessThanOrEqual(1);
      expect(pose.drift).toBeGreaterThanOrEqual(0);
      expect(pose.drift).toBeLessThanOrEqual(1);
      expect(Math.abs(pose.pitch)).toBeLessThanOrEqual(10);
      expect(Object.isFrozen(pose)).toBe(true);
    }
  });
});
