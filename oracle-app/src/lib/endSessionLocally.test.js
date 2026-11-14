// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

const caches = vi.hoisted(() => ({ clearPrivateCaches: vi.fn(async () => {}) }));
vi.mock('./clearPrivateCaches.js', () => caches);
const { endSessionLocally } = await import('./endSessionLocally.js');

afterEach(() => { vi.restoreAllMocks(); caches.clearPrivateCaches.mockReset(); });

describe('endSessionLocally (WEB-002)', () => {
  it('still clears the caches and shows sign-in when storage throws', async () => {
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new DOMException('x', 'SecurityError'); });
    const setAuthed = vi.fn();
    await endSessionLocally(setAuthed);
    expect(caches.clearPrivateCaches).toHaveBeenCalledTimes(1);
    expect(setAuthed).toHaveBeenCalledWith(false);
  });

  it('shows sign-in even when clearing the caches fails', async () => {
    caches.clearPrivateCaches.mockRejectedValueOnce(new Error('IndexedDB blocked'));
    const setAuthed = vi.fn();
    await endSessionLocally(setAuthed);
    expect(setAuthed).toHaveBeenCalledWith(false);
  });

  it('forgets the role', async () => {
    sessionStorage.setItem('oracle_role', 'agent');
    await endSessionLocally(vi.fn());
    expect(sessionStorage.getItem('oracle_role')).toBeNull();
  });
});
