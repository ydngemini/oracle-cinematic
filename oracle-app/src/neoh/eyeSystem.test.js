import { describe, expect, it } from 'vitest';

import { EYE_EXPRESSIONS, EYE_EXPRESSION_NAMES, eyeExpression, eyeGeometry } from './eyeSystem';
import { avatarState } from './avatarModel';

/**
 * The eye set is the whole emotional vocabulary of a character with no mouth,
 * so these tests guard the two things that actually matter: that every
 * product state has a face, and that the faces stay distinguishable.
 */

describe('eyeExpression', () => {
  const STATES = [
    'idle', 'listening', 'thinking', 'speaking', 'acting',
    'success', 'needs_attention', 'error', 'disconnected',
  ];

  it('gives every semantic state an expression', () => {
    for (const state of STATES) {
      expect(eyeExpression(state).name).toBeTruthy();
    }
  });

  it('covers every state avatarModel can actually produce', () => {
    // If avatarModel gains a state, this fails rather than the avatar
    // silently falling back to neutral and losing the distinction.
    const produced = new Set();
    const facts = [
      {}, { micActive: true }, { messages: [{ role: 'user', pending: true }] },
      { speaking: true }, { actionName: 'call_lead' }, { succeeded: true },
      { attention: true }, { failed: true }, { connection: 'offline' },
    ];
    for (const f of facts) produced.add(avatarState(f).state);
    for (const state of produced) {
      expect(EYE_EXPRESSION_NAMES).toContain(eyeExpression(state).name);
    }
  });

  it('falls back to neutral for a state it has never heard of', () => {
    expect(eyeExpression('nonsense').name).toBe('neutral');
    expect(eyeExpression(undefined).name).toBe('neutral');
  });

  it('is frozen, so no caller can mutate the shared face', () => {
    expect(Object.isFrozen(EYE_EXPRESSIONS)).toBe(true);
    expect(Object.isFrozen(EYE_EXPRESSIONS.neutral)).toBe(true);
  });

  it('keeps exactly the eight expressions the design defines', () => {
    expect(EYE_EXPRESSION_NAMES).toHaveLength(8);
  });

  it('draws only success as an arc — everything else is an open eye', () => {
    for (const name of EYE_EXPRESSION_NAMES) {
      expect(EYE_EXPRESSIONS[name].arc).toBe(name === 'happy');
    }
  });

  it('narrows the eye for the two states that mean "less alive"', () => {
    // error and offline must read as settled/dim rather than alert.
    expect(EYE_EXPRESSIONS.error.ry).toBeLessThan(EYE_EXPRESSIONS.neutral.ry);
    expect(EYE_EXPRESSIONS.offline.ry).toBeLessThan(EYE_EXPRESSIONS.neutral.ry);
    expect(EYE_EXPRESSIONS.offline.glow).toBeLessThan(EYE_EXPRESSIONS.error.glow);
  });

  it('opens wider for attention than at rest', () => {
    expect(EYE_EXPRESSIONS.attention.ry).toBeGreaterThan(EYE_EXPRESSIONS.neutral.ry);
    expect(EYE_EXPRESSIONS.attention.glow).toBeGreaterThan(EYE_EXPRESSIONS.neutral.glow);
  });

  it('looks up and away while thinking, and nowhere else', () => {
    expect(EYE_EXPRESSIONS.thinking.gazeY).toBeLessThan(0);
    expect(EYE_EXPRESSIONS.thinking.tilt).not.toBe(0);
    for (const name of ['neutral', 'speaking', 'happy', 'attention', 'error', 'offline']) {
      expect(EYE_EXPRESSIONS[name].gazeY).toBe(0);
      expect(EYE_EXPRESSIONS[name].tilt).toBe(0);
    }
  });
});

describe('eyeGeometry', () => {
  it('places two eyes symmetrically about the head centre', () => {
    const geo = eyeGeometry(EYE_EXPRESSIONS.neutral);
    expect(geo.cx[0] + geo.cx[1]).toBeCloseTo(32, 5); // mirrored about x=16
    expect(geo.cy[0]).toBe(geo.cy[1]);
  });

  it('survives a null drift — the caller passes null, not undefined', () => {
    // A `= {}` default would not catch this, and it crashed the whole avatar
    // the first time round.
    expect(() => eyeGeometry(EYE_EXPRESSIONS.neutral, null)).not.toThrow();
    const a = eyeGeometry(EYE_EXPRESSIONS.neutral, null);
    const b = eyeGeometry(EYE_EXPRESSIONS.neutral);
    expect(a).toEqual(b);
  });

  it('offsets both eyes together when drifting', () => {
    const base = eyeGeometry(EYE_EXPRESSIONS.neutral);
    const drifted = eyeGeometry(EYE_EXPRESSIONS.neutral, { gazeX: 0.5, gazeY: -0.3 });
    expect(drifted.cx[0] - base.cx[0]).toBeCloseTo(0.5, 5);
    expect(drifted.cx[1] - base.cx[1]).toBeCloseTo(0.5, 5);
    expect(drifted.cy[0] - base.cy[0]).toBeCloseTo(-0.3, 5);
  });

  it('never lets drift change the eye shape, only where it looks', () => {
    const base = eyeGeometry(EYE_EXPRESSIONS.focused);
    const drifted = eyeGeometry(EYE_EXPRESSIONS.focused, { gazeX: 1, gazeY: 1 });
    expect(drifted.rx).toBe(base.rx);
    expect(drifted.ry).toBe(base.ry);
    expect(drifted.arc).toBe(base.arc);
  });
});
