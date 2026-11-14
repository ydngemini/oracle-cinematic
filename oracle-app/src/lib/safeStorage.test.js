// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { safeRemove, safeSet } from './safeStorage.js';

afterEach(() => vi.restoreAllMocks());

describe('safeStorage (WEB-002)', () => {
  it('writes and removes when storage works', () => {
    expect(safeSet('sessionStorage', 'k', 'v')).toBe(true);
    expect(sessionStorage.getItem('k')).toBe('v');
    expect(safeRemove('sessionStorage', 'k')).toBe(true);
    expect(sessionStorage.getItem('k')).toBeNull();
  });

  it('never throws when storage refuses', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new DOMException('x', 'SecurityError'); });
    expect(safeSet('localStorage', 'k', 'v')).toBe(false);
    expect(safeRemove('sessionStorage', 'k')).toBe(false);
  });

  it('never throws when merely reading window.sessionStorage throws', () => {
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new DOMException('x', 'SecurityError'); });
    expect(safeSet('sessionStorage', 'k', 'v')).toBe(false);
    expect(safeRemove('sessionStorage', 'k')).toBe(false);
  });
});
