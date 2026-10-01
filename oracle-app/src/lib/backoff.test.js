import { describe, it, expect } from 'vitest';
import { jitteredBackoff, retryAfterMs } from './backoff.js';

describe('jitteredBackoff', () => {
  it('spreads a crowd across the window instead of retrying in lockstep', () => {
    const delays = Array.from({ length: 200 }, (_, i) =>
      jitteredBackoff(3, { base: 2000, max: 60000, random: () => i / 200 }));
    expect(Math.min(...delays)).toBe(8000);          // floor: half the 16 s step
    expect(Math.max(...delays)).toBeGreaterThan(15500);
    expect(new Set(delays).size).toBeGreaterThan(150);
  });

  it('never exceeds the cap, however many attempts', () => {
    expect(jitteredBackoff(40, { base: 2000, max: 60000, random: () => 1 })).toBe(60000);
    expect(jitteredBackoff(40, { base: 2000, max: 60000, random: () => 0 })).toBe(30000);
  });
});

describe('retryAfterMs', () => {
  it('reads delta-seconds and HTTP dates', () => {
    expect(retryAfterMs('60')).toBe(60000);
    expect(retryAfterMs('Thu, 01 Jan 2026 00:00:30 GMT', Date.parse('Thu, 01 Jan 2026 00:00:00 GMT'))).toBe(30000);
    expect(retryAfterMs(null)).toBeNull();
    expect(retryAfterMs('soon')).toBeNull();
  });
});
